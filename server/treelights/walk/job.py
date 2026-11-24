"""Processing a stored walk-around view (run in a worker process).

Inputs, under scans/<scan>/walk/<view>/:
  live.jsonl + live/*.jpg   frames streamed during the walk (phone time, quaternion)
  motion.jsonl              orientation samples (phone time, quaternion)
  ../../videos/<view>.upload  the full-rate recording, if it was uploaded

Both sources are decoded (walk/process.py); keyframes from both become the
view's segments, written to walk/<view>/observations.json so that later solves
of the scan (e.g. after the user edits overrides) include them without decoding
again.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .. import storage
from ..progress import Progress
from ..capture.store import Scan
from ..reconstruction.job import run_solve
from .process import analyze, frames_from_images

MAX_VIDEO_WIDTH = 1280  # larger videos are downscaled for blob detection


def load_motion(d: Path) -> tuple[np.ndarray | None, np.ndarray | None]:
    t, q = [], []
    for rec in storage.iter_jsonl(d / "motion.jsonl"):
        if t and rec["t"] <= t[-1]:
            continue
        t.append(float(rec["t"]))
        q.append(rec["q"])
    if len(t) < 10:
        return None, None
    return np.array(t), np.array(q, float)


def _live_images(d: Path):
    import cv2

    for rec in storage.iter_jsonl(d / "live.jsonl"):
        img = cv2.imread(str(d / "live" / rec["file"]), cv2.IMREAD_COLOR)
        if img is not None:
            yield float(rec["t"]), img[..., ::-1]


def _video_images(path: Path):
    import av
    import cv2

    with av.open(str(path)) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        for fr in c.decode(s):
            if fr.time is None:
                continue
            img = fr.to_ndarray(format="rgb24")
            h, w = img.shape[:2]
            if w > MAX_VIDEO_WIDTH:
                img = cv2.resize(img, (MAX_VIDEO_WIDTH, round(h * MAX_VIDEO_WIDTH / w)), interpolation=cv2.INTER_AREA)
            yield float(fr.time), img


def _video_frame_count(path: Path) -> int:
    """About how many frames the recording has (for progress; 0 if unknown)."""
    import av

    try:
        with av.open(str(path)) as c:
            s = c.streams.video[0]
            if s.frames:
                return int(s.frames)
            dur = float(s.duration * s.time_base) if s.duration else (c.duration or 0) / 1e6
            return int(dur * float(s.average_rate or 30))
    except Exception:  # noqa: BLE001
        return 0


def _starts_when_used(make):
    """A reporter whose step begins (is shown) only when it first reports."""
    r = []

    def report(frac: float = 0.0, detail: str = "") -> None:
        if not r:
            r.append(make())
        r[0](frac, detail)

    return report


def process_walk(scan_root: str, vid: str, tree_height_cm: float | None, prev_map: dict | None, wled: dict | None,
                 f0_prior: float | None = None, video_path: str | None = None, progress_path: str | None = None) -> dict:
    scan = Scan(Path(scan_root))
    d = scan.root / "walk" / vid
    view = scan.view(vid)
    n = int(scan.meta["led_count"])
    log_t, log_q = load_motion(d)
    # Progress: five steps, weighted by frames (decoding a frame takes ~3x finding its LEDs;
    # timings of a real 600-LED walk), with the solve a tenth of the whole.
    has_video = bool(video_path and Path(video_path).exists() and Path(video_path).stat().st_size > 0)
    n_live = sum(1 for _ in storage.iter_jsonl(d / "live.jsonl")) if (d / "live.jsonl").exists() else 0
    n_video = _video_frame_count(Path(video_path)) if has_video else 0
    w = [n_live, n_video, 3 * n_live, 3 * n_video]
    w.append(0.1 * max(sum(w), 1) / 0.9)
    edges = np.concatenate([[0.0], np.cumsum(w) / max(sum(w), 1e-9)])
    steps = ["Finding the LEDs in the live frames", "Finding the LEDs in the recording", "Decoding the live frames",
             "Decoding the recording", "Building the 3D map"]
    prog = Progress(progress_path)

    def part(k: int):
        return prog.part(float(edges[k]), float(edges[k + 1]), f"Step {k + 1} of 5: {steps[k]}")

    live = frames_from_images(_live_images(d), part(0), n_live)
    video = None
    notes = []
    if has_video:
        try:
            video = frames_from_images(_video_images(Path(video_path)), part(1), n_video)
        except Exception as e:  # noqa: BLE001
            notes.append(f"The recording couldn't be decoded ({e}); using the live frames only.")
    elif not video_path:
        notes.append("No recording was uploaded; using the live frames only.")
    wr = analyze(n, live if live[0] else None, video if video and video[0] else None, log_t, log_q, view.get("rec_start"),
                 report_live=_starts_when_used(lambda: part(2)), report_video=_starts_when_used(lambda: part(3)))
    wr.notes = notes + wr.notes
    # Segment ids unique within the scan: <view><keyframe>.
    obs = {f"{vid}{k}": v for k, v in wr.obs.items()}
    segs = {f"{vid}{k}": dict(v, view=vid) for k, v in wr.segments.items()}
    info = {k: v for k, v in wr.info.items() if k != "window_s"}
    storage.atomic_write_json(d / "observations.json", {
        "obs": {s: {str(led): c for led, c in o.items()} for s, o in obs.items()},
        "segments": segs, "info": info, "notes": wr.notes,
        "focal_prior": wr.info.get("focal_prior"),
    })
    scan.set_view(vid, status="processed", walk=info, segments=sorted(segs))
    if not obs:
        raise RuntimeError("No LEDs were identified in the walk: " + " ".join(wr.notes or ["is the pattern visible?"]))
    m = run_solve(str(scan.root), tree_height_cm, prev_map, wled, f0_prior, report=part(4))
    return m
