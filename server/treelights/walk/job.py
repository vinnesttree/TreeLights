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


def process_walk(scan_root: str, vid: str, tree_height_cm: float | None, prev_map: dict | None, wled: dict | None,
                 f0_prior: float | None = None, video_path: str | None = None) -> dict:
    scan = Scan(Path(scan_root))
    d = scan.root / "walk" / vid
    view = scan.view(vid)
    n = int(scan.meta["led_count"])
    log_t, log_q = load_motion(d)
    live = frames_from_images(_live_images(d))
    video = None
    notes = []
    if video_path and Path(video_path).exists() and Path(video_path).stat().st_size > 0:
        try:
            video = frames_from_images(_video_images(Path(video_path)))
        except Exception as e:  # noqa: BLE001
            notes.append(f"The recording couldn't be decoded ({e}); using the live frames only.")
    elif not video_path:
        notes.append("No recording was uploaded; using the live frames only.")
    wr = analyze(n, live if live[0] else None, video if video and video[0] else None, log_t, log_q, view.get("rec_start"))
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
    m = run_solve(str(scan.root), tree_height_cm, prev_map, wled, f0_prior)
    return m
