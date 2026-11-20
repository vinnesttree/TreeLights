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
    ix, iy = np.unravel_index(np.argmax(Hs2), Hs2.shape)
    c = np.array([edges[ix] + 1.0, edges[iy] + 1.0])
    near = np.abs(d - c).max(axis=1) <= 3.0
    if not near.any():
        return np.zeros(2), 0
    return d[near].mean(axis=0), int(near.sum())


def track(frames: list[Frame], K: np.ndarray | None = None, use_gyro: bool = True) -> TrackSet:
    """frames must be sorted by time."""
    pos = np.zeros((0, 2))
    vel = np.zeros((0, 2))
    last = np.zeros(0)
    ids = np.zeros(0, int)
    next_id = 0
    rows = []
    shifts = np.zeros((len(frames), 2))
    prev = None
    for fi, fr in enumerate(frames):
        b = fr.blobs
        bxy = b[:, [X, Y]].astype(float) if len(b) else np.zeros((0, 2))
        gyro = use_gyro and K is not None and fr.R is not None and prev is not None and prev.R is not None
        if prev is not None and len(pos):
            dt = fr.t - prev.t
            base = apply_h(rotation_homography(prev.R, fr.R, K), pos) if gyro else pos.copy()
            pred = base + vel * dt
            s, votes = _vote_shift(pred, bxy, VOTE_GATE_GYRO if gyro else VOTE_GATE_NOGYRO)
            if votes >= 3:
                pred = pred + s
                base = base + s
                shifts[fi] = s
        else:
            pred = pos.copy()
            base = pos.copy()
            dt = 0.0
        matched_t = np.full(len(pos), -1)
        matched_b = np.full(len(bxy), -1)
        if len(pos) and len(bxy):
            tree = cKDTree(bxy)
            dist, j = tree.query(pred, k=2, distance_upper_bound=GATE_PX)
            cand = []
            for i in range(len(pred)):
                for kk in range(2):
                    if np.isfinite(dist[i, kk]):
                        cand.append((dist[i, kk], i, int(j[i, kk])))
            cand.sort()
            for _d, i, jj in cand:
                if matched_t[i] < 0 and matched_b[jj] < 0:
                    matched_t[i] = jj
                    matched_b[jj] = i
        # Update matched tracks.
        m = matched_t >= 0
        if m.any():
            new = bxy[matched_t[m]]
            if dt > 0:
                vel[m] = (1 - VEL_ALPHA) * vel[m] + VEL_ALPHA * (new - base[m]) / dt
            pos[m] = new
            last[m] = fr.t
            for i in np.nonzero(m)[0]:
                bb = b[matched_t[i]]
                rows.append((ids[i], fi, bb[X], bb[Y], bb[FLUX], bb[SHARP], bb[SAT]))
        pos[~m] = pred[~m]
        # Drop stale tracks.
        alive = (fr.t - last) <= MAX_COAST_S
        pos, vel, last, ids = pos[alive], vel[alive], last[alive], ids[alive]
        # New tracks for unmatched blobs.
        nb = np.nonzero(matched_b < 0)[0]
        if len(nb):
            pos = np.vstack([pos, bxy[nb]])
            vel = np.vstack([vel, np.zeros((len(nb), 2))])
            last = np.concatenate([last, np.full(len(nb), fr.t)])
            new_ids = np.arange(next_id, next_id + len(nb))
            next_id += len(nb)
            ids = np.concatenate([ids, new_ids])
            for k, jj in enumerate(nb):
                bb = b[jj]
                rows.append((new_ids[k], fi, bb[X], bb[Y], bb[FLUX], bb[SHARP], bb[SAT]))
        prev = fr
    arr = np.array(rows, float) if rows else np.zeros((0, 7))
    return TrackSet(arr, next_id, shifts)
