"""Decoding LED identities from tracks.

For each track, detections in the middle of code slots are grouped by (cycle, bit):
the LED's flux in the bit's pattern slot is compared with its complement slot in
the same cycle (0.2 s apart, so distance and occlusion barely change), giving one
vote per bit per cycle. If the dim state of a pair wasn't detected (too faint)
while the track was seen in the neighbouring slots, that counts as a weaker vote.

Tracks can swap onto a neighbouring LED partway through (two LEDs passing close in
the image while the phone moves). So votes are not pooled over the whole track:
consecutive cycles are merged only while their votes agree, the track is split
where they start to contradict, and each piece is decoded on its own. A detection
gets an identity only from the piece covering its time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..detection import binary
from .pattern import CYCLE_SLOTS, CYCLE_T, N_BITS, SYNC, T_SLOT, decode_votes, slot_info
from .timing import Timing
from .tracking import TrackSet

PHASE_LO, PHASE_HI = 0.2, 0.8
MISSING_DIM_STRENGTH = 0.4
TYPICAL_CONTRAST = 0.5  # |bright - dim| / (bright + dim) of an unsaturated LED is ~0.6
CONTRAST_FLOOR = 0.08
CONFLICT_STRENGTH = 0.15
TRIM_RUN = 3
WHOLE_TRACK_AGREEMENT = 0.85
MIN_GOOD_VOTES = 6  # strong votes for a cycle to confirm an identity on its own


@dataclass
class Identity:
    index: int
    votes: int  # complete (pattern, complement) pairs used
    weakest: float
    t0: float = -np.inf  # pattern-time range this identity covers
    t1: float = np.inf


def _cycle_votes(rows: np.ndarray, k: np.ndarray, ph: np.ndarray,
                 sampled: set[int] | None = None) -> dict[int, dict[int, tuple[bool, float]]]:
    """cycle -> bit -> (value, strength)

    sampled: slots that have a frame in their middle (from all frames, not just this
    track's). A slot the track has no detection in counts as "LED dim and not seen" only
    if the camera did look during it; at the live stream's ~8 frames/s many slots simply
    have no frame in the middle. None: every slot is taken as sampled."""
    good = (ph > PHASE_LO) & (ph < PHASE_HI)
    slots_seen = set(k.tolist())
    flux: dict[tuple[int, int, bool], list[float]] = {}
    for kk, f in zip(k[good], rows[good, 4]):
        info = slot_info(int(kk))
        if info.kind == "bit":
            flux.setdefault((int(kk) // CYCLE_SLOTS, info.bit, info.complement), []).append(float(f))
    out: dict[int, dict[int, tuple[bool, float]]] = {}
    for c in sorted({c for c, _b, _cmp in flux}):
        raw: dict[int, float] = {}
        for b in range(N_BITS):
            P = flux.get((c, b, False))
            C = flux.get((c, b, True))
            kP = c * CYCLE_SLOTS + len(SYNC) + 2 * b
            kC = kP + 1
            if P and C:
                p, q = float(np.mean(P)), float(np.mean(C))
                if p + q > 0:
                    out.setdefault(c, {})[b] = (p > q, abs(p - q) / (p + q))
                    raw[b] = abs(p - q) / (p + q)
            elif P and not C and (kC + 1) in slots_seen and (sampled is None or kC in sampled):
                out.setdefault(c, {})[b] = (True, MISSING_DIM_STRENGTH)
            elif C and not P and (kP - 1) in slots_seen and (sampled is None or kP in sampled):
                out.setdefault(c, {})[b] = (False, MISSING_DIM_STRENGTH)
        # Relative contrast: when an LED saturates (close to the camera), bright and dim
        # both clip and every bit of the cycle shows a low contrast, though the signs are
        # still right. Scale each cycle so its typical bit has the contrast of an
        # unsaturated LED; the floor keeps unmodulated blobs (static lights, noise:
        # a few percent) weak.
        if len(raw) >= 4:
            gain = TYPICAL_CONTRAST / max(float(np.median(list(raw.values()))), CONTRAST_FLOOR)
            if gain > 1:
                out[c] = {b: (v, min(1.0, st * gain) if b in raw else st) for b, (v, st) in out[c].items()}
    return out


def _conflicts(merged: dict[int, list[tuple[bool, float]]], cyc: dict[int, tuple[bool, float]]) -> int:
    n = 0
    for b, (v, s) in cyc.items():
        if s < CONFLICT_STRENGTH:
            continue
        prev = [pv for pv, ps in merged.get(b, []) if ps >= CONFLICT_STRENGTH]
        if prev and sum(1 for pv in prev if pv != v) > len(prev) / 2:
            n += 1
    return n


def sampled_slots(frame_t: np.ndarray, timing: Timing) -> set[int]:
    """Slots with at least one frame in their middle."""
    k, ph = timing.slot_phase(np.asarray(frame_t))
    return set(k[(ph > PHASE_LO) & (ph < PHASE_HI)].tolist())


def decode_track(rows: np.ndarray, frame_t: np.ndarray, timing: Timing, n_leds: int,
                 sampled: set[int] | None = None) -> list[Identity]:
    fi = rows[:, 1].astype(int)
    k, ph = timing.slot_phase(frame_t[fi])
    if sampled is None:
        sampled = sampled_slots(frame_t, timing)
    cycles = _cycle_votes(rows, k, ph, sampled)
    pieces: list[tuple[list[int], dict[int, list[tuple[bool, float]]]]] = []
    for c in sorted(cycles):
        cyc = cycles[c]
        if pieces and pieces[-1][0][-1] == c - 1 and _conflicts(pieces[-1][1], cyc) == 0:
            pieces[-1][0].append(c)
            for b, vs in cyc.items():
                pieces[-1][1].setdefault(b, []).append(vs)
        else:
            pieces.append(([c], {b: [vs] for b, vs in cyc.items()}))
    # No swap at all is the common case: try the whole track first.
    if len(pieces) > 1:
        whole: dict[int, list[tuple[bool, float]]] = {}
        for c in cycles:
            for b, vs in cycles[c].items():
                whole.setdefault(b, []).append(vs)
        idx, info = decode_votes(whole, n_leds, len(cycles))
        if idx is not None and info["weakest"] >= WHOLE_TRACK_AGREEMENT:
            pieces = [(sorted(cycles), whole)]
    out = []
    table = binary.code_table(n_leds)
    for cs, votes in pieces:
        idx, info = decode_votes(votes, n_leds, len(cs))
        if idx is None:
            continue
        n_pairs = sum(len(v) for v in votes.values())
        for run in _consistent_runs(cs, cycles, table[idx]):
            t0, t1 = _trim(run, cycles, table[idx])
            if t1 > t0:
                out.append(Identity(idx, n_pairs, float(info.get("weakest", 0.0)), t0, t1))
    return out


def _consistent_runs(cs: list[int], cycles: dict, code: np.ndarray) -> list[list[int]]:
    """Split a decoded piece into runs of cycles that each agree with its codeword.

    A cycle is good when enough of its strong votes were seen and at most one disagrees,
    bad when two or more disagree (two LEDs' codes differ in at least 4 bits, so after a
    swap most cycles show several), and weak otherwise. Runs never contain a bad cycle,
    and start and end on good cycles: the decode as a whole can be right while the
    track spent part of its time on another LED."""
    status = []
    for c in cs:
        agrees = [bool(v) == bool(code[b]) for b, (v, s) in cycles.get(c, {}).items() if s >= CONFLICT_STRENGTH]
        dis = agrees.count(False)
        status.append("bad" if dis >= 2 else "good" if len(agrees) >= MIN_GOOD_VOTES else "weak")
    runs, cur = [], []
    for c, st in zip(cs, status):
        if st == "bad":
            runs.append(cur)
            cur = []
        else:
            cur.append((c, st))
    runs.append(cur)
    out = []
    for r in runs:
        good = [k for k, (_c, st) in enumerate(r) if st == "good"]
        if good:
            out.append([c for c, _st in r[good[0] : good[-1] + 1]])
    return out


def _pair_time(c: int, b: int) -> float:
    """Pattern time at the end of bit b's (pattern, complement) pair in cycle c."""
    return (c * CYCLE_SLOTS + len(SYNC) + 2 * b + 2) * T_SLOT


def _trim(cs: list[int], cycles: dict, code: np.ndarray | None) -> tuple[float, float]:
    """Shrink a piece's time range to where its votes agree with the decoded codeword.

    A swap partway through a cycle leaves that cycle's later bits agreeing with the old
    code only by chance, so the cycle can still merge; trimming the ends back to the
    last run of agreeing votes keeps the other LED's detections from inheriting this
    identity."""
    t0, t1 = cs[0] * CYCLE_T, (cs[-1] + 1) * CYCLE_T
    if code is None:
        return t0, t1
    seq = []  # (time, agrees) in time order
    for c in cs:
        for b in range(N_BITS):
            vs = cycles.get(c, {}).get(b)
            if vs is not None and vs[1] >= CONFLICT_STRENGTH:
                seq.append((_pair_time(c, b), bool(vs[0]) == bool(code[b])))
    if not seq:
        return t0, t1
    run = TRIM_RUN
    # The last and the first run of `run` consecutive agreeing votes.
    end = None
    for k in range(len(seq) - 1, run - 2, -1):
        if all(a for _t, a in seq[k - run + 1 : k + 1]):
            end = k
            break
    start = None
    for k in range(0, len(seq) - run + 1):
        if all(a for _t, a in seq[k : k + run]):
            start = k
            break
    if end is None or start is None or end < start:
        return t0, t0
    # From the first to the last agreeing pair (sync slots inside the piece stay covered;
    # at its ends they're left out, since a swap there can't be placed).
    return seq[start][0] - 2 * T_SLOT, seq[end][0]


def decode_tracks(ts: TrackSet, frame_t: np.ndarray, timing: Timing, n_leds: int, min_rows: int = 20) -> dict[int, list[Identity]]:
    """track id -> identities with the pattern-time range each covers."""
    out: dict[int, list[Identity]] = {}
    if not timing.ok or len(ts.rows) == 0:
        return out
    sampled = sampled_slots(frame_t, timing)
    for tid, rows in ts.by_track().items():
        if len(rows) < min_rows:
            continue
        ids = decode_track(rows, frame_t, timing, n_leds, sampled)
        if ids:
            out[tid] = ids
    return out


def identity_at(ids: list[Identity], pattern_t: float) -> Identity | None:
    for ident in ids:
        if ident.t0 <= pattern_t < ident.t1:
            return ident
    return None
