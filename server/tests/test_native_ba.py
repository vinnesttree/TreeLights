"""Native bundle adjustment (server/native/src/ba.rs) against the scipy version."""

import copy

import numpy as np
import pytest

from treelights import native
from treelights.reconstruction import sfm
from treelights.reconstruction.camera import Intrinsics, look_at, project, rodrigues, to_rvec

pytestmark = pytest.mark.skipif(not native.available("ba_minimize"), reason="native module not installed")


def scene(seed: int = 0, n_cams: int = 8, n_pts: int = 120, noise: float = 0.5, outliers: int = 10):
    """Cameras on a ring around a cone of points, observations with noise and outliers."""
    rng = np.random.default_rng(seed)
    h = rng.uniform(0, 1.6, n_pts)
    r = (1.6 - h) / 1.6 * 0.6 * np.sqrt(rng.uniform(0, 1, n_pts))
    a = rng.uniform(0, 2 * np.pi, n_pts)
    P = np.c_[r * np.cos(a), h, r * np.sin(a)]
    W, H = 1280, 720
    f0, k1 = 0.85, -0.05
    segs, poses = {}, {}
    for k in range(n_cams):
        th = 2 * np.pi * k / n_cams * 0.6
        eye = np.array([3.0 * np.cos(th), 0.8 + 0.2 * np.sin(3 * th), 3.0 * np.sin(th)])
        R, t = look_at(eye, np.array([0, 0.8, 0]))
        K = Intrinsics(f0 * W, (W - 1) / 2, (H - 1) / 2, k1, 0.0)
        uv, z = project(P, R, t, K)
        vis = (z > 0) & (rng.uniform(size=n_pts) < 0.85)
        uv = uv + rng.normal(0, noise, uv.shape)
        bad = rng.choice(np.nonzero(vis)[0], outliers, replace=False)
        uv[bad] += rng.normal(0, 30, (outliers, 2))
        sid = f"s{k}"
        segs[sid] = sfm.SegmentObs(sid, W, H, {i: uv[i] for i in np.nonzero(vis)[0]})
        poses[sid] = (R, t)
    # Start from perturbed cameras and points, in the frame of camera 0 (as the solver does).
    R0, t0 = poses["s0"]
    cams = sfm.Cameras(f0 * 1.03, 0.0, 0.0)
    for sid, (R, t) in poses.items():
        Rc = R @ R0.T
        tc = t - Rc @ t0
        if sid != "s0":
            dr = rng.normal(0, 0.01, 3)
            Rc = rodrigues(dr) @ Rc
            tc = tc + rng.normal(0, 0.03, 3)
        cams.poses[sid] = (Rc, tc)
        cams.fdev[sid] = 0.0
        cams.sizes[sid] = (W, H)
    Pc = P @ R0.T + t0
    cams.points = {i: Pc[i] + rng.normal(0, 0.02, 3) for i in range(n_pts)}
    return segs, cams


def _run(segs, cams, use_native: bool, **kw):
    c = copy.deepcopy(cams)
    lib = native.lib
    try:
        if not use_native:
            native.lib = None
        sfm.bundle_adjust(segs, c, "s0", "s1", **kw)
    finally:
        native.lib = lib
    return c


def test_gradient_matches_finite_differences():
    segs, cams = scene(1, n_cams=5, n_pts=30)
    # The problem as bundle_adjust lays it out.
    sids = list(cams.poses)
    free = [s for s in sids if s != "s0"]
    leds = sorted(cams.points)
    li = {l: k for k, l in enumerate(leds)}
    obs = [(sids.index(s), li[l], uv) for s in sids for l, uv in segs[s].leds.items() if l in li]
    obs_s = np.array([o[0] for o in obs], np.int64)
    obs_p = np.array([o[1] for o in obs], np.int64)
    obs_uv = np.array([o[2] for o in obs], float)
    rng = np.random.default_rng(3)
    x = np.concatenate([[np.log(cams.f0), -0.03, 0.01], rng.normal(0, 0.01, len(sids)),
                        np.concatenate([np.r_[to_rvec(cams.poses[s][0]), cams.poses[s][1]] for s in free]),
                        np.array([cams.points[l] for l in leds]).ravel()])
    size = np.full(len(sids), 1280.0)
    cx, cy = np.full(len(sids), 639.5), np.full(len(sids), 359.5)
    free_idx = np.array([-1] + list(range(len(free))), np.int64)
    R0, t0 = cams.poses["s0"]
    C1 = -cams.poses["s1"][0].T @ cams.poses["s1"][1]
    scale0 = float(np.linalg.norm(C1 + R0.T @ t0)) * 1.1
    for fix_f0 in (False, True):
        args = (obs_s, obs_p, obs_uv, size, cx, cy, free_idx, R0, t0, len(leds), fix_f0, float(np.log(cams.f0)) + 0.01,
                sfm.FOCAL_PRIOR, 1, scale0)

        def cost(xx):
            r, _g = native.lib.ba_residuals(xx, *args)
            return 0.5 * 4.0 * np.log1p((r / 2.0) ** 2).sum()

        _r, g = native.lib.ba_residuals(x, *args)
        num = np.zeros_like(x)
        for k in range(len(x)):
            e = np.zeros_like(x)
            e[k] = 1e-6 * max(1.0, abs(x[k]))
            num[k] = (cost(x + e) - cost(x - e)) / (2 * e[k])
        assert np.allclose(g, num, rtol=1e-4, atol=1e-4 * np.abs(num).max())


def test_native_reaches_scipys_minimum():
    # scipy (trf, lsmr, finite-difference Jacobian) needs ~2000 evaluations on this scene to
    # converge; the native solver gets there in a few. Focal length and depth trade off
    # (cameras on a ring at one distance), so the two settle a little apart along that valley.
    segs, cams = scene(2)
    a = _run(segs, cams, True, max_nfev=50)
    b = _run(segs, cams, False, max_nfev=2000)
    assert a.cost == pytest.approx(b.cost, rel=2e-3)
    assert a.rms_px == pytest.approx(b.rms_px, rel=0.01)
    assert abs(np.log(a.f0 / b.f0)) < 0.02
    scale = max(np.linalg.norm(a.center(s)) for s in cams.poses)
    for s in cams.poses:
        assert np.linalg.norm(a.center(s) - b.center(s)) < 0.03 * scale
    assert set(a.points) == set(b.points)
    d = np.array([np.linalg.norm(a.points[i] - b.points[i]) for i in a.points])
    assert np.median(d) < 0.03 * scale


@pytest.mark.parametrize("fix_f0", [False, True])
@pytest.mark.parametrize("max_nfev", [10, 25])
def test_native_no_worse_than_scipy_at_the_same_budget(fix_f0, max_nfev):
    segs, cams = scene(5)
    a = _run(segs, cams, True, max_nfev=max_nfev, fix_f0=fix_f0)
    b = _run(segs, cams, False, max_nfev=max_nfev, fix_f0=fix_f0)
    assert a.cost <= b.cost * 1.001
    if fix_f0:
        assert a.f0 == pytest.approx(cams.f0)


def test_native_is_deterministic():
    segs, cams = scene(4)
    a = _run(segs, cams, True, max_nfev=25)
    b = _run(segs, cams, True, max_nfev=25)
    assert a.f0 == b.f0 and a.k1 == b.k1
    for s in cams.poses:
        assert np.array_equal(a.poses[s][0], b.poses[s][0]) and np.array_equal(a.poses[s][1], b.poses[s][1])
    for i in a.points:
        assert np.array_equal(a.points[i], b.points[i])
