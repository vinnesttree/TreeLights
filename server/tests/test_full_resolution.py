"""The full-resolution walk option: off by default; a walk recorded with it keeps its
recording's size for finding LEDs, others are downscaled to 1280 px wide as before."""

import av
import numpy as np

from treelights.config import Settings
from treelights.walk import job


def _video(path, w: int, h: int, n: int = 6) -> None:
    with av.open(str(path), "w") as c:
        s = c.add_stream("libx264", rate=30)
        s.width, s.height, s.pix_fmt = w, h, "yuv420p"
        for k in range(n):
            img = np.zeros((h, w, 3), np.uint8)
            img[h // 2, (k * 37) % w] = 255
            for p in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                c.mux(p)
        for p in s.encode():
            c.mux(p)


def test_off_by_default():
    assert Settings().capture.walk_full_resolution is False


def test_max_width_follows_the_walk():
    assert job.video_max_width({}) == job.MAX_VIDEO_WIDTH == 1280
    assert job.video_max_width({"camera": {"width": 1280, "height": 720}}) == 1280
    assert job.video_max_width({"camera": {"full_resolution": False}}) == 1280
    assert job.video_max_width({"camera": {"width": 1920, "height": 1080, "full_resolution": True}}) is None


def test_recording_kept_at_full_size_only_when_asked(tmp_path):
    p = tmp_path / "walk.mp4"
    _video(p, 1920, 1080)
    small = [img.shape for _t, img in job._video_images(p)]
    full = [img.shape for _t, img in job._video_images(p, None)]
    assert small and all(s == (720, 1280, 3) for s in small)
    assert full and all(s == (1080, 1920, 3) for s in full)
