"""WLED discovery when mDNS is unavailable (architecture §5.2)."""

import asyncio
import concurrent.futures
import ipaddress
import socket

from treelights.wled import discovery


async def test_mdns_port_taken_falls_back_instead_of_failing():
    # Another mDNS responder on the host (avahi, Home Assistant...) owns UDP 5353.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", 5353))
    except OSError:
        pass  # already taken on this machine: same situation
    try:
        assert await discovery.browse(0.5) == []  # unicast mDNS: no error
        assert isinstance(await discovery.discover(0.5), list)
    finally:
        s.close()


async def test_sweep_finds_wled_and_ignores_other_web_servers():
    from treelights.sim.virtual_wled import VirtualWled

    wled = VirtualWled(10)
    await wled.start()

    async def other(reader, writer):  # a non-WLED web server on another port
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 12\r\n\r\n{\"ver\": \"1\"}")
        await writer.drain()
        writer.close()

    srv = await asyncio.start_server(other, "127.0.0.1", 0)
    other_port = srv.sockets[0].getsockname()[1]
    try:
        net = [ipaddress.ip_network("127.0.0.0/30")]
        found = await discovery.sweep(net, port=wled.http_port)
        assert [d["host"] for d in found] == ["127.0.0.1"] and found[0]["name"] == "Virtual WLED"
        assert await discovery.sweep(net, port=other_port) == []
    finally:
        srv.close()
        await wled.stop()


def test_sweep_networks_prefer_the_lan_over_docker_bridges():
    nets = discovery.sweep_networks(["172.17.0.1", "192.168.1.20", "10.8.0.5", "127.0.0.1", "8.8.8.8", "169.254.3.4"])
    assert nets == [ipaddress.ip_network("192.168.1.0/24"), ipaddress.ip_network("10.8.0.0/24")]
    assert discovery.sweep_networks(["172.17.0.1"]) == [ipaddress.ip_network("172.17.0.0/24")]


def test_discover_endpoint_reports_a_reason_not_a_bare_500(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from treelights.app import AppState, create_app

    async def broken(timeout=3.0):
        raise discovery.DiscoveryError("Couldn't search the network (mDNS: x; network sweep: y).")

    monkeypatch.setattr(discovery, "discover", broken)
    state = AppState(tmp_path, executor=concurrent.futures.ThreadPoolExecutor(1))
    try:
        with TestClient(create_app(state)) as c:
            r = c.get("/api/wled/discover")
            assert r.status_code == 502 and "Couldn't search the network" in r.json()["detail"]
    finally:
        state.output.stop()
