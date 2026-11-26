"""Phone orientation sensor -> camera rotation, self-calibrated from the video.

The phone reports its orientation sensor quaternion (gyroscope + accelerometer;
world frame with z up). How the device axes map to the camera's image axes
depends on the platform, the screen orientation and the sensor's quaternion
convention, and getting it wrong would silently corrupt everything. So it isn't
assumed: the mapping (one of the 24 axis-aligned rotations, for both quaternion
conventions), the focal length and the sensor-to-frame time offset are chosen
so that the sensor's rotations between frames a few seconds apart agree with the
rotations recovered from the images (essential matrices of the tracked LEDs).
The gyro is used only if one mapping clearly beats every mapping that would
predict something different, and beats "no rotation".
"""

from __future__ import annotations

import itertools
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

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
    info: dict = field(default_factory=dict)  # calibration diagnostics

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


def collect_long_pairs(rows: np.ndarray, frame_t: np.ndarray, gap_s: float = 1.0, step_s: float = 0.5,
                       min_common: int = 25, max_pairs: int = 60):
    """Frame pairs about gap_s apart (every step_s) with the positions of tracks seen in both."""
    by_frame: dict[int, dict[int, np.ndarray]] = {}
    for r in rows:
        by_frame.setdefault(int(r[1]), {})[int(r[0])] = r[2:4]
    frames = np.array(sorted(by_frame))
    if len(frames) < 2:
        return []
    ft = frame_t[frames]
    out, next_t = [], ft[0]
    for k, a in enumerate(frames):
        if ft[k] < next_t:
            continue
        j = int(np.searchsorted(ft, ft[k] + gap_s))
        if j >= len(frames) or abs(ft[j] - ft[k] - gap_s) > 0.2:
            continue
        b = frames[j]
        common = sorted(set(by_frame[a]) & set(by_frame[b]))
        if len(common) < min_common:
            continue
        out.append((int(a), int(b), np.array([by_frame[a][i] for i in common]), np.array([by_frame[b][i] for i in common])))
        next_t = ft[k] + step_s
        if len(out) >= max_pairs:
            break
    return out


def _image_rotations(pairs, f: float, cx: float, cy: float) -> list:
    """Relative rotation of each pair from the image alone (essential matrix: handles the
    walking translation), or None where it can't be estimated.

    The pairs are independent and OpenCV releases the GIL, so they run on threads: most of
    decoding a walk is spent here (each source tries about 11 focal lengths). OpenCV's RANSAC
    starts from the same seed on every call, so the result is identical to running them in
    order."""
    if len(pairs) < 2 or _THREADS < 2:
        return [_pair_rotation(p, q, f, cx, cy) for _a, _b, p, q in pairs]
    with ThreadPoolExecutor(min(_THREADS, len(pairs))) as ex:
        return list(ex.map(lambda pr: _pair_rotation(pr[2], pr[3], f, cx, cy), pairs))


_THREADS = min(4, os.cpu_count() or 1)


def _pair_rotation(p: np.ndarray, q: np.ndarray, f: float, cx: float, cy: float) -> np.ndarray | None:
    import cv2

    pa = (p - [cx, cy]) / f
    pb = (q - [cx, cy]) / f
    E, mask = cv2.findEssentialMat(pa, pb, np.eye(3), method=cv2.RANSAC, prob=0.999, threshold=1.5 / f)
    if E is None or E.shape != (3, 3):
        return None
    n_in, R, _t, _m = cv2.recoverPose(E, pa, pb, np.eye(3), mask=mask)
    return R if n_in >= 0.6 * len(pa) else None


def _angle_deg(R: np.ndarray) -> float:
    return float(np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1))))


