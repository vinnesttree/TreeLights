"""Reference constellation: detect camera or tree movement (architecture §7.5).

A fixed set of LEDs is lit together; their blob positions form a point set.
Two captures match when the blobs line up within ~1.5 px.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from .detector import Reference, find_peaks, project_color, robust_sigma

import cv2

MOVE_PX = 1.5
MATCH_RADIUS_PX = 12.0
MIN_MATCH_FRACTION = 0.6


def constellation_points(frame: np.ndarray, ref: Reference) -> np.ndarray:
    diff = frame.astype(np.float32) - ref.image
    D = cv2.GaussianBlur(project_color(diff, None), (0, 0), 1.0)
    sigma = max(robust_sigma(D), 0.3)
    pk = find_peaks(D, max(8 * sigma, 6.0), nms=3)
    if not len(pk):
        return np.zeros((0, 2))
    # Sub-pixel refinement with a small centroid.
    out = []
    for x, y in pk[:200]:
        x0, x1 = max(0, x - 3), min(D.shape[1], x + 4)
        y0, y1 = max(0, y - 3), min(D.shape[0], y + 4)
        w = np.clip(D[y0:y1, x0:x1] - 0.5 * D[y, x], 0, None)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        s = w.sum()
        out.append([(w * xx).sum() / s, (w * yy).sum() / s] if s > 0 else [x, y])
    return np.array(out, float)


@dataclass
class ConstellationMatch:
    moved: bool
    median_shift: float
    matched_fraction: float
    reason: str = ""


def compare(ref_pts: np.ndarray, pts: np.ndarray) -> ConstellationMatch:
    if len(ref_pts) < 3:
        return ConstellationMatch(False, 0.0, 1.0, "too_few_points")
    if len(pts) == 0:
        return ConstellationMatch(True, float("inf"), 0.0, "nothing_visible")
    tree = cKDTree(pts)
    d, j = tree.query(ref_pts, distance_upper_bound=MATCH_RADIUS_PX)
    ok = np.isfinite(d)
    frac = float(ok.mean())
    if frac < MIN_MATCH_FRACTION:
        return ConstellationMatch(True, float("inf"), frac, "pattern_changed")
    disp = pts[j[ok]] - ref_pts[ok]
    # Median displacement handles translation; residual spread handles rotation/zoom.
    med = np.median(disp, axis=0)
    shift = float(np.hypot(*med))
    spread = float(np.median(np.linalg.norm(disp - med, axis=1)))
    moved = shift > MOVE_PX or spread > MOVE_PX
    return ConstellationMatch(moved, max(shift, spread), frac)
