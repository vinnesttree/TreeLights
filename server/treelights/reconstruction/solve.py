"""The full solve: observations -> versioned map (architecture §9, §10, §14)."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

from .. import __version__
from . import chain, fill as fillmod, hypotheses as hyp, orient
from .sfm import SegmentObs, solve_cameras, unambiguous

log = logging.getLogger(__name__)

MIN_SEGMENT_OBS = 8
MIN_RAY_SNR = 15.0


class ReconstructionError(Exception):
    pass


@dataclass
class SolveInput:
    n: int
    obs: dict[str, dict[int, list[dict]]]
    segments: dict[str, dict]  # seg -> {width, height, up_cam, view}
    tree_height_cm: float | None
    output_breaks: list[int]
    overrides: dict
    prev_map: dict | None = None
    f0_prior: float | None = None
    scan_id: str = ""
    wled: dict | None = None
    # Breaks between WLED controllers (also in output_breaks); only labelled differently.
    controller_breaks: list[int] = field(default_factory=list)


COINCIDENT_FRAC = 0.2  # of the typical LED spacing: closer than this, two LEDs can't both be there


def _drop_coincident(hyps: dict[int, list]) -> list[int]:
    """Two different LEDs can't be at the same place. When the best positions of two
    LEDs coincide, one of them is the other LED seen under the wrong identity: walking
    around the tree, two LEDs that keep passing each other in the picture merge into
    one blob for a while, and in some pattern cycles the merged blob carries the other
    LED's code. Such a position is consistent across views, so nothing else rejects it,
    and as a "trusted" LED it would even teach the chain a fake connector link. The one
    whose position fits its own chain neighbours worse loses that hypothesis (ties: the
    one with less support). Returns the LEDs that lost one."""
    top = {i: lst[0] for i, lst in hyps.items() if lst}
    if len(top) < 4:
        return []
    idx = sorted(top)
    steps = [float(np.linalg.norm(top[i + 1].X - top[i].X)) for i in idx if i + 1 in top]
    if len(steps) < 3:
        return []
    L = float(np.median(steps))
    if L <= 0:
        return []
    X = np.array([top[i].X for i in idx])
    pairs = cKDTree(X).query_pairs(COINCIDENT_FRAC * L)

    def misfit(i: int) -> float:
        bad = 0.0
        for k in (i - 1, i + 1):
            if k in top:
                bad += max(0.0, float(np.linalg.norm(top[i].X - top[k].X)) / L - 1.5)
        return bad

    dropped = []
    for a, b in sorted(pairs):
        i, j = idx[a], idx[b]
        if not hyps[i] or not hyps[j] or hyps[i][0] is not top[i] or hyps[j][0] is not top[j]:
            continue  # already resolved through another pair
        mi, mj = misfit(i), misfit(j)
        if abs(mi - mj) > 1e-9:
            loser = i if mi > mj else j
        else:
            loser = i if top[i].n < top[j].n else j
        hyps[loser] = hyps[loser][1:]
        dropped.append(loser)
    return dropped


def _apply_overrides(obs: dict, overrides: dict) -> dict:
    bad = {int(i) for i in overrides.get("bad", [])}
    if not bad:
        return obs
    return {s: {l: c for l, c in o.items() if l not in bad} for s, o in obs.items()}


def solve(inp: SolveInput, progress=lambda msg: None) -> dict:
    n = inp.n
    obs = _apply_overrides(inp.obs, inp.overrides)
    segs: dict[str, SegmentObs] = {}
    for sid, o in obs.items():
        meta = inp.segments.get(sid, {})
        leds = {i: p for i, c in o.items() if (p := unambiguous(c)) is not None}
        if len(leds) >= MIN_SEGMENT_OBS:
            segs[sid] = SegmentObs(sid, int(meta.get("width", 1280)), int(meta.get("height", 720)), leds,
                                   np.array(meta["up_cam"]) if meta.get("up_cam") else None)
    if len(segs) < 2:
        raise ReconstructionError("At least 2 segments with detections are needed.")
    progress("Solving camera positions...")
    cams = solve_cameras(segs, inp.f0_prior)
    if cams is None or len(cams.poses) < 2:
        raise ReconstructionError("Couldn't work out the camera positions. More views with overlap are needed.")

    # ---- orientation (§9.4) -------------------------------------------------
    pts = np.array(list(cams.points.values()))
    cand = orient.up_candidates(cams, {s: inp.segments.get(s, {}).get("up_cam") for s in cams.poses}, pts)
    up, up_report = orient.up_consensus(cand)
    R_up = orient.rotation_to_y(up)
    centers_y = np.array([R_up @ cams.center(s) for s in cams.poses])
    walk = any(str(inp.segments.get(s, {}).get("device", "")).startswith("walk") for s in cams.poses)
    axis_xz, axis_src = orient.tree_axis_xz(centers_y, pts @ R_up.T, walk=walk)
    y0 = float((pts @ R_up.T)[:, 1].min())
    origin = np.array([axis_xz[0], y0, axis_xz[1]])

    def W(X: np.ndarray) -> np.ndarray:
        return np.atleast_2d(X) @ R_up.T - origin

    views: dict[str, fillmod.View] = {}
    for s in cams.poses:
        R, t = cams.poses[s]
        Rw = R @ R_up.T  # x_cam = R X = R R_up^T (Xw + origin)
        tw = t + Rw @ origin
        w, h = cams.sizes[s]
        views[s] = fillmod.View(s, Rw, tw, cams.intrinsics(s), w, h)

    # ---- hypotheses (§9.5) ----------------------------------------------------
    progress("Building LED hypotheses...")
    hyps = hyp.build_hypotheses(obs, cams, n)
    for lst in hyps.values():
        for h in lst:
            h.X = W(h.X)[0]
    reliable = np.array([lst[0].X for lst in hyps.values() if lst[0].n >= 3 and lst[0].err_px < 1.5]).reshape(-1, 3)
    if len(reliable) < 0.3 * len(hyps):
        # Too few multi-view points to outline the tree (e.g. few views): use every LED's
        # best hypothesis; the per-height 90th percentile is robust to a few outliers.
        reliable = np.array([lst[0].X for lst in hyps.values()]).reshape(-1, 3)
    if len(reliable) < 4:
        raise ReconstructionError("Too few LEDs could be triangulated.")
    prior = hyp.volume_prior(reliable)
    for lst in hyps.values():
        for h in lst:
            h.cost = hyp.hypothesis_cost(h, prior)
    H_world = float(np.ptp(reliable[:, 1])) or 1.0
    # Map units per world unit (preliminary): cm with a tree height, else tree heights.
    # Link bounds and the cap you set on the Map page are in map units.
    s0 = (inp.tree_height_cm / H_world) if inp.tree_height_cm else 1.0 / H_world
    user = {"breaks": inp.overrides.get("breaks", [])}
    user["links"] = {int(k): float(v) / s0 for k, v in (inp.overrides.get("links") or {}).items() if float(v) > 0}
    if inp.overrides.get("cap"):
        user["cap"] = float(inp.overrides["cap"]) / s0

    # ---- chain selection with dynamic bounds (§9.6, §10) -------------------------
    def strong(h: hyp.Hypothesis, tier: int) -> bool:
        # Counted on point-source support: light scattered by branches (diffuse) may also
        # support the hypothesis, but doesn't make it trusted. Errors are relative to each
        # observation's own uncertainty.
        n_point = int(round(h.n * (1.0 - h.diffuse)))
        if tier == 1:
            return n_point >= 3 and h.nerr < 1.5
        # Tier 2, used only when tier 1 gives too few links (e.g. few views):
        # two point views at a wide angle with a tight fit.
        return n_point >= 2 and h.nerr < 1.0 and h.angle_deg >= 20

    def dominant(lst: list[hyp.Hypothesis], tier: int) -> bool:
        # Clearly better than any alternative: alternatives are often weak 2-view pairings
        # of glints or reflections, so more supporting views is decisive as well as cost.
        h0 = lst[0]
        if not strong(h0, tier):
            return False
        return len(lst) == 1 or lst[1].cost > h0.cost + 1.0 or h0.n - lst[1].n >= 2

    def links_in(t: set[int]) -> int:
        return sum(1 for i in t if i + 1 in t)

    dropped = _drop_coincident(hyps)
    hyps = {i: lst for i, lst in hyps.items() if lst}
    sel_pos = {i: lst[0].X for i, lst in hyps.items()}
    sigma = {i: lst[0].sigma for i, lst in hyps.items()}
    tier = 1
    trusted = {i for i, lst in hyps.items() if dominant(lst, 1)}
    if links_in(trusted) < chain.MIN_TRUSTED_LINKS:
        tier = 2
        trusted = {i for i, lst in hyps.items() if dominant(lst, 2)}
    prev_kinds = None
    bounds = None
    selected: dict[int, int] = {}
    support: dict[int, list] = {}
    # Strong, dominant hypotheses stay evidence for learning the bounds even if a round of
    # chain selection drops them: a connector that wasn't learned yet makes the chain drop
    # one of its LEDs, and without this the connector could never be learned afterwards.
    strong_best = {i: lst[0] for i, lst in hyps.items() if dominant(lst, tier)}
    for it in range(3):
        progress(f"Selecting the chain (round {it + 1})...")
        learn_pos = {i: h.X for i, h in strong_best.items()}
        learn_pos.update(sel_pos)
        learn_sigma = {i: h.sigma for i, h in strong_best.items()}
        learn_sigma.update(sigma)
        bounds = chain.learn_bounds(n, learn_pos, learn_sigma, trusted | set(strong_best), H_world, inp.output_breaks, user,
                                    controller_breaks=inp.controller_breaks)
        nodes = {i: [chain.Node(h.X, h.sigma, h.cost) for h in lst] for i, lst in hyps.items()}
        selected = chain.select_chain(n, nodes, bounds)
        sel_pos = {i: hyps[i][k].X for i, k in selected.items()}
        sigma = {i: hyps[i][k].sigma for i, k in selected.items()}
        support = {i: list(zip(hyps[i][k].segs, hyps[i][k].uv)) for i, k in selected.items()}
        sel_pos = fillmod.refine_points(sel_pos, support, views, bounds, sigma)
        trusted = {i for i, k in selected.items() if strong(hyps[i][k], tier)}
        kinds = tuple(bounds.kind)
        if kinds == prev_kinds:
            break
        prev_kinds = kinds
    assert bounds is not None

    # ---- surface and filling (§9.8, §9.9) ------------------------------------------
    progress("Filling in LEDs that weren't measured...")
    P_meas = np.array(list(sel_pos.values()))
    surface = hyp.radius_profile(P_meas)
    ray_cands: dict[int, list] = {}
    for s in views:
        for led, cands in obs.get(s, {}).items():
            if led not in sel_pos and cands:
                # A single view can't be cross-checked, so only clearly strong blobs place an LED.
                strong = [c for c in cands if c.get("snr", 0) >= MIN_RAY_SNR]
                # Prefer point sources; light scattered by branches only if nothing sharper was seen.
                points = [c for c in strong if c.get("kind", "point") == "point"]
                ray_cands.setdefault(led, []).extend((s, np.array([c["x"], c["y"]])) for c in (points or strong))
    fp, fstatus, fsig = fillmod.fill(n, sel_pos, bounds, views, ray_cands, surface, H_world)

    # Visibility-side check on measured LEDs (§9.3).
    seen = {s: [sel_pos[i] for i, k in selected.items() if s in hyps[i][k].segs] for s in views}
    side = orient.visibility_side_fraction({s: v.center for s, v in views.items()}, seen, np.zeros(2))

    # ---- scale, front, continuity (§9.10) --------------------------------------------
    ys = P_meas[:, 1]
    span = float(ys.max() - ys.min()) or 1.0
    if inp.tree_height_cm:
        scale, units = inp.tree_height_cm / span, "cm"
    else:
        scale, units = 1.0 / span, "tree_heights"
    ymin = float(ys.min())
    first = sorted(views)[0]
    c0 = views[first].center
    ang = np.arctan2(c0[0], c0[2])  # rotate about y so the first view's camera is at +z
    Ry = np.array([[np.cos(-ang), 0, np.sin(-ang)], [0, 1, 0], [-np.sin(-ang), 0, np.cos(-ang)]])
    T = np.array([0.0, -ymin, 0.0])

    def to_map(X: np.ndarray) -> np.ndarray:
        return scale * ((np.atleast_2d(X) + T) @ Ry.T)

    leds_out = []
    positions = {}
    statuses = {}
    sigmas = {}
    for i in range(n):
        if i in sel_pos:
            k = selected[i]
            positions[i], statuses[i], sigmas[i] = to_map(sel_pos[i])[0], "measured", hyps[i][k].sigma * scale
        elif i in fp:
            positions[i], statuses[i], sigmas[i] = to_map(fp[i])[0], fstatus[i], fsig[i] * scale
        else:
            positions[i], statuses[i], sigmas[i] = None, "unknown", None
    for i, p in (inp.overrides.get("positions") or {}).items():
        i = int(i)
        if 0 <= i < n:
            positions[i], statuses[i], sigmas[i] = np.array(p, float), "manual", 0.0
    align = None
    if inp.prev_map:
        positions, align = _align_to_previous(positions, statuses, inp.prev_map)
    for i in range(n):
        k = selected.get(i)
        leds_out.append({
            "i": i,
            "p": None if positions[i] is None else [round(float(v), 3) for v in positions[i]],
            "status": statuses[i],
            "segments": hyps[i][k].n if k is not None else (1 if statuses[i] == "single_view" else 0),
            "err_px": round(hyps[i][k].err_px, 3) if k is not None else None,
            "sigma": None if sigmas[i] is None else round(float(sigmas[i]), 3),
        })

    # Cameras in map coordinates: X_map = scale * Ry (W(X) + T).
    cams_out = []
    for s, v in views.items():
        Rm = v.R @ Ry.T
        tm = scale * v.t - Rm @ (scale * (Ry @ T))
        if align:
            Ra, ta = np.array(align["R"]), np.array(align["t"])
            Rm, tm = Rm @ Ra.T, tm - Rm @ Ra.T @ ta
        cams_out.append({"segment": s, "size": [v.width, v.height], "f": round(v.K.f, 2), "k": [round(v.K.k1, 5), round(v.K.k2, 5)],
                         "R": np.round(Rm, 6).tolist(), "t": np.round(tm, 3).tolist()})

    counts = {k: sum(1 for x in statuses.values() if x == k) for k in ("measured", "single_view", "estimated", "unknown", "manual")}
    meas_pos = {i: positions[i] for i in range(n) if statuses[i] == "measured"}
    viol = chain.link_violations({i: p / scale for i, p in meas_pos.items() if p is not None},
                                 {i: (sigmas[i] or 0) / scale for i in meas_pos}, bounds)
    errs = [hyps[i][k].err_px for i, k in selected.items()]
    warnings = list(bounds.notes)
    if counts["measured"] < 0.7 * n:
        warnings.append(f"Only {counts['measured']} of {n} LEDs were measured from 2+ views.")
    if errs and float(np.median(errs)) > 2.0:
        warnings.append("High reprojection error; the picture may have moved during capture.")
    if up_report["disagree"]:
        warnings.append("Up-direction estimates disagree: " + ", ".join(up_report["disagree"]) + ". Check the map orientation.")
    # A depth-reversed map scores about 1 - (the true value). LEDs glimpsed through the
    # branches from the far side lower the true value, so the test is "more plausible than
    # the reversed map", i.e. clearly above one half.
    if side < 0.55:
        warnings.append("Visibility check failed: the map may be inside-out.")
    if dropped:
        warnings.append(f"LED(s) {', '.join(map(str, sorted(dropped)))} were seen at the same spot as another LED "
                        "(two LEDs overlapping in the picture); that position was set aside.")
    unknown_runs = _runs([i for i in range(n) if statuses[i] == "unknown"])
    for a, b in unknown_runs:
        if b == n - 1:
            warnings.append(f"LEDs {a}-{b} were never seen. If the string is shorter, lower WLED's LED count.")
        else:
            warnings.append(f"LEDs {a}-{b} have no position.")
    breaks = [{"from": i, "reason": bounds.kind[i]} for i in range(n - 1) if not np.isfinite(bounds.bound[i])]
    hard_fail = len(cams.poses) < 3
    return {
        "format": 2,
        "id": dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S"),
        "scan": inp.scan_id,
        "software": __version__,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "units": units,
        "tree_height": inp.tree_height_cm,
        "frame": {"up": "+y", "front": "+z", "origin": "axis_at_lowest_led"},
        "wled": inp.wled or {},
        "chain": {
            "base_link": round(bounds.base * scale, 3),
            "base_link_raw": round(bounds.base_raw * scale, 3),
            "trusted_links": bounds.trusted_links,
            "trusted_tier": tier,
            "fallback": bounds.fallback,
            "period": bounds.period,
            "long_links": [{"from": i, "bound": round(float(bounds.bound[i] * scale), 3), "kind": bounds.kind[i]}
                           for i in range(n - 1) if bounds.kind[i] in ("long", "long_predicted", "user")],
            "breaks": breaks,
            # Every link's bound (map units; null = break), for review and editing.
            "link_bounds": [round(float(b * scale), 3) if np.isfinite(b) else None for b in bounds.bound],
            "cap": inp.overrides.get("cap"),
        },
        "surface": {"h": np.round((surface[0] - ymin) * scale, 3).tolist(), "r": np.round(surface[1] * scale, 3).tolist()},
        "cameras": cams_out,
        "leds": leds_out,
        "quality": {
            "registered_segments": len(cams.poses),
            "segments_total": len(obs),
            "focal_px": round(float(np.median([v.K.f for v in views.values()])), 1),
            "camera_rms_px": round(cams.rms_px, 3),
            "median_err_px": round(float(np.median(errs)), 3) if errs else None,
            "counts": counts,
            "measured": round(counts["measured"] / n, 4),
            "up_axis": up_report,
            "tree_axis": axis_src,
            "visibility_side": round(side, 3),
            "violations": len(viol),
            "warnings": warnings,
            "activation_blocked": "Fewer than 3 segments were registered." if hard_fail else None,
        },
        "alignment": align,
    }


def _runs(idx: list[int]) -> list[tuple[int, int]]:
    out = []
    for i in idx:
        if out and out[-1][1] == i - 1:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


def _align_to_previous(positions: dict, statuses: dict, prev: dict) -> tuple[dict, dict | None]:
    """Rotate about the vertical axis and shift to line up with the previous map
    using LEDs measured well in both (§9.10)."""
    prev_p = {l["i"]: np.array(l["p"]) for l in prev.get("leds", []) if l.get("status") == "measured" and l.get("p")}
    common = [i for i, s in statuses.items() if s == "measured" and i in prev_p and positions[i] is not None]
    if len(common) < 50:
        return positions, None
    A = np.array([positions[i] for i in common])
    B = np.array([prev_p[i] for i in common])
    ma, mb = A.mean(0), B.mean(0)
    a2, b2 = (A - ma)[:, [0, 2]], (B - mb)[:, [0, 2]]
    th = np.arctan2((a2[:, 0] * b2[:, 1] - a2[:, 1] * b2[:, 0]).sum(), (a2 * b2).sum())
    c, s = np.cos(th), np.sin(th)
    R = np.array([[c, 0, -s], [0, 1, 0], [s, 0, c]])
    t = mb - R @ ma
    resid = np.linalg.norm(A @ R.T + t - B, axis=1)
    out = {i: (None if p is None else R @ p + t) for i, p in positions.items()}
    return out, {"R": R.tolist(), "t": t.tolist(), "common": len(common), "median_residual": float(np.median(resid))}
