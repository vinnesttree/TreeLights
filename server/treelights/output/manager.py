"""The output thread: the only code that sends LED data (architecture §5.4, §6.2).

- One lease holder at a time, by priority: calibration > identify > test.
- While a lease is held the current frame is resent every 0.5 s, so WLED never
  drops out of realtime mode because something else in the server is busy.
- When no lease is held nothing is sent and WLED returns to its own effects.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

log = logging.getLogger(__name__)

PRIORITY = {"calibration": 3, "identify": 2, "test": 1}


class FrameSender(Protocol):
    def send(self, rgb: np.ndarray, repeats: int = 1) -> None: ...
    def close(self) -> None: ...


@dataclass
class Lease:
    owner: str
    kind: str
    expires: float  # time.monotonic()

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.kind, 0)


@dataclass
class _Pending:
    frame: np.ndarray
    repeats: int
    future: concurrent.futures.Future


class PowerLimiter:
    """Scales a frame so its estimated current stays under a limit."""

    def __init__(self, max_ma: float | None, ma_per_channel: float = 20.0, idle_ma_per_led: float = 1.0):
        self.max_ma = max_ma
        self.ma_per_channel = ma_per_channel
        self.idle_ma_per_led = idle_ma_per_led

    def estimate_ma(self, rgb: np.ndarray) -> float:
        return float(rgb.astype(np.float64).sum() / 255.0 * self.ma_per_channel + self.idle_ma_per_led * len(rgb))

    def apply(self, rgb: np.ndarray) -> np.ndarray:
        if not self.max_ma:
            return rgb
        idle = self.idle_ma_per_led * len(rgb)
        budget = self.max_ma - idle
        drive = self.estimate_ma(rgb) - idle
        if drive <= budget or drive <= 0:
            return rgb
        scale = max(0.0, budget) / drive
        return np.floor(rgb.astype(np.float64) * scale).astype(np.uint8)


class OutputManager:
    KEEPALIVE_S = 0.5
    DEFAULT_TTL_S = 10.0

    def __init__(self, led_count: int, power: PowerLimiter | None = None):
        self.led_count = led_count
        self.power = power or PowerLimiter(None)
        self._cv = threading.Condition()
        self._sender: FrameSender | None = None
        self._frame = np.zeros((led_count, 3), np.uint8)
        self._pending: list[_Pending] = []
        self._renderer: Callable[[float], np.ndarray] | None = None
        self._render_period = 1 / 40
        self._lease: Lease | None = None
        self._last_send = 0.0
        self._stop = False
        self.frames_sent = 0
        self._listeners: list[Callable[[np.ndarray], None]] = []
        self._thread = threading.Thread(target=self._run, name="output", daemon=True)
        self._thread.start()

    # ---- configuration -------------------------------------------------
    def set_sender(self, sender: FrameSender | None) -> None:
        with self._cv:
            old, self._sender = self._sender, sender
            self._cv.notify()
        if old is not None and old is not sender:
            old.close()

    def set_led_count(self, n: int) -> None:
        with self._cv:
            self.led_count = n
            self._frame = np.zeros((n, 3), np.uint8)

    def add_listener(self, fn: Callable[[np.ndarray], None]) -> None:
        """Called (from the output thread) with every frame sent; used by the live preview."""
        self._listeners.append(fn)

    # ---- lease ---------------------------------------------------------
    def acquire(self, owner: str, kind: str, ttl: float = DEFAULT_TTL_S) -> tuple[bool, Lease | None]:
        now = time.monotonic()
        with self._cv:
            cur = self._lease
            if cur and cur.expires < now:
                cur = self._lease = None
            if cur is None or cur.owner == owner or PRIORITY.get(kind, 0) > cur.priority:
                if cur and cur.owner != owner:
                    log.info("lease %s (%s) preempted by %s (%s)", cur.owner, cur.kind, owner, kind)
                    self._renderer = None
                self._lease = Lease(owner, kind, now + ttl)
                self._cv.notify()
                return True, self._lease
            return False, cur

    def renew(self, owner: str, ttl: float = DEFAULT_TTL_S) -> bool:
        with self._cv:
            if self._lease and self._lease.owner == owner:
                self._lease.expires = time.monotonic() + ttl
                return True
            return False

    def release(self, owner: str) -> None:
        with self._cv:
            if self._lease and self._lease.owner == owner:
                self._lease = None
                self._renderer = None
                self._cv.notify()

    def holder(self) -> Lease | None:
        with self._cv:
            if self._lease and self._lease.expires < time.monotonic():
                self._lease = None
            return self._lease

    def holds(self, owner: str) -> bool:
        lease = self.holder()
        return lease is not None and lease.owner == owner

    # ---- frames --------------------------------------------------------
    def show(self, owner: str, frame: np.ndarray, repeats: int = 1) -> concurrent.futures.Future:
        """Queue a frame; the future resolves to the monotonic time it was sent."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        frame = np.ascontiguousarray(frame, dtype=np.uint8).reshape(-1, 3)
        with self._cv:
            if not (self._lease and self._lease.owner == owner):
                fut.set_exception(PermissionError("output lease not held"))
                return fut
            if len(frame) != self.led_count:
                fut.set_exception(ValueError(f"frame has {len(frame)} LEDs, expected {self.led_count}"))
                return fut
            self._renderer = None
            self._pending.append(_Pending(frame, repeats, fut))
            self._cv.notify()
        return fut

    def set_renderer(self, owner: str, fn: Callable[[float], np.ndarray] | None, fps: float = 40) -> bool:
        with self._cv:
            if not (self._lease and self._lease.owner == owner):
                return False
            self._renderer = fn
            self._render_period = 1.0 / fps
            self._cv.notify()
            return True

    def current_frame(self) -> np.ndarray:
        with self._cv:
            return self._frame.copy()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join(timeout=2)

    # ---- thread --------------------------------------------------------
    def _send(self, sender: FrameSender | None, frame: np.ndarray, repeats: int) -> None:
        frame = self.power.apply(frame)
        if sender is not None:
            sender.send(frame, repeats=repeats)
        self._last_send = time.monotonic()
        self.frames_sent += 1
        for fn in self._listeners:
            try:
                fn(frame)
            except Exception:  # noqa: BLE001
                log.exception("output listener failed")

    def _run(self) -> None:
        while True:
            with self._cv:
                if self._stop:
                    return
                now = time.monotonic()
                if self._lease and self._lease.expires < now:
                    log.info("lease %s expired", self._lease.owner)
                    self._lease = None
                    self._renderer = None
                pending, self._pending = self._pending, []
                renderer = self._renderer if self._lease else None
                sender = self._sender
                active = self._lease is not None
                if not pending and not renderer:
                    wait = self.KEEPALIVE_S - (now - self._last_send) if active else None
                    if wait is None or wait > 0:
                        self._cv.wait(timeout=wait if wait is not None else 1.0)
                        continue
            # Work happens outside the lock.
            if pending:
                for p in pending:
                    with self._cv:
                        self._frame = p.frame
                    try:
                        self._send(sender, p.frame, p.repeats)
                        p.future.set_result(self._last_send)
                    except Exception as e:  # noqa: BLE001
                        p.future.set_exception(e)
            elif renderer:
                t0 = time.monotonic()
                try:
                    frame = np.ascontiguousarray(renderer(t0), dtype=np.uint8).reshape(-1, 3)
                except Exception:  # noqa: BLE001
                    log.exception("renderer failed; stopping it")
                    with self._cv:
                        self._renderer = None
                    continue
                with self._cv:
                    self._frame = frame
                self._send(sender, frame, 1)
                sleep = self._render_period - (time.monotonic() - t0)
                if sleep > 0:
                    with self._cv:
                        self._cv.wait(timeout=sleep)
            elif active:
                with self._cv:
                    frame = self._frame
                self._send(sender, frame, 1)  # keepalive
