"""Color palettes, WLED style.

A palette is a 256-entry gradient. Effects sample it with a position 0..1 that wraps. Besides the
fixed gradients there are palettes built from the three color slots ("Color 1", "Colors 1&2",
"Color Gradient", "Colors Only") and "Random Cycle", which blends to a new random palette every
few seconds. "Default" means the effect's own palette and is resolved by the effect.
"""

from __future__ import annotations

import colorsys

import numpy as np

Stop = tuple[int, str]  # (position 0..255, "#rrggbb")


def _even(hexes: list[str]) -> list[Stop]:
    """FastLED-style 16-entry palette: evenly spaced entries that wrap back to the first."""
    k = len(hexes)
    return [(round(i * 256 / k), c) for i, c in enumerate(hexes)]


# The first seven are FastLED's built-in palettes (also WLED's), the rest are ours.
GRADIENTS: dict[str, tuple[str, list[Stop], bool]] = {
    # id: (label, stops, wraps)
    "rainbow": ("Rainbow", _even(["#ff0000", "#d52a00", "#ab5500", "#ab7f00", "#abab00", "#56d500", "#00ff00", "#00d52a",
                                  "#00ab55", "#0056aa", "#0000ff", "#2a00d5", "#5500ab", "#7f0081", "#ab0055", "#d5002b"]), True),
    "rainbow_bands": ("Rainbow Bands", _even(["#ff0000", "#000000", "#ab5500", "#000000", "#abab00", "#000000", "#00ff00", "#000000",
                                              "#00ab55", "#000000", "#0000ff", "#000000", "#5500ab", "#000000", "#ab0055", "#000000"]), True),
    "party": ("Party", _even(["#5500ab", "#84007c", "#b5004b", "#e5001b", "#e81700", "#b84700", "#ab7700", "#abab00",
                              "#ab5500", "#dd2200", "#f2000e", "#c2003e", "#8f0071", "#5f00a1", "#2f00d0", "#0007f9"]), True),
    "cloud": ("Cloud", _even(["#0000ff", "#00008b", "#00008b", "#00008b", "#00008b", "#00008b", "#00008b", "#00008b",
                              "#0000ff", "#00008b", "#87ceeb", "#87ceeb", "#add8e6", "#ffffff", "#add8e6", "#87ceeb"]), True),
    "lava": ("Lava", _even(["#000000", "#800000", "#000000", "#800000", "#8b0000", "#8b0000", "#800000", "#8b0000",
                            "#8b0000", "#8b0000", "#ff0000", "#ffa500", "#ffffff", "#ffa500", "#ff0000", "#8b0000"]), True),
    "ocean": ("Ocean", _even(["#191970", "#00008b", "#191970", "#000080", "#00008b", "#0000cd", "#2e8b57", "#008080",
                              "#5f9ea0", "#0000ff", "#008b8b", "#6495ed", "#7fffd4", "#2e8b57", "#00ffff", "#87cefa"]), True),
    "forest": ("Forest", _even(["#006400", "#006400", "#556b2f", "#006400", "#008000", "#228b22", "#6b8e23", "#008000",
                                "#2e8b57", "#66cdaa", "#32cd32", "#9acd32", "#90ee90", "#7cfc00", "#66cdaa", "#228b22"]), True),
    "heat": ("Fire", [(0, "#000000"), (40, "#330000"), (85, "#990000"), (120, "#ff0000"), (160, "#ff6600"),
                      (200, "#ffcc00"), (230, "#ffff66"), (255, "#ffffff")], False),
    "sunset": ("Sunset", [(0, "#1a0033"), (60, "#7a0045"), (120, "#e3242b"), (180, "#ff8c1a"), (230, "#ffe066"), (255, "#1a0033")], True),
    "ice": ("Ice", [(0, "#000a28"), (90, "#0040c0"), (170, "#40c8ff"), (220, "#ffffff"), (255, "#000a28")], True),
    "icefire": ("Ice & Fire", [(0, "#000000"), (50, "#002080"), (110, "#00c0ff"), (128, "#ffffff"), (146, "#ffc000"),
                               (206, "#ff3000"), (255, "#000000")], True),
    "candy_cane": ("Candy Cane", [(0, "#ff0000"), (127, "#ff0000"), (128, "#ffffff"), (255, "#ffffff")], True),
    "christmas": ("Christmas", [(0, "#ff0000"), (127, "#ff0000"), (128, "#00b000"), (255, "#00b000")], True),
    "holly": ("Holly", [(0, "#003a0c"), (90, "#00801c"), (128, "#d00000"), (166, "#00801c"), (255, "#003a0c")], True),
    "bulbs": ("Classic Bulbs", [(0, "#ff1000"), (63, "#ff1000"), (64, "#ff7000"), (127, "#ff7000"), (128, "#00a010"),
                                (191, "#00a010"), (192, "#1020ff"), (255, "#1020ff")], True),
    "aurora": ("Aurora", [(0, "#001a10"), (60, "#00c060"), (115, "#00ffa0"), (170, "#2040ff"), (215, "#a020ff"), (255, "#001a10")], True),
    "pastel": ("Pastel", [(0, "#ff9aa2"), (64, "#ffdac1"), (128, "#e2f0cb"), (192, "#b5ead7"), (240, "#c7ceea"), (255, "#ff9aa2")], True),
    "autumn": ("Autumn", [(0, "#5a1a00"), (80, "#c04000"), (150, "#ff9000"), (200, "#c0a000"), (255, "#5a1a00")], True),
    "sakura": ("Sakura", [(0, "#ff4080"), (100, "#ffb0c8"), (160, "#ffffff"), (220, "#ff80a0"), (255, "#ff4080")], True),
    "beach": ("Beach", [(0, "#f0d080"), (90, "#40e0d0"), (170, "#0060c0"), (255, "#f0d080")], True),
    "orange_teal": ("Orange & Teal", [(0, "#008c8c"), (100, "#00c8b4"), (155, "#ff8c00"), (220, "#ff5000"), (255, "#008c8c")], True),
    "temperature": ("Temperature", [(0, "#1030ff"), (85, "#00c0ff"), (128, "#ffffff"), (170, "#ffc040"), (255, "#ff2000")], False),
    "warm_white": ("Warm White", [(0, "#ff6a10"), (128, "#ffb060"), (200, "#ffe0b0"), (255, "#ff6a10")], True),
    "gold": ("Gold", [(0, "#4a2a00"), (100, "#c08000"), (170, "#ffd040"), (220, "#fff0b0"), (255, "#4a2a00")], True),
    "toxic": ("Toxic", [(0, "#002000"), (100, "#40ff00"), (170, "#c0ff00"), (255, "#002000")], True),
    "magenta_yellow": ("Magenta & Yellow", [(0, "#ff00a0"), (128, "#ff6040"), (200, "#ffe000"), (255, "#ff00a0")], True),
    "midnight": ("Midnight", [(0, "#000010"), (100, "#100060"), (180, "#6000a0"), (255, "#000010")], True),
}

