"""3D volumetric effects: shapes and fields that fill the tree's volume, using every LED's
measured 3D position (inner LEDs included).

Coordinates (see Effect.setup): `self.P` is (n,3) with y from 0 (lowest LED) to 1 (highest),
x and z in the same units, the tree's axis at x = z = 0.
"""

from __future__ import annotations

import numpy as np

from .base import Effect
from .noise import fbm


def _tri(x: np.ndarray | float) -> np.ndarray | float:
    """Triangle wave 0..1..0 with period 1."""
    return 1 - np.abs((np.asarray(x) % 1.0) * 2 - 1)


def _rot_y(a: float) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _rot_x(a: float) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


class Volumetric(Effect):
    category = "3d"
    needs_map = True
    color_slots = ("Color 1", "Background", "Color 3")


class PlaneSweep(Volumetric):
    name = "plane"
    label = "Plane Sweep"
    description = "A glowing plane sweeps back and forth through the tree while its tilt turns around the axis."
    controls = {"speed": "Speed", "intensity": "Thickness", "c1": "Tilt", "c2": "Color spread"}
    defaults = {"intensity": 70, "c1": 60, "c2": 80}
    default_palette = "rainbow"

    def on_setup(self) -> None:
        d = self.P - self.center
        self.R = float(np.linalg.norm(d[self.tree.has_pos], axis=1).max()) if self.tree.has_pos.any() else 1.0

    def render(self, t: float, out: np.ndarray) -> None:
        tilt = self.s("c1") * np.pi / 2
        az = self.T * 0.35
        nrm = np.array([np.sin(tilt) * np.cos(az), np.cos(tilt), np.sin(tilt) * np.sin(az)])
        proj = (self.P - self.center) @ nrm
        pos = self.R * (2 * _tri(self.T * 0.22) - 1)
        w = 0.02 + 0.25 * self.s("intensity")
        a = np.clip(1 - np.abs(proj - pos) / w, 0, 1) ** 1.5
        x = self.T * 0.04 + 0.25 * pos / self.R + self.s("c2") * (self.u + 0.5 * self.P[:, 1])
        self.over_bg(out, a, self.pal(x))


