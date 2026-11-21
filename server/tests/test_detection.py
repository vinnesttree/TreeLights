import itertools

import numpy as np
import pytest

from treelights.detection import binary, constellation
from treelights.detection.detector import build_reference, color_class, decode_image, detect_single
from treelights.sim.scene import Faults, Scene, encode_jpeg
from treelights.sim.tree import make_tree


@pytest.fixture(scope="module")
def scene():
    sc = Scene(make_tree(seed=1), seed=1)
    sc.add_view(0)
    return sc


def grab(sc, leds, view=0):
    return decode_image(encode_jpeg(sc.render(sc.views[view], leds)))


def dark_ref(sc, view=0, n=5):
    return build_reference([grab(sc, np.zeros((600, 3), np.uint8), view) for _ in range(n)])


def test_hamming_roundtrip_and_distance():
    codes = binary.code_table(2048)
    for i in range(0, 2048, 37):
        assert binary.value_to_index(binary.decode(codes[i])) == i
    # Bit planes are balanced for 600 LEDs.
    frac = binary.code_table(600).mean(axis=0)
    assert frac.min() > 0.3 and frac.max() < 0.7
    # Minimum distance 4 over a sample of pairs.
    idx = np.random.default_rng(0).choice(2048, 300, replace=False)
    dmin = min(int((codes[a] != codes[b]).sum()) for a, b in itertools.combinations(idx, 2))
    assert dmin >= 4
    # Every 1-, 2- and 3-bit error is rejected.
    c = codes[613]
    for k in (1, 2, 3):
        for flip in itertools.combinations(range(16), k):
            e = c.copy()
            e[list(flip)] ^= 1
            assert binary.decode(e) is None


def test_pattern_frames_light_each_led_in_one_frame_per_pair():
    fr = binary.pattern_frames(600, (30, 30, 30))
    assert len(fr) == 32
    for b in range(16):
        on_p = fr[2 * b].max(1) > 0
        on_c = fr[2 * b + 1].max(1) > 0
        assert np.all(on_p ^ on_c)


def test_single_led_accuracy_and_color(scene):
    """Clearly visible LEDs, with light scattered by the branches around them and needle glints."""
    ref = dark_ref(scene)
    uv, _ = scene.project_view(scene.views[0])
    vis = scene.views[0].vis
    errs, missed = [], 0
    basis = np.array([[1, 0.12, 0.02], [0.10, 1, 0.15], [0.02, 0.12, 1.0]])  # rows: R, G, B responses
    colors = [(70, 0, 0), (0, 70, 0), (0, 0, 70)]
    for k, i in enumerate(np.nonzero(vis > 0.5)[0][:60]):
        leds = np.zeros((600, 3), np.uint8)
        leds[i] = colors[k % 3]
        det = detect_single(grab(scene, leds), ref, expected_color=basis[k % 3])
        assert det.valid, det.reason
        if not det.candidates:
            missed += 1
            continue
        c = det.candidates[0]
        errs.append(np.hypot(c.x - uv[i, 0], c.y - uv[i, 1]))
        assert c.kind == "point"
        assert color_class(np.array(c.color), basis)[0] == k % 3
    assert missed <= 1
    assert np.median(errs) < 0.35 and np.max(errs) < 1.5


def test_partly_hidden_led_found_despite_brighter_glint():
    """A needle glint brighter than a mostly hidden LED: the LED is still a candidate."""
    tree = make_tree(seed=4)
    sc = Scene(tree, seed=4, faults=Faults(glint_prob=1.0))  # every LED glints somewhere nearby
    v = sc.add_view(0)
    ref = dark_ref(sc)
    uv, _ = sc.project_view(v)
    partly = np.nonzero((v.vis > 0.12) & (v.vis < 0.4))[0][:40]
    assert len(partly) >= 20
    best_ok = in_cands = 0
    for i in partly:
        leds = np.zeros((600, 3), np.uint8)
        leds[i] = (0, 60, 0)
        det = detect_single(grab(sc, leds), ref, expected_color=np.array([0.1, 1, 0.15]))
        assert det.valid
        d = [np.hypot(c.x - uv[i, 0], c.y - uv[i, 1]) for c in det.candidates]
        in_cands += any(x < 2.5 for x in d)
        best_ok += bool(d) and d[0] < 2.5
        # Nothing far from the LED: everything else is a reflection close to it.
        assert all(x < 45 for x in d)
    assert in_cands >= 0.9 * len(partly)
    assert best_ok >= 0.6 * len(partly)


def test_dim_branch_reflections_next_to_bright_led_are_dropped():
    tree = make_tree(seed=4)
    sc = Scene(tree, seed=4, faults=Faults(glint_prob=1.0, foliage=2.0))
    v = sc.add_view(0)
    ref = dark_ref(sc)
    uv, _ = sc.project_view(v)
    single = 0
    bright = np.nonzero(v.vis > 0.9)[0][:30]
    for i in bright:
        leds = np.zeros((600, 3), np.uint8)
        leds[i] = (0, 60, 0)
        det = detect_single(grab(sc, leds), ref, expected_color=np.array([0.1, 1, 0.15]))
        c = det.candidates[0]
        assert np.hypot(c.x - uv[i, 0], c.y - uv[i, 1]) < 1.5
        single += len(det.candidates) == 1 or det.candidates[1].score < 0.5  # unambiguous
    assert single >= 0.9 * len(bright)


