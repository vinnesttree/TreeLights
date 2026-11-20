"""Capture session: the server-driven state machine for one scan (architecture §7).

One view = one placement of the tree in front of the tripod phone. Per view:
  latency probe -> settle (tree stopped swaying, nobody in the picture) ->
  dark reference + reference constellation -> color check + auto-brightness ->
  [binary pass -> spot-check] -> sequential pass -> retries -> final check.

Every frame is verified (§7.4); movement starts a new segment (§7.5);
camera disconnects and WLED problems pause instead of corrupting data (§7.6).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import numpy as np

from ..config import Settings
from ..detection import binary
from ..detection.constellation import compare as compare_constellation
from ..detection.detector import color_class
from ..output.manager import OutputManager
from ..wled.link import MonitorEvent
from . import work
from .protocol import unpack_frame
from .store import Scan

log = logging.getLogger(__name__)

STEP_COLORS = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], np.float64)  # commanded R, G, B
MAX_ATTEMPTS = 4
MIN_WAIT_MS = 60
MAX_WAIT_MS = 1500
DEFAULT_WAIT_MS = 250
TARGET_PEAK = 90.0  # dark-subtracted DN for a typical LED


class CameraLost(Exception):
    pass


class Stopped(Exception):
    pass


class Moved(Exception):
    pass


@dataclass
class Calibration:
    """Per-view camera response learned from the color check (§7.3 steps 2-3)."""

    basis: np.ndarray = field(default_factory=lambda: STEP_COLORS.copy())  # observed RGB response per commanded color
    levels: np.ndarray = field(default_factory=lambda: np.array([64, 48, 80]))  # LED level per commanded color
    color_verify: bool = True
    binary_level: int = 40
    warnings: list[str] = field(default_factory=list)


@dataclass
class SegmentState:
    id: str
    ref_path: str
    ref_version: int
    constellation: np.ndarray
    last_good_step: int
    since_check: list[tuple[int, int]] = field(default_factory=list)  # (step, led) since last good check


class CaptureSession:
    def __init__(self, scan: Scan, output: OutputManager, executor: concurrent.futures.Executor,
                 settings: Settings, owner: str = "calibration", seed: int = 0):
        self.scan = scan
        self.output = output
        self.executor = executor
        self.settings = settings
        self.cfg = settings.capture
        self.n = scan.meta["led_count"]
        self.owner = owner
        self.rng = np.random.default_rng(seed)
        self._camera_send: Callable[[dict], Awaitable[None]] | None = None
        self._camera_info: dict = {}
        self._camera_ready = asyncio.Event()
        self._waiting: dict[int, asyncio.Future] = {}
        self._probe: asyncio.Future | None = None
        self._last_camera_msg = 0.0
        self._step = max([int(r.get("step", 0)) for r in scan.detections.read()] + [0]) + 1
        self._task: asyncio.Task | None = None
        self._stop = False
        self._resume = asyncio.Event()
        self._resume.set()
        self._interrupt: MonitorEvent | None = None
        self._motion = False
        self.wait_ms = float(DEFAULT_WAIT_MS)
        self.base_wait_ms = float(DEFAULT_WAIT_MS)
        self._clean_steps = 0
        self.status: dict[str, Any] = {"type": "status", "scan": scan.id, "phase": "idle", "done": 0, "total": 0,
                                       "warnings": [], "message": "", "paused": False, "camera": False}
        self._status_listeners: list[Callable[[dict], None]] = []
        self.predicted_visible: Callable[[str], set[int] | None] | None = None  # set by live reconstruction
        self.after_view: Callable[[str], Awaitable[None]] | None = None
        self.seg: SegmentState | None = None
        self.cal = Calibration()
        self._hb_task: asyncio.Task | None = None

    # ================================================================ status
    def on_status(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        self._status_listeners.append(fn)
        fn(dict(self.status))
        return lambda: self._status_listeners.remove(fn) if fn in self._status_listeners else None

    def _emit(self, **fields: Any) -> None:
        self.status.update(fields)
        self.status["wait_ms"] = round(self.wait_ms)
        snap = dict(self.status)
        for fn in list(self._status_listeners):
            try:
                fn(snap)
            except Exception:  # noqa: BLE001
                log.exception("status listener failed")

    def _warn(self, msg: str) -> None:
        if msg not in self.status["warnings"]:
            self.status["warnings"] = self.status["warnings"] + [msg]
        self.scan.events.append({"type": "warning", "message": msg})
        self._emit()

    # ================================================================ camera link
    async def camera_connected(self, send: Callable[[dict], Awaitable[None]], hello: dict) -> None:
        self._camera_send = send
        self._camera_info = hello
        self._last_camera_msg = time.monotonic()
        self._camera_ready.set()
        self.scan.events.append({"type": "camera_connected", "camera": hello.get("camera"), "up_cam": hello.get("up_cam")})
        await send({"type": "welcome", "scan": self.scan.id,
                    "settings": {"width": self.cfg.width, "height": self.cfg.height, "jpeg_quality": self.cfg.jpeg_quality}})
        self._emit(camera=True)
        if self._hb_task is None:
            self._hb_task = asyncio.ensure_future(self._heartbeat_watch())

    def camera_disconnected(self, reason: str = "disconnected") -> None:
        if not self._camera_ready.is_set():
            return
        self._camera_ready.clear()
        self._camera_send = None
        for fut in self._waiting.values():
            if not fut.done():
                fut.set_exception(CameraLost(reason))
        self._waiting.clear()
        if self._probe and not self._probe.done():
            self._probe.set_exception(CameraLost(reason))
        self.scan.events.append({"type": "camera_lost", "reason": reason})
        self._emit(camera=False)

    async def _heartbeat_watch(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self._camera_ready.is_set() and time.monotonic() - self._last_camera_msg > 5.0:
                self.camera_disconnected("heartbeat timeout")

    async def on_camera_text(self, msg: dict) -> None:
        self._last_camera_msg = time.monotonic()
        t = msg.get("type")
        if t == "hb":
            if msg.get("up_cam") is not None:
                self._camera_info["up_cam"] = msg["up_cam"]
        elif t == "motion":
            self._motion = True
        elif t == "camera_info":
            self._camera_info.update({k: msg[k] for k in ("camera", "up_cam") if k in msg})
            self.scan.events.append({"type": "camera_info", "camera": msg.get("camera")})
        elif t == "probe_result" and self._probe and not self._probe.done():
            self._probe.set_result(msg.get("delays_ms") or [])

    def on_camera_binary(self, data: bytes) -> None:
        self._last_camera_msg = time.monotonic()
        header, jpeg = unpack_frame(data)
        fut = self._waiting.pop(int(header.get("step", -1)), None)
        if fut and not fut.done():
            fut.set_result((header, jpeg))  # frames for unexpected steps are dropped

    # ================================================================ control
    def on_monitor(self, ev: MonitorEvent) -> None:
        if self._task and not self._task.done() and ev.kind != "recovered":
            self._interrupt = ev

    def start_view(self, mode: str = "hybrid", view_id: str | None = None) -> str:
        if self._task and not self._task.done():
            raise RuntimeError("a view is already being captured")
        vid = view_id or self.scan.add_view({"mode": mode})
        self._stop = False
        self._task = asyncio.ensure_future(self._run_view(vid, mode))
        return vid

    def busy(self) -> bool:
        return bool(self._task and not self._task.done())

    async def stop(self) -> None:
        self._stop = True
        self._resume.set()
        for fut in self._waiting.values():
            if not fut.done():
                fut.set_exception(Stopped())
        if self._task:
            try:
                await self._task
            except Exception:  # noqa: BLE001
                pass
        if self._hb_task:
            self._hb_task.cancel()
            self._hb_task = None

    def resume(self) -> None:
        self._resume.set()

    async def wait_done(self) -> None:
        if self._task:
            await self._task

    # ================================================================ primitives
    def _next_step(self) -> int:
        s = self._step
        self._step += 1
        return s

    async def _offload(self, fn: Callable, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self.executor, fn, *args)

    async def _show(self, frame: np.ndarray, repeats: int = 2) -> None:
        if self._stop:
            raise Stopped()
        ok, holder = self.output.acquire(self.owner, "calibration", ttl=30)
        if not ok:
            raise RuntimeError(f"LED output is held by {holder.owner if holder else 'someone'}")
        await asyncio.wrap_future(self.output.show(self.owner, frame, repeats=repeats))

    async def _capture(self, kind: str, wait_ms: float | None = None) -> tuple[int, str]:
        """Ask the camera for one frame taken after wait_ms; returns (step, file path)."""
        if self._stop:
            raise Stopped()
        if not self._camera_ready.is_set():
            raise CameraLost("not connected")
        seg = self.seg.id if self.seg else "setup"
        step = self._next_step()
        fut = asyncio.get_running_loop().create_future()
        self._waiting[step] = fut
        wait = self.wait_ms if wait_ms is None else wait_ms
        send = self._camera_send
        if send is None:
            raise CameraLost("not connected")
        await send({"type": "capture", "step": step, "kind": kind, "wait_ms": int(wait)})
        try:
            _header, jpeg = await asyncio.wait_for(fut, timeout=wait / 1000 + 10)
        except asyncio.TimeoutError:
            self._waiting.pop(step, None)
            raise CameraLost("frame timeout")
        path = self.scan.write_frame(seg, step, kind, jpeg)
        self.output.renew(self.owner, ttl=30)
        return step, str(path)

    def _frame(self, leds: dict[int, np.ndarray] | None = None) -> np.ndarray:
        f = np.zeros((self.n, 3), np.uint8)
        if leds:
            for i, c in leds.items():
                f[i] = c
        return f

    def _color_frame(self, idx: list[int] | np.ndarray, cls: int | None, level: int | None = None) -> np.ndarray:
        f = np.zeros((self.n, 3), np.uint8)
        if cls is None:
            f[np.asarray(idx, int)] = level or 48
        else:
            lv = int(level if level is not None else self.cal.levels[cls])
            f[np.asarray(idx, int)] = (STEP_COLORS[cls] * lv).astype(np.uint8)
        return f

    def _constellation_leds(self) -> np.ndarray:
        return np.unique(np.linspace(0, self.n - 1, min(40, self.n)).round().astype(int))

    async def _checkpoint(self) -> None:
        """Called between steps: honours stop/pause, WLED problems and motion spikes."""
        if self._stop:
            raise Stopped()
        if self._interrupt is not None:
            ev, self._interrupt = self._interrupt, None
            self.scan.events.append({"type": "wled_problem", "kind": ev.kind, "detail": ev.detail})
            if ev.kind == "foreign_source":
                await self._pause(f"{ev.detail}. Stop the other sender, then resume.")
            else:
                self._emit(message=f"WLED: {ev.detail}. Re-checking...")
                await asyncio.sleep(3.0)
            raise Moved()  # treat like movement: re-verify and re-capture since the last good check
        if not self._resume.is_set():
            self._emit(paused=True)
            await self._resume.wait()
            self._emit(paused=False)
            if self._stop:
                raise Stopped()
        if self._motion:
            self._motion = False
            if await self._constellation_moved():
                raise Moved()

    async def _pause(self, message: str) -> None:
        self._resume.clear()
        self._emit(paused=True, message=message)
        self.scan.events.append({"type": "paused", "message": message})
        await self._resume.wait()
        self._emit(paused=False, message="")
        if self._stop:
            raise Stopped()

    # ================================================================ view
    async def _run_view(self, vid: str, mode: str) -> None:
        self._emit(view=vid, phase="starting", message="", warnings=[], done=0, total=0)
        try:
            await self._wait_camera()
            await self._latency_probe()
            await self._new_segment(vid, settle=True)
            await self._color_check()
            remaining = self._remaining(vid)
            decoded: set[int] = set()
            if mode == "hybrid":
                decoded = self._captured_in_view(vid, kinds=("binary",))
                if not decoded:
                    decoded = await self._with_movement(vid, self._binary_pass, vid)
            queue = self._sequential_queue(vid, remaining, decoded, mode)
            await self._with_movement(vid, self._sequential_pass, vid, queue, "seq")
            await self._with_movement(vid, self._retry_pass, vid)
            await self._with_movement(vid, self._final_check)
            self.scan.set_view(vid, status="done")
            self.scan.events.append({"type": "view_done", "view": vid})
            self._emit(phase="done", message="View complete. Turn the tree for the next view.")
            if self.after_view:
                # In the background: the view is finished, the next one may start right away.
                asyncio.ensure_future(self.after_view(vid))
        except Stopped:
            self.scan.set_view(vid, status="stopped")
            self._emit(phase="stopped", message="Stopped.")
        except Exception as e:  # noqa: BLE001
            log.exception("view %s failed", vid)
            self.scan.set_view(vid, status="failed", error=str(e))
            self._emit(phase="failed", message=f"Capture failed: {e}")
        finally:
            self.output.release(self.owner)

    async def _wait_camera(self) -> None:
        if not self._camera_ready.is_set():
            self._emit(phase="waiting_camera", message="Waiting for the camera page to connect...")
            await self._camera_ready.wait()

    async def _with_movement(self, vid: str, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """Run a phase; on movement or camera loss start a new segment and run it again
        (each phase re-derives its remaining work from what has been recorded)."""
        moves: list[float] = []
        while True:
            try:
                return await fn(*args)
            except (Moved, CameraLost) as e:
                lost = isinstance(e, CameraLost)
                seg = self.seg
                if seg:
                    self.scan.invalidate(seg.id, seg.last_good_step, "camera_lost" if lost else "moved")
                if lost:
                    self._emit(phase="paused", message="Camera disconnected. Reopen the Calibrate page to continue.")
                    await self._wait_camera()
                    if await self._resume_same_segment():
                        continue
                else:
                    moves.append(time.monotonic())
                    moves = [t for t in moves if moves[-1] - t < 120]
                    if len(moves) > 3:
                        await self._pause("The picture keeps moving. Check the tripod, then resume.")
                        moves.clear()
                    self._emit(message="Movement detected: starting a new segment and re-capturing.")
                await self._new_segment(vid, settle=True)

    async def _resume_same_segment(self) -> bool:
        """After a reconnect: same segment if the constellation still matches."""
        if not self.seg:
            return False
        try:
            moved = await self._constellation_moved()
        except CameraLost:
            return False
        return not moved

    def _remaining(self, vid: str) -> list[int]:
        """LEDs without a valid capture in this view yet (supports resuming a view)."""
        return list(range(self.n))

    # ================================================================ setup phases
    async def _latency_probe(self) -> None:
        self._emit(phase="latency", message="Measuring delay...")
        loop = asyncio.get_running_loop()
        self._probe = loop.create_future()
        pid = self._next_step()
        allon = self._color_frame(np.arange(self.n), None, level=24)
        off = self._frame()
        send = self._camera_send
        if send is None:
            raise CameraLost("not connected")
        await send({"type": "probe_start", "id": pid, "count": 10})
        for k in range(10):
            await self._show(allon if k % 2 == 0 else off)
            await send({"type": "probe_mark", "id": pid, "k": k, "on": k % 2 == 0})
            await asyncio.sleep(0.45)
        await send({"type": "probe_end", "id": pid})
        try:
            delays = await asyncio.wait_for(self._probe, timeout=5)
        except asyncio.TimeoutError:
            delays = []
        delays = [float(d) for d in delays if d is not None and 0 <= float(d) < 3000]
        if len(delays) >= 4:
            wait = float(np.percentile(delays, 95)) + 34 + 20
        else:
            wait = DEFAULT_WAIT_MS
            self._warn("Couldn't measure the delay; using a safe default.")
        self.base_wait_ms = self.wait_ms = float(np.clip(wait, MIN_WAIT_MS, MAX_WAIT_MS))
        self.scan.events.append({"type": "latency", "delays_ms": delays, "wait_ms": self.wait_ms})

    async def _new_segment(self, vid: str, settle: bool) -> None:
        """Wait for the scene to settle, then take the dark reference and constellation."""
        self._emit(phase="settling", message="Waiting for the tree to stop moving...")
        cl = self._constellation_leds()
        white = self._color_frame(cl, None, level=40)
        tmp_seg = self.scan.add_segment(vid, self._camera_meta())
        self.seg = SegmentState(tmp_seg, "", 0, np.zeros((0, 2)), self._step)
        prev_pts = None
        prev_dark = None
        t0 = time.monotonic()
        while True:
            await self._show(self._frame())
            _s, dark = await self._capture("dark")
            ref_path = str(self.scan.ref_path(tmp_seg, 99))
            await self._offload(work.make_reference, [dark], ref_path)
            await self._show(white)
            _s, cpath = await self._capture("constellation")
            pts = np.array(await self._offload(work.constellation_frame, cpath, ref_path)).reshape(-1, 2)
            if not settle:
                break
            if prev_pts is not None:
                still = not compare_constellation(prev_pts, pts).moved
                quiet = (await self._offload(work.dark_changed_fraction, prev_dark, dark)) < 0.002
                if still and quiet and len(pts) >= 3:
                    break
            if time.monotonic() - t0 > 30:
                self._emit(message="Still waiting: the tree is moving or something in the picture keeps changing.")
            prev_pts, prev_dark = pts, dark
            await asyncio.sleep(0.6)
        # Final reference from several dark frames.
        await self._show(self._frame())
        darks = [(await self._capture("dark"))[1] for _ in range(4)]
        ref_path = str(self.scan.ref_path(tmp_seg, 0))
        info = await self._offload(work.make_reference, darks, ref_path)
        await self._show(white)
        _s, cpath = await self._capture("constellation")
        pts = np.array(await self._offload(work.constellation_frame, cpath, ref_path)).reshape(-1, 2)
        if len(pts) < 3:
            self._warn("Few reference LEDs visible from this side; movement checks are weaker.")
        self.seg = SegmentState(tmp_seg, ref_path, 0, pts, self._step)
        self.scan.set_segment(tmp_seg, constellation=pts.tolist(), noise=info["sigma"], up_cam=self._camera_info.get("up_cam"))
        self._emit(segment=tmp_seg)

    def _camera_meta(self) -> dict:
        cam = self._camera_info.get("camera") or {}
        return {"width": cam.get("width", self.cfg.width), "height": cam.get("height", self.cfg.height),
                "device": cam.get("deviceId"), "locks": cam.get("locks"), "up_cam": self._camera_info.get("up_cam")}

    async def _constellation_moved(self) -> bool:
        seg = self.seg
        assert seg is not None
        await self._show(self._color_frame(self._constellation_leds(), None, level=40))
        step, cpath = await self._capture("constellation")
        pts = np.array(await self._offload(work.constellation_frame, cpath, seg.ref_path)).reshape(-1, 2)
        m = compare_constellation(seg.constellation, pts)
        self.scan.events.append({"type": "constellation", "segment": seg.id, "step": step, "moved": m.moved,
                                 "shift": m.median_shift if np.isfinite(m.median_shift) else None, "matched": m.matched_fraction})
        if not m.moved:
            seg.last_good_step = step
            seg.since_check.clear()
        return m.moved

    async def _color_check(self) -> None:
        """Learn how commanded R/G/B look to this camera; choose LED levels (§7.3 steps 2-3)."""
        self._emit(phase="color_check", message="Checking colors and brightness...")
        seg = self.seg
        assert seg is not None
        cl = self._constellation_leds()
        pts = seg.constellation.tolist()
        cal = Calibration()
        if len(pts) < 3:
            self.cal = cal
            self._warn("Color check skipped: too few reference LEDs visible.")
            return
        test_level = 64
        responses, mags = [], []
        for cls in range(3):
            await self._show(self._color_frame(cl, cls, level=test_level))
            _s, p = await self._capture("color")
            r = await self._offload(work.blob_colors, p, seg.ref_path, pts)
            responses.append(np.array(r["colors"]))
            mags.append(np.array(r["peaks"]))
        sigma = r["sigma"]
        strong = np.all([m > 10 * sigma + 4 for m in mags], axis=0)
        if strong.sum() < 3:
            self.cal = cal
            self._warn("Color check inconclusive; colors won't be used to verify frames.")
            cal.color_verify = False
            return
        basis = []
        for cls in range(3):
            v = responses[cls][strong]
            v = v / np.linalg.norm(v, axis=1, keepdims=True)
            basis.append(np.median(v, axis=0))
        cal.basis = np.array(basis)
        # Color order: commanded red should be dominant in the camera's red channel, etc.
        dominant = [int(np.argmax(b)) for b in cal.basis]
        if dominant != [0, 1, 2]:
            names = "RGB"
            order = "".join(names[dominant.index(k)] if k in dominant else "?" for k in range(3))
            cal.warnings.append(f"Colors look swapped (WLED color order may be wrong, e.g. {order}). Verification compensates.")
        # Separability: can each strong blob's color be classified correctly?
        correct = []
        for cls in range(3):
            for v in responses[cls][strong]:
                c, margin = color_class(v, cal.basis)
                correct.append(c == cls and margin > 0.05)
        cal.color_verify = float(np.mean(correct)) >= 0.9
        if not cal.color_verify:
            cal.warnings.append("This camera doesn't separate the LED colors well; relying on position checks.")
        # Levels: aim the 75th percentile of LED peaks at TARGET_PEAK.
        for cls in range(3):
            p75 = float(np.percentile(mags[cls][strong], 75))
            cal.levels[cls] = int(np.clip(test_level * TARGET_PEAK / max(p75, 1.0), 6, 255))
        cal.binary_level = int(np.clip(min(cal.levels) * 0.6, 6, 255))
        if np.any(cal.levels >= 255):
            cal.warnings.append("LEDs are dim for this camera even at full level; raise WLED's master brightness if detection is poor.")
        self.cal = cal
        for w in cal.warnings:
            self._warn(w)
        self.scan.set_segment(seg.id, color_basis=cal.basis.tolist(), levels=cal.levels.tolist(), color_verify=cal.color_verify)

    # ================================================================ binary pass
    async def _binary_pass(self, vid: str) -> set[int]:
        seg = self.seg
        assert seg is not None
        self._emit(phase="binary", message="Binary pass: all LEDs blinking codes...", done=0, total=binary.N_BITS)
        level = self.cal.binary_level
        allon_frame = self._color_frame(np.arange(self.n), 1, level=level)
        await self._show(allon_frame)
        allon = [(await self._capture("allon"))[1] for _ in range(2)]
        frames = binary.pattern_frames(self.n, (0, level, 0))
        pairs: dict[int, tuple[str, str]] = {}
        for bit in range(binary.N_BITS):
            for attempt in range(3):
                await self._checkpoint()
                await self._show(frames[2 * bit])
                _s, pp = await self._capture(f"bit{bit}p")
                await self._show(frames[2 * bit + 1])
                _s, pc = await self._capture(f"bit{bit}c")
                chk = await self._offload(work.binary_pair, seg.ref_path, allon, self.n, bit, pp, pc)
                if chk["ok"]:
                    pairs[bit] = (pp, pc)
                    break
                self.scan.events.append({"type": "binary_pair_retry", "segment": seg.id, "bit": bit, **chk})
                if chk["reason"] == "moved":
                    raise Moved()
                self._bump_wait()
            else:
                self._warn("Binary pass unreliable from this side; using one-at-a-time capture.")
                self.scan.events.append({"type": "binary_discarded", "segment": seg.id, "reason": "pairs"})
                return set()
            self._emit(done=bit + 1)
        results = await self._offload(work.binary_decode, seg.ref_path, allon, self.n, pairs)
        step = self._step
        by_led: dict[int, list[dict]] = {}
        for r in results:
            by_led.setdefault(r["index"], []).append(r)
        for led, rs in by_led.items():
            cands = [{"x": r["x"], "y": r["y"], "score": float(min(1.0, r["snr"] / 40) * r["reliability"]),
                      "snr": r["snr"], "reliability": r["reliability"]} for r in rs]
            self.scan.record({"segment": seg.id, "view": vid, "led": led, "kind": "binary", "step": step, "candidates": cands})
        # Spot-check: a few decoded LEDs lit one at a time must appear where they were decoded.
        good = [led for led, rs in by_led.items() if len(rs) == 1 and rs[0]["reliability"] >= 0.5]
        sample = list(self.rng.choice(good, size=min(10, len(good)), replace=False)) if good else []
        mismatches = 0
        self._emit(phase="spot_check", message="Spot-checking decoded LEDs...")
        for led in sample:
            res = await self._capture_led(vid, int(led), kind="spot")
            if res and res["candidates"]:
                c = res["candidates"][0]
                d = by_led[int(led)][0]
                if np.hypot(c["x"] - d["x"], c["y"] - d["y"]) > 2.5:
                    mismatches += 1
        if mismatches:
            self._warn("Binary results failed the spot-check; capturing this view one LED at a time.")
            self.scan.events.append({"type": "binary_discarded", "segment": seg.id, "reason": "spot_check", "mismatches": mismatches})
            return set()
        self.scan.events.append({"type": "binary_done", "segment": seg.id, "decoded": len(by_led), "peaks": len(results)})
        # Confirm nothing moved during the pass, so its results get a good checkpoint.
        if await self._constellation_moved():
            raise Moved()
        return set(by_led)

    def _sequential_queue(self, vid: str, remaining: list[int], decoded: set[int], mode: str) -> list[int]:
        undecoded = [i for i in remaining if i not in decoded]
        if mode != "hybrid" or self.cfg.thorough or not decoded:
            queue = undecoded
        else:
            und = set(undecoded)
            queue = []
            i = 0
            while i < self.n:
                if i in und:
                    j = i
                    while j + 1 < self.n and j + 1 in und:
                        j += 1
                    run = list(range(i, j + 1))
                    bordered = (i - 1 in decoded) or (j + 1 in decoded)
                    if len(run) <= self.cfg.short_run and bordered:
                        queue += run
                    i = j + 1
                else:
                    i += 1
            predicted = self.predicted_visible(vid) if self.predicted_visible else None
            if predicted:
                queue = sorted(set(queue) | (predicted & und))
        order = np.array(queue, int)
        self.rng.shuffle(order)
        return order.tolist()

    # ================================================================ sequential pass
    async def _sequential_pass(self, vid: str, queue: list[int], kind: str) -> None:
        done_here = self._captured_in_view(vid, kinds=("seq",))
        todo = [i for i in queue if i not in done_here]
        total = len(queue)
        self._emit(phase="sequential", message="Capturing LEDs one at a time...", done=total - len(todo), total=total)
        attempts: dict[int, int] = {}
        pending = list(todo)
        since_check = 0
        while pending:
            await self._checkpoint()
            if since_check >= self.cfg.constellation_every:
                since_check = 0
                if await self._constellation_moved():
                    raise Moved()
            led = pending.pop(0)
            res = await self._capture_led(vid, led, kind=kind)
            since_check += 1
            if res is None:
                attempts[led] = attempts.get(led, 0) + 1
                if attempts[led] < MAX_ATTEMPTS:
                    pending.insert(min(len(pending), 3), led)  # retry soon, not immediately
                else:
                    self.scan.record({"segment": self.seg.id, "view": vid, "led": led, "kind": kind, "step": self._step,
                                      "candidates": [], "unverified": True})
            self._emit(done=total - len(pending))

    async def _capture_led(self, vid: str, led: int, kind: str) -> dict | None:
        """Light one LED, capture and verify (§7.4). Returns the recorded result or
        None if the frame was rejected (caller re-queues)."""
        seg = self.seg
        assert seg is not None
        step_cls = self._step % 3
        await self._show(self._color_frame([led], step_cls))
        step, path = await self._capture("led")
        expected = self.cal.basis[step_cls].tolist() if self.cal.color_verify else None
        det = await self._offload(work.detect_frame, path, seg.ref_path, expected)
        verdict = self._verify(det, step_cls)
        if verdict != "ok":
            self.scan.events.append({"type": "rejected", "segment": seg.id, "step": step, "led": led, "why": verdict})
            if verdict in ("stale", "recent_position"):
                self._bump_wait()
            elif verdict in ("scene_changed", "exposure_jump"):
                await self._refresh_reference()
            return None
        self._clean_step()
        rec = {"segment": seg.id, "view": vid, "led": led, "kind": kind, "step": step, "candidates": det["candidates"]}
        self.scan.record(rec)
        seg.since_check.append((step, led))
        if det["candidates"]:
            c = det["candidates"][0]
            self._recent = (getattr(self, "_recent", []) + [(c["x"], c["y"])])[-2:]
            self._emit(last={"x": c["x"], "y": c["y"], "led": led})
        else:
            self._recent = (getattr(self, "_recent", []) + [None])[-2:]
        return rec

    def _verify(self, det: dict, step_cls: int) -> str:
        if not det["valid"]:
            return det["reason"] or "invalid"
        cands = det["candidates"]
        if not cands:
            return "ok"  # seen nothing in a valid frame
        best = cands[0]
        if self.cal.color_verify:
            cls, margin = color_class(np.array(best["color"]), self.cal.basis)
            if cls != step_cls or margin < 0.05:
                return "stale"  # the previous LED (previous color) is what we see
        for prev in getattr(self, "_recent", []):
            if prev is not None and np.hypot(best["x"] - prev[0], best["y"] - prev[1]) < 3.0:
                return "recent_position"
        return "ok"

    def _bump_wait(self) -> None:
        self.wait_ms = float(min(MAX_WAIT_MS, self.wait_ms * 1.3))
        self._clean_steps = 0
        self._emit()

    def _clean_step(self) -> None:
        self._clean_steps += 1
        if self._clean_steps >= 50:
            self._clean_steps = 0
            self.wait_ms = float(max(self.base_wait_ms, self.wait_ms * 0.95))

    async def _refresh_reference(self) -> None:
        seg = self.seg
        assert seg is not None
        self._emit(message="Something changed in the picture; taking a new dark reference...")
        await asyncio.sleep(1.0)
        await self._show(self._frame())
        darks = [(await self._capture("dark"))[1] for _ in range(3)]
        seg.ref_version += 1
        ref_path = str(self.scan.ref_path(seg.id, seg.ref_version))
        await self._offload(work.make_reference, darks, ref_path)
        seg.ref_path = ref_path
        if await self._constellation_moved():
            raise Moved()

    def _captured_in_view(self, vid: str, kinds: tuple[str, ...]) -> set[int]:
        segs = set(self.scan.view(vid)["segments"])
        return {int(r["led"]) for r in self.scan.valid_records(segs, kinds)}

    # ================================================================ retries and final check
    async def _retry_pass(self, vid: str) -> None:
        segs = self.scan.view(vid)["segments"]
        recs = [r for r in self.scan.detections.read() if r.get("segment") in segs and r.get("kind") == "seq"]
        counts: dict[int, int] = {}
        ambiguous: set[int] = set()
        for r in recs:
            counts[r["led"]] = counts.get(r["led"], 0) + 1
            c = r.get("candidates") or []
            if len(c) >= 2 and c[1]["score"] >= 0.6 * c[0]["score"]:
                ambiguous.add(int(r["led"]))
        todo = [i for i in sorted(ambiguous) if counts.get(i, 0) <= self.cfg.retries]
        if not todo:
            return
        self._emit(phase="retries", message="Re-capturing ambiguous LEDs...", done=0, total=len(todo))
        for k in range(self.cfg.retries):
            for n, led in enumerate(todo):
                await self._checkpoint()
                await self._capture_led(vid, led, kind="seq")
                self._emit(done=n + 1)

    async def _final_check(self) -> None:
        if self.seg and len(self.seg.constellation) >= 3 and await self._constellation_moved():
            raise Moved()
