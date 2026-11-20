"""Synthetic scene: a fixed tripod camera and a tree turned between views.

Renders what the phone would see for a given LED state, with optional faults
(architecture §17): reflections, glossy highlights, glow through needles,
exposure drift, a bumped camera, a person walking past, TV flicker.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from ..reconstruction.camera import Intrinsics, look_at, project, rodrigues
from .tree import SynthTree


def rot_y(a: float) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


@dataclass
class Faults:
    mirror: bool = False  # a wall mirror beside the tree
    glossy_prob: float = 0.0  # per (LED, view) chance of an ornament highlight
    exposure_drift: float = 0.0  # relative random walk step per frame
    bump_at_frame: int | None = None  # camera shifts permanently at this frame number
    bump_px: float = 6.0
    person_frames: tuple[int, int] | None = None  # [start, end) frame numbers with a person in view
    tv_flicker: bool = False
    noise_sigma: float = 1.5
    # Branches around each LED: light from the LED scatters off nearby needles (a diffuse glow
    # that is still visible when the LED itself is hidden) and some needles give sharp,
    # view-dependent glints. foliage scales the scattered light; 0 disables it.
    foliage: float = 1.0
    glint_prob: float = 0.15


@dataclass
class View:
    angle: float
    R_tree: np.ndarray  # tree frame -> room frame
    s_tree: np.ndarray
    vis: np.ndarray  # (N,) attenuation 0..1 (0 = hidden)
    glow_offset: np.ndarray  # (N,2) px: where the diffuse glow appears relative to the LED (synthetic observations)
    patch_vis: np.ndarray | None = None  # (N, P) visibility of each lit-needle patch around each LED


@dataclass
class Scene:
    tree: SynthTree
    width: int = 1280
    height: int = 720
    intr: Intrinsics = field(default_factory=lambda: Intrinsics(1000.0, 639.5, 359.5, -0.04, 0.0))
    eye: np.ndarray = field(default_factory=lambda: np.array([0.0, 110.0, -330.0]))
    target: np.ndarray = field(default_factory=lambda: np.array([0.0, 100.0, 0.0]))
    faults: Faults = field(default_factory=Faults)
    led_gain: float = 1600.0  # core peak (DN) for a full single channel at reference distance
    seed: int = 0

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)
        self.R_cam, self.t_cam = look_at(self.eye, self.target)
        self.views: list[View] = []
        self.frame_no = 0
        self.exposure = 1.0
        self.bump_R = np.eye(3)
        self.crosstalk = np.array([[1.0, 0.10, 0.02], [0.12, 1.0, 0.12], [0.02, 0.15, 1.0]])  # cam RGB = M @ led RGB
        yy, xx = np.mgrid[0 : self.height, 0 : self.width].astype(np.float32)
        bg = 18 + 10 * np.sin(xx / 97.0) * np.cos(yy / 61.0) + 6 * np.sin((xx + 2 * yy) / 23.0)
        self.background = np.clip(bg, 2, 60)[..., None] * np.array([0.9, 1.0, 1.1], np.float32)
        nrng = np.random.default_rng(self.seed + 1)
        self._noise = [nrng.standard_normal((self.height, self.width, 3), dtype=np.float32) for _ in range(6)]
        self.mirror_p0 = np.array([170.0, 0.0, 40.0])
        self.mirror_n = np.array([-1.0, 0.0, 0.0])
        # Lit-needle patches: fixed 3D points on branches 2-8 cm around each LED (real surfaces,
        # so they are consistent between views), each scattering a few % of the LED's light.
        prng = np.random.default_rng(self.seed + 2)
        n, P = self.tree.n, 6
        d = prng.normal(0, 1, (n, P, 3))
        d /= np.linalg.norm(d, axis=2, keepdims=True)
        self.patch_pos = self.tree.positions[:, None, :] + d * prng.uniform(2.0, 8.0, (n, P, 1))
        self.patch_w = prng.uniform(0.01, 0.06, (n, P))

    # ---- views ----------------------------------------------------------
    def add_view(self, angle_deg: float, jitter: bool = True) -> View:
        rng = self.rng
        a = np.radians(angle_deg + (rng.uniform(-3, 3) if jitter else 0))
        tilt = rodrigues(rng.normal(0, np.radians(0.4), 3) * np.array([1, 0, 1])) if jitter else np.eye(3)
        R_tree = tilt @ rot_y(a)
        s_tree = np.array([rng.uniform(-1.5, 1.5), 0.0, rng.uniform(-1.5, 1.5)]) if jitter else np.zeros(3)
        vis, glow = self._visibility(R_tree, s_tree)
        pv, _ = self._visibility(R_tree, s_tree, self.patch_pos.reshape(-1, 3))
        v = View(a, R_tree, s_tree, vis, glow, pv.reshape(self.patch_pos.shape[:2]))
        self.views.append(v)
        return v

    def camera_pose(self, v: View) -> tuple[np.ndarray, np.ndarray]:
        """Ground-truth pose of the camera in the tree frame."""
        Rc = self.bump_R @ self.R_cam
        return Rc @ v.R_tree, Rc @ v.s_tree + self.bump_R @ self.t_cam

    def up_in_camera(self) -> np.ndarray:
        """World 'up' in camera coordinates, as the phone's gravity sensor reports it."""
        u = (self.bump_R @ self.R_cam) @ np.array([0.0, 1.0, 0.0])
        return u + self.rng.normal(0, 0.005, 3)

    def _visibility(self, R_tree: np.ndarray, s_tree: np.ndarray, points: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
        tree = self.tree
        P = tree.positions if points is None else points
        # Camera center in the tree frame.
        C = R_tree.T @ (self.eye - s_tree)
        ts = np.linspace(0.02, 0.98, 40)
        seg = P[None, :, :] * (1 - ts[:, None, None]) + C[None, None, :] * ts[:, None, None]  # toward camera
        r = np.hypot(seg[..., 0], seg[..., 2])
        rs = tree.surface_radius(seg[..., 1]) * 0.9
        inside = (r < rs) & (seg[..., 1] > tree.bottom - 5)
        step = np.linalg.norm(C[None, :] - P, axis=1) * (ts[1] - ts[0])
        penetration = inside.sum(axis=0) * step  # cm of branches between LED and camera
        rng = np.random.default_rng(abs(hash((round(float(C[0]), 3), round(float(C[2]), 3)))) % 2**32)
        att = np.exp(-penetration / 14.0) * rng.uniform(0.7, 1.0, len(P))
        att[att < 0.12] = 0.0
        glow = rng.normal(0, 1.0, (len(P), 2)) * np.clip(1 - att, 0, 1)[:, None] * 2.0
        return att, glow

    # ---- rendering ------------------------------------------------------
    def project_view(self, v: View) -> tuple[np.ndarray, np.ndarray]:
        R, t = self.camera_pose(v)
        return project(self.tree.positions, R, t, self.intr)

    def apparent_uv(self, v: View) -> np.ndarray:
        """Ground truth for where each LED's core appears in the image. With branch
        modelling on, the core is at its true projection (scattered light and glints are
        drawn separately); the older model shifted the core itself."""
        uv, _ = self.project_view(v)
        return uv if self.faults.foliage else uv + v.glow_offset

    def render(self, v: View, leds: np.ndarray) -> np.ndarray:
        """Render a BGR uint8 image for commanded LED colors (N,3) uint8."""
        f = self.faults
        rng = self.rng
        self.frame_no += 1
        if f.bump_at_frame is not None and self.frame_no == f.bump_at_frame:
            ang = f.bump_px / self.intr.f
            self.bump_R = rodrigues(np.array([0.0, ang, ang * 0.3])) @ self.bump_R
        if f.exposure_drift:
            self.exposure = float(np.clip(self.exposure * (1 + rng.normal(0, f.exposure_drift)), 0.6, 1.6))

        img = self.background.copy()
        leds = np.asarray(leds, np.float32)
        lit = np.nonzero(leds.max(axis=1) > 0)[0]
        if len(lit):
            uv, z = self.project_view(v)
            emit = (leds[lit] / 255.0) @ self.crosstalk.T  # camera RGB response
            dist_fac = (330.0 / np.maximum(z[lit], 1.0)) ** 2
            amp = self.led_gain * emit * (v.vis[lit] * dist_fac)[:, None]
            centers = uv[lit] if f.foliage else uv[lit] + v.glow_offset[lit]
            full = self.led_gain * emit * dist_fac[:, None]  # unoccluded amplitude
            if f.foliage and v.patch_vis is not None:
                R, t = self.camera_pose(v)
                puv, pz = project(self.patch_pos[lit].reshape(-1, 3), R, t, self.intr)
                puv = puv.reshape(len(lit), -1, 2)
                albedo = np.array([0.55, 0.8, 0.45])  # needles are greenish
                for k, i in enumerate(lit):
                    if z[i] <= 0:
                        continue
                    for j in range(puv.shape[1]):
                        w = self.patch_w[i, j] * v.patch_vis[i, j] * f.foliage
                        if w > 0:
                            self._splat(img, puv[k, j], full[k] * albedo * w, sigma=3.0, halo=0.0)
                    # Specular glints off needles: sharp, view-dependent, near the LED, and up to
                    # half as bright as the unoccluded LED (so they can beat a mostly hidden one).
                    if f.glint_prob and _hash01(i, int(v.angle * 1000), 11) < f.glint_prob:
                        off = (np.array([_hash01(i, int(v.angle * 1000), 12), _hash01(i, int(v.angle * 1000), 13)]) - 0.5) * 36
                        off += np.sign(off) * 4
                        self._splat(img, uv[i] + off, full[k] * (0.1 + 0.4 * _hash01(i, int(v.angle * 1000), 14)), sigma=1.1, halo=0.0)
            for k, i in enumerate(lit):
                if v.vis[i] <= 0 or z[i] <= 0:
                    continue
                sharp = 0.5 + 0.5 * v.vis[i]
                self._splat(img, centers[k], amp[k], sigma=1.2 / sharp, halo=0.05 / sharp)
                if f.glossy_prob and _hash01(i, int(v.angle * 1000), 7) < f.glossy_prob:
                    off = (np.array([_hash01(i, 1, 9), _hash01(i, 2, 9)]) - 0.5) * 40
                    self._splat(img, centers[k] + off, amp[k] * 0.35, sigma=1.5, halo=0.02)
            if f.mirror:
                R, t = self.camera_pose(v)
                Xw = (self.tree.positions[lit] @ v.R_tree.T) + v.s_tree  # room frame
                d = (Xw - self.mirror_p0) @ self.mirror_n
                Xm = Xw - 2 * d[:, None] * self.mirror_n[None, :]
                Rc = self.bump_R @ self.R_cam
                uvm, zm = project(Xm, Rc, self.bump_R @ self.t_cam, self.intr)
                for k, i in enumerate(lit):
                    if zm[k] > 0 and v.vis[i] > 0 and d[k] < 0:
                        self._splat(img, uvm[k], amp[k] * 0.3, sigma=1.6, halo=0.03)
            # Light from the LEDs brightens the room a little.
            img += (leds[lit].sum() / 255.0) * 0.004
        if f.person_frames and f.person_frames[0] <= self.frame_no < f.person_frames[1]:
            x0 = int(self.width * 0.3)
            img[100:700, x0 : x0 + 220] += np.array([40, 45, 55], np.float32)
        if f.tv_flicker:
            img[40:160, 1050:1230] += rng.uniform(0, 60)
        img *= self.exposure
        n1 = self._noise[rng.integers(len(self._noise))]
        n2 = self._noise[rng.integers(len(self._noise))]
        sh = (int(rng.integers(self.height)), int(rng.integers(self.width)))
        img += np.roll(n1, sh, axis=(0, 1)) * f.noise_sigma
        img += n2 * np.sqrt(np.clip(img, 0, None)) * 0.15
        rgb = np.clip(img, 0, 255).astype(np.uint8)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    def _splat(self, img: np.ndarray, c: np.ndarray, amp: np.ndarray, sigma: float, halo: float) -> None:
        R = 18
        x0, y0 = int(round(c[0])), int(round(c[1]))
        xa, xb = max(0, x0 - R), min(self.width, x0 + R + 1)
        ya, yb = max(0, y0 - R), min(self.height, y0 + R + 1)
        if xa >= xb or ya >= yb:
            return
        yy, xx = np.mgrid[ya:yb, xa:xb].astype(np.float32)
        d2 = (xx - c[0]) ** 2 + (yy - c[1]) ** 2
        prof = np.exp(-d2 / (2 * sigma**2)) + halo * np.exp(-d2 / (2 * 6.0**2))
        img[ya:yb, xa:xb] += prof[..., None] * amp[None, None, :].astype(np.float32)


def _hash01(*vals: int) -> float:
    h = 2166136261
    for v in vals:
        h = ((h ^ (v & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    return h / 2**32


def encode_jpeg(bgr: np.ndarray, quality: int = 90) -> bytes:
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes()