class Lighthouse(Volumetric):
    name = "lighthouse"
    label = "Lighthouse"
    description = "Vertical beams rotate around the axis; twist bends them into spirals."
    controls = {"speed": "Speed", "intensity": "Beams", "c1": "Twist", "c2": "Width"}
    defaults = {"intensity": 40, "c1": 128, "c2": 90}
    default_palette = "party"

    def render(self, t: float, out: np.ndarray) -> None:
        n = 1 + int(self.params.intensity * 6 // 256)
        twist = (self.s("c1") - 0.5) * 4  # turns over the height
        phase = (self.tree.theta - self.T * 1.6 - twist * 2 * np.pi * self.P[:, 1]) * n
        width = 0.05 + 0.6 * self.s("c2")
        a = np.clip((np.cos(phase) - (1 - width)) / width, 0, 1)
        k = np.floor(((phase / (2 * np.pi)) + 0.5)) % n
        self.over_bg(out, a, self.pal(k / n + 0.3 * self.P[:, 1] + self.T * 0.02))


class Ripples3D(Volumetric):
    name = "ripples"
    label = "Ripples 3D"
    description = "Spherical shells expand from random points inside the tree."
    controls = {"speed": "Speed", "intensity": "Rate", "c1": "Thickness"}
    defaults = {"intensity": 110, "c1": 90}
    default_palette = "party"

    def on_setup(self) -> None:
        self.ripples: list[list] = []  # [center (3,), radius, palette x]
        self.spawn = 1.0  # the first ripple starts at once
        idx = np.flatnonzero(self.tree.has_pos)
        self.inside = self.P[idx] if len(idx) else np.array([self.center])

    def render(self, t: float, out: np.ndarray) -> None:
        self.spawn += self.dt * (0.3 + 3.0 * self.s("intensity"))
        while self.spawn >= 1 and len(self.ripples) < 12:
            self.spawn -= 1
            self.ripples.append([self.inside[self.rng.integers(len(self.inside))], 0.0, self.rng.random()])
        self.spawn = min(self.spawn, 1.0)
        th = 0.025 + 0.12 * self.s("c1")
        rmax = 0.9
        acc = np.zeros_like(out)
        alpha = np.zeros(len(out))
        for rp in self.ripples:
            rp[1] += self.dt * 0.45
            d = np.linalg.norm(self.P - rp[0], axis=1)
            a = np.clip(1 - np.abs(d - rp[1]) / th, 0, 1) * max(0.0, 1 - rp[1] / rmax)
            acc += a[:, None] * self.pal(np.full(1, rp[2]))
            alpha += a
        self.ripples = [rp for rp in self.ripples if rp[1] < rmax]
        out[:] = self.color(1) * (1 - np.clip(alpha, 0, 1))[:, None] + np.clip(acc, 0, 1)


class Plasma3D(Volumetric):
    name = "plasma"
    label = "Plasma 3D"
    description = "Interfering waves in three dimensions, mapped through the palette."
    controls = {"speed": "Speed", "intensity": "Scale"}
    defaults = {"intensity": 100}
    default_palette = "party"

    def render(self, t: float, out: np.ndarray) -> None:
        x, y, z = self.P.T
        f = 2 + 12 * self.s("intensity")
        T = self.T
        v = (np.sin(x * f + T) + np.sin(y * f * 0.8 - T * 1.3) + np.sin((x + z) * f * 0.7 + T * 0.7)
             + np.sin(np.sqrt(x * x + (y - 0.5) ** 2 + z * z) * f * 1.5 - T * 1.1))
        out[:] = self.pal(v / 8 + 0.5)


class Clouds3D(Volumetric):
    name = "clouds"
    label = "Noise 3D"
    description = "A flowing 3D noise field: clouds, lava or ocean depending on the palette."
    controls = {"speed": "Speed", "intensity": "Scale", "c1": "Contrast"}
    defaults = {"intensity": 90, "c1": 80}
    default_palette = "cloud"

    def render(self, t: float, out: np.ndarray) -> None:
        sc = 1 + 6 * self.s("intensity")
        x, y, z = self.P.T * sc
        T = self.T * 0.4
        v = fbm(x + T * 0.6, y - T * 0.25, z + T * 0.35, octaves=3)
        v = np.clip((v - 0.5) * (1 + 3 * self.s("c1")) + 0.5, 0, 1)
        out[:] = self.pal(v * 0.97 + self.T * 0.01)


class Fire3D(Volumetric):
    name = "fire"
    label = "Fire 3D"
    description = "Flames rise from the bottom of the tree through its volume."
    controls = {"speed": "Speed", "intensity": "Flame height", "c1": "Turbulence"}
    defaults = {"intensity": 150, "c1": 100}
    default_palette = "heat"

    def render(self, t: float, out: np.ndarray) -> None:
        x, y, z = self.P.T
        turb = 2 + 6 * self.s("c1")
        n = fbm(x * turb, y * 4 - self.T * 2.2, z * turb, octaves=3)
        height = 0.25 + 1.0 * self.s("intensity")
        r = np.hypot(x, z) / self.rmax
        heat = np.clip(n * (1.35 - y / height) - 0.15 * r, 0, 1)
        self.over_bg(out, np.clip(heat * 3, 0, 1), self.pal(heat * 0.94))


class Snowfall(Volumetric):
    name = "snow"
    label = "Snowfall"
    description = "Flakes drift down through the tree; each one lights the LEDs it passes."
    controls = {"speed": "Speed", "intensity": "Density", "c1": "Flake size"}
    defaults = {"intensity": 110, "c1": 90, "speed": 110}
    default_palette = "ice"

    def on_setup(self) -> None:
        self.flakes = np.zeros((0, 5))  # x y z palette-x phase

    def _new(self, k: int, top: bool) -> np.ndarray:
        a = self.rng.uniform(0, 2 * np.pi, k)
        rr = self.rmax * np.sqrt(self.rng.random(k)) * 1.05
        y = np.full(k, 1.08) if top else self.rng.uniform(0, 1.08, k)
        return np.c_[rr * np.cos(a), y, rr * np.sin(a), self.rng.random(k), self.rng.uniform(0, 2 * np.pi, k)]

    def render(self, t: float, out: np.ndarray) -> None:
        want = 4 + int(70 * self.s("intensity"))
        if len(self.flakes) < want:
            self.flakes = np.r_[self.flakes, self._new(want - len(self.flakes), top=len(self.flakes) > 0)]
        elif len(self.flakes) > want:
            self.flakes = self.flakes[:want]
        f = self.flakes
        f[:, 1] -= self.dt * 0.16 * (0.75 + 0.5 * f[:, 3])
        f[:, 0] += self.dt * 0.03 * np.sin(self.T * 1.3 + f[:, 4])
        f[:, 2] += self.dt * 0.03 * np.cos(self.T * 1.1 + f[:, 4])
        gone = f[:, 1] < -0.06
        if gone.any():
            f[gone] = self._new(int(gone.sum()), top=True)
        size = 0.02 + 0.09 * self.s("c1")
        d2 = ((self.P[:, None, :] - f[None, :, :3]) ** 2).sum(-1)
        w = np.exp(-d2 / (size * size))
        acc = w @ self.pal(f[:, 3])
        out[:] = self.color(1) * (1 - np.clip(w.sum(1), 0, 1))[:, None] + np.clip(acc, 0, 1)


class Fireworks3D(Volumetric):
    name = "fireworks"
    label = "Fireworks 3D"
    description = "Rockets climb through the tree and burst into expanding, falling shells."
    controls = {"speed": "Speed", "intensity": "Launch rate", "c1": "Burst size", "c2": "Sparkle"}
    defaults = {"intensity": 100, "c1": 140, "c2": 120}
    default_palette = "party"

    def on_setup(self) -> None:
        self.rockets: list[dict] = []
        self.shells: list[dict] = []
        self.spawn = 1.0  # the first rocket launches at once

    def render(self, t: float, out: np.ndarray) -> None:
        dt = self.dt
        self.spawn += dt * (0.15 + 1.2 * self.s("intensity"))
        if self.spawn >= 1 and len(self.rockets) + len(self.shells) < 8:
            self.spawn = 0.0
            a, rr = self.rng.uniform(0, 2 * np.pi), self.rmax * 0.6 * np.sqrt(self.rng.random())
            self.rockets.append({"p": np.array([rr * np.cos(a), -0.02, rr * np.sin(a)]),
                                 "vy": self.rng.uniform(0.9, 1.2), "top": self.rng.uniform(0.45, 0.85),
                                 "x": self.rng.random()})
        self.spawn = min(self.spawn, 1.0)
        acc = np.zeros_like(out)
        alpha = np.zeros(len(out))
        keep = []
        for r in self.rockets:
            r["p"][1] += r["vy"] * dt
            d2 = ((self.P - r["p"]) ** 2).sum(1)
            a = np.exp(-d2 / 0.0016)
            acc += a[:, None] * (0.5 + 0.5 * self.pal(np.full(1, r["x"])))
            alpha += a
            if r["p"][1] >= r["top"]:
                self.shells.append({"c": r["p"].copy(), "age": 0.0, "x": r["x"]})
            else:
                keep.append(r)
        self.rockets = keep
        size = 0.12 + 0.35 * self.s("c1")
        sparkle = self.s("c2")
        keep = []
        for s in self.shells:
            s["age"] += dt
            age = s["age"]
            radius = size * (1 - np.exp(-age * 3.5))
            c = s["c"] - np.array([0, 0.12 * age * age, 0])
            d = np.linalg.norm(self.P - c, axis=1)
            fade = np.exp(-age * 1.4)
            a = np.clip(1 - np.abs(d - radius) / (0.03 + 0.25 * radius), 0, 1) * fade
            if sparkle > 0:
                a *= 1 - sparkle * self.rng.random(len(a)) ** 2
            acc += a[:, None] * self.pal(s["x"] + 0.15 * d)
            alpha += a
            if fade > 0.02:
                keep.append(s)
        self.shells = keep
        out[:] = self.color(1) * (1 - np.clip(alpha, 0, 1))[:, None] + np.clip(acc, 0, 1)


class Orbs(Volumetric):
    name = "orbs"
    label = "Orbs"
    description = "Glowing balls wander through the tree on looping paths."
    controls = {"speed": "Speed", "intensity": "Count", "c1": "Size"}
    defaults = {"intensity": 80, "c1": 110}
    default_palette = "rainbow"

    def on_setup(self) -> None:
        rng = np.random.default_rng(7)
        self.freq = rng.uniform(0.25, 0.7, (8, 3))
        self.ph = rng.uniform(0, 2 * np.pi, (8, 3))

    def render(self, t: float, out: np.ndarray) -> None:
        k = 1 + int(self.params.intensity * 8 // 256)
        size = 0.05 + 0.25 * self.s("c1")
        acc = np.zeros_like(out)
        alpha = np.zeros(len(out))
        for i in range(k):
            f, p = self.freq[i], self.ph[i]
            y = 0.5 + 0.42 * np.sin(self.T * f[1] + p[1])
            rad = self.rmax * (1.05 - y) * 0.75
            c = np.array([rad * np.sin(self.T * f[0] + p[0]), y, rad * np.cos(self.T * f[2] + p[2])])
            d2 = ((self.P - c) ** 2).sum(1)
            a = np.exp(-d2 / (size * size))
            acc += a[:, None] * self.pal(np.full(1, i / k + self.T * 0.02))
            alpha += a
        out[:] = self.color(1) * (1 - np.clip(alpha, 0, 1))[:, None] + np.clip(acc, 0, 1)


class Helix3D(Volumetric):
    name = "helix"
    label = "Helix 3D"
    description = "Tubes wind around the axis like a DNA strand and climb the tree."
    controls = {"speed": "Speed", "intensity": "Thickness", "c1": "Turns", "c2": "Strands"}
    defaults = {"intensity": 90, "c1": 70, "c2": 100}
    default_palette = "party"

    def render(self, t: float, out: np.ndarray) -> None:
        y = self.P[:, 1]
        strands = 1 + int(self.params.c2 * 4 // 256)
        turns = 0.5 + 6 * self.s("c1")
        thick = 0.02 + 0.13 * self.s("intensity")
        rc = self.rs * 0.8
        acc = np.zeros_like(out)
        alpha = np.zeros(len(out))
        for s in range(strands):
            phi = 2 * np.pi * (turns * y + s / strands) - self.T * 1.5
            cx, cz = rc * np.cos(phi), -rc * np.sin(phi)
            d = np.hypot(self.P[:, 0] - cx, self.P[:, 2] - cz)
            a = np.clip(1 - d / thick, 0, 1) ** 1.3
            acc += a[:, None] * self.pal(s / strands + 0.5 * y + self.T * 0.04)
            alpha += a
        out[:] = self.color(1) * (1 - np.clip(alpha, 0, 1))[:, None] + np.clip(acc, 0, 1)


class Vortex(Volumetric):
    name = "vortex"
    label = "Vortex"
    description = "Spiral arms swirl around the axis; inner and outer LEDs sit on different turns of the spiral."
    controls = {"speed": "Speed", "intensity": "Arms", "c1": "Radial twist", "c2": "Vertical twist"}
    defaults = {"intensity": 60, "c1": 180, "c2": 150}
    default_palette = "rainbow"

    def render(self, t: float, out: np.ndarray) -> None:
        arms = 1 + int(self.params.intensity * 8 // 256)
        rn = self.tree.r / max(self.tree.span, 1e-9) / self.rmax
        x = (arms * self.u + (self.s("c1") - 0.5) * 6 * rn + (self.s("c2") - 0.5) * 4 * self.P[:, 1]
             - self.T * 0.3)
        out[:] = self.pal(x)


class RotatingCube(Volumetric):
    name = "cube"
    label = "Rotating Cube"
    description = "The shell (or just the edges) of a cube tumbling inside the tree."
    controls = {"speed": "Speed", "intensity": "Size", "c1": "Thickness", "c2": "Edges only"}
    defaults = {"intensity": 140, "c1": 70, "c2": 0}
    default_palette = "rainbow"

    def render(self, t: float, out: np.ndarray) -> None:
        R = _rot_x(self.T * 0.45) @ _rot_y(self.T * 0.7)
        q = (self.P - self.center) @ R.T
        size = 0.12 + 0.38 * self.s("intensity")
        th = 0.02 + 0.1 * self.s("c1")
        aq = np.abs(q)
        srt = np.sort(aq, axis=1)
        if self.params.c2 >= 128:  # edges: the two largest coordinates are both at the surface
            a = np.clip(1 - np.abs(srt[:, 2] - size) / th, 0, 1) * np.clip(1 - np.abs(srt[:, 1] - size) / th, 0, 1)
            a *= srt[:, 2] < size + th
        else:
            a = np.clip(1 - np.abs(srt[:, 2] - size) / th, 0, 1)
        face = np.argmax(aq, axis=1) + 3 * (q[np.arange(len(q)), np.argmax(aq, axis=1)] < 0)
        self.over_bg(out, a, self.pal(face / 6 + self.T * 0.03))


EFFECTS_3D = [PlaneSweep, Lighthouse, Ripples3D, Plasma3D, Clouds3D, Fire3D, Snowfall, Fireworks3D, Orbs,
              Helix3D, Vortex, RotatingCube]
