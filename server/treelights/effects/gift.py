"""Effects inspired by Matt Parker's (Stand-up Maths) 3D-mapped Christmas tree and the animations
the community made for it (github.com/standupmaths/xmastree2020 and xmastree2021).

Those animations were shared as pre-rendered frames for Matt's own 500 LEDs, so they can't be
played on another tree. These are new implementations of the same ideas, computed live from this
tree's map, with WLED-style sliders, colors and palettes:

- Two-Tone Spin: Matt's 2020 animation, a plane turning through the tree with a different
  color on each side.
- Bouncing Balls ("bouncy-ball"), Explosions ("explosion", "explosions-v2"), Rainbow Explosion
  and Implosion, Rainbow Ripple, Rainbow Spin/Corkscrew, Equalizer, Color Lerp ("lerp"),
  Particle Accelerator, Pulsing Heart, Santa Hat and Morse Xmas, after the community examples
  of the same names.
"""

from __future__ import annotations

import numpy as np

from .base import Effect


class Gift(Effect):
    category = "gift"
    needs_map = True
    color_slots = ("Color 1", "Background", "Color 3")

    def glow(self, centers: np.ndarray, size: float) -> np.ndarray:
        """(n,k): a solid ball of radius `size` around each center, with a soft edge. Solid balls
        read better than a Gaussian glow on a tree, where LEDs are several cm apart."""
        if len(centers) == 0:
            return np.zeros((len(self.P), 0))
        d = np.sqrt(((self.P[:, None, :] - centers[None, :, :]) ** 2).sum(-1))
        return np.clip((1 - d / size) / 0.35, 0, 1)

    def add(self, out: np.ndarray, w: np.ndarray, colors: np.ndarray) -> None:
        """Additive light from k sources (w: (n,k), colors: (k,3)) over the background."""
        a = np.clip(w.sum(1), 0, 1)[:, None]
        out[:] = self.color(1) * (1 - a) + np.clip(w @ colors, 0, 1)

    def cone_radius(self, y: np.ndarray | float) -> np.ndarray:
        """Surface radius at normalised height y (from the map's surface model, via the LEDs)."""
        has = self.tree.has_pos
        if not has.any():
            return np.asarray(y) * 0 + self.rmax
        order = np.argsort(self.P[has, 1])
        return np.interp(y, self.P[has, 1][order], self.rs[has][order])


class TwoToneSpin(Gift):
    name = "twotone"
    label = "Two-Tone Spin"
    description = ("Matt Parker's 2020 tree animation: a plane spins through the tree with a different color on "
                   "each side, while the colors slowly shift.")
    controls = {"speed": "Speed", "intensity": "Color shift", "c1": "Tilt", "c2": "Edge softness"}
    defaults = {"intensity": 40, "c1": 50, "c2": 30}
    default_palette = "christmas"

    def render(self, t: float, out: np.ndarray) -> None:
        a = self.T * 1.2
        tilt = (self.s("c1") - 0.5) * np.pi * 0.9
        # Normal of a plane through the axis, turned by `a` and tilted out of the vertical.
        nrm = np.array([np.cos(a) * np.cos(tilt), np.sin(tilt), np.sin(a) * np.cos(tilt)])
        d = (self.P - self.center) @ nrm
        soft = 0.005 + 0.2 * self.s("c2")
        side = np.clip(0.5 + d / (2 * soft), 0, 1)[:, None]
        shift = self.T * 0.05 * self.s("intensity") * 4
        out[:] = self.pal(np.full(len(d), 0.25 + shift)) * side + self.pal(np.full(len(d), 0.75 + shift)) * (1 - side)


