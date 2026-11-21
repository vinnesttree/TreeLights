"""End to end: the real app (HTTP + WebSockets + process pool) driven by the
simulated rig (virtual WLED over real DDP/UDP, virtual phone over WebSocket)."""

import asyncio
import concurrent.futures
import multiprocessing
import socket

import numpy as np
import pytest
import uvicorn

from treelights.app import AppState, create_app
from treelights.sim.rig import run_rig


@pytest.mark.timeout(900)
async def test_full_rig_over_http_and_websockets(tmp_path):
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn"))
    state = AppState(tmp_path, executor=pool, capture_executor=concurrent.futures.ThreadPoolExecutor(2))
    app = create_app(state)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        r = await run_rig(f"http://127.0.0.1:{port}", n=150, views=4, seed=7, verbose=False, drop_prob=0.03)
        m = r["map"]
        tree = r["tree"]
        st = np.array([l["status"] for l in m["leds"]])
        ok = st == "measured"
        assert ok.mean() > 0.6
        P = np.array([l["p"] for l in m["leds"]])[ok]
        Y = tree.positions[ok]
        ma, mb = P.mean(0), Y.mean(0)
        U, S, Vt = np.linalg.svd((P - ma).T @ (Y - mb))
        D = np.eye(3)
        D[2, 2] = np.sign(np.linalg.det(U @ Vt))
        R = (U @ D @ Vt).T
        s = (S * np.diag(D)).sum() / ((P - ma) ** 2).sum()
        err = np.linalg.norm(s * (P - ma) @ R.T + mb - Y, axis=1)
        span = np.ptp(tree.positions[:, 1])
        assert abs(s - 1) < 0.03
        assert np.median(err) < 0.01 * span and err.max() < 0.05 * span
        assert r["activate"]["id"] == r["map_id"]
        assert r["guidance"] and "Turn the tree" in r["guidance"]["message"]
        assert state.maps.active_id() == r["map_id"]
    finally:
        server.should_exit = True
        await task
        await state.session.stop() if state.session else None
        state.output.stop()
        pool.shutdown(wait=False, cancel_futures=True)
