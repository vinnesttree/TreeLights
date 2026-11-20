"""Closed-loop simulation harness (architecture §17).

Wires a real CaptureSession and OutputManager (sending real DDP over UDP) to a
virtual WLED and a virtual phone. Used by tests and the demo/simulation mode.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..capture.session import CaptureSession
from ..capture.store import Scan, ScanStore
from ..config import Settings
from ..output.manager import OutputManager
from ..wled.ddp import UdpFrameSender
from .scene import Faults, Scene
from .tree import SynthTree, make_tree
from .virtual_phone import VirtualPhone
from .virtual_wled import VirtualWled


@dataclass
class Sim:
    tree: SynthTree
    scene: Scene
    wled: VirtualWled
    phone: VirtualPhone
    output: OutputManager
    scan: Scan
    session: CaptureSession
    executor: concurrent.futures.Executor
    angles: list[float] = field(default_factory=list)

    async def run_view(self, angle: float, mode: str = "hybrid") -> str:
        self.scene.add_view(angle)
        self.phone.view_index = len(self.scene.views) - 1
        self.angles.append(angle)
        vid = self.session.start_view(mode)
        await self.session.wait_done()
        return vid

    async def close(self) -> None:
        await self.session.stop()
        self.phone.stop()
        self.output.stop()
        await self.wled.stop()
        self.executor.shutdown(wait=False)


async def make_sim(data_dir: Path, n: int = 120, seed: int = 0, faults: Faults | None = None,
                   output_break_at: int | None = None, latency_ms: float = 40.0, settings: Settings | None = None,
                   tree: SynthTree | None = None, width: int = 1280, height: int = 720) -> Sim:
    tree = tree or make_tree(n=n, seed=seed, output_break_at=output_break_at, height=180.0 * (n / 600) ** 0.5 if n < 600 else 180.0,
                             base_radius=65.0 * (n / 600) ** 0.5 if n < 600 else 65.0, bottom=25.0 * (n / 600) ** 0.5 if n < 600 else 25.0)
    scene = Scene(tree, seed=seed, faults=faults or Faults(), width=width, height=height,
                  intr=_intr_for(width, height))
    if n < 600:
        # A smaller tree: bring the camera closer so it fills a similar part of the image.
        k = (n / 600) ** 0.5
        scene.eye = scene.eye * np.array([1, k, k])
        scene.target = scene.target * k
        scene.__post_init__()
    buses = [(0, output_break_at), (output_break_at, n - output_break_at)] if output_break_at else [(0, n)]
    wled = VirtualWled(n, buses=buses, seed=seed)
    await wled.start()
    out = OutputManager(n)
    out.set_sender(UdpFrameSender("127.0.0.1", port=wled.udp_port))
    settings = settings or Settings()
    settings.wled.led_count = n
    scan = ScanStore(data_dir).create(n, {"output_breaks": [b - 1 for b, _ in buses[1:]]})
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    session = CaptureSession(scan, out, executor, settings, seed=seed)
    phone = VirtualPhone(scene, wled, latency_ms=latency_ms, seed=seed)

    async def to_session_binary(data: bytes) -> None:
        session.on_camera_binary(data)

    await phone.start(session.on_camera_text, to_session_binary)
    await session.camera_connected(phone.handle, phone.hello())
    return Sim(tree, scene, wled, phone, out, scan, session, executor)


def _intr_for(width: int, height: int):
    from ..reconstruction.camera import Intrinsics

    f = 1000.0 * width / 1280
    return Intrinsics(f, (width - 1) / 2, (height - 1) / 2, -0.04, 0.0)


def identity_errors(sim: Sim, tol_px: float = 2.5) -> dict:
    """Compare recorded best candidates with ground truth: how many observations
    point at a different LED than the one they're recorded for."""
    obs = sim.scan.observations()
    seg_view = {s: m["view"] for s, m in sim.scan.meta["segments"].items()}
    view_ids = [v["id"] for v in sim.scan.meta["views"]]
    wrong, right, total = [], 0, 0
    for seg, leds in obs.items():
        vi = view_ids.index(seg_view[seg])
        v = sim.scene.views[vi]
        uv, _ = sim.scene.project_view(v)
        true = uv + v.glow_offset
        seen = np.nonzero(v.vis > 0)[0]
        for led, cands in leds.items():
            if not cands:
                continue
            total += 1
            c = cands[0]
            d_true = np.hypot(*(true[led] - [c["x"], c["y"]]))
            if v.vis[led] > 0 and d_true <= tol_px:
                right += 1
                continue
            dists = np.hypot(true[seen, 0] - c["x"], true[seen, 1] - c["y"])
            nearest = int(seen[np.argmin(dists)]) if len(seen) else -1
            wrong.append({"seg": seg, "led": int(led), "nearest": nearest, "d_true": float(d_true),
                          "d_nearest": float(dists.min()) if len(seen) else None, "visible": bool(v.vis[led] > 0)})
    return {"total": total, "right": right, "wrong": wrong}
