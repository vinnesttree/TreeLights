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

from . import __version__, camcal
from .capture import video as videomode
from .capture.session import CaptureSession
from .capture.store import ScanStore
from .config import ConfigStore
from .effects import EFFECTS, Identify, TreeGeometry, make_renderer
from .maps import guidance as guide
from .maps import rescan
from .maps.store import ActivationBlocked, MapStore, export_csv
from .output.manager import OutputManager, PowerLimiter
from .reconstruction.job import run_solve
from .walk import protocol as walkproto
from .walk.job import process_walk
from .walk.session import WalkSession
from .wled import discovery
from .wled.brightness import BrightnessGuard
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
        self.brightness = BrightnessGuard(self.data_dir, self.link)
        if executor is None:
            ctx = multiprocessing.get_context("spawn")
            executor = concurrent.futures.ProcessPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1)), mp_context=ctx)
        self.executor = executor
        self.capture_executor = capture_executor or executor
        self.scans = ScanStore(self.data_dir)
        self.maps = MapStore(self.data_dir)
        self.profiles = camcal.ProfileStore(self.data_dir)
        self.session: CaptureSession | None = None
        self.walk: WalkSession | None = None
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
            # Retention (§13): raw frames of old scans are deleted; detections are kept.
            active = self.maps.active()
            self.scans.prune_frames({active["scan"]} if active else set(), s.raw_frame_retention_scans)
            breaks = self.link.cfg.output_breaks() if self.link.cfg else []
            scan = self.scans.create(s.wled.led_count, {
                "output_breaks": breaks,
                "wled": {"mac": self.link.mac, "led_count": s.wled.led_count, "config_hash": self.fingerprint()},
                "settings": s.capture.model_dump(),
            })
        if self.session:
            asyncio.ensure_future(self.session.stop())
        sess = CaptureSession(scan, self.output, self.capture_executor, s)
        sess.boost_brightness, sess.restore_brightness = self.brightness.boost, self.brightness.restore
        sess.on_status(self.broadcast_status)
        sess.after_view = self._after_view
        self.monitor_unsub = self.monitor.subscribe(sess.on_monitor)
        self.session = sess
        self.guidance = None
        return sess

    def open_walk(self, scan_id: str | None = None) -> WalkSession:
        """A walk-around capture (§7.9), in the given scan or a new one."""
        if self.session and self.session.busy():
            raise HTTPException(409, "A view is being captured; stop it first.")
        if self.walk and self.walk.state == "walking":
            raise HTTPException(409, "A walk is already in progress.")
        s = self.config.settings
        if scan_id:
            scan = self.scans.get(scan_id)
        else:
            active = self.maps.active()
            self.scans.prune_frames({active["scan"]} if active else set(), s.raw_frame_retention_scans)
            breaks = self.link.cfg.output_breaks() if self.link.cfg else []
            scan = self.scans.create(s.wled.led_count, {
                "output_breaks": breaks,
                "wled": {"mac": self.link.mac, "led_count": s.wled.led_count, "config_hash": self.fingerprint()},
                "settings": s.capture.model_dump(),
            })
        level = 48
        if self.session and self.session.scan.id == scan.id and getattr(self.session, "cal", None) is not None:
            lv = [int(v) for v in self.session.cal.levels if v]
            level = int(np.median(lv)) if lv else level
        self.walk = WalkSession(scan, self.output, level=level)
        self.walk.boost_brightness, self.walk.restore_brightness = self.brightness.boost, self.brightness.restore
        return self.walk

    def start_walk_processing(self, scan_id: str, vid: str, video_path: str | None) -> Job:
        scan = self.scans.get(scan_id)
        try:
            view = scan.view(vid)
        except StopIteration:
            raise HTTPException(404, "no such view")
        if view.get("mode") != "walk":
            raise HTTPException(400, "not a walk-around view")
        if self.walk and self.walk.scan.id == scan_id and self.walk.vid == vid and self.walk.state == "walking":
            raise HTTPException(409, "The walk is still in progress.")
        job = Job("walk", scan_id)
        self.jobs[job.id] = job
        s = self.config.settings
        prev = self.maps.active()
        wled = {"mac": self.link.mac, "led_count": s.wled.led_count, "config_hash": self.fingerprint()}
        scan.set_view(vid, status="processing")

        async def run() -> None:
            try:
                m = await asyncio.get_running_loop().run_in_executor(
                    self.executor, process_walk, str(scan.root), vid, s.tree_height_cm, prev, wled, None, video_path)
                job.result = {"map": self.maps.save(m), "quality": m["quality"]}
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                log.exception("walk processing failed")
                job.status, job.error = "failed", str(e)
                scan.set_view(vid, status="failed", error=str(e))
            job.finished = time.time()

        asyncio.ensure_future(run())
        return job

    def fingerprint(self) -> str | None:
        return config_fingerprint(self.link.info, self.link.cfg) if self.link.info else None

    def recover(self) -> dict:
        """After a restart (§7.6): captures that were running when the server stopped.

        - A walk that was in progress is ended where it stopped: its live frames are on
          disk, and the phone, when it reconnects, is told to upload its recording.
        - A tripod view that was being captured is reopened in its scan; the next
          "start view" continues it, keeping everything already captured.
        Only the most recent scan is reopened."""
        out: dict = {"walks": [], "view": None}
        latest = None
        for p in sorted(self.scans.dir.iterdir(), reverse=True):
            try:
                scan = self.scans.get(p.name)
            except FileNotFoundError:
                continue
            for v in scan.meta.get("views", []):
                if v.get("mode") == "walk" and v.get("status") == "walking":
                    scan.set_view(v["id"], status="awaiting_upload", stop_reason="server restarted")
                    scan.events.append({"type": "walk_interrupted", "view": v["id"]})
                    out["walks"].append((scan.id, v["id"]))
                elif v.get("status") == "capturing" and latest is None:
                    latest = (scan, v)
        if latest is not None:
            scan, v = latest
            sess = self.open_session(scan.id)
            sess.interrupted_view = v["id"]
            sess._emit(message=f"View {v['id']} was interrupted when the server stopped. Start view continues it.")
            scan.events.append({"type": "view_interrupted", "view": v["id"]})
            out["view"] = (scan.id, v["id"])
        return out

    def map_check(self) -> str | None:
        """Does the active map still match the controller (§11.4)? A warning, or None.
        Checked at start-up, whenever the state is read, and before activation."""
        m = self.maps.active()
        if not m or not self.link.info:
            return None
        w = m.get("wled") or {}
        mac, n = self.link.info.mac or self.link.mac, self.link.info.led_count
        if w.get("mac") and mac and w["mac"].lower() != mac.lower():
            return (f"The active map was made with another WLED controller (MAC {w['mac']}, now {mac}). "
                    "If the string moved to a new controller with the same wiring, the map still fits; otherwise re-scan.")
        if n and len(m.get("leds", [])) != n:
            return (f"The active map has {len(m['leds'])} LEDs but WLED now drives {n}. "
                    "Check WLED's LED count, or re-scan.")
        fp = self.fingerprint()
        if w.get("config_hash") and fp and w["config_hash"] != fp:
            return ("WLED's LED configuration (outputs, LED count or realtime settings) changed since the active map was made. "
                    "If LEDs now light in a different order, re-scan; otherwise change it back in WLED.")
        return None

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
                f0 = self.profiles.prior_for(scan.meta.get("segments", {}))
                m = await asyncio.get_running_loop().run_in_executor(
                    self.executor, run_solve, str(scan.root), s.tree_height_cm, prev, wled, f0)
                job.result = {"map": self.maps.save(m), "quality": m["quality"]}
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                log.exception("solve failed")
                job.status, job.error = "failed", str(e)
            job.finished = time.time()

        asyncio.ensure_future(run())
        return job

    # ---- test animation --------------------------------------------------------------------
    def start_effect(self, name: str, brightness: float, index: int | list[int] | None = None) -> None:
        owner = "test"
        kind = "identify" if name == "identify" else "test"
        ok, holder = self.output.acquire(owner, kind, ttl=3600)
        if not ok:
            raise HTTPException(409, f"LED output is in use ({holder.kind if holder else '?'}).")
        m = self.maps.active()
        n = self.output.led_count
        tree = TreeGeometry.from_map(m) if m and len(m["leds"]) == n else TreeGeometry.chain_only(n)
        if name == "identify":
            eff = Identify(index if isinstance(index, list) else int(index or 0))
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
        rec = state.recover()
        if rec["view"] or rec["walks"]:
            log.info("recovered after restart: %s", rec)
        if state.link.host:
            try:
                await state.link.refresh(with_cfg=True)
                state._apply_power()
                warning = state.map_check()
                if warning:
                    log.warning("active map: %s", warning)
                if state.brightness.boosted:  # the server stopped during a calibration (§5.4)
                    await state.brightness.restore()
            except Exception as e:  # noqa: BLE001
                log.warning("WLED not reachable at start-up: %s", e)
        state.monitor.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if state.session:
            await state.session.stop()
        if state.walk:
            await state.walk.stop("server shutdown")
        if state.brightness.boosted:
            try:
                await state.brightness.restore()
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't restore WLED brightness; will retry at start-up: %s", e)
        await state.monitor.stop()
        await state.link.close()
        state.output.stop()

    @app.middleware("http")
    async def pin_check(request: Request, call_next):  # noqa: ANN001
        pin = state.config.settings.pin
        if pin and request.url.path.startswith("/api") and request.url.path not in ("/api/auth", "/api/health"):
            if request.headers.get("x-treelights-pin") != pin and request.cookies.get("tl_pin") != pin:
                return JSONResponse({"detail": "PIN required"}, status_code=401)
        return await call_next(request)

    def ws_allowed(ws: WebSocket) -> bool:
        pin = state.config.settings.pin
        return not pin or ws.cookies.get("tl_pin") == pin or ws.query_params.get("pin") == pin

    # ---- general ------------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

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
            "map_warning": state.map_check(),
            "scan": state.session.scan.id if state.session else None,
            "session": state.session.status if state.session else None,
            "rescan": None if not (state.session and state.session.scan.meta.get("rescan")) else
            {k: len(v) if isinstance(v, list) else v for k, v in state.session.scan.meta["rescan"].items()},
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

    # ---- camera calibration (§9.2) -----------------------------------------------------------
    @app.get("/api/camcal/board.png")
    def camcal_board(w: int = 1920, h: int = 1080) -> Response:
        return Response(camcal.board_png(w, h), media_type="image/png")

    @app.post("/api/camcal/frame")
    async def camcal_frame(request: Request, width: int, height: int, device: str = "") -> dict:
        data = await request.body()
        if len(data) > 20 * 2**20:
            raise HTTPException(413, "image too large")
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, state.profiles.add_frame, device or None, width, height, data)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/camcal/solve")
    async def camcal_solve(body: dict = Body(...)) -> dict:
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, state.profiles.solve, body.get("device") or None, int(body["width"]), int(body["height"]))
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/camcal/reset")
    def camcal_reset(body: dict = Body(...)) -> dict:
        state.profiles.reset(body.get("device") or None, int(body["width"]), int(body["height"]))
        return {"ok": True}

    @app.get("/api/camcal/profiles")
    def camcal_profiles() -> dict:
        return state.profiles.profiles()

    # ---- recorded-video mode (§7.7) -------------------------------------------------------------
    MAX_VIDEO = 2 * 2**30

    @app.post("/api/scans/{sid}/video/play")
    async def video_play(sid: str, body: dict = Body(default={})) -> dict:
        try:
            scan = state.scans.get(sid)
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")
        if state.session and state.session.busy():
            raise HTTPException(409, "A live view is being captured.")
        n = scan.meta["led_count"]
        levels = None
        if state.session and state.session.scan.id == sid:
            levels = [int(v) for v in state.session.cal.levels]
        seed = int(time.time()) % 100000
        tl = videomode.Timeline.build(n, seed, levels)
        countdown = float(body.get("countdown_s", 10))
        vid = scan.add_view({"mode": "video", "status": "recording", "timeline": tl.to_json()})
        owner = "video-sequence"
        ok, holder = state.output.acquire(owner, "calibration", ttl=countdown + tl.duration + 30)
        if not ok:
            raise HTTPException(409, f"LED output is in use ({holder.kind if holder else '?'}).")
        state.output.set_renderer(owner, videomode.player(tl, countdown), fps=40)

        async def finish() -> None:
            await asyncio.sleep(countdown + tl.duration + 1.5)
            state.output.release(owner)
            scan.set_view(vid, status="awaiting_upload")

        asyncio.ensure_future(finish())
        return {"view": vid, "countdown_s": countdown, "duration_s": round(tl.duration, 1)}

    def _video_path(sid: str, vid: str) -> Path:
        scan = state.scans.get(sid)
        if not any(v["id"] == vid for v in scan.meta["views"]):
            raise HTTPException(404, "no such view")
        p = scan.root / "videos" / f"{vid}.upload"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @app.get("/api/scans/{sid}/video/{vid}/upload")
    def video_upload_size(sid: str, vid: str) -> dict:
        p = _video_path(sid, vid)
        return {"size": p.stat().st_size if p.exists() else 0}

    @app.put("/api/scans/{sid}/video/{vid}/upload")
    async def video_upload_chunk(sid: str, vid: str, request: Request, offset: int = 0) -> dict:
        """Resumable upload: chunks are appended at `offset`, which must equal the current size."""
        p = _video_path(sid, vid)
        size = p.stat().st_size if p.exists() else 0
        if offset != size:
            raise HTTPException(409, f"offset {offset} != size {size}")
        with open(p, "ab") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_VIDEO:
                    raise HTTPException(413, "video too large")
                f.write(chunk)
        return {"size": size}

    @app.post("/api/scans/{sid}/video/{vid}/decode")
    async def video_decode(sid: str, vid: str) -> dict:
        p = _video_path(sid, vid)
        if not p.exists() or p.stat().st_size == 0:
            raise HTTPException(400, "upload the video first")
        scan = state.scans.get(sid)
        tl_meta = scan.view(vid).get("timeline")
        job = Job("video", sid)
        state.jobs[job.id] = job

        async def run() -> None:
            try:
                job.result = await asyncio.get_running_loop().run_in_executor(
                    state.executor, videomode.decode_job, str(scan.root), vid, str(p), tl_meta)
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                job.status, job.error = "failed", str(e)
            job.finished = time.time()

        asyncio.ensure_future(run())
        return job.to_dict()

    # ---- re-scan and maintenance (§11.5) --------------------------------------------------------
    def _scan_kind(scan) -> str:  # noqa: ANN001
        modes = [v.get("mode") for v in scan.meta["views"] if v.get("status") in ("done", "processed")]
        return "walk" if modes and all(m == "walk" for m in modes) else "tripod"

    @app.get("/api/maps/{mid}/rescan")
    def rescan_info(mid: str) -> dict:
        m = get_map(mid)
        low = rescan.low_confidence(m)
        try:
            kind = _scan_kind(state.scans.get(m["scan"]))
        except FileNotFoundError:
            kind = None
        return {"map": mid, "scan": m["scan"], "kind": kind, "low_confidence": low, "n": len(m["leds"])}

    @app.post("/api/maps/{mid}/rescan")
    async def rescan_setup(mid: str, body: dict = Body(default={})) -> dict:
        """Choose LEDs to re-scan; sets up the capture in the map's scan. Tripod scans
        get a pending re-scan view (started from Control or Calibrate); walk scans are
        re-scanned by walking again into the same scan (the Scan page with ?scan=)."""
        m = get_map(mid)
        n = len(m["leds"])
        try:
            if body.get("low_confidence"):
                leds = rescan.low_confidence(m)
            elif body.get("text") is not None:
                leds = rescan.parse_selection(str(body["text"]), n)
            else:
                leds = sorted({int(i) for i in body.get("leds", []) if 0 <= int(i) < n})
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not leds:
            raise HTTPException(400, "No LEDs selected.")
        try:
            scan = state.scans.get(m["scan"])
        except FileNotFoundError:
            raise HTTPException(404, "The scan this map was built from no longer exists.")
        kind = _scan_kind(scan)
        out = {"map": mid, "scan": scan.id, "kind": kind, "leds": leds}
        if kind == "tripod":
            anc = rescan.anchors(m, exclude=set(leds))
            sess = state.session if state.session and state.session.scan.id == scan.id else state.open_session(scan.id)
            sess.scan.meta["rescan"] = {"map": mid, "leds": leds, "anchors": anc, "capture": sorted(set(leds) | set(anc))}
            sess.scan.save()
            out["anchors"] = anc
        return out

    @app.post("/api/leds/show")
    def leds_show(body: dict = Body(default={})) -> dict:
        """Blink a set of LEDs on the tree (e.g. the ones about to be re-scanned)."""
        n = state.output.led_count
        leds = sorted({int(i) for i in body.get("leds", []) if 0 <= int(i) < n})
        if not leds:
            raise HTTPException(400, "No LEDs given.")
        state.start_effect("identify", float(body.get("brightness", 0.6)), leds)
        return {"ok": True, "count": len(leds)}

    # ---- walk-around capture (§7.9) -----------------------------------------------------------
    @app.post("/api/scans/{sid}/walk/{vid}/process")
    async def walk_process(sid: str, vid: str, body: dict = Body(default={})) -> dict:
        """Decode the walk's live frames and uploaded recording (if any), then solve."""
        try:
            p = _video_path(sid, vid)
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")
        use_video = body.get("video", True) and p.exists() and p.stat().st_size > 0
        return state.start_walk_processing(sid, vid, str(p) if use_video else None).to_dict()

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

    @app.websocket("/ws/walk")
    async def ws_walk(ws: WebSocket) -> None:
        """The phone during a walk-around capture (protocol: walk/protocol.py)."""
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
        walk = state.walk
        resume = walk is not None and walk.state == "walking" and hello.get("view") == walk.vid
        if not resume and hello.get("view") and hello.get("scan"):
            # The phone was walking when the server stopped: that walk has ended; tell the
            # phone to upload its recording and process what was captured.
            try:
                old = state.scans.get(hello["scan"]).view(hello["view"])
            except (FileNotFoundError, StopIteration):
                old = None
            if old and old.get("mode") == "walk" and old.get("status") == "awaiting_upload":
                sid, vid = hello["scan"], hello["view"]
                await send({"type": "welcome", "scan": sid, "view": vid, "n": state.output.led_count,
                            **walkproto.timing_info(), "resumed": False})
                await send({"type": "stopped", "scan": sid, "view": vid, "reason": old.get("stop_reason", "ended"),
                            "upload": f"/api/scans/{sid}/video/{vid}/upload", "process": f"/api/scans/{sid}/walk/{vid}/process"})
                await ws.close()
                return
        try:
            if not resume:
                walk = state.open_walk(hello.get("scan"))
        except (HTTPException, FileNotFoundError) as e:
            await send({"type": "error", "message": getattr(e, "detail", None) or "no such scan"})
            await ws.close()
            return
        walk.send = send
        walk.disconnected_at = None
        camera = hello.get("camera") or {}
        await send({"type": "welcome", "scan": walk.scan.id, "view": walk.vid, "n": walk.n, **walkproto.timing_info(),
                    "resumed": resume})
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    try:
                        header, jpeg = walkproto.unpack_frame(msg["bytes"])
                        walk.on_frame(header, jpeg)
                    except (ValueError, KeyError):
                        pass
                    continue
                if msg.get("text") is None:
                    continue
                try:
                    cmd = json.loads(msg["text"])
                except ValueError:
                    continue
                t = cmd.get("type")
                try:
                    if t == "start":
                        vid = walk.start(cmd.get("camera") or camera)
                        await send({"type": "started", "scan": walk.scan.id, "view": vid})
                    elif t == "motion":
                        walk.on_motion(cmd.get("t") or [], cmd.get("q") or [])
                    elif t == "rec_start":
                        walk.on_rec_start(float(cmd["t"]))
                    elif t == "stop":
                        await walk.stop(cmd.get("reason", "stopped"))
                        await send({"type": "stopped", "scan": walk.scan.id, "view": walk.vid,
                                    "upload": f"/api/scans/{walk.scan.id}/video/{walk.vid}/upload",
                                    "process": f"/api/scans/{walk.scan.id}/walk/{walk.vid}/process"})
                    elif t == "ping":
                        await send({"type": "pong", "t": cmd.get("t")})
                except (RuntimeError, HTTPException, KeyError, ValueError) as e:
                    await send({"type": "error", "message": getattr(e, "detail", None) or str(e)})
        except WebSocketDisconnect:
            pass
        finally:
            if walk.send is send:
                walk.send = None
                if walk.state == "walking":
                    # A Wi-Fi blip shouldn't end the walk: the phone reconnects with the view id.
                    walk.disconnected_at = time.monotonic()

                    async def grace(w=walk) -> None:
                        await asyncio.sleep(walkproto.RECONNECT_GRACE_S)
                        if w.send is None and w.state == "walking":
                            await w.stop("phone disconnected")

                    asyncio.ensure_future(grace())

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
                        mode = cmd.get("mode", "hybrid")
                        leds = None
                        if mode == "rescan":
                            leds = (sess.scan.meta.get("rescan") or {}).get("capture")
                            if not leds:
                                raise RuntimeError("No re-scan is set up for this scan. Choose LEDs on the Map page first.")
                        resume = getattr(sess, "interrupted_view", None)
                        if resume and mode != "rescan":
                            # Continue the view the server was capturing when it stopped (§7.6).
                            sess.interrupted_view = None
                            view = sess.scan.view(resume)
                            sess.start_view(view.get("mode", mode), view_id=resume, leds=view.get("leds"))
                        else:
                            sess.start_view(mode, leds=leds)
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
