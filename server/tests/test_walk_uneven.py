"""An uneven walk, the way people actually walk: closer on one side, an oval, off-centre
path, the phone raised and lowered, with every simulated fault. Decoded and solved,
then checked against the truth (map accuracy, connectors, up direction, tree axis)."""

import numpy as np
import pytest

from treelights.sim.scene import Faults
from treelights.sim.tree import make_tree
from treelights.sim.walksim import WalkConfig, decode_video_frames, jpeg_rgb, simulate
from treelights.walk.process import analyze, frames_from_images, solve_walk

N = 150


@pytest.mark.timeout(1500)
def test_uneven_walk_with_every_fault(tmp_path):
    k = (N / 600) ** 0.5
    tree = make_tree(n=N, seed=8, height=180 * k, base_radius=65 * k, bottom=25 * k)
    cfg = WalkConfig.uneven(duration=50.0, seed=8, led_stale_prob=0.03)
    faults = Faults(foliage=1.0, glint_prob=0.15, mirror=True, exposure_drift=0.002, tv_flicker=True, noise_sigma=1.5)
    cap = simulate(tree, cfg, faults, str(tmp_path / "walk.mp4"))
    live = frames_from_images((t, jpeg_rgb(j)) for t, j, _q in cap.live)
    video = frames_from_images(decode_video_frames(cap.video_path))
    wr = analyze(N, live, video, cap.log_t, cap.log_q, cap.rec_start)
    assert all(r.timing.ok for r in wr.results)
    span = float(np.ptp(tree.positions[:, 1]))
    m = solve_walk(wr, N, span, list(tree.breaks), {})
    st = np.array([l["status"] for l in m["leds"]])
    ok = st == "measured"
    assert ok.mean() > 0.85
    P = np.array([l["p"] for l, o in zip(m["leds"], ok) if o], float)
    Y = tree.positions[ok]
    ma, mb = P.mean(0), Y.mean(0)
    U, S, Vt = np.linalg.svd((P - ma).T @ (Y - mb))
    D = np.eye(3)
    D[2, 2] = np.sign(np.linalg.det(U @ Vt))
    R = (U @ D @ Vt).T
    s = (S * np.diag(D)).sum() / ((P - ma) ** 2).sum()
    err = np.linalg.norm(s * (P - ma) @ R.T + mb - Y, axis=1)
    assert abs(s - 1) < 0.03
    assert np.median(err) < 0.01 * span and err.max() < 0.05 * span
    assert sorted(l["from"] for l in m["chain"]["long_links"]) == sorted(tree.long_links)
    # The map's axis (x = z = 0, y up) against the tree's true axis.
    A = s * (np.array([[0, P[:, 1].min(), 0], [0, P[:, 1].max(), 0]]) - ma) @ R.T + mb
    up = (A[1] - A[0]) / np.linalg.norm(A[1] - A[0])
    assert np.degrees(np.arccos(np.clip(up[1], -1, 1))) < 2.0
    assert np.hypot(*A.mean(0)[[0, 2]]) < 0.1 * 65 * k
    assert m["quality"]["tree_axis"] == "led_outline"  # the off-centre path isn't mistaken for the axis
