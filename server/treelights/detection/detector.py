"""Single-LED frame detection (architecture §8.1).

All images here are RGB float32 (H, W, 3). Steps:
1. signed difference to the segment's dark reference;
2. projection onto the expected color (matched filter), off-color part kept;
3. robust noise estimate (MAD) and threshold k * sigma with an absolute floor;
4. peak finding (local maxima) so an LED and a nearby reflection stay apart;
5. core-only centroid: pixels above 50% of the peak, so glow through needles
   doesn't pull the position;
6. frame sanity: outside candidate regions the difference must look like noise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

K_SIGMA = 7.0
ABS_FLOOR = 10.0  # DN; compression noise (JPEG/H.264) is heavy-tailed, so k*sigma alone isn't enough
PEAK_NMS_PX = 4
CORE_WIN = 6
MAX_CANDIDATES = 3
SANITY_EXCLUDE_PX = 14
SANITY_MAX_FRACTION = 0.0015
POINT_AREA = 6.0  # px above half maximum for a sharp point source


def decode_image(data: bytes) -> np.ndarray:
    """JPEG/PNG bytes -> RGB uint8."""
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise ValueError("undecodable image")
    return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)


@dataclass
class Reference:
    image: np.ndarray  # float32 (H, W, 3), median of dark frames
    sigma: np.ndarray  # (3,) per-channel robust noise between dark frames

    @property
    def shape(self) -> tuple[int, int]:
        return self.image.shape[:2]


def build_reference(frames: list[np.ndarray]) -> Reference:
    stack = np.stack([f.astype(np.float32) for f in frames])
    med = np.median(stack, axis=0) if len(frames) > 1 else stack[0]
    if len(frames) > 1:
        dev = (stack[0] - stack[1])[::3, ::3].reshape(-1, 3) / np.sqrt(2)
        sigma = 1.4826 * np.median(np.abs(dev - np.median(dev, axis=0)), axis=0)
    else:
        sigma = np.full(3, 2.0, np.float32)
    return Reference(med.astype(np.float32), np.maximum(sigma, 0.5).astype(np.float32))


@dataclass
class Candidate:
    x: float
    y: float
    peak: float
    snr: float
    sharpness: float  # 1 = point-like, lower = diffuse
    saturated: bool
    color: list[float]  # mean signed difference RGB in the core
    score: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items()}


@dataclass
class Detection:
    candidates: list[Candidate]
    valid: bool
    reason: str = ""
    noise: float = 0.0
    changed_fraction: float = 0.0
    extra: dict = field(default_factory=dict)


def robust_sigma(a: np.ndarray) -> float:
    s = a[::4, ::4].ravel() if a.ndim == 2 else a.ravel()
    med = np.median(s)
    return float(1.4826 * np.median(np.abs(s - med)))


def find_peaks(D: np.ndarray, thresh: float, nms: int = PEAK_NMS_PX, limit: int = 2000) -> np.ndarray:
    """Local maxima above thresh, strongest first, as (k, 2) int array of (x, y)."""
    k = 2 * nms + 1
    dil = cv2.dilate(D, np.ones((k, k), np.uint8))
    mask = (D >= dil) & (D > thresh)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return np.zeros((0, 2), int)
    order = np.argsort(-D[ys, xs])[:limit]
    pts = np.stack([xs[order], ys[order]], axis=1)
    # Plateaus produce several equal maxima; keep the first of each cluster.
    keep: list[np.ndarray] = []
    for p in pts:
        if all(abs(p[0] - q[0]) > nms or abs(p[1] - q[1]) > nms for q in keep):
            keep.append(p)
    return np.array(keep, int)


def measure_peak(D: np.ndarray, diff: np.ndarray | None, frame: np.ndarray | None, x: int, y: int, sigma: float) -> Candidate:
    h, w = D.shape
    xa, xb = max(0, x - CORE_WIN), min(w, x + CORE_WIN + 1)
    ya, yb = max(0, y - CORE_WIN), min(h, y + CORE_WIN + 1)
    win = D[ya:yb, xa:xb]
    peak = float(D[y, x])
    core = win >= 0.5 * peak
    wts = np.where(core, win - 0.5 * peak, 0.0)
    tot = float(wts.sum())
    yy, xx = np.mgrid[ya:yb, xa:xb]
    cx = float((wts * xx).sum() / tot) if tot > 0 else float(x)
    cy = float((wts * yy).sum() / tot) if tot > 0 else float(y)
    area = float(core.sum())
    sharp = float(min(1.0, np.sqrt(POINT_AREA / max(area, 1.0))))
    color = diff[ya:yb, xa:xb][core].mean(axis=0).tolist() if diff is not None and core.any() else [0.0, 0.0, 0.0]
    sat = bool(frame is not None and (frame[ya:yb, xa:xb][core] >= 250).any())
    snr = peak / max(sigma, 1e-3)
    return Candidate(cx, cy, peak, snr, sharp, sat, [float(c) for c in color])


def project_color(diff: np.ndarray, color: np.ndarray | None) -> np.ndarray:
    if color is None:
        return diff.mean(axis=2)
    c = np.asarray(color, np.float32)
    c = c / max(float(np.linalg.norm(c)), 1e-6)
    return diff @ c


def detect_single(frame: np.ndarray, ref: Reference, expected_color: np.ndarray | None = None) -> Detection:
    """Find up to MAX_CANDIDATES LED candidates in a frame where one LED is lit."""
    f32 = frame.astype(np.float32)
    diff = f32 - ref.image
    D = project_color(diff, expected_color)
    Db = cv2.GaussianBlur(D, (0, 0), 1.0)
    sigma = max(robust_sigma(Db), 0.3)
    med = float(np.median(Db[::4, ::4]))
    if abs(med) > 4 * sigma + 3:
        return Detection([], False, "exposure_jump", sigma)
    thresh = max(K_SIGMA * sigma, ABS_FLOOR) + med
    peaks = find_peaks(Db, thresh)
    cands = [measure_peak(Db, diff, frame, int(x), int(y), sigma) for x, y in peaks[:12]]
    for c in cands:
        c.score = float(min(1.0, c.snr / 40.0) * (0.3 + 0.7 * c.sharpness))
    cands.sort(key=lambda c: -c.score)
    # Frame sanity: changes away from the candidates must be noise.
    above = (Db > thresh).astype(np.uint8)
    for c in cands[:12]:
        cv2.circle(above, (int(round(c.x)), int(round(c.y))), SANITY_EXCLUDE_PX, 0, -1)
    frac = float(above.mean())
    if frac > SANITY_MAX_FRACTION:
        return Detection(cands[:MAX_CANDIDATES], False, "scene_changed", sigma, frac)
    if cands:
        best = cands[0].score
        cands = [c for c in cands if c.score >= 0.15 * best]
    return Detection(cands[:MAX_CANDIDATES], True, "", sigma, frac)


def color_class(color: np.ndarray, basis: np.ndarray) -> tuple[int, float]:
    """Classify a blob's color against learned responses (rows of basis, one per
    commanded color). Returns (class, margin) where margin is the cosine gap
    between the best and second-best class."""
    v = np.asarray(color, np.float64)
    nv = np.linalg.norm(v)
    if nv < 1e-6:
        return -1, 0.0
    B = basis / np.linalg.norm(basis, axis=1, keepdims=True)
    cos = B @ (v / nv)
    order = np.argsort(-cos)
    return int(order[0]), float(cos[order[0]] - cos[order[1]])
