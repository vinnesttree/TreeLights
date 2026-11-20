"""Phone orientation sensor -> camera rotation, self-calibrated from the video.

The phone reports its orientation sensor quaternion (gyroscope + accelerometer;
world frame with z up). How the device axes map to the camera's image axes
depends on the platform, the screen orientation and the sensor's quaternion
convention, and getting it wrong would silently corrupt everything. So it isn't
assumed: the mapping (one of the 24 axis-aligned rotations, for both quaternion
conventions), the focal length and the sensor-to-frame time offset are chosen
to best predict how tracked LEDs move between consecutive frames, using
rotation-only homographies. If no mapping predicts the motion clearly better
than "no rotation", the gyro is not used.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

from .tracking import apply_h


def quat_to_R(q: np.ndarray) -> np.ndarray:
    """(x, y, z, w) -> rotation matrix."""
    x, y, z, w = np.asarray(q, float) / np.linalg.norm(q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def R_to_quat(R: np.ndarray) -> np.ndarray:
    """rotation matrix -> (x, y, z, w)."""
    m = R
    tr = np.trace(m)
    if tr > 0:
        s = np.sqrt(tr + 1.0) * 2
        w, x, y, z = 0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w, x, y, z = (m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w, x, y, z = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w, x, y, z = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s
    return np.array([x, y, z, w])


def axis_rotations() -> list[np.ndarray]:
    out = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            M = np.zeros((3, 3))
            for r, (c, sg) in enumerate(zip(perm, signs)):
                M[r, c] = sg
            if np.linalg.det(M) > 0:
                out.append(M)
    return out


CANDIDATES = axis_rotations()
# The usual case: portrait, back camera, W3C quaternion = device -> world.
DEFAULT_M = np.diag([1.0, -1.0, -1.0])


def slerp_log(log_t: np.ndarray, log_q: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Interpolate orientation samples (N, 4 xyzw) at times t (normalized lerp; samples are dense)."""
    t = np.clip(np.asarray(t, float), log_t[0], log_t[-1])
    i = np.clip(np.searchsorted(log_t, t) - 1, 0, len(log_t) - 2)
    a = (t - log_t[i]) / np.maximum(log_t[i + 1] - log_t[i], 1e-9)
    q0, q1 = log_q[i], log_q[i + 1].copy()
    flip = (q0 * q1).sum(axis=1) < 0
    q1[flip] *= -1
    q = q0 * (1 - a)[:, None] + q1 * a[:, None]
    return q / np.linalg.norm(q, axis=1, keepdims=True)


@dataclass
class GyroModel:
    M: np.ndarray
    inverse: bool  # True: the quaternion maps world -> device
    f: float  # pixels, for the calibration resolution
    delta: float  # seconds to add to frame times to read the sensor log
    residual_px: float
    baseline_px: float  # residual predicting no rotation
    ok: bool
    size: int = 0  # longer image side the focal length refers to

    def R_cam(self, q: np.ndarray) -> np.ndarray:
        """World -> camera rotation for a sensor quaternion."""
        Rd = quat_to_R(q)
        Rwd = Rd if self.inverse else Rd.T  # world -> device
        return self.M @ Rwd

    def up_cam(self, q: np.ndarray) -> np.ndarray:
        return self.R_cam(q) @ np.array([0.0, 0.0, 1.0])

    def heading_deg(self, q: np.ndarray) -> float:
        fwd = self.R_cam(q).T @ np.array([0.0, 0.0, 1.0])
        return float(np.degrees(np.arctan2(fwd[1], fwd[0])))


def _pairs_score(pairs, Rs: dict[int, np.ndarray], K: np.ndarray, cap: float = 20.0) -> float:
    """How well rotation-only prediction explains LED motion between frames.

    While walking around the tree, the camera's translation adds an image shift
    comparable to hand-jitter rotation, but it changes slowly and smoothly. So the
    score is the *high-frequency* part of each pair's mean residual (residual minus
    a moving median over neighbouring pairs) plus the spread within each pair.
    A wrong mapping or focal length leaves jitter-driven residuals that change from
    frame to frame."""
    Kinv = np.linalg.inv(K)
    means, spreads = [], []
    for i, j, p, q in pairs:
        Ri, Rj = Rs.get(i), Rs.get(j)
        if Ri is None or Rj is None:
            continue
        pred = apply_h(K @ (Rj @ Ri.T) @ Kinv, p)
        r = pred - q
        m = np.median(r, axis=0)
        means.append(m)
        spreads.append(min(float(np.median(np.linalg.norm(r - m, axis=1))), cap))
    if len(means) < 5:
        return np.inf
    M = np.array(means)
    w = 4
    smooth = np.array([np.median(M[max(0, k - w) : k + w + 1], axis=0) for k in range(len(M))])
    hf = np.minimum(np.linalg.norm(M - smooth, axis=1), cap)
    return float(np.median(hf) + np.median(spreads))


