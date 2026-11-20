"""Closed loop with branch occlusion, scattered light and glints: capture (rendered
frames, real session, real DDP) then solve; compare with ground truth."""

import asyncio
import sys
import tempfile
from pathlib import Path

import numpy as np

from treelights.reconstruction.job import run_solve
from treelights.sim.harness import make_sim
from treelights.sim.scene import Faults


def align_err(P, Y):
    ma, mb = P.mean(0), Y.mean(0)
    U, S, Vt = np.linalg.svd((P - ma).T @ (Y - mb))
    D = np.eye(3)
    D[2, 2] = np.sign(np.linalg.det(U @ Vt))
    R = (U @ D @ Vt).T
    s = (S * np.diag(D)).sum() / ((P - ma) ** 2).sum()
    return lambda X: s * (X - ma) @ R.T + mb


async def run(foliage: float, glint: float, n: int = 150, views: int = 6, seed: int = 21) -> dict:
    d = Path(tempfile.mkdtemp())
    sim = await make_sim(d, n=n, seed=seed, faults=Faults(foliage=foliage, glint_prob=glint))
    try:
        for k in range(views):
            await sim.run_view(360.0 * k / views, "hybrid")
    finally:
        await sim.close()
    span = float(np.ptp(sim.tree.positions[:, 1]))
    m = run_solve(str(sim.scan.root), span, None, None)
    st = np.array([l["status"] for l in m["leds"]])
    P = np.array([l["p"] if l["p"] else [np.nan] * 3 for l in m["leds"]])
    ok = st == "measured"
    tf = align_err(P[ok], sim.tree.positions[ok])
    out = {"counts": m["quality"]["counts"], "frames": sim.phone.frames_sent,
           "visibility_side": m["quality"]["visibility_side"], "warnings": m["quality"]["warnings"],
           "long_links": [l["from"] for l in m["chain"]["long_links"]], "true_long": sim.tree.long_links}
    for kind in ("measured", "single_view", "estimated"):
        k = st == kind
        if k.any():
            e = np.linalg.norm(tf(P[k]) - sim.tree.positions[k], axis=1)
            out[kind] = {"median_cm": round(float(np.median(e)), 2), "p95_cm": round(float(np.percentile(e, 95)), 2),
                         "max_cm": round(float(e.max()), 2), "over_link": int((e > sim.tree.link).sum())}
    out["span_cm"] = round(span, 1)
    return out


if __name__ == "__main__":
    fol = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    print(asyncio.run(run(fol, 0.15 if fol else 0.0)))
