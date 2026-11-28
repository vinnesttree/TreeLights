"""Simulated rig: a virtual WLED and a virtual phone driving a real TreeLights
server over HTTP and WebSockets, turning the virtual tree between views.

    python -m treelights.sim.rig --server http://127.0.0.1:8443 --views 8

Used by the end-to-end test and as a hardware-free demo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ssl

import httpx
import numpy as np
import websockets

from .scene import Faults, Scene
from .tree import make_tree
from .virtual_phone import VirtualPhone
from .virtual_wled import VirtualWled, VirtualWledGroup

log = logging.getLogger(__name__)


async def run_rig(server: str, n: int = 600, views: int = 8, mode: str = "hybrid", seed: int = 0,
                  faults: Faults | None = None, drop_prob: float = 0.0, solve: bool = True, verbose: bool = True,
                  output_break_at: int | None = None, controllers: list[int] | None = None,
                  delays_ms: list[tuple[float, float]] | None = None, tree_id: str | None = None,
                  tree_kw: dict | None = None) -> dict:
    """controllers: the LEDs split over several virtual WLED controllers with these LED
    counts (in index order), each with its own network delay (delays_ms[k], ms), as one
    tree. tree_id: the server's tree to scan (default: its default tree)."""
    if controllers and sum(controllers) != n:
        raise ValueError("the controllers' LED counts must add up to n")
    starts = [sum(controllers[:j]) for j in range(1, len(controllers))] if controllers else None
    tree = make_tree(n=n, seed=seed, output_break_at=output_break_at, breaks_at=starts, **(tree_kw or {}))
    scene = Scene(tree, seed=seed, faults=faults or Faults())
    if n < 600 and tree_kw:
        # A smaller tree: bring the camera closer so it fills a similar part of the image.
        k = (n / 600) ** 0.5
        scene.eye = scene.eye * np.array([1, k, k])
        scene.target = scene.target * k
        scene.__post_init__()
    if controllers:
        wled = VirtualWledGroup(controllers, seed=seed, delays_ms=delays_ms)
        for w in wled:
            w.faults.drop_prob = drop_prob
    else:
        buses = [(0, output_break_at), (output_break_at, n - output_break_at)] if output_break_at else [(0, n)]
        wled = VirtualWled(n, buses=buses, seed=seed)
        wled.faults.drop_prob = drop_prob
        if delays_ms:
            wled.faults.delay_ms = tuple(delays_ms[0])
    await wled.start()
    sslctx = ssl.create_default_context()
    sslctx.check_hostname = False
    sslctx.verify_mode = ssl.CERT_NONE  # the simulator talks to a local self-signed server
    ws_base = server.replace("https://", "wss://").replace("http://", "ws://")
    wss_ssl = sslctx if ws_base.startswith("wss://") else None
    params = {"tree": tree_id} if tree_id else None
    ws_query = f"?tree={tree_id}" if tree_id else ""
    async with httpx.AsyncClient(base_url=server, verify=False, timeout=30, params=params) as http:
        checks = None
        if controllers:
            (await http.put("/api/controllers", json=wled.settings())).raise_for_status()
            checks = (await http.get("/api/wled/checks")).json()
        else:
            await http.put("/api/config", json={"wled": {"host": "127.0.0.1", "http_port": wled.http_port,
                                                          "udp_port": wled.udp_port, "led_count": n}})
            r = await http.post("/api/wled/select", json={"host": "127.0.0.1"})
            r.raise_for_status()
        span = float(np.ptp(tree.positions[:, 1]))
        await http.put("/api/config", json={"tree_height_cm": round(span, 2)})
        scan = (await http.post("/api/scans")).json()["scan"]
        phone = VirtualPhone(scene, wled, latency_ms=60, seed=seed)
        async with websockets.connect(f"{ws_base}/ws/capture{ws_query}", ssl=wss_ssl, max_size=None) as cam, \
                websockets.connect(f"{ws_base}/ws/control{ws_query}", ssl=wss_ssl, max_size=None) as ctl:
            async def send_text(m: dict) -> None:
                await cam.send(json.dumps(m))

            async def send_bin(b: bytes) -> None:
                await cam.send(b)

            await cam.send(json.dumps(phone.hello()))
            await phone.start(send_text, send_bin)

            async def cam_loop() -> None:
                async for msg in cam:
                    if isinstance(msg, str):
                        await phone.handle(msg)

            cam_task = asyncio.ensure_future(cam_loop())
            statuses: list[dict] = []
            for k in range(views):
                scene.add_view(360.0 * k / views)
                phone.view_index = k
                await ctl.send(json.dumps({"type": "start_view", "mode": mode}))
                started = False
                while True:
                    st = json.loads(await asyncio.wait_for(ctl.recv(), timeout=600))
                    if st.get("type") == "error":
                        raise RuntimeError(st["message"])
                    if st.get("type") != "status":
                        continue
                    if st.get("view") == f"v{k + 1}":
                        started = True
                    if started and st.get("phase") in ("done", "failed", "stopped"):
                        statuses.append(st)
                        if verbose:
                            print(f"view {k + 1}: {st['phase']} {st.get('message', '')}")
                        break
                if st["phase"] != "done":
                    raise RuntimeError(f"view {k + 1} {st['phase']}: {st.get('message')}")
            # Wait briefly for guidance from the last view.
            guidance = None
            try:
                while True:
                    st = json.loads(await asyncio.wait_for(ctl.recv(), timeout=60))
                    if st.get("guidance") and st["guidance"].get("measured") is not None:
                        guidance = st["guidance"]
                        break
            except asyncio.TimeoutError:
                pass
            phone.stop()
            cam_task.cancel()
        result = {"scan": scan, "statuses": statuses, "guidance": guidance, "tree": tree, "scene": scene,
                  "checks": checks}
        if solve:
            job = (await http.post(f"/api/scans/{scan}/solve")).json()
            while job["status"] == "running":
                await asyncio.sleep(1)
                job = (await http.get(f"/api/jobs/{job['id']}")).json()
            if job["status"] != "done":
                raise RuntimeError(f"solve failed: {job['error']}")
            mid = job["result"]["map"]
            result["map_id"] = mid
            result["map"] = (await http.get(f"/api/maps/{mid}")).json()
            result["activate"] = (await http.post(f"/api/maps/{mid}/activate", json={})).json()
            if verbose:
                q = result["map"]["quality"]
                print(f"map {mid}: measured {q['counts']}, median err {q['median_err_px']} px, warnings {q['warnings']}")
    await wled.stop()
    return result


def main() -> None:
    p = argparse.ArgumentParser(prog="treelights.sim.rig")
    p.add_argument("--server", default="https://127.0.0.1:8443")
    p.add_argument("--n", type=int, default=600)
    p.add_argument("--views", type=int, default=8)
    p.add_argument("--mode", default="hybrid", choices=["hybrid", "sequential"])
    p.add_argument("--drop", type=float, default=0.0, help="DDP packet loss probability")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_rig(a.server, a.n, a.views, a.mode, drop_prob=a.drop))


if __name__ == "__main__":
    main()
