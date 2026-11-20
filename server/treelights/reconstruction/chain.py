"""Dynamic chain bounds (architecture §10) and chain selection (§9.6).

Bounds are learned per link, scale-free (world units), from "trusted links":
consecutive LEDs both measured confidently. Long connector links and breaks
are detected from the data; WLED output boundaries and user marks are breaks.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np

MIN_TRUSTED_LINKS = 30
FALLBACK_FRACTION = 0.12  # of tree height, when too few trusted links
MARGIN = 0.10
LONG_FACTOR = 3.0
GAP_WINDOW = 20


def slack(base: float, s1, s2):
    """Allowance for position uncertainty when checking a bound, capped so a badly
    constrained point can't hide a violation behind its own uncertainty."""
    return np.minimum(2 * np.sqrt(np.asarray(s1) ** 2 + np.asarray(s2) ** 2), 0.5 * base)


@dataclass
class Bounds:
    base: float  # learned normal link length bound (world units)
    base_raw: float
    bound: np.ndarray  # (n-1,) per-link bound; np.inf = break
    kind: list[str]  # per link: normal | long | long_predicted | break_data | break_output | break_user | user
    trusted_links: int
    fallback: bool = False
    period: int | None = None
    notes: list[str] = field(default_factory=list)

    def is_break(self, i: int) -> bool:
        return not np.isfinite(self.bound[i])


def learn_bounds(n: int, pos: dict[int, np.ndarray], sigma: dict[int, float], trusted: set[int], height: float,
                 output_breaks: list[int] = (), user: dict | None = None) -> Bounds:
    """pos/sigma: current best positions; trusted: LEDs measured confidently.
    user: {"breaks": [i...], "links": {i: bound_world_units}, "cap": world_units|None}"""
    user = user or {}
    known_break = set(output_breaks) | {int(i) for i in user.get("breaks", [])}
    d_tr = {i: float(np.linalg.norm(pos[i + 1] - pos[i])) for i in range(n - 1)
            if i in trusted and i + 1 in trusted and i not in known_break}
    notes = []
    fallback = len(d_tr) < MIN_TRUSTED_LINKS
    if fallback:
        # Too few trusted links: estimate from every consecutive pair of current positions
        # (less reliable, so a bigger margin). A fixed share of the tree height is the last
        # resort; it depends on how densely the tree is wired.
        d_all = np.array([float(np.linalg.norm(pos[i + 1] - pos[i])) for i in range(n - 1)
                          if i in pos and i + 1 in pos and i not in known_break])
        if len(d_all) >= 10:
            med = np.median(d_all)
            mad = 1.4826 * np.median(np.abs(d_all - med))
            base_raw = float(np.percentile(d_all[d_all <= med + 6 * max(mad, 0.02 * med)], 95)) * 1.25
        else:
            base_raw = FALLBACK_FRACTION * height
        notes.append(f"Only {len(d_tr)} trusted links; using a wider bound estimated from all positions.")
    else:
        d = np.array(list(d_tr.values()))
        med = np.median(d)
        mad = 1.4826 * np.median(np.abs(d - med))
        bulk = d[d <= med + 6 * max(mad, 0.02 * med)]
        base_raw = float(np.percentile(bulk, 95))
    base = base_raw * (1 + MARGIN)
    bound = np.full(n - 1, base)
    kind = ["normal"] * (n - 1)
    longs = []
    if not fallback:
        for i, d in d_tr.items():
            unc = float(slack(base, sigma.get(i, 0.0), sigma.get(i + 1, 0.0)))
            if d <= base + unc:
                continue
            if d <= LONG_FACTOR * base_raw:
                bound[i], kind[i] = d * (1 + MARGIN), "long"
                longs.append(i)
            else:
                bound[i], kind[i] = np.inf, "break_data"
    period = None
    if len(longs) >= 3:
        diffs = np.diff(sorted(longs))
        common = Counter(int(x) for x in diffs if x >= 5).most_common(1)
        if common and common[0][1] >= 2:
            period = common[0][0]
            phase = Counter(i % period for i in longs).most_common(1)[0][0]
            long_len = float(np.median([bound[i] for i in longs]))
            for i in range(phase, n - 1, period):
                if kind[i] == "normal" and i not in d_tr:
                    bound[i], kind[i] = long_len * 1.05, "long_predicted"
    for i in known_break:
        if 0 <= i < n - 1:
            bound[i] = np.inf
            kind[i] = "break_output" if i in output_breaks else "break_user"
    for i, b in (user.get("links") or {}).items():
        i = int(i)
        if 0 <= i < n - 1 and i not in known_break:
            bound[i], kind[i] = float(b), "user"
    cap = user.get("cap")
    if cap:
        fin = np.isfinite(bound)
        bound[fin] = np.minimum(bound[fin], float(cap))
    # Never below a trusted link's measured length.
    for i, d in d_tr.items():
        if np.isfinite(bound[i]) and bound[i] < d:
            bound[i] = d
    return Bounds(base, base_raw, bound, kind, len(d_tr), fallback, period, notes)


