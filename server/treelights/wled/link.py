"""Connection to the configured controller: identity by MAC, checks, monitoring.

Architecture §5.2 (identity, setup checks) and §5.3 (monitoring).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from .. import netutil
from .client import WledClient, WledConfigView, WledInfo, config_fingerprint
from . import discovery

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail" | "unknown"
    detail: str = ""


@dataclass
class MonitorEvent:
    kind: str  # "foreign_source" | "reboot" | "unreachable" | "not_live" | "recovered"
    detail: str = ""
    at: float = field(default_factory=time.time)


class WledLink:
    def __init__(self, host: str | None, mac: str | None, port: int = 80):
        self.host = host
        self.mac = mac
        self.port = port
        self.info: WledInfo | None = None
        self.cfg: WledConfigView | None = None
        self._client: WledClient | None = WledClient(host, port) if host else None
        self._host_listeners: list[Callable[[str], None]] = []

    def on_host_change(self, fn: Callable[[str], None]) -> None:
        self._host_listeners.append(fn)

    def set_host(self, host: str, mac: str | None = None, port: int | None = None) -> None:
        if host != self.host or (port is not None and port != self.port) or self._client is None:
            old = self._client
            self.host = host
            if port is not None:
                self.port = port
            self._client = WledClient(host, self.port)
            if old:
                asyncio.ensure_future(old.close())
            for fn in self._host_listeners:
                fn(host)
        if mac:
            self.mac = mac

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> WledClient:
        if not self._client:
            raise RuntimeError("no WLED controller configured")
        return self._client

    async def refresh(self, with_cfg: bool = True) -> WledInfo:
        self.info = await self.client.info()
        if self.info.mac and not self.mac:
            self.mac = self.info.mac
        if with_cfg:
            self.cfg = await self.client.cfg()
        return self.info

    async def refresh_or_rediscover(self) -> WledInfo | None:
        try:
            return await self.refresh(with_cfg=False)
        except (httpx.HTTPError, RuntimeError):
            pass
        if not self.mac:
            return None
        dev = await discovery.find_by_mac(self.mac)
        if dev:
            log.info("controller %s moved to %s", self.mac, dev["host"])
            self.set_host(dev["host"], self.mac)
            try:
                return await self.refresh(with_cfg=False)
            except httpx.HTTPError:
                return None
        return None

    def fingerprint(self) -> str:
        return config_fingerprint(self.info, self.cfg)

    def own_ips(self) -> set[str]:
        ips = set(netutil.local_ipv4_addresses())
        if self.host:
            src = netutil.source_ip_for(self.host)
            if src:
                ips.add(src)
        return ips

    async def setup_checks(self, configured_led_count: int, realtime_probe: Callable[[], Awaitable[None]] | None = None) -> list[Check]:
        """The Setup page checklist (architecture §5.2)."""
        checks: list[Check] = []
        try:
            info = await self.refresh(with_cfg=True)
        except Exception as e:  # noqa: BLE001
            return [Check("reachable", "fail", f"Can't reach WLED at {self.host}: {e}")]
        checks.append(Check("reachable", "ok", f"{info.name or 'WLED'} at {self.host}, MAC {info.mac or 'unknown'}"))
        vok = info.version_ok()
        checks.append(Check("firmware", "unknown" if vok is None else ("ok" if vok else "warn"),
                            f"WLED {info.version or 'unknown version'}" + ("" if vok in (None, True) else " (0.14 or newer recommended)")))
        if info.led_count is None:
            checks.append(Check("led_count", "unknown", "WLED didn't report its LED count"))
        elif info.led_count != configured_led_count:
            checks.append(Check("led_count", "warn", f"WLED is configured for {info.led_count} LEDs, TreeLights for {configured_led_count}"))
        else:
            checks.append(Check("led_count", "ok", f"{info.led_count} LEDs"))
        checks.append(Check("current_limit", "ok" if info.max_current_ma else "unknown",
                            f"{info.max_current_ma} mA" if info.max_current_ma else "not reported; set a limit in Setup"))
        cfg = self.cfg
        if cfg is None or cfg.raw is None:
            checks.append(Check("config", "unknown", "WLED configuration not readable (settings PIN?). Output boundaries can't be detected automatically."))
        else:
            breaks = cfg.output_breaks()
            detail = f"{len(cfg.buses)} output(s)" + (f"; chain breaks after LEDs {', '.join(map(str, breaks))}" if breaks else "")
            checks.append(Check("outputs", "ok" if cfg.buses else "unknown", detail))
        if realtime_probe is not None:
            await realtime_probe()
            await asyncio.sleep(0.3)
            try:
                info2 = await self.refresh(with_cfg=False)
                own = self.own_ips()
                if info2.live is None:
                    checks.append(Check("realtime", "unknown", "WLED didn't report realtime status"))
                elif not info2.live:
                    checks.append(Check("realtime", "fail", "WLED ignored the test frame. Enable 'Receive UDP realtime' in WLED's Sync settings."))
                elif info2.live_ip and info2.live_ip not in own:
                    checks.append(Check("realtime", "warn", f"WLED is showing realtime data from {info2.live_ip}, not this server"))
                else:
                    checks.append(Check("realtime", "ok", f"Realtime mode active ({info2.live_mode or 'DDP'})"))
            except httpx.HTTPError as e:
                checks.append(Check("realtime", "fail", str(e)))
        return checks


class WledMonitor:
    """Polls /json/info while the output is held and reports anything suspicious."""

    def __init__(self, link: WledLink, is_sending: Callable[[], bool], period_s: float = 3.0):
        self.link = link
        self.is_sending = is_sending
        self.period_s = period_s
        self._listeners: list[Callable[[MonitorEvent], None]] = []
        self._last_uptime: int | None = None
        self._unreachable = False
        self._task: asyncio.Task | None = None
        self.last_ok: float | None = None

    def subscribe(self, fn: Callable[[MonitorEvent], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _emit(self, ev: MonitorEvent) -> None:
        log.warning("WLED monitor: %s %s", ev.kind, ev.detail)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                log.exception("monitor listener failed")

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def check_once(self) -> None:
        if not self.link.host:
            return
        info = await self.link.refresh_or_rediscover()
        if info is None:
            if not self._unreachable:
                self._unreachable = True
                self._emit(MonitorEvent("unreachable", f"No answer from {self.link.host}"))
            return
        if self._unreachable:
            self._unreachable = False
            self._emit(MonitorEvent("recovered", f"{self.link.host} answers again"))
        if info.uptime_s is not None:
            if self._last_uptime is not None and info.uptime_s + 2 < self._last_uptime:
                self._emit(MonitorEvent("reboot", f"Controller restarted (uptime {self._last_uptime}s -> {info.uptime_s}s). Often a power supply problem."))
            self._last_uptime = info.uptime_s
        if self.is_sending():
            if info.live is False:
                self._emit(MonitorEvent("not_live", "WLED isn't in realtime mode although frames are being sent"))
            elif info.live and info.live_ip and info.live_ip not in self.link.own_ips():
                self._emit(MonitorEvent("foreign_source", f"Another device ({info.live_ip}) is sending realtime data"))
        self.last_ok = time.time()

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("monitor check failed")
            await asyncio.sleep(self.period_s)
