"""The Rust hypotheses and point refinement (native/src/hyp.rs) against the Python ones."""

import numpy as np
import pytest

from treelights import native
from treelights.reconstruction import fill as F
from treelights.reconstruction import hypotheses as H
from treelights.reconstruction.camera import Intrinsics, look_at, project
from treelights.reconstruction.chain import Bounds
from treelights.reconstruction.sfm import Cameras

pytestmark = pytest.mark.skipif(not native.available("build_hypotheses"), reason="native module not installed")


def scene(seed=0, n=60, n_cams=8, k1=0.05, k2=-0.01):
    rng = np.random.default_rng(seed)
    P = np.c_[rng.uniform(-30, 30, n), rng.uniform(0, 150, n), rng.uniform(-30, 30, n)]
    cams = Cameras(1.0, k1, k2)
    for c in range(n_cams):
        a = 2 * np.pi * c / n_cams
        eye = np.array([200 * np.cos(a), 75 + 20 * np.sin(3 * a), 200 * np.sin(a)])
        R, t = look_at(eye, np.array([0.0, 75.0, 0.0]))
        s = f"s{c}"
        cams.poses[s] = (R, t)
        cams.sizes[s] = (1280, 720)
        cams.fdev[s] = 0.01 * (c - 3)
    obs = {}
    for s in cams.poses:
        R, t = cams.poses[s]
        uv, z = project(P, R, t, cams.intrinsics(s))
        o = {}
        for i in range(n):
            if z[i] <= 0 or rng.random() < 0.2:
                continue
            cands = [{"x": float(uv[i, 0] + rng.normal(0, 0.4)), "y": float(uv[i, 1] + rng.normal(0, 0.4)),
                      "score": float(rng.uniform(0.3, 1)), "sigma_px": float(rng.choice([1.0, 2.5])),
                      "kind": "diffuse" if rng.random() < 0.15 else "point"}]
            if rng.random() < 0.3:  # a reflection elsewhere in the picture
                cands.append({"x": float(rng.uniform(0, 1280)), "y": float(rng.uniform(0, 720)),
                              "score": float(rng.uniform(0.2, 1))})
            if rng.random() < 0.1:  # equal scores: order must follow the stable sort
                cands.append(dict(cands[0], x=cands[0]["x"] + 6.0))
            o[i] = cands
        obs[s] = o
    return P, cams, obs


def run(fn, use_native, *a, **k):
    lib = native.lib
    native.lib = lib if use_native else None
    try:
        return fn(*a, **k)
    finally:
        native.lib = lib


@pytest.mark.parametrize("seed,k1,k2", [(0, 0.05, -0.01), (1, 0.0, 0.0), (2, -0.2, 0.08)])
def test_build_hypotheses_matches_python(seed, k1, k2):
    _P, cams, obs = scene(seed, k1=k1, k2=k2)
    obs["s0"][999] = [{"x": 1.0, "y": 2.0}]  # an LED past n_leds is ignored by both
    a = run(H.build_hypotheses, False, obs, cams, 60)
    b = run(H.build_hypotheses, True, obs, cams, 60)
    assert set(a) == set(b) and len(a) > 50
    for led in a:
        assert len(a[led]) == len(b[led])
        for x, y in zip(a[led], b[led]):
            assert x.segs == y.segs
            assert all(np.array_equal(u, v) for u, v in zip(x.uv, y.uv))
            # MINPACK stops a little early where the depth is poorly constrained (two
            # near-parallel rays); the native solve goes on to a slightly lower cost.
            assert np.abs(y.X - x.X).max() < 1e-6 + 1e-5 * x.sigma
            assert y.sigma == pytest.approx(x.sigma, rel=1e-4, abs=1e-6)
            for f in ("err_px", "nerr", "angle_deg", "score", "diffuse"):
                assert getattr(y, f) == pytest.approx(getattr(x, f), abs=1e-5), f
    # Deterministic (LEDs run in parallel).
    c = run(H.build_hypotheses, True, obs, cams, 60)
    assert all(np.array_equal(x.X, y.X) for led in b for x, y in zip(b[led], c[led]))


