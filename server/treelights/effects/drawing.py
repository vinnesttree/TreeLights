"""Drawing: a still picture painted on the tree from the phone (the Draw page).

The picture is one color per LED, kept in a `Canvas`. The brush colors every LED within its
radius of the stroke, in one of two spaces:

- **3d**: points in map coordinates; an LED is painted if its position is within the radius of
  the stroke. The Draw page's 3D view sends points on the surface model, where the finger meets
  the tree.
- **uv**: points on the unwrapped surface, as the surface patterns use it (u = angle around the
  axis, 0..1; v = height, 0..1). Distances around the tree are measured along the circumference at
  each LED's height, so the brush is round on the tree, and strokes join across the seam at u = 0.

The radius is a fraction of the tree's height in both spaces. A stroke is a polyline: every
segment between consecutive points is painted, so a quick finger leaves no gaps. LEDs without a
position are never painted, and `DrawingEffect` keeps them off as other map effects do.
"""

from __future__ import annotations

import threading

import numpy as np

from .base import Effect, TreeGeometry, _color

SPACES = ("3d", "uv")
MIN_RADIUS, MAX_RADIUS = 0.005, 0.5  # fraction of the tree's height


class Canvas:
    """One sRGB color (0..1) per LED. Written by the API, read by the output thread."""

    def __init__(self, n: int):
        self.rgb = np.zeros((n, 3))
        self.lock = threading.Lock()

    @property
    def n(self) -> int:
        return len(self.rgb)

    def resize(self, n: int) -> None:
        with self.lock:
            if n != len(self.rgb):
                new = np.zeros((n, 3))
                m = min(n, len(self.rgb))
                new[:m] = self.rgb[:m]
                self.rgb = new

    def snapshot(self) -> np.ndarray:
        with self.lock:
            return self.rgb.copy()

    def restore(self, rgb: np.ndarray) -> None:
        with self.lock:
            m = min(len(rgb), len(self.rgb))
            self.rgb[:] = 0
            self.rgb[:m] = rgb[:m]

    def paint(self, mask: np.ndarray, rgb: np.ndarray) -> np.ndarray:
        """Set the LEDs in `mask` to `rgb`; returns the indices that changed."""
        with self.lock:
            idx = np.flatnonzero(mask[: len(self.rgb)])
            idx = idx[np.any(self.rgb[idx] != rgb, axis=1)]
            self.rgb[idx] = rgb
            return idx

    def to_hex(self, idx: np.ndarray | None = None) -> list[str]:
        with self.lock:
            sel = self.rgb if idx is None else self.rgb[idx]
            v = np.round(np.clip(sel, 0, 1) * 255).astype(int)
        return ["#%02x%02x%02x" % tuple(c) for c in v]

    def set_hex(self, colors: list) -> None:
        rgb = np.zeros((len(self.rgb), 3))
        for i, c in enumerate(colors[: len(rgb)]):
            rgb[i] = np.asarray(_color(c, (0, 0, 0)), float) / 255.0
        self.restore(rgb)


def parse_color(c) -> np.ndarray:  # noqa: ANN001
    """A color as '#rrggbb' or [r, g, b] (0..255) -> sRGB 0..1. Raises ValueError."""
    if c is None:
        raise ValueError("no color given")
    return np.asarray(_color(c, (0, 0, 0)), float) / 255.0


class BrushSpace:
    """Per-LED coordinates the brush works in, derived from a map's `TreeGeometry`."""

    def __init__(self, tree: TreeGeometry):
        self.tree = tree
        self.span = float(tree.span or 1.0)
        self.has = tree.has_pos
        self.pos = tree.pos
        self.u = (tree.theta / (2 * np.pi)) % 1.0
        self.v = (tree.h - tree.lo) / self.span
        rs = tree.rs if tree.rs is not None else np.maximum(tree.r, 1e-9)
        # Circumference at each LED's height, in tree heights: turns du into a length.
        self.circ = np.maximum(2 * np.pi * rs / self.span, 1e-3)

    def mask(self, points: np.ndarray, radius: float, space: str) -> np.ndarray:
        """LEDs within `radius` (tree heights) of the polyline through `points`."""
        if space == "3d":
            return self.has & _near_polyline(self.pos / self.span, points / self.span, radius)
        if space == "uv":
            return self.has & self._near_polyline_uv(points, radius)
        raise ValueError(f"unknown space {space!r} (use {' or '.join(SPACES)})")

    def _near_polyline_uv(self, points: np.ndarray, radius: float) -> np.ndarray:
        hit = np.zeros(self.tree.n, bool)
        segs = [(points[0], points[0])] if len(points) == 1 else list(zip(points[:-1], points[1:]))
        for a, b in segs:
            # Local coordinates around a, in tree heights; du wraps so strokes cross the seam.
            du = _wrap(self.u - a[0]) * self.circ
            dv = self.v - a[1]
            su = _wrap(b[0] - a[0]) * self.circ
            sv = np.full_like(self.v, b[1] - a[1])
            hit |= _seg_dist2(du, dv, su, sv) <= radius * radius
        return hit


