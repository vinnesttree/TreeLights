"""End to end walk-around capture: the real app (HTTP, /ws/walk, process pool, DDP to
a virtual WLED) driven by a virtual phone walking around the simulated tree in real
time, streaming live frames and orientation samples, then uploading its recording.
Run without noise and with every simulated fault."""

import asyncio
import concurrent.futures
import multiprocessing
import socket

import numpy as np
import pytest
import uvicorn

from treelights.app import AppState, create_app
from treelights.sim.scene import Faults
from treelights.sim.virtual_walker import run_walk_rig
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


CONFIGS = {
    "ideal": (lambda: WalkConfig.ideal(duration=42.0, seed=5),
              lambda: Faults(foliage=0.0, glint_prob=0.0, noise_sigma=0.0, shot_noise=0.0)),
    "noisy": (lambda: WalkConfig(duration=42.0, seed=5, led_stale_prob=0.03),
              lambda: Faults(foliage=1.0, glint_prob=0.15, mirror=True, exposure_drift=0.002, tv_flicker=True,
                             noise_sigma=1.5)),
}


@pytest.mark.timeout(1500)
@pytest.mark.parametrize("kind", ["ideal", "noisy"])
async def test_walk_around_over_http_and_websockets(tmp_path, kind):
    state, pool, server, task, port = await _serve(tmp_path)
    cfg, faults = CONFIGS[kind][0](), CONFIGS[kind][1]()
    try:
        r = await run_walk_rig(f"http://127.0.0.1:{port}", n=150, cfg=cfg, faults=faults, seed=5, verbose=False,
                               workdir=str(tmp_path))
        # Live feedback kept coming during the whole walk (each update decodes a bounded
        # window, so it doesn't fall behind as the walk goes on).
        times = [s["elapsed_s"] for s in r["statuses"] if "elapsed_s" in s]
        assert len(times) >= 10 and times[-1] > cfg.duration - 8
        if kind == "ideal":
            # The pattern locked, LEDs were identified, and the walk went (almost) all the way round.
            # (With every scene fault, rendering in real time here only reaches ~4 live frames/s,
            # half a phone's rate, too few to decode; the offline tests cover the noisy live
            # stream at 8 frames/s.)
            last = r["statuses"][-1]
            assert last["locked"] and last["identified"] > 60
            assert sum(1 for s in last["sectors"] if s > 0) >= 8
        m, tree = r["map"], r["tree"]
        ok, s, err = _align_error(m, tree)
        span = np.ptp(tree.positions[:, 1])
        assert ok.mean() > 0.85
        assert abs(s - 1) < 0.03
        assert np.median(err) < 0.01 * span and err.max() < 0.05 * span
        assert sorted(l["from"] for l in m["chain"]["long_links"]) == sorted(tree.long_links)
        walk = m["quality"]["walk"][r["view"]]
        assert walk["decoded"]["video"] > 0.85 * 150 and walk["timing_ok"]["video"]
        assert state.maps.active_id() == r["map_id"]
        # Both sources were stored; re-solving the scan reuses the walk's keyframes.
        scan = state.scans.get(r["scan"])
        assert (scan.root / "walk" / r["view"] / "observations.json").exists()
        assert scan.view(r["view"])["status"] == "processed"
    finally:
        server.should_exit = True
        await task
        if state.walk:
            await state.walk.stop()
        state.output.stop()
        pool.shutdown(wait=False, cancel_futures=True)
