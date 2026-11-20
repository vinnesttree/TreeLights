"""Per-LED 3D hypotheses and the tree-volume prior (architecture §9.5)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .camera import Intrinsics, undistort_normalized
from .geometry import refine_point, triangulate_rays
from .sfm import Cameras

INLIER_PX = 3.0
MAX_HYPS = 5
MAX_PAIRS = 40


@dataclass
class Hypothesis:
    X: np.ndarray
    segs: list[str]
    uv: list[np.ndarray]
    err_px: float
    angle_deg: float
    sigma: float  # position uncertainty (world units, 1 px noise)
    score: float  # mean candidate score
    cost: float = 0.0

    @property
    def n(self) -> int:
        return len(self.segs)


def _project(X: np.ndarray, R: np.ndarray, t: np.ndarray, K: Intrinsics) -> tuple[np.ndarray, float]:
    Xc = R @ X + t
    if Xc[2] <= 1e-9:
        return np.array([np.inf, np.inf]), Xc[2]
    p = Xc[:2] / Xc[2]
    r2 = float(p @ p)
    p = p * (1 + K.k1 * r2 + K.k2 * r2 * r2) * K.f + np.array([K.cx, K.cy])
    return p, Xc[2]


def build_hypotheses(obs: dict[str, dict[int, list[dict]]], cams: Cameras, n_leds: int) -> dict[int, list[Hypothesis]]:
    Ks = {s: cams.intrinsics(s) for s in cams.poses}
    rays: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    out: dict[int, list[Hypothesis]] = {}
    scene = np.ptp(np.array([cams.center(s) for s in cams.poses]), axis=0).max() or 1.0
    merge = 0.004 * scene
    for led in range(n_leds):
        entries = []  # (seg, uv, score)
        for s in cams.poses:
            for c in obs.get(s, {}).get(led, []) or []:
                entries.append((s, np.array([c["x"], c["y"]], float), float(c.get("score", 0.5))))
        if len({e[0] for e in entries}) < 2:
            continue
        entries.sort(key=lambda e: -e[2])
        by_seg: dict[str, list[tuple[np.ndarray, float]]] = {}
        for s, uv, sc in entries:
            by_seg.setdefault(s, []).append((uv, sc))
        pairs = [(a, b) for ai, a in enumerate(entries) for b in entries[ai + 1 :] if a[0] != b[0]][:MAX_PAIRS]
        hyps: list[Hypothesis] = []
        for a, b in pairs:
            Cs, ds = [], []
            for s, uv, _ in (a, b):
                R, t = cams.poses[s]
                nrm = undistort_normalized(uv[None], Ks[s])[0]
                d = R.T @ np.array([nrm[0], nrm[1], 1.0])
                Cs.append(-R.T @ t)
                ds.append(d / np.linalg.norm(d))
            cosang = float(np.clip(ds[0] @ ds[1], -1, 1))
            if np.degrees(np.arccos(cosang)) < 1.0:
                continue
            X = triangulate_rays(np.array(Cs), np.array(ds))
            inl = _inliers(X, by_seg, cams, Ks)
            if len(inl) < 2:
                continue
            segs = [s for s, _, _ in inl]
            uvs = [uv for _, uv, _ in inl]
            X, errs, cov = refine_point(X, np.array(uvs), [cams.poses[s][0] for s in segs], [cams.poses[s][1] for s in segs], [Ks[s] for s in segs])
            inl = _inliers(X, by_seg, cams, Ks)
            if len(inl) < 2:
                continue
            segs = [s for s, _, _ in inl]
            if any(np.linalg.norm(h.X - X) < merge and set(h.segs) == set(segs) for h in hyps):
                continue
            uvs = [uv for _, uv, _ in inl]
            X, errs, cov = refine_point(X, np.array(uvs), [cams.poses[s][0] for s in segs], [cams.poses[s][1] for s in segs], [Ks[s] for s in segs])
            centers = np.array([cams.center(s) for s in segs])
            dirs = (centers - X) / np.linalg.norm(centers - X, axis=1, keepdims=True)
            ang = max(float(np.degrees(np.arccos(np.clip(dirs[i] @ dirs[j], -1, 1)))) for i in range(len(dirs)) for j in range(i + 1, len(dirs)))
            sigma = float(np.sqrt(max(np.linalg.eigvalsh(cov).max(), 0.0)))
            hyps.append(Hypothesis(X, segs, uvs, float(np.mean(errs)), ang, sigma, float(np.mean([sc for _, _, sc in inl]))))
        if hyps:
            # Collapse near-duplicates (same point reached from different pairs).
            hyps.sort(key=lambda h: (-h.n, h.err_px))
            uniq: list[Hypothesis] = []
            for h in hyps:
                if not any(np.linalg.norm(h.X - u.X) < merge for u in uniq):
                    uniq.append(h)
            out[led] = uniq[:MAX_HYPS]
    return out


def _inliers(X: np.ndarray, by_seg: dict, cams: Cameras, Ks: dict) -> list[tuple[str, np.ndarray, float]]:
    out = []
    for s, cands in by_seg.items():
        R, t = cams.poses[s]
        p, z = _project(X, R, t, Ks[s])
        if z <= 0:
            continue
        best = min(cands, key=lambda c: np.hypot(*(c[0] - p)))
        if np.hypot(*(best[0] - p)) < INLIER_PX:
            out.append((s, best[0], best[1]))
    return out


@dataclass
class VolumePrior:
    h: np.ndarray  # bin centers (y-up frame)
    r_env: np.ndarray
    h_lo: float
    h_hi: float
    scale: float  # tree height in world units

    def penalty(self, P: np.ndarray) -> np.ndarray:
        P = np.atleast_2d(P)
        r = np.hypot(P[:, 0], P[:, 2])
        env = np.interp(P[:, 1], self.h, self.r_env)
        excess = np.maximum(0, r - env) + np.maximum(0, P[:, 1] - self.h_hi) + np.maximum(0, self.h_lo - P[:, 1])
        return 3.0 * (excess / (0.1 * self.scale)) ** 2


def radius_profile(P: np.ndarray, bins: int = 12, q: float = 90) -> tuple[np.ndarray, np.ndarray]:
    """Outer radius per height band in a y-up frame centered on the tree axis,
    smoothed and non-increasing towards the top (architecture §9.8)."""
    h = P[:, 1]
    r = np.hypot(P[:, 0], P[:, 2])
    lo, hi = float(h.min()), float(h.max())
    edges = np.linspace(lo, hi, bins + 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    prof = np.full(bins, np.nan)
    for k in range(bins):
        m = (h >= edges[k]) & (h <= edges[k + 1])
        if m.sum() >= 3:
            prof[k] = np.percentile(r[m], q)
    ok = ~np.isnan(prof)
    if ok.sum() == 0:
        return centers, np.full(bins, float(np.percentile(r, q)))
    prof = np.interp(centers, centers[ok], prof[ok])
    prof = np.convolve(np.pad(prof, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    # Non-increasing toward the top: running max from the top down.
    prof = np.maximum.accumulate(prof[::-1])[::-1]
    return centers, prof


def volume_prior(P_reliable: np.ndarray) -> VolumePrior:
    h, prof = radius_profile(P_reliable)
    H = float(np.ptp(P_reliable[:, 1])) or 1.0
    return VolumePrior(h, 1.3 * prof + 0.05 * H, float(P_reliable[:, 1].min() - 0.1 * H), float(P_reliable[:, 1].max() + 0.1 * H), H)


def hypothesis_cost(h: Hypothesis, prior: VolumePrior | None, Xw: np.ndarray | None = None) -> float:
    c = h.err_px / 1.5 + 1.0 * max(0, 3 - h.n) + 0.5 * (1 - min(1.0, h.angle_deg / 15.0)) + 0.5 * (1 - min(1.0, h.score))
    if prior is not None:
        c += float(prior.penalty(h.X if Xw is None else Xw)[0])
    return c