# Palettes that depend on the color slots or on time. "default" is resolved by the effect.
DYNAMIC: dict[str, str] = {
    "default": "Default",
    "random": "* Random Cycle",
    "color1": "* Color 1",
    "colors12": "* Colors 1&2",
    "colorgrad": "* Color Gradient",
    "colorsonly": "* Colors Only",
}


def _hex(c: str) -> tuple[float, float, float]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def build_lut(stops: list[tuple[int, tuple[float, float, float]]], wraps: bool) -> np.ndarray:
    """(256,3) float gradient. Stops at the same position give a hard edge."""
    stops = sorted(stops, key=lambda s: s[0])
    if wraps:
        stops = stops + [(256, stops[0][1])]
    elif stops[-1][0] < 255:
        stops = stops + [(255, stops[-1][1])]
    if stops[0][0] > 0:
        stops = [(0, stops[0][1])] + stops
    xs = np.arange(256, dtype=float)
    lut = np.zeros((256, 3))
    for i in range(len(stops) - 1):
        (p0, c0), (p1, c1) = stops[i], stops[i + 1]
        sel = (xs >= p0) & (xs < p1) if i < len(stops) - 2 else (xs >= p0) & (xs <= p1)
        if not sel.any():
            continue
        f = ((xs[sel] - p0) / max(p1 - p0, 1e-9))[:, None]
        lut[sel] = np.asarray(c0) * (1 - f) + np.asarray(c1) * f
    return lut


