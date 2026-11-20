"""Synthetic observations straight from projections (fast reconstruction tests).

Produces the same structure as Scan.observations() plus segment metadata,
without rendering frames.
"""

from __future__ import annotations

import numpy as np

from ..reconstruction.camera import project
from .scene import Scene


def synthetic_observations(scene: Scene, noise_px: float = 0.3, mirror_prob: float = 0.0, false_prob: float = 0.0,
                           swap_prob: float = 0.0, seed: int = 0, glint_prob: float = 0.0,
                           scatter_prob: float = 0.0) -> tuple[dict, dict]:
    """Returns (obs {seg: {led: [cands]}}, segments {seg: meta}).

    mirror_prob: chance a visible LED also shows a geometrically consistent
      mirror reflection (a second candidate that triangulates behind a wall).
    false_prob: chance of a random extra candidate (glare) near the LED.
    swap_prob: chance the recorded best candidate is actually another LED
      (an identity error that slipped through capture checks).
    glint_prob: chance of a needle glint: a sharp point 4-22 px from the LED, different
      in every view; for a mostly hidden LED (vis < 0.4) it outranks the LED.
    scatter_prob: chance that a hidden LED is seen only as light scattered by the
      branches around it: a diffuse candidate at the glow's center (biased).
    """
    rng = np.random.default_rng(seed)
    obs: dict[str, dict[int, list[dict]]] = {}
    segs: dict[str, dict] = {}
    mirror_n = np.array([-1.0, 0.0, 0.0])
    mirror_p0 = np.array([170.0, 0.0, 40.0])
    for k, v in enumerate(scene.views):
        sid = f"v{k + 1}a"
        R, t = scene.camera_pose(v)
        uv, z = project(scene.tree.positions, R, t, scene.intr)
        uv = uv + v.glow_offset
        # Mirror images: reflect the LED's room position, project with the room camera.
        Xw = scene.tree.positions @ v.R_tree.T + v.s_tree
        d = (Xw - mirror_p0) @ mirror_n
        Xm = Xw - 2 * d[:, None] * mirror_n
        uvm, zm = project(Xm, scene.bump_R @ scene.R_cam, scene.bump_R @ scene.t_cam, scene.intr)
        o: dict[int, list[dict]] = {}
        vis_idx = np.nonzero(v.vis > 0)[0]
        for i in range(scene.tree.n):
            if v.vis[i] <= 0 or z[i] <= 0:
                o[i] = []
                if z[i] > 0 and scatter_prob and rng.random() < scatter_prob:
                    q = uv[i] + rng.normal(0, 6, 2)
                    o[i] = [{"x": float(q[0]), "y": float(q[1]), "score": 1.0, "snr": 20.0, "kind": "diffuse", "sigma_px": 6.0}]
                if z[i] > 0 and glint_prob and rng.random() < glint_prob:
                    q = uv[i] + rng.choice([-1, 1], 2) * rng.uniform(4, 22, 2)
                    o[i] = [{"x": float(q[0]), "y": float(q[1]), "score": 1.0, "snr": 30.0, "kind": "point", "sigma_px": 0.5}] + o[i]
                continue
            p = uv[i] + rng.normal(0, noise_px / max(v.vis[i], 0.3), 2)
            if swap_prob and rng.random() < swap_prob:
                j = int(rng.choice(vis_idx))
                p = uv[j] + rng.normal(0, noise_px, 2)
            cands = [{"x": float(p[0]), "y": float(p[1]), "score": float(0.5 + 0.5 * v.vis[i]), "snr": 40.0 * v.vis[i]}]
            if mirror_prob and rng.random() < mirror_prob and zm[i] > 0 and d[i] < 0:
                in_img = 0 <= uvm[i, 0] < scene.width and 0 <= uvm[i, 1] < scene.height
                if in_img:
                    cands.append({"x": float(uvm[i, 0]), "y": float(uvm[i, 1]), "score": 0.3, "snr": 12.0})
            if glint_prob and rng.random() < glint_prob:
                q = uv[i] + rng.choice([-1, 1], 2) * rng.uniform(4, 22, 2)
                glint = {"x": float(q[0]), "y": float(q[1]), "score": 0.6, "snr": 30.0, "kind": "point", "sigma_px": 0.5}
                if v.vis[i] < 0.4:
                    glint["score"], cands[0]["score"] = 1.0, 0.5  # outshines the mostly hidden LED
                cands.append(glint)
            if false_prob and rng.random() < false_prob:
                q = p + rng.uniform(-30, 30, 2)
                cands.append({"x": float(q[0]), "y": float(q[1]), "score": 0.25, "snr": 10.0})
            cands.sort(key=lambda c: -c["score"])
            o[i] = cands
        obs[sid] = o
        segs[sid] = {"view": f"v{k + 1}", "width": scene.width, "height": scene.height,
                     "up_cam": scene.up_in_camera().tolist()}
    return obs, segs
