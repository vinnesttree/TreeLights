"""Closed-loop capture tests: real session + output thread + DDP over UDP,
virtual WLED and virtual phone (architecture §17)."""

import numpy as np
import pytest

from treelights.sim.harness import identity_errors, make_sim
from treelights.sim.scene import Faults


def summarize(sim):
    ids = identity_errors(sim)
    obs = sim.scan.observations()
    seen = {led for s in obs.values() for led, c in s.items() if c}
    return ids, seen


@pytest.mark.timeout(300)
async def test_hybrid_view_clean(tmp_path):
    sim = await make_sim(tmp_path, n=120, seed=3)
    try:
        await sim.run_view(0, "hybrid")
        view = sim.scan.meta["views"][0]
        assert view["status"] == "done", view
        ids, seen = summarize(sim)
        visible = int((sim.scene.views[0].vis > 0).sum())
        assert ids["wrong"] == [], ids["wrong"][:5]
        assert len(seen) >= 0.85 * visible
        evs = [e["type"] for e in sim.scan.events.read()]
        assert "binary_done" in evs
    finally:
        await sim.close()


@pytest.mark.timeout(300)
async def test_sequential_with_packet_loss_and_latency_spikes(tmp_path):
    sim = await make_sim(tmp_path, n=80, seed=4)
    sim.wled.faults.drop_prob = 0.08
    sim.wled.faults.spike_prob = 0.05
    try:
        await sim.run_view(0, "sequential")
        assert sim.scan.meta["views"][0]["status"] == "done"
        ids, seen = summarize(sim)
        assert ids["wrong"] == [], ids["wrong"][:5]
        rejected = [e for e in sim.scan.events.read() if e["type"] == "rejected"]
        assert rejected, "packet loss should have produced rejected (stale) frames"
        visible = int((sim.scene.views[0].vis > 0).sum())
        assert len(seen) >= 0.8 * visible
    finally:
        await sim.close()


@pytest.mark.timeout(300)
async def test_bump_starts_new_segment_and_recaptures(tmp_path):
    sim = await make_sim(tmp_path, n=80, seed=5, faults=Faults(bump_at_frame=60, bump_px=5))
    try:
        await sim.run_view(0, "sequential")
        view = sim.scan.meta["views"][0]
        assert view["status"] == "done"
        assert len(view["segments"]) >= 2, "movement should have started a new segment"
        assert any(e["type"] == "invalidate" for e in sim.scan.events.read())
        # Identity check against the pose in effect: the bump happened before the second segment.
        obs = sim.scan.observations()
        last = view["segments"][-1]
        true = sim.scene.apparent_uv(sim.scene.views[0])  # current (bumped) pose
        errs = [np.hypot(*(true[led] - [c[0]["x"], c[0]["y"]])) for led, c in obs.get(last, {}).items() if c]
        assert errs and np.median(errs) < 1.0
    finally:
        await sim.close()


@pytest.mark.timeout(1200)
async def test_branch_occlusion_and_reflections_full_pipeline(tmp_path):
    """Rendered frames with LEDs hidden behind branches, light scattered by the branches
    and needle glints; captured through the real session, then solved."""
    from treelights.reconstruction.job import run_solve

    sim = await make_sim(tmp_path, n=150, seed=21, faults=Faults(foliage=1.5, glint_prob=0.3))
    try:
        for k in range(6):
            await sim.run_view(60.0 * k, "hybrid")
    finally:
        await sim.close()
    tree = sim.tree
    span = float(np.ptp(tree.positions[:, 1]))
    m = run_solve(str(sim.scan.root), span, None, None)
    st = np.array([l["status"] for l in m["leds"]])
    meas = st == "measured"
    assert meas.mean() >= 0.95
    P = np.array([l["p"] for l in m["leds"]])[meas]
    Y = tree.positions[meas]
    ma, mb = P.mean(0), Y.mean(0)
    U, S, Vt = np.linalg.svd((P - ma).T @ (Y - mb))
    D = np.eye(3)
    D[2, 2] = np.sign(np.linalg.det(U @ Vt))
    R = (U @ D @ Vt).T
    s = (S * np.diag(D)).sum() / ((P - ma) ** 2).sum()
    err = np.linalg.norm(s * (P - ma) @ R.T + mb - Y, axis=1)
    assert np.median(err) < 0.01 * span and err.max() < tree.link
    assert {l["from"] for l in m["chain"]["long_links"]} >= set(tree.long_links)
    assert not any("inside-out" in w for w in m["quality"]["warnings"])
