"""Virtual phone for the walk-around capture: walks around the simulated tree in real
time, against a real server and a virtual WLED.

What it does mirrors the web page: it streams ~8 frames/s with the phone time and
orientation quaternion, orientation samples at 60 Hz and the recording start over
/ws/walk; on stop it uploads the full-rate recording in chunks and starts
processing. What the camera shows is whatever the virtual WLED displayed (via
real DDP packets) at the frame time minus the camera latency, rendered from the
walker's pose, so lost packets and timing jitter are real. The 30 fps recording
is rendered after the walk from the poses and LED states recorded during it
(rendering every frame live wouldn't keep up).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from ..walk.protocol import pack_frame
from .scene import Scene, encode_jpeg
from .virtual_wled import VirtualWled
from .walksim import WalkConfig, Walker


@dataclass
class WalkRecording:
    video_t: list[float] = field(default_factory=list)  # from recording start
    states: list[np.ndarray] = field(default_factory=list)
    poses: list[tuple] = field(default_factory=list)  # (scene t, R, t, blur pose)


class VirtualWalker:
    def __init__(self, scene: Scene, wled: VirtualWled, cfg: WalkConfig, phone_clock0: float = 5000.0):
        self.scene = scene
        self.wled = wled
        self.cfg = cfg
        self.walker = Walker(scene.tree, cfg, scene)
        self.rng = np.random.default_rng(cfg.seed + 7)
        self.phone_clock0 = phone_clock0
        self.t0 = 0.0  # monotonic time of scene time 0
        self.rec = WalkRecording()
        self.live_sent = 0
        self.statuses: list[dict] = []

    def phone_time(self, scene_t: float) -> float:
        return self.phone_clock0 + scene_t

    def scene_time(self) -> float:
        return time.monotonic() - self.t0

    def quat(self, scene_t: float) -> np.ndarray:
        R, _ = self.walker.pose(scene_t)
        return self.walker.sensor_quat(R, scene_t)

    def _leds(self, scene_t: float) -> np.ndarray:
        """What the camera sees: the LED state at the frame time minus the latency."""
        return self.wled.state_at(self.t0 + scene_t - self.cfg.latency_s)

    async def walk(self, ws, rec_delay_s: float | None = None) -> None:  # noqa: ANN001
        """Walk for cfg.duration seconds (scene time 0 = the moment the walk starts)."""
        cfg = self.cfg
        rec_delay = cfg.rec_delay_s if rec_delay_s is None else rec_delay_s
        self.t0 = time.monotonic()
        loop = asyncio.get_running_loop()
        next_motion = next_live = 0.0
        next_video = rec_delay
        motion_t: list[float] = []
        motion_q: list[list[float]] = []
        rec_started = False
        pending_live: set[asyncio.Future] = set()
        while True:
            st = self.scene_time()
            if st >= cfg.duration:
                break
            if not rec_started and st >= rec_delay:
                rec_started = True
                await ws.send(json.dumps({"type": "rec_start", "t": self.phone_time(rec_delay)}))
            while next_motion <= st:
                motion_t.append(self.phone_time(next_motion) + cfg.gyro_time_offset_s)
                motion_q.append(self.quat(next_motion).tolist())
                next_motion += 1 / 60
            if len(motion_t) >= 6:
                await ws.send(json.dumps({"type": "motion", "t": motion_t, "q": motion_q}))
                motion_t, motion_q = [], []
            while rec_started and next_video <= st:
                tv = next_video + (self.rng.normal(0, cfg.frame_jitter_ms / 1000) if cfg.frame_jitter_ms else 0.0)
                R, tt = self.walker.pose(tv)
                blur = self.walker.pose(tv + cfg.exposure_s) if cfg.exposure_s else None
                self.rec.video_t.append(tv - rec_delay)
                self.rec.states.append(self._leds(tv))
                self.rec.poses.append((tv, R, tt, blur))
                next_video += 1 / cfg.fps
            if next_live <= st and len(pending_live) < 2:
                next_live = st + 1 / cfg.live_fps
                leds = self._leds(st)
                R, tt = self.walker.pose(st)
                blur = self.walker.pose(st + cfg.exposure_s) if cfg.exposure_s else None
                q = self.quat(st)
                phone_t = self.phone_time(st)

                def render(R=R, tt=tt, leds=leds, blur=blur) -> bytes:
                    return encode_jpeg(self.scene.render_pose(R, tt, leds, blur_to=blur), 88)

                fut = loop.run_in_executor(None, render)
                pending_live.add(fut)

                def done(f, phone_t=phone_t, q=q) -> None:  # noqa: ANN001
                    pending_live.discard(f)
                    if f.cancelled() or f.exception() is not None:
                        return
                    if cfg.live_drop and self.rng.random() < cfg.live_drop:
                        return
                    asyncio.ensure_future(ws.send(pack_frame({"t": phone_t, "q": q.tolist()}, f.result())))
                    self.live_sent += 1

                fut.add_done_callback(done)
            await asyncio.sleep(0.005)
        if pending_live:
            await asyncio.wait(pending_live)
        await asyncio.sleep(0.2)

    def render_video(self, path: str) -> str:
        cfg = self.cfg
        w, h = self.scene.width, self.scene.height
        with av.open(path, "w") as c:
            s = c.add_stream("libx264", rate=int(cfg.fps))
            s.width, s.height, s.pix_fmt = w, h, "yuv420p"
            s.time_base = Fraction(1, 90000)
            s.options = {"crf": "18", "preset": "veryfast"}
            last = -1
            for vt, leds, (tv, R, tt, blur) in zip(self.rec.video_t, self.rec.states, self.rec.poses):
                if cfg.video_drop and self.rng.random() < cfg.video_drop:
                    continue
                pts = int(round(max(vt, 0) * 90000))
                if pts <= last:
                    continue
                img = self.scene.render_pose(R, tt, leds, blur_to=blur)
                fr = av.VideoFrame.from_ndarray(img[..., ::-1].copy(), format="rgb24")
                fr.pts, fr.time_base = pts, s.time_base
                for pkt in s.encode(fr):
                    c.mux(pkt)
                last = pts
            for pkt in s.encode():
                c.mux(pkt)
        return path


async def upload(http, url: str, path: str, chunk: int = 1 << 20) -> int:  # noqa: ANN001
    """Resumable chunked upload, as the web page does it."""
    data = Path(path).read_bytes()
    size = (await http.get(url)).json()["size"]
    while size < len(data):
        r = await http.put(url, params={"offset": size}, content=data[size : size + chunk])
        r.raise_for_status()
        size = r.json()["size"]
    return size


async def run_walk_rig(server: str, n: int = 150, cfg: WalkConfig | None = None, faults=None, seed: int = 0,  # noqa: ANN001
                       tree=None, use_video: bool = True, verbose: bool = True, workdir: str | None = None) -> dict:  # noqa: ANN001
    """Walk once around a simulated tree against a running server; returns the map and statuses."""
    import ssl
    import tempfile

    import httpx
    import websockets

    from .scene import Faults
    from .tree import make_tree

    cfg = cfg or WalkConfig(duration=45.0, seed=seed)
    if tree is None:
        k = (n / 600) ** 0.5 if n < 600 else 1.0
        tree = make_tree(n=n, seed=seed, height=180 * k, base_radius=65 * k, bottom=25 * k)
    from ..reconstruction.camera import Intrinsics

    W, H = 960, 540
    scene = Scene(tree, width=W, height=H, intr=Intrinsics(0.78 * W, (W - 1) / 2, (H - 1) / 2, -0.03, 0.0),
                  faults=faults or Faults(), seed=seed)
    wled = VirtualWled(n, seed=seed)
    await wled.start()
    sslctx = ssl.create_default_context()
    sslctx.check_hostname = False
    sslctx.verify_mode = ssl.CERT_NONE
    ws_base = server.replace("https://", "wss://").replace("http://", "ws://")
    wss_ssl = sslctx if ws_base.startswith("wss://") else None
    out: dict = {"tree": tree, "scene": scene}
    try:
        async with httpx.AsyncClient(base_url=server, verify=False, timeout=60) as http:
            await http.put("/api/config", json={"wled": {"host": "127.0.0.1", "http_port": wled.http_port,
                                                          "udp_port": wled.udp_port, "led_count": n}})
            (await http.post("/api/wled/select", json={"host": "127.0.0.1"})).raise_for_status()
            await http.put("/api/config", json={"tree_height_cm": round(float(np.ptp(tree.positions[:, 1])), 2)})
            phone = VirtualWalker(scene, wled, cfg)
            async with websockets.connect(f"{ws_base}/ws/walk", ssl=wss_ssl, max_size=None) as ws:
                await ws.send(json.dumps({"type": "hello", "role": "walk", "camera": {"width": W, "height": H,
                                                                                     "device": "virtual"}}))
                welcome = json.loads(await ws.recv())
                if welcome.get("type") != "welcome":
                    raise RuntimeError(f"walk refused: {welcome}")
                await ws.send(json.dumps({"type": "start"}))
                stopped: dict = {}

                async def reader() -> None:
                    async for msg in ws:
                        m = json.loads(msg)
                        if m.get("type") == "walk_status":
                            phone.statuses.append(m)
                            if verbose:
                                print("status", {k: m.get(k) for k in ("elapsed_s", "locked", "identified", "covered",
                                                                       "warnings", "level")})
                        elif m.get("type") == "stopped":
                            stopped.update(m)
                            return
                        elif m.get("type") == "error":
                            raise RuntimeError(m["message"])

                rt = asyncio.ensure_future(reader())
                # Let the pattern start before walking (the page waits for "started" the same way).
                await asyncio.sleep(0.3)
                await phone.walk(ws)
                await ws.send(json.dumps({"type": "stop"}))
                await asyncio.wait_for(rt, 30)
            out["statuses"] = phone.statuses
            out["live_sent"] = phone.live_sent
            sid, vid = stopped["scan"], stopped["view"]
            out["scan"], out["view"] = sid, vid
            if use_video:
                d = Path(workdir or tempfile.mkdtemp())
                path = await asyncio.get_running_loop().run_in_executor(None, phone.render_video, str(d / "walk.mp4"))
                out["uploaded"] = await upload(http, stopped["upload"], path)
            job = (await http.post(stopped["process"], json={"video": use_video})).json()
            while job["status"] == "running":
                await asyncio.sleep(1)
                job = (await http.get(f"/api/jobs/{job['id']}")).json()
            if job["status"] != "done":
                raise RuntimeError(f"walk processing failed: {job['error']}")
            mid = job["result"]["map"]
            out["map_id"] = mid
            out["map"] = (await http.get(f"/api/maps/{mid}")).json()
            out["activate"] = (await http.post(f"/api/maps/{mid}/activate", json={})).json()
            if verbose:
                q = out["map"]["quality"]
                print(f"map {mid}: {q['counts']}, warnings {q['warnings']}")
    finally:
        await wled.stop()
    return out


def main() -> None:
    import argparse
    import logging

    from .scene import Faults

    p = argparse.ArgumentParser(prog="treelights.sim.virtual_walker", description="Walk around a simulated tree.")
    p.add_argument("--server", default="https://127.0.0.1:8443")
    p.add_argument("--n", type=int, default=150)
    p.add_argument("--duration", type=float, default=45.0)
    p.add_argument("--ideal", action="store_true", help="no noise anywhere")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    if a.ideal:
        cfg, faults = WalkConfig.ideal(duration=a.duration), Faults(foliage=0.0, glint_prob=0.0, noise_sigma=0.0, shot_noise=0.0)
    else:
        cfg, faults = WalkConfig(duration=a.duration), Faults()
    asyncio.run(run_walk_rig(a.server, a.n, cfg, faults))


if __name__ == "__main__":
    main()