def _refine_delta(rows, frame_t, log_t, log_q, M, inverse, f, cx, cy, span, d0) -> float:
    pairs = collect_long_pairs(rows, frame_t, gap_s=SHORT_GAP_S, step_s=max(0.25, span / 150), max_pairs=150)
    if len(pairs) < 20:
        return d0
    img = _image_rotations(pairs, f, cx, cy)
    used = sorted({i for a, b, _p, _q in pairs for i in (a, b)})

    def cost(delta: float) -> float:
        Rs = {i: quat_to_R(q) for i, q in zip(used, slerp_log(log_t, log_q, frame_t[used] + delta))}
        d = []
        for (a, b, _p, _q), Ri in zip(pairs, img):
            if Ri is not None:
                Ra = M @ (Rs[a] if inverse else Rs[a].T)
                Rb = M @ (Rs[b] if inverse else Rs[b].T)
                d.append(_angle_deg(Ri.T @ (Rb @ Ra.T)))
        return float(np.mean(np.minimum(d, 5.0))) if len(d) >= 10 else np.inf

    grid = np.arange(-0.15, 0.1501, 0.01)
    c = np.array([cost(float(x)) for x in grid])
    k = int(np.argmin(c))
    # Only move when the short pairs clearly prefer another offset (a steady walk
    # without jitter leaves the cost flat).
    c0 = cost(d0)
    if not np.isfinite(c[k]) or c0 - c[k] < 0.02:
        return d0
    return float(grid[k])


SHORT_GAP_S = 0.5
GAPS_S = (3.0, 2.0, 1.0)
MIN_PAIRS = 20


