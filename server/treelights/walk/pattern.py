"""The LED pattern for walk-around capture.

The camera moves continuously, so LEDs can't be lit one at a time. Instead every
LED blinks its own index, all at once, in a repeating cycle of slots:

  slots 0-3   sync: all LEDs bright, off, bright, off
  slots 4-35  16 code bits, each as a pattern slot then its complement slot

In code slots an LED is never off, only bright or dim (DIM of bright), so it can
be tracked from frame to frame while the phone moves; a bit is decided by
comparing the LED's brightness in the pattern slot with the adjacent complement
slot. The code is the scrambled extended Hamming(16,11) code of the binary pass:
any 1-3 bit errors give an invalid word. Static lights (lamps, a TV) aren't
modulated, so they never decode.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..detection import binary

T_SLOT = 0.2  # seconds; 6 frames at 30 fps, ~2 at the live stream's rate
SYNC = (1, 0, 1, 0)  # all bright / all off
N_BITS = binary.N_BITS
CYCLE_SLOTS = len(SYNC) + 2 * N_BITS
CYCLE_T = CYCLE_SLOTS * T_SLOT
DIM = 0.25
COLOR = np.array([0.0, 1.0, 0.0])  # green: the camera's most sensitive channel


@dataclass(frozen=True)
class SlotInfo:
    kind: str  # "sync_on" | "sync_off" | "bit"
    bit: int = -1
    complement: bool = False


def slot_info(k: int) -> SlotInfo:
    k %= CYCLE_SLOTS
    if k < len(SYNC):
        return SlotInfo("sync_on" if SYNC[k] else "sync_off")
    j = k - len(SYNC)
    return SlotInfo("bit", j // 2, bool(j % 2))


def template(k: np.ndarray) -> np.ndarray:
    """Expected overall LED brightness per slot (all bright = 1)."""
    k = np.asarray(k) % CYCLE_SLOTS
    out = np.full(k.shape, (1 + DIM) / 2)
    for s, v in enumerate(SYNC):
        out[k == s] = float(v)
    return out


class WalkPattern:
    def __init__(self, n: int, level: int = 48):
        self.n = n
        self.level = int(level)
        self.codes = binary.code_table(n)  # (n, 16)

    def frame_for_slot(self, k: int, level: int | None = None) -> np.ndarray:
        lv = self.level if level is None else int(level)
        info = slot_info(k)
        f = np.zeros((self.n, 3), np.uint8)
        if info.kind == "sync_on":
            f[:] = (COLOR * lv).astype(np.uint8)
        elif info.kind == "bit":
            on = self.codes[:, info.bit] == (0 if info.complement else 1)
            bright = (COLOR * lv).astype(np.uint8)
            dim = (COLOR * max(1, round(lv * DIM))).astype(np.uint8)
            f[:] = dim
            f[on] = bright
        return f

    def renderer(self):
        """Output-thread renderer. The level can change between cycles (see set_level)."""
        t0: list[float | None] = [None]
        state = {"cycle": -1, "level": self.level}

        def render(now: float) -> np.ndarray:
            if t0[0] is None:
                t0[0] = now
                self.t0 = now
            t = now - t0[0]
            k = int(t // T_SLOT)
            cycle = k // CYCLE_SLOTS
            if cycle != state["cycle"]:  # brightness changes only between cycles
                state["cycle"] = cycle
                state["level"] = self.level
            return self.frame_for_slot(k, state["level"])

        return render

    def set_level(self, level: int) -> None:
        self.level = int(np.clip(level, 6, 255))


def decode_bits(votes: dict[int, list[tuple[bool, float]]], n_leds: int, min_agree: float = 0.75,
                min_strength: float = 0.2) -> tuple[int | None, dict]:
    """Decode an LED index from per-bit votes [(bit value, strength), ...].

    A bit is reliable when its votes agree (>= min_agree, weighted by strength) and are
    strong enough; a bit without votes is an erasure. One erasure is filled by trying
    both values: with minimum distance 4 at most one gives a valid codeword. Conflicting
    votes reject the word (more likely a tracking mix-up than noise)."""
    bits = np.zeros(N_BITS, np.uint8)
    erased = []
    weakest = 1.0
    for b in range(N_BITS):
        vs = votes.get(b, [])
        if not vs:
            erased.append(b)
            continue
        w1 = sum(s for v, s in vs if v)
        w0 = sum(s for v, s in vs if not v)
        tot = w1 + w0
        if tot <= 0:
            erased.append(b)
            continue
        agree = max(w1, w0) / tot
        strength = tot / len(vs)
        if agree < min_agree or strength < min_strength:
            return None, {"reason": "unreliable_bit", "bit": b}
        bits[b] = 1 if w1 > w0 else 0
        weakest = min(weakest, strength * agree)
    if len(erased) > 1:
        return None, {"reason": "erasures", "erased": erased}
    options = [bits]
    if erased:
        alt = bits.copy()
        alt[erased[0]] = 1
        options = [bits, alt]
    valid = [v for v in (binary.decode(o) for o in options) if v is not None]
    if len(valid) != 1:
        return None, {"reason": "invalid"}
    idx = binary.value_to_index(valid[0])
    if idx >= n_leds:
        return None, {"reason": "out_of_range"}
    return int(idx), {"weakest": weakest, "erased": len(erased)}
