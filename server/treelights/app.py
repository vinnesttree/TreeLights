"""FastAPI application: REST API, WebSockets, static web app (architecture §15, §16).

Several trees can be served at once (§5.5). Every tree-scoped route acts on the tree
named by the `tree` query parameter (or the `X-TreeLights-Tree` header, or the
`/api/trees/<id>/...` and `/ws/trees/<id>/...` path prefix); without one, on the default
tree, so clients written for a single tree keep working.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import logging
import multiprocessing
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.requests import HTTPConnection
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError

from . import __version__, camcal, progress, storage
from .capture import video as videomode
from .config import TREES_DIR, ConfigStore, tree_dir
from .effects import catalogue, library
from .lights import LightsBusy
from .maps import rescan
from .maps.store import ActivationBlocked, export_csv
from .tree import Tree
from .walk import protocol as walkproto
from .wled import discovery

log = logging.getLogger(__name__)


class Job:
    def __init__(self, kind: str, scan: str, progress_dir: Path | None = None, view: str | None = None,
                 tree: str | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.scan = scan
        self.view = view
        self.tree = tree
        self.status = "running"
        self.result: Any = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        # The worker process writes its progress here (treelights.progress).
        self.progress_path: str | None = None
        if progress_dir is not None:
            progress_dir.mkdir(parents=True, exist_ok=True)
            self.progress_path = str(progress_dir / f"{self.id}.json")

    def finish(self) -> None:
        self.finished = time.time()
        if self.progress_path:
            Path(self.progress_path).unlink(missing_ok=True)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "scan": self.scan, "view": self.view, "tree": self.tree,
                "status": self.status, "result": self.result, "error": self.error, "started": self.started,
                "finished": self.finished, "now": time.time(),
                "progress": progress.read(self.progress_path) if self.status == "running" else None}


# Attributes of the default tree that AppState passes through, so code and tests written
# for a single tree (`state.output`, `state.lights`, ...) keep working.
_TREE_ATTRS = frozenset({
    "output", "link", "links", "monitor", "monitors", "brightness", "scans", "maps", "session", "walk", "lights",
    "scheduler", "ha", "guidance", "control_clients", "test_owner", "effect_renderer", "index", "effect_state",
    "start_effect", "stop_effect", "update_effect", "has_map", "fingerprint", "map_check", "recover", "open_session",
    "open_walk", "start_solve", "start_walk_processing", "broadcast_status", "_apply_sender", "_apply_power",
    "reconfigure", "wled_meta",
})


class AppState:
    def __init__(self, data_dir: Path, executor: concurrent.futures.Executor | None = None,
                 capture_executor: concurrent.futures.Executor | None = None):
        self.data_dir = Path(data_dir)
        self.config = ConfigStore(self.data_dir)  # also moves a single-tree install into tree 1 (config.migrate)
        if executor is None:
            ctx = multiprocessing.get_context("spawn")
            executor = concurrent.futures.ProcessPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1)), mp_context=ctx)
        self.executor = executor
        self.capture_executor = capture_executor or executor
        self.profiles = camcal.ProfileStore(self.data_dir)
        self.jobs: dict[str, Job] = {}
        for stale in (self.data_dir / "jobs").glob("*.json*"):  # progress of jobs from before a restart
            stale.unlink(missing_ok=True)
        self._started = False
        self.trees: dict[str, Tree] = {}
        for tid in self.config.tree_ids():
            self.trees[tid] = Tree(self, tid)

    def __getattr__(self, name: str) -> Any:
        if name in _TREE_ATTRS and "trees" in self.__dict__:
            return getattr(self.default, name)
        raise AttributeError(name)

    # ---- trees -----------------------------------------------------------------------
    @property
    def default(self) -> Tree:
        return self.trees[self.config.default_tree]

    def tree(self, tid: str | None = None) -> Tree:
        """A tree by id (the default tree for None). KeyError if there is none."""
        return self.default if not tid else self.trees[tid]

    def trees_summary(self) -> list[dict]:
        return [t.summary() for t in self.trees.values()]

    async def add_tree(self, name: str | None = None) -> Tree:
        tid = self.config.add_tree(name)
        t = self.trees[tid] = Tree(self, tid)
        if self._started:
            await t.start()
        return t

    async def remove_tree(self, tid: str) -> None:
        """Remove a tree from the configuration. Its folder is kept, renamed to
        trees/.removed/<id>-<time>, so no scan or map is lost."""
        t = self.trees.get(tid)
        if t is None:
            raise KeyError(tid)
        if tid == self.config.default_tree:
            raise ValueError("The first tree can't be removed.")
        if t.busy():
            raise ValueError(f"{t.name} is being scanned; stop the scan first.")
        self.config.remove_tree(tid)
        del self.trees[tid]
        await t.ha.stop(remove_entities=True)  # its light disappears from Home Assistant
        await t.stop()
        src = tree_dir(self.data_dir, tid)
        if src.exists():
            dst = self.data_dir / TREES_DIR / ".removed" / f"{tid}-{dt.datetime.now():%Y-%m-%dT%H-%M-%S}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)

    async def start(self) -> None:
        self._started = True
        for t in list(self.trees.values()):
            await t.start()

    async def stop(self) -> None:
        for t in list(self.trees.values()):
            await t.stop()


TREE_PREFIX = re.compile(r"^/(api|ws)/trees/([A-Za-z0-9_-]{1,32})(/.+)$")


class TreePathPrefix:
    """`/api/trees/<id>/<route>` is `/api/<route>?tree=<id>` (and the same for `/ws/`)."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            m = TREE_PREFIX.match(scope["path"])
            if m:
                path = f"/{m[1]}{m[3]}"
                qs = scope.get("query_string", b"")
                scope = dict(scope, path=path, raw_path=path.encode(),
                             query_string=(qs + b"&" if qs else b"") + b"tree=" + m[2].encode())
        await self.app(scope, receive, send)


