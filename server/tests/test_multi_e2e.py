"""End to end with several WLED controllers (architecture §5.5, §17): the real app (HTTP,
WebSockets, process pool, DDP over UDP) driven by the simulator.

- One tree split over two virtual controllers, with different network delays (timing
  skew): a walk-around scan and a tripod scan each map both halves as one tree, with a
  chain break at the boundary.
- Two independent trees: a walk on one while an effect runs on the other."""

import asyncio
import concurrent.futures
import multiprocessing
import socket
import time

import httpx
import numpy as np
import pytest
import uvicorn

from treelights.app import AppState, create_app
from treelights.sim.rig import run_rig
from treelights.sim.scene import Faults
from treelights.sim.virtual_walker import run_walk_rig
from treelights.sim.virtual_wled import VirtualWled
from treelights.sim.walksim import WalkConfig


def _align_error(m, tree):
    st = np.array([l["status"] for l in m["leds"]])
    ok = st == "measured"
    P = np.array([l["p"] for l, o in zip(m["leds"], ok) if o], float)
    Y = tree.positions[ok]
    ma, mb = P.mean(0), Y.mean(0)
    U, S, Vt = np.linalg.svd((P - ma).T @ (Y - mb))
    D = np.eye(3)
    D[2, 2] = np.sign(np.linalg.det(U @ Vt))
    R = (U @ D @ Vt).T
    s = (S * np.diag(D)).sum() / ((P - ma) ** 2).sum()
    return ok, s, np.linalg.norm(s * (P - ma) @ R.T + mb - Y, axis=1)


async def _serve(tmp_path):
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn"))
    state = AppState(tmp_path, executor=pool, capture_executor=concurrent.futures.ThreadPoolExecutor(2))
    app = create_app(state)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.05)
    return state, pool, server, task, sock.getsockname()[1]


async def _shutdown(state, pool, server, task):
    server.should_exit = True
    await task  # the app's shutdown stops every tree
    pool.shutdown(wait=False, cancel_futures=True)


def _check_split_map(r, state, counts, n, measured=0.85, half=0.8):
    m, tree = r["map"], r["tree"]
    ok, s, err = _align_error(m, tree)
    span = np.ptp(tree.positions[:, 1])
    first = counts[0]
    assert ok.mean() > measured
    # The map's units come from the tree height, lowest to highest *measured* LED (§9.10).
    # Both simulated strings start at the lowest branches, and a few of their first LEDs
    # can stay unmeasured (unbounded ends of a chain are `unknown`, §9.9); then the scale
    # is that of the measured extent.
    y = tree.positions[:, 1]
    assert abs(s - np.ptp(y[ok]) / np.ptp(y)) < 0.03
    assert np.median(err) < 0.01 * span and err.max() < 0.05 * span
    # Both halves were mapped, as one tree in one index space.
    assert ok[:first].mean() > half and ok[first:].mean() > half
    breaks = {b["from"]: b["reason"] for b in m["chain"]["breaks"]}
    assert breaks.get(first - 1) == "break_controller"
    # The map's WLED fingerprint covers both controllers (§11.4).
    w = m["wled"]
    assert w["led_count"] == n and len(w["controllers"]) == 2
    assert [c["start"] for c in w["controllers"]] == [0, first]
    assert len({c["mac"] for c in w["controllers"]}) == 2 and w["config_hash"]
    assert [row["placed"] > half for row in m["quality"]["controllers"]] == [True, True]
    assert not any("Controller" in x for x in m["quality"]["warnings"])
    assert state.maps.active_id() == r["map_id"] and state.map_check() is None
    # Setup checks ran on each controller.
    checks = r["checks"]
    assert checks[0]["name"] == "controllers"
    assert {(c["controller"], c["name"]) for c in checks if c["status"] == "ok" and c.get("controller") is not None} >= {
        (k, name) for k in (0, 1) for name in ("reachable", "led_count", "realtime")}


@pytest.mark.timeout(1500)
async def test_walk_around_one_tree_on_two_controllers_with_skew(tmp_path):
    """The walk pattern runs over both controllers at once. Controller 2 receives its
    packets 20 ms later than controller 1 (simulated network delay), so its LEDs switch
    slots later; the decoder only uses frames in the middle 60% of each 200 ms slot."""
    state, pool, server, task, port = await _serve(tmp_path)
    counts, n = [80, 70], 150
    try:
        r = await run_walk_rig(f"http://127.0.0.1:{port}", n=n, cfg=WalkConfig.ideal(duration=42.0, seed=5),
                               faults=Faults(foliage=0.0, glint_prob=0.0, noise_sigma=0.0, shot_noise=0.0), seed=5,
                               verbose=False, workdir=str(tmp_path), controllers=counts,
                               delays_ms=[(2.0, 8.0), (22.0, 28.0)])
        last = r["statuses"][-1]
        assert last["locked"] and last["identified"] > 60
        walk = r["map"]["quality"]["walk"][r["view"]]
        assert walk["decoded"]["video"] > 0.85 * n and walk["timing_ok"]["video"]
        _check_split_map(r, state, counts, n)
        assert state.scans.get(r["scan"]).meta["controller_breaks"] == [79]
    finally:
        await _shutdown(state, pool, server, task)


