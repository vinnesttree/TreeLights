"""Synthetic phone recordings of the video-mode sequence (for tests).

Frames come at a jittery ~30 fps (variable frame rate) with some dropped
frames; the recording starts before the sequence, as a person would start it.
"""

from __future__ import annotations

from fractions import Fraction

import av
import numpy as np

from ..capture.video import Timeline
from .scene import Scene, View


def write_video(path: str, scene: Scene, view: View, tl: Timeline, lead_s: float = 2.0, tail_s: float = 1.0,
                fps: float = 30.0, drop_prob: float = 0.01, jitter_ms: float = 4.0, latency_ms: float = 40.0,
                seed: int = 0, codec: str = "libx264") -> int:
    rng = np.random.default_rng(seed)
    total = lead_s + tl.duration + tail_s
    with av.open(path, "w") as out:
        st = out.add_stream(codec, rate=int(fps))
        st.width, st.height = scene.width, scene.height
        st.pix_fmt = "yuv420p"
        st.time_base = Fraction(1, 90000)
        # One thread: deterministic output (see sim/walksim.py).
        st.options = {"crf": "18", "preset": "veryfast", "threads": "1"} if codec == "libx264" else {}
        t = 0.0
        last_pts = -1
        frames = 0
        while t < total:
            t += 1.0 / fps + rng.normal(0, jitter_ms / 1000)
            if rng.random() < drop_prob:
                continue
            seq_t = t - lead_s - latency_ms / 1000
            leds = tl.frame_at(seq_t)
            img = scene.render(view, leds)  # BGR
            fr = av.VideoFrame.from_ndarray(img[..., ::-1].copy(), format="rgb24")
            pts = int(round(t * 90000))
            if pts <= last_pts:
                continue
            fr.pts, fr.time_base = pts, st.time_base
            last_pts = pts
            for pkt in st.encode(fr):
                out.mux(pkt)
            frames += 1
        for pkt in st.encode():
            out.mux(pkt)
    return frames
