"""Processing a walk-around capture: live frames and/or the recorded video -> map."""

from __future__ import annotations

import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np

from ..progress import Report
from ..reconstruction.solve import SolveInput, solve
from .blobs import detect_blobs
from .pipeline import Source, SourceResult, build_observations, focal_prior, process_source, video_phone_times
from .tracking import Frame


@dataclass
class WalkResult:
    results: list[SourceResult]
    obs: dict
    segments: dict
    info: dict
    notes: list[str] = field(default_factory=list)


BLOB_THREADS = min(4, os.cpu_count() or 1)


def frames_from_images(items: Iterable[tuple[float, np.ndarray]], report: Report | None = None,
                       total: int | None = None, threads: int | None = None) -> tuple[list[Frame], int, int]:
    """Blobs of every image, in order. Frames are found on threads while the next images
    are read (OpenCV and most of numpy release the GIL); each frame's blobs depend only on
    its own image, so the result is the same as one at a time. At most two frames per
    thread are held at once."""
    n_threads = BLOB_THREADS if threads is None else threads
    frames, w, h = [], 0, 0
    if n_threads < 2:
        for k, (t, img) in enumerate(items):
            if report is not None and total and k % 25 == 0:
                report(k / total, f"{k} of {total} frames")
            h, w = img.shape[:2]
            frames.append(Frame(float(t), detect_blobs(img)))
        return frames, w, h
    pending: deque = deque()
    with ThreadPoolExecutor(n_threads) as ex:
        for k, (t, img) in enumerate(items):
            if report is not None and total and k % 25 == 0:
                report(k / total, f"{k} of {total} frames")
            h, w = img.shape[:2]
            pending.append((float(t), ex.submit(detect_blobs, img)))
            if len(pending) >= 2 * n_threads:
                tt, fut = pending.popleft()
                frames.append(Frame(tt, fut.result()))
        while pending:
            tt, fut = pending.popleft()
            frames.append(Frame(tt, fut.result()))
    return frames, w, h


def analyze(n: int, live: tuple[list[Frame], int, int] | None, video: tuple[list[Frame], int, int] | None,
            log_t: np.ndarray | None, log_q: np.ndarray | None, rec_start: float | None,
            progress: Callable[[str], None] = lambda m: None,
            report_live: Report | None = None, report_video: Report | None = None) -> WalkResult:
    results: list[SourceResult] = []
    notes: list[str] = []
    live_res = None
    if live and live[0]:
        frames, w, h = live
        frames.sort(key=lambda f: f.t)
        progress("Decoding the live frames...")
        live_res = process_source(Source("live", frames, w, h, np.array([f.t for f in frames])), n, log_t, log_q,
                                  report=report_live)
        results.append(live_res)
        notes += live_res.notes
        if live_res.log_shift and log_t is not None:
            log_t = log_t + live_res.log_shift  # the recording then reads the log lined up
    if video and video[0]:
        frames, w, h = video
        frames.sort(key=lambda f: f.t)
        progress("Decoding the video...")
        fn = lambda t, timing: video_phone_times(t, timing, live_res, rec_start)  # noqa: E731
        res = process_source(Source("video", frames, w, h), n, log_t, log_q, phone_time_fn=fn, report=report_video)
        results.append(res)
        notes += res.notes
        if live_res is None or not live_res.timing.ok:
            notes.append("Video frames were timed from the recording start only (no live frames to align with).")
    obs, segs, info = build_observations(results)
    info["focal_prior"] = focal_prior(results)
    info["decoded"] = {r.source.name: len(r.decoded_leds()) for r in results}
    info["timing_ok"] = {r.source.name: r.timing.ok for r in results}
    info["gyro_ok"] = {r.source.name: r.gyro is not None for r in results}
    return WalkResult(results, obs, segs, info, notes)


def solve_walk(wr: WalkResult, n: int, tree_height_cm: float | None, output_breaks: list[int], overrides: dict,
               prev_map: dict | None = None, scan_id: str = "", wled: dict | None = None,
               f0_prior: float | None = None) -> dict:
    m = solve(SolveInput(n, wr.obs, wr.segments, tree_height_cm, output_breaks, overrides, prev_map,
                         f0_prior or wr.info.get("focal_prior"), scan_id, wled))
    m["quality"]["walk"] = {k: v for k, v in wr.info.items() if k != "window_s"}
    m["quality"]["warnings"] = wr.notes + m["quality"]["warnings"]
    return m