def _tree_id(conn: HTTPConnection) -> str | None:
    return conn.query_params.get("tree") or conn.headers.get("x-treelights-tree") or None
def create_app(state: AppState, web_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="TreeLights", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.tl = state
    app.add_middleware(TreePathPrefix)

    def current_tree(conn: HTTPConnection) -> Tree:
        """The tree a request acts on (see the module docstring)."""
        tid = _tree_id(conn)
        try:
            return state.tree(tid)
        except KeyError:
            raise HTTPException(404, f"no tree {tid!r}")

    def ws_tree(ws: WebSocket) -> Tree | None:
        try:
            return state.tree(_tree_id(ws))
        except KeyError:
            return None

    @app.on_event("startup")
    async def _startup() -> None:
        await state.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await state.stop()

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
    def get_state(t: Tree = Depends(current_tree)) -> dict:
        lease = t.output.holder()
        info = t.link.info
        s = t.config.settings
        return {
            "version": __version__,
            "tree": {"id": t.id, "name": s.tree_name},
            "trees": [{"id": x.id, "name": x.name} for x in state.trees.values()],
            "wled": {"host": t.link.host, "mac": t.link.mac, "version": info.version if info else None,
                     "led_count": info.led_count if info else None, "monitor_ok": t.monitor.last_ok},
            "controllers": [{"host": l.host, "mac": l.mac, "name": c.name or (l.info.name if l.info else None),
                             "version": l.info.version if l.info else None,
                             "led_count": l.info.led_count if l.info else None, "start": sl.start,
                             "count": sl.count, "monitor_ok": m.last_ok}
                            for l, c, sl, m in zip(t.links, s.controllers, t.index.slices(), t.monitors)],
            "led_count": t.output.led_count,
            "lease": None if not lease else {"owner": lease.owner, "kind": lease.kind},
            "active_map": t.maps.active_id(),
            "map_warning": t.map_check(),
            "scan": t.session.scan.id if t.session else None,
            "session": t.session.status if t.session else None,
            "rescan": None if not (t.session and t.session.scan.meta.get("rescan")) else
            {k: len(v) if isinstance(v, list) else v for k, v in t.session.scan.meta["rescan"].items()},
            "guidance": t.guidance,
            "tree_height_cm": s.tree_height_cm,
            "lights": {"on": t.effect_state() is not None},
            "next_timer": t.scheduler.next_event(),
        }

    def _config_out(t: Tree) -> dict:
        d = t.config.settings.model_dump()
        d["pin"] = bool(d["pin"])
        d["mqtt"]["password"] = bool(d["mqtt"]["password"])  # secrets are written, never read back
        return d

    @app.get("/api/config")
    def get_config(t: Tree = Depends(current_tree)) -> dict:
        """The tree's settings and the server-wide ones, in the single-tree shape: `wled` is
        the tree's first controller, `controllers` all of them."""
        return _config_out(t)

    async def _after_config_change(t: Tree, patch: dict) -> None:
        if "mqtt" in patch:  # the broker is shared by all trees; the topic is the tree's own
            for x in list(state.trees.values()):
                await x.ha.apply()
        if "schedule" in patch:
            t.scheduler.poke()
        if "wled" in patch or "controllers" in patch:
            s = t.config.settings
            t.reconfigure()
            if "wled" in patch and s.wled.host:
                t.link.set_host(s.wled.host, s.wled.mac, s.wled.http_port)
            t._apply_sender()
        if "power" in patch:
            t._apply_power()

    def _refuse_while_busy(t: Tree, patch: dict) -> None:
        if ("wled" in patch or "controllers" in patch) and t.busy():
            raise HTTPException(409, f"{t.name} is being scanned; change its controllers after the scan.")

    @app.put("/api/config")
    async def put_config(patch: dict = Body(...), t: Tree = Depends(current_tree)) -> dict:
        if "pin" in patch and patch["pin"] in (True, False):
            patch.pop("pin")
        if isinstance(patch.get("mqtt"), dict) and patch["mqtt"].get("password") in (True, False):
            patch["mqtt"].pop("password")  # the masked value from GET: unchanged
        _refuse_while_busy(t, patch)
        try:
            t.config.update(patch)
        except ValidationError as e:
            raise HTTPException(422, _validation_message(e))
        await _after_config_change(t, patch)
        return _config_out(t)

    # ---- trees (§5.5) ----------------------------------------------------------------------
    @app.get("/api/trees")
    def trees_list() -> list[dict]:
        return state.trees_summary()

    @app.post("/api/trees")
    async def trees_add(body: dict = Body(default={})) -> dict:
        try:
            t = await state.add_tree((body.get("name") or "").strip() or None)
        except ValidationError as e:
            raise HTTPException(422, _validation_message(e))
        return t.summary()

    @app.put("/api/trees/{tid}")
    def trees_rename(tid: str, body: dict = Body(...)) -> dict:
        if tid not in state.trees:
            raise HTTPException(404, f"no tree {tid!r}")
        try:
            state.config.rename_tree(tid, str(body.get("name", "")))
        except ValidationError as e:
            raise HTTPException(422, _validation_message(e))
        state.trees[tid].ha.changed()
        return state.trees[tid].summary()

    @app.delete("/api/trees/{tid}")
    async def trees_remove(tid: str) -> dict:
        try:
            await state.remove_tree(tid)
        except KeyError:
            raise HTTPException(404, f"no tree {tid!r}")
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    # ---- controllers of a tree (§5.5) --------------------------------------------------------
    @app.get("/api/controllers")
    def controllers_get(t: Tree = Depends(current_tree)) -> list[dict]:
        s = t.config.settings
        return [{**c.model_dump(), "start": sl.start, "last": sl.stop - 1,
                 "wled": None if not l.info else {"name": l.info.name, "version": l.info.version,
                                                  "led_count": l.info.led_count, "mac": l.info.mac}}
                for c, sl, l in zip(s.controllers, t.index.slices(), t.links)]

    @app.put("/api/controllers")
    async def controllers_put(body: list[dict] = Body(...), t: Tree = Depends(current_tree)) -> list[dict]:
        """Replace the tree's controllers. Their order is the order of the LED indices:
        the first controller drives LEDs 0..n1-1, the second the next ones, and so on."""
        if not isinstance(body, list) or not body:
            raise HTTPException(400, "A tree needs at least one controller.")
        patch = {"controllers": [{k: v for k, v in c.items() if k not in ("start", "last", "wled")} for c in body]}
        _refuse_while_busy(t, patch)
        try:
            t.config.update(patch)
        except ValidationError as e:
            raise HTTPException(422, _validation_message(e))
        await _after_config_change(t, patch)
        return controllers_get(t)

    # ---- WLED ----------------------------------------------------------------------------
    @app.get("/api/wled/discover")
    async def wled_discover() -> list[dict]:
        try:
            found = await discovery.discover(timeout=3.0)
        except discovery.DiscoveryError as e:
            raise HTTPException(502, str(e))
        except Exception as e:  # noqa: BLE001 - show the reason, not a bare 500
            log.exception("WLED discovery failed")
            raise HTTPException(500, f"Searching for controllers failed ({type(e).__name__}: {e}). "
                                     "Enter the controller's address instead; the server log has details.")
        # Which tree already uses each controller (a controller drives one tree only).
        owners: dict[str, str] = {}
        for x in state.trees.values():
            for c in x.config.settings.controllers:
                for key in (c.host, c.mac):
                    if key:
                        owners[key.lower()] = x.id
        for d in found:
            d["tree"] = owners.get(str(d.get("mac") or "").lower()) or owners.get(str(d.get("host") or "").lower())
        return found

    @app.post("/api/wled/select")
    async def wled_select(body: dict = Body(...), t: Tree = Depends(current_tree)) -> dict:
        """Use the controller at `host` for this tree: as its first controller (the default),
        as controller `controller` (0-based), or as an extra one after the others (`add`)."""
        host = str(body.get("host", "")).strip()
        if not host:
            raise HTTPException(400, "host required")
        s = t.config.settings
        add = bool(body.get("add"))
        k = len(s.controllers) if add else int(body.get("controller", 0))
        if not 0 <= k <= len(s.controllers) or (k == len(s.controllers) and not add):
            raise HTTPException(400, f"no controller {k + 1} in {s.tree_name}")
        port = int(body.get("http_port") or (s.controllers[k].http_port if k < len(s.controllers) else 80))
        from .wled.link import WledLink

        probe = WledLink(host, None, port)
        try:
            info = await probe.refresh(with_cfg=False)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Can't reach WLED at {host}: {e}")
        finally:
            await probe.close()
        ctl = [c.model_dump() for c in s.controllers]
        entry = {**(ctl[k] if k < len(ctl) else {}), "host": host, "mac": info.mac, "http_port": port}
        if info.led_count:
            entry["led_count"] = info.led_count
        if k < len(ctl):
            ctl[k] = entry
        else:
            ctl.append(entry)
        patch = {"controllers": ctl}
        _refuse_while_busy(t, patch)
        try:
            t.config.update(patch)
        except ValidationError as e:
            raise HTTPException(409, _validation_message(e))
        t.reconfigure()
        link = t.links[k]
        if link.host != host or link.port != port:
            link.set_host(host, info.mac, port)
        try:
            await link.refresh(with_cfg=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Can't reach WLED at {host}: {e}")
        t._apply_sender()
        return {"host": host, "mac": info.mac, "led_count": info.led_count, "version": info.version, "controller": k}

    @app.get("/api/wled/checks")
    async def wled_checks(t: Tree = Depends(current_tree)) -> list[dict]:
        return await t.setup_checks()

    @app.post("/api/wled/identify")
    def wled_identify(body: dict = Body(...), t: Tree = Depends(current_tree)) -> dict:
        t.start_effect("identify", float(body.get("brightness", 0.6)), int(body.get("index", 0)))
        return {"ok": True}

    # ---- scans ---------------------------------------------------------------------------
    @app.get("/api/scans")
    def scans(t: Tree = Depends(current_tree)) -> list[dict]:
        return t.scans.list()

    @app.post("/api/scans")
    async def new_scan(t: Tree = Depends(current_tree)) -> dict:
        sess = t.open_session()
        return {"scan": sess.scan.id}

    @app.post("/api/scans/{sid}/open")
    async def open_scan(sid: str, t: Tree = Depends(current_tree)) -> dict:
        try:
            sess = t.open_session(sid)
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")
        return {"scan": sess.scan.id}

    @app.get("/api/scans/{sid}")
    def scan_detail(sid: str, t: Tree = Depends(current_tree)) -> dict:
        try:
            scan = t.scans.get(sid)
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")
        obs = scan.observations()
        seen = {l for s in obs.values() for l, c in s.items() if c}
        return {"meta": scan.meta, "seen_leds": len(seen), "overrides": scan.overrides()}

    @app.get("/api/scans/{sid}/overrides")
    def get_overrides(sid: str, t: Tree = Depends(current_tree)) -> dict:
        return t.scans.get(sid).overrides()

    @app.put("/api/scans/{sid}/overrides")
    def put_overrides(sid: str, body: dict = Body(...), t: Tree = Depends(current_tree)) -> dict:
        allowed = {"bad", "positions", "breaks", "links", "cap", "locked"}
        clean = {k: v for k, v in body.items() if k in allowed}
        # Link bounds and the cap are lengths in map units (cm, or tree heights): positive numbers.
        try:
            links = {str(int(k)): float(v) for k, v in (clean.get("links") or {}).items()}
            if any(not (v > 0) for v in links.values()):
                raise ValueError("a link bound must be a positive length")
            clean["links"] = links
            if clean.get("cap") is not None:
                clean["cap"] = float(clean["cap"])
                if not clean["cap"] > 0:
                    raise ValueError("the cap must be a positive length")
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"Invalid link limits: {e}")
        t.scans.get(sid).set_overrides(clean)
        return clean

    @app.post("/api/scans/{sid}/solve")
    async def solve_scan(sid: str, t: Tree = Depends(current_tree)) -> dict:
        try:
            return t.start_solve(sid).to_dict()
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")

    @app.get("/api/jobs")
    def jobs_running(t: Tree = Depends(current_tree)) -> list[dict]:
        """Jobs still running (decoding or solving), so a reloaded page can show their progress."""
        return [j.to_dict() for j in state.jobs.values() if j.status == "running" and j.tree in (None, t.id)]

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
    async def video_play(sid: str, body: dict = Body(default={}), t: Tree = Depends(current_tree)) -> dict:
        try:
            scan = t.scans.get(sid)
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")
        if t.session and t.session.busy():
            raise HTTPException(409, "A live view is being captured.")
        n = scan.meta["led_count"]
        levels = None
        if t.session and t.session.scan.id == sid:
            levels = [int(v) for v in t.session.cal.levels]
        seed = int(time.time()) % 100000
        tl = videomode.Timeline.build(n, seed, levels)
        countdown = float(body.get("countdown_s", 10))
        vid = scan.add_view({"mode": "video", "status": "recording", "timeline": tl.to_json()})
        owner = "video-sequence"
        ok, holder = t.output.acquire(owner, "calibration", ttl=countdown + tl.duration + 30)
        if not ok:
            raise HTTPException(409, f"LED output is in use ({holder.kind if holder else '?'}).")
        t.output.set_renderer(owner, videomode.player(tl, countdown), fps=40)

        async def finish() -> None:
            await asyncio.sleep(countdown + tl.duration + 1.5)
            t.output.release(owner)
            scan.set_view(vid, status="awaiting_upload")

        asyncio.ensure_future(finish())
        return {"view": vid, "countdown_s": countdown, "duration_s": round(tl.duration, 1)}

    def _video_path(sid: str, vid: str, t: Tree) -> Path:
        scan = t.scans.get(sid)
        if not any(v["id"] == vid for v in scan.meta["views"]):
            raise HTTPException(404, "no such view")
        p = scan.root / "videos" / f"{vid}.upload"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @app.get("/api/scans/{sid}/video/{vid}/upload")
    def video_upload_size(sid: str, vid: str, t: Tree = Depends(current_tree)) -> dict:
        p = _video_path(sid, vid, t)
        return {"size": p.stat().st_size if p.exists() else 0}

    @app.put("/api/scans/{sid}/video/{vid}/upload")
    async def video_upload_chunk(sid: str, vid: str, request: Request, offset: int = 0, t: Tree = Depends(current_tree)) -> dict:
        """Resumable upload: chunks are appended at `offset`, which must equal the current size."""
        p = _video_path(sid, vid, t)
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
    async def video_decode(sid: str, vid: str, t: Tree = Depends(current_tree)) -> dict:
        p = _video_path(sid, vid, t)
        if not p.exists() or p.stat().st_size == 0:
            raise HTTPException(400, "upload the video first")
        scan = t.scans.get(sid)
        tl_meta = scan.view(vid).get("timeline")
        job = Job("video", sid, tree=t.id)
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
    def rescan_info(mid: str, t: Tree = Depends(current_tree)) -> dict:
        m = get_map(mid, t)
        low = rescan.low_confidence(m)
        try:
            kind = _scan_kind(t.scans.get(m["scan"]))
        except FileNotFoundError:
            kind = None
        return {"map": mid, "scan": m["scan"], "kind": kind, "low_confidence": low, "n": len(m["leds"])}

    @app.post("/api/maps/{mid}/rescan")
    async def rescan_setup(mid: str, body: dict = Body(default={}), t: Tree = Depends(current_tree)) -> dict:
        """Choose LEDs to re-scan; sets up the capture in the map's scan. Tripod scans
        get a pending re-scan view (started from Control or Calibrate); walk scans are
        re-scanned by walking again into the same scan (the Scan page with ?scan=)."""
        m = get_map(mid, t)
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
            scan = t.scans.get(m["scan"])
        except FileNotFoundError:
            raise HTTPException(404, "The scan this map was built from no longer exists.")
        kind = _scan_kind(scan)
        out = {"map": mid, "scan": scan.id, "kind": kind, "leds": leds}
        if kind == "tripod":
            anc = rescan.anchors(m, exclude=set(leds))
            sess = t.session if t.session and t.session.scan.id == scan.id else t.open_session(scan.id)
            sess.scan.meta["rescan"] = {"map": mid, "leds": leds, "anchors": anc, "capture": sorted(set(leds) | set(anc))}
            sess.scan.save()
            out["anchors"] = anc
        return out

    @app.post("/api/leds/show")
    def leds_show(body: dict = Body(default={}), t: Tree = Depends(current_tree)) -> dict:
        """Blink a set of LEDs on the tree (e.g. the ones about to be re-scanned)."""
        n = t.output.led_count
        leds = sorted({int(i) for i in body.get("leds", []) if 0 <= int(i) < n})
        if not leds:
            raise HTTPException(400, "No LEDs given.")
        t.start_effect("identify", float(body.get("brightness", 0.6)), leds)
        return {"ok": True, "count": len(leds)}

    # ---- walk-around capture (§7.9) -----------------------------------------------------------
    @app.get("/api/walks")
    def walks(t: Tree = Depends(current_tree)) -> list[dict]:
        """Every walk recorded, newest first, with what's still on disk: the raw recording and
        live frames (needed to decode again; old scans' raw data is pruned, §13) and the
        decoded keyframes (kept; enough to solve again)."""
        out = []
        active = t.maps.active()
        for p in sorted(t.scans.dir.iterdir(), reverse=True):
            try:
                scan = t.scans.get(p.name)
            except FileNotFoundError:
                continue
            for v in scan.meta.get("views", []):
                if v.get("mode") != "walk":
                    continue
                wdir = scan.root / "walk" / v["id"]
                video = scan.root / "videos" / f"{v['id']}.upload"
                live = wdir / "live"
                obs = storage.read_json(wdir / "observations.json", default=None) or {}
                out.append({
                    "scan": scan.id, "view": v["id"], "created": scan.meta.get("created"), "status": v.get("status"),
                    "duration_s": v.get("duration_s"),
                    "video_mb": round(video.stat().st_size / 1e6, 1) if video.exists() else 0.0,
                    "live_frames": sum(1 for _ in live.glob("*.jpg")) if live.is_dir() else 0,
                    "keyframes": len(obs.get("segments", {})),
                    "decoded": (obs.get("info") or {}).get("decoded"),
                    "active_map_scan": bool(active and active.get("scan") == scan.id),
                    "busy": bool(t.walk and t.walk.scan.id == scan.id and t.walk.vid == v["id"] and t.walk.state == "walking"),
                })
        return out

    @app.post("/api/scans/{sid}/walk/{vid}/process")
    async def walk_process(sid: str, vid: str, body: dict = Body(default={}), t: Tree = Depends(current_tree)) -> dict:
        """Decode the walk's live frames and uploaded recording (if any), then solve."""
        try:
            p = _video_path(sid, vid, t)
        except FileNotFoundError:
            raise HTTPException(404, "no such scan")
        use_video = body.get("video", True) and p.exists() and p.stat().st_size > 0
        live = t.scans.get(sid).root / "walk" / vid / "live"
        if not use_video and not (live.is_dir() and any(live.glob("*.jpg"))):
            raise HTTPException(409, "This walk's recording and live frames are no longer stored (old scans' raw data is "
                                     "deleted to save space), so it can't be decoded again. Solve the scan again instead: "
                                     "that uses the keyframes decoded before.")
        return t.start_walk_processing(sid, vid, str(p) if use_video else None).to_dict()

    # ---- maps -----------------------------------------------------------------------------
    @app.get("/api/maps")
    def maps(t: Tree = Depends(current_tree)) -> list[dict]:
        return t.maps.list()

    @app.get("/api/maps/active")
    def active_map(t: Tree = Depends(current_tree)) -> dict:
        m = t.maps.active()
        if not m:
            raise HTTPException(404, "no active map")
        return m

    @app.get("/api/maps/{mid}")
    def get_map(mid: str, t: Tree = Depends(current_tree)) -> dict:
        try:
            return t.maps.get(mid)
        except FileNotFoundError:
            raise HTTPException(404, "no such map")

    @app.post("/api/maps/{mid}/activate")
    def activate(mid: str, body: dict = Body(default={}), t: Tree = Depends(current_tree)) -> dict:
        try:
            return t.maps.activate(mid, t.fingerprint(), force=bool(body.get("force")))
        except ActivationBlocked as e:
            raise HTTPException(409, str(e))
        except FileNotFoundError:
            raise HTTPException(404, "no such map")

    @app.post("/api/maps/rollback")
    def rollback(t: Tree = Depends(current_tree)) -> dict:
        return {"active": t.maps.rollback()}

    @app.get("/api/maps/{mid}/export")
    def export(mid: str, fmt: str = "json", t: Tree = Depends(current_tree)) -> Response:
        m = get_map(mid, t)
        if fmt == "csv":
            return PlainTextResponse(export_csv(m), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="treelights-{mid}.csv"'})
        return JSONResponse(m, headers={"Content-Disposition": f'attachment; filename="treelights-{mid}.json"'})

    # ---- test animation ------------------------------------------------------------------
    @app.post("/api/test/start")
    def test_start(body: dict = Body(default={}), t: Tree = Depends(current_tree)) -> dict:
        running = t.start_effect(body.get("effect", "sweep"), float(body.get("brightness", 0.5)),
                                     params=body.get("params"))
        return {"ok": True, "effect": running}

    # ---- effects library -----------------------------------------------------------------
    @app.get("/api/effects")
    def effects_list(t: Tree = Depends(current_tree)) -> dict:
        return {"effects": library(), "palettes": catalogue(), "current": t.effect_state()}

    @app.post("/api/effects/params")
    def effects_params(body: dict = Body(default={}), t: Tree = Depends(current_tree)) -> dict:
        b = body.get("brightness")
        return {"ok": True, "current": t.update_effect(body.get("params"), None if b is None else float(b))}

    @app.post("/api/test/stop")
    def test_stop(t: Tree = Depends(current_tree)) -> dict:
        t.stop_effect()
        return {"ok": True}

    # ---- lights on/off, timers, Home Assistant ------------------------------------------------
    @app.get("/api/lights")
    def lights_get(t: Tree = Depends(current_tree)) -> dict:
        return t.lights.status()

    @app.post("/api/lights")
    async def lights_set(body: dict = Body(default={}), t: Tree = Depends(current_tree)) -> dict:
        """`{"on": true, "effect"?, "params"?, "brightness"?}` or `{"on": false}`. Without "on",
        changes the parameters (kept for the next "on" while off)."""
        b = body.get("brightness")
        try:
            if body.get("on") is False:
                return await t.lights.turn_off(source="app")
            if body.get("on") is True:
                return await t.lights.turn_on(body.get("effect"), body.get("params"),
                                                  None if b is None else float(b), source="app")
            return await t.lights.set_params(body.get("params"), None if b is None else float(b), source="app")
        except LightsBusy as e:
            raise HTTPException(409, str(e))
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.get("/api/schedule")
    def schedule_get(t: Tree = Depends(current_tree)) -> dict:
        return t.scheduler.status()

    @app.put("/api/schedule")
    def schedule_put(body: dict = Body(...), t: Tree = Depends(current_tree)) -> dict:
        """The schedule settings (timers replace the list as a whole); returns the new status."""
        try:
            t.config.update({"schedule": body})
        except ValidationError as e:
            raise HTTPException(422, _validation_message(e))
        t.scheduler.poke()
        return t.scheduler.status()

    @app.get("/api/homeassistant")
    def homeassistant_status(t: Tree = Depends(current_tree)) -> dict:
        return t.ha.status()

    # ---- WebSockets ----------------------------------------------------------------------
    @app.websocket("/ws/capture")
    async def ws_capture(ws: WebSocket) -> None:
        if not ws_allowed(ws):
            await ws.close(code=4401)
            return
        tr = ws_tree(ws)
        if tr is None:
            await ws.close(code=4404)
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
        sess = tr.session or tr.open_session()
        await sess.camera_connected(send, hello)
        unsub = sess.on_status(lambda st: asyncio.ensure_future(_safe(send, dict(st, guidance=tr.guidance))))
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
            if tr.session is sess:
                sess.camera_disconnected("websocket closed")

    @app.websocket("/ws/walk")
    async def ws_walk(ws: WebSocket) -> None:
        """The phone during a walk-around capture (protocol: walk/protocol.py)."""
        if not ws_allowed(ws):
            await ws.close(code=4401)
            return
        tr = ws_tree(ws)
        if tr is None:
            await ws.close(code=4404)
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
        walk = tr.walk
        resume = walk is not None and walk.state == "walking" and hello.get("view") == walk.vid
        if not resume and hello.get("view") and hello.get("scan"):
            # The phone was walking when the server stopped: that walk has ended; tell the
            # phone to upload its recording and process what was captured.
            try:
                old = tr.scans.get(hello["scan"]).view(hello["view"])
            except (FileNotFoundError, StopIteration):
                old = None
            if old and old.get("mode") == "walk" and old.get("status") == "awaiting_upload":
                sid, vid = hello["scan"], hello["view"]
                await send({"type": "welcome", "scan": sid, "view": vid, "n": tr.output.led_count,
                            **walkproto.timing_info(), "resumed": False})
                await send({"type": "stopped", "scan": sid, "view": vid, "reason": old.get("stop_reason", "ended"),
                            "upload": f"/api/scans/{sid}/video/{vid}/upload", "process": f"/api/scans/{sid}/walk/{vid}/process"})
                await ws.close()
                return
        try:
            if not resume:
                walk = tr.open_walk(hello.get("scan"))
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
        tr = ws_tree(ws)
        if tr is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        tr.control_clients.add(q)
        if tr.session:
            q.put_nowait(dict(tr.session.status, guidance=tr.guidance))

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
                        sess = tr.session or tr.open_session()
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
                    elif t == "stop" and tr.session:
                        await tr.session.stop()
                    elif t == "resume" and tr.session:
                        tr.session.resume()
                    elif t == "new_scan":
                        tr.open_session()
                        await ws.send_json({"type": "scan", "scan": tr.session.scan.id})
                except (RuntimeError, HTTPException) as e:
                    await ws.send_json({"type": "error", "message": getattr(e, "detail", None) or str(e)})
        except (WebSocketDisconnect, ValueError):
            pass
        finally:
            pt.cancel()
            tr.control_clients.discard(q)

    @app.websocket("/ws/preview")
    async def ws_preview(ws: WebSocket) -> None:
        if not ws_allowed(ws):
            await ws.close(code=4401)
            return
        tr = ws_tree(ws)
        if tr is None:
            await ws.close(code=4404)
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

        tr.output.add_listener(on_frame)
        try:
            while True:
                await ws.send_bytes(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if on_frame in tr.output._listeners:
                tr.output._listeners.remove(on_frame)

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


def _validation_message(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())


async def _safe(send, msg: dict) -> None:  # noqa: ANN001
    try:
        await send(msg)
    except Exception:  # noqa: BLE001
        pass
