"""Walk-around pipeline on simulated captures, checked against ground truth: the live
stream (~8 frames/s) and the 30 fps recording, without noise and with every fault."""

import numpy as np
import pytest

from treelights.reconstruction.camera import project
from treelights.sim.scene import Faults
from treelights.sim.tree import make_tree
from treelights.sim.walksim import WalkConfig, decode_video_frames, jpeg_rgb, simulate
from treelights.walk.process import analyze, frames_from_images

N = 120


def _tree(seed):
    k = (N / 600) ** 0.5
    return make_tree(n=N, seed=seed, height=180 * k, base_radius=65 * k, bottom=25 * k)


CONFIGS = {
    "ideal": (lambda: WalkConfig.ideal(duration=50.0, seed=21),
              lambda: Faults(foliage=0.0, glint_prob=0.0, noise_sigma=0.0, shot_noise=0.0)),
    "noisy": (lambda: WalkConfig(duration=50.0, seed=21, led_stale_prob=0.03),
              lambda: Faults(foliage=1.0, glint_prob=0.15, mirror=True, exposure_drift=0.002, tv_flicker=True,
                             noise_sigma=1.5)),
}


def _identity_errors(res, cap, rec_delay):
    pt = np.array([p[0] for p in cap.poses])
    cache = {}
    right = wrong = 0
    for r in res.tracks.rows:
        ident = res.identity(int(r[0]), int(r[1]))
        if ident is None:
            continue
        fr = res.source.frames[int(r[1])]
        st = fr.t - 1000.0 if res.source.name == "live" else fr.t + rec_delay
        k = int(np.argmin(np.abs(pt - st)))
        if k not in cache:
            cache[k] = project(cap.tree.positions, cap.poses[k][1], cap.poses[k][2], cap.scene.intr)[0]
        d = float(np.hypot(*(cache[k][ident.index] - r[2:4])))
        # Within 45 px: the LED itself or its light scattered onto branches nearby.
        right += d < 45
        wrong += d >= 45
    return right, wrong


@pytest.mark.timeout(1200)
@pytest.mark.parametrize("kind", ["ideal", "noisy"])
def test_walk_pipeline_against_truth(tmp_path, kind):
    cfg, faults = CONFIGS[kind][0](), CONFIGS[kind][1]()
    cap = simulate(_tree(21), cfg, faults, str(tmp_path / "walk.mp4"))
    live = frames_from_images((t, jpeg_rgb(j)) for t, j, _q in cap.live)
    video = frames_from_images(decode_video_frames(cap.video_path))
    wr = analyze(N, live, video, cap.log_t, cap.log_q, cap.rec_start)
    by = {r.source.name: r for r in wr.results}
    min_live = 0.95 if kind == "ideal" else 0.7
    assert len(by["video"].decoded_leds()) >= 0.95 * N
    assert len(by["live"].decoded_leds()) >= min_live * N
    for res in wr.results:
        assert res.timing.ok
        right, wrong = _identity_errors(res, cap, cfg.rec_delay_s)
        assert wrong <= 0.01 * (right + wrong), (res.source.name, right, wrong)
        # The orientation sensor's mapping onto the camera is found, with its time offset.
        g = res.gyro
        assert g is not None and np.allclose(g.M, cfg.device_M) and g.inverse == cfg.quat_inverse
        assert abs(g.f / cap.scene.intr.f - 1) < 0.06
        if res.source.name == "video":
            # (the expected offset includes half the exposure: blur is rendered over [t, t + exposure])
            assert abs(g.delta - (cfg.gyro_time_offset_s + cfg.exposure_s / 2)) < 0.03
    assert wr.info["keyframes"] >= 30
    assert wr.info["by_source"]["video"] >= 0.9 * wr.info["keyframes"]
