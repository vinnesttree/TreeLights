"""Refinement (§9.7) and filling what wasn't measured (§9.9).

All positions here are in the y-up frame with the tree axis at x = z = 0.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from .. import native
from .camera import Intrinsics, undistort_normalized
from .chain import Bounds, slack


@dataclass
class View:
    """A registered segment expressed in the y-up frame."""

    id: str
    R: np.ndarray
    t: np.ndarray
    K: Intrinsics
    width: int
    height: int

    @property
    def center(self) -> np.ndarray:
        return -self.R.T @ self.t

    def project(self, P: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        Xc = np.atleast_2d(P) @ self.R.T + self.t
        z = Xc[:, 2]
        zs = np.where(np.abs(z) < 1e-9, 1e-9, z)
        x, y = Xc[:, 0] / zs, Xc[:, 1] / zs
        r2 = x * x + y * y
        d = 1 + self.K.k1 * r2 + self.K.k2 * r2 * r2
        return np.stack([self.K.f * x * d + self.K.cx, self.K.f * y * d + self.K.cy], axis=1), z

    def ray(self, uv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        n = undistort_normalized(np.asarray(uv, float)[None], self.K)[0]
        d = self.R.T @ np.array([n[0], n[1], 1.0])
        return self.center, d / np.linalg.norm(d)


def refine_points(pos: dict[int, np.ndarray], support: dict[int, list[tuple[str, np.ndarray]]], views: dict[str, View],
                  bounds: Bounds, sigma: dict[int, float], weight_px: float = 0.005) -> dict[int, np.ndarray]:
    """Points-only least squares: reprojection of each point's inlier observations
    plus a hinge penalty on links longer than their bound (cameras fixed)."""
    leds = sorted(pos)
    if not leds:
        return pos
    li = {l: k for k, l in enumerate(leds)}
    obs = [(li[l], s, uv) for l in leds for s, uv in support.get(l, [])]
    links = [i for i in leds if i + 1 in li and np.isfinite(bounds.bound[i])]
    scale = 1.0 / (weight_px * bounds.base)  # a violation of 0.5% of the base length ~ 1 px
    if native.available("refine_points"):
        return _refine_points_native(leds, li, obs, links, pos, views, bounds, sigma, scale)
    x0 = np.array([pos[l] for l in leds]).ravel()
    vR = {s: v for s, v in views.items()}

    def res(x: np.ndarray) -> np.ndarray:
        P = x.reshape(-1, 3)
        out = []
        for k, s, uv in obs:
            p, _ = vR[s].project(P[k])
            out.append(p[0] - uv)
        r = np.concatenate(out) if out else np.zeros(0)
        h = []
        for i in links:
            d = np.linalg.norm(P[li[i + 1]] - P[li[i]])
            allowed = bounds.bound[i] + float(slack(bounds.base, sigma.get(i, 0.0), sigma.get(i + 1, 0.0)))
            h.append(max(0.0, d - allowed) * scale)
        return np.concatenate([r, np.array(h)])

    m = 2 * len(obs) + len(links)
    A = lil_matrix((m, len(x0)), dtype=int)
    for r_i, (k, _s, _uv) in enumerate(obs):
        A[2 * r_i : 2 * r_i + 2, 3 * k : 3 * k + 3] = 1
    for q, i in enumerate(links):
        A[2 * len(obs) + q, 3 * li[i] : 3 * li[i] + 3] = 1
        A[2 * len(obs) + q, 3 * li[i + 1] : 3 * li[i + 1] + 3] = 1
    sol = least_squares(res, x0, jac_sparsity=A, method="trf", x_scale="jac", max_nfev=50)
    P = sol.x.reshape(-1, 3)
    return {l: P[li[l]] for l in leds}


def _refine_points_native(leds, li, obs, links, pos, views, bounds, sigma, scale) -> dict[int, np.ndarray]:
    """refine_points' least squares in Rust (native/src/hyp.rs): an exact block-tridiagonal
    Levenberg-Marquardt instead of scipy's sparse finite differences."""
    vids = sorted({s for _k, s, _uv in obs})
    vi = {s: k for k, s in enumerate(vids)}
    cam_r = np.array([views[s].R for s in vids], float).reshape(-1, 3, 3)
    cam_t = np.array([views[s].t for s in vids], float).reshape(-1, 3)
    cam_k = np.array([[views[s].K.f, views[s].K.cx, views[s].K.cy, views[s].K.k1, views[s].K.k2] for s in vids],
                     float).reshape(-1, 5)
    allowed = np.array([bounds.bound[i] + float(slack(bounds.base, sigma.get(i, 0.0), sigma.get(i + 1, 0.0)))
                        for i in links], float)
    P = native.lib.refine_points(
        cam_r, cam_t, cam_k, np.array([pos[l] for l in leds], float).reshape(-1, 3),
        np.array([k for k, _s, _uv in obs], np.int64), np.array([vi[s] for _k, s, _uv in obs], np.int64),
        np.array([uv for _k, _s, uv in obs], float).reshape(-1, 2), np.array([li[i] for i in links], np.int64),
        allowed, float(scale))
    return {l: P[li[l]] for l in leds}


