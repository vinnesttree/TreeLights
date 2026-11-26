"""Blob detection on threads gives the same frames, in the same order, as one at a time."""

import numpy as np

from treelights.walk.process import frames_from_images


def _images(n: int = 30, seed: int = 0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:240, 0:320]
    for k in range(n):
        img = rng.normal(20, 2, size=(240, 320, 3))
        for x, y in rng.uniform([10, 10], [310, 230], size=(40, 2)):
            img[..., 1] += 150 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / 3.0)
        yield 0.1 * k, np.clip(img, 0, 255).astype(np.uint8)


def test_threaded_blob_detection_matches_sequential():
    seq, w, h = frames_from_images(_images(), threads=1)
    par, w2, h2 = frames_from_images(_images(), threads=4)
    assert (w, h) == (w2, h2) == (320, 240)
    assert len(seq) == len(par) == 30
    assert all(len(f.blobs) > 20 for f in seq)
    for a, b in zip(seq, par):
        assert a.t == b.t
        assert np.array_equal(a.blobs, b.blobs)