@dataclass
class Node:
    X: np.ndarray
    sigma: float
    cost: float


def select_chain(n: int, nodes: dict[int, list[Node]], bounds: Bounds, c_miss: float = 2.5,
                 c_break: float = 8.0, window: int = GAP_WINDOW) -> dict[int, int]:
    """Exact shortest path through the DAG of (LED, hypothesis) nodes.

    Normal edges (j -> i, i - j <= window) are allowed when the distance fits
    the summed bounds of links j..i-1 (plus both uncertainties); skipped LEDs
    cost c_miss each. Break edges allow any jump at cost c_break, except across
    a known break where the jump is free. Returns {led: hypothesis index}.
    """
    INF = np.inf
    fin_bounds = np.where(np.isfinite(bounds.bound), bounds.bound, 0.0)
    csum = np.concatenate([[0.0], np.cumsum(fin_bounds)])
    brk = np.concatenate([[0], np.cumsum(~np.isfinite(bounds.bound))])  # breaks among links [0, k)
    best: list[np.ndarray] = []
    back: list[list[tuple[int, int]]] = []
    X: list[np.ndarray] = []
    S: list[np.ndarray] = []
    # Running minima of (best - j * c_miss) for break edges: over all j (unknown break)
    # and over j up to the last known break (free jump).
    run_min, run_arg = INF, (-1, -1)
    hist_min: list[tuple[float, tuple[int, int]]] = []  # prefix min after processing LED j
    last_break_before = np.full(n, -1)
    lb = -1
    for i in range(n):
        last_break_before[i] = lb  # last j such that a known break lies between j and i
        if i < n - 1 and not np.isfinite(bounds.bound[i]):
            lb = i
    for i in range(n):
        hyps = nodes.get(i, [])
        Xi = np.array([h.X for h in hyps]).reshape(-1, 3)
        Si = np.array([h.sigma for h in hyps])
        Ci = np.array([h.cost for h in hyps])
        bi = np.full(len(hyps), i * c_miss)  # start here, skipping 0..i-1
        bk: list[tuple[int, int]] = [(-1, -1)] * len(hyps)
        if len(hyps):
            for j in range(max(0, i - window), i):
                if not len(best[j]):
                    continue
                skip = (i - j - 1) * c_miss
                if brk[i] - brk[j] > 0:
                    continue  # across a known break: handled by the free-jump term below
                allowed = (csum[i] - csum[j]) + slack(bounds.base, S[j][None, :], Si[:, None])
                dist = np.linalg.norm(Xi[:, None, :] - X[j][None, :, :], axis=2)
                cand = np.where(dist <= allowed, best[j][None, :] + skip, INF)
                k = np.argmin(cand, axis=1)
                v = cand[np.arange(len(hyps)), k]
                better = v < bi
                for h in np.nonzero(better)[0]:
                    bi[h] = v[h]
                    bk[h] = (j, int(k[h]))
            # Unknown break: any earlier node, at c_break.
            if run_min < INF:
                v = run_min + (i - 1) * c_miss + c_break
                for h in range(len(hyps)):
                    if v < bi[h]:
                        bi[h], bk[h] = v, run_arg
            # Known break: free jump from anything before the break.
            j_lim = last_break_before[i]
            if j_lim >= 0 and hist_min[j_lim][0] < INF:
                v = hist_min[j_lim][0] + (i - 1) * c_miss
                for h in range(len(hyps)):
                    if v < bi[h]:
                        bi[h], bk[h] = v, hist_min[j_lim][1]
            bi = bi + Ci
        best.append(bi)
        back.append(bk)
        X.append(Xi)
        S.append(Si)
        if len(bi):
            h = int(np.argmin(bi))
            if bi[h] - i * c_miss < run_min:
                run_min, run_arg = float(bi[h] - i * c_miss), (i, h)
        hist_min.append((run_min, run_arg))
    # End: skip the remaining LEDs.
    end_cost, end = n * c_miss, (-1, -1)
    for i in range(n):
        if len(best[i]):
            h = int(np.argmin(best[i]))
            v = best[i][h] + (n - 1 - i) * c_miss
            if v < end_cost:
                end_cost, end = v, (i, h)
    out: dict[int, int] = {}
    cur = end
    while cur[0] >= 0:
        out[cur[0]] = cur[1]
        cur = back[cur[0]][cur[1]]
    return out


def link_violations(pos: dict[int, np.ndarray], sigma: dict[int, float], bounds: Bounds) -> list[dict]:
    out = []
    for i in range(len(bounds.bound)):
        if i in pos and i + 1 in pos and np.isfinite(bounds.bound[i]):
            d = float(np.linalg.norm(pos[i + 1] - pos[i]))
            allowed = bounds.bound[i] + float(slack(bounds.base, sigma.get(i, 0.0), sigma.get(i + 1, 0.0)))
            if d > allowed * 1.001:
                out.append({"link": i, "length": d, "allowed": float(allowed)})
    return out
