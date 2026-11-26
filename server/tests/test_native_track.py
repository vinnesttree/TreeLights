"""The Rust tracker (server/native/src/track.rs) gives the same tracks as the Python one."""

import numpy as np
import pytest

from treelights import native
from treelights.sim.scene import Faults
from treelights.sim.tree import make_tree
from treelights.sim.walksim import WalkConfig, jpeg_rgb, simulate
from treelights.walk import tracking
from treelights.walk.process import frames_from_images
from treelights.walk.tracking import Frame

pytestmark = pytest.mark.skipif(not native.available("track"), reason="native module not built")


def _both(frames, K=None, use_gyro=False, monkeypatch=None):
    lib = native.lib
    monkeypatch.setattr(native, "lib", None)
    py = tracking.track(frames, K, use_gyro=use_gyro)
    monkeypatch.setattr(native, "lib", lib)
    seen = []
    rs = tracking.track(frames, K, use_gyro=use_gyro, progress=seen.append)
    assert seen and seen[0] == 0.0 and all(0 <= s < 1 for s in seen)
    return py, rs


def _assert_same(py, rs):
    assert rs.n_tracks == py.n_tracks
    assert rs.rows.shape == py.rows.shape
    np.testing.assert_array_equal(rs.rows, py.rows)  # same detections, same ids, same order
    np.testing.assert_allclose(rs.shifts, py.shifts, rtol=0, atol=1e-9)


def _blobs(xy, rng):
    b = np.zeros((len(xy), 6), np.float32)
    b[:, 0:2] = xy
    b[:, 2] = rng.uniform(100, 900, len(xy))
    b[:, 3] = 100
    b[:, 4] = rng.uniform(0.3, 1.0, len(xy))
    b[:, 5] = rng.random(len(xy)) < 0.1
    return b


def test_native_track_matches_python_on_a_noisy_constellation(monkeypatch):
    """Panning, blinking, dropped LEDs and clutter: coasting, losing and re-acquiring tracks."""
    rng = np.random.default_rng(11)
    base = rng.uniform([50, 50], [910, 490], (120, 2))
    frames = []
    for i in range(240):
        t = i / 30 + rng.normal(0, 0.002)
        shift = np.array([40 * t + 4 * np.sin(5 * t), 8 * np.sin(1.5 * t)])
        xy = base + shift + rng.normal(0, 0.4, base.shape)
        show = rng.random(len(base)) > (0.9 if i % 15 in (7, 8) else 0.1)
        clutter = rng.uniform([0, 0], [960, 540], (rng.integers(0, 30), 2))
        pts = np.vstack([xy[show], clutter])
        frames.append(Frame(t, _blobs(pts, rng) if len(pts) else np.zeros((0, 6), np.float32)))
    frames.append(Frame(frames[-1].t + 0.05, np.zeros((0, 6), np.float32)))  # an empty frame
    _assert_same(*_both(frames, monkeypatch=monkeypatch))


def test_native_track_matches_python_with_gyro(monkeypatch):
    rng = np.random.default_rng(6)
    f = 750.0
    K = np.array([[f, 0, 479.5], [0, f, 269.5], [0, 0, 1.0]])
    P = np.c_[rng.uniform(-60, 60, (80, 2)), rng.uniform(250, 350, 80)]
    frames = []
    for i in range(120):
        a = np.radians(40 * i / 30)
        R = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
        X = P @ R.T + [0.02 * i, 0, 0]  # rotation plus a little translation the gyro doesn't see
        uv = X[:, :2] / X[:, 2:3] * f + K[:2, 2] + rng.normal(0, 0.3, (len(P), 2))
        ok = (X[:, 2] > 0) & (uv[:, 0] > 0) & (uv[:, 0] < 960) & (uv[:, 1] > 0) & (uv[:, 1] < 540)
        ok &= rng.random(len(P)) > 0.05
        frames.append(Frame(i / 30, _blobs(uv[ok], rng), R=None if i == 50 else R))  # one frame without the gyro
    _assert_same(*_both(frames, K, use_gyro=True, monkeypatch=monkeypatch))


def test_native_track_matches_python_on_a_simulated_walk(monkeypatch, tmp_path):
    tree = make_tree(n=60, seed=4, height=110, base_radius=40, bottom=15)
    cap = simulate(tree, WalkConfig(duration=12.0, seed=4, led_stale_prob=0.03),
                   Faults(foliage=1.0, glint_prob=0.15, noise_sigma=1.5), None)
    frames, _w, _h = frames_from_images((t, jpeg_rgb(j)) for t, j, _q in cap.live)
    frames.sort(key=lambda fr: fr.t)
    _assert_same(*_both(frames, monkeypatch=monkeypatch))


def test_local_field_still_uses_python(monkeypatch):
    calls = []
    monkeypatch.setattr(tracking, "_track_native", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(tracking, "LOCAL_FIELD", True)
    tracking.track([Frame(0.0, np.zeros((0, 6)))])
    assert not calls
