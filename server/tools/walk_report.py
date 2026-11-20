"""Walk-around capture in simulation, checked against ground truth at every stage.

    PYTHONPATH=. python tools/walk_report.py ideal|noisy [n] [duration] [seed]
"""

import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from treelights.reconstruction.camera import project
from treelights.sim.scene import Faults
from treelights.sim.tree import make_tree
from treelights.sim.walksim import WalkConfig, decode_video_frames, jpeg_rgb, simulate
from treelights.walk.process import analyze, frames_from_images, solve_walk


def small_tree(n: int, seed: int):
    k = (n / 600) ** 0.5 if n < 600 else 1.0
    return make_tree(n=n, seed=seed, height=180 * k, base_radius=65 * k, bottom=25 * k)


def configs(kind: str, duration: float, seed: int):
    if kind == "ideal":
        return WalkConfig.ideal(duration=duration, seed=seed), Faults(foliage=0.0, glint_prob=0.0, noise_sigma=0.0, shot_noise=0.0)
    return (WalkConfig(duration=duration, seed=seed, led_stale_prob=0.03),
            Faults(foliage=1.0, glint_prob=0.15, mirror=True, exposure_drift=0.002, tv_flicker=True,
                   person_frames=(300, 330), noise_sigma=1.5))


def identity_check(res, cap, video_offset_scene: float) -> dict:
    """Decoded tracks vs truth: detections at the LED (<3 px), near it (reflection), or wrong."""
    poses = cap.poses
    pt = np.array([p[0] for p in poses])
    right = refl = wrong = 0
    P = cap.tree.positions
    for r in res.tracks.rows:
        ident = res.identity(int(r[0]), int(r[1]))
        if ident is None:
            continue
        fr = res.source.frames[int(r[1])]
        scene_t = fr.t - 1000.0 if res.source.name == "live" else fr.t + video_offset_scene
        k = int(np.argmin(np.abs(pt - scene_t)))
        uv, _ = project(P[ident.index : ident.index + 1], poses[k][1], poses[k][2], cap.scene.intr)
        d = float(np.hypot(*(uv[0] - r[2:4])))
        if d < 3:
            right += 1
        elif d < 45:
            refl += 1
        else:
            wrong += 1
    return {"right": right, "reflection": refl, "wrong": wrong}


def run(kind: str = "ideal", n: int = 150, duration: float = 40.0, seed: int = 3, use_video: bool = True,
        use_live: bool = True, verbose: bool = True) -> dict:
    tree = small_tree(n, seed)
    cfg, faults = configs(kind, duration, seed)
    d = Path(tempfile.mkdtemp())
    t0 = time.time()
    cap = simulate(tree, cfg, faults, str(d / "walk.mp4") if use_video else None)
    t_sim = time.time() - t0
    live = frames_from_images((t, jpeg_rgb(j)) for t, j, _q in cap.live) if use_live else None
    video = frames_from_images(decode_video_frames(cap.video_path)) if use_video else None
    t1 = time.time()
    wr = analyze(n, live, video, cap.log_t, cap.log_q, cap.rec_start)
    t_an = time.time() - t1
    out = {"kind": kind, "frames_live": len(cap.live), "frames_video": len(cap.video_t), "sim_s": round(t_sim, 1),
           "analyze_s": round(t_an, 1), "info": wr.info, "notes": wr.notes}
    for res in wr.results:
        g = res.gyro
        out[res.source.name] = {
            "timing": (round(res.timing.contrast, 3), res.timing.cycles, res.timing.ok),
            "tracks": res.tracks.n_tracks, "decoded_tracks": len(res.ids),
            "decoded_leds": len(res.decoded_leds()),
            "gyro": None if g is None else {"M": g.M.astype(int).tolist(), "inverse": g.inverse, "f": round(g.f, 1),
                                            "delta": round(g.delta, 3), "resid": round(g.residual_px, 2), "base": round(g.baseline_px, 2)},
            "gyro_cal": res.gyro_cal,
            "identity": identity_check(res, cap, cfg.rec_delay_s),
        }
    t2 = time.time()
    span = float(np.ptp(tree.positions[:, 1]))
    try:
        m = solve_walk(wr, n, span, list(tree.breaks), {})
    except Exception as e:  # noqa: BLE001
        out["solve_error"] = str(e)
        if verbose:
            import json
            print(json.dumps(out, indent=1, default=str))
        return out
    out["solve_s"] = round(time.time() - t2, 1)
    st = np.array([l["status"] for l in m["leds"]])
    ok = st == "measured"
    Pm = np.array([l["p"] for l, o in zip(m["leds"], ok) if o], float).reshape(-1, 3)
    Y = tree.positions[ok]
    ma, mb = Pm.mean(0), Y.mean(0)
    U, S, Vt = np.linalg.svd((Pm - ma).T @ (Y - mb))
    D = np.eye(3)
    D[2, 2] = np.sign(np.linalg.det(U @ Vt))
    R = (U @ D @ Vt).T
    s = (S * np.diag(D)).sum() / ((Pm - ma) ** 2).sum()
    err = np.linalg.norm(s * (Pm - ma) @ R.T + mb - Y, axis=1)
    out["map"] = {"counts": m["quality"]["counts"], "scale": round(float(s), 3), "median_cm": round(float(np.median(err)), 2),
                  "p95_cm": round(float(np.percentile(err, 95)), 2), "max_cm": round(float(err.max()), 2),
                  "over_link": int((err > tree.link).sum()), "focal_px": m["quality"]["focal_px"], "true_f": cap.scene.intr.f,
                  "long_links": [l["from"] for l in m["chain"]["long_links"]], "true_long": tree.long_links,
                  "up": m["quality"]["up_axis"], "warnings": m["quality"]["warnings"]}
    if verbose:
        import json
        print(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    run(a[0] if a else "ideal", int(a[1]) if len(a) > 1 else 150, float(a[2]) if len(a) > 2 else 40.0,
        seed=int(a[3]) if len(a) > 3 else 3)
