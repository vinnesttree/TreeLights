"""Virtual WLED: real DDP over UDP plus a JSON API, with fault injection.

Behaves like the parts of WLED TreeLights depends on:
- DDP frames are applied on PUSH (or on every packet until a PUSH is seen);
- realtime mode times out after 2.5 s without data, then WLED shows its own
  effect (here: every LED dim orange, which corrupts captures noticeably);
- /json/info reports live, lip, uptime, leds.count, leds.maxpwr, mac, ver.
"""

from __future__ import annotations

import asyncio
import bisect
import socket
import threading
import time
from dataclasses import dataclass

import numpy as np
import uvicorn
from fastapi import FastAPI

from ..wled import ddp


@dataclass
class WledFaults:
    drop_prob: float = 0.0  # per packet
    delay_ms: tuple[float, float] = (2.0, 8.0)  # uniform processing/network delay
    spike_prob: float = 0.0  # chance a packet is delayed by spike_ms
    spike_ms: float = 250.0


class VirtualWled:
    TIMEOUT_S = 2.5

    def __init__(self, n: int = 600, buses: list[tuple[int, int]] | None = None, seed: int = 0, max_ma: int = 15000):
        self.n = n
        self.buses = buses or [(0, n)]
        self.faults = WledFaults()
        self.rng = np.random.default_rng(seed)
        self.max_ma = max_ma
        self._lock = threading.Lock()
        self._buf = np.zeros((n, 3), np.uint8)
        self._shown = np.zeros((n, 3), np.uint8)
        self._seen_push = False
        self._last_rx = 0.0
        self.live_ip = ""
        self.boot = time.monotonic()
        self.history_t: list[float] = [time.monotonic()]
        self.history: list[np.ndarray] = [self._shown.copy()]
        self.packets = 0
        self.dropped = 0
        self.foreign_ip: str | None = None
        self.udp_port = 0
        self.http_port = 0
        self._transport = None
        self._server: uvicorn.Server | None = None
        self._watch: asyncio.Task | None = None

    # ---- state ----------------------------------------------------------
    def _own_effect(self) -> np.ndarray:
        out = np.zeros((self.n, 3), np.uint8)
        out[:] = (40, 15, 0)
        return out

    def _record(self, frame: np.ndarray) -> None:
        self._shown = frame
        self.history_t.append(time.monotonic())
        self.history.append(frame.copy())
        if len(self.history) > 4000:
            del self.history_t[:2000], self.history[:2000]

    def state_at(self, t: float) -> np.ndarray:
        """What the LEDs showed at monotonic time t (for camera latency simulation)."""
        with self._lock:
            k = bisect.bisect_right(self.history_t, t) - 1
            return self.history[max(k, 0)].copy()

    @property
    def live(self) -> bool:
        return time.monotonic() - self._last_rx < self.TIMEOUT_S

    def reboot(self) -> None:
        with self._lock:
            self.boot = time.monotonic()
            self._last_rx = 0.0
            self._seen_push = False
            self._buf[:] = 0
            self._record(self._own_effect())

    # ---- UDP ------------------------------------------------------------
    def _handle(self, data: bytes, addr: tuple) -> None:
        try:
            _flags, offset, payload, push = ddp.decode_packet(data)
        except ValueError:
            return
        with self._lock:
            start = offset // 3
            px = np.frombuffer(payload, np.uint8)[: (len(payload) // 3) * 3].reshape(-1, 3)
            end = min(self.n, start + len(px))
            if start < self.n:
                self._buf[start:end] = px[: end - start]
            self._last_rx = time.monotonic()
            self.live_ip = self.foreign_ip or addr[0]
            if push:
                self._seen_push = True
            if push or not self._seen_push:
                self._record(self._buf.copy())

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.packets += 1
        f = self.faults
        if f.drop_prob and self.rng.random() < f.drop_prob:
            self.dropped += 1
            return
        delay = self.rng.uniform(*f.delay_ms) / 1000
        if f.spike_prob and self.rng.random() < f.spike_prob:
            delay += f.spike_ms / 1000
        asyncio.get_running_loop().call_later(delay, self._handle, data, addr)

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            with self._lock:
                if self._last_rx and not self.live and not np.array_equal(self._shown, self._own_effect()):
                    self._record(self._own_effect())  # realtime timed out: back to own effect

    # ---- HTTP -----------------------------------------------------------
    def info(self) -> dict:
        return {
            "ver": "0.15.0", "name": "Virtual WLED", "mac": "02:00:00:00:00:01",
            "leds": {"count": self.n, "maxpwr": self.max_ma, "pwr": 0, "fps": 40},
            "live": self.live, "lip": self.live_ip if self.live else "", "lm": "DDP" if self.live else "",
            "uptime": int(time.monotonic() - self.boot),
        }

    def cfg(self) -> dict:
        return {"hw": {"led": {"total": self.n, "ins": [{"start": s, "len": l, "order": 0, "rev": False, "skip": 0} for s, l in self.buses]}},
                "if": {"live": {"en": True, "timeout": 25}}}

    def _app(self) -> FastAPI:
        app = FastAPI()
        state = {"on": True, "bri": 128}
        app.get("/json/info")(lambda: self.info())
        app.get("/json/cfg")(lambda: self.cfg())
        app.get("/json/state")(lambda: state)

        @app.post("/json/state")
        def set_state(patch: dict) -> dict:
            state.update({k: v for k, v in patch.items() if k in ("on", "bri")})
            return state

        return app

    async def start(self, udp_port: int = 0, http_port: int = 0) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _Proto(self), local_addr=("127.0.0.1", udp_port))
        self.udp_port = self._transport.get_extra_info("sockname")[1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", http_port))
        self.http_port = sock.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(self._app(), log_level="warning", lifespan="off"))
        self._server_task = asyncio.ensure_future(self._server.serve(sockets=[sock]))
        self._watch = asyncio.ensure_future(self._watchdog())
        while not self._server.started:
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if self._watch:
            self._watch.cancel()
        if self._transport:
            self._transport.close()
        if self._server:
            self._server.should_exit = True
            await self._server_task


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, w: VirtualWled):
        self.w = w

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.w.datagram_received(data, addr)
