"""Finding the pattern's timing in a stream of frames (in-band sync).

Each frame's total LED flux, normalized within a sliding window (framing changes
as the user walks), is matched against the pattern's expected overall
brightness per slot (sync bright/off slots, ~(1+DIM)/2 in code slots). A global
search finds the offset; a per-cycle refinement with a robust line fit absorbs
clock drift. Latency (WLED, camera pipeline) is included automatically because
the timing is read from the images themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .pattern import CYCLE_SLOTS, CYCLE_T, T_SLOT, template

# Normalisation window (each side): a whole cycle, so a window nearly always holds
# frames from the short sync slots even when frames are dropped (with +-4 s, a gap
# over the sync slots left the window's darkest frames dim code frames, and the fit
# failed).
WINDOW_S = 7.2
MAX_DRIFT = 2e-4  # s/s
EDGE = 0.15  # fraction of a slot at each end that may mix with its neighbour (transition, exposure)
RAMP = 0.06  # half-width of the modelled transition, as a fraction of a slot (~12 ms: exposure)


def normalize(t: np.ndarray, flux: np.ndarray) -> np.ndarray:
    out = np.zeros_like(flux, dtype=float)
    lo_i = np.searchsorted(t, t - WINDOW_S)
    hi_i = np.searchsorted(t, t + WINDOW_S)
    for k in range(len(t)):
        w = flux[lo_i[k] : hi_i[k]]
        # Sync "off" and "all bright" slots are each only ~1/18 of frames: use the extremes.
        lo, hi = np.percentile(w, 1), np.percentile(w, 99)
        out[k] = (flux[k] - lo) / (hi - lo) if hi - lo > 1e-6 else 0.5
    return np.clip(out, -0.5, 1.5)


def _cost(t: np.ndarray, r: np.ndarray, offset: np.ndarray) -> np.ndarray:
    """Mean squared mismatch for each candidate offset (vectorized over offsets).

    Sync slots are fixed at 1 (all bright) and 0 (off); the level of code slots is
    fitted per offset, because bright LEDs saturating in the camera lose flux and
    move it away from the nominal (1 + DIM) / 2. Near a slot boundary the prediction
    ramps linearly between the two slots (exposure and rolling shutter mix them), so
    the cost keeps changing as the offset moves by a few ms: ignoring those frames
    instead leaves a flat-bottomed cost and an arbitrary offset within ~EDGE of a slot."""
    u = (t[None, :] - offset[:, None]) / T_SLOT
    k = np.floor(u).astype(int)
    ph = u - k
    core = (ph > EDGE) & (ph < 1 - EDGE)
    here = template(k)
    code = (here > 0.01) & (here < 0.99)
    wc = core * code
    level = (wc * r[None, :]).sum(axis=1) / np.maximum(wc.sum(axis=1), 1)
    level = np.clip(level, 0.3, 0.95)[:, None]

    def lv(kk):
        p = template(kk)
        return np.where((p > 0.01) & (p < 0.99), level, p)

    a = lv(k)
    # Blend weight of the neighbouring slot: 0.5 at the boundary, 0 beyond RAMP.
    w_prev = np.clip(0.5 - ph / (2 * RAMP), 0, 0.5)
    w_next = np.clip(0.5 - (1 - ph) / (2 * RAMP), 0, 0.5)
    pred = a * (1 - w_prev - w_next) + lv(k - 1) * w_prev + lv(k + 1) * w_next
    return ((r[None, :] - pred) ** 2).mean(axis=1)


@dataclass
class Timing:
    a: float  # offset(t) = a + b * t  (pattern_time = t - offset(t))
    b: float
    contrast: float  # best cost / typical cost: low = clear lock
    cycles: int
    ok: bool

    def pattern_time(self, t: np.ndarray | float) -> np.ndarray:
        t = np.asarray(t, float)
        return t - (self.a + self.b * t)

    def source_time(self, pt: np.ndarray | float) -> np.ndarray:
        pt = np.asarray(pt, float)
        return (pt + self.a) / (1 - self.b)

    def slot_phase(self, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        u = self.pattern_time(t) / T_SLOT
        k = np.floor(u).astype(int)
        return k, u - k


def estimate(t: np.ndarray, flux: np.ndarray) -> Timing:
    t = np.asarray(t, float)
    flux = np.asarray(flux, float)
    if len(t) < 20 or t[-1] - t[0] < CYCLE_T:
        return Timing(0.0, 0.0, 1.0, 0, False)
    r = normalize(t, flux)
    grid = np.arange(0.0, CYCLE_T, 0.005)
    cost = np.concatenate([_cost(t, r, g) for g in np.array_split(grid, max(1, len(grid) // 60))])
    best = float(grid[np.argmin(cost)])
    typical = max(float(np.median(cost)), 1e-9)
    contrast = float(cost.min() / typical)
    # Per-cycle refinement (drift, latency changes).
    pts = []
    c0 = int(np.floor((t[0] - best) / CYCLE_T))
    c1 = int(np.floor((t[-1] - best) / CYCLE_T))
    for c in range(c0, c1 + 1):
        sel = (t >= best + c * CYCLE_T) & (t < best + (c + 1) * CYCLE_T)
        if sel.sum() < CYCLE_SLOTS:  # need roughly a frame per slot
            continue
        local = best + np.arange(-0.08, 0.0801, 0.005)
        lc = _cost(t[sel], r[sel], local)
        if lc.min() < 0.5 * typical:
            pts.append((float(t[sel].mean()), float(local[np.argmin(lc)])))
    a, b = best, 0.0
    if pts:
        P = np.array(pts)
        a = float(np.median(P[:, 1]))
        if len(P) >= 4:
            # Clock drift between phone and server is tiny (crystal clocks: tens of ppm);
            # a larger slope would be fitting noise.
            b_fit, a_fit = np.polyfit(P[:, 0], P[:, 1], 1)
            if abs(b_fit) <= MAX_DRIFT:
                resid = np.abs(P[:, 1] - (a_fit + b_fit * P[:, 0]))
                keep = resid < 0.03
                if keep.sum() >= 4:
                    b_fit, a_fit = np.polyfit(P[keep, 0], P[keep, 1], 1)
                    if abs(b_fit) <= MAX_DRIFT:
                        a, b = float(a_fit), float(b_fit)
    ok = contrast < 0.4 and len(pts) >= 1
    return Timing(float(a), float(b), contrast, len(pts), bool(ok))
