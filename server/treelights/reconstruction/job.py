"""Solve job entry point, run in a worker process (architecture §6.2)."""

from __future__ import annotations

from pathlib import Path

from ..capture.store import Scan
from ..progress import Progress, Report
from .solve import SolveInput, solve


# Where the solve's steps sit within it (rough timings).
SOLVE_STEPS = {"Solving camera positions": 0.0, "Building LED hypotheses": 0.55, "Selecting the chain": 0.7,
               "Filling in LEDs": 0.9}


def solve_report(report: Report):
    """solve()'s progress messages -> a reporter's fraction and detail."""

    def progress(msg: str) -> None:
        frac = next((v for k, v in SOLVE_STEPS.items() if msg.startswith(k)), None)
        if frac is not None:
            report(frac, msg.rstrip("."))

    return progress


def run_solve(scan_root: str, tree_height_cm: float | None, prev_map: dict | None, wled: dict | None,
              f0_prior: float | None = None, progress_path: str | None = None,
              report: Report | None = None) -> dict:
    scan = Scan(Path(scan_root))
    if report is None:
        report = Progress(progress_path).part(0.0, 1.0, "Building the 3D map")
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
        controller_breaks=list(scan.meta.get("controller_breaks", [])),
    )
    m = solve(inp, progress=solve_report(report))
    if walk["segments"]:
        m["quality"]["walk"] = walk["info"]
        m["quality"]["warnings"] = walk["notes"] + m["quality"]["warnings"]
    return m
