import numpy as np

from treelights.capture.store import ScanStore
from treelights.effects import Chase, StatusColors, SweepTest, TreeGeometry, make_renderer
from treelights.maps.store import ActivationBlocked, MapStore, export_csv
from treelights.output.manager import PowerLimiter


def small_map(n=50):
    t = np.linspace(0, 6 * np.pi, n)
    h = np.linspace(0, 100, n)
    r = 40 * (1 - h / 110)
    leds = [{"i": i, "p": [float(r[i] * np.cos(t[i])), float(h[i]), float(r[i] * np.sin(t[i]))],
             "status": "measured" if i % 7 else "estimated", "segments": 2, "err_px": 0.5, "sigma": 0.5} for i in range(n)]
    leds[-1]["p"], leds[-1]["status"] = None, "unknown"
    return {"id": "m1", "scan": "s1", "leds": leds, "surface": {"h": [0, 50, 100], "r": [40, 22, 4]},
            "quality": {"activation_blocked": None, "warnings": []}, "wled": {"config_hash": "sha256:a"}}


def test_sweep_phases_light_the_right_leds():
    m = small_map()
    tree = TreeGeometry.from_map(m)
    eff = SweepTest()
    eff.setup(tree)
    out = np.zeros((tree.n, 3))
    eff.render(0.5 * SweepTest.PHASE * 0.5, out)  # rising plane at a quarter of the height
    lit = out.max(1) > 0.5
    assert lit.any() and np.all(np.abs(tree.h_norm[lit] - 0.25) < 0.07)
    assert out[-1].max() == 0  # LED without a position stays off
    eff.render(SweepTest.PHASE * 1.25, out)  # rotating half-plane
    assert (out.max(1) > 0.5).sum() > 0


def test_status_colors_and_chase_without_map():
    tree = TreeGeometry.from_map(small_map())
    eff = StatusColors()
    eff.setup(tree)
    out = np.zeros((tree.n, 3))
    eff.render(0, out)
    assert tuple(out[1]) == (0, 1, 0) and tuple(out[0]) == (1, 0.3, 0) and out[-1].max() == 0
    c = Chase()
    c.setup(TreeGeometry.chain_only(30))
    r = make_renderer(c, brightness=0.5)
    f = r(100.0)
    assert f.dtype == np.uint8 and f.shape == (30, 3) and f.max() <= 128


def test_power_limiter_caps_full_white():
    pl = PowerLimiter(2000, 20, 1)
    full = np.full((600, 3), 255, np.uint8)
    assert pl.estimate_ma(pl.apply(full)) <= 2000


def test_map_store_versions_activation_fingerprint_rollback(tmp_path):
    ms = MapStore(tmp_path)
    a = ms.save(small_map())
    b = ms.save(small_map())  # same id: stored as a new version, never overwritten
    assert a != b
    ms.activate(a, "sha256:a")
    try:
        ms.activate(b, "sha256:changed")
        raise AssertionError("should need confirmation")
    except ActivationBlocked:
        pass
    assert ms.activate(b, "sha256:changed", force=True)["warning"]
    assert ms.rollback() == a and ms.active_id() == a
    blocked = small_map()
    blocked["quality"]["activation_blocked"] = "Fewer than 3 segments"
    c = ms.save(blocked)
    try:
        ms.activate(c)
        raise AssertionError("blocked map activated")
    except ActivationBlocked:
        pass
    assert export_csv(ms.get(a)).splitlines()[0] == "i,x,y,z,status,sigma"


def test_frame_retention_keeps_active_and_recent(tmp_path):
    store = ScanStore(tmp_path)
    ids = []
    for _ in range(4):
        s = store.create(10)
        s.write_frame("v1a", 1, "led", b"x")
        ids.append(s.id)
    pruned = store.prune_frames({ids[0]}, keep_recent=2)
    assert set(pruned) == {ids[1]}
    assert (tmp_path / "scans" / ids[0] / "frames").exists()
