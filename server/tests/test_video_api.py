import asyncio
import concurrent.futures
import socket

import httpx
import numpy as np
import pytest
import uvicorn

from treelights.app import AppState, create_app
from treelights.capture.video import Timeline
from treelights.sim.scene import Scene
from treelights.sim.synthvideo import write_video
from treelights.sim.tree import make_tree


@pytest.mark.timeout(600)
async def test_video_play_resumable_upload_and_decode(tmp_path):
    pool = concurrent.futures.ThreadPoolExecutor(2)
    state = AppState(tmp_path / "data", executor=pool)
    app = create_app(state)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60) as http:
            n = 70
            await http.put("/api/config", json={"wled": {"led_count": n}})
            scan = (await http.post("/api/scans")).json()["scan"]
            r = (await http.post(f"/api/scans/{scan}/video/play", json={"countdown_s": 0})).json()
            vid = r["view"]
            assert state.output.holder().owner == "video-sequence"
            meta = (await http.get(f"/api/scans/{scan}")).json()["meta"]
            tl_meta = next(v for v in meta["views"] if v["id"] == vid)["timeline"]
            tl = Timeline.build(tl_meta["n"], tl_meta["seed"], tl_meta["levels"], tl_meta["sync_level"])
            # Record it with the simulated phone.
            tree = make_tree(n=n, seed=5, height=110, base_radius=40, bottom=15)
            scene = Scene(tree, seed=5, width=960, height=540)
            scene.intr.f, scene.intr.cx, scene.intr.cy = 750.0, 479.5, 269.5
            scene.eye = scene.eye * np.array([1, 0.7, 0.7])
            scene.target = scene.target * 0.6
            scene.__post_init__()
            v = scene.add_view(0)
            path = tmp_path / "rec.mp4"
            write_video(str(path), scene, v, tl, seed=3)
            data = path.read_bytes()
            base = f"/api/scans/{scan}/video/{vid}"
            half = len(data) // 2
            assert (await http.put(f"{base}/upload?offset=0", content=data[:half])).json()["size"] == half
            # A retry with a stale offset is refused; the client resumes from the server's size.
            assert (await http.put(f"{base}/upload?offset=0", content=data[:10])).status_code == 409
            size = (await http.get(f"{base}/upload")).json()["size"]
            assert (await http.put(f"{base}/upload?offset={size}", content=data[size:])).json()["size"] == len(data)
            job = (await http.post(f"{base}/decode")).json()
            while job["status"] == "running":
                await asyncio.sleep(0.5)
                job = (await http.get(f"/api/jobs/{job['id']}")).json()
            assert job["status"] == "done", job
            res = job["result"]
            assert res["blocks_ok"] == res["blocks_total"]
            assert res["detections"] >= 0.8 * int((v.vis > 0).sum())
            meta = (await http.get(f"/api/scans/{scan}")).json()["meta"]
            assert next(x for x in meta["views"] if x["id"] == vid)["status"] == "done"
    finally:
        server.should_exit = True
        await task
        state.output.stop()
        pool.shutdown(wait=False)
