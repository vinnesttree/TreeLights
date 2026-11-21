"""Find WLED controllers on the LAN (architecture §5.2).

Two methods, merged:
- mDNS (`_wled._tcp`). With host networking the container shares the host's
  network stack, and the host often runs its own mDNS responder (avahi-daemon,
  Home Assistant, ...) that owns UDP port 5353. Then the normal mode can't bind the
  port, so it falls back to zeroconf's unicast mode: queries go out from a private
  port and devices answer directly to it, so 5353 isn't needed.
- An HTTP sweep of the host's local /24 networks for WLED's `/json/info`. It needs
  no multicast at all, so it also works where mDNS is blocked or filtered.

Discovery never fails as a whole: a method that fails is logged and reported, and
what the other found is still returned.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging

import httpx

from . import client as wclient
from .client import WledClient, normalize_mac

log = logging.getLogger(__name__)

SERVICE = "_wled._tcp.local."
SWEEP_CONNECT_S = 0.6
SWEEP_CONCURRENCY = 96
MAX_SWEEP_NETS = 4


async def _browse_with(azc, timeout: float) -> list[dict]:  # noqa: ANN001
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

    found: dict[str, dict] = {}
    pending: set[asyncio.Task] = set()

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
    return list(found.values())


async def browse(timeout: float = 3.0) -> list[dict]:
    """mDNS: [{name, host, port}] for controllers that answered within timeout.
    Raises only if mDNS can't be used at all (both modes failed)."""
    try:
        from zeroconf.asyncio import AsyncZeroconf
    except ImportError:  # pragma: no cover
        return []
    try:
        azc = AsyncZeroconf()
    except OSError as e:
        # Typically "Address already in use": another mDNS responder on the host owns 5353.
        log.info("mDNS port 5353 unavailable (%s); using unicast mDNS", e)
        azc = AsyncZeroconf(unicast=True)
    try:
        return await _browse_with(azc, timeout)
    finally:
        await azc.async_close()


def sweep_networks(ips: list[str] | None = None) -> list[ipaddress.IPv4Network]:
    """The local /24 networks worth sweeping: private LAN addresses of this host,
    Docker's own bridge networks (172.17-31.x) only if there is nothing else."""
    from .. import netutil

    nets: list[ipaddress.IPv4Network] = []
    docker: list[ipaddress.IPv4Network] = []
    for ip in ips if ips is not None else netutil.local_ipv4_addresses():
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.version != 4 or not a.is_private or a.is_loopback or a.is_link_local:
            continue
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        bucket = docker if a in ipaddress.ip_network("172.16.0.0/12") else nets
        if net not in bucket:
            bucket.append(net)
    return (nets or docker)[:MAX_SWEEP_NETS]


async def _probe(host: str, sem: asyncio.Semaphore, port: int = 80) -> dict | None:
    async with sem:
        try:
            _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), SWEEP_CONNECT_S)
            w.close()
        except (OSError, asyncio.TimeoutError):
            return None
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get(f"http://{host}:{port}/json/info")
                raw = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
    if not isinstance(raw, dict) or "ver" not in raw or not isinstance(raw.get("leds"), dict):
        return None  # some other web server
    info = wclient.WledInfo.parse(raw)
    return {"name": info.name or "WLED", "host": host, "port": port}


async def sweep(nets: list[ipaddress.IPv4Network] | None = None, port: int = 80) -> list[dict]:
    """HTTP sweep of the local /24 networks for WLED (no multicast needed)."""
    nets = sweep_networks() if nets is None else nets
    sem = asyncio.Semaphore(SWEEP_CONCURRENCY)
    hosts = [str(h) for n in nets for h in n.hosts()]
    results = await asyncio.gather(*(_probe(h, sem, port) for h in hosts))
    return [r for r in results if r]


async def discover(timeout: float = 3.0) -> list[dict]:
    """mDNS and the HTTP sweep together, then each controller's identity."""
    errors: list[str] = []
    found: dict[str, dict] = {}
    m, s = await asyncio.gather(browse(timeout), sweep(), return_exceptions=True)
    for name, res in (("mDNS", m), ("network sweep", s)):
        if isinstance(res, BaseException):
            log.warning("WLED discovery via %s failed", name, exc_info=res)
            errors.append(f"{name}: {res}")
            continue
        for dev in res:
            found.setdefault(dev["host"], dev)
    if errors and len(errors) == 2 and not found:
        raise DiscoveryError("Couldn't search the network (" + "; ".join(errors) + "). Enter the controller's address instead.")
    out = []
    for dev in found.values():
        client = WledClient(dev["host"], dev["port"], timeout=2.0)
        try:
            info = await client.info()
            dev.update(mac=info.mac, version=info.version, led_count=info.led_count, name=info.name or dev["name"])
        except Exception as e:  # noqa: BLE001 - report, don't fail discovery
            dev["error"] = str(e)
        finally:
            await client.close()
        out.append(dev)
    return sorted(out, key=lambda d: tuple(int(p) for p in d["host"].split(".")) if d["host"].count(".") == 3 else (999,))


class DiscoveryError(RuntimeError):
    pass


async def find_by_mac(mac: str, timeout: float = 3.0) -> dict | None:
    want = normalize_mac(mac)
    try:
        devices = await discover(timeout)
    except DiscoveryError as e:
        log.warning("rediscovery failed: %s", e)
        return None
    for dev in devices:
        if dev.get("mac") and dev["mac"] == want:
            return dev
    return None
