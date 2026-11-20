"""Vectorized geometry helpers for reconstruction."""

from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares

from .camera import Intrinsics, undistort_normalized


def rodrigues_batch(rvecs: np.ndarray) -> np.ndarray:
    """(M,3) rotation vectors -> (M,3,3) matrices."""
    rvecs = np.asarray(rvecs, float).reshape(-1, 3)
    th = np.linalg.norm(rvecs, axis=1)
    k = np.where(th[:, None] > 1e-12, rvecs / np.maximum(th, 1e-12)[:, None], 0.0)
    K = np.zeros((len(k), 3, 3))
    K[:, 0, 1], K[:, 0, 2] = -k[:, 2], k[:, 1]
    K[:, 1, 0], K[:, 1, 2] = k[:, 2], -k[:, 0]
    K[:, 2, 0], K[:, 2, 1] = -k[:, 1], k[:, 0]
    s, c = np.sin(th)[:, None, None], np.cos(th)[:, None, None]
    return np.eye(3)[None] + s * K + (1 - c) * (K @ K)


def project_points(X: np.ndarray, R: np.ndarray, t: np.ndarray, f: np.ndarray, cx: np.ndarray, cy: np.ndarray,
                   k1: float, k2: float) -> tuple[np.ndarray, np.ndarray]:
    """Per-observation projection: X (M,3), R (M,3,3), t (M,3), f/cx/cy (M,)."""
    Xc = np.einsum("mij,mj->mi", R, X) + t
    z = Xc[:, 2]
    zs = np.where(np.abs(z) < 1e-9, 1e-9, z)
    x, y = Xc[:, 0] / zs, Xc[:, 1] / zs
    r2 = x * x + y * y
    d = 1 + k1 * r2 + k2 * r2 * r2
    return np.stack([f * x * d + cx, f * y * d + cy], axis=1), z


def triangulate_rays(centers: np.ndarray, dirs: np.ndarray) -> np.ndarray:
    """Least-squares intersection of rays (C_k + s d_k)."""
    A = np.zeros((3, 3))
    b = np.zeros(3)
    for C, d in zip(centers, dirs):
        d = d / np.linalg.norm(d)
        P = np.eye(3) - np.outer(d, d)
        A += P
        b += P @ C
    return np.linalg.lstsq(A, b, rcond=None)[0]


def pixel_ray(uv: np.ndarray, R: np.ndarray, t: np.ndarray, K: Intrinsics) -> tuple[np.ndarray, np.ndarray]:
    """World-frame ray (center, unit direction) through pixel(s) uv (N,2)."""
    n = undistort_normalized(np.asarray(uv, float).reshape(-1, 2), K)
    d_cam = np.concatenate([n, np.ones((len(n), 1))], axis=1)
    d = d_cam @ R  # R^T d_cam for each row
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    C = -R.T @ t
    return C, d


def refine_point(X0: np.ndarray, obs_uv: np.ndarray, Rs: list[np.ndarray], ts: list[np.ndarray], Ks: list[Intrinsics],
                 loss: str = "linear") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Nonlinear triangulation of one point with fixed cameras.
    Returns (X, per-observation reprojection error px, 3x3 covariance for 1 px noise)."""
    Rm = np.stack(Rs)
    tm = np.stack(ts)
    f = np.array([k.f for k in Ks])
    cx = np.array([k.cx for k in Ks])
    cy = np.array([k.cy for k in Ks])
    k1 = np.array([k.k1 for k in Ks])
    k2 = np.array([k.k2 for k in Ks])

    def res(X: np.ndarray) -> np.ndarray:
        Xc = np.einsum("mij,j->mi", Rm, X) + tm
        z = np.where(np.abs(Xc[:, 2]) < 1e-9, 1e-9, Xc[:, 2])
        x, y = Xc[:, 0] / z, Xc[:, 1] / z
        r2 = x * x + y * y
        d = 1 + k1 * r2 + k2 * r2 * r2
        uv = np.stack([f * x * d + cx, f * y * d + cy], axis=1)
        return (uv - obs_uv).ravel()

    sol = least_squares(res, X0, loss=loss, f_scale=2.0, method="lm" if loss == "linear" else "trf", max_nfev=50)
    r = res(sol.x).reshape(-1, 2)
    J = sol.jac
    try:
        cov = np.linalg.inv(J.T @ J)
    except np.linalg.LinAlgError:
        cov = np.eye(3) * 1e6
    return sol.x, np.linalg.norm(r, axis=1), cov