def test_refine_points_matches_python():
    P, cams, obs = scene(3, k1=0.03, k2=0.0)
    views = {s: F.View(s, R, t, cams.intrinsics(s), 1280, 720) for s, (R, t) in cams.poses.items()}
    rng = np.random.default_rng(5)
    leds = [i for i in range(60) if i not in (20, 21)]  # a gap: no link across it
    support = {i: [(s, np.array([o[i][0]["x"], o[i][0]["y"]])) for s, o in obs.items() if i in o] for i in leds}
    pos = {i: P[i] + rng.normal(0, 0.5, 3) for i in leds}
    d = np.linalg.norm(np.diff(P, axis=0), axis=1)
    bound = np.where(np.arange(59) % 13 == 5, np.inf, 0.8 * d)  # most links too long for their bound: hinge active
    bounds = Bounds(base=float(np.median(d)), base_raw=float(np.median(d)), bound=bound, kind=["normal"] * 59,
                    trusted_links=50)
    sigma = {i: 0.05 for i in leds}
    a = run(F.refine_points, False, pos, support, views, bounds, sigma)
    b = run(F.refine_points, True, pos, support, views, bounds, sigma)
    assert set(a) == set(b) == set(leds)
    moved = max(np.linalg.norm(a[i] - pos[i]) for i in leds)
    diff = max(np.linalg.norm(a[i] - b[i]) for i in leds)
    assert moved > 0.1
    assert diff < 1e-3 * moved, (diff, moved)


def test_refine_points_empty_and_unlinked():
    _P, cams, _obs = scene(4)
    views = {s: F.View(s, R, t, cams.intrinsics(s), 1280, 720) for s, (R, t) in cams.poses.items()}
    bounds = Bounds(1.0, 1.0, np.full(10, np.inf), ["break_data"] * 10, 0)
    assert run(F.refine_points, True, {}, {}, views, bounds, {}) == {}
    R, t = cams.poses["s0"]
    X = np.array([1.0, 70.0, -2.0])
    uv0 = project(X[None], R, t, cams.intrinsics("s0"))[0][0]
    R1, t1 = cams.poses["s1"]
    uv1 = project(X[None], R1, t1, cams.intrinsics("s1"))[0][0]
    out = run(F.refine_points, True, {3: X + 0.3}, {3: [("s0", uv0), ("s1", uv1)]}, views, bounds, {})
    np.testing.assert_allclose(out[3], X, atol=1e-6)


def test_intrinsics_undistortion_matches_opencv():
    # The Rust ray uses its own copy of cv2.undistortPoints; a 1-LED, 2-camera case where
    # the hypothesis depends on it directly.
    cams = Cameras(1.0, 0.25, -0.1)
    for s, eye in (("a", [-60.0, 50.0, -200.0]), ("b", [80.0, 40.0, -190.0])):
        cams.poses[s] = look_at(np.array(eye), np.array([0.0, 50.0, 0.0]))
        cams.sizes[s] = (1280, 720)
    X = np.array([25.0, 90.0, 10.0])  # off-centre: distortion matters
    obs = {s: {0: [dict(zip("xy", project(X[None], *cams.poses[s], cams.intrinsics(s))[0][0].tolist()))]}
           for s in cams.poses}
    a = run(H.build_hypotheses, False, obs, cams, 1)[0][0]
    b = run(H.build_hypotheses, True, obs, cams, 1)[0][0]
    np.testing.assert_allclose(b.X, a.X, rtol=1e-9, atol=1e-9)
    np.testing.assert_allclose(b.X, X, atol=1e-6)
    assert isinstance(cams.intrinsics("a"), Intrinsics)
