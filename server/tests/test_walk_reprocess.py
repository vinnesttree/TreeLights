"""Decoding a stored walk again with the current version, and solving again once its
raw recording has been deleted (Map page "Recordings")."""

import concurrent.futures
import json
import shutil
import time

import numpy as np
import pytest

from treelights.sim.scene import Faults
from treelights.sim.tree import make_tree
from treelights.sim.walksim import WalkConfig, simulate

N = 100


def _store_walk(state, tmp_path):
    """A walk stored the way WalkSession and the upload endpoint store it."""
    k = (N / 600) ** 0.5
    tree = make_tree(n=N, seed=12, height=180 * k, base_radius=65 * k, bottom=25 * k)
    cap = simulate(tree, WalkConfig.ideal(duration=40.0, seed=12),
                   Faults(foliage=0.0, glint_prob=0.0, noise_sigma=0.0, shot_noise=0.0), str(tmp_path / "rec.mp4"))
    scan = state.scans.create(N, {"output_breaks": list(tree.breaks)})
    vid = scan.add_view({"mode": "walk", "status": "walking"})
    d = scan.root / "walk" / vid
    (d / "live").mkdir(parents=True)
    with open(d / "live.jsonl", "w") as f:
        for i, (t, jpeg, q) in enumerate(cap.live):
            (d / "live" / f"{i:05d}.jpg").write_bytes(jpeg)
            f.write(json.dumps({"i": i, "t": t, "q": q.tolist(), "file": f"{i:05d}.jpg"}) + "\n")
    with open(d / "motion.jsonl", "w") as f:
        for t, q in zip(cap.log_t, cap.log_q):
            f.write(json.dumps({"t": float(t), "q": q.tolist()}) + "\n")
    (scan.root / "videos").mkdir()
    shutil.copy(cap.video_path, scan.root / "videos" / f"{vid}.upload")
    scan.set_view(vid, status="processed", rec_start=cap.rec_start, duration_s=40.0)
    return tree, scan, vid


def _wait(c, job):
    t0 = time.time()
    while job["status"] == "running" and time.time() - t0 < 900:
        time.sleep(1)
        job = c.get(f"/api/jobs/{job['id']}").json()
    return job


@pytest.mark.timeout(1500)
def test_decode_again_then_solve_again_without_raw_data(tmp_path):
    from fastapi.testclient import TestClient

    from treelights.app import AppState, create_app

    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1),
                     capture_executor=concurrent.futures.ThreadPoolExecutor(1))
    try:
        tree, scan, vid = _store_walk(state, tmp_path)
        span = float(np.ptp(tree.positions[:, 1]))
        state.config.update({"tree_height_cm": round(span, 2)})
        with TestClient(create_app(state)) as c:
            w = [x for x in c.get("/api/walks").json() if x["scan"] == scan.id][0]
            assert w["view"] == vid and w["video_mb"] > 0 and w["live_frames"] > 100 and w["keyframes"] == 0
            # Decode again (here: for the first time) from the stored recording and live frames.
            job = _wait(c, c.post(f"/api/scans/{scan.id}/walk/{vid}/process", json={"video": True}).json())
            assert job["status"] == "done", job.get("error")
            m1 = c.get(f"/api/maps/{job['result']['map']}").json()
            assert m1["quality"]["counts"]["measured"] > 0.9 * N
            w = [x for x in c.get("/api/walks").json() if x["scan"] == scan.id][0]
            assert w["keyframes"] > 20 and w["decoded"]["video"] > 0.9 * N
            # The raw data gets pruned (an older scan): decoding again is refused with a reason...
            shutil.rmtree(scan.root / "videos")
            shutil.rmtree(scan.root / "walk" / vid / "live")
            r = c.post(f"/api/scans/{scan.id}/walk/{vid}/process", json={"video": True})
            assert r.status_code == 409 and "Solve the scan again" in r.json()["detail"]
            w = [x for x in c.get("/api/walks").json() if x["scan"] == scan.id][0]
            assert w["video_mb"] == 0 and w["live_frames"] == 0 and w["keyframes"] > 20
            # ...and solving again from the stored keyframes still works.
            job = _wait(c, c.post(f"/api/scans/{scan.id}/solve", json={}).json())
            assert job["status"] == "done", job.get("error")
            m2 = c.get(f"/api/maps/{job['result']['map']}").json()
            assert m2["quality"]["counts"]["measured"] == m1["quality"]["counts"]["measured"]
    finally:
        state.output.stop()