@pytest.mark.timeout(1500)
async def test_tripod_scan_of_one_tree_on_two_controllers_with_skew(tmp_path):
    """Tripod capture lights LEDs on either controller one at a time and checks every
    frame; with controller 2 slower, a frame taken too early shows the previous LED or
    none, and is rejected and retried, never misidentified."""
    state, pool, server, task, port = await _serve(tmp_path)
    counts, n = [90, 60], 150
    try:
        r = await run_rig(f"http://127.0.0.1:{port}", n=n, views=4, seed=7, verbose=False, drop_prob=0.03,
                          controllers=counts, delays_ms=[(2.0, 8.0), (25.0, 35.0)])
        assert r["activate"]["id"] == r["map_id"]
        # Four tripod views measure only part of the LEDs from two or more views; the same
        # bounds as test_e2e.py's single-controller scan.
        _check_split_map(r, state, counts, n, measured=0.6, half=0.5)
    finally:
        await _shutdown(state, pool, server, task)


@pytest.mark.timeout(1500)
async def test_two_independent_trees(tmp_path):
    """Tree 2 is scanned (a walk) while tree 1 runs an effect: separate controllers,
    outputs, leases, scans and maps."""
    state, pool, server, task, port = await _serve(tmp_path)
    base = f"http://127.0.0.1:{port}"
    wled1 = VirtualWled(60, mac="02:00:00:00:51:01")
    await wled1.start()
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as http:
            tid = (await http.post("/api/trees", json={"name": "Porch tree"})).json()["id"]
            assert tid == "tree2"
            await http.put("/api/config", json={"wled": {"host": "127.0.0.1", "http_port": wled1.http_port,
                                                          "udp_port": wled1.udp_port, "led_count": 60}})
            (await http.post("/api/wled/select", json={"host": "127.0.0.1", "http_port": wled1.http_port})).raise_for_status()
            r = await http.post("/api/lights", json={"on": True, "effect": "solid", "brightness": 0.5,
                                                     "params": {"colors": ["#ff0000"]}})
            assert r.json()["on"]
            walk_task = asyncio.ensure_future(run_walk_rig(
                base, n=120, cfg=WalkConfig.ideal(duration=38.0, seed=3),
                faults=Faults(foliage=0.0, glint_prob=0.0, noise_sigma=0.0, shot_noise=0.0), seed=3, verbose=False,
                workdir=str(tmp_path), tree_id=tid, mac_base=0x52))
            # While tree 2 is walked, tree 1's effect keeps its LEDs (red) and its lease.
            seen_walk = False
            while not walk_task.done():
                await asyncio.sleep(1.0)
                t1 = (await http.get("/api/trees")).json()
                lease = {t["id"]: t["lease"] for t in t1}
                assert lease["tree1"] and lease["tree1"]["owner"] == "test"
                if lease[tid] and lease[tid]["owner"] == "walk":
                    seen_walk = True
                    shown = wled1.state_at(time.monotonic())
                    assert wled1.live and (shown[:, 0] > 0).all() and (shown[:, 1] == 0).all()
            r2 = walk_task.result()
            assert seen_walk
            ok, s, err = _align_error(r2["map"], r2["tree"])
            assert ok.mean() > 0.85 and abs(s - 1) < 0.03
            # Each tree has its own maps and scans.
            assert state.trees[tid].maps.active_id() == r2["map_id"]
            assert state.trees["tree1"].maps.active_id() is None
            assert (await http.get("/api/maps")).json() == []
            assert [m["id"] for m in (await http.get(f"/api/trees/{tid}/maps")).json()] == [r2["map_id"]]
            assert r2["scan"] in {s["id"] for s in (await http.get("/api/scans", params={"tree": tid})).json()}
            assert (await http.get("/api/scans")).json() == []
            # On/off is per tree.
            assert (await http.get("/api/lights")).json()["on"]
            assert not (await http.get(f"/api/trees/{tid}/lights")).json()["on"]
            assert (await http.post(f"/api/trees/{tid}/lights", json={"on": True, "effect": "helix"})).json()["on"]
            assert (await http.post("/api/lights", json={"on": False})).json()["on"] is False
            assert (await http.get(f"/api/trees/{tid}/lights")).json()["effect"] == "helix"
            assert wled1.state["on"] is False
    finally:
        await _shutdown(state, pool, server, task)
        await wled1.stop()
