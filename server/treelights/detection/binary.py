"""Binary-coded capture (architecture §7.3 step 5, §8.2).

Each LED index is an extended Hamming(16,11) codeword: minimum distance 4, so
any 1-3 bit errors produce an invalid word. Invalid words are rejected, never
corrected. Each bit is shown as a pattern frame and its complement, so every
LED is lit in exactly one frame of each pair and bits are decided by
comparing the LED's own brightness in the two frames.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np
from scipy.spatial import cKDTree

from .detector import Reference, find_peaks, measure_peak, robust_sigma

N_BITS = 16
DATA_BITS = 11
_PARITY_POS = (1, 2, 4, 8)  # 1-based positions in the 15-bit Hamming code
_DATA_POS = [p for p in range(1, 16) if p not in _PARITY_POS]
RELIABILITY_MIN = 0.3
PAIR_MIN_FRACTION = 0.6
MAX_SHIFT_PX = 3.0
MATCH_PX = 5.0
MIN_MATCHED = 0.7


def encode(value: int) -> np.ndarray:
    """11-bit value -> 16 bits (index 0 = overall parity, 1..15 = Hamming positions)."""
    if not 0 <= value < 2**DATA_BITS:
        raise ValueError("value out of range")
    code = np.zeros(16, np.uint8)
    for k, pos in enumerate(_DATA_POS):
        code[pos] = (value >> k) & 1
    for p in _PARITY_POS:
        code[p] = np.bitwise_xor.reduce([code[i] for i in range(1, 16) if i & p and i != p])
    code[0] = np.bitwise_xor.reduce(code[1:])
    return code


def decode(bits: np.ndarray) -> int | None:
    """Return the value for a valid codeword, None for anything else."""
    bits = np.asarray(bits, np.uint8)
    syndrome = 0
    for i in range(1, 16):
        if bits[i]:
            syndrome ^= i
    if syndrome != 0 or int(bits.sum()) % 2 != 0:
        return None
    return sum(int(bits[pos]) << k for k, pos in enumerate(_DATA_POS))


# Indices are scrambled with an invertible map mod 2048 before encoding, so that
# every bit plane is lit for roughly half the LEDs. Without it, for 600 LEDs the
# high data bits would be 0 for every LED and their pattern frames would be dark.
_MULT, _ADD = 1031, 517
_MULT_INV = pow(_MULT, -1, 2**DATA_BITS)


def index_to_value(i: int) -> int:
    return (i * _MULT + _ADD) % 2**DATA_BITS


def value_to_index(v: int) -> int:
    return ((v - _ADD) * _MULT_INV) % 2**DATA_BITS


def code_table(n: int) -> np.ndarray:
    """(n, 16) codewords for LED indices 0..n-1 (read-only: the table is shared)."""
    return _code_table(n)


@lru_cache(maxsize=8)
def _code_table(n: int) -> np.ndarray:
    # Decoding asks for the table once per track: building it each time was most of the
    # decoding time of a 600-LED walk.
    t = np.stack([encode(index_to_value(i)) for i in range(n)])
    t.setflags(write=False)
    return t


def pattern_frames(n: int, rgb: tuple[int, int, int]) -> list[np.ndarray]:
    """32 frames: for each bit, the pattern then its complement."""
    codes = code_table(n)
    color = np.array(rgb, np.uint8)
    frames = []
    for b in range(N_BITS):
        on = codes[:, b] == 1
        for mask in (on, ~on):
            f = np.zeros((n, 3), np.uint8)
            f[mask] = color
            frames.append(f)
    return frames


def _gray_diff(frame: np.ndarray, ref: Reference) -> np.ndarray:
    d = (frame.astype(np.float32) - ref.image).mean(axis=2)
    return cv2.GaussianBlur(d, (0, 0), 1.0)


def register(img: np.ndarray, ref_peaks: np.ndarray, sigma: float) -> tuple[np.ndarray, float]:
    """Translation of a pattern frame relative to the all-on reference.

    Each pattern frame shows a subset of the reference peaks, so its own peaks
    are matched to the nearest reference peak and the median displacement is
    the shift. Returns (shift, fraction of frame peaks that matched).
    Point matching is used instead of phase correlation, which can lock onto a
    wrong offset with sparse, repetitive point patterns.
    """
    pk = find_peaks(img, max(7 * sigma, 5.0), nms=2).astype(float)
    if len(pk) < 3 or len(ref_peaks) < 3:
        return np.zeros(2), 0.0
    d, j = cKDTree(ref_peaks).query(pk, distance_upper_bound=MATCH_PX)
    ok = np.isfinite(d)
    if ok.sum() < 3:
        return np.array([np.inf, np.inf]), float(ok.mean())
    shift = np.median(pk[ok] - ref_peaks[j[ok]], axis=0)
    return shift, float(ok.mean())


def aperture_sum(D: np.ndarray, pts: np.ndarray, r: int = 1) -> np.ndarray:
    """Sum of D in a (2r+1)^2 window around each (x, y) (rounded)."""
    h, w = D.shape
    out = np.zeros(len(pts))
    P = np.pad(D, r)
    xi = np.clip(np.round(pts[:, 0]).astype(int), 0, w - 1)
    yi = np.clip(np.round(pts[:, 1]).astype(int), 0, h - 1)
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            out += P[yi + dy, xi + dx]
    return out


@dataclass
class PairCheck:
    ok: bool
    shift: np.ndarray
    fraction_exclusive: float
    reason: str = ""


@dataclass
class BinaryDecoder:
    """Incremental decoder: feed the all-on reference, then frame pairs."""

    ref: Reference
    n_leds: int

    def set_allon(self, frames: list[np.ndarray]) -> int:
        D = np.mean([_gray_diff(f, self.ref) for f in frames], axis=0)
        self.allon = D
        sigma = max(robust_sigma(D), 0.3)
        self.sigma = sigma
        self.peaks = find_peaks(D, max(7 * sigma, 5.0), nms=2).astype(float)
        self.peak_snr = D[self.peaks[:, 1].astype(int), self.peaks[:, 0].astype(int)] / sigma if len(self.peaks) else np.zeros(0)
        self.P = np.zeros((len(self.peaks), N_BITS))
        self.C = np.zeros((len(self.peaks), N_BITS))
        self.done = np.zeros(N_BITS, bool)
        return len(self.peaks)

    def add_pair(self, bit: int, pattern: np.ndarray, complement: np.ndarray) -> PairCheck:
        Dp, Dc = _gray_diff(pattern, self.ref), _gray_diff(complement, self.ref)
        # Registration against the all-on image (each frame shows a subset of its peaks).
        sp, mp = register(Dp, self.peaks, self.sigma)
        sc, mc = register(Dc, self.peaks, self.sigma)
        regs = [(s, m) for s, m in ((sp, mp), (sc, mc)) if m > 0]  # m == 0: too few lit peaks to register
        if not regs:
            return PairCheck(False, np.zeros(2), 0.0, "nothing_visible")
        for s, m in regs:
            if not np.all(np.isfinite(s)) or np.hypot(*s) > MAX_SHIFT_PX or m < MIN_MATCHED:
                return PairCheck(False, s, m, "moved")
        if mp == 0:
            sp = sc
        if mc == 0:
            sc = sp
        p = aperture_sum(Dp, self.peaks + sp)
        c = aperture_sum(Dc, self.peaks + sc)
        rel = np.abs(p - c) / (np.abs(p) + np.abs(c) + 1e-6)
        frac = float((rel >= RELIABILITY_MIN).mean()) if len(rel) else 0.0
        if len(rel) and frac < PAIR_MIN_FRACTION:
            return PairCheck(False, (sp + sc) / 2, frac, "inconsistent")  # stale or mid-change
        self.P[:, bit], self.C[:, bit] = p, c
        self.done[bit] = True
        return PairCheck(True, (sp + sc) / 2, frac)

    def result(self) -> list[dict]:
        """Decoded peaks: [{index, x, y, reliability, snr}], valid codewords only."""
        if not self.done.all():
            raise RuntimeError("not all bit pairs captured")
        bits = (self.P > self.C).astype(np.uint8)
        rel = np.abs(self.P - self.C) / (np.abs(self.P) + np.abs(self.C) + 1e-6)
        out = []
        for k in range(len(self.peaks)):
            if rel[k].min() < RELIABILITY_MIN:
                continue  # any unreliable bit: merged LEDs, too dim, or glare
            v = decode(bits[k])
            if v is None:
                continue
            idx = value_to_index(v)
            if idx >= self.n_leds:
                continue
            # Point source or light scattered by branches? (Reflections off branches blink
            # with their LED and decode to the same index.)
            m = measure_peak(self.allon, None, None, int(self.peaks[k, 0]), int(self.peaks[k, 1]), self.sigma)
            out.append({"index": int(idx), "x": float(m.x), "y": float(m.y), "peak": float(m.peak),
                        "reliability": float(rel[k].min()), "snr": float(self.peak_snr[k]),
                        "kind": m.kind, "sigma_px": m.sigma_px, "sharpness": m.sharpness})
        return out
