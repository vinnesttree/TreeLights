"""A walk with realistic LED sizes. Real LEDs are extended sources: from 1-2 m with a phone
camera each covers many pixels, more so close up or at high resolution. The point/scattered-light
test then has to judge against the frame's typical LED, not a fixed size in pixels: with the
fixed size, every LED counted as scattered light, too few were left to place the cameras, and
the solve failed outright (a real walk identified ~95% of its LEDs live and measured ~5%)."""

import numpy as np
import pytest

from treelights.sim.scene import Faults
from treelights.sim.tree import make_tree
from treelights.sim.walksim import WalkConfig, decode_video_frames, jpeg_rgb, simulate
from treelights.walk.process import analyze, frames_from_images, solve_walk

N = 150


@pytest.mark.timeout(1500)
def test_walk_with_realistic_led_sizes(tmp_path):
    k = (N / 600) ** 0.5
    tree = make_tree(n=N, seed=8, height=180 * k, base_radius=65 * k, bottom=25 * k)
    cfg = WalkConfig.uneven(duration=50.0, seed=8, led_radius_cm=0.35)
    faults = Faults(foliage=1.0, glint_prob=0.15, mirror=True, exposure_drift=0.002, tv_flicker=True, noise_sigma=1.5)
    cap = simulate(tree, cfg, faults, str(tmp_path / "walk.mp4"))
    live = frames_from_images((t, jpeg_rgb(j)) for t, j, _q in cap.live)
    video = frames_from_images(decode_video_frames(cap.video_path))
    wr = analyze(N, live, video, cap.log_t, cap.log_q, cap.rec_start)
    sharp = np.concatenate([f.blobs[:, 4] for f in video[0][::30] if len(f.blobs)])
    assert np.mean(sharp >= 0.5) < 0.5  # the fixed size would call most LEDs scattered light
    span = float(np.ptp(tree.positions[:, 1]))
    m = solve_walk(wr, N, span, list(tree.breaks), {})
    st = np.array([l["status"] for l in m["leds"]])
    ok = st == "measured"
    assert ok.mean() > 0.65
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
    assert np.median(err) < 0.01 * span and np.percentile(err, 95) < 0.03 * span
