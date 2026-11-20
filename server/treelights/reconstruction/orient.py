"""World orientation: up axis by consensus, tree axis, visibility check (§9.3, §9.4)."""

from __future__ import annotations

import itertools

import numpy as np

from .camera import to_rvec
from .sfm import Cameras

AGREE_DEG = 15.0
WEIGHTS = {"turning_axis": 3.0, "gravity": 2.0, "camera_up": 1.0, "camera_plane": 1.0, "point_cloud": 0.5}


def _unit(v: np.ndarray) -> np.ndarray:
    return v / max(float(np.linalg.norm(v)), 1e-12)


def _angle(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.degrees(np.arccos(np.clip(_unit(a) @ _unit(b), -1, 1))))


def up_candidates(cams: Cameras, up_cam: dict[str, list[float] | None], points: np.ndarray) -> dict[str, np.ndarray]:
    sids = list(cams.poses)
    cand: dict[str, np.ndarray] = {}
    cam_up = _unit(np.mean([-cams.poses[s][0][1] for s in sids], axis=0))
    cand["camera_up"] = cam_up
    # Turning axis: relative rotations between views all share the tree's vertical axis.
    axes = []
    for a, b in itertools.combinations(sids, 2):
        Rrel = cams.poses[a][0].T @ cams.poses[b][0]
        rv = to_rvec(Rrel)
        if np.degrees(np.linalg.norm(rv)) > 15:
            ax = _unit(rv)
            axes.append(ax if ax @ cam_up >= 0 else -ax)
    if len(axes) >= 2:
        cand["turning_axis"] = _unit(np.mean(axes, axis=0))
    g = [cams.poses[s][0].T @ np.asarray(up_cam[s], float) for s in sids if up_cam.get(s) is not None]
    if g:
        cand["gravity"] = _unit(np.mean([_unit(v) for v in g], axis=0))
    if len(sids) >= 3:
        C = np.array([cams.center(s) for s in sids])
        _u, sv, vt = np.linalg.svd(C - C.mean(0))
        if sv[1] > 1e-6 and sv[2] < 0.5 * sv[1]:  # cameras spread out in a plane
            nrm = vt[2]
            cand["camera_plane"] = nrm if nrm @ cam_up >= 0 else -nrm
    if len(points) >= 10:
        _u, _s, vt = np.linalg.svd(points - points.mean(0))
        ax = vt[0]
        cand["point_cloud"] = ax if ax @ cam_up >= 0 else -ax
    return cand


def up_consensus(cand: dict[str, np.ndarray]) -> tuple[np.ndarray, dict]:
    best_name, best_w = None, -1.0
    for name, v in cand.items():
        w = sum(WEIGHTS[n] for n, u in cand.items() if _angle(u, v) <= AGREE_DEG)
        if w > best_w:
            best_name, best_w = name, w
    group = [n for n, u in cand.items() if _angle(u, cand[best_name]) <= AGREE_DEG]
    up = _unit(sum(WEIGHTS[n] * cand[n] for n in group))
    report = {"used": group, "angles_deg": {n: round(_angle(u, up), 1) for n, u in cand.items()},
              "disagree": [n for n in cand if n not in group]}
    return up, report


def rotation_to_y(up: np.ndarray) -> np.ndarray:
    """Rotation R with R @ up = +y (minimal rotation)."""
    a = _unit(up)
    b = np.array([0.0, 1.0, 0.0])
    v = np.cross(a, b)
    c = float(a @ b)
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1 / (1 + c))


def fit_circle_xz(P: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Algebraic circle fit in the horizontal plane. Returns (center xz, radius, rms residual)."""
    x, z = P[:, 0], P[:, 2]
    A = np.stack([x, z, np.ones_like(x)], axis=1)
    b = x * x + z * z
    sol = np.linalg.lstsq(A, b, rcond=None)[0]
    cx, cz = sol[0] / 2, sol[1] / 2
    r = float(np.sqrt(max(sol[2] + cx * cx + cz * cz, 0)))
    res = np.hypot(x - cx, z - cz) - r
    return np.array([cx, cz]), r, float(np.sqrt(np.mean(res**2)))


def tree_axis_xz(camera_centers_y: np.ndarray, points_y: np.ndarray) -> tuple[np.ndarray, str]:
    """Horizontal position of the tree axis in a y-up frame.

    With the tree turned in front of a fixed camera, the camera centers (in
    the tree's frame) lie on a circle around the turning axis.
    """
    if len(camera_centers_y) >= 3:
        c, r, rms = fit_circle_xz(camera_centers_y)
        spread = np.ptp(np.degrees(np.arctan2(camera_centers_y[:, 2] - c[1], camera_centers_y[:, 0] - c[0])))
        if r > 0 and rms < 0.08 * r and spread > 60:
            return c, "turning_axis"
    return np.median(points_y[:, [0, 2]], axis=0), "point_median"


def visibility_side_fraction(centers: dict[str, np.ndarray], seen: dict[str, list[np.ndarray]], axis_xz: np.ndarray) -> float:
    """Share of detections that lie on the camera's side of the tree (y-up frame).
    A depth-reversed ("inside-out") reconstruction gives a low value."""
    good = total = 0
    for s, pts in seen.items():
        if not pts:
            continue
        P = np.array(pts)
        cdir = centers[s][[0, 2]] - axis_xz
        pdir = P[:, [0, 2]] - axis_xz
        far = np.linalg.norm(pdir, axis=1) > 1e-6
        good += int(((pdir[far] @ cdir) > 0).sum())
        total += int(far.sum())
    return good / total if total else 1.0
