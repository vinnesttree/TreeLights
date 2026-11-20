"""Re-scan and maintenance (architecture §11.5)."""

import numpy as np
import pytest

from treelights.maps import rescan
from treelights.sim.harness import make_sim
from treelights.sim.scene import Faults


def _map(statuses, sigmas, pts):
    return {"leds": [{"status": s, "sigma": g, "p": p} for s, g, p in zip(statuses, sigmas, pts)]}


def test_low_confidence_selection():
    st = ["measured"] * 6 + ["single_view", "estimated", "unknown", "manual"]
    sig = [0.5, 0.6, 0.5, 0.55, 3.0, 0.5, 1.0, 2.0, None, 0.0]
    m = _map(st, sig, [[0, 0, 0]] * 10)
    # Not measured from 2+ views, plus the measured LED with a far larger uncertainty;
    # manual positions are the user's own and stay.
    assert rescan.low_confidence(m) == [4, 6, 7, 8]


def test_anchors_are_spread_and_well_measured():
    rng = np.random.default_rng(0)
    n = 400
    az = rng.uniform(-np.pi, np.pi, n)
    y = rng.uniform(0, 150, n)
    r = 40 * (1 - y / 180)
    pts = np.c_[r * np.sin(az), y, r * np.cos(az)]
    sig = rng.uniform(0.2, 2.0, n)
    st = ["measured"] * n
    st[5] = "estimated"
    m = _map(st, sig.tolist(), pts.tolist())
    a = rescan.anchors(m, exclude={1, 2, 3})
    assert 30 <= len(a) <= rescan.MAX_ANCHORS
    assert not {1, 2, 3, 5} & set(a)
    # Around the whole tree and up its height.
    sec = {int((az[i] + np.pi) / (2 * np.pi) * 12) % 12 for i in a}
    assert len(sec) == 12
    assert np.ptp(y[a]) > 0.8 * np.ptp(y)
    # The best-measured LED of its cell.
    assert np.median(sig[a]) < np.median(sig)


def test_parse_selection():
    assert rescan.parse_selection("12, 40-43 7  3-1", 100) == [1, 2, 3, 7, 12, 40, 41, 42, 43]
    assert rescan.parse_selection("98-105", 100) == [98, 99]
    with pytest.raises(ValueError):
        rescan.parse_selection("12, abc", 100)


@pytest.mark.timeout(1200)
async def test_rescan_view_adds_a_camera_for_the_chosen_leds(tmp_path):
    from treelights.reconstruction.job import run_solve

    sim = await make_sim(tmp_path, n=120, seed=4, faults=Faults(foliage=1.0, glint_prob=0.1))
    try:
        for k in range(3):
            await sim.run_view(120.0 * k, "hybrid")
        span = float(np.ptp(sim.tree.positions[:, 1]))
        m1 = run_solve(str(sim.scan.root), span, None, None)
        # Re-scan from a side between two views: the low-confidence LEDs, or (if there
        # are few) the LEDs facing that side.
        leds = rescan.low_confidence(m1)
        P = sim.tree.positions
        facing = [i for i in range(len(P)) if abs(((np.degrees(np.arctan2(P[i, 0], P[i, 2])) - 60 + 180) % 360) - 180) < 30]
        if len(leds) < 10:
            leds = sorted(set(leds) | set(facing[:25]))
        anc = rescan.anchors(m1, exclude=set(leds))
        capture = sorted(set(leds) | set(anc))
        vid = await sim.run_view(60.0, "rescan", leds=capture)
        view = sim.scan.view(vid)
        assert view["status"] == "done" and view["mode"] == "rescan"
        recorded = {int(r["led"]) for r in sim.scan.detections.read() if r.get("segment") in view["segments"]}
        assert recorded and recorded <= set(capture)  # nothing else was captured
        m2 = run_solve(str(sim.scan.root), span, m1, None)
    finally:
        await sim.close()
    q1, q2 = m1["quality"], m2["quality"]
    assert q2["registered_segments"] > q1["registered_segments"]  # the new camera was placed
    seen_before = sum(m1["leds"][i]["segments"] for i in leds)
    seen_after = sum(m2["leds"][i]["segments"] for i in leds)
    assert seen_after > seen_before
    assert q2["counts"]["measured"] >= q1["counts"]["measured"]
    # Still accurate, and in the same frame as before (frame continuity, §9.10).
    ok = np.array([l["status"] == "measured" for l in m2["leds"]])
    p1 = {i: np.array(l["p"]) for i, l in enumerate(m1["leds"]) if l["status"] == "measured"}
    common = [i for i in p1 if ok[i]]
    d = np.array([np.linalg.norm(np.array(m2["leds"][i]["p"]) - p1[i]) for i in common])
    assert np.median(d) < 0.01 * span


def test_rescan_api_sets_up_tripod_and_walk_rescans(tmp_path):
    import concurrent.futures

    from fastapi.testclient import TestClient

    from treelights.app import AppState, create_app

    state = AppState(tmp_path, executor=concurrent.futures.ThreadPoolExecutor(1),
                     capture_executor=concurrent.futures.ThreadPoolExecutor(1))
    try:
        n = 60
        rng = np.random.default_rng(1)
        pts = rng.normal(0, 20, (n, 3)).tolist()
        st = ["measured"] * n
        st[10:14] = ["estimated"] * 4

        def make(kind: str) -> str:
            scan = state.scans.create(n)
            vid = scan.add_view({"mode": kind})
            scan.set_view(vid, status="processed" if kind == "walk" else "done")
            m = _map(st, [0.5] * n, pts)
            m.update(id=f"map-{kind}", scan=scan.id)
            return state.maps.save(m)

        tripod, walk = make("hybrid"), make("walk")
        with TestClient(create_app(state)) as c:
            info = c.get(f"/api/maps/{tripod}/rescan").json()
            assert info["kind"] == "tripod" and info["low_confidence"] == [10, 11, 12, 13]
            r = c.post(f"/api/maps/{tripod}/rescan", json={"low_confidence": True}).json()
            assert r["leds"] == [10, 11, 12, 13] and len(r["anchors"]) >= 10
            s = c.get("/api/state").json()
            assert s["scan"] == r["scan"] and s["rescan"]["leds"] == 4  # the session is on the map's scan
            meta = state.scans.get(r["scan"]).meta["rescan"]
            assert set(meta["capture"]) == set(r["leds"]) | set(r["anchors"])
            r = c.post(f"/api/maps/{walk}/rescan", json={"text": "1-3, 50"}).json()
            assert r["kind"] == "walk" and r["leds"] == [1, 2, 3, 50] and "anchors" not in r
            assert c.post(f"/api/maps/{walk}/rescan", json={"text": "x"}).status_code == 400
            assert c.post("/api/leds/show", json={"leds": [1, 2]}).json()["count"] == 2
    finally:
        state.output.stop()
