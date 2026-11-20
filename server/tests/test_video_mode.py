import numpy as np
import pytest

from treelights.capture.store import ScanStore
from treelights.capture.video import Timeline, decode_video, find_preamble
from treelights.sim.scene import Faults, Scene
from treelights.sim.synthvideo import write_video
from treelights.sim.tree import make_tree


def check_identities(scan, scene, view):
    obs = scan.observations()
    uv, _ = scene.project_view(view)
    true = uv + view.glow_offset
    seen = np.nonzero(view.vis > 0)[0]
    wrong, right = [], 0
    for seg, leds in obs.items():
        for led, c in leds.items():
            if not c:
                continue
            d = np.hypot(true[seen, 0] - c[0]["x"], true[seen, 1] - c[0]["y"])
            if view.vis[led] > 0 and np.hypot(*(true[led] - [c[0]["x"], c[0]["y"]])) <= 2.5:
                right += 1
            else:
                wrong.append((led, int(seen[np.argmin(d)])))
    return right, wrong


@pytest.mark.timeout(600)
def test_video_mode_decodes_with_vfr_and_dropped_frames(tmp_path):
    n = 70
    tree = make_tree(n=n, seed=5, height=110, base_radius=40, bottom=15)
    scene = Scene(tree, seed=5, width=960, height=540)
    scene.intr.f, scene.intr.cx, scene.intr.cy = 750.0, 479.5, 269.5
    scene.eye = scene.eye * np.array([1, 0.7, 0.7])
    scene.target = scene.target * 0.6
    scene.__post_init__()
    v = scene.add_view(0)
    tl = Timeline.build(n, seed=1)
    path = str(tmp_path / "rec.mp4")
    frames = write_video(path, scene, v, tl, drop_prob=0.02, seed=2)
    assert frames > 30 * tl.duration * 0.9
    scan = ScanStore(tmp_path).create(n)
    vid = scan.add_view({"mode": "video"})
    res = decode_video(scan, vid, path, tl)
    assert res.blocks_ok == res.blocks_total == 2
    right, wrong = check_identities(scan, scene, v)
    visible = int((v.vis > 0).sum())
    assert wrong == []
    assert right >= 0.85 * visible
    assert res.segments and res.detections >= right


def test_preamble_detection_on_synthetic_brightness():
    tl = Timeline.build(10, seed=0)
    times = np.arange(0, 8, 1 / 30)
    vals = np.array([10 + 50 * float(tl.frame_at(t - 1.3).max() == tl.sync_level) for t in times])
    t0 = find_preamble(times, vals)
    assert t0 is not None and abs(t0 - 1.3) < 0.05


@pytest.mark.timeout(600)
def test_video_mode_bump_starts_new_segment(tmp_path):
    n = 70
    tree = make_tree(n=n, seed=6, height=110, base_radius=40, bottom=15)
    tl = Timeline.build(n, seed=3)
    # Bump the camera during the second block (after ~ the first block's frames).
    bump_frame = int(30 * (2.0 + [e for e in tl.events if e.kind == "const" and e.data["block"] == 1][0].start - 0.8))
    scene = Scene(tree, seed=6, width=960, height=540, faults=Faults(bump_at_frame=bump_frame, bump_px=5))
    scene.intr.f, scene.intr.cx, scene.intr.cy = 750.0, 479.5, 269.5
    scene.eye = scene.eye * np.array([1, 0.7, 0.7])
    scene.target = scene.target * 0.6
    scene.__post_init__()
    v = scene.add_view(0)
    path = str(tmp_path / "rec.mp4")
    write_video(path, scene, v, tl, drop_prob=0.0, seed=4)
    scan = ScanStore(tmp_path).create(n)
    vid = scan.add_view({"mode": "video"})
    res = decode_video(scan, vid, path, tl)
    assert len(res.segments) == 2
    assert any("Movement" in w for w in res.warnings)
