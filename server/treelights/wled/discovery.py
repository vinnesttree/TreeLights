"""Find WLED controllers on the LAN via mDNS (_wled._tcp)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging

from .client import WledClient, normalize_mac

log = logging.getLogger(__name__)

SERVICE = "_wled._tcp.local."


async def browse(timeout: float = 3.0) -> list[dict]:
    """Return [{name, host, port}] for controllers that answered within timeout."""
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:  # pragma: no cover
        return []

    found: dict[str, dict] = {}
    pending: set[asyncio.Task] = set()
    azc = AsyncZeroconf()

    async def resolve(name: str) -> None:
        info = AsyncServiceInfo(SERVICE, name)
        if await info.async_request(azc.zeroconf, 2000):
            for addr in info.parsed_addresses():
                try:
                    if ipaddress.ip_address(addr).version == 4:
                        found[name] = {"name": name.split(".")[0], "host": addr, "port": info.port or 80}
                        break
                except ValueError:
                    continue

    def on_change(zeroconf, service_type, name, state_change):  # noqa: ANN001
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            t = asyncio.ensure_future(resolve(name))
            pending.add(t)
            t.add_done_callback(pending.discard)

    browser = AsyncServiceBrowser(azc.zeroconf, SERVICE, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        if pending:
            await asyncio.wait(pending, timeout=2.0)
    finally:
        await browser.async_cancel()
        await azc.async_close()
    return list(found.values())


async def discover(timeout: float = 3.0) -> list[dict]:
    """Browse, then ask each controller for its identity."""
    out = []
    for dev in await browse(timeout):
        client = WledClient(dev["host"], dev["port"], timeout=2.0)
        try:
            info = await client.info()
            dev.update(mac=info.mac, version=info.version, led_count=info.led_count, name=info.name or dev["name"])
        except Exception as e:  # noqa: BLE001 - report, don't fail discovery
            dev["error"] = str(e)
        finally:
            await client.close()
        out.append(dev)
    return out


async def find_by_mac(mac: str, timeout: float = 3.0) -> dict | None:
    want = normalize_mac(mac)
    for dev in await discover(timeout):
        if dev.get("mac") and dev["mac"] == want:
            return dev
    return None