def coverage(P: np.ndarray, views: list[View], surface: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """How many cameras would plausibly have seen an LED at each point: in the
    image, on the camera's side of the tree, and in the outer part of the tree."""
    P = np.atleast_2d(P)
    rs = np.interp(P[:, 1], surface[0], surface[1])
    r = np.hypot(P[:, 0], P[:, 2])
    outer = r >= 0.5 * np.maximum(rs, 1e-6)
    out = np.zeros(len(P))
    for v in views:
        uv, z = v.project(P)
        inside = (z > 0) & (uv[:, 0] >= 0) & (uv[:, 0] < v.width) & (uv[:, 1] >= 0) & (uv[:, 1] < v.height)
        c = v.center
        facing = (P[:, 0] * c[0] + P[:, 2] * c[2]) > 0
        out += inside & facing & outer
    return out


def _cyl(P: np.ndarray, surface) -> np.ndarray:
    rs = np.interp(P[..., 1], surface[0], surface[1])
    r = np.hypot(P[..., 0], P[..., 2])
    return np.stack([P[..., 1], np.arctan2(P[..., 2], P[..., 0]), r / np.maximum(rs, 1e-6)], axis=-1)


def _from_cyl(h: np.ndarray, th: np.ndarray, rho: np.ndarray, surface) -> np.ndarray:
    rs = np.interp(h, surface[0], surface[1])
    r = rho * rs
    return np.stack([r * np.cos(th), h, r * np.sin(th)], axis=-1)


def surface_path(a: np.ndarray, b: np.ndarray, m: int, surface, views: list[View], max_len: float) -> np.ndarray:
    """m interior points from a to b around the tree, in whichever direction passes
    through places the cameras could not see; straight line if neither fits."""
    ca, cb = _cyl(a, surface), _cyl(b, surface)
    s = np.linspace(0, 1, m + 2)[1:-1]
    dth = (cb[1] - ca[1] + np.pi) % (2 * np.pi) - np.pi
    options = []
    for d in (dth, dth - 2 * np.pi if dth > 0 else dth + 2 * np.pi):
        P = _from_cyl(ca[0] + s * (cb[0] - ca[0]), ca[1] + s * d, ca[2] + s * (cb[2] - ca[2]), surface)
        full = np.vstack([a, P, b])
        length = float(np.linalg.norm(np.diff(full, axis=0), axis=1).sum())
        if length <= max_len * 1.02:
            options.append((float(coverage(P, views, surface).sum()), length, P))
    if options:
        options.sort(key=lambda o: (o[0], o[1]))
        return options[0][2]
    return a + s[:, None] * (b - a)


def solve_gap(a: np.ndarray, b: np.ndarray | None, links: list[float], path: np.ndarray,
              rays: dict[int, tuple[np.ndarray, np.ndarray]], scale_ref: float) -> np.ndarray:
    """Place m interior LEDs: link bounds, rays for single-view LEDs, weak pull to path.
    links[k] bounds the link between consecutive points of [a, p0..pm-1, (b)]."""
    m = len(path)

    def res(x: np.ndarray) -> np.ndarray:
        P = x.reshape(m, 3)
        chain = [a] + list(P) + ([b] if b is not None else [])
        out = []
        for k in range(len(chain) - 1):
            d = np.linalg.norm(chain[k + 1] - chain[k])
            out.append(max(0.0, d - links[k]) / (0.01 * scale_ref))
        for k, (C, dvec) in rays.items():
            v = P[k] - C
            out.extend((v - (v @ dvec) * dvec) / (0.005 * scale_ref))
        out.extend(((P - path) / (0.3 * scale_ref)).ravel())
        return np.array(out)

    x0 = path.ravel().copy()
    for k, (C, dvec) in rays.items():  # start single-view LEDs on their ray
        v = path[k] - C
        x0[3 * k : 3 * k + 3] = C + max(v @ dvec, 0.0) * dvec
    if native.available("solve_gap"):
        # The same problem in Rust (native/src/fill.rs): exact block-tridiagonal steps instead
        # of a dense finite-difference Jacobian, whose cost grows with the cube of the gap.
        ks = sorted(rays)
        return native.lib.solve_gap(
            np.asarray(a, float), None if b is None else np.asarray(b, float), np.asarray(links, float),
            np.ascontiguousarray(path, float).reshape(m, 3), x0.reshape(m, 3), np.array(ks, np.int64),
            np.array([rays[k][0] for k in ks], float).reshape(-1, 3), np.array([rays[k][1] for k in ks], float).reshape(-1, 3),
            float(scale_ref))
    sol = least_squares(res, x0, method="trf", max_nfev=200)
    return sol.x.reshape(m, 3)


def fill(n: int, pos: dict[int, np.ndarray], bounds: Bounds, views: dict[str, View],
         ray_cands: dict[int, list[tuple[str, np.ndarray]]], surface, height: float) -> tuple[dict[int, np.ndarray], dict[int, str], dict[int, float]]:
    """Returns (positions, status, sigma) for LEDs not in pos."""
    vlist = list(views.values())
    out_p: dict[int, np.ndarray] = {}
    status: dict[int, str] = {}
    sig: dict[int, float] = {}
    measured = sorted(pos)
    if not measured:
        return out_p, {i: "unknown" for i in range(n)}, sig

    def best_ray(i: int, near: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
        best, bd = None, np.inf
        for s, uv in ray_cands.get(i, []):
            C, d = views[s].ray(uv)
            v = near - C
            dist = np.linalg.norm(v - (v @ d) * d)
            if dist < bd:
                best, bd = (C, d), dist
        return best

    def link_b(i: int) -> float:
        return bounds.bound[i] if np.isfinite(bounds.bound[i]) else np.inf

    # Interior gaps between measured LEDs with no break inside.
    for a, b in zip(measured, measured[1:]):
        if b - a < 2:
            continue
        lks = [link_b(i) for i in range(a, b)]
        if np.all(np.isfinite(lks)):
            m = b - a - 1
            path = surface_path(pos[a], pos[b], m, surface, vlist, float(np.sum(lks)))
            rays = {}
            for k in range(m):
                r = best_ray(a + 1 + k, path[k])
                if r is not None:
                    rays[k] = r
            P = solve_gap(pos[a], pos[b], lks, path, rays, height)
            for k in range(m):
                i = a + 1 + k
                out_p[i] = P[k]
                status[i] = "single_view" if k in rays else "estimated"
                depth = min(k + 1, m - k)
                sig[i] = float(bounds.base * (0.5 if k in rays else 0.5 * np.sqrt(depth)))
        else:
            # A break inside: treat each side as a chain end.
            j = a + int(np.argmax(~np.isfinite(lks)))  # link j..j+1 is the first break
            _fill_end(a, list(range(a + 1, j + 1)), pos, bounds, best_ray, out_p, status, sig)
            _fill_end(b, list(range(b - 1, j, -1)), pos, bounds, best_ray, out_p, status, sig)
    _fill_end(measured[0], list(range(measured[0] - 1, -1, -1)), pos, bounds, best_ray, out_p, status, sig)
    _fill_end(measured[-1], list(range(measured[-1] + 1, n)), pos, bounds, best_ray, out_p, status, sig)
    return out_p, status, sig


def _fill_end(anchor: int, run: list[int], pos, bounds: Bounds, best_ray, out_p, status, sig) -> None:
    """LEDs past the last measured one in a direction: only single-view LEDs close to
    the anchor can be placed (on their ray, within reach); the rest are unknown."""
    prev = pos[anchor]
    reach = 0.0
    for k, i in enumerate(run):
        link = i if i < anchor else i - 1  # the link between i and its neighbour toward the anchor
        b = bounds.bound[link] if 0 <= link < len(bounds.bound) else np.inf
        if not np.isfinite(b):
            for j in run[k:]:
                status[j] = "unknown"
            return
        reach += b
        r = best_ray(i, prev)
        placed = False
        if r is not None and k < 5:
            C, d = r
            v = pos[anchor] - C
            P = C + max(v @ d, 0.0) * d
            if np.linalg.norm(P - pos[anchor]) <= reach:
                out_p[i], status[i], sig[i] = P, "single_view", float(bounds.base * (1 + k))
                prev = P
                placed = True
        if not placed:
            for j in run[k:]:
                status[j] = "unknown"
            return
