"""Which noise breaks tracking? Video-only decode stats for one variant.

    PYTHONPATH=. python tools/walk_ablate.py <variant> [duration]
"""
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np

from tools.walk_report import configs, small_tree
from treelights.sim.walksim import decode_video_frames, simulate
from treelights.walk.pipeline import Source, process_source
from treelights.walk.process import frames_from_images


def variant(name, duration, seed=3):
    cfg_i, fa_i = configs("ideal", duration, seed)
    cfg_n, fa_n = configs("noisy", duration, seed)
    return {
        "scene_faults_only": (cfg_i, fa_n),
        "walk_noise_only": (cfg_n, fa_i),
        "walk_noise_no_blur": (replace(cfg_n, exposure_s=0.0), fa_i),
        "walk_noise_no_jitter": (replace(cfg_n, jitter_deg=0.0, tremor_deg=0.0), fa_i),
        "foliage_only": (cfg_i, replace(fa_i, foliage=1.0, glint_prob=0.15)),
        "all": (cfg_n, fa_n),
        "ideal": (cfg_i, fa_i),
    }[name]


if __name__ == "__main__":
    name = sys.argv[1]
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 25
    tree = small_tree(150, 3)
    cfg, fa = variant(name, dur)
    d = Path(tempfile.mkdtemp())
    cap = simulate(tree, replace(cfg, live_fps=0.001), fa, str(d / "w.mp4"))
    fr, w, h = frames_from_images(decode_video_frames(cap.video_path))
    blobs = np.mean([len(f.blobs) for f in fr])
    res = process_source(Source("video", fr, w, h), 150, cap.log_t, cap.log_q, phone_time_fn=lambda t, tm: cap.rec_start + t)
    print(json.dumps({"variant": name, "blobs_per_frame": round(float(blobs), 1), "tracks": res.tracks.n_tracks,
                      "rows": len(res.tracks.rows), "decoded": len(res.decoded_leds()),
                      "timing": round(res.timing.contrast, 3), "gyro": None if res.gyro is None else round(res.gyro.residual_px, 2)}))
