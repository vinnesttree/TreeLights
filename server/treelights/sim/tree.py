"""Synthetic tree: a cone with a wired LED chain (architecture §17).

The chain is made of strands joined by longer connector links, optionally on
two WLED outputs (a break with no physical link). Every normal link's
straight-line length is <= `link` (the wire can sag but not stretch).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class SynthTree:
    positions: np.ndarray  # (N, 3) cm, tree frame: y up, origin on the axis at the floor
    height: float
    base_radius: float
    bottom: float  # height of the lowest branches
    link: float
    connector: float
    long_links: list[int] = field(default_factory=list)  # i: link i -> i+1 is a connector
    breaks: list[int] = field(default_factory=list)  # i: no physical link i -> i+1

    @property
    def n(self) -> int:
        return len(self.positions)

    def surface_radius(self, h: np.ndarray | float) -> np.ndarray:
        h = np.asarray(h, dtype=float)
        frac = np.clip((self.height - h) / (self.height - self.bottom), 0.0, 1.0)
        return self.base_radius * frac

    def link_bound(self, i: int) -> float | None:
        if i in self.breaks:
            return None
        return self.connector if i in self.long_links else self.link


def _curve(turns: float, phase: float, height: float, bottom: float, base_radius: float, depth: np.ndarray, m: int) -> np.ndarray:
    s = np.linspace(0.0, 1.0, m)
    h = bottom + s * (height - bottom) * 0.97
    rs = base_radius * np.clip((height - h) / (height - bottom), 0, 1)
    theta = phase + 2 * np.pi * turns * s
    r = rs * depth
    return np.stack([r * np.cos(theta), h, r * np.sin(theta)], axis=1)


def _place_along(curve: np.ndarray, spacings: np.ndarray) -> np.ndarray | None:
    seg = np.linalg.norm(np.diff(curve, axis=0), axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    targets = np.concatenate([[0.0], np.cumsum(spacings)])
    if targets[-1] > arc[-1]:
        return None
    out = np.empty((len(targets), 3))
    for k in range(3):
        out[:, k] = np.interp(targets, arc, curve[:, k])
    return out


def make_tree(
    n: int = 600,
    height: float = 180.0,
    base_radius: float = 65.0,
    bottom: float = 25.0,
    link: float = 10.0,
    strand: int = 50,
    connector: float = 20.0,
    output_break_at: int | None = None,
    seed: int = 0,
) -> SynthTree:
    rng = np.random.default_rng(seed)
    parts = [(0, output_break_at), (output_break_at, n)] if output_break_at else [(0, n)]
    positions = []
    long_links: list[int] = []
    for pi, (a, b) in enumerate(parts):
        count = b - a
        spacing = np.empty(count - 1)
        for j in range(count - 1):
            i = a + j
            if (i + 1) % strand == 0:
                spacing[j] = connector * rng.uniform(0.75, 0.95)
                long_links.append(i)
            else:
                spacing[j] = link * rng.uniform(0.75, 0.97)
        m = 6000
        # Depth inside the tree wanders smoothly between 0.55 and 1.0 of the surface radius.
        knots = rng.uniform(0.55, 1.0, size=40)
        depth = np.interp(np.linspace(0, 1, m), np.linspace(0, 1, len(knots)), knots)
        phase = rng.uniform(0, 2 * np.pi) + pi * np.pi
        # Find the number of turns so the curve is just long enough (curve length grows with turns).
        lo, hi = 0.5, 80.0
        need = spacing.sum()
        for _ in range(50):
            mid = 0.5 * (lo + hi)
            c = _curve(mid, phase, height, bottom, base_radius, depth, m)
            if np.linalg.norm(np.diff(c, axis=0), axis=1).sum() < need * 1.002:
                lo = mid
            else:
                hi = mid
        c = _curve(hi, phase, height, bottom, base_radius, depth, m)
        pts = _place_along(c, spacing)
        assert pts is not None
        positions.append(pts)
    pos = np.concatenate(positions)
    breaks = [output_break_at - 1] if output_break_at else []
    tree = SynthTree(pos, height, base_radius, bottom, link, connector, long_links, breaks)
    # Placement along an arc guarantees chord <= arc length <= nominal bound.
    for i in range(n - 1):
        bnd = tree.link_bound(i)
        if bnd is not None:
            assert np.linalg.norm(pos[i + 1] - pos[i]) <= bnd + 1e-9
    return tree
