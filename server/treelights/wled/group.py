"""Several WLED controllers driving one tree (architecture §5.5).

A tree's controllers are in a fixed order, and together they make one global index
space: the first controller drives global LEDs 0 .. n0-1, the second n0 .. n0+n1-1, and
so on. Everything above the output (maps, scans, effects, captures) works in global
indices; only the sender splits each frame into the controllers' slices.

- `IndexMap`: global index <-> (controller, local index), slices, and the boundaries
  between controllers, which are chain breaks: no wire joins the last LED of one
  controller to the first LED of the next (§10.3).
- `MultiSender`: sends every controller its slice of each frame from the output thread,
  back to back. The data packets of all controllers go first and their PUSH packets
  (which make WLED show the frame) last, together, so the controllers switch to a new
  frame within a fraction of a millisecond of each other on the sending side. What is
  left is the difference in network and processing delay between controllers; captures
  check every frame against what the camera sees, so it can cost time but not
  identities (§5.5).
- `SlicedPower`: the power cap applied per controller, each with its own limit.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from .client import config_fingerprint
from .ddp import UdpFrameSender


@dataclass(frozen=True)
class Slice:
    controller: int
    start: int  # first global index
    count: int

    @property
    def stop(self) -> int:
        return self.start + self.count


class IndexMap:
    """The global index space of one tree over its controllers, in order."""

    def __init__(self, counts: Sequence[int]):
        if not counts or any(int(c) <= 0 for c in counts):
            raise ValueError("every controller needs at least one LED")
        self.counts = [int(c) for c in counts]
        self.offsets = [0]
        for c in self.counts[:-1]:
            self.offsets.append(self.offsets[-1] + c)
        self.total = self.offsets[-1] + self.counts[-1]

    def __len__(self) -> int:
        return len(self.counts)

    def slices(self) -> list[Slice]:
        return [Slice(k, o, c) for k, (o, c) in enumerate(zip(self.offsets, self.counts))]

    def to_local(self, i: int) -> tuple[int, int]:
        """Global index -> (controller, index on that controller)."""
        i = int(i)
        if not 0 <= i < self.total:
            raise IndexError(f"LED {i} is outside 0..{self.total - 1}")
        k = int(np.searchsorted(self.offsets, i, side="right")) - 1
        return k, i - self.offsets[k]

    def to_global(self, controller: int, j: int) -> int:
        if not 0 <= controller < len(self.counts):
            raise IndexError(f"no controller {controller}")
        if not 0 <= j < self.counts[controller]:
            raise IndexError(f"LED {j} is outside controller {controller}'s 0..{self.counts[controller] - 1}")
        return self.offsets[controller] + int(j)

    def controller_of(self, i: int) -> int:
        return self.to_local(i)[0]

    def boundaries(self) -> list[int]:
        """Chain breaks between controllers: i such that LED i is the last LED of one
        controller and i+1 the first of the next."""
        return [o - 1 for o in self.offsets[1:]]

    def shift_breaks(self, controller: int, local_breaks: Sequence[int]) -> list[int]:
        """A controller's own output breaks (local indices) as global indices."""
        o, n = self.offsets[controller], self.counts[controller]
        return [o + int(b) for b in local_breaks if 0 <= int(b) < n - 1]

    def split(self, frame: np.ndarray) -> list[np.ndarray]:
        frame = np.asarray(frame).reshape(-1, 3)
        if len(frame) != self.total:
            raise ValueError(f"frame has {len(frame)} LEDs, expected {self.total}")
        return [frame[s.start : s.stop] for s in self.slices()]

    def describe(self) -> list[dict]:
        return [{"controller": s.controller, "start": s.start, "count": s.count, "last": s.stop - 1} for s in self.slices()]


class MultiSender:
    """A FrameSender for a tree with one or more controllers. Owned by the output thread.

    `senders[k]` is None for a controller without an address yet: its slice is skipped.
    With one controller this sends exactly what UdpFrameSender.send sends."""

    def __init__(self, index: IndexMap, senders: Sequence[UdpFrameSender | None], push_gap_s: float = 0.0005):
        if len(senders) != len(index):
            raise ValueError("one sender per controller")
        self.index = index
        self.senders = list(senders)
        self.push_gap_s = push_gap_s
        self.frames_sent = 0

    @property
    def packets_sent(self) -> int:
        return sum(s.packets_sent for s in self.senders if s is not None)

    def send(self, rgb: np.ndarray, repeats: int = 1, gap_s: float = 0.002) -> None:
        parts = self.index.split(rgb)
        work = [(s, s.encode(p)) for s, p in zip(self.senders, parts) if s is not None]
        if not work:
            return
        for r in range(repeats):
            if r:
                time.sleep(gap_s)
            # 1. Every controller's data packets but its last (PUSH) one.
            for s, packets in work:
                for p in packets[:-1]:
                    s.send_raw(p)
            # 2. A head start for them, so a PUSH packet is unlikely to overtake its data.
            if any(len(packets) > 1 for _s, packets in work):
                time.sleep(self.push_gap_s)
            # 3. The PUSH packets, back to back: the controllers show the frame together.
            for s, packets in work:
                s.send_raw(packets[-1])
        self.frames_sent += 1

    def close(self) -> None:
        for s in self.senders:
            if s is not None:
                s.close()


class SlicedPower:
    """The power cap per controller: each slice of a frame is scaled to its controller's
    own limit (each controller has its own power supply and its own WLED limit)."""

    def __init__(self, index: IndexMap, limiters: Sequence):
        if len(limiters) != len(index):
            raise ValueError("one limiter per controller")
        self.index = index
        self.limiters = list(limiters)

    @property
    def max_ma(self) -> float | None:
        """The total of the limits (None if any controller is unlimited)."""
        lims = [lim.max_ma for lim in self.limiters]
        return None if any(not m for m in lims) else float(sum(lims))

    def estimate_ma(self, rgb: np.ndarray) -> float:
        return float(sum(lim.estimate_ma(p) for lim, p in zip(self.limiters, self.index.split(rgb))))

    def apply(self, rgb: np.ndarray) -> np.ndarray:
        if len(self.limiters) == 1:
            return self.limiters[0].apply(rgb)
        if len(rgb) != self.index.total:
            return rgb
        parts = [lim.apply(p) for lim, p in zip(self.limiters, self.index.split(rgb))]
        return np.concatenate(parts, axis=0)


def make_sender(index: IndexMap, hosts: Sequence[tuple[str | None, str, int | None]],
                factory: Callable[[str, str, int | None], UdpFrameSender] = UdpFrameSender) -> MultiSender | None:
    """hosts[k] = (host or None, protocol, udp port or None). None if no controller has an address."""
    senders = [factory(h, proto, port) if h else None for h, proto, port in hosts]
    if all(s is None for s in senders):
        return None
    return MultiSender(index, senders)


def tree_fingerprint(parts: Sequence[tuple]) -> str | None:
    """The WLED fingerprint of a tree (§11.4). parts[k] = (WledInfo or None, WledConfigView
    or None) of controller k. With one controller it is that controller's fingerprint, as
    before there were several; with more, a hash over every controller's MAC, LED count
    and own fingerprint, in order. None while any controller is unknown."""
    if not parts or any(info is None for info, _cfg in parts):
        return None
    if len(parts) == 1:
        return config_fingerprint(*parts[0])
    blob = json.dumps([{"mac": info.mac, "count": info.led_count, "hash": config_fingerprint(info, cfg)}
                       for info, cfg in parts], sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()
