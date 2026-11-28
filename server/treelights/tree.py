"""One tree: its controllers, output, captures, maps, effect, timers and Home Assistant
entity (architecture §5.5, §12.1).

Every tree is independent. It has its own output thread and lease (§5.4), so scanning one
tree never blocks effects on another, and its own folder for scans, maps and state:
<data>/trees/<id>/ (§13). A tree's LEDs may be split over several WLED controllers; they
make one global index space (`wled/group.py`) and everything here works in it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

import numpy as np
from fastapi import HTTPException

from .capture.session import CaptureSession
from .capture.store import ScanStore
from .config import tree_dir
from .drawing import Drawing
from .effects import EFFECTS, LIBRARY, PALETTE_IDS, DrawingEffect, Identify, TreeGeometry, make_renderer
from .homeassistant import HomeAssistant
from .lights import Lights
from .maps import guidance as guide
from .maps.store import MapStore
from .output.manager import OutputManager, PowerLimiter
from .reconstruction.job import run_solve
from .schedule import Scheduler
from .walk.job import process_walk
from .walk.session import WalkSession
from .wled.brightness import GroupBrightness
from .wled.client import config_fingerprint
from .wled.group import IndexMap, SlicedPower, make_sender, tree_fingerprint
from .wled.link import MonitorEvent, WledLink, WledMonitor

if TYPE_CHECKING:
    from .app import AppState, Job

log = logging.getLogger(__name__)

PLACED = ("measured", "single_view", "manual")


class Tree:
    def __init__(self, app: "AppState", tid: str):
        self.app = app
        self.id = tid
        self.config = app.config.for_tree(tid)
        self.data_dir = tree_dir(app.data_dir, tid)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        s = self.config.settings
        self.index = IndexMap([c.led_count for c in s.controllers])
        self.output = OutputManager(self.index.total, PowerLimiter(None, s.power.ma_per_channel, s.power.idle_ma_per_led))
        self.links: list[WledLink] = []
        self.monitors: list[WledMonitor] = []
        self._monitor_listeners: list[Callable[[MonitorEvent], None]] = []
        self._session_unsub: Callable[[], None] | None = None
        self._started = False
        self._sync_links(s)
        self.brightness = GroupBrightness(self.data_dir, lambda: self.links)
        self.scans = ScanStore(self.data_dir)
        self.maps = MapStore(self.data_dir)
        self.session: CaptureSession | None = None
        self.walk: WalkSession | None = None
        self.guidance: dict | None = None
        self.control_clients: set[asyncio.Queue] = set()
        self.test_owner: str | None = None
        self.effect_renderer = None  # the running library effect, changeable live
        self.drawing = Drawing(self)  # the picture painted on the Draw page
        self.lights = Lights(self)  # the tree as one on/off light (timers, Home Assistant)
        self.scheduler = Scheduler(self.config, self.lights)
        self.ha = HomeAssistant(self)
        self._apply_sender()

    @property
    def name(self) -> str:
        return self.config.settings.tree_name

    # ---- controllers ------------------------------------------------------------------
    @property
    def link(self) -> WledLink:
        """The first controller (the only one on most trees)."""
        return self.links[0]

    @property
    def monitor(self) -> WledMonitor:
        return self.monitors[0]

    def controller_label(self, k: int) -> str:
        sl = self.index.slices()[k] if k < len(self.index) else None
        rng = f" (LEDs {sl.start}–{sl.stop - 1})" if sl else ""
        return f"Controller {k + 1}{rng}"

    def _sync_links(self, s) -> None:  # noqa: ANN001
        """One link and monitor per configured controller, reusing those whose address is
        unchanged."""
        pool = {(l.host, l.port): l for l in self.links}
        links = []
        for c in s.controllers:
            link = pool.pop((c.host, c.http_port), None)
            if link is None:
                link = WledLink(c.host, c.mac, c.http_port)
                link.on_host_change(lambda _h: self._apply_sender())
            elif c.mac and not link.mac:
                link.mac = c.mac
            links.append(link)
        for old in pool.values():
            if _loop_running():
                asyncio.ensure_future(old.close())
        changed = [id(l) for l in links] != [id(l) for l in self.links]
        self.links = links
        if changed or not self.monitors:
            for m in self.monitors:
                if _loop_running():
                    asyncio.ensure_future(m.stop())
            self.monitors = []
            for k, link in enumerate(links):
                mon = WledMonitor(link, lambda: self.output.holder() is not None)
                mon.subscribe(lambda ev, k=k: self._on_monitor(k, ev))
                self.monitors.append(mon)
                if self._started:
                    mon.start()

    def reconfigure(self) -> None:
        """After the tree's controllers changed in the settings."""
        self._sync_links(self.config.settings)
        self._apply_sender()

    def _apply_sender(self) -> None:
        s = self.config.settings
        counts = [c.led_count for c in s.controllers]
        if counts != self.index.counts:
            self.index = IndexMap(counts)
        if self.output.led_count != self.index.total:
            self.output.set_led_count(self.index.total)
        hosts = []
        for k, c in enumerate(s.controllers):
            link = self.links[k] if k < len(self.links) else None
            hosts.append(((link.host if link else None) or c.host, c.protocol, c.udp_port))
        self.output.set_sender(make_sender(self.index, hosts))
        self._apply_power()

    def _apply_power(self) -> None:
        """A cap per controller: its own setting, else the tree's, else WLED's limit (§12)."""
        s = self.config.settings
        limiters = []
        for k, c in enumerate(s.controllers):
            lim = c.max_current_ma if c.max_current_ma is not None else s.power.max_current_ma
            link = self.links[k] if k < len(self.links) else None
            if lim is None and link and link.info and link.info.max_current_ma:
                lim = link.info.max_current_ma
            limiters.append(PowerLimiter(lim or None, s.power.ma_per_channel, s.power.idle_ma_per_led))
        self.output.power = limiters[0] if len(limiters) == 1 else SlicedPower(self.index, limiters)

    def subscribe_monitor(self, fn: Callable[[MonitorEvent], None]) -> Callable[[], None]:
        self._monitor_listeners.append(fn)
        return lambda: self._monitor_listeners.remove(fn) if fn in self._monitor_listeners else None

    def _on_monitor(self, k: int, ev: MonitorEvent) -> None:
        if len(self.links) > 1:
            ev = MonitorEvent(ev.kind, f"{self.controller_label(k)}: {ev.detail}", ev.at)
        for fn in list(self._monitor_listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                log.exception("monitor listener failed")

    def output_breaks(self) -> list[int]:
        """Chain breaks known from the wiring: between controllers, and between the GPIO
        outputs of each controller (§10.3)."""
        out = set(self.index.boundaries())
        for k, link in enumerate(self.links[: len(self.index)]):
            if link.cfg:
                out |= set(self.index.shift_breaks(k, link.cfg.output_breaks()))
        return sorted(out)

    def fingerprint(self) -> str | None:
        return tree_fingerprint([(l.info, l.cfg) for l in self.links])

    def wled_meta(self) -> dict:
        """What a scan and a map record about the controllers (§11.4, §14)."""
        d = {"mac": self.link.mac, "led_count": self.index.total, "config_hash": self.fingerprint()}
        if len(self.links) > 1:
            d["controllers"] = [{"mac": l.mac or (l.info.mac if l.info else None), "host": l.host, "start": sl.start, "led_count": sl.count,
                                 "config_hash": config_fingerprint(l.info, l.cfg) if l.info else None}
                                for l, sl in zip(self.links, self.index.slices())]
        return d

    def scan_extra(self) -> dict:
        s = self.config.settings
        extra = {"output_breaks": self.output_breaks(), "wled": self.wled_meta(), "settings": s.capture.model_dump()}
        if len(self.links) > 1:
            extra["controller_breaks"] = self.index.boundaries()
            extra["controllers"] = self.index.describe()
        return extra

    async def setup_checks(self) -> list[dict]:
        """The Setup checklist (§5.2), run for every controller of the tree."""
        s = self.config.settings
        if not any(l.host for l in self.links):
            return [{"name": "reachable", "status": "fail", "detail": "No controller selected."}]
        owner = "setup-check"
        multi = len(self.links) > 1

        async def probe() -> None:
            ok, _ = self.output.acquire(owner, "identify", ttl=3)
            if ok:
                f = np.zeros((self.output.led_count, 3), np.uint8)
                for sl in self.index.slices():
                    f[sl.start] = (8, 8, 8)  # the first LED of each controller
                await asyncio.wrap_future(self.output.show(owner, f, repeats=2))

        out: list[dict] = []
        if multi:
            ranges = "; ".join(f"{k + 1}: LEDs {sl.start}–{sl.stop - 1}" for k, sl in enumerate(self.index.slices()))
            out.append({"name": "controllers", "status": "ok", "controller": None,
                        "detail": f"{len(self.links)} controllers ({ranges}). Chain breaks between them after LEDs "
                                  f"{', '.join(map(str, self.index.boundaries()))}."})
        try:
            for k, (link, c) in enumerate(zip(self.links, s.controllers)):
                if not link.host:
                    out.append({"name": "reachable", "status": "fail", "controller": k,
                                "detail": f"{self.controller_label(k)}: no address set."})
                    continue
                for ch in await link.setup_checks(c.led_count, probe):
                    d = dict(ch.__dict__)
                    if multi:
                        d["controller"] = k
                        d["detail"] = f"{self.controller_label(k)}: {d['detail']}"
                    out.append(d)
        finally:
            self.output.release(owner)
        self._apply_power()
        return out

    # ---- sessions ----------------------------------------------------------------
    def _new_scan(self):  # noqa: ANN202
        s = self.config.settings
        # Retention (§13): raw frames of old scans are deleted; detections are kept.
        active = self.maps.active()
        self.scans.prune_frames({active["scan"]} if active else set(), s.raw_frame_retention_scans)
        return self.scans.create(self.index.total, self.scan_extra())

    def open_session(self, scan_id: str | None = None) -> CaptureSession:
        if self.session and self.session.busy():
            raise HTTPException(409, "A view is being captured; stop it first.")
        s = self.config.settings
        scan = self.scans.get(scan_id) if scan_id else self._new_scan()
        if self.session:
            asyncio.ensure_future(self.session.stop())
        sess = CaptureSession(scan, self.output, self.app.capture_executor, s)
        sess.boost_brightness, sess.restore_brightness = self.brightness.boost, self.brightness.restore
        sess.on_status(self.broadcast_status)
        sess.after_view = self._after_view
        if self._session_unsub:
            self._session_unsub()
        self._session_unsub = self.subscribe_monitor(sess.on_monitor)
        self.session = sess
        self.guidance = None
        return sess

    def open_walk(self, scan_id: str | None = None) -> WalkSession:
        """A walk-around capture (§7.9), in the given scan or a new one."""
        if self.session and self.session.busy():
            raise HTTPException(409, "A view is being captured; stop it first.")
        if self.walk and self.walk.state == "walking":
            raise HTTPException(409, "A walk is already in progress.")
        scan = self.scans.get(scan_id) if scan_id else self._new_scan()
        level = 48
        if self.session and self.session.scan.id == scan.id and getattr(self.session, "cal", None) is not None:
            lv = [int(v) for v in self.session.cal.levels if v]
            level = int(np.median(lv)) if lv else level
        self.walk = WalkSession(scan, self.output, level=level)
        self.walk.boost_brightness, self.walk.restore_brightness = self.brightness.boost, self.brightness.restore
        return self.walk

    def start_walk_processing(self, scan_id: str, vid: str, video_path: str | None) -> "Job":
        from .app import Job

        scan = self.scans.get(scan_id)
        try:
            view = scan.view(vid)
        except StopIteration:
            raise HTTPException(404, "no such view")
        if view.get("mode") != "walk":
            raise HTTPException(400, "not a walk-around view")
        if self.walk and self.walk.scan.id == scan_id and self.walk.vid == vid and self.walk.state == "walking":
            raise HTTPException(409, "The walk is still in progress.")
        job = Job("walk", scan_id, self.app.data_dir / "jobs", vid, tree=self.id)
        self.app.jobs[job.id] = job
        s = self.config.settings
        prev = self.maps.active()
        wled = self.wled_meta()
        scan.set_view(vid, status="processing")

        async def run() -> None:
            try:
                m = await asyncio.get_running_loop().run_in_executor(
                    self.app.executor, process_walk, str(scan.root), vid, s.tree_height_cm, prev, wled, None, video_path,
                    job.progress_path)
                self.annotate_controllers(m, scan.meta)
                job.result = {"map": self.maps.save(m), "quality": m["quality"]}
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                log.exception("walk processing failed")
                job.status, job.error = "failed", str(e)
                scan.set_view(vid, status="failed", error=str(e))
            job.finish()

        asyncio.ensure_future(run())
        return job

    def start_solve(self, scan_id: str) -> "Job":
        from .app import Job

        scan = self.scans.get(scan_id)
        job = Job("solve", scan_id, self.app.data_dir / "jobs", tree=self.id)
        self.app.jobs[job.id] = job
        s = self.config.settings
        prev = self.maps.active()
        wled = self.wled_meta()

        async def run() -> None:
            try:
                f0 = self.app.profiles.prior_for(scan.meta.get("segments", {}))
                m = await asyncio.get_running_loop().run_in_executor(
                    self.app.executor, run_solve, str(scan.root), s.tree_height_cm, prev, wled, f0, job.progress_path)
                self.annotate_controllers(m, scan.meta)
                job.result = {"map": self.maps.save(m), "quality": m["quality"]}
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                log.exception("solve failed")
                job.status, job.error = "failed", str(e)
            job.finish()

        asyncio.ensure_future(run())
        return job

    @staticmethod
    def annotate_controllers(m: dict, scan_meta: dict) -> None:
        """Verify, don't assume (§5.5): how many LEDs of each controller the map placed. A
        controller far behind the others most likely didn't show the pattern (not
        receiving, another sender, or too late), which the per-LED checks can't tell from
        LEDs hidden by branches."""
        ctl = scan_meta.get("controllers")
        if not ctl or len(ctl) < 2:
            return
        st = [l.get("status") for l in m.get("leds", [])]
        rows = []
        for k, c in enumerate(ctl):
            part = st[c["start"] : c["start"] + c["count"]]
            frac = sum(1 for x in part if x in PLACED) / max(1, len(part))
            rows.append({"controller": k + 1, "start": c["start"], "count": c["count"], "placed": round(frac, 3)})
        q = m.setdefault("quality", {})
        q["controllers"] = rows
        best = max(r["placed"] for r in rows)
        for r in rows:
            if r["placed"] < 0.5 and r["placed"] < 0.6 * best:
                q.setdefault("warnings", []).append(
                    f"Controller {r['controller']} (LEDs {r['start']}–{r['start'] + r['count'] - 1}): only "
                    f"{round(100 * r['placed'])}% of its LEDs were placed, against {round(100 * best)}% on another. "
                    "Check it shows the pattern (Setup checks), then scan again.")

    def has_map(self) -> bool:
        """Is there an active map that fits the LED count (needed by 3D and surface effects)?"""
        m = self.maps.active()
        return bool(m and len(m["leds"]) == self.output.led_count and any(l.get("p") for l in m["leds"]))

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
        """Does the active map still match the controllers (§11.4)? A warning, or None.
        Checked at start-up, whenever the state is read, and before activation."""
        m = self.maps.active()
        if not m:
            return None
        w = m.get("wled") or {}
        saved = w.get("controllers")
        if len(self.links) == 1 and not saved:
            return self._map_check_one(m, w, self.link, None)
        if any(l.info is None for l in self.links):
            return None
        saved = saved or [{"mac": w.get("mac"), "led_count": len(m.get("leds", [])), "config_hash": w.get("config_hash")}]
        if len(saved) != len(self.links):
            return (f"The active map was made with {len(saved)} WLED controller(s); this tree now has {len(self.links)}. "
                    "Put the controllers back as they were, or re-scan.")
        for k, (sv, link) in enumerate(zip(saved, self.links)):
            msg = self._map_check_one(m, sv, link, k)
            if msg:
                return msg
        fp = self.fingerprint()
        if w.get("config_hash") and fp and w["config_hash"] != fp:
            return ("The WLED controllers' LED configuration (order, outputs, LED counts or realtime settings) changed since "
                    "the active map was made. If LEDs now light in a different order, re-scan; otherwise change it back.")
        return None

    def _map_check_one(self, m: dict, w: dict, link: WledLink, k: int | None) -> str | None:
        if not link.info:
            return None
        who = "" if k is None else f"{self.controller_label(k)}: "
        mac, n = link.info.mac or link.mac, link.info.led_count
        if w.get("mac") and mac and w["mac"].lower() != mac.lower():
            return (f"{who}The active map was made with another WLED controller (MAC {w['mac']}, now {mac}). "
                    "If the string moved to a new controller with the same wiring, the map still fits; otherwise re-scan.")
        if k is None:
            if n and len(m.get("leds", [])) != n:
                return (f"The active map has {len(m['leds'])} LEDs but WLED now drives {n}. "
                        "Check WLED's LED count, or re-scan.")
            fp = self.fingerprint()
            if w.get("config_hash") and fp and w["config_hash"] != fp:
                return ("WLED's LED configuration (outputs, LED count or realtime settings) changed since the active map "
                        "was made. If LEDs now light in a different order, re-scan; otherwise change it back in WLED.")
            return None
        if n and w.get("led_count") and int(w["led_count"]) != n:
            return (f"{who}the active map has {w['led_count']} LEDs on this controller but WLED now drives {n}. "
                    "Check WLED's LED count, or re-scan.")
        own = config_fingerprint(link.info, link.cfg)
        if w.get("config_hash") and w["config_hash"] != own:
            return (f"{who}WLED's LED configuration (outputs, LED count or realtime settings) changed since the active map "
                    "was made. If LEDs now light in a different order, re-scan; otherwise change it back in WLED.")
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
                self.app.executor, run_solve, str(sess.scan.root), None, None, None)
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

    # ---- test animation and effects ---------------------------------------------------------
    def start_effect(self, name: str, brightness: float, index: int | list[int] | None = None,
                     params: dict | None = None) -> str:
        """Start an effect on the LEDs; returns the effect actually running (the index chase
        stands in for effects that need a map when there is none)."""
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
            if EFFECTS[name].needs_map and not tree.has_pos.any():
                name = "chase"  # no map yet: index chase
            self._check_params(params)
            try:
                eff = EFFECTS[name](params)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            if isinstance(eff, DrawingEffect):  # shows the Draw page's picture, live
                self.drawing.fit()
                eff.canvas = self.drawing.canvas
        eff.setup(tree)
        renderer = make_renderer(eff, brightness)
        self.output.set_renderer(owner, renderer, fps=40)
        self.test_owner = owner
        self.effect_renderer = renderer if name in LIBRARY else None
        self.lights.sync("app")
        return name

    @staticmethod
    def _check_params(params: dict | None) -> None:
        if params and params.get("palette") is not None and params["palette"] not in PALETTE_IDS:
            raise HTTPException(422, f"unknown palette {params['palette']}")

    def update_effect(self, params: dict | None, brightness: float | None) -> dict:
        """Change the running library effect's sliders, palette, colors or brightness without restarting it."""
        r = self.effect_renderer
        if r is None or not self.output.holds("test"):
            raise HTTPException(409, "No effect is running.")
        self._check_params(params)
        try:
            r.effect.set_params(params)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if brightness is not None:
            r.brightness = float(min(max(brightness, 0.0), 1.0))
        self.lights.sync("app")
        return self.effect_state()

    def effect_state(self) -> dict | None:
        r = self.effect_renderer
        if r is None or not self.output.holds("test"):
            return None
        return {"effect": r.effect.name, "params": r.effect.params.to_dict(), "brightness": r.brightness}

    def stop_effect(self) -> None:
        self.output.release("test")
        self.effect_renderer = None
        self.lights.sync("app")

    # ---- lifecycle ---------------------------------------------------------------------
    def busy(self) -> str | None:
        """What holds this tree's LEDs for a capture, if anything."""
        holder = self.output.holder()
        if holder and holder.owner != "test":
            return holder.kind
        if self.walk and self.walk.state == "walking":
            return "walk"
        if self.session and self.session.busy():
            return "calibration"
        return None

    def summary(self) -> dict:
        s = self.config.settings
        lease = self.output.holder()
        return {"id": self.id, "name": s.tree_name, "default": self.id == self.app.config.default_tree,
                "led_count": self.index.total, "controllers": [
                    {"host": l.host or c.host, "name": c.name or (l.info.name if l.info else None), "start": sl.start,
                     "led_count": sl.count} for l, c, sl in zip(self.links, s.controllers, self.index.slices())],
                "lights_on": self.effect_state() is not None, "active_map": self.maps.active_id(),
                "lease": None if not lease else {"owner": lease.owner, "kind": lease.kind},
                "mqtt_topic": s.mqtt.base_topic}

    async def start(self) -> None:
        rec = self.recover()
        if rec["view"] or rec["walks"]:
            log.info("tree %s: recovered after restart: %s", self.id, rec)
        if any(l.host for l in self.links):
            for link in self.links:
                if not link.host:
                    continue
                try:
                    await link.refresh(with_cfg=True)
                except Exception as e:  # noqa: BLE001
                    log.warning("tree %s: WLED not reachable at start-up at %s: %s", self.id, link.host, e)
            self._apply_power()
            warning = self.map_check()
            if warning:
                log.warning("tree %s: active map: %s", self.id, warning)
            if self.brightness.boosted:  # the server stopped during a calibration (§5.4)
                try:
                    await self.brightness.restore()
                except Exception as e:  # noqa: BLE001
                    log.warning("tree %s: couldn't restore WLED brightness yet: %s", self.id, e)
        self._started = True
        for mon in self.monitors:
            mon.start()
        await self.lights.restore()  # lights that were on before the restart
        self.lights.start()
        self.scheduler.start()  # also applies a timer missed while the server was down
        await self.ha.start()

    async def stop(self) -> None:
        if self.session:
            await self.session.stop()
        if self.walk:
            await self.walk.stop("server shutdown")
        if self.brightness.boosted:
            try:
                await self.brightness.restore()
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't restore WLED brightness; will retry at start-up: %s", e)
        self.drawing.flush()
        await self.ha.stop()
        await self.scheduler.stop()
        await self.lights.stop()
        for mon in self.monitors:
            await mon.stop()
        for link in self.links:
            await link.close()
        self.output.stop()


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
