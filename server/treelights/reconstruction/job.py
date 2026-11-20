"""Solve job entry point, run in a worker process (architecture §6.2)."""

from __future__ import annotations

from pathlib import Path

from ..capture.store import Scan
from .solve import SolveInput, solve


def run_solve(scan_root: str, tree_height_cm: float | None, prev_map: dict | None, wled: dict | None,
              f0_prior: float | None = None) -> dict:
    scan = Scan(Path(scan_root))
    obs = scan.observations()
    segs = {s: m for s, m in scan.meta["segments"].items() if s in obs}
    walk = scan.walk_observations()
    obs.update(walk["obs"])
    segs.update(walk["segments"])
    if f0_prior is None:
        f0_prior = walk["focal_prior"]
    inp = SolveInput(
        n=scan.meta["led_count"], obs=obs, segments=segs, tree_height_cm=tree_height_cm,
        output_breaks=list(scan.meta.get("output_breaks", [])), overrides=scan.overrides(),
        prev_map=prev_map, f0_prior=f0_prior, scan_id=scan.id, wled=wled,
    )
    m = solve(inp)
    if walk["segments"]:
        m["quality"]["walk"] = walk["info"]
        m["quality"]["warnings"] = walk["notes"] + m["quality"]["warnings"]
    return m
