"""Simulated walk-around capture: a person carries the phone around the tree.

Produces what the phone would: a recorded video (30 fps, variable frame timing,
dropped frames, motion blur), a live stream of frames (~8 fps, network drops),
and the orientation sensor log (gyroscope + accelerometer fusion: its own device
axes, an arbitrary yaw origin, drift, noise and a time offset from the camera).
Ground truth (LED positions, camera poses) is kept for tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import av
import cv2
import numpy as np

from ..reconstruction.camera import look_at, rodrigues
from ..walk.gyro import DEFAULT_M, R_to_quat
from ..walk.pattern import T_SLOT, WalkPattern
from .scene import Faults, Scene, encode_jpeg
from .tree import SynthTree

# Tree frame (y up) -> sensor world frame (z up).
Q_TREE_TO_WORLD = np.array([[1.0, 0, 0], [0, 0, -1.0], [0, 1.0, 0]])


@dataclass
class WalkConfig:
    duration: float = 45.0
    loops: float = 1.1  # a little more than one loop, for overlap
    radius_factor: float = 3.2  # walking radius / tree base radius
    fps: float = 30.0
    live_fps: float = 8.0
    exposure_s: float = 1 / 60
    jitter_deg: float = 1.2  # hand jitter (slow sway)
    tremor_deg: float = 0.06
    speed_var: float = 0.25
    bob_cm: float = 1.5
    latency_s: float = 0.06  # WLED + camera pipeline
    frame_jitter_ms: float = 3.0
    video_drop: float = 0.01
    live_drop: float = 0.03
    gyro_noise_deg: float = 0.05
    gyro_drift_deg_min: float = 2.0
    gyro_time_offset_s: float = 0.02
    device_M: np.ndarray = field(default_factory=lambda: DEFAULT_M.copy())  # camera = M @ device
    quat_inverse: bool = False  # sensor quaternion convention
    pattern_level: int = 48
    led_stale_prob: float = 0.0  # chance a displayed frame shows the previous slot (lost/late packets)
    rec_delay_s: float = 0.4  # recording starts after the pattern
    # The path: by default a circle at a steady height. Real walks aren't.
    radius_var: float = 0.0  # slow changes of the distance to the tree, as a fraction (0.3 = +-30%)
    ellipse: float = 0.0  # an oval path: radius x (1 + ellipse) along one direction, (1 - ellipse) across
    center_offset: float = 0.0  # path centred this fraction of the radius away from the tree's axis
    height_frac: float | None = None  # camera height within the lit part of the tree (0 bottom, 1 top); None: a bit above the middle
    height_var: float = 0.0  # slow raising/lowering of the phone, as a fraction of the tree's lit height
    # Standing close: the lit part of the tree is `fill` times the frame height (None: it fits, at ~75%),
    # so LEDs leave the picture. People then tilt the phone up and down to take in the whole tree:
    # the aim point moves +-aim_var of the lit height, `aim_cycles` times per loop.
    fill: float | None = None
    aim_var: float = 0.0
    aim_cycles: float = 3.0
    seed: int = 0

    @staticmethod
    def uneven(**kw) -> "WalkConfig":
        """A walk the way people actually walk: closer on one side, an oval, off-centre path,
        and the phone raised and lowered as they go."""
        base = dict(radius_var=0.2, ellipse=0.15, center_offset=0.2, height_var=0.25)
        base.update(kw)
        return WalkConfig(**base)

    @staticmethod
    def close(**kw) -> "WalkConfig":
        """A small room: too close for the tree to fit in the picture, tilting the phone up and
        down to take it all in, so LEDs keep leaving and re-entering the frame."""
        base = dict(fill=1.7, aim_var=0.3, radius_var=0.1, height_var=0.1)
        base.update(kw)
        return WalkConfig(**base)

    @staticmethod
    def ideal(**kw) -> "WalkConfig":
        """No noise anywhere: steady walk, exact sensor, no drops, no blur."""
        base = dict(jitter_deg=0.0, tremor_deg=0.0, speed_var=0.0, bob_cm=0.0, frame_jitter_ms=0.0, video_drop=0.0,
                    live_drop=0.0, gyro_noise_deg=0.0, gyro_drift_deg_min=0.0, gyro_time_offset_s=0.0,
                    exposure_s=0.0, latency_s=0.0)
        base.update(kw)
        return WalkConfig(**base)


@dataclass
class WalkCapture:
    width: int
    height: int
    video_path: str | None
    video_t: np.ndarray  # video timestamps (s, from recording start)
    live: list[tuple[float, bytes, np.ndarray]]  # (phone time s, jpeg, sensor quaternion xyzw)
    log_t: np.ndarray  # phone time (s)
    log_q: np.ndarray  # (N, 4) xyzw
    rec_start: float  # phone time the recording started
    poses: list[tuple[float, np.ndarray, np.ndarray]]  # (scene time, R, t) for every rendered frame
    pattern: WalkPattern
    tree: SynthTree
    scene: Scene


class Walker:
    def __init__(self, tree: SynthTree, cfg: WalkConfig, scene: Scene):
        self.cfg = cfg
        self.tree = tree
        self.scene = scene
        rng = np.random.default_rng(cfg.seed + 100)
        self.rng = rng
        lit_mid = 0.5 * (tree.positions[:, 1].min() + tree.positions[:, 1].max())
        # Far enough that the lit part of the tree fills ~75% of the frame height.
        span = float(np.ptp(tree.positions[:, 1]))
        vfov_half = np.arctan((scene.height / 2) / scene.intr.f)
        fit = (span / 2) / np.tan(0.75 * vfov_half) + 0.5 * tree.base_radius
        self.radius = max(cfg.radius_factor * tree.base_radius, fit)
        if cfg.fill is not None:  # closer: the lit span is `fill` frame heights tall
            self.radius = max((span / 2) / (cfg.fill * np.tan(vfov_half)) + tree.base_radius, 1.4 * tree.base_radius)
        self.target = np.array([0.0, lit_mid, 0.0])
        y0, y1 = float(tree.positions[:, 1].min()), float(tree.positions[:, 1].max())
        self.lit = (y0, y1)
        self.height = lit_mid + 0.15 * (tree.height - tree.bottom) if cfg.height_frac is None else y0 + cfg.height_frac * (y1 - y0)
        self.phi0 = rng.uniform(0, 2 * np.pi)
        self.path_dir = rng.uniform(0, np.pi)  # orientation of the oval and of the off-centre shift
        self.path_phase = rng.uniform(0, 2 * np.pi, 3)
        # Speed variation: smooth random modulation of the walking speed.
        self.sv = [(rng.uniform(0.05, 0.25), rng.uniform(0, 2 * np.pi)) for _ in range(3)]
        self.jit = [(rng.uniform(0.25, 1.8), rng.uniform(0, 2 * np.pi), rng.normal(0, 1, 3)) for _ in range(6)]
        self.trem = [(rng.uniform(7, 11), rng.uniform(0, 2 * np.pi), rng.normal(0, 1, 3)) for _ in range(2)]
        self.yaw0 = rng.uniform(-np.pi, np.pi)
        self.drift_rw = 0.0

    def _progress(self, t: float) -> float:
        """Fraction of the walk done at time t: steady pace, varied by +-speed_var."""
        c = self.cfg
        u = t / c.duration
        if c.speed_var:
            w = 2 * np.pi * 6 / c.duration  # six slow-down/speed-up periods per walk
            u += c.speed_var / (w * c.duration) * np.sin(w * t + self.sv[0][1])
        return u

    def pose(self, t: float) -> tuple[np.ndarray, np.ndarray]:
        c = self.cfg
        phi = self.phi0 + 2 * np.pi * c.loops * self._progress(t)
        bob = c.bob_cm * np.sin(2 * np.pi * 1.8 * t)
        r = self.radius
        if c.radius_var:  # drifting closer and further, about twice per loop
            r *= 1 + c.radius_var * np.sin(2 * phi + self.path_phase[0])
        if c.ellipse:
            r *= 1 + c.ellipse * np.cos(2 * (phi - self.path_dir))
        off = c.center_offset * self.radius * np.array([np.cos(self.path_dir), np.sin(self.path_dir)])
        h = self.height
        if c.height_var:  # raising and lowering the phone, slowly
            h += c.height_var * (self.lit[1] - self.lit[0]) * np.sin(3 * phi + self.path_phase[1])
        C = np.array([off[0] + r * np.cos(phi), h + bob, off[1] + r * np.sin(phi)])
        # People aim at the middle of what they see; with the tree near the frame's edge
        # they re-aim, so the camera looks at the tree's middle.
        target = self.target
        if c.aim_var:  # tilting the phone up and down to take in a tree that doesn't fit
            target = target + np.array([0.0, c.aim_var * (self.lit[1] - self.lit[0])
                                        * np.sin(c.aim_cycles * phi + self.path_phase[2]), 0.0])
        R, _ = look_at(C, target)
        rv = np.zeros(3)
        if c.jitter_deg:
            rv += sum(np.radians(c.jitter_deg) / len(self.jit) * 2 * np.sin(2 * np.pi * f * t + p) * ax / np.linalg.norm(ax)
                      for f, p, ax in self.jit)
        if c.tremor_deg:
            rv += sum(np.radians(c.tremor_deg) * np.sin(2 * np.pi * f * t + p) * ax / np.linalg.norm(ax) for f, p, ax in self.trem)
        R = rodrigues(rv) @ R
        return R, -R @ C

    def sensor_quat(self, R_cam_tree: np.ndarray, t: float) -> np.ndarray:
        """What the orientation sensor reports for a camera pose (tree frame -> camera)."""
        c = self.cfg
        yaw = self.yaw0 + np.radians(c.gyro_drift_deg_min / 60.0 * t)
        Rz = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1.0]])
        R_world_cam = R_cam_tree @ Q_TREE_TO_WORLD.T @ Rz.T  # sensor world -> camera
        R_world_dev = c.device_M.T @ R_world_cam
        if c.gyro_noise_deg:
            R_world_dev = rodrigues(self.rng.normal(0, np.radians(c.gyro_noise_deg), 3)) @ R_world_dev
        return R_to_quat(R_world_dev if c.quat_inverse else R_world_dev.T)


def simulate(tree: SynthTree, cfg: WalkConfig, faults: Faults, out_video: str | None, width: int = 960, height: int = 540,
             f_px: float | None = None) -> WalkCapture:
    from ..reconstruction.camera import Intrinsics

    rng = np.random.default_rng(cfg.seed)
    f = f_px or 0.78 * width
    scene = Scene(tree, width=width, height=height, intr=Intrinsics(f, (width - 1) / 2, (height - 1) / 2, -0.03, 0.0),
                  faults=faults, seed=cfg.seed)
    walker = Walker(tree, cfg, scene)
    pattern = WalkPattern(tree.n, cfg.pattern_level)
    phone0 = 1000.0  # phone clock at scene time 0
    # Sensor log at 60 Hz.
    ts = np.arange(0.0, cfg.duration + cfg.rec_delay_s + 1.0, 1 / 60)
    log_q = np.array([walker.sensor_quat(walker.pose(t)[0], t) for t in ts])
    log_t = ts + phone0 + cfg.gyro_time_offset_s
    # Frames.
    times = []
    t = cfg.rec_delay_s
    while t < cfg.duration:
        times.append(t + (rng.normal(0, cfg.frame_jitter_ms / 1000) if cfg.frame_jitter_ms else 0.0))
        t += 1 / cfg.fps
    times = np.array(sorted(times))
    live_every = cfg.fps / cfg.live_fps
    next_live = cfg.rec_delay_s
    poses, live, vid_t = [], [], []
    container = stream = None
    if out_video:
        container = av.open(out_video, "w")
        stream = container.add_stream("libx264", rate=int(cfg.fps))
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        stream.time_base = Fraction(1, 90000)
# One encoder thread: libx264's multi-threaded output depended on what the process had
        # encoded before (another simulated walk), so a test's recording, and with it its
        # result, changed with the tests run before it.
        stream.options = {"crf": "18", "preset": "veryfast", "threads": "1"}
    last_pts = -1
    for t in times:
        k = int(np.floor((t - cfg.latency_s) / T_SLOT))
        if cfg.led_stale_prob and rng.random() < cfg.led_stale_prob:
            k -= 1
        leds = pattern.frame_for_slot(k)
        R, tt = walker.pose(t)
        blur = walker.pose(t + cfg.exposure_s) if cfg.exposure_s else None
        img = scene.render_pose(R, tt, leds, blur_to=blur)
        poses.append((t, R, tt))
        if container is not None and not (cfg.video_drop and rng.random() < cfg.video_drop):
            vt = t - cfg.rec_delay_s
            pts = int(round(max(vt, 0) * 90000))
            if pts > last_pts:
                fr = av.VideoFrame.from_ndarray(img[..., ::-1].copy(), format="rgb24")
                fr.pts, fr.time_base = pts, stream.time_base
                for pkt in stream.encode(fr):
                    container.mux(pkt)
                last_pts = pts
                vid_t.append(vt)
        if t >= next_live:
            next_live += live_every / cfg.fps * (1 + (rng.normal(0, 0.1) if cfg.frame_jitter_ms else 0))
            if not (cfg.live_drop and rng.random() < cfg.live_drop):
                pt = phone0 + t
                i = int(np.clip(np.searchsorted(log_t, pt), 0, len(log_t) - 1))
                live.append((pt, encode_jpeg(img, 88), log_q[i]))
    if container is not None:
        for pkt in stream.encode():
            container.mux(pkt)
        container.close()
    return WalkCapture(width, height, out_video, np.array(vid_t), live, log_t, log_q, phone0 + cfg.rec_delay_s,
                       poses, pattern, tree, scene)


def decode_video_frames(path: str):
    """Yield (time s, RGB image) from a video file."""
    with av.open(path) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        for fr in c.decode(s):
            if fr.time is not None:
                yield float(fr.time), fr.to_ndarray(format="rgb24")


def jpeg_rgb(data: bytes) -> np.ndarray:
    return cv2.cvtColor(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
