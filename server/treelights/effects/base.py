"""Effect interface, effect parameters and the test animations (architecture §12)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .palettes import PaletteBank

# ---------------------------------------------------------------------------------------------
# Geometry


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
    rs: np.ndarray | None = None  # surface radius at each LED's height (map units)
    lo: float = 0.0  # height of the lowest LED
    span: float = 1.0  # height of the highest LED above the lowest

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
        rs = np.interp(h, sh, sr) if len(sh) else np.maximum(r, 1e-9)
        return TreeGeometry(n, pos, has, [l["status"] for l in leds], h, (h - lo) / max(hi - lo, 1e-9),
                            np.arctan2(-pos[:, 2], pos[:, 0]), r, r / np.maximum(rs, 1e-9), np.arange(n) / max(n - 1, 1),
                            rs=rs, lo=float(lo), span=float(max(hi - lo, 1e-9)))

    @staticmethod
    def chain_only(n: int) -> "TreeGeometry":
        z = np.zeros(n)
        return TreeGeometry(n, np.zeros((n, 3)), np.zeros(n, bool), ["unknown"] * n, z, z, z, z, z, np.arange(n) / max(n - 1, 1))


# ---------------------------------------------------------------------------------------------
# Parameters (WLED-style: speed, intensity, three custom sliders, a palette and three colors)

SLIDERS = ("speed", "intensity", "c1", "c2", "c3")
DEFAULT_COLORS = ((255, 160, 0), (0, 0, 0), (0, 0, 0))  # WLED's defaults: orange, black, black


def _color(v, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if isinstance(v, str):
        s = v.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) != 6:
            raise ValueError(f"bad color {v!r}")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return tuple(int(np.clip(round(float(c)), 0, 255)) for c in v[:3])  # type: ignore[return-value]
    if v is None:
        return default
    raise ValueError(f"bad color {v!r}")


@dataclass(frozen=True)
class Params:
    speed: int = 128
    intensity: int = 128
    c1: int = 128
    c2: int = 128
    c3: int = 0
    palette: str = "default"
    colors: tuple[tuple[int, int, int], ...] = DEFAULT_COLORS

    def merged(self, d: dict | None) -> "Params":
        """A copy with the fields in `d` replaced. Unknown keys are ignored; bad values raise ValueError."""
        if not d:
            return self
        kw: dict = {}
        for k in SLIDERS:
            if k in d and d[k] is not None:
                kw[k] = int(np.clip(round(float(d[k])), 0, 255))
        if d.get("palette") is not None:
            kw["palette"] = str(d["palette"])
        if d.get("colors") is not None:
            cs = list(d["colors"])[:3]
            kw["colors"] = tuple(_color(cs[i] if i < len(cs) else None, self.colors[i]) for i in range(3))
        return Params(**{**self.__dict__, **kw})

    def to_dict(self) -> dict:
        return {**{k: getattr(self, k) for k in SLIDERS}, "palette": self.palette,
                "colors": ["#%02x%02x%02x" % c for c in self.colors]}


# ---------------------------------------------------------------------------------------------
# Effects


class Effect:
    """An effect renders float RGB 0..1 into `out` (n,3) for time `t` in seconds.

    Library effects describe themselves for the UI: `label`, `category` ("3d", "surface", "basic"
    or "test"), the sliders they use (`controls`: slider -> label), the color slots they use
    (`color_slots`), their own palette for "Default", and slider defaults. Animated effects read
    `self.T`, effect time scaled by the speed slider, so changing the speed never jumps."""

    name = "effect"
    label = ""
    category = "test"
    description = ""
    needs_map = False
    controls: dict[str, str] = {}
    color_slots: tuple[str, ...] = ()
    default_palette = "rainbow"
    defaults: dict = {}
    uses_palette = True

    def __init__(self, params: Params | dict | None = None):
        base = Params().merged(self.defaults)
        self.params = params if isinstance(params, Params) else base.merged(params)
        self.palettes = PaletteBank()
        self.T = 0.0
        self.dt = 0.0
        self.t = 0.0
        self._last: float | None = None

    @classmethod
    def describe(cls) -> dict:
        return {"name": cls.name, "label": cls.label or cls.name.title(), "category": cls.category,
                "description": cls.description, "needs_map": cls.needs_map, "controls": cls.controls,
                "color_slots": list(cls.color_slots), "default_palette": cls.default_palette,
                "uses_palette": cls.uses_palette,
                "defaults": Params().merged(cls.defaults).to_dict()}

    # -- lifecycle ------------------------------------------------------------------------------
    def setup(self, tree: TreeGeometry) -> None:
        self.tree = tree
        span = tree.span or 1.0
        # Normalised coordinates: y 0..1 from the lowest to the highest LED, x and z in the same units.
        self.P = np.c_[tree.pos[:, 0] / span, (tree.pos[:, 1] - tree.lo) / span, tree.pos[:, 2] / span]
        has = tree.has_pos
        self.center = np.array([0.0, float(self.P[has, 1].mean()) if has.any() else 0.5, 0.0])
        rn = tree.r / span
        self.rmax = float(rn[has].max()) if has.any() else 0.5
        self.rmax = max(self.rmax, 1e-3)
        self.rs = (tree.rs / span) if tree.rs is not None else np.full(tree.n, self.rmax)
        self.u = (tree.theta / (2 * np.pi)) % 1.0  # 0..1 around the axis
        self.rng = np.random.default_rng()
        self.on_setup()

    def on_setup(self) -> None:
        pass

    def set_params(self, d: dict | None) -> None:
        self.params = self.params.merged(d)

    def rate(self) -> float:
        """Speed slider -> time multiplier: 128 is 1x, 0 about 0.1x, 255 about 9x."""
        return float(2 ** ((self.params.speed - 128) / 40))

    def frame(self, t: float, out: np.ndarray) -> None:
        """Advance effect time and render. The output thread calls this; `render` is the effect body."""
        dt = 0.0 if self._last is None else min(max(t - self._last, 0.0), 0.25)
        self._last = t
        self.t = t
        self.dt = dt * self.rate()
        self.T += self.dt
        self.render(t, out)
        if self.needs_map:
            out[~self.tree.has_pos] = 0

    def render(self, t: float, out: np.ndarray) -> None:  # out: (n,3) float 0..1
        raise NotImplementedError

    # -- helpers for library effects -------------------------------------------------------------
    def s(self, key: str) -> float:
        """A slider as 0..1."""
        return getattr(self.params, key) / 255.0

    def color(self, i: int) -> np.ndarray:
        return np.asarray(self.params.colors[i], float) / 255.0

    def pal(self, x: np.ndarray) -> np.ndarray:
        """Palette colors (len(x),3) for positions x; positions wrap at 1 as in WLED."""
        name = self.params.palette if self.params.palette != "default" else self.default_palette
        return self.palettes.sample(name, self.params.colors, self.t, x)

    def over_bg(self, out: np.ndarray, alpha: np.ndarray, rgb: np.ndarray) -> None:
        """out = background (color slot 2) blended with rgb by alpha."""
        a = np.clip(alpha, 0, 1)[:, None]
        out[:] = self.color(1)[None, :] * (1 - a) + rgb * a


class SweepTest(Effect):
    """Rising plane -> rotating half-plane -> growing sphere, looping (6 s each)."""

    name = "sweep"
    PHASE = 6.0
    needs_map = True
    uses_palette = False

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
    needs_map = True
    uses_palette = False
    COLORS = {"measured": (0, 1, 0), "single_view": (1, 0.8, 0), "estimated": (1, 0.3, 0), "manual": (0, 0.3, 1), "unknown": (0, 0, 0)}

    def render(self, t: float, out: np.ndarray) -> None:
        for i, s in enumerate(self.tree.status):
            out[i] = self.COLORS.get(s, (0, 0, 0))


class Chase(Effect):
    """Index chase: works before any map exists."""

    name = "chase"
    uses_palette = False

    def render(self, t: float, out: np.ndarray) -> None:
        n = self.tree.n
        head = (t * 30) % n
        d = (head - np.arange(n)) % n
        out[:] = np.clip(1 - d / 8, 0, 1)[:, None] * [1.0, 1.0, 1.0]


class Identify(Effect):
    """Blink one LED, or a set of them (the rest off)."""

    name = "identify"
    uses_palette = False

    def __init__(self, index: int | list[int]):
        super().__init__()
        self.indices = [int(index)] if isinstance(index, (int, np.integer)) else [int(i) for i in index]
        self.index = self.indices[0] if self.indices else -1

    def render(self, t: float, out: np.ndarray) -> None:
        out[:] = 0
        if (t * 2) % 1 < 0.6:
            idx = [i for i in self.indices if 0 <= i < len(out)]
            out[idx] = (1, 1, 1)


class Renderer:
    """An effect as an output-thread renderer: float 0..1 -> gamma -> uint8.
    `brightness` can be changed while it runs."""

    def __init__(self, effect: Effect, brightness: float, gamma: float = 2.2):
        self.effect = effect
        self.brightness = float(brightness)
        self.gamma = gamma
        self.buf = np.zeros((effect.tree.n, 3))
        self.t0: float | None = None

    def __call__(self, now: float) -> np.ndarray:
        if self.t0 is None:
            self.t0 = now
        self.buf[:] = 0
        self.effect.frame(now - self.t0, self.buf)
        v = np.clip(np.nan_to_num(self.buf), 0, 1) ** self.gamma * 255 * self.brightness
        return v.astype(np.uint8)


def make_renderer(effect: Effect, brightness: float, gamma: float = 2.2) -> Renderer:
    return Renderer(effect, brightness, gamma)


__all__ = ["TreeGeometry", "Params", "Effect", "SweepTest", "StatusColors", "Chase", "Identify",
           "Renderer", "make_renderer", "SLIDERS"]
