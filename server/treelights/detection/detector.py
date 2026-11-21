"""Single-LED frame detection (architecture §8.1).

All images here are RGB float32 (H, W, 3). Steps:
1. signed difference to the segment's dark reference;
2. projection onto the expected color (matched filter), off-color part kept;
3. robust noise estimate (MAD) and threshold k * sigma with an absolute floor;
4. peak finding (local maxima) so an LED and a nearby reflection stay apart;
5. core-only centroid: pixels above 50% of the peak, so glow through needles
   doesn't pull the position;
6. branches: each blob is classified as a point source (the LED itself, or a
   needle glint) or diffuse (light scattered by the branches around the LED,
   often all that is seen of a hidden LED). Diffuse blobs carry a larger
   position uncertainty. Dim blobs next to a much brighter point source are
   reflections of it off nearby branches and are dropped;
7. frame sanity: outside candidate regions the difference must look like noise.
   A candidate's whole glow region is excluded, since lit branches around an
   LED can spread well beyond its core.
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
MAX_GLOW_FRACTION = 0.002  # a candidate's glow region larger than this (of the image) is a scene change
POINT_AREA = 6.0  # px above half maximum for a sharp point source
POINT_MIN_SHARPNESS = 0.5  # below this a blob is diffuse (scattered light), not a point source
REFLECTION_RADIUS_PX = 40.0
REFLECTION_RATIO = 0.35  # a point blob this much dimmer than a nearby brighter one is its reflection
DIFFUSE_WEIGHT = 0.4  # diffuse blobs rank below point sources of the same brightness


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
    score: float = 0.0  # relative to the frame's strongest blob (1 = strongest)
    kind: str = "point"  # "point" (LED or glint) | "diffuse" (light scattered by branches)
    sigma_px: float = 0.5  # localization uncertainty

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
    # Half maximum above the local background: an LED core often sits on a pedestal of
    # light scattered by the branches around it (or, with many LEDs lit, by its neighbours);
    # measured from zero, the pedestal would make a sharp core look diffuse.
    yy0, xx0 = np.mgrid[ya:yb, xa:xb]
    ring = np.hypot(xx0 - x, yy0 - y) >= CORE_WIN - 1
    bg = float(np.clip(np.percentile(win[ring], 20), 0.0, 0.8 * peak)) if ring.any() else 0.0
    half = bg + 0.5 * (peak - bg)
    core = win >= half
    wts = np.where(core, win - half, 0.0)
    tot = float(wts.sum())
    yy, xx = yy0, xx0
    cx = float((wts * xx).sum() / tot) if tot > 0 else float(x)
    cy = float((wts * yy).sum() / tot) if tot > 0 else float(y)
    area = float(core.sum())
    sharp = float(min(1.0, np.sqrt(POINT_AREA / max(area, 1.0))))
    color = diff[ya:yb, xa:xb][core].mean(axis=0).tolist() if diff is not None and core.any() else [0.0, 0.0, 0.0]
    sat = bool(frame is not None and (frame[ya:yb, xa:xb][core] >= 250).any())
    snr = peak / max(sigma, 1e-3)
    kind = "point" if (sharp >= POINT_MIN_SHARPNESS or sat) else "diffuse"
    # Point sources localize to a fraction of a pixel; for scattered light the LED can be
    # anywhere under the glow, so the uncertainty is about the glow's radius.
    sig_px = 0.4 / max(sharp, 0.3) if kind == "point" else max(2.0, float(np.sqrt(area / np.pi)))
    return Candidate(cx, cy, peak, snr, sharp, sat, [float(c) for c in color], kind=kind, sigma_px=round(sig_px, 2))


def rank_candidates(cands: list[Candidate]) -> list[Candidate]:
    """Order by brightness (not capped SNR, so a bright LED beats a sharper but dimmer
    glint), diffuse blobs down-weighted; drop dim point blobs next to a much brighter
    point source (its reflections off nearby branches); normalize scores to the best."""
    def strength(c: Candidate) -> float:
        return c.peak * (1.0 if c.kind == "point" else DIFFUSE_WEIGHT)

    cands = sorted(cands, key=lambda c: -strength(c))
    kept: list[Candidate] = []
    for c in cands:
        reflection = any(
            k.kind == "point" and np.hypot(c.x - k.x, c.y - k.y) <= REFLECTION_RADIUS_PX
            and c.peak < REFLECTION_RATIO * k.peak for k in kept)
        if not reflection:
            kept.append(c)
    if kept:
        top = strength(kept[0])
        for c in kept:
            c.score = float(strength(c) / top)
    return kept


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
    all_cands = [measure_peak(Db, diff, frame, int(x), int(y), sigma) for x, y in peaks[:12]]
    # Frame sanity: changes away from the candidates must be noise. The whole connected
    # glow region around a candidate is excluded (lit branches spread beyond the core),
    # unless it is so large that something other than one LED changed.
    above = (Db > thresh).astype(np.uint8)
    n_lab, labels, stats, _ = cv2.connectedComponentsWithStats(above, connectivity=8)
    max_glow = MAX_GLOW_FRACTION * above.size
    for c in all_cands:
        xi = int(np.clip(round(c.x), 0, above.shape[1] - 1))
        yi = int(np.clip(round(c.y), 0, above.shape[0] - 1))
        lab = labels[yi, xi]
        if lab > 0 and stats[lab, cv2.CC_STAT_AREA] <= max_glow:
            above[labels == lab] = 0
        cv2.circle(above, (xi, yi), SANITY_EXCLUDE_PX, 0, -1)
    frac = float(above.mean())
    cands = rank_candidates(all_cands)
    if frac > SANITY_MAX_FRACTION:
        return Detection(cands[:MAX_CANDIDATES], False, "scene_changed", sigma, frac)
    cands = [c for c in cands if c.score >= 0.15]
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
