"""Decoding LED identities from tracks.

For each track, detections in the middle of code slots are grouped by (cycle, bit):
the LED's flux in the bit's pattern slot is compared with its complement slot in
the same cycle (0.2 s apart, so distance and occlusion barely change). Every
cycle the track spans adds a vote per bit; cycles combine because the pattern
repeats identically. If the dim state of a pair wasn't detected (too faint) while
the track was seen in the neighbouring slots, that counts as a weaker vote.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .pattern import CYCLE_SLOTS, decode_bits, slot_info
from .timing import Timing
from .tracking import TrackSet

PHASE_LO, PHASE_HI = 0.2, 0.8
MISSING_DIM_STRENGTH = 0.4


@dataclass
class Identity:
    index: int
    votes: int  # complete (pattern, complement) pairs used
    weakest: float


def decode_tracks(ts: TrackSet, frame_t: np.ndarray, timing: Timing, n_leds: int, min_rows: int = 20) -> dict[int, Identity]:
    out: dict[int, Identity] = {}
    if not timing.ok or len(ts.rows) == 0:
        return out
    for tid, rows in ts.by_track().items():
        if len(rows) < min_rows:
            continue
        fi = rows[:, 1].astype(int)
        k, ph = timing.slot_phase(frame_t[fi])
        good = (ph > PHASE_LO) & (ph < PHASE_HI)
        slots_seen = set(k.tolist())  # any detection in a slot (edges included) means "tracked"
        flux: dict[tuple[int, int, bool], list[float]] = {}
        for kk, f in zip(k[good], rows[good, 4]):
            info = slot_info(int(kk))
            if info.kind != "bit":
                continue
            flux.setdefault((int(kk) // CYCLE_SLOTS, info.bit, info.complement), []).append(float(f))
        votes: dict[int, list[tuple[bool, float]]] = {}
        n_pairs = 0
        cycles = {c for c, _b, _cmp in flux}
        for c in cycles:
            for b in range(16):
                P = flux.get((c, b, False))
                C = flux.get((c, b, True))
                kP = c * CYCLE_SLOTS + 4 + 2 * b
                kC = kP + 1
                if P and C:
                    p, q = float(np.mean(P)), float(np.mean(C))
                    if p + q > 0:
                        votes.setdefault(b, []).append((p > q, abs(p - q) / (p + q)))
                        n_pairs += 1
                elif P and not C and (kC + 1) in slots_seen and kP in slots_seen:
                    votes.setdefault(b, []).append((True, MISSING_DIM_STRENGTH))
                elif C and not P and (kP - 1) in slots_seen and kC in slots_seen:
                    votes.setdefault(b, []).append((False, MISSING_DIM_STRENGTH))
        idx, info = decode_bits(votes, n_leds)
        if idx is not None:
            out[tid] = Identity(idx, n_pairs, float(info.get("weakest", 0.0)))
    return out