def color_stops(kind: str, colors) -> tuple[list[tuple[int, tuple[float, float, float]]], bool]:
    c1, c2, c3 = (tuple(np.asarray(c, float) / 255.0) for c in colors[:3])
    if kind == "color1":
        return [(0, c1), (255, c1)], False
    if kind == "colors12":
        return [(0, c1), (127, c1), (128, c2), (255, c2)], False
    if kind == "colorgrad":
        return [(0, c1), (128, c2), (255, c3)], False
    if kind == "colorsonly":
        return [(0, c1), (84, c1), (85, c2), (169, c2), (170, c3), (255, c3)], False
    raise KeyError(kind)


def _random_lut(rng: np.random.Generator) -> np.ndarray:
    h0 = rng.random()
    stops = []
    for i in range(4):
        h = (h0 + i * rng.uniform(0.12, 0.35)) % 1.0
        s = rng.uniform(0.75, 1.0)
        v = rng.uniform(0.5, 1.0) if i % 2 else 1.0
        stops.append((i * 64, colorsys.hsv_to_rgb(h, s, v)))
    return build_lut(stops, True)


class PaletteBank:
    """Per-effect palette cache. Cheap to call every frame."""

    RANDOM_HOLD_S = 5.0
    RANDOM_BLEND_S = 1.5

    def __init__(self, seed: int | None = None):
        self._cache: dict[tuple, np.ndarray] = {}
        self._rng = np.random.default_rng(seed)
        self._rand_from = _random_lut(self._rng)
        self._rand_to = _random_lut(self._rng)
        self._rand_t0: float | None = None

    def lut(self, name: str, colors, t: float = 0.0) -> np.ndarray:
        if name == "random":
            return self._random(t)
        key = (name, tuple(map(tuple, colors)) if name in DYNAMIC else None)
        lut = self._cache.get(key)
        if lut is None:
            if name in GRADIENTS:
                _, stops, wraps = GRADIENTS[name]
                lut = build_lut([(p, _hex(c)) for p, c in stops], wraps)
            elif name in DYNAMIC and name != "default":
                lut = build_lut(*color_stops(name, colors))
            else:
                _, stops, wraps = GRADIENTS["rainbow"]
                lut = build_lut([(p, _hex(c)) for p, c in stops], wraps)
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = lut
        return lut

    def _random(self, t: float) -> np.ndarray:
        if self._rand_t0 is None or t < self._rand_t0:
            self._rand_t0 = t
        age = t - self._rand_t0
        if age >= self.RANDOM_HOLD_S:
            self._rand_from, self._rand_to = self._rand_to, _random_lut(self._rng)
            self._rand_t0, age = t, 0.0
        f = min(age / self.RANDOM_BLEND_S, 1.0)
        return self._rand_from * (1 - f) + self._rand_to * f

    def sample(self, name: str, colors, t: float, x) -> np.ndarray:
        lut = self.lut(name, colors, t)
        p = (np.asarray(x, float) % 1.0) * 256.0
        i0 = np.floor(p).astype(int) % 256
        f = (p - np.floor(p))[..., None]
        return lut[i0] * (1 - f) + lut[(i0 + 1) % 256] * f


def catalogue() -> list[dict]:
    """Palettes for the UI. Fixed gradients carry their stops so the UI can draw a swatch;
    dynamic ones carry only their kind."""
    out = [{"id": k, "name": v, "dynamic": True} for k, v in DYNAMIC.items()]
    out += [{"id": k, "name": label, "dynamic": False, "wraps": wraps, "stops": [[p, c] for p, c in stops]}
            for k, (label, stops, wraps) in GRADIENTS.items()]
    return out
