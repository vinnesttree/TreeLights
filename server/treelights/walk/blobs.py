"""Per-frame blob detection for walk-around capture.

The camera moves, so there is no dark reference to subtract. Instead the local
background is removed with a wide blur (LEDs are small; room lighting and lit
walls vary slowly across the image), and point-like peaks above a robust noise
threshold are measured.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..detection.detector import robust_sigma

BG_SIGMA = 7.0
K_SIGMA = 6.0
FLOOR = 8.0
MAX_BLOBS = 3000
FLUX_R = 4  # the whole 9x9 window: a big, saturated LED (close up, high resolution) differs between bright
# and dim mostly in its extent, which a 5x5 core misses (100 vs 120 of 150 decoded in simulation)

# Columns of a blob array.
X, Y, FLUX, PEAK, SHARP, SAT = range(6)


def channel(img_rgb: np.ndarray) -> np.ndarray:
    """The pattern is green; the green channel carries most of its light."""
    return img_rgb[..., 1].astype(np.float32)


def detect_blobs(img_rgb: np.ndarray) -> np.ndarray:
    """(k, 6) float32: x, y, flux, peak, sharpness, saturated."""
    g = channel(img_rgb)
    D = g - cv2.GaussianBlur(g, (0, 0), BG_SIGMA)
    Db = cv2.GaussianBlur(D, (0, 0), 1.0)
    sigma = max(robust_sigma(Db), 0.3)
    peaks = fast_peaks(Db, max(K_SIGMA * sigma, FLOOR), nms=3, limit=MAX_BLOBS)
    if not len(peaks):
        return np.zeros((0, 6), np.float32)
    return measure_all(Db, g, peaks.astype(int))


def fast_peaks(D: np.ndarray, thresh: float, nms: int = 3, limit: int = MAX_BLOBS) -> np.ndarray:
    """Local maxima above thresh, one per plateau (via connected components),
    strongest first, as (k, 2) int (x, y)."""
    k = 2 * nms + 1
    dil = cv2.dilate(D, np.ones((k, k), np.uint8))
    mask = ((D >= dil) & (D > thresh)).astype(np.uint8)
    n, _labels, _stats, cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return np.zeros((0, 2), int)
    pts = np.round(cent[1:]).astype(int)
    pts[:, 0] = np.clip(pts[:, 0], 0, D.shape[1] - 1)
    pts[:, 1] = np.clip(pts[:, 1], 0, D.shape[0] - 1)
    order = np.argsort(-D[pts[:, 1], pts[:, 0]])[:limit]
    return pts[order]


_R = 4
_dy, _dx = np.mgrid[-_R : _R + 1, -_R : _R + 1]
_RING = np.hypot(_dx, _dy) >= _R - 0.5


def measure_all(Db: np.ndarray, g: np.ndarray, peaks: np.ndarray) -> np.ndarray:
    """Vectorized core-centroid / flux / sharpness for all peaks of a frame (same
    definitions as detection.detector.measure_peak: the core is the part above half
    the peak height over the local background, so a glow pedestal doesn't widen it)."""
    P = np.pad(Db, _R)
    xs, ys = peaks[:, 0], peaks[:, 1]
    W = P[ys[:, None, None] + _R + _dy[None], xs[:, None, None] + _R + _dx[None]]  # (k, 9, 9)
    peak = Db[ys, xs]
    bg = np.percentile(W[:, _RING], 20, axis=1)
    bg = np.clip(bg, 0.0, 0.8 * peak)
    half = bg + 0.5 * (peak - bg)
    core = W >= half[:, None, None]
    wts = np.where(core, W - half[:, None, None], 0.0)
    tot = wts.sum(axis=(1, 2))
    safe = np.where(tot > 0, tot, 1.0)
    cx = xs + np.where(tot > 0, (wts * _dx).sum(axis=(1, 2)) / safe, 0.0)
    cy = ys + np.where(tot > 0, (wts * _dy).sum(axis=(1, 2)) / safe, 0.0)
    area = core.sum(axis=(1, 2)).astype(np.float32)
    sharp = np.minimum(1.0, np.sqrt(6.0 / np.maximum(area, 1.0)))
    flux = np.clip(W[:, _R - FLUX_R : _R + FLUX_R + 1, _R - FLUX_R : _R + FLUX_R + 1], 0, None).sum(axis=(1, 2))
    Gp = np.pad(g, 1)
    sat = np.max([Gp[ys + 1 + a, xs + 1 + b] for a in (-1, 0, 1) for b in (-1, 0, 1)], axis=0) >= 250
    return np.stack([cx, cy, flux, peak, sharp, sat.astype(np.float32)], axis=1).astype(np.float32)