class BouncingBalls(Gift):
    name = "bouncy"
    label = "Bouncing Balls"
    description = "Balls drop from the top, fall under gravity and bounce on the floor until they come to rest."
    controls = {"speed": "Speed", "intensity": "Balls", "c1": "Size", "c2": "Bounciness"}
    defaults = {"intensity": 100, "c1": 130, "c2": 190}
    default_palette = "rainbow"
    G = 2.5  # gravity, tree heights per second^2

    def on_setup(self) -> None:
        self.balls = np.zeros((0, 8))  # x y z vx vy vz palette-x fade
        self.spawn = 1.0

    def _drop(self) -> np.ndarray:
        y = self.rng.uniform(0.85, 1.1)
        a = self.rng.uniform(0, 2 * np.pi)
        r = float(self.cone_radius(min(y, 1.0))) * 0.5 + self.rmax * 0.4 * self.rng.random()
        sp = self.rng.uniform(0.05, 0.2)
        return np.array([r * np.cos(a), y, r * np.sin(a), sp * np.cos(a + 2), 0.0, sp * np.sin(a + 2),
                         self.rng.random(), 1.0])

    def render(self, t: float, out: np.ndarray) -> None:
        want = 1 + int(self.params.intensity * 8 // 256)
        self.spawn += self.dt * 0.8
        if len(self.balls) < want and self.spawn >= 1:
            self.spawn = 0.0
            self.balls = np.r_[self.balls, self._drop()[None]]
        b = self.balls
        size = 0.05 + 0.15 * self.s("c1")
        e = 0.45 + 0.5 * self.s("c2")  # coefficient of restitution
        steps = max(1, int(np.ceil(self.dt / 0.01)))
        h = self.dt / steps
        for _ in range(steps):
            b[:, 4] -= self.G * h
            b[:, :3] += b[:, 3:6] * h
            low = b[:, 1] < size * 0.6
            if low.any():
                b[low, 1] = size * 0.6
                b[low, 4] = np.abs(b[low, 4]) * e
                b[low, 3] *= 0.9
                b[low, 5] *= 0.9
            # Keep balls inside the tree: bounce off the cone.
            rr = np.hypot(b[:, 0], b[:, 2])
            lim = np.maximum(self.cone_radius(np.clip(b[:, 1], 0, 1)) * 1.05, self.rmax * 0.15)
            out_ = rr > lim
            if out_.any():
                nx, nz = b[out_, 0] / rr[out_], b[out_, 2] / rr[out_]
                vr = b[out_, 3] * nx + b[out_, 5] * nz
                push = np.maximum(vr, 0)
                b[out_, 3] -= 2 * push * nx
                b[out_, 5] -= 2 * push * nz
                b[out_, 0] = nx * lim[out_]
                b[out_, 2] = nz * lim[out_]
        # A ball that has come to rest fades out and makes room for the next.
        resting = (b[:, 1] <= size * 0.61) & (np.abs(b[:, 4]) < 0.15)
        b[resting, 7] -= self.dt * 1.5
        self.balls = b[b[:, 7] > 0]
        b = self.balls
        w = self.glow(b[:, :3], size) * np.clip(b[:, 7], 0, 1)[None, :]
        self.add(out, w, self.pal(b[:, 6]))


class Explosions(Gift):
    name = "explosions"
    label = "Explosions"
    description = "Fireballs burst at random points in the tree: a bright core swells, then cools through the palette and fades."
    controls = {"speed": "Speed", "intensity": "Rate", "c1": "Size"}
    defaults = {"intensity": 90, "c1": 140}
    default_palette = "heat"

    def on_setup(self) -> None:
        self.booms: list[list] = []  # [center, age]
        self.spawn = 1.0

    def render(self, t: float, out: np.ndarray) -> None:
        self.spawn += self.dt * (0.2 + 2.0 * self.s("intensity"))
        has = np.flatnonzero(self.tree.has_pos)
        if self.spawn >= 1 and len(self.booms) < 8 and len(has):
            self.spawn = 0.0
            self.booms.append([self.P[has[self.rng.integers(len(has))]].copy(), 0.0])
        size = 0.12 + 0.4 * self.s("c1")
        heat = np.zeros(len(out))
        for bm in self.booms:
            bm[1] += self.dt * 0.9
            age = bm[1]
            radius = size * (1 - np.exp(-age * 6))
            d = np.linalg.norm(self.P - bm[0], axis=1)
            core = np.clip(1 - d / max(radius, 1e-3), 0, 1) ** 0.6  # 1 at the center, 0 at the edge
            shell = np.clip(1 - np.abs(d - radius) / (0.04 + 0.15 * radius), 0, 1)
            temp = np.maximum(core, 0.8 * shell) * np.clip(1.15 - age, 0, 1)
            heat = np.maximum(heat, temp)
        self.booms = [bm for bm in self.booms if bm[1] < 1.2]
        self.over_bg(out, np.clip(heat * 4, 0, 1), self.pal(heat * 0.96))


class RainbowBurst(Gift):
    name = "rainbow_explosion"
    label = "Rainbow Explosion"
    description = "A rainbow bursts out from the middle of the tree to its edges, again and again."
    controls = {"speed": "Speed", "intensity": "Band width", "c1": "Center height"}
    defaults = {"intensity": 110, "c1": 110}
    default_palette = "rainbow"
    inward = False

    def on_setup(self) -> None:
        self.cycle = 0

    def render(self, t: float, out: np.ndarray) -> None:
        c = np.array([0.0, 0.1 + 0.8 * self.s("c1"), 0.0])
        d = np.linalg.norm(self.P - c, axis=1)
        dmax = float(d[self.tree.has_pos].max()) if self.tree.has_pos.any() else 1.0
        phase = (self.T * 0.4) % 1.0
        front = phase * (dmax + 0.3)
        if self.inward:
            front = dmax + 0.3 - front
            d = -d
            front = -front
        width = 0.1 + 0.6 * self.s("intensity")
        behind = front - d  # how far the front has passed this LED
        a = np.clip(behind / 0.03, 0, 1) * np.clip(1 - behind / (width * 2.5), 0, 1)
        self.over_bg(out, a, self.pal(behind / width + int(self.T * 0.4) * 0.37))


class RainbowImplosion(RainbowBurst):
    name = "rainbow_implosion"
    label = "Rainbow Implosion"
    description = "A rainbow sweeps in from the edges of the tree and collapses into the middle."
    inward = True


class RainbowRipple(Gift):
    name = "rainbow_ripple"
    label = "Rainbow Ripple"
    description = "Rainbow rings spread out in 3D from a point in the tree, like ripples on a pond."
    controls = {"speed": "Speed", "intensity": "Ring spacing", "c1": "Center height"}
    defaults = {"intensity": 100, "c1": 200}
    default_palette = "rainbow"

    def render(self, t: float, out: np.ndarray) -> None:
        c = np.array([0.0, self.s("c1"), 0.0])
        d = np.linalg.norm(self.P - c, axis=1)
        k = 0.5 + 4 * (1 - self.s("intensity"))
        out[:] = self.pal(d * k - self.T * 0.5)


class RainbowSpin(Gift):
    name = "rainbow_spin"
    label = "Rainbow Spin / Corkscrew"
    description = "The rainbow wraps around the tree and spins. Add twist to turn it into a corkscrew."
    controls = {"speed": "Speed", "intensity": "Rainbows around", "c1": "Twist"}
    defaults = {"intensity": 0, "c1": 128}
    default_palette = "rainbow"

    def render(self, t: float, out: np.ndarray) -> None:
        n = 1 + int(self.params.intensity * 4 // 256)
        twist = (self.s("c1") - 0.5) * 6
        out[:] = self.pal(n * self.u + twist * self.P[:, 1] - self.T * 0.4)


class Equalizer(Gift):
    name = "equalizer"
    label = "Equalizer"
    description = "The tree becomes a 3D audio meter: slices around it rise and fall like equalizer bars, with peak markers."
    controls = {"speed": "Speed", "intensity": "Bars", "c1": "Liveliness"}
    defaults = {"intensity": 100}
    default_palette = "vu"

    def on_setup(self) -> None:
        self.level = np.zeros(16)
        self.target = np.zeros(16)
        self.peak = np.zeros(16)
        self.hold = np.zeros(16)

    def render(self, t: float, out: np.ndarray) -> None:
        nb = 3 + int(self.params.intensity * 14 // 256)
        live = 0.5 + 3 * self.s("c1")
        dt = self.dt
        new = self.rng.random(16) < dt * live * 3
        # Low "frequencies" (first bars) tend to be louder, like real music.
        bias = np.linspace(1.0, 0.6, 16)
        self.target[new] = np.clip(self.rng.random(new.sum()) ** 0.7 * bias[new], 0.05, 1)
        self.level += (self.target - self.level) * min(1.0, dt * 12)
        self.target *= np.exp(-dt * live)
        rise = self.level > self.peak
        self.peak[rise], self.hold[rise] = self.level[rise], 0.6
        self.hold -= dt
        fall = self.hold < 0
        self.peak[fall] = np.maximum(self.level[fall], self.peak[fall] - dt * 0.5)
        bar = np.floor(self.u * nb).astype(int) % nb
        gap = np.abs(((self.u * nb) % 1.0) - 0.5) < 0.42  # a thin dark line between bars
        v = self.P[:, 1]
        lit = (v <= self.level[bar]) & gap
        tip = (np.abs(v - self.peak[bar]) < 0.03) & gap
        a = lit.astype(float)
        col = self.pal(v * 0.98)
        col[tip] = 1.0
        a[tip] = 1.0
        self.over_bg(out, a, col)


class ColorLerp(Gift):
    name = "lerp"
    label = "Color Lerp"
    description = "The whole tree glides from one palette color to the next. Wipe makes the new color sweep through the tree."
    controls = {"speed": "Speed", "intensity": "Hold time", "c1": "Wipe"}
    defaults = {"intensity": 60, "c1": 0}
    default_palette = "party"

    def on_setup(self) -> None:
        self.a, self.b = self.rng.random(), self.rng.random()
        self.age = 0.0
        self.dir = np.array([0.0, 1.0, 0.0])

    def render(self, t: float, out: np.ndarray) -> None:
        hold = 0.3 + 3 * self.s("intensity")
        span = 1.0 + hold
        self.age += self.dt * 0.5
        if self.age >= span:
            self.age -= span
            self.a, self.b = self.b, (self.b + self.rng.uniform(0.2, 0.8)) % 1.0
            v = self.rng.normal(size=3)
            self.dir = v / np.linalg.norm(v)
        f = min(self.age, 1.0)
        wipe = self.s("c1")
        if wipe > 0:
            proj = (self.P - self.center) @ self.dir
            lo, hi = (proj[self.tree.has_pos].min(), proj[self.tree.has_pos].max()) if self.tree.has_pos.any() else (0, 1)
            x = (proj - lo) / max(hi - lo, 1e-9)
            soft = 0.05 + 0.95 * (1 - wipe)
            mix = np.clip((f * (1 + soft) - x) / soft, 0, 1)
        else:
            mix = np.full(len(out), f)
        mix = mix * mix * (3 - 2 * mix)
        ca = self.pal(np.array([self.a]))[0]
        cb = self.pal(np.array([self.b]))[0]
        out[:] = ca * (1 - mix[:, None]) + cb * mix[:, None]


class ParticleAccelerator(Gift):
    name = "accelerator"
    label = "Particle Accelerator"
    description = ("Particles race around a ring inside the tree with glowing trails, speeding up until they "
                   "collide in a flash, then it starts again.")
    controls = {"speed": "Speed", "intensity": "Particles", "c1": "Ring height", "c2": "Trail length"}
    defaults = {"intensity": 40, "c1": 100, "c2": 160}
    default_palette = "icefire"

    def on_setup(self) -> None:
        self.trail = np.zeros((self.tree.n, 3))
        self.angle = np.zeros(8)
        self.vel = 0.0
        self.flash = 0.0

    def render(self, t: float, out: np.ndarray) -> None:
        k = 1 + int(self.params.intensity * 6 // 256)
        if len(self.angle) != k or self.vel == 0:
            self.angle = np.linspace(0, 2 * np.pi, k, endpoint=False)
            self.vel = 1.0
        y = 0.1 + 0.7 * self.s("c1")
        r = float(self.cone_radius(y)) * 0.7
        self.vel *= np.exp(self.dt * 0.35)  # accelerate
        dirs = np.where(np.arange(k) % 2 == 0, 1.0, -1.0)  # counter-rotating beams
        self.angle += dirs * self.vel * self.dt
        if self.vel > 14:
            self.flash, self.vel = 1.0, 0.0
        centers = np.c_[r * np.cos(self.angle), np.full(k, y), -r * np.sin(self.angle)]
        w = self.glow(centers, 0.06 + 0.04 * self.s("intensity"))
        col = self.pal(0.4 + 0.25 * np.arange(k) / max(k, 1))
        decay = np.exp(-self.dt * (0.4 + 8 * (1 - self.s("c2"))))
        self.trail = np.maximum(self.trail * decay, np.clip(w @ col, 0, 1))
        out[:] = self.trail
        if self.flash > 0:
            d = np.linalg.norm(self.P - np.array([0, y, 0]), axis=1)
            boom = np.clip(1 - np.abs(d - (1 - self.flash) * 0.9) / 0.15, 0, 1) * self.flash
            out[:] = np.maximum(out, boom[:, None])
            self.flash = max(0.0, self.flash - self.dt * 0.9)
            if self.flash == 0:
                self.trail[:] = 0


class PulsingHeart(Gift):
    name = "heart"
    label = "Pulsing Heart"
    description = "A 3D heart inside the tree beats with a lub-dub rhythm and turns slowly so it reads from every side."
    controls = {"speed": "Speed", "intensity": "Size", "c1": "Turn speed", "c2": "Hollow"}
    defaults = {"intensity": 150, "c1": 60, "c2": 0}
    default_palette = "valentine"

    def render(self, t: float, out: np.ndarray) -> None:
        beat = (self.T * 1.1) % 1.0
        pulse = 1 + 0.18 * np.exp(-((beat - 0.1) / 0.05) ** 2) + 0.1 * np.exp(-((beat - 0.3) / 0.05) ** 2)
        # The heart is about 2.2 scale wide and 2.3 tall; size it to the tree's width.
        scale = self.rmax * (0.3 + 0.45 * self.s("intensity")) * pulse
        a = self.T * self.s("c1") * 1.5
        c, s = np.cos(a), np.sin(a)
        q = self.P - np.array([0.0, 0.15 + self.rmax * 0.9, 0.0])
        x = (q[:, 0] * c + q[:, 2] * s) / scale
        zz = (-q[:, 0] * s + q[:, 2] * c) / scale  # depth of the heart
        y = q[:, 1] / scale + 0.15
        # Taubin's heart surface: inside where f < 0.
        g = x * x + 2.25 * zz * zz + y * y - 1
        f = g ** 3 - x * x * y ** 3 - 0.1125 * zz * zz * y ** 3
        if self.params.c2 >= 128:
            a_ = np.clip(1 - np.abs(f) / 0.08, 0, 1)
        else:
            a_ = np.clip(-f / 0.05 + 0.5, 0, 1)
        self.over_bg(out, a_ * (0.55 + 0.45 * (pulse - 1) / 0.18), self.pal(np.clip(0.5 - f, 0, 1) * 0.5 + 0.25))


class SantaHat(Gift):
    name = "santa_hat"
    label = "Santa Hat"
    description = "The top of the tree becomes a Santa hat: a hat with a fluffy white brim and a bobble on the tip. Palette: hat, then trim."
    controls = {"speed": "Shimmer", "intensity": "Hat size", "c1": "Brim width", "c2": "Bobble size"}
    defaults = {"intensity": 110, "c1": 90, "c2": 110}
    default_palette = "candy_cane"

    def render(self, t: float, out: np.ndarray) -> None:
        v = self.P[:, 1]
        base = 1 - (0.15 + 0.6 * self.s("intensity"))
        brim = 0.02 + 0.1 * self.s("c1")
        bob = 0.03 + 0.1 * self.s("c2")
        hat_col = self.pal(np.full(len(v), 0.25))
        trim_col = self.pal(np.full(len(v), 0.75))
        top = np.array([0.0, 1.0, 0.0])
        has = self.tree.has_pos
        if has.any():
            top = self.P[has][np.argmax(self.P[has, 1])]
        in_hat = v >= base
        in_brim = np.abs(v - base) < brim
        in_bob = np.linalg.norm(self.P - top, axis=1) < bob
        trim = in_brim | in_bob
        shimmer = 0.75 + 0.25 * np.sin(self.T * 3 + self.tree.chain * 97.0)
        out[:] = self.color(1)
        out[in_hat] = hat_col[in_hat]
        out[trim] = trim_col[trim] * shimmer[trim, None]


MORSE = {"A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.", "H": "....", "I": "..",
         "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
         "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--.."}


def morse_timeline(text: str) -> list[tuple[bool, int, int]]:
    """(on, length in dot units, letter index) runs: dot 1, dash 3, gap 1, letter gap 3, word gap 7."""
    runs: list[tuple[bool, int, int]] = []
    li = 0
    for w, word in enumerate(text.upper().split()):
        if w:
            runs.append((False, 7, li))
        for c, ch in enumerate(word):
            if c:
                runs.append((False, 3, li))
            for s, sym in enumerate(MORSE.get(ch, "")):
                if s:
                    runs.append((False, 1, li))
                runs.append((True, 1 if sym == "." else 3, li))
            li += 1
    runs.append((False, 14, li))
    return runs


class MorseXmas(Gift):
    name = "morse"
    label = "Morse Xmas"
    description = "The whole tree flashes MERRY XMAS in Morse code: in color 1, a new palette color for each letter, or as twinkles."
    controls = {"speed": "Speed", "c1": "Style (color 1 / palette / twinkle)"}
    defaults = {"c1": 0, "speed": 110}
    needs_map = False
    default_palette = "party"
    TEXT = "MERRY XMAS"

    def on_setup(self) -> None:
        self.runs = morse_timeline(self.TEXT)
        self.total = sum(r[1] for r in self.runs)
        self.sparkle = self.rng.random(self.tree.n)

    def render(self, t: float, out: np.ndarray) -> None:
        unit = (self.T * 5) % self.total
        on, letter, acc = False, 0, 0.0
        for r_on, length, li in self.runs:
            if unit < acc + length:
                on, letter = r_on, li
                break
            acc += length
        out[:] = self.color(1)
        if not on:
            return
        style = self.params.c1 * 3 // 256
        if style == 0:
            out[:] = self.color(0)
        elif style == 1:
            out[:] = self.pal(np.array([letter * 0.13]))[0]
        else:
            if int(self.T * 10) != getattr(self, "_tick", None):
                self._tick = int(self.T * 10)
                self.sparkle = self.rng.random(self.tree.n)
            lit = self.sparkle > 0.6
            out[lit] = self.pal(self.sparkle[lit] * 3)


EFFECTS_GIFT = [TwoToneSpin, BouncingBalls, Explosions, RainbowBurst, RainbowImplosion, RainbowRipple, RainbowSpin,
                Equalizer, ColorLerp, ParticleAccelerator, PulsingHeart, SantaHat, MorseXmas]
