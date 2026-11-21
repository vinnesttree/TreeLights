"""Surface patterns: images painted on the tree's outer surface (the cone the map's surface model
describes), unwrapped as u = angle around the axis (0..1) and v = height (0..1).

Patterns are seamless around the tree: anything that repeats around it repeats a whole number of
times. Sizes are kept even over the height by counting repeats from the circumference at each
height, so polka dots near the top are as round as those at the bottom. The "Depth fade" slider
dims LEDs inside the tree, so only the outer shell shows the pattern.
"""

from __future__ import annotations

import numpy as np

from .base import Effect
from .noise import fbm


def _frac(x: np.ndarray) -> np.ndarray:
    return x - np.floor(x)


def _hash01(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    h = (a.astype(np.int64) * 0x8DA6B343 + b.astype(np.int64) * 0xD8163841) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0x5BD1E995) & 0xFFFFFFFF
    h ^= h >> 15
    return h.astype(float) / 0xFFFFFFFF


class SurfacePattern(Effect):
    category = "surface"
    needs_map = True
    color_slots = ("Color 1", "Background", "Color 3")

    def on_setup(self) -> None:
        self.v = self.P[:, 1]
        self.circ = np.maximum(2 * np.pi * self.rs, 1e-3)  # circumference at each LED's height (height units)
        has = self.tree.has_pos
        self.circ_mid = float(np.median(self.circ[has])) if has.any() else 1.0

    def row_circ(self, row: np.ndarray) -> np.ndarray:
        """Mean circumference of each LED's row, so every LED in a row agrees on the row's repeat count."""
        has = self.tree.has_pos
        r0 = row - row.min()
        cnt = np.bincount(r0[has], minlength=r0.max() + 1).astype(float)
        tot = np.bincount(r0[has], weights=self.circ[has], minlength=r0.max() + 1)
        mean = np.where(cnt > 0, tot / np.maximum(cnt, 1), self.circ_mid)
        return mean[r0]

    def frame(self, t: float, out: np.ndarray) -> None:
        super().frame(t, out)
        f = self.s("c3")
        if f > 0 and "c3" in self.controls:
            w = np.clip((self.tree.depth - (f - 0.15)) / 0.15, 0, 1)[:, None]
            bg = self.color(1)[None, :]
            out[:] = bg + (out - bg) * w
            out[~self.tree.has_pos] = 0


DEPTH = {"c3": "Depth fade"}


