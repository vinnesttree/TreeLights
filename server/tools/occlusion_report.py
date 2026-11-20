"""Measure single-LED detection against ground truth with branch occlusion,
scattered light and needle glints (python tools/occlusion_report.py)."""

import sys

import numpy as np

from treelights.detection.detector import build_reference, decode_image, detect_single
from treelights.sim.scene import Faults, Scene, encode_jpeg
from treelights.sim.tree import make_tree


def run(seed: int = 1, foliage: float = 1.0, glint: float = 0.15, verbose: bool = True) -> dict:
    tree = make_tree(seed=seed)
    sc = Scene(tree, seed=seed, faults=Faults(foliage=foliage, glint_prob=glint))
    v = sc.add_view(0)
    grab = lambda leds: decode_image(encode_jpeg(sc.render(v, leds)))  # noqa: E731
    ref = build_reference([grab(np.zeros((600, 3), np.uint8)) for _ in range(5)])
    uv, _ = sc.project_view(v)
    basis = np.array([0.10, 1.0, 0.15])
    stats = {"visible": [], "partial": [], "hidden": []}
    for i in range(0, 600, 2):
        leds = np.zeros((600, 3), np.uint8)
        leds[i] = (0, 48, 0)
        det = detect_single(grab(leds), ref, expected_color=basis)
        cl = "visible" if v.vis[i] > 0.5 else "partial" if v.vis[i] > 0 else "hidden"
        best = det.candidates[0] if det.candidates else None
        err = None if best is None else float(np.hypot(best.x - uv[i, 0], best.y - uv[i, 1]))
        stats[cl].append({"i": i, "valid": det.valid, "reason": det.reason, "err": err,
                          "kind": getattr(best, "kind", "point") if best else None, "n": len(det.candidates),
                          "any_near": any(np.hypot(c.x - uv[i, 0], c.y - uv[i, 1]) < 2.5 for c in det.candidates)})
    out = {}
    for cl, rows in stats.items():
        n = len(rows)
        if not n:
            continue
        invalid = sum(not r["valid"] for r in rows)
        found = [r for r in rows if r["err"] is not None]
        good = sum(r["err"] < 2.5 for r in found)
        far_point = sum(r["err"] >= 2.5 and r["kind"] == "point" for r in found)
        out[cl] = dict(n=n, invalid=invalid, with_candidate=len(found), best_within_2_5px=good,
                       best_far_and_point_like=far_point, true_in_candidates=sum(r["any_near"] for r in rows))
        if verbose:
            print(cl, out[cl])
    return out


if __name__ == "__main__":
    run(foliage=float(sys.argv[1]) if len(sys.argv) > 1 else 1.0)
