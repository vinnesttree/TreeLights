import cv2
import numpy as np

from treelights import camcal
from treelights.reconstruction.camera import look_at, project, Intrinsics


def render_board_view(board_img: np.ndarray, K: Intrinsics, R: np.ndarray, t: np.ndarray, size=(1280, 720)) -> np.ndarray:
    """Warp the board image as a plane z=0 (x right, y down, 1 unit = 100 px of the image)."""
    h, w = board_img.shape
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    plane = np.array([[0, 0, 0], [w, 0, 0], [w, h, 0], [0, h, 0]], float) / 100.0
    uv, z = project(plane, R, t, K)
    H = cv2.getPerspectiveTransform(src, uv.astype(np.float32))
    out = cv2.warpPerspective(board_img, H, size, borderValue=40)
    return out


def test_board_png_and_calibration_recovers_focal(tmp_path):
    png = camcal.board_png(1600, 1000)
    board_img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    K = Intrinsics(1000.0, 639.5, 359.5, 0.0, 0.0)
    center = np.array([8.0, 5.0, 0.0])
    store = camcal.ProfileStore(tmp_path)
    rng = np.random.default_rng(0)
    added = 0
    for k in range(14):
        eye = center + np.array([rng.uniform(-6, 6), rng.uniform(-4, 4), -rng.uniform(14, 20)])
        R, t = look_at(eye, center + rng.uniform(-1, 1, 3), up=np.array([0.0, -1.0, 0.0]))
        img = render_board_view(board_img, K, R, t)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        r = store.add_frame("dev1", 1280, 720, buf.tobytes())
        added = r["frames"]
    assert added >= camcal.MIN_FRAMES
    prof = store.solve("dev1", 1280, 720)
    assert abs(prof["f"] - 1000) / 1000 < 0.02
    assert prof["rms_px"] < 1.0
    prior = store.prior_for({"v1a": {"device": "dev1", "width": 1280, "height": 720}})
    assert abs(prior - 1000 / 1280) < 0.02
    assert store.prior_for({"v1a": {"device": "other", "width": 1280, "height": 720}}) is None