class CandyStripes(SurfacePattern):
    name = "stripes"
    label = "Candy Stripes"
    description = "Stripes spiral around the tree like a candy cane and scroll upward."
    controls = {"speed": "Speed", "intensity": "Stripes", "c1": "Twist", **DEPTH}
    defaults = {"intensity": 50, "c1": 170}
    default_palette = "candy_cane"

    def render(self, t: float, out: np.ndarray) -> None:
        n = 1 + int(self.params.intensity * 16 // 256)
        twist = (self.s("c1") - 0.5) * 8
        out[:] = self.pal(n * (self.u + twist * self.v) - self.T * 0.3)


class Rings(SurfacePattern):
    name = "rings"
    label = "Rings"
    description = "Horizontal bands climb the tree; tilt makes them wobble."
    controls = {"speed": "Speed", "intensity": "Bands", "c1": "Tilt", **DEPTH}
    defaults = {"intensity": 60, "c1": 40}
    default_palette = "rainbow_bands"

    def render(self, t: float, out: np.ndarray) -> None:
        n = 0.5 + 8 * self.s("intensity")
        tilt = 0.3 * self.s("c1") * np.sin(2 * np.pi * self.u + self.T * 0.8)
        out[:] = self.pal(n * (self.v + tilt) - self.T * 0.3)


class Waves(SurfacePattern):
    name = "waves"
    label = "Waves"
    description = "Wavy bands roll around the tree."
    controls = {"speed": "Speed", "intensity": "Bands", "c1": "Amplitude", "c2": "Waves around", **DEPTH}
    defaults = {"intensity": 50, "c1": 110, "c2": 60}
    default_palette = "ocean"

    def render(self, t: float, out: np.ndarray) -> None:
        n = 0.5 + 8 * self.s("intensity")
        k = 1 + int(self.params.c2 * 8 // 256)
        amp = 1.2 * self.s("c1")
        out[:] = self.pal(n * self.v + amp * np.sin(2 * np.pi * k * self.u - self.T * 2.0) - self.T * 0.2)


class ZigZag(SurfacePattern):
    name = "zigzag"
    label = "Zig-Zag"
    description = "Chevron bands wrap the tree and climb it."
    controls = {"speed": "Speed", "intensity": "Bands", "c1": "Zigs around", "c2": "Depth of zig", **DEPTH}
    defaults = {"intensity": 50, "c1": 80, "c2": 110}
    default_palette = "christmas"

    def render(self, t: float, out: np.ndarray) -> None:
        n = 0.5 + 8 * self.s("intensity")
        k = 2 + int(self.params.c1 * 14 // 256)
        amp = 1.5 * self.s("c2")
        tri = np.abs(_frac(k * self.u) * 2 - 1)
        out[:] = self.pal(n * self.v + amp * tri - self.T * 0.3)


class Checkerboard(SurfacePattern):
    name = "checker"
    label = "Checkerboard"
    description = "Square checks over the surface; the checks stay square from bottom to top."
    controls = {"speed": "Speed", "intensity": "Rows", "c1": "Twist", "c2": "Color cycle", **DEPTH}
    defaults = {"intensity": 60, "c1": 128, "c2": 0}
    default_palette = "christmas"

    def render(self, t: float, out: np.ndarray) -> None:
        rows = 2 + int(14 * self.s("intensity"))
        row = np.clip(np.floor(self.v * rows), 0, rows - 1).astype(int)
        cols = np.maximum(2, 2 * np.round(self.row_circ(row) * rows / 2)).astype(int)
        uu = _frac(self.u + (self.s("c1") - 0.5) * 2 * self.v - self.T * 0.04)
        col = np.floor(uu * cols).astype(int)
        parity = (row + col) % 2
        out[:] = self.pal(0.25 + 0.5 * parity + self.s("c2") * self.T * 0.1)


class PolkaDots(SurfacePattern):
    name = "dots"
    label = "Polka Dots"
    description = "Round dots in staggered rows turn slowly around the tree, each in its own palette color."
    controls = {"speed": "Speed", "intensity": "Dot size", "c1": "Spacing", **DEPTH}
    defaults = {"intensity": 150, "c1": 90}
    default_palette = "party"

    def render(self, t: float, out: np.ndarray) -> None:
        sp = 0.05 + 0.2 * self.s("c1")
        row = np.round(self.v / sp).astype(int)
        cnt = np.maximum(1, np.round(self.row_circ(row) / sp)).astype(int)
        off = 0.5 * (row % 2)
        uu = (self.u - self.T * 0.04) * cnt - off
        j = np.round(uu)
        du = (uu - j) / cnt * self.circ
        dv = self.v - row * sp
        d = np.hypot(du, dv)
        radius = sp * 0.5 * (0.25 + 0.75 * self.s("intensity"))
        a = np.clip((radius - d) / (0.3 * radius), 0, 1)
        self.over_bg(out, a, self.pal(_hash01(row, j.astype(int) % cnt)))


class Argyle(SurfacePattern):
    name = "argyle"
    label = "Argyle"
    description = "A diamond pattern like a knitted sweater, with optional stitching in color 3."
    controls = {"speed": "Speed", "intensity": "Diamonds around", "c1": "Stitching", **DEPTH}
    defaults = {"intensity": 70, "c1": 0, "speed": 60}
    color_slots = ("Color 1", "Color 2", "Stitching")
    default_palette = "christmas"

    def render(self, t: float, out: np.ndarray) -> None:
        n = 2 + int(self.params.intensity * 14 // 256)
        m = n / self.circ_mid / 1.4  # diamonds a little taller than wide
        fx = _frac(self.u * n - self.T * 0.05)
        fy = _frac(self.v * m)
        a = np.abs(fx - 0.5) + np.abs(fy - 0.5)
        out[:] = self.pal(np.where(a < 0.5, 0.25, 0.75))
        w = 0.08 * self.s("c1")
        if w > 0:
            line = np.clip(1 - np.abs(a - 0.5) / w, 0, 1)[:, None]
            out[:] = out * (1 - line) + self.color(2) * line


class Garland(SurfacePattern):
    name = "garland"
    label = "Garland"
    description = "Thin ribbons wrap the tree in a spiral, with color flowing along them."
    controls = {"speed": "Speed", "intensity": "Width", "c1": "Turns", "c2": "Ribbons", **DEPTH}
    defaults = {"intensity": 70, "c1": 90, "c2": 0}
    default_palette = "gold"

    def render(self, t: float, out: np.ndarray) -> None:
        turns = 1 + 7 * self.s("c1")
        g = 1 + int(self.params.c2 * 4 // 256)
        phase = (self.v * turns + self.u) * g - self.T * 0.25
        d = (_frac(phase + 0.5) - 0.5) / g / turns
        width = 0.008 + 0.06 * self.s("intensity")
        a = np.clip(1 - np.abs(d) / width, 0, 1)
        self.over_bg(out, a, self.pal(0.5 * (self.v * turns + self.u) - self.T * 0.1))


class Marble(SurfacePattern):
    name = "marble"
    label = "Marble"
    description = "Seamless flowing noise painted on the surface: marble, camouflage or lava."
    controls = {"speed": "Speed", "intensity": "Scale", "c1": "Contrast", **DEPTH}
    defaults = {"intensity": 80, "c1": 100}
    default_palette = "lava"

    def render(self, t: float, out: np.ndarray) -> None:
        k = 0.6 + 4 * self.s("intensity")
        a = 2 * np.pi * (self.u - self.T * 0.02)
        rad = k * self.circ_mid / (2 * np.pi) * 2
        v = fbm(np.cos(a) * rad, np.sin(a) * rad, self.v * k * 2 - self.T * 0.3, octaves=3)
        v = np.clip((v - 0.5) * (1 + 3 * self.s("c1")) + 0.5, 0, 1)
        out[:] = self.pal(v * 0.97)


EFFECTS_SURFACE = [CandyStripes, Rings, Waves, ZigZag, Checkerboard, PolkaDots, Argyle, Garland, Marble]
