"""Recorded-video mode (architecture §7.7).

The server plays a fixed sequence on the LEDs while the phone records with its
own camera app (manual exposure/focus possible); the video is uploaded and
decoded here. Timing comes from the video itself (frame timestamps), so network
latency doesn't matter; the sync preamble and a flash + block number at the
start of every block of LEDs keep the decoding aligned through dropped frames
and variable frame rates.

Sequence (seconds):
  preamble  on .4 off .4 on .4 off .8 on .8 off .4   (all LEDs, sync level)
  dark 1.0                                          (dark reference)
  color check: constellation red .5, green .5, blue .5, dark .3
  per block of 50 LEDs (shuffled order, rotating colors):
     constellation .5, dark .4, block number 7 bits x .25 (all LEDs dim = 1), dark .25,
     LEDs x .25 each
  end: on .2 off .2 x 3
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..detection import constellation as cons
from ..detection.detector import build_reference, color_class, detect_single

PREAMBLE = [(1, 0.4), (0, 0.4), (1, 0.4), (0, 0.8), (1, 0.8), (0, 0.4)]
BLOCK = 50
BITS = 7
T_LED = 0.25
T_BIT = 0.25
STEP_COLORS = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], float)


@dataclass
class Event:
    start: float
    dur: float
    kind: str  # sync_on sync_off dark color const bit led end_on end_off
    data: dict


@dataclass
class Timeline:
    n: int
    seed: int
    levels: list[int]  # LED level per commanded color (R, G, B)
    sync_level: int
    constellation: list[int]
    events: list[Event]
    order: list[int]

    @property
    def duration(self) -> float:
        e = self.events[-1]
        return e.start + e.dur

    def to_json(self) -> dict:
        return {"n": self.n, "seed": self.seed, "levels": self.levels, "sync_level": self.sync_level}

    @staticmethod
    def build(n: int, seed: int, levels: list[int] | None = None, sync_level: int = 24) -> "Timeline":
        levels = [int(v) for v in (levels or [64, 48, 80])]
        rng = np.random.default_rng(seed)
        order = rng.permutation(n).tolist()
        constellation = np.unique(np.linspace(0, n - 1, min(40, n)).round().astype(int)).tolist()
        ev: list[Event] = []
        t = 0.0

        def add(dur: float, kind: str, **data) -> None:
            nonlocal t
            ev.append(Event(t, dur, kind, data))
            t += dur

        for on, d in PREAMBLE:
            add(d, "sync_on" if on else "sync_off")
        add(1.0, "dark", purpose="initial")
        for cls in range(3):
            add(0.5, "color", cls=cls)
        add(0.3, "dark", purpose="color")
        step = 0
        for b in range(0, n, BLOCK):
            blk = b // BLOCK
            add(0.5, "const", block=blk)
            add(0.4, "dark", purpose="block", block=blk)
            for k in range(BITS):
                add(T_BIT, "bit", block=blk, k=k, value=(blk >> k) & 1)
            add(0.25, "dark", purpose="gap", block=blk)
            for led in order[b : b + BLOCK]:
                add(T_LED, "led", block=blk, led=int(led), cls=step % 3, step=step)
                step += 1
        for _ in range(3):
            add(0.2, "end_on")
            add(0.2, "end_off")
        return Timeline(n, seed, levels, sync_level, constellation, ev, order)

    def frame_at(self, t: float) -> np.ndarray:
        """LED frame (n,3) uint8 the sequence shows at sequence time t."""
        f = np.zeros((self.n, 3), np.uint8)
        if t < 0 or t >= self.duration:
            return f
        lo, hi = 0, len(self.events) - 1
        while lo < hi:  # binary search for the event containing t
            mid = (lo + hi + 1) // 2
            if self.events[mid].start <= t:
                lo = mid
            else:
                hi = mid - 1
        e = self.events[lo]
        if e.kind in ("sync_on", "end_on"):
            f[:] = self.sync_level
        elif e.kind == "color":
            f[self.constellation] = (STEP_COLORS[e.data["cls"]] * self.levels[e.data["cls"]]).astype(np.uint8)
        elif e.kind == "const":
            f[self.constellation] = 40
        elif e.kind == "bit" and e.data["value"]:
            f[:] = max(6, self.sync_level // 2)
        elif e.kind == "led":
            f[e.data["led"]] = (STEP_COLORS[e.data["cls"]] * self.levels[e.data["cls"]]).astype(np.uint8)
        return f


def player(tl: Timeline, countdown_s: float = 10.0):
    """Output-thread renderer: a breathing 'ready' pulse during the countdown, then the sequence."""
    t0: list[float | None] = [None]

    def render(now: float) -> np.ndarray:
        if t0[0] is None:
            t0[0] = now
        t = now - t0[0] - countdown_s
        if t < 0:
            level = int(4 + 10 * (0.5 + 0.5 * np.sin(2 * np.pi * now)))
            return np.full((tl.n, 3), (level, level // 2, 0), np.uint8)
        return tl.frame_at(t)

    return render


# ---------------------------------------------------------------- decoding
def bright_tail(gray: np.ndarray) -> float:
    """Mean of the brightest 1% of pixels. LEDs are small, so the whole-frame mean
    barely moves when they switch; the bright tail does."""
    flat = gray.ravel()
    k = max(1, flat.size // 100)
    return float(np.partition(flat, flat.size - k)[-k:].mean())


def _small_gray(img: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    return cv2.resize(g, (max(1, g.shape[1] // 4), max(1, g.shape[0] // 4)), interpolation=cv2.INTER_AREA)


def _brightness_timeline(path: str) -> tuple[np.ndarray, np.ndarray, tuple[int, int]]:
    import av

    times, vals = [], []
    size = (0, 0)
    with av.open(path) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        for fr in c.decode(s):
            if fr.time is None:
                continue
            g = fr.to_ndarray(format="gray")
            size = (g.shape[1], g.shape[0])
            small = cv2.resize(g, (max(1, g.shape[1] // 4), max(1, g.shape[0] // 4)), interpolation=cv2.INTER_AREA)
            # Mean of the brightest 1% of pixels: LEDs are small, so the whole-frame
            # mean barely moves when they switch; the bright tail does.
            times.append(float(fr.time))
            vals.append(bright_tail(small))
    return np.array(times), np.array(vals), size


def _runs(times: np.ndarray, on: np.ndarray) -> list[tuple[int, float, float]]:
    out = []
    start = 0
    for k in range(1, len(on) + 1):
        if k == len(on) or on[k] != on[start]:
            t_end = times[k] if k < len(on) else times[-1]
            out.append((int(on[start]), float(times[start]), float(t_end - times[start])))
            start = k
    return out


def find_preamble(times: np.ndarray, vals: np.ndarray, tol: float = 0.15) -> float | None:
    """Video time where the sequence starts (first preamble flash)."""
    lo, hi = np.percentile(vals, 5), np.percentile(vals, 99.5)
    noise = 1.4826 * float(np.median(np.abs(np.diff(vals)))) + 1e-3
    if hi - lo < 6 * noise:
        return None
    on = vals > lo + 0.5 * (hi - lo)
    runs = _runs(times, on)
    for k in range(len(runs) - len(PREAMBLE) + 1):
        ok = True
        for j, (want_on, want_d) in enumerate(PREAMBLE[:-1]):  # the last off-run merges with the dark period
            r_on, _t, d = runs[k + j]
            if r_on != want_on or abs(d - want_d) > tol:
                ok = False
                break
        if ok:
            return runs[k][1]
    return None


@dataclass
class VideoResult:
    view: str
    segments: list[str]
    blocks_ok: int
    blocks_total: int
    detections: int
    failed_leds: list[int]
    warnings: list[str]


def decode_video(scan, view_id: str, video_path: str, tl: Timeline, keep_frames: bool = True) -> VideoResult:
    """Decode a recorded sequence into detections for `view_id` of `scan` (a capture.store.Scan)."""
    import av

    times, vals, (w, h) = _brightness_timeline(video_path)
    warnings: list[str] = []
    if len(times) < 10:
        raise ValueError("The video has too few frames.")
    t0 = find_preamble(times, vals)
    if t0 is None:
        raise ValueError("The sync flashes weren't found. Start recording before tapping Play, and keep the whole tree in the picture.")
    # Drift: the end marker's first flash (if recorded) fixes the time scale.
    scale = 1.0
    end_ev = next(e for e in tl.events if e.kind == "end_on")
    lo, hi = np.percentile(vals, 5), np.percentile(vals, 99.5)
    on = vals > lo + 0.5 * (hi - lo)
    exp_end = t0 + end_ev.start
    near = [r for r in _runs(times, on) if r[0] == 1 and abs(r[1] - exp_end) < 1.0]
    if near:
        scale = (near[0][1] - t0) / end_ev.start
    elif times[-1] < exp_end:
        warnings.append("The recording stopped before the end of the sequence; later LEDs are missing.")

    def vt(seq_t: float, offset: float = 0.0) -> float:
        return t0 + offset + scale * seq_t

    # Per-block offset from the constellation flash's rising edge.
    offsets: dict[int, float] = {}
    for e in (e for e in tl.events if e.kind == "const"):
        b = e.data["block"]
        exp = vt(e.start)
        win = (times > exp - 0.6) & (times < exp + 0.6)
        if win.sum() < 3:
            continue
        tw, vw = times[win], vals[win]
        base, peak = np.percentile(vw, 10), np.percentile(vw, 90)
        if peak - base < 0.2:
            continue
        rising = np.nonzero(vw > base + 0.5 * (peak - base))[0]
        if len(rising):
            offsets[b] = float(tw[rising[0]] - exp)

    # Frames to sample: (video time, purpose, event).
    wanted: list[tuple[float, str, Event]] = []
    for e in tl.events:
        b = e.data.get("block")
        off = offsets.get(b, 0.0) if b is not None else 0.0
        mid = vt(e.start + e.dur / 2, off)
        if e.kind == "dark" and e.data.get("purpose") in ("initial", "block"):
            for frac in (0.35, 0.5, 0.65):
                wanted.append((vt(e.start + e.dur * frac, off), "dark", e))
        elif e.kind in ("color", "const", "led"):
            wanted.append((mid, e.kind, e))
        elif e.kind == "bit":
            wanted.append((mid, "bit", e))
    wanted.sort(key=lambda x: x[0])

    # Second pass: stream the video and process each wanted frame as it is decoded
    # (only one full-resolution frame is held in memory).
    idx_for = [int(np.argmin(np.abs(times - t))) for t, _p, _e in wanted]
    jobs = sorted(zip(idx_for, range(len(wanted))))
    st = _DecodeState(scan, view_id, tl, w, h, keep_frames)
    ptr = 0
    with av.open(video_path) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        k = -1
        for fr in c.decode(s):
            if fr.time is None:
                continue
            k += 1
            if ptr < len(jobs) and jobs[ptr][0] == k:
                img = fr.to_ndarray(format="rgb24")
                while ptr < len(jobs) and jobs[ptr][0] == k:
                    t, purpose, e = wanted[jobs[ptr][1]]
                    st.handle(img if abs(times[k] - t) <= 0.1 else None, purpose, e)
                    ptr += 1
            if ptr >= len(jobs):
                break
    while ptr < len(jobs):  # the recording ended early
        t, purpose, e = wanted[jobs[ptr][1]]
        st.handle(None, purpose, e)
        ptr += 1
    warnings += st.warnings
    n_blocks = (tl.n + BLOCK - 1) // BLOCK
    return VideoResult(view_id, st.segs, st.blocks_ok, n_blocks, st.detections, sorted(set(st.failed)), warnings)


class _DecodeState:
    """Per-video decoding state, fed frames in sequence order."""

    def __init__(self, scan, view_id: str, tl: Timeline, w: int, h: int, keep_frames: bool):
        self.scan, self.view_id, self.tl, self.keep_frames = scan, view_id, tl, keep_frames
        self.segs: list[str] = []
        self.cur_seg: str | None = None
        self.ref = None
        self.ref_pts = None
        self.basis = STEP_COLORS.copy()
        self.color_verify = False
        self.blocks_ok = 0
        self.detections = 0
        self.failed: list[int] = []
        self.darks: list[np.ndarray] = []
        self.color_frames: dict[int, np.ndarray] = {}
        self.bits: dict[int, dict[int, float]] = {}
        self.block_ok: dict[int, bool] = {}
        self.recent: list = []
        self.dark_level: float | None = None
        self.warnings: list[str] = []
        self.seg_meta = {"width": w, "height": h, "device": "video", "locks": None, "up_cam": None, "source": "video"}

    def new_segment(self, reason: str) -> str:
        sid = self.scan.add_segment(self.view_id, self.seg_meta)
        self.segs.append(sid)
        self.scan.events.append({"type": "video_segment", "segment": sid, "reason": reason})
        return sid

    def handle(self, img: np.ndarray | None, purpose: str, e: Event) -> None:
        if img is None:
            if e.kind == "led":
                self.failed.append(e.data["led"])
            return
        if purpose == "dark":
            self.darks.append(img)
            if len(self.darks) == 3:
                self.ref = build_reference(self.darks)
                self.dark_level = float(np.mean([bright_tail(_small_gray(d)) for d in self.darks]))
                self.darks = []
        elif purpose == "color" and self.ref is not None:
            self.color_frames[e.data["cls"]] = img
        elif purpose == "const" and self.ref is not None:
            pts = cons.constellation_points(img, self.ref)
            if self.ref_pts is None:
                self.ref_pts = pts
                self.cur_seg = self.new_segment("start")
                if len(self.color_frames) == 3:
                    self.basis, self.color_verify = _color_basis(self.color_frames, self.ref, pts)
                self.color_frames = {}
            elif cons.compare(self.ref_pts, pts).moved:
                self.ref_pts = pts
                self.cur_seg = self.new_segment("moved")
                self.warnings.append(f"Movement detected at block {e.data['block']}; started a new segment.")
        elif purpose == "bit":
            self.bits.setdefault(e.data["block"], {})[e.data["k"]] = bright_tail(_small_gray(img))
        elif purpose == "led":
            self._led(img, e)

    def _block_aligned(self, b: int) -> bool:
        if b not in self.block_ok:
            vals_b = self.bits.get(b, {})
            ok = False
            if len(vals_b) == BITS and self.dark_level is not None:
                v = np.array([vals_b[i] for i in range(BITS)])
                # A lit bit shows every LED; compare with this block's dark level.
                if v.max() - self.dark_level > 3.0:
                    thr = self.dark_level + 0.5 * (v.max() - self.dark_level)
                    ok = sum(int(v[i] > thr) << i for i in range(BITS)) == b
                else:
                    ok = b == 0  # no bit lit: only block 0 (all zeros) is valid
            self.block_ok[b] = ok
            if ok:
                self.blocks_ok += 1
            else:
                self.warnings.append(f"Block {b} couldn't be aligned; its LEDs are listed for a re-scan.")
        return self.block_ok[b]

    def _led(self, img: np.ndarray, e: Event) -> None:
        led, cls = e.data["led"], e.data["cls"]
        if not self._block_aligned(e.data["block"]) or self.ref is None or self.cur_seg is None:
            self.failed.append(led)
            return
        det = detect_single(img, self.ref, self.basis[cls] if self.color_verify else None)
        if not det.valid:
            self.failed.append(led)
            return
        cands = [c.to_dict() for c in det.candidates]
        if cands and self.color_verify:
            c_cls, margin = color_class(np.array(cands[0]["color"]), self.basis)
            if c_cls != cls or margin < 0.05:
                self.failed.append(led)
                return
        if cands and any(p is not None and np.hypot(cands[0]["x"] - p[0], cands[0]["y"] - p[1]) < 3.0 for p in self.recent):
            self.failed.append(led)
            return
        self.recent = (self.recent + [(cands[0]["x"], cands[0]["y"]) if cands else None])[-2:]
        step = 1_000_000 + e.data["step"]
        if self.keep_frames and cands:
            ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                self.scan.write_frame(self.cur_seg, step, "video", buf.tobytes())
        self.scan.record({"segment": self.cur_seg, "view": self.view_id, "led": led, "kind": "seq", "step": step,
                          "candidates": cands, "source": "video"})
        self.detections += 1


def _color_basis(color_frames: dict[int, np.ndarray], ref, pts: np.ndarray) -> tuple[np.ndarray, bool]:
    diffs = {c: f.astype(np.float32) - ref.image for c, f in color_frames.items()}
    vecs: dict[int, list[np.ndarray]] = {0: [], 1: [], 2: []}
    for x, y in pts:
        xi, yi = int(round(x)), int(round(y))
        win = {c: d[max(0, yi - 2) : yi + 3, max(0, xi - 2) : xi + 3].reshape(-1, 3).mean(axis=0) for c, d in diffs.items()}
        if all(np.linalg.norm(v) > 10 for v in win.values()):
            for c, v in win.items():
                vecs[c].append(v / np.linalg.norm(v))
    if min(len(v) for v in vecs.values()) < 3:
        return STEP_COLORS.copy(), False
    basis = np.array([np.median(vecs[c], axis=0) for c in range(3)])
    correct = [color_class(v, basis)[0] == c for c in range(3) for v in vecs[c]]
    return basis, float(np.mean(correct)) >= 0.9


def save_timeline_meta(path: Path, tl: Timeline) -> None:
    path.write_text(json.dumps(tl.to_json()))


def decode_job(scan_root: str, view_id: str, video_path: str, tl_meta: dict) -> dict:
    """Worker-process entry point."""
    from .store import Scan

    scan = Scan(Path(scan_root))
    tl = Timeline.build(tl_meta["n"], tl_meta["seed"], tl_meta.get("levels"), tl_meta.get("sync_level", 24))
    try:
        res = decode_video(scan, view_id, video_path, tl)
    except ValueError as e:
        scan.set_view(view_id, status="failed", error=str(e))
        raise
    scan.set_view(view_id, status="done", source="video", failed_leds=res.failed_leds)
    scan.events.append({"type": "view_done", "view": view_id, "source": "video"})
    return {"view": res.view, "segments": res.segments, "blocks_ok": res.blocks_ok, "blocks_total": res.blocks_total,
            "detections": res.detections, "failed_leds": res.failed_leds, "warnings": res.warnings}
