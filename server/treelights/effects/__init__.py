"""Effect interface and the test animation (architecture §12)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class TreeGeometry:
    """Per-LED features from a map, as numpy arrays. LEDs without a position are
    marked by `has_pos` and placed at the axis so effects can ignore them."""

    n: int
    pos: np.ndarray  # (n,3) map units
    has_pos: np.ndarray  # (n,) bool
    status: list[str]
    h: np.ndarray  # height
    h_norm: np.ndarray  # 0..1
    theta: np.ndarray  # angle around the axis
    r: np.ndarray
    depth: np.ndarray  # r / surface radius at that height
    chain: np.ndarray  # i / (n-1)

    @staticmethod
    def from_map(m: dict) -> "TreeGeometry":
        leds = m["leds"]
        n = len(leds)
        pos = np.zeros((n, 3))
        has = np.zeros(n, bool)
        for l in leds:
            if l["p"]:
                pos[l["i"]] = l["p"]
                has[l["i"]] = True
        h = pos[:, 1]
        lo, hi = (h[has].min(), h[has].max()) if has.any() else (0.0, 1.0)
        r = np.hypot(pos[:, 0], pos[:, 2])
        sh, sr = np.array(m["surface"]["h"]), np.array(m["surface"]["r"])
        rs = np.interp(h, sh, sr) if len(sh) else np.ones(n)
        return TreeGeometry(n, pos, has, [l["status"] for l in leds], h, (h - lo) / max(hi - lo, 1e-9),
                            np.arctan2(-pos[:, 2], pos[:, 0]), r, r / np.maximum(rs, 1e-9), np.arange(n) / max(n - 1, 1))

    @staticmethod
    def chain_only(n: int) -> "TreeGeometry":
        z = np.zeros(n)
        return TreeGeometry(n, np.zeros((n, 3)), np.zeros(n, bool), ["unknown"] * n, z, z, z, z, z, np.arange(n) / max(n - 1, 1))


class Effect:
    name = "effect"

    def setup(self, tree: TreeGeometry) -> None:
        self.tree = tree

    def render(self, t: float, out: np.ndarray) -> None:  # out: (n,3) float 0..1
        raise NotImplementedError


class SweepTest(Effect):
    """Rising plane -> rotating half-plane -> growing sphere, looping (6 s each)."""

    name = "sweep"
    PHASE = 6.0

    def render(self, t: float, out: np.ndarray) -> None:
        tr = self.tree
        out[:] = 0
        if not tr.has_pos.any():
            return
        k = int(t // self.PHASE) % 3
        u = (t % self.PHASE) / self.PHASE
        if k == 0:  # horizontal plane rising up the tree
            d = np.abs(tr.h_norm - u)
            out[:] = np.clip(1 - d / 0.06, 0, 1)[:, None] * [1.0, 0.2, 0.05]
        elif k == 1:  # vertical half-plane rotating around the axis
            a = (tr.theta - u * 2 * np.pi + np.pi) % (2 * np.pi) - np.pi
            out[:] = np.clip(1 - np.abs(a) / 0.35, 0, 1)[:, None] * [0.1, 1.0, 0.2]
        else:  # sphere growing from the middle of the tree
            c = np.array([0.0, tr.h[tr.has_pos].mean(), 0.0])
            dist = np.linalg.norm(tr.pos - c, axis=1)
            rmax = dist[tr.has_pos].max()
            out[:] = np.clip(1 - np.abs(dist - u * rmax) / (0.08 * rmax), 0, 1)[:, None] * [0.2, 0.4, 1.0]
        out[~tr.has_pos] = 0


class StatusColors(Effect):
    """Map status on the real tree: measured green, single_view yellow, estimated orange,
    manual blue, unknown off."""

    name = "status"
    COLORS = {"measured": (0, 1, 0), "single_view": (1, 0.8, 0), "estimated": (1, 0.3, 0), "manual": (0, 0.3, 1), "unknown": (0, 0, 0)}

    def render(self, t: float, out: np.ndarray) -> None:
        for i, s in enumerate(self.tree.status):
            out[i] = self.COLORS.get(s, (0, 0, 0))


class Chase(Effect):
    """Index chase: works before any map exists."""

    name = "chase"

    def render(self, t: float, out: np.ndarray) -> None:
        n = self.tree.n
        head = (t * 30) % n
        d = (head - np.arange(n)) % n
        out[:] = np.clip(1 - d / 8, 0, 1)[:, None] * [1.0, 1.0, 1.0]


class Identify(Effect):
    """Blink one LED, or a set of them (the rest off)."""

    name = "identify"

    def __init__(self, index: int | list[int]):
        self.indices = [int(index)] if isinstance(index, (int, np.integer)) else [int(i) for i in index]
        self.index = self.indices[0] if self.indices else -1

    def render(self, t: float, out: np.ndarray) -> None:
        out[:] = 0
        if (t * 2) % 1 < 0.6:
            idx = [i for i in self.indices if 0 <= i < len(out)]
            out[idx] = (1, 1, 1)


EFFECTS = {"sweep": SweepTest, "status": StatusColors, "chase": Chase}


def make_renderer(effect: Effect, brightness: float, gamma: float = 2.2):
    """Wrap an effect as an output-thread renderer: float 0..1 -> gamma -> uint8."""
    buf = np.zeros((effect.tree.n, 3))
    t0 = [None]

    def render(now: float) -> np.ndarray:
        if t0[0] is None:
            t0[0] = now
        effect.render(now - t0[0], buf)
        v = np.clip(buf, 0, 1) ** gamma * 255 * brightness
        return v.astype(np.uint8)

    return render
