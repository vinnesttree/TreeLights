"""A live walk-around capture (architecture §7.9).

The phone connects to /ws/walk. On "start" the server takes the LED output and
plays the walk pattern (every LED blinking its own index) for as long as the walk
lasts. While the user walks around the tree with the camera running, the phone
streams:

  - frames, ~8 per second (JPEG, with the phone time and the latest orientation
    quaternion): kept on disk for processing and analysed live for feedback;
  - batches of orientation samples (phone time, quaternion), ~60 Hz;
  - "rec_start": the phone time its full-rate video recording started.

Every couple of seconds the latest 30 s of live frames are decoded in a worker
thread (what was found earlier is kept), and the phone gets a status: whether the pattern is locked, how many LEDs are
identified, coverage per 30° sector of the walk (from the orientation sensor),
and warnings (LEDs out of frame, too few LEDs, pattern not found). During the first
two cycles the LED brightness may be adjusted (between cycles) if nearly all LEDs
saturate or they are very faint; then it stays fixed.

On "stop" the pattern stops, and the phone uploads the recording to the view's
upload endpoint; processing then uses both the video and the live frames
(walk/job.py).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable

import cv2
import numpy as np

from ..capture.store import Scan
from ..output.manager import OutputManager
from . import gyro as gyromod
from .blobs import FLUX, PEAK, SAT, X, Y, detect_blobs
from .pattern import CYCLE_T, WalkPattern
from .pipeline import Source, process_source
from .tracking import Frame

log = logging.getLogger(__name__)

OWNER = "walk"
SECTORS = 12
STATUS_EVERY_S = 2.5
MAX_WALK_S = 600.0
MIN_SECTOR_LEDS = 15  # identified LEDs for a sector to count as covered
ADAPT_S = 2 * CYCLE_T  # brightness may be adjusted only this early in the walk
MAX_LEVEL_CHANGES = 2


def walk_dir(scan: Scan, vid: str) -> Path:
    p = scan.root / "walk" / vid
    (p / "live").mkdir(parents=True, exist_ok=True)
    return p


class WalkSession:
    def __init__(self, scan: Scan, output: OutputManager, level: int = 48):
        self.scan = scan
        self.output = output
        # Own threads: blob detection keeps up with ~8 frames/s; the periodic analysis
        # mustn't queue behind it (or behind solve jobs in the shared process pool).
        self.detect_pool = concurrent.futures.ThreadPoolExecutor(2, thread_name_prefix="walk-detect")
        self.analysis_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="walk-analysis")
        self.cal_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="walk-gyro")
        self._cal: concurrent.futures.Future | None = None
        self.n = int(scan.meta["led_count"])
        self.pattern = WalkPattern(self.n, level)
        self.vid: str | None = None
        self.dir: Path | None = None
        self.state = "idle"  # idle | walking | stopped
        self.camera: dict = {}
        self.frames: list[Frame] = []
        self.frame_meta: list[dict] = []
        self.log_t: list[float] = []
        self.log_q: list[list[float]] = []
        self.rec_start: float | None = None
        self.started_at = 0.0
        self.send: Callable[[dict], Awaitable[None]] | None = None
        self.status: dict = {"type": "walk_status", "state": "idle"}
        self._lock = threading.Lock()
        self._analysis: concurrent.futures.Future | None = None
        self._task: asyncio.Task | None = None
        self._live_log = None
        self._motion_log = None
        self._pending_detect = 0
        self.disconnected_at: float | None = None
        self.coverage = LiveCoverage()  # touched only by the analysis thread
        self._level_changes = 0

    # ---- lifecycle ---------------------------------------------------------------
    def start(self, camera: dict) -> str:
        if self.state == "walking":
            return self.vid or ""
        ok, holder = self.output.acquire(OWNER, "calibration", ttl=MAX_WALK_S)
        if not ok:
            raise RuntimeError(f"LED output is in use ({holder.kind if holder else '?'}).")
        self.camera = dict(camera or {})
        self.vid = self.scan.add_view({"mode": "walk", "status": "walking", "camera": self.camera,
                                       "level": self.pattern.level})
        self.dir = walk_dir(self.scan, self.vid)
        self._live_log = open(self.dir / "live.jsonl", "a")
        self._motion_log = open(self.dir / "motion.jsonl", "a")
        self.output.set_renderer(OWNER, self.pattern.renderer(), fps=40)
        self.state = "walking"
        self.started_at = time.monotonic()
        self.status = {"type": "walk_status", "state": "walking", "view": self.vid, "scan": self.scan.id}
        self._task = asyncio.ensure_future(self._status_loop())
        return self.vid

    async def stop(self, reason: str = "stopped") -> dict:
        if self.state != "walking":
            return self.status
        self.state = "stopped"
        self.output.release(OWNER)
        if self._task:
            self._task.cancel()
        # Let queued detections finish so the stored frame list is complete.
        for _ in range(100):
            if self._pending_detect == 0:
                break
            await asyncio.sleep(0.05)
        for f in (self._live_log, self._motion_log):
            if f:
                f.close()
        self._live_log = self._motion_log = None
        self.scan.set_view(self.vid, status="awaiting_upload", rec_start=self.rec_start, live_frames=len(self.frame_meta),
                           duration_s=round(time.monotonic() - self.started_at, 1), stop_reason=reason,
                           pattern={"cycle_s": CYCLE_T, "level": self.pattern.level})
        self.status = dict(self.status, state="stopped", reason=reason)
        await self._emit(self.status)
        for pool in (self.detect_pool, self.analysis_pool, self.cal_pool):
            pool.shutdown(wait=False)
        return self.status

    # ---- input from the phone -------------------------------------------------------
    def on_motion(self, t: list[float], q: list[list[float]]) -> None:
        if self.state != "walking" or not t or len(t) != len(q):
            return
        with self._lock:
            for tt, qq in zip(t, q):
                if len(qq) != 4 or (self.log_t and tt <= self.log_t[-1]):
                    continue
                self.log_t.append(float(tt))
                self.log_q.append([float(v) for v in qq])
                if self._motion_log:
                    self._motion_log.write(json.dumps({"t": float(tt), "q": [float(v) for v in qq]}) + "\n")
        if self._motion_log:
            self._motion_log.flush()

    def on_rec_start(self, t: float) -> None:
        self.rec_start = float(t)
        if self.vid:
            self.scan.set_view(self.vid, rec_start=self.rec_start)

    def on_frame(self, header: dict, jpeg: bytes) -> None:
        if self.state != "walking" or self.dir is None:
            return
        t = float(header["t"])
        i = len(self.frame_meta)
        name = f"{i:05d}.jpg"
        (self.dir / "live" / name).write_bytes(jpeg)
        meta = {"i": i, "t": t, "q": header.get("q"), "file": name}
        self.frame_meta.append(meta)
        if self._live_log:
            self._live_log.write(json.dumps(meta) + "\n")
            self._live_log.flush()
        self.output.renew(OWNER, ttl=MAX_WALK_S)
        with self._lock:
            self._pending_detect += 1
        fut = self.detect_pool.submit(_detect, jpeg)
        fut.add_done_callback(lambda f, t=t: self._detected(t, f))

    def _detected(self, t: float, fut: concurrent.futures.Future) -> None:
        try:
            blobs, w, h = fut.result()
            with self._lock:
                self.frames.append(Frame(t, blobs))
                self.camera.setdefault("live_size", [w, h])
        except Exception:  # noqa: BLE001
            log.exception("blob detection failed")
        finally:
            with self._lock:
                self._pending_detect -= 1

    # ---- live feedback ------------------------------------------------------------------
    async def _emit(self, msg: dict) -> None:
        if self.send:
            try:
                await self.send(msg)
            except Exception:  # noqa: BLE001
                pass

    async def _status_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self.state == "walking":
            await asyncio.sleep(STATUS_EVERY_S)
            if time.monotonic() - self.started_at > MAX_WALK_S:
                await self.stop("time limit")
                return
            if self._analysis is not None and not self._analysis.done():
                continue
            with self._lock:
                frames = sorted(self.frames, key=lambda f: f.t)
                cut = frames[-1].t - WINDOW_S - 5.0 if frames else 0.0
                frames = [Frame(f.t, f.blobs) for f in frames if f.t > cut]  # analysis may set .R; keep ours clean
                log_t = np.array(self.log_t)
                log_q = np.array(self.log_q)
            size = self.camera.get("live_size") or [self.camera.get("width", 0), self.camera.get("height", 0)]
            self._analysis = loop.run_in_executor(self.analysis_pool, live_status, frames, int(size[0]), int(size[1]),
                                                  self.n, log_t, log_q, time.monotonic() - self.started_at, self.coverage)
            try:
                st = await self._analysis
            except Exception as e:  # noqa: BLE001
                log.exception("live walk analysis failed")
                st = {"warnings": [f"analysis failed: {e}"]}
            req, self.coverage.cal_request = self.coverage.cal_request, None
            if req is not None and (self._cal is None or self._cal.done()):
                self._cal = self.cal_pool.submit(self.coverage.calibrate, req)
            self._adapt_level(st)
            self.status = {"type": "walk_status", "state": self.state, "view": self.vid, "scan": self.scan.id,
                           "level": self.pattern.level, "frames": len(self.frame_meta), **st}
            await self._emit(self.status)

    def _adapt_level(self, st: dict) -> None:
        """Brightness is settled during the first cycles of the walk, then left alone.

        Changes apply from the next pattern cycle (bit votes compare slots within one
        cycle, so a change never splits a comparison), but every change also disturbs
        the timing sync, which normalises brightness over a few seconds. So: only during
        ADAPT_S, only outside a wide dead band, at most MAX_LEVEL_CHANGES times. A
        saturated bright state is harmless (bits compare bright with dim); what matters
        is the dim state still being seen."""
        if time.monotonic() - self.started_at > ADAPT_S or self._level_changes >= MAX_LEVEL_CHANGES:
            return
        sat, peak = st.get("saturated_frac"), st.get("median_peak")
        if sat is None or peak is None:
            return
        lv = self.pattern.level
        if sat > 0.6 and lv > 12:
            self.pattern.set_level(int(lv * 0.6))
            self._level_changes += 1
        elif sat < 0.02 and peak < 25 and lv < 200:
            self.pattern.set_level(int(lv * 1.6) + 1)
            self._level_changes += 1


def _detect(jpeg: bytes) -> tuple[np.ndarray, int, int]:
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("bad JPEG")
    h, w = img.shape[:2]
    return detect_blobs(img[..., ::-1]), w, h


class LiveCoverage:
    """What the live feedback has found so far, kept across updates so each update only
    has to decode a recent window of frames (the cost stays bounded however long the
    walk takes)."""

    def __init__(self) -> None:
        self.model: gyromod.GyroModel | None = None
        self.last_cal_t = -np.inf
        self.identified: set[int] = set()
        self.sectors: list[set[int]] = [set() for _ in range(SECTORS)]
        self.locked_once = False
        self.cal_request: tuple | None = None  # tracks to calibrate the gyro from, set by live_status

    def calibrate(self, req: tuple) -> None:
        model = gyromod.calibrate(*req)
        if model.ok:
            self.model = model
            self.sectors = [set() for _ in range(SECTORS)]  # headings change with the mapping


WINDOW_S = 30.0  # live frames decoded per update (a few pattern cycles)
CAL_EVERY_S = 15.0  # retry the gyro calibration this often until it succeeds


def live_status(frames: list[Frame], width: int, height: int, n: int, log_t: np.ndarray, log_q: np.ndarray,
                elapsed: float, cov: LiveCoverage | None = None) -> dict:
    """Feedback for the walking user, from the live frames received so far."""
    cov = cov or LiveCoverage()
    out: dict = {"elapsed_s": round(elapsed, 1), "warnings": []}
    recent = [f for f in frames if frames and f.t > frames[-1].t - 2.0]
    if recent:
        b = np.concatenate([f.blobs for f in recent if len(f.blobs)] or [np.zeros((0, 6))])
        out["blobs_per_frame"] = round(len(b) / len(recent), 1)
        if len(b):
            out["saturated_frac"] = round(float((b[:, SAT] > 0).mean()), 3)
            out["median_peak"] = round(float(np.median(b[:, PEAK])), 1)
            # LEDs near the frame edges: the tree isn't centred / the user is too close.
            mx = 0.04 * max(width, height)
            edge = ((b[:, X] < mx) | (b[:, X] > width - mx) | (b[:, Y] < mx) | (b[:, Y] > height - mx)).mean()
            out["edge_frac"] = round(float(edge), 3)
            if edge > 0.15:
                out["warnings"].append("out_of_frame")
        if out["blobs_per_frame"] < 5:
            out["warnings"].append("few_leds")
    window = [f for f in frames if f.t > frames[-1].t - WINDOW_S] if frames else []
    if len(window) < 40 or width <= 0:
        out.update(locked=cov.locked_once, identified=len(cov.identified), sectors=[len(x) for x in cov.sectors],
                   covered=int(sum(1 for x in cov.sectors if len(x) >= MIN_SECTOR_LEDS)))
        return out
    t = np.array([f.t for f in window])
    # Decoding without the gyro: cheap, and the gyro mostly helps the full-rate video.
    res = process_source(Source("live", window, width, height, t), n, None, None)
    out["locked"] = bool(res.timing.ok)
    cov.locked_once |= res.timing.ok
    if not res.timing.ok and elapsed > 3 * CYCLE_T:
        out["warnings"].append("pattern_not_found")
    have_log = len(log_t) >= 10
    if have_log and (cov.model is None or not cov.model.ok) and t[-1] - cov.last_cal_t > CAL_EVERY_S and np.ptp(t) > 15:
        # The calibration takes seconds; the session runs it on its own thread.
        cov.last_cal_t = float(t[-1])
        cov.cal_request = (res.tracks.rows, t, log_t, log_q, width, height)
    cov.identified |= res.decoded_leds()
    out["identified"] = len(cov.identified)
    out["gyro_ok"] = bool(cov.model and cov.model.ok)
    # Coverage: which LEDs were identified while the camera faced each sector of the walk.
    if have_log:
        model = cov.model if (cov.model and cov.model.ok) else gyromod.GyroModel(gyromod.DEFAULT_M, False, 0.0, 0.0, 0.0, 0.0, False)
        h0 = model.heading_deg(log_q[0])
        q = gyromod.slerp_log(log_t, log_q, t + model.delta)
        heading = np.array([model.heading_deg(qq) for qq in q])
        sec = (((heading - h0) % 360) // (360 / SECTORS)).astype(int)
        for r in res.tracks.rows:
            ident = res.identity(int(r[0]), int(r[1]))
            if ident is not None:
                cov.sectors[sec[int(r[1])]].add(ident.index)
        out["heading_deg"] = round(float((heading[-1] - h0) % 360), 1)
    out["sectors"] = [len(x) for x in cov.sectors]
    out["covered"] = int(sum(1 for x in cov.sectors if len(x) >= MIN_SECTOR_LEDS))
    return out
