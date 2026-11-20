"""Pinhole camera with radial distortion (architecture §9.2).

Conventions (same as OpenCV): x_cam = R @ X + t; the camera looks along +z,
image x to the right, image y down. Distortion: x_d = x * (1 + k1 r^2 + k2 r^4)
on normalized coordinates, so cv2 functions can be used with
dist = [k1, k2, 0, 0].
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Intrinsics:
    f: float  # pixels
    cx: float
    cy: float
    k1: float = 0.0
    k2: float = 0.0

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.f, 0, self.cx], [0, self.f, self.cy], [0, 0, 1.0]])

    @property
    def dist(self) -> np.ndarray:
        return np.array([self.k1, self.k2, 0.0, 0.0])

    @staticmethod
    def from_fov(width: int, height: int, hfov_deg: float) -> "Intrinsics":
        f = 0.5 * max(width, height) / np.tan(np.radians(hfov_deg) / 2)
        return Intrinsics(f, (width - 1) / 2, (height - 1) / 2)


def rodrigues(rvec: np.ndarray) -> np.ndarray:
    return cv2.Rodrigues(np.asarray(rvec, dtype=np.float64).reshape(3, 1))[0]


def to_rvec(R: np.ndarray) -> np.ndarray:
    return cv2.Rodrigues(np.asarray(R, dtype=np.float64))[0].ravel()


def project(X: np.ndarray, R: np.ndarray, t: np.ndarray, K: Intrinsics) -> tuple[np.ndarray, np.ndarray]:
    """Project (N,3) points; returns (uv (N,2), depth (N,))."""
    Xc = np.asarray(X, float) @ np.asarray(R).T + np.asarray(t).reshape(1, 3)
    z = Xc[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        x = Xc[:, 0] / z
        y = Xc[:, 1] / z
    r2 = x * x + y * y
    d = 1 + K.k1 * r2 + K.k2 * r2 * r2
    uv = np.stack([K.f * x * d + K.cx, K.f * y * d + K.cy], axis=1)
    return uv, z


def undistort_normalized(uv: np.ndarray, K: Intrinsics) -> np.ndarray:
    """Pixel coordinates -> undistorted normalized coordinates (N,2)."""
    pts = np.asarray(uv, np.float64).reshape(-1, 1, 2)
    return cv2.undistortPoints(pts, K.K, K.dist).reshape(-1, 2)


def camera_center(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    return -np.asarray(R).T @ np.asarray(t).reshape(3)


def look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray = np.array([0.0, 1.0, 0.0])) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) for a camera at eye looking at target with world `up` mapping to image up (-y)."""
    z = np.asarray(target, float) - np.asarray(eye, float)
    z /= np.linalg.norm(z)
    x = np.cross(-np.asarray(up, float), z)  # right-handed with y pointing down
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])
    t = -R @ np.asarray(eye, float)
    return R, t
