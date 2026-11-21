"""Tracking LED blobs across frames while the phone moves.

Per frame:
1. predict every live track: rotation between the frames from the gyro (as the
   homography K dR K^-1) when available, plus the track's own residual velocity
   (parallax: near and far LEDs move differently while walking);
2. correct the global motion: displacements between predicted tracks and detected
   blobs vote for a common shift (gyro timing/bias errors, no gyro at all);
3. associate one-to-one within a small gate; unmatched blobs start tracks; tracks
   coast through short gaps (the sync "off" slots) and end after MAX_COAST_S.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

from .blobs import FLUX, SAT, SHARP, X, Y

GATE_PX = 5.0
VOTE_GATE_GYRO = 25.0
VOTE_GATE_NOGYRO = 70.0
MAX_COAST_S = 0.6
MAX_DEVIATION_PX = 8.0
LOCK_FRAMES = 3
SHIFT_ALPHA = 0.3
MAX_LOST_S = 2.0
REACQUIRE_PX_PER_S = 5.0
REACQUIRE_MAX_PX = 15.0
VEL_ALPHA = 0.3


@dataclass
class Frame:
    t: float  # source time, seconds
    blobs: np.ndarray  # (k, 6)
    R: np.ndarray | None = None  # world -> camera rotation (gyro), if known
    meta: dict = field(default_factory=dict)


@dataclass
class TrackSet:
    # Detection rows: track id, frame index, x, y, flux, sharpness, saturated
    rows: np.ndarray
    n_tracks: int
    shifts: np.ndarray  # per frame global correction applied (px)

    def by_track(self) -> dict[int, np.ndarray]:
        order = np.argsort(self.rows[:, 0], kind="stable")
        r = self.rows[order]
        ids, starts = np.unique(r[:, 0].astype(int), return_index=True)
        out = {}
        for k, tid in enumerate(ids):
            end = starts[k + 1] if k + 1 < len(starts) else len(r)
            out[int(tid)] = r[starts[k] : end]
        return out


def rotation_homography(R_prev: np.ndarray, R_cur: np.ndarray, K: np.ndarray) -> np.ndarray:
    dR = R_cur @ R_prev.T
    return K @ dR @ np.linalg.inv(K)


def apply_h(H: np.ndarray, p: np.ndarray) -> np.ndarray:
    if len(p) == 0:
        return p
    q = np.c_[p, np.ones(len(p))] @ H.T
    return q[:, :2] / q[:, 2:3]


def _vote_shift(pred: np.ndarray, blobs: np.ndarray, gate: float) -> tuple[np.ndarray, int]:
    """Most common displacement between predicted tracks and blobs: a 2 px histogram of all
    pairwise displacements within the gate, smoothed 3x3; refined by the mean near the peak."""
    if len(pred) == 0 or len(blobs) == 0:
        return np.zeros(2), 0
    M = cKDTree(pred).sparse_distance_matrix(cKDTree(blobs), gate, output_type="coo_matrix")
    if M.nnz == 0:
        return np.zeros(2), 0
    d = blobs[M.col] - pred[M.row]
    nb = int(np.ceil(gate / 2.0))
    edges = np.arange(-nb, nb + 1) * 2.0 - 1.0
    H, _, _ = np.histogram2d(d[:, 0], d[:, 1], bins=[edges, edges])
    Hs = H.copy()
    Hs[1:, :] += H[:-1, :]
    Hs[:-1, :] += H[1:, :]
    Hs2 = Hs.copy()
    Hs2[:, 1:] += Hs[:, :-1]
    Hs2[:, :-1] += Hs[:, 1:]
    # Strong peaks (>= 70% of the best) compete; the one nearest zero motion wins, since a
    # lattice of LEDs also produces peaks at multiples of the LED spacing.
    centers = edges[:-1] + 1.0
    strong = np.argwhere(Hs2 >= 0.7 * Hs2.max())
    mag = np.hypot(centers[strong[:, 0]], centers[strong[:, 1]])
    ix, iy = strong[int(np.argmin(mag))]
    c = np.array([centers[ix], centers[iy]])
    near = np.abs(d - c).max(axis=1) <= 3.0
    if not near.any():
        return np.zeros(2), 0
    return d[near].mean(axis=0), int(near.sum())


def _greedy_match(pred: np.ndarray, pts: np.ndarray, gate: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
    """One-to-one nearest matching within a gate (scalar or per prediction)."""
    mt = np.full(len(pred), -1)
    mb = np.full(len(pts), -1)
    if not len(pred) or not len(pts):
        return mt, mb
    g = np.broadcast_to(np.asarray(gate, float), (len(pred),))
    dist, j = cKDTree(pts).query(pred, k=2, distance_upper_bound=float(g.max()))
    cand = [(dist[i, kk], i, int(j[i, kk])) for i in range(len(pred)) for kk in range(2)
            if np.isfinite(dist[i, kk]) and dist[i, kk] <= g[i]]
    cand.sort()
    for _d, i, jj in cand:
        if mt[i] < 0 and mb[jj] < 0:
            mt[i], mb[jj] = jj, i
    return mt, mb


def track(frames: list[Frame], K: np.ndarray | None = None, use_gyro: bool = True) -> TrackSet:
    """frames must be sorted by time.

    Lost tracks (not seen for MAX_COAST_S, e.g. hidden behind a branch) keep being
    predicted for up to MAX_LOST_S and can be re-acquired within a gate that grows with
    the gap: an LED is then one long track, which is what decoding needs (a full code
    cycle is 7.2 s). A wrong re-acquisition gives conflicting bit votes and the track
    is rejected by the decoder rather than mislabelled."""
    pos = np.zeros((0, 2)); vel = np.zeros((0, 2)); last = np.zeros(0); ids = np.zeros(0, int)  # noqa: E702
    lpos = np.zeros((0, 2)); lvel = np.zeros((0, 2)); llast = np.zeros(0); lids = np.zeros(0, int)  # noqa: E702
    next_id = 0
    rows = []
    shifts = np.zeros((len(frames), 2))
    shift_rate = np.zeros(2)  # px/s of motion not explained by the gyro
    lock = 0
    prev = None
    for fi, fr in enumerate(frames):
        b = fr.blobs
        bxy = b[:, [X, Y]].astype(float) if len(b) else np.zeros((0, 2))
        gyro = use_gyro and K is not None and fr.R is not None and prev is not None and prev.R is not None
        dt = fr.t - prev.t if prev is not None else 0.0
        H = rotation_homography(prev.R, fr.R, K) if gyro else None

        def move(p, v):
            base = apply_h(H, p) if H is not None else p.copy()
            return base, base + v * dt

        base, pred = move(pos, vel)
        lbase, lpred = move(lpos, lvel)
        if prev is not None and len(pred):
            # The motion the gyro doesn't explain (walking translation) changes smoothly:
            # start from the expected shift and correct around it. The vote takes the strong
            # peak nearest the expectation, and a large deviation is rejected: that is the
            # vote locking onto the LED lattice (every track moved onto its neighbour), which
            # happens most after the sync "off" slots when all tracks have coasted.
            # Walking around the tree while keeping it in view, the camera's rotation and its
            # translation largely cancel for LEDs near the tree, so the gyro's rotation alone
            # over-predicts their motion. Until that offset is learned (lock), vote wide.
            expected = shift_rate * dt
            pred, base, lpred, lbase = pred + expected, base + expected, lpred + expected, lbase + expected
            locked = gyro and lock >= LOCK_FRAMES
            s, votes = _vote_shift(pred, bxy, VOTE_GATE_GYRO if locked else VOTE_GATE_NOGYRO)
            if locked and np.hypot(*s) > MAX_DEVIATION_PX + 0.3 * np.hypot(*expected):
                votes = 0  # the vote locked onto the LED lattice
            if votes >= 3:
                pred, base, lpred, lbase = pred + s, base + s, lpred + s, lbase + s
                if dt > 0:
                    rate = (expected + s) / dt
                    shift_rate = rate if lock == 0 else (1 - SHIFT_ALPHA) * shift_rate + SHIFT_ALPHA * rate
                lock += 1
            else:
                lock = 0
            shifts[fi] = expected + (s if votes >= 3 else 0)
        mt, mb = _greedy_match(pred, bxy, GATE_PX)
        m = mt >= 0
        if m.any():
            new = bxy[mt[m]]
            if dt > 0:
                vel[m] = (1 - VEL_ALPHA) * vel[m] + VEL_ALPHA * (new - base[m]) / dt
            pos[m] = new
            last[m] = fr.t
            for i in np.nonzero(m)[0]:
                bb = b[mt[i]]
                rows.append((ids[i], fi, bb[X], bb[Y], bb[FLUX], bb[SHARP], bb[SAT]))
        pos[~m] = pred[~m]
        lpos = lpred
        # Unmatched blobs: re-acquire lost tracks before starting new ones.
        free = np.nonzero(mb < 0)[0]
        if len(free) and len(lpos):
            gate = np.minimum(GATE_PX + REACQUIRE_PX_PER_S * (fr.t - llast), REACQUIRE_MAX_PX)
            lt, lb = _greedy_match(lpos, bxy[free], gate)
            back = np.nonzero(lt >= 0)[0]
            if len(back):
                jj = free[lt[back]]
                pos = np.vstack([pos, bxy[jj]])
                vel = np.vstack([vel, lvel[back]])
                last = np.concatenate([last, np.full(len(back), fr.t)])
                ids = np.concatenate([ids, lids[back]])
                for i, j in zip(back, jj):
                    bb = b[j]
                    rows.append((lids[i], fi, bb[X], bb[Y], bb[FLUX], bb[SHARP], bb[SAT]))
                keep = np.ones(len(lpos), bool)
                keep[back] = False
                lpos, lvel, llast, lids = lpos[keep], lvel[keep], llast[keep], lids[keep]
                taken = np.zeros(len(bxy), bool)
                taken[jj] = True
                free = np.array([f for f in free if not taken[f]], int)
        # Live tracks unseen too long become lost; lost tracks expire.
        stale = (fr.t - last) > MAX_COAST_S
        if stale.any():
            lpos = np.vstack([lpos, pos[stale]])
            lvel = np.vstack([lvel, vel[stale]])
            llast = np.concatenate([llast, last[stale]])
            lids = np.concatenate([lids, ids[stale]])
            pos, vel, last, ids = pos[~stale], vel[~stale], last[~stale], ids[~stale]
        alive = (fr.t - llast) <= MAX_LOST_S
        lpos, lvel, llast, lids = lpos[alive], lvel[alive], llast[alive], lids[alive]
        if len(free):
            pos = np.vstack([pos, bxy[free]])
            vel = np.vstack([vel, np.zeros((len(free), 2))])
            last = np.concatenate([last, np.full(len(free), fr.t)])
            new_ids = np.arange(next_id, next_id + len(free))
            next_id += len(free)
            ids = np.concatenate([ids, new_ids])
            for k, jj in enumerate(free):
                bb = b[jj]
                rows.append((new_ids[k], fi, bb[X], bb[Y], bb[FLUX], bb[SHARP], bb[SAT]))
        prev = fr
    arr = np.array(rows, float) if rows else np.zeros((0, 7))
    return TrackSet(arr, next_id, shifts)