def _wrap(x: np.ndarray) -> np.ndarray:
    """Angle difference in turns, to -0.5..0.5."""
    return (x + 0.5) % 1.0 - 0.5


def _seg_dist2(px: np.ndarray, py: np.ndarray, sx: np.ndarray, sy: np.ndarray) -> np.ndarray:
    """Squared distance from points (px, py) to segments from the origin to (sx, sy)."""
    ll = sx * sx + sy * sy
    t = np.where(ll > 0, np.clip((px * sx + py * sy) / np.where(ll > 0, ll, 1), 0, 1), 0)
    return (px - t * sx) ** 2 + (py - t * sy) ** 2


def _near_polyline(p: np.ndarray, pts: np.ndarray, radius: float) -> np.ndarray:
    hit = np.zeros(len(p), bool)
    segs = [(pts[0], pts[0])] if len(pts) == 1 else list(zip(pts[:-1], pts[1:]))
    for a, b in segs:
        d = b - a
        ll = float(d @ d)
        t = np.clip(((p - a) @ d) / ll, 0, 1) if ll > 0 else np.zeros(len(p))
        q = a + t[:, None] * d
        hit |= ((p - q) ** 2).sum(axis=1) <= radius * radius
    return hit


def parse_points(points, space: str, max_points: int = 256) -> np.ndarray:  # noqa: ANN001
    """Validate a stroke's points: [[x, y, z], ...] for 3d, [[u, v], ...] for uv."""
    if space not in SPACES:
        raise ValueError(f"unknown space {space!r} (use {' or '.join(SPACES)})")
    dim = 3 if space == "3d" else 2
    if not isinstance(points, (list, tuple)) or not points:
        raise ValueError("a stroke needs at least one point")
    if len(points) > max_points:
        raise ValueError(f"too many points in one stroke message (at most {max_points})")
    try:
        arr = np.asarray(points, float)
    except (TypeError, ValueError) as e:
        raise ValueError("points must be lists of numbers") from e
    if arr.ndim != 2 or arr.shape[1] != dim:
        raise ValueError(f"each point needs {dim} numbers in {space} space")
    if not np.isfinite(arr).all():
        raise ValueError("points must be finite numbers")
    return arr


def clamp_radius(radius) -> float:  # noqa: ANN001
    try:
        r = float(radius)
    except (TypeError, ValueError) as e:
        raise ValueError("radius must be a number (a fraction of the tree's height)") from e
    if not np.isfinite(r):
        raise ValueError("radius must be a finite number")
    return float(min(max(r, MIN_RADIUS), MAX_RADIUS))


class DrawingEffect(Effect):
    """Shows the drawing, still. Its canvas is attached by the server (`AppState.start_effect`);
    without one it shows nothing."""

    name = "drawing"
    label = "Drawing"
    category = "drawing"
    description = "The picture painted on the Draw page, shown still."
    needs_map = True
    uses_palette = False
    controls: dict[str, str] = {}

    def __init__(self, params=None, canvas: Canvas | None = None):  # noqa: ANN001
        super().__init__(params)
        self.canvas = canvas

    def render(self, t: float, out: np.ndarray) -> None:
        out[:] = 0
        if self.canvas is None:
            return
        rgb = self.canvas.snapshot()
        m = min(len(rgb), len(out))
        out[:m] = rgb[:m]


EFFECTS_DRAWING = [DrawingEffect]