def calibrate(rows: np.ndarray, frame_t: np.ndarray, log_t: np.ndarray, log_q: np.ndarray, width: int, height: int,
              f_prior: float | None = None) -> GyroModel:
    """Choose the device->camera mapping, focal length and time offset under which the
    sensor's rotations agree with rotations estimated from the images.

    Image rotations come from essential matrices between frames ~1 s apart, so the
    walking translation (which cancels much of the rotation's image motion when the
    phone stays pointed at the tree) doesn't confuse the comparison."""
    size = max(width, height)
    cx, cy = (width - 1) / 2, (height - 1) / 2
    none = GyroModel(DEFAULT_M, False, f_prior or 0.7 * size, 0.0, np.inf, np.inf, False, size)
    if log_t is None or len(log_t) < 10 or len(rows) == 0:
        return none
    # The longest gap that still gives enough pairs: rotations of 20-30 degrees separate
    # the mappings far better than the ~8 degrees of a 1 s gap (when the walk is steady,
    # wrong mappings differ from the right one only by the camera's small pitch, an
    # error that grows with the rotation compared). Pairs are spread over the whole walk.
    span = float(np.ptp(frame_t[np.unique(rows[:, 1].astype(int))]))
    pairs = []
    for gap in GAPS_S:
        pairs = collect_long_pairs(rows, frame_t, gap_s=gap, step_s=max(0.5, (span - gap) / 60))
        if len(pairs) >= MIN_PAIRS:
            break
    if len(pairs) < 6:
        return none
    used = sorted({i for a, b, _p, _q in pairs for i in (a, b)})

    def sensor(delta: float) -> dict[int, np.ndarray]:
        q = slerp_log(log_t, log_q, frame_t[used] + delta)
        return {i: quat_to_R(qq) for i, qq in zip(used, q)}

    def score(img_R, Rd, M, inverse) -> float:
        diffs = []
        for (a, b, _p, _q), Ri in zip(pairs, img_R):
            if Ri is None:
                continue
            Ra = M @ (Rd[a] if inverse else Rd[a].T)
            Rb = M @ (Rd[b] if inverse else Rd[b].T)
            diffs.append(_angle_deg(Ri.T @ (Rb @ Ra.T)))
        return float(np.median(diffs)) if len(diffs) >= 4 else np.inf

    f_grid = [f_prior] if f_prior else list(size * np.array([0.45, 0.55, 0.65, 0.75, 0.9, 1.1, 1.35]))
    Rd0 = sensor(0.0)
    best = (np.inf, None)
    per_mapping: dict[tuple, float] = {}  # best score of each (mapping, convention)
    img_cache = {}
    for f in f_grid:
        img_cache[f] = _image_rotations(pairs, f, cx, cy)
        for inverse in (False, True):
            for mi, M in enumerate(CANDIDATES):
                sc = score(img_cache[f], Rd0, M, inverse)
                key = (mi, inverse)
                per_mapping[key] = min(per_mapping.get(key, np.inf), sc)
                if sc < best[0]:
                    best = (sc, (M, inverse, f))
    if best[1] is None:
        return none
    s0, (M, inverse, f) = best
    if not f_prior:
        for ff in f * np.array([0.88, 0.94, 1.06, 1.12]):
            img_cache[ff] = _image_rotations(pairs, ff, cx, cy)
            sc = score(img_cache[ff], Rd0, M, inverse)
            if sc < s0:
                s0, f = sc, ff
    best_d = 0.0
    for delta in np.arange(-0.1, 0.1001, 0.02):
        sc = score(img_cache[f], sensor(float(delta)), M, inverse)
        if sc < s0 - 1e-6:
            s0, best_d = sc, float(delta)
    # The time offset hardly changes rotations over seconds of steady turning; hand
    # jitter over short gaps shows it. Refine it there once the mapping is known.
    best_d = _refine_delta(rows, frame_t, log_t, log_q, M, inverse, f, cx, cy, span, best_d)
    # Baseline: how much the camera actually rotated (vs. claiming no rotation).
    base = [(_angle_deg(Ri)) for Ri in img_cache[f] if Ri is not None]
    baseline = float(np.median(base)) if base else 0.0
    # Accept when the chosen mapping clearly beats every mapping that would predict
    # something different, and beats "no rotation". While walking around the tree the
    # camera turns mostly about the vertical, so mappings that differ only by a turn
    # about that axis fit equally well; they predict the same relative rotations and
    # the same "up", so they are equivalent for every use here and don't count as rivals.
    Rd = sensor(best_d)
    used_pairs = [(a, b) for (a, b, _p, _q), Ri in zip(pairs, img_cache[f]) if Ri is not None]

    def cam(Mx, inv, i):
        return Mx @ (Rd[i] if inv else Rd[i].T)

    def predictions(Mx, inv):
        return [cam(Mx, inv, b) @ cam(Mx, inv, a).T for a, b in used_pairs]

    up_frames = used[:: max(1, len(used) // 20)]
    z = np.array([0.0, 0.0, 1.0])
    P0 = predictions(M, inverse)
    up0 = [cam(M, inverse, i) @ z for i in up_frames]
    equiv_tol = max(0.5, s0)
    runner_up = np.inf
    for (mi, inv), sc in per_mapping.items():
        Mx = CANDIDATES[mi]
        if inv == inverse and np.allclose(Mx, M):
            continue
        if sc >= runner_up:
            continue
        P = predictions(Mx, inv)
        dpred = float(np.median([_angle_deg(A.T @ B) for A, B in zip(P0, P)])) if P else np.inf
        dup = max(float(np.degrees(np.arccos(np.clip(u0 @ (cam(Mx, inv, i) @ z), -1, 1)))) for u0, i in zip(up0, up_frames))
        if dpred < equiv_tol and dup < 3.0:
            continue  # equivalent
        runner_up = sc
    ok = bool(s0 < 3.0 and runner_up > 1.5 * s0 and baseline > 2.5 * s0 and baseline > 2.0)
    info = {"pairs": len(used_pairs), "gap_s": round(float(frame_t[pairs[0][1]] - frame_t[pairs[0][0]]), 2), "score_deg": round(s0, 3), "runner_up_deg": round(float(runner_up), 3),
            "baseline_deg": round(baseline, 3)}
    return GyroModel(M, inverse, float(f), best_d, s0, baseline, ok, size, info)
