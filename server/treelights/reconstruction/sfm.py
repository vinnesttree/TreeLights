"""Camera poses and intrinsics from LED correspondences (architecture §9.2, §9.3).

Structure from motion with known correspondences: the LED index identifies a
point in every segment, so no feature matching is needed. Only unambiguous
observations (one clear candidate) are used to estimate the cameras.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from .camera import Intrinsics, camera_center, rodrigues, to_rvec, undistort_normalized
from .geometry import project_points, rodrigues_batch, triangulate_rays

log = logging.getLogger(__name__)

FOV_STARTS = (55.0, 65.0, 75.0, 85.0)
FOCAL_PRIOR = 0.02  # per-segment focal length tied to the shared one (relative sigma)
REPROJ_INLIER_PX = 4.0
MIN_PNP = 12


@dataclass
class SegmentObs:
    id: str
    width: int
    height: int
    leds: dict[int, np.ndarray]  # led -> unambiguous pixel (2,)
    up_cam: np.ndarray | None = None


@dataclass
class Cameras:
    f0: float  # shared focal length, in units of the longer image side
    k1: float
    k2: float
    poses: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)  # seg -> (R, t)
    fdev: dict[str, float] = field(default_factory=dict)  # seg -> log focal deviation
    points: dict[int, np.ndarray] = field(default_factory=dict)  # led -> X used for camera estimation
    cost: float = np.inf
    rms_px: float = np.inf
    n_obs: int = 0
    sizes: dict[str, tuple[int, int]] = field(default_factory=dict)
    seed: tuple[str, str] = ("", "")

    def intrinsics(self, seg: str) -> Intrinsics:
        w, h = self.sizes[seg]
        f = self.f0 * max(w, h) * float(np.exp(self.fdev.get(seg, 0.0)))
        return Intrinsics(f, (w - 1) / 2, (h - 1) / 2, self.k1, self.k2)

    def center(self, seg: str) -> np.ndarray:
        R, t = self.poses[seg]
        return camera_center(R, t)


def unambiguous(cands: list[dict]) -> np.ndarray | None:
    if not cands:
        return None
    if len(cands) == 1 or cands[1].get("score", 0) < 0.5 * cands[0].get("score", 1):
        return np.array([cands[0]["x"], cands[0]["y"]], float)
    return None


def _K(seg: SegmentObs, f0: float) -> Intrinsics:
    return Intrinsics(f0 * max(seg.width, seg.height), (seg.width - 1) / 2, (seg.height - 1) / 2)


def _pair_init(a: SegmentObs, b: SegmentObs, f0: float) -> tuple[np.ndarray, np.ndarray, dict[int, np.ndarray], float] | None:
    common = sorted(set(a.leds) & set(b.leds))
    if len(common) < 15:
        return None
    Ka, Kb = _K(a, f0), _K(b, f0)
    pa = undistort_normalized(np.array([a.leds[i] for i in common]), Ka)
    pb = undistort_normalized(np.array([b.leds[i] for i in common]), Kb)
    thr = REPROJ_INLIER_PX / Ka.f
    E, mask = cv2.findEssentialMat(pa, pb, np.eye(3), method=cv2.RANSAC, prob=0.999, threshold=thr)
    if E is None or E.shape != (3, 3):
        return None
    n_in, R, t, mask2 = cv2.recoverPose(E, pa, pb, np.eye(3), mask=mask)
    if n_in < 12:
        return None
    t = t.ravel()
    P1 = np.hstack([np.eye(3), np.zeros((3, 1))])
    P2 = np.hstack([R, t[:, None]])
    inl = mask2.ravel() > 0
    Xh = cv2.triangulatePoints(P1, P2, pa[inl].T, pb[inl].T)
    X = (Xh[:3] / Xh[3]).T
    ids = [common[k] for k in np.nonzero(inl)[0]]
    ok = (X[:, 2] > 0) & ((X @ R.T + t)[:, 2] > 0)
    pts = {i: x for i, x, o in zip(ids, X, ok) if o}
    if len(pts) < 12:
        return None
    # Triangulation angle: rays from both centers.
    C2 = -R.T @ t
    Xa = np.array(list(pts.values()))
    v1 = Xa / np.linalg.norm(Xa, axis=1, keepdims=True)
    v2 = (Xa - C2) / np.linalg.norm(Xa - C2, axis=1, keepdims=True)
    ang = float(np.degrees(np.median(np.arccos(np.clip((v1 * v2).sum(1), -1, 1)))))
    return R, t, pts, ang


def _register(seg: SegmentObs, cams: Cameras) -> bool:
    ids = [i for i in seg.leds if i in cams.points]
    if len(ids) < MIN_PNP:
        return False
    K = _K(seg, cams.f0)
    K.k1, K.k2 = cams.k1, cams.k2
    obj = np.array([cams.points[i] for i in ids], np.float64)
    img = np.array([seg.leds[i] for i in ids], np.float64)
    ok, rvec, tvec, inl = cv2.solvePnPRansac(obj, img, K.K, K.dist, reprojectionError=REPROJ_INLIER_PX,
                                             iterationsCount=500, confidence=0.999, flags=cv2.SOLVEPNP_EPNP)
    if not ok or inl is None or len(inl) < MIN_PNP:
        return False
    inl = inl.ravel()
    rvec, tvec = cv2.solvePnPRefineLM(obj[inl], img[inl], K.K, K.dist, rvec, tvec)
    cams.poses[seg.id] = (rodrigues(rvec.ravel()), tvec.ravel())
    cams.fdev[seg.id] = 0.0
    cams.sizes[seg.id] = (seg.width, seg.height)
    return True


def _triangulate_new(segs: dict[str, SegmentObs], cams: Cameras) -> None:
    by_led: dict[int, list[tuple[str, np.ndarray]]] = {}
    for sid, s in segs.items():
        if sid not in cams.poses:
            continue
        for led, uv in s.leds.items():
            by_led.setdefault(led, []).append((sid, uv))
    for led, ob in by_led.items():
        if led in cams.points or len(ob) < 2:
            continue
        Cs, ds = [], []
        for sid, uv in ob:
            R, t = cams.poses[sid]
            K = cams.intrinsics(sid)
            n = undistort_normalized(uv[None], K)[0]
            d = R.T @ np.array([n[0], n[1], 1.0])
            Cs.append(-R.T @ t)
            ds.append(d / np.linalg.norm(d))
        X = triangulate_rays(np.array(Cs), np.array(ds))
        # Keep only if in front of all cameras and consistent.
        good = 0
        for sid, uv in ob:
            R, t = cams.poses[sid]
            K = cams.intrinsics(sid)
            Xc = R @ X + t
            if Xc[2] <= 0:
                good = -99
                break
            p = Xc[:2] / Xc[2]
            r2 = p @ p
            p = p * (1 + K.k1 * r2 + K.k2 * r2 * r2) * K.f + [K.cx, K.cy]
            good += np.hypot(*(p - uv)) < 2 * REPROJ_INLIER_PX
        if good >= 2:
            cams.points[led] = X


def bundle_adjust(segs: dict[str, SegmentObs], cams: Cameras, fixed: str, scale_ref: str | None,
                  max_nfev: int = 100, loss: str = "cauchy") -> None:
    """Robust sparse BA over poses, per-segment focal deviation, shared f0/k1/k2 and points."""
    sids = [s for s in cams.poses]
    free = [s for s in sids if s != fixed]
    leds = sorted(cams.points)
    li = {l: k for k, l in enumerate(leds)}
    si = {s: k for k, s in enumerate(sids)}
    obs_s, obs_p, obs_uv = [], [], []
    for s in sids:
        for led, uv in segs[s].leds.items():
            if led in li:
                obs_s.append(si[s])
                obs_p.append(li[led])
                obs_uv.append(uv)
    if not obs_uv:
        return
    obs_s = np.array(obs_s)
    obs_p = np.array(obs_p)
    obs_uv = np.array(obs_uv)
    nfree, npts, nseg = len(free), len(leds), len(sids)
    fi = {s: k for k, s in enumerate(free)}
    R0, t0 = cams.poses[fixed]
    scale0 = np.linalg.norm(camera_center(*cams.poses[scale_ref]) - camera_center(R0, t0)) if scale_ref else None
    size = np.array([max(cams.sizes[s]) for s in sids], float)
    cx = np.array([(cams.sizes[s][0] - 1) / 2 for s in sids])
    cy = np.array([(cams.sizes[s][1] - 1) / 2 for s in sids])

    # Parameter layout: [f0, k1, k2] [fdev per seg] [rvec,t per free seg] [points]
    o_fd = 3
    o_pose = o_fd + nseg
    o_pts = o_pose + 6 * nfree
    x0 = np.concatenate([[np.log(cams.f0), cams.k1, cams.k2], [cams.fdev.get(s, 0.0) for s in sids],
                         np.concatenate([np.concatenate([to_rvec(cams.poses[s][0]), cams.poses[s][1]]) for s in free]) if free else np.zeros(0),
                         np.array([cams.points[l] for l in leds]).ravel()])
    Rfix = R0
    tfix = t0
    is_free = np.array([s in fi for s in sids])
    free_idx = np.array([fi.get(s, -1) for s in sids])

    def unpack(x: np.ndarray):
        poses = x[o_pose:o_pts].reshape(nfree, 6) if nfree else np.zeros((0, 6))
        Rf = rodrigues_batch(poses[:, :3]) if nfree else np.zeros((0, 3, 3))
        R = np.empty((nseg, 3, 3))
        t = np.empty((nseg, 3))
        R[~is_free] = Rfix
        t[~is_free] = tfix
        R[is_free] = Rf[free_idx[is_free]]
        t[is_free] = poses[free_idx[is_free], 3:]
        return R, t, x[o_pts:].reshape(npts, 3)

    def residuals(x: np.ndarray) -> np.ndarray:
        R, t, P = unpack(x)
        f = np.exp(x[0] + x[o_fd + obs_s]) * size[obs_s]
        uv, z = project_points(P[obs_p], R[obs_s], t[obs_s], f, cx[obs_s], cy[obs_s], x[1], x[2])
        r = (uv - obs_uv).ravel()
        prior = x[o_fd : o_fd + nseg] / FOCAL_PRIOR
        extra = [prior, [x[2] * 10.0]]  # keep k2 small unless the data insists
        if scale0:
            C = -np.einsum("ji,j->i", R[si[scale_ref]], t[si[scale_ref]])
            Cf = -Rfix.T @ tfix
            extra.append([(np.linalg.norm(C - Cf) - scale0) / scale0 * 100.0])
        return np.concatenate([r] + [np.asarray(e, float) for e in extra])

    m = 2 * len(obs_uv)
    n_extra = nseg + 1 + (1 if scale0 else 0)
    A = lil_matrix((m + n_extra, len(x0)), dtype=int)
    rows = np.arange(len(obs_uv))
    for c in range(3):
        A[:m, c] = 1
    for k in range(2):
        A[2 * rows + k, o_fd + obs_s] = 1
        for j in range(3):
            A[2 * rows + k, o_pts + 3 * obs_p + j] = 1
        fr = free_idx[obs_s]
        sel = fr >= 0
        for j in range(6):
            A[2 * rows[sel] + k, o_pose + 6 * fr[sel] + j] = 1
    for k in range(nseg):
        A[m + k, o_fd + k] = 1
    A[m + nseg, 2] = 1
    if scale0:
        A[m + nseg + 1, o_pose + 6 * fi[scale_ref] : o_pose + 6 * fi[scale_ref] + 6] = 1
    sol = least_squares(residuals, x0, jac_sparsity=A, loss=loss, f_scale=2.0, x_scale="jac",
                        max_nfev=max_nfev, method="trf", tr_solver="lsmr")
    x = sol.x
    R, t, P = unpack(x)
    cams.f0, cams.k1, cams.k2 = float(np.exp(x[0])), float(x[1]), float(x[2])
    for s in sids:
        cams.fdev[s] = float(x[o_fd + si[s]])
        cams.poses[s] = (R[si[s]], t[si[s]])
    for l in leds:
        cams.points[l] = P[li[l]]
    r = residuals(x)[:m].reshape(-1, 2)
    e = np.linalg.norm(r, axis=1)
    cams.rms_px = float(np.sqrt(np.mean(np.minimum(e, 10) ** 2)))
    cams.cost = float(np.mean(np.log1p((e / 2.0) ** 2)))
    cams.n_obs = len(e)
    # Drop points that stayed inconsistent so they don't steer later steps.
    bad = np.zeros(npts, bool)
    np.maximum.at(bad, obs_p, e > 3 * REPROJ_INLIER_PX)
    for l in [leds[k] for k in np.nonzero(bad)[0]]:
        cams.points.pop(l, None)


def _incremental(segs: dict[str, SegmentObs], a: str, b: str, init, f0: float) -> Cameras:
    R, t, pts, _ang = init
    cams = Cameras(f0, 0.0, 0.0)
    cams.poses[a] = (np.eye(3), np.zeros(3))
    cams.poses[b] = (R, t)
    for s in (a, b):
        cams.fdev[s] = 0.0
        cams.sizes[s] = (segs[s].width, segs[s].height)
    cams.points = dict(pts)
    bundle_adjust(segs, cams, a, b, max_nfev=15)
    remaining = [s for s in segs if s not in cams.poses]
    while remaining:
        remaining.sort(key=lambda s: -sum(1 for i in segs[s].leds if i in cams.points))
        added = False
        for s in list(remaining):
            if _register(segs[s], cams):
                remaining.remove(s)
                _triangulate_new(segs, cams)
                bundle_adjust(segs, cams, a, b, max_nfev=10)
                added = True
                break
        if not added:
            break
    _triangulate_new(segs, cams)
    bundle_adjust(segs, cams, a, b, max_nfev=25)
    cams.seed = (a, b)
    return cams


def solve_cameras(segs: dict[str, SegmentObs], f0_prior: float | None = None) -> Cameras | None:
    """Multi-start: focal guesses x top seed pairs; keep the best solution (§9.3)."""
    starts = [f0_prior] if f0_prior else [0.5 / np.tan(np.radians(fov) / 2) for fov in FOV_STARTS]
    best: Cameras | None = None
    ids = list(segs)
    for f0 in starts:
        cand = []
        for a, b in itertools.combinations(ids, 2):
            init = _pair_init(segs[a], segs[b], f0)
            if init is None:
                continue
            n_in, ang = len(init[2]), init[3]
            if ang < 3.0:
                continue
            cand.append((n_in * min(1.0, ang / 10.0), a, b, init))
        cand.sort(key=lambda c: -c[0])
        for _score, a, b, init in cand[:2]:
            try:
                cams = _incremental(segs, a, b, init, f0)
            except (np.linalg.LinAlgError, ValueError, cv2.error) as e:
                log.debug("start failed: %s", e)
                continue
            key = (-len(cams.poses), cams.cost)
            if best is None or key < (-len(best.poses), best.cost):
                best = cams
            if len(cams.poses) == len(segs):
                break  # the next seed pair only matters if this one left segments out
    if best is not None:
        a, b = best.seed
        _triangulate_new(segs, best)
        bundle_adjust(segs, best, a, b, max_nfev=300)
    return best
