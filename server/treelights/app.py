"""FastAPI application: REST API, WebSockets, static web app (architecture §15, §16)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import multiprocessing
import os
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from . import __version__
from .capture.session import CaptureSession
from .capture.store import ScanStore
from .config import ConfigStore
from .effects import EFFECTS, Identify, TreeGeometry, make_renderer
from .maps import guidance as guide
from .maps.store import ActivationBlocked, MapStore, export_csv
from .output.manager import OutputManager, PowerLimiter
from .reconstruction.job import run_solve
from .wled import discovery
from .wled.client import config_fingerprint
from .wled.ddp import UdpFrameSender
from .wled.link import WledLink, WledMonitor

log = logging.getLogger(__name__)


class Job:
    def __init__(self, kind: str, scan: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.scan = scan
        self.status = "running"
        self.result: Any = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "scan": self.scan, "status": self.status, "result": self.result,
                "error": self.error, "started": self.started, "finished": self.finished}


class AppState:
    def __init__(self, data_dir: Path, executor: concurrent.futures.Executor | None = None,
                 capture_executor: concurrent.futures.Executor | None = None):
        self.data_dir = Path(data_dir)
        self.config = ConfigStore(self.data_dir)
        s = self.config.settings
        self.output = OutputManager(s.wled.led_count, PowerLimiter(None, s.power.ma_per_channel, s.power.idle_ma_per_led))
        self.link = WledLink(s.wled.host, s.wled.mac, s.wled.http_port)
        self.link.on_host_change(lambda h: self._apply_sender())
        self.monitor = WledMonitor(self.link, lambda: self.output.holder() is not None)
        if executor is None:
            ctx = multiprocessing.get_context("spawn")
            executor = concurrent.futures.ProcessPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1)), mp_context=ctx)
        self.executor = executor
        self.capture_executor = capture_executor or executor
        self.scans = ScanStore(self.data_dir)
        self.maps = MapStore(self.data_dir)
        self.session: CaptureSession | None = None
        self.jobs: dict[str, Job] = {}
        self.guidance: dict | None = None
        self.control_clients: set[asyncio.Queue] = set()
        self.test_owner: str | None = None
        self._apply_sender()

    # ---- output configuration ------------------------------------------------
    def _apply_sender(self) -> None:
        s = self.config.settings
        host = self.link.host or s.wled.host
        self.output.set_led_count(s.wled.led_count) if self.output.led_count != s.wled.led_count else None
        self.output.set_sender(UdpFrameSender(host, s.wled.protocol, s.wled.udp_port) if host else None)
        self._apply_power()

    def _apply_power(self) -> None:
        s = self.config.settings
        lim = s.power.max_current_ma
        if lim is None and self.link.info and self.link.info.max_current_ma:
            lim = self.link.info.max_current_ma
        self.output.power = PowerLimiter(lim or None, s.power.ma_per_channel, s.power.idle_ma_per_led)

    # ---- sessions ----------------------------------------------------------------
    def open_session(self, scan_id: str | None = None) -> CaptureSession:
        if self.session and self.session.busy():
            raise HTTPException(409, "A view is being captured; stop it first.")
        s = self.config.settings
        if scan_id:
            scan = self.scans.get(scan_id)
        else:
            breaks = self.link.cfg.output_breaks() if self.link.cfg else []
            scan = self.scans.create(s.wled.led_count, {
                "output_breaks": breaks,
                "wled": {"mac": self.link.mac, "led_count": s.wled.led_count, "config_hash": self.fingerprint()},
                "settings": s.capture.model_dump(),
            })
        if self.session:
            asyncio.ensure_future(self.session.stop())
        sess = CaptureSession(scan, self.output, self.capture_executor, s)
        sess.on_status(self.broadcast_status)
        sess.after_view = self._after_view
        self.monitor_unsub = self.monitor.subscribe(sess.on_monitor)
        self.session = sess
        self.guidance = None
        return sess

    def fingerprint(self) -> str | None:
        return config_fingerprint(self.link.info, self.link.cfg) if self.link.info else None

    def broadcast_status(self, st: dict) -> None:
        msg = dict(st, guidance=self.guidance)
        for q in list(self.control_clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    async def _after_view(self, vid: str) -> None:
        """Quick reconstruction for coverage guidance (§7.8)."""
        sess = self.session
        if not sess:
            return
        done_views = [v for v in sess.scan.meta["views"] if v.get("status") == "done"]
        if len(done_views) < 2:
            self.guidance = {"message": "Turn the tree about 45° and start the next view.", "turn_deg": 45.0}
            self.broadcast_status(sess.status)
            return
        try:
            m = await asyncio.get_running_loop().run_in_executor(
                self.executor, run_solve, str(sess.scan.root), None, None, None)
        except Exception as e:  # noqa: BLE001
            self.guidance = {"message": f"Couldn't build a preview map yet ({e}). Turn the tree about 45° and continue."}
            self.broadcast_status(sess.status)
            return
        last_seg = sess.scan.view(vid)["segments"][-1]
        self.guidance = guide.guidance(m, last_seg)
        target = self.guidance.get("target_azimuth")
        if target is not None:
            vis = guide.predicted_visible(m, target)
            sess.predicted_visible = lambda _vid, vis=vis: vis
        self.broadcast_status(sess.status)

    # ---- jobs --------------------------------------------------------------------------
    def start_solve(self, scan_id: str) -> Job:
        scan = self.scans.get(scan_id)
        job = Job("solve", scan_id)
        self.jobs[job.id] = job
        s = self.config.settings
        prev = self.maps.active()
        wled = {"mac": self.link.mac, "led_count": s.wled.led_count, "config_hash": self.fingerprint()}

        async def run() -> None:
            try:
                m = await asyncio.get_running_loop().run_in_executor(
                    self.executor, run_solve, str(scan.root), s.tree_height_cm, prev, wled)
                job.result = {"map": self.maps.save(m), "quality": m["quality"]}
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                log.exception("solve failed")
                job.status, job.error = "failed", str(e)
            job.finished = time.time()

        asyncio.ensure_future(run())
        return job

    # ---- test animation --------------------------------------------------------------------
    def start_effect(self, name: str, brightness: float, index: int | None = None) -> None:
        owner = "test"
        kind = "identify" if name == "identify" else "test"
        ok, holder = self.output.acquire(owner, kind, ttl=3600)
        if not ok:
            raise HTTPException(409, f"LED output is in use ({holder.kind if holder else '?'}).")
        m = self.maps.active()
        n = self.output.led_count
        tree = TreeGeometry.from_map(m) if m and len(m["leds"]) == n else TreeGeometry.chain_only(n)
        if name == "identify":
            eff = Identify(int(index or 0))
        else:
            if name not in EFFECTS:
                raise HTTPException(404, f"unknown effect {name}")
            if name != "chase" and not tree.has_pos.any():
                name = "chase"  # no map yet: index chase
            eff = EFFECTS[name]()
        eff.setup(tree)
        self.output.set_renderer(owner, make_renderer(eff, brightness), fps=40)
        self.test_owner = owner

    def stop_effect(self) -> None:
        self.output.release("test")


def create_app(state: AppState, web_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="TreeLights", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.tl = state

    @app.on_event("startup")
    async def _startup() -> None:
        if state.link.host:
            try:
                await state.link.refresh(with_cfg=True)
                state._apply_power()
            except Exception as e:  # noqa: BLE001
                log.warning("WLED not reachable at start-up: %s", e)
        state.monitor.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if state.session:
            await state.session.stop()
        await state.monitor.stop()
        state.output.stop()

    @app.middleware("http")
    async def pin_check(request: Request, call_next):  # noqa: ANN001
        pin = state.config.settings.pin
        if pin and request.url.path.startswith("/api") and request.url.path != "/api/auth":
            if request.headers.get("x-treelights-pin") != pin and request.cookies.get("tl_pin") != pin:
                return JSONResponse({"detail": "PIN required"}, status_code=401)
        return await call_next(request)

    def ws_allowed(ws: WebSocket) -> bool:
        pin = state.config.settings.pin
        return not pin or ws.cookies.get("tl_pin") == pin or ws.query_params.get("pin") == pin

    # ---- general ------------------------------------------------------------------------
    @app.post("/api/auth")
    def auth(body: dict = Body(...)) -> Response:
        pin = state.config.settings.pin
        if pin and body.get("pin") != pin:
            raise HTTPException(401, "wrong PIN")
        r = JSONResponse({"ok": True})
        if pin:
            r.set_cookie("tl_pin", pin, httponly=True, samesite="strict", secure=True, max_age=3600 * 24 * 90)
        return r

    @app.get("/api/state")
    def get_state() -> dict:
        lease = state.output.holder()
        info = state.link.info
        return {
            "version": __version__,
            "wled": {"host": state.link.host, "mac": state.link.mac, "version": info.version if info else None,
                     "led_count": info.led_count if info else None, "monitor_ok": state.monitor.last_ok},
            "led_count": state.output.led_count,
            "lease": None if not lease else {"owner": lease.owner, "kind": lease.kind},
            "active_map": state.maps.active_id(),
            "scan": state.session.scan.id if state.session else None,
            "session": state.session.status if state.session else None,
            "guidance": state.guidance,
            "tree_height_cm": state.config.settings.tree_height_cm,
        }

    @app.get("/api/config")
    def get_config() -> dict:
        d = state.config.settings.model_dump()
        d["pin"] = bool(d["pin"])
        return d

    @app.put("/api/config")
    async def put_config(patch: dict = Body(...)) -> dict:
        if "pin" in patch and patch["pin"] in (True, False):
            patch.pop("pin")
        s = state.config.update(patch)
        if "wled" in patch:
            if s.wled.host:
                state.link.set_host(s.wled.host, s.wled.mac, s.wled.http_port)
            state._apply_sender()
        if "power" in patch:
            state._apply_power()
        return get_config()

    # ---- WLED ----------------------------------------------------------------------------
    @app.get("/api/wled/discover")
    async def wled_discover() -> list[dict]:
        return await discovery.discover(timeout=3.0)

    @app.post("/api/wled/select")
    async def wled_select(body: dict = Body(...)) -> dict:
        host = str(body.get("host", "")).strip()
        if not host:
            raise HTTPException(400, "host required")
        state.link.set_host(host, port=state.config.settings.wled.http_port)
        try:
            info = await state.link.refresh(with_cfg=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Can't reach WLED at {host}: {e}")
        patch: dict = {"wled": {"host": host, "mac": info.mac}}
        if info.led_count:
            patch["wled"]["led_count"] = info.led_count
        state.config.update(patch)
        state._apply_sender()
        return {"host": host, "mac": info.mac, "led_count": info.led_count, "version": info.version}

    @app.get("/api/wled/checks")
    async def wled_checks() -> list[dict]:
        if not state.link.host:
            return [{"name": "reachable", "status": "fail", "detail": "No controller selected."}]
        owner = "setup-check"

        async def probe() -> None:
            ok, _ = state.output.acquire(owner, "identify", ttl=3)
            if ok:
                f = np.zeros((state.output.led_count, 3), np.uint8)
                f[0] = (8, 8, 8)
                await asyncio.wrap_future(state.output.show(owner, f, repeats=2))

        try:
            checks = await state.link.setup_checks(state.config.settings.wled.led_count, probe)
        finally:
            state.output.release(owner)
        state._apply_power()
        return [c.__dict__ for c in checks]

    @app.post("/api/wled/identify")
    def wled_identify(body: dict = Body(...)) -> dict:
        state.start_effect("identify", float(body.get("brightness", 0.6)), int(body.get("index", 0)))
        return {"ok": True}

    # ---- scans ---------------------------------------------------------------------------
    @app.get("/api/scans")
    def scans() -> list[dict]:
        return state.scans.list()

    @app.post("/api/scans")
    async def new_scan() -> dict:
        sess = state.open_session()
        return {"scan": sess.scan.id}

    @app.post("/api/scans/{sid}/open")
    async def open_scan(sid: str) -> dict:
        try:
            sess = state.open_session(sid)
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")
        return {"scan": sess.scan.id}

    @app.get("/api/scans/{sid}")
    def scan_detail(sid: str) -> dict:
        try:
            scan = state.scans.get(sid)
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")
        obs = scan.observations()
        seen = {l for s in obs.values() for l, c in s.items() if c}
        return {"meta": scan.meta, "seen_leds": len(seen), "overrides": scan.overrides()}

    @app.get("/api/scans/{sid}/overrides")
    def get_overrides(sid: str) -> dict:
        return state.scans.get(sid).overrides()

    @app.put("/api/scans/{sid}/overrides")
    def put_overrides(sid: str, body: dict = Body(...)) -> dict:
        allowed = {"bad", "positions", "breaks", "links", "cap", "locked"}
        clean = {k: v for k, v in body.items() if k in allowed}
        state.scans.get(sid).set_overrides(clean)
        return clean

    @app.post("/api/scans/{sid}/solve")
    async def solve_scan(sid: str) -> dict:
        try:
            return state.start_solve(sid).to_dict()
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")

    @app.get("/api/jobs/{jid}")
    def job(jid: str) -> dict:
        j = state.jobs.get(jid)
        if not j:
            raise HTTPException(404, "no such job")
        return j.to_dict()

    # ---- maps -----------------------------------------------------------------------------
    @app.get("/api/maps")
    def maps() -> list[dict]:
        return state.maps.list()

    @app.get("/api/maps/active")
    def active_map() -> dict:
        m = state.maps.active()
        if not m:
            raise HTTPException(404, "no active map")
        return m

    @app.get("/api/maps/{mid}")
    def get_map(mid: str) -> dict:
        try:
            return state.maps.get(mid)
        except FileNotFoundError:
            raise HTTPException(404, "no such map")

    @app.post("/api/maps/{mid}/activate")
    def activate(mid: str, body: dict = Body(default={})) -> dict:
        try:
            return state.maps.activate(mid, state.fingerprint(), force=bool(body.get("force")))
        except ActivationBlocked as e:
            raise HTTPException(409, str(e))
        except FileNotFoundError:
            raise HTTPException(404, "no such map")

    @app.post("/api/maps/rollback")
    def rollback() -> dict:
        return {"active": state.maps.rollback()}

    @app.get("/api/maps/{mid}/export")
    def export(mid: str, fmt: str = "json") -> Response:
        m = get_map(mid)
        if fmt == "csv":
            return PlainTextResponse(export_csv(m), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="treelights-{mid}.csv"'})
        return JSONResponse(m, headers={"Content-Disposition": f'attachment; filename="treelights-{mid}.json"'})

    # ---- test animation ------------------------------------------------------------------
    @app.post("/api/test/start")
    def test_start(body: dict = Body(default={})) -> dict:
        state.start_effect(body.get("effect", "sweep"), float(body.get("brightness", 0.5)))
        return {"ok": True}

    @app.post("/api/test/stop")
    def test_stop() -> dict:
        state.stop_effect()
        return {"ok": True}

    # ---- WebSockets ----------------------------------------------------------------------
    @app.websocket("/ws/capture")
    async def ws_capture(ws: WebSocket) -> None:
        if not ws_allowed(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        lock = asyncio.Lock()

        async def send(msg: dict) -> None:
            async with lock:
                await ws.send_json(msg)

        try:
            hello = await ws.receive_json()
        except (WebSocketDisconnect, ValueError):
            return
        sess = state.session or state.open_session()
        await sess.camera_connected(send, hello)
        unsub = sess.on_status(lambda st: asyncio.ensure_future(_safe(send, dict(st, guidance=state.guidance))))
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    try:
                        await sess.on_camera_text(json.loads(msg["text"]))
                    except ValueError:
                        pass
                elif msg.get("bytes") is not None:
                    try:
                        sess.on_camera_binary(msg["bytes"])
                    except ValueError:
                        pass
        except WebSocketDisconnect:
            pass
        finally:
            unsub()
            if state.session is sess:
                sess.camera_disconnected("websocket closed")

    @app.websocket("/ws/control")
    async def ws_control(ws: WebSocket) -> None:
        if not ws_allowed(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        state.control_clients.add(q)
        if state.session:
            q.put_nowait(dict(state.session.status, guidance=state.guidance))

        async def pump() -> None:
            while True:
                await ws.send_json(await q.get())

        pt = asyncio.ensure_future(pump())
        try:
            while True:
                cmd = await ws.receive_json()
                t = cmd.get("type")
                try:
                    if t == "start_view":
                        sess = state.session or state.open_session()
                        delay = float(cmd.get("delay_s", 0))
                        if delay:
                            await asyncio.sleep(min(delay, 10))
                        sess.start_view(cmd.get("mode", "hybrid"))
                    elif t == "stop" and state.session:
                        await state.session.stop()
                    elif t == "resume" and state.session:
                        state.session.resume()
                    elif t == "new_scan":
                        state.open_session()
                        await ws.send_json({"type": "scan", "scan": state.session.scan.id})
                except (RuntimeError, HTTPException) as e:
                    await ws.send_json({"type": "error", "message": getattr(e, "detail", None) or str(e)})
        except (WebSocketDisconnect, ValueError):
            pass
        finally:
            pt.cancel()
            state.control_clients.discard(q)

    @app.websocket("/ws/preview")
    async def ws_preview(ws: WebSocket) -> None:
        if not ws_allowed(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        last = [0.0]

        def on_frame(frame: np.ndarray) -> None:  # output thread
            now = time.monotonic()
            if now - last[0] < 0.05:
                return
            last[0] = now
            data = frame.tobytes()

            def put() -> None:
                if not q.full():
                    q.put_nowait(data)

            loop.call_soon_threadsafe(put)

        state.output.add_listener(on_frame)
        try:
            while True:
                await ws.send_bytes(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if on_frame in state.output._listeners:
                state.output._listeners.remove(on_frame)

    # ---- static web app ------------------------------------------------------------------
    if web_dir and Path(web_dir).is_dir():
        root = Path(web_dir).resolve()

        @app.get("/{path:path}")
        def static(path: str) -> Response:
            p = (root / path).resolve()
            if path and p.is_file() and root in p.parents:
                return FileResponse(p)
            return FileResponse(root / "index.html")

    return app


async def _safe(send, msg: dict) -> None:  # noqa: ANN001
    try:
        await send(msg)
    except Exception:  # noqa: BLE001
        pass
