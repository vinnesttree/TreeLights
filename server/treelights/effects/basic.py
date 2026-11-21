"""Basic effects that need no map: they work along the string's wiring order."""

from __future__ import annotations

import numpy as np

from .base import Effect


class Basic(Effect):
    category = "basic"
    color_slots = ("Color 1", "Background", "Color 3")


class Solid(Basic):
    name = "solid"
    label = "Solid"
    description = "Every LED in color 1."
    controls: dict[str, str] = {}
    color_slots = ("Color 1",)
    uses_palette = False

    def render(self, t: float, out: np.ndarray) -> None:
        out[:] = self.color(0)


class PaletteFlow(Basic):
    name = "palette"
    label = "Palette Flow"
    description = "The palette flows along the string."
    controls = {"speed": "Speed", "intensity": "Repeats"}
    defaults = {"intensity": 40}
    color_slots = ("Color 1", "Color 2", "Color 3")

    def render(self, t: float, out: np.ndarray) -> None:
        k = 0.25 + 6 * self.s("intensity")
        out[:] = self.pal(self.tree.chain * k - self.T * 0.2)


class Twinkle(Basic):
    name = "twinkle"
    label = "Twinkle"
    description = "Random LEDs fade in and out in palette colors over the background."
    controls = {"speed": "Speed", "intensity": "Density"}
    defaults = {"intensity": 110}
    default_palette = "party"

    def on_setup(self) -> None:
        n = self.tree.n
        self.age = np.full(n, -1.0)
        self.x = self.rng.random(n)

    def render(self, t: float, out: np.ndarray) -> None:
        live = self.age >= 0
        self.age[live] += self.dt * 0.8
        self.age[self.age > 1] = -1
        idle = np.flatnonzero(self.age < 0)
        p = self.dt * (0.05 + 1.5 * self.s("intensity"))
        if len(idle):
            start = idle[self.rng.random(len(idle)) < p * 0.5]
            self.age[start] = 0.0
            self.x[start] = self.rng.random(len(start))
        a = np.where(self.age >= 0, np.sin(np.pi * np.clip(self.age, 0, 1)), 0.0) ** 2
        self.over_bg(out, a, self.pal(self.x))


EFFECTS_BASIC = [Solid, PaletteFlow, Twinkle]
