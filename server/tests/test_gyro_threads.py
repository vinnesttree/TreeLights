"""Image rotations for the gyro calibration run on threads: the same result as in order."""

import numpy as np

from treelights.walk import gyro


def _pairs(n_pairs: int = 12, n_pts: int = 60, seed: int = 0):
    rng = np.random.default_rng(seed)
    f, cx, cy = 700.0, 479.5, 269.5
    out = []
    for k in range(n_pairs):
        X = rng.normal(size=(n_pts, 3)) * [0.5, 0.8, 0.4] + [0, 0, 3.0]
        a = rng.normal(scale=0.15, size=3)
        th = np.linalg.norm(a)
        K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]]) / th
        R = np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K
        Xb = X @ R.T + [0.3, 0.0, 0.05]
        p = X[:, :2] / X[:, 2:] * f + [cx, cy] + rng.normal(scale=0.3, size=(n_pts, 2))
        q = Xb[:, :2] / Xb[:, 2:] * f + [cx, cy] + rng.normal(scale=0.3, size=(n_pts, 2))
        out.append((2 * k, 2 * k + 1, p, q))
    return out, f, cx, cy


def test_threaded_image_rotations_match_sequential(monkeypatch):
    pairs, f, cx, cy = _pairs()
    monkeypatch.setattr(gyro, "_THREADS", 1)
    seq = gyro._image_rotations(pairs, f, cx, cy)
    monkeypatch.setattr(gyro, "_THREADS", 4)
    par = gyro._image_rotations(pairs, f, cx, cy)
    assert sum(r is not None for r in seq) >= len(pairs) // 2
    for a, b in zip(seq, par):
        assert (a is None) == (b is None)
        if a is not None:
            assert np.array_equal(a, b)
