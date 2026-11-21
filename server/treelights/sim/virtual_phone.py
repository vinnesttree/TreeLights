"""Virtual phone: answers the capture protocol by rendering the simulated scene.

What the camera sees at time t is the LED state the virtual WLED showed at
t - camera_latency, so lost or late packets and a too-short wait time produce
exactly the stale frames the real system has to catch.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import numpy as np

from ..capture.protocol import pack_frame
from .scene import Scene, encode_jpeg
from .virtual_wled import VirtualWled


class VirtualPhone:
    def __init__(self, scene: Scene, wled: VirtualWled, latency_ms: float = 60.0, jitter_ms: float = 15.0,
                 frame_ms: float = 33.3, seed: int = 0):
        self.scene = scene
        self.wled = wled
        self.latency_ms = latency_ms
        self.jitter_ms = jitter_ms
        self.frame_ms = frame_ms
        self.rng = np.random.default_rng(seed)
        self.view_index = 0
        self.send_text: Callable[[dict], Awaitable[None]] | None = None
        self.send_binary: Callable[[bytes], Awaitable[None]] | None = None
        self.frames_sent = 0
        self.drop_next_frames = 0  # simulate a frame that never arrives
        self._probe: dict | None = None
        self._tasks: set[asyncio.Task] = set()
        self.connected = False

    @property
    def view(self):
        return self.scene.views[self.view_index]

    def _spawn(self, coro) -> None:  # noqa: ANN001
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def hello(self) -> dict:
        return {"type": "hello", "role": "camera",
                "camera": {"width": self.scene.width, "height": self.scene.height, "deviceId": "virtual-back",
                           "locks": {"exposure": True, "focus": True, "whiteBalance": True}},
                "up_cam": self.scene.up_in_camera().tolist()}

    async def start(self, send_text: Callable[[dict], Awaitable[None]], send_binary: Callable[[bytes], Awaitable[None]]) -> None:
        self.send_text, self.send_binary = send_text, send_binary
        self.connected = True
        self._spawn(self._heartbeat())

    def stop(self) -> None:
        self.connected = False
        for t in list(self._tasks):
            t.cancel()

    async def _heartbeat(self) -> None:
        while self.connected:
            await asyncio.sleep(1.0)
            if self.connected and self.send_text:
                await self.send_text({"type": "hb", "up_cam": self.scene.up_in_camera().tolist()})

    def _camera_time(self) -> float:
        """The scene time the next frame shows (latency with jitter)."""
        lat = self.latency_ms + self.rng.uniform(-self.jitter_ms, self.jitter_ms)
        return time.monotonic() - lat / 1000.0

    async def handle(self, msg: dict | str) -> None:
        if isinstance(msg, str):
            msg = json.loads(msg)
        t = msg.get("type")
        if t == "capture":
            self._spawn(self._capture(msg))
        elif t == "probe_start":
            self._probe = {"id": msg["id"], "marks": [], "delays": []}
            self._spawn(self._probe_watch(msg["id"]))
        elif t == "probe_mark" and self._probe:
            self._probe["marks"].append((time.monotonic(), bool(msg["on"])))
        elif t == "probe_end" and self._probe:
            p, self._probe = self._probe, None
            await asyncio.sleep(0.3)
            await self.send_text({"type": "probe_result", "id": p["id"], "delays_ms": p["delays"]})

    async def _capture(self, msg: dict) -> None:
        await asyncio.sleep(msg.get("wait_ms", 0) / 1000.0)
        # The next camera frame boundary.
        await asyncio.sleep(self.rng.uniform(0, self.frame_ms) / 1000.0)
        leds = self.wled.state_at(self._camera_time())
        img = self.scene.render(self.view, leds)
        jpeg = encode_jpeg(img, 90)
        if self.drop_next_frames > 0:
            self.drop_next_frames -= 1
            return
        self.frames_sent += 1
        await self.send_binary(pack_frame({"type": "frame", "step": msg["step"], "kind": msg.get("kind")}, jpeg))

    async def _probe_watch(self, pid: int) -> None:
        """Watch overall brightness at the camera frame rate, like the web page does."""
        last_on = None
        while self._probe and self._probe["id"] == pid:
            leds = self.wled.state_at(self._camera_time())
            on = bool(leds.sum() > 0)
            p = self._probe
            if p["marks"] and last_on is not None and on != last_on:
                mt, mon = p["marks"][-1]
                if on == mon and len(p["delays"]) < len(p["marks"]):
                    p["delays"].append((time.monotonic() - mt) * 1000.0)
            last_on = on
            await asyncio.sleep(self.frame_ms / 1000.0)