def collect_pairs(rows: np.ndarray, frame_t: np.ndarray, max_dt: float = 0.1, min_common: int = 15, max_pairs: int = 400):
    """Consecutive frame pairs with the positions of tracks seen in both."""
    by_frame: dict[int, dict[int, np.ndarray]] = {}
    for r in rows:
        by_frame.setdefault(int(r[1]), {})[int(r[0])] = r[2:4]
    frames = sorted(by_frame)
    out = []
    for a, b in zip(frames, frames[1:]):
        if frame_t[b] - frame_t[a] > max_dt:
            continue
        common = set(by_frame[a]) & set(by_frame[b])
        if len(common) < min_common:
            continue
        ids = sorted(common)
        out.append((a, b, np.array([by_frame[a][i] for i in ids]), np.array([by_frame[b][i] for i in ids])))
    if len(out) > max_pairs:
        step = len(out) / max_pairs
        out = [out[int(k * step)] for k in range(max_pairs)]
    return out


def calibrate(rows: np.ndarray, frame_t: np.ndarray, log_t: np.ndarray, log_q: np.ndarray, width: int, height: int,
              f_prior: float | None = None) -> GyroModel:
    """Choose the device->camera mapping, focal length and time offset that best predict
    tracked LED motion between consecutive frames."""
    size = max(width, height)
    cx, cy = (width - 1) / 2, (height - 1) / 2
    pairs = collect_pairs(rows, frame_t)
    none = GyroModel(DEFAULT_M, False, f_prior or 0.7 * size, 0.0, np.inf, np.inf, False, size)
    if len(pairs) < 10 or len(log_t) < 10:
        return none
    used = sorted({i for a, b, _p, _q in pairs for i in (a, b)})

    def Kf(f: float) -> np.ndarray:
        return np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])

    def rotations(M, inverse, delta):
        q = slerp_log(log_t, log_q, frame_t[used] + delta)
        Rs = {}
        for i, qq in zip(used, q):
            Rd = quat_to_R(qq)
            Rs[i] = M @ (Rd if inverse else Rd.T)
        return Rs

    eye = {i: np.eye(3) for i in used}
    baseline = _pairs_score(pairs, eye, Kf(0.7 * size))
    f_grid = [f_prior] if f_prior else list(size * np.array([0.45, 0.55, 0.65, 0.75, 0.9, 1.1, 1.35]))
    best = (np.inf, None)
    for inverse in (False, True):
        for M in CANDIDATES:
            Rs = rotations(M, inverse, 0.0)
            for f in f_grid:
                s = _pairs_score(pairs, Rs, Kf(f))
                if s < best[0]:
                    best = (s, (M, inverse, f))
    _s, (M, inverse, f) = best
    # Refine focal length and time offset for the chosen mapping.
    best_s, best_f, best_d = best[0], f, 0.0
    for delta in np.arange(-0.1, 0.1001, 0.01):
        Rs = rotations(M, inverse, float(delta))
        for ff in f * np.array([0.85, 0.92, 1.0, 1.08, 1.17]) if not f_prior else [f]:
            s = _pairs_score(pairs, Rs, Kf(ff))
            if s < best_s:
                best_s, best_f, best_d = s, ff, float(delta)
    Rs = rotations(M, inverse, best_d)
    for ff in best_f * np.linspace(0.94, 1.06, 13) if not f_prior else [best_f]:
        s = _pairs_score(pairs, Rs, Kf(ff))
        if s < best_s:
            best_s, best_f = s, ff
    ok = bool(best_s < 3.0 and best_s < 0.6 * baseline)
    return GyroModel(M, inverse, float(best_f), best_d, best_s, baseline, ok, size)