def test_hidden_led_gives_only_nearby_reflections_and_person_invalidates(scene):
    ref = dark_ref(scene)
    v = scene.views[0]
    uv, _ = scene.project_view(v)
    for hidden in np.nonzero(v.vis == 0)[0][:40]:
        leds = np.zeros((600, 3), np.uint8)
        leds[hidden] = (70, 70, 70)
        det = detect_single(grab(scene, leds), ref)
        assert det.valid
        # The LED itself can't be seen; whatever is (glints, lit branches) is near it.
        assert all(np.hypot(c.x - uv[hidden, 0], c.y - uv[hidden, 1]) < 45 for c in det.candidates)
    # Without branch light, a hidden LED shows nothing at all.
    scene.faults = Faults(foliage=0.0, glint_prob=0.0)
    leds = np.zeros((600, 3), np.uint8)
    leds[np.nonzero(v.vis == 0)[0][0]] = (70, 70, 70)
    det = detect_single(grab(scene, leds), ref)
    assert det.valid and not det.candidates
    scene.faults = Faults(person_frames=(scene.frame_no + 1, scene.frame_no + 2))
    det = detect_single(grab(scene, leds), ref)
    scene.faults = Faults()
    assert not det.valid and det.reason == "scene_changed"


def test_scattered_light_alone_is_diffuse_with_large_uncertainty():
    """A hidden LED whose light is only seen on the branches around it."""
    tree = make_tree(seed=6)
    sc = Scene(tree, seed=6, faults=Faults(foliage=6.0, glint_prob=0.0))
    v = sc.add_view(0)
    ref = dark_ref(sc)
    uv, _ = sc.project_view(v)
    seen_diffuse = 0
    for i in np.nonzero((v.vis == 0) & (v.patch_vis.sum(axis=1) > 0.6))[0][:25]:
        leds = np.zeros((600, 3), np.uint8)
        leds[i] = (0, 80, 0)
        det = detect_single(grab(sc, leds), ref, expected_color=np.array([0.1, 1, 0.15]))
        assert det.valid, det.reason  # a glowing branch region isn't a "scene change"
        for c in det.candidates:
            if c.kind == "diffuse":
                seen_diffuse += 1
                assert c.sigma_px >= 2.0
    assert seen_diffuse >= 5


def test_binary_decode_has_no_wrong_indices(scene):
    ref = dark_ref(scene)
    dec = binary.BinaryDecoder(ref, 600)
    allon = np.full((600, 3), 22, np.uint8)
    n = dec.set_allon([grab(scene, allon) for _ in range(2)])
    assert n > 200
    frames = binary.pattern_frames(600, (22, 22, 22))
    for b in range(16):
        chk = dec.add_pair(b, grab(scene, frames[2 * b]), grab(scene, frames[2 * b + 1]))
        assert chk.ok, chk.reason
    res = dec.result()
    uv, _ = scene.project_view(scene.views[0])
    v = scene.views[0]
    seen = np.nonzero(v.vis > 0)[0]
    wrong = []
    for r in res:
        d_own = np.hypot(*(uv[r["index"]] - [r["x"], r["y"]]))
        d = np.hypot(uv[seen, 0] - r["x"], uv[seen, 1] - r["y"])
        # Reflections off branches blink with their LED and decode to its index, near it.
        # An identity error is a blob far from its LED or on another LED's core.
        if d_own > 45 or (d_own > 2.5 and d.min() < 1.5 and seen[np.argmin(d)] != r["index"] and r["kind"] == "point"):
            wrong.append(r)
    assert wrong == []
    # Clearly visible LEDs decode at their own core; the one-at-a-time pass covers the rest.
    at_core = {r["index"] for r in res if np.hypot(*(uv[r["index"]] - [r["x"], r["y"]])) <= 2.5}
    clear = set(np.nonzero(v.vis > 0.5)[0])
    assert len(at_core & clear) >= 0.75 * len(clear)
    assert len({r["index"] for r in res}) > 0.6 * len(seen)


def test_binary_stale_pair_is_rejected(scene):
    ref = dark_ref(scene)
    dec = binary.BinaryDecoder(ref, 600)
    dec.set_allon([grab(scene, np.full((600, 3), 22, np.uint8))])
    frames = binary.pattern_frames(600, (22, 22, 22))
    same = grab(scene, frames[4])
    chk = dec.add_pair(2, same, same)  # complement frame never arrived
    assert not chk.ok and chk.reason == "inconsistent"


def test_constellation_detects_bump():
    sc = Scene(make_tree(seed=2), seed=2)
    sc.add_view(0)
    ref = dark_ref(sc)
    leds = np.zeros((600, 3), np.uint8)
    leds[::30] = (60, 60, 60)
    p0 = constellation.constellation_points(grab(sc, leds), ref)
    p1 = constellation.constellation_points(grab(sc, leds), ref)
    assert not constellation.compare(p0, p1).moved
    sc.faults = Faults(bump_at_frame=sc.frame_no + 1, bump_px=4)
    p2 = constellation.constellation_points(grab(sc, leds), ref)
    m = constellation.compare(p0, p2)
    assert m.moved and m.median_shift > 2
