import numpy as np
import pytest

from treelights.reconstruction import chain
from treelights.reconstruction.solve import SolveInput, solve
from treelights.sim.scene import Scene
from treelights.sim.synthobs import synthetic_observations
from treelights.sim.tree import make_tree


def align(P, Y):
    """Similarity transform of P onto Y (least squares); returns transformed P."""
    ma, mb = P.mean(0), Y.mean(0)
    U, S, Vt = np.linalg.svd((P - ma).T @ (Y - mb))
    D = np.eye(3)
    D[2, 2] = np.sign(np.linalg.det(U @ Vt))
    R = (U @ D @ Vt).T
    s = (S * np.diag(D)).sum() / ((P - ma) ** 2).sum()
    return s, lambda X: s * (X - ma) @ R.T + mb


def scene_with_views(tree, angles, seed):
    sc = Scene(tree, seed=seed)
    for a in angles:
        sc.add_view(a)
    return sc


@pytest.fixture(scope="module")
def full():
    tree = make_tree(seed=1, output_break_at=300)
    sc = scene_with_views(tree, range(0, 360, 45), 1)
    obs, segs = synthetic_observations(sc, mirror_prob=0.3, false_prob=0.1, swap_prob=0.01, seed=1)
    span = float(np.ptp(tree.positions[:, 1]))
    inp = SolveInput(600, obs, segs, span, [299], {})
    return tree, sc, inp, solve(inp)


def test_accuracy_targets(full):
    tree, _sc, _inp, m = full
    P = np.array([l["p"] for l in m["leds"]])
    st = np.array([l["status"] for l in m["leds"]])
    meas = st == "measured"
    assert meas.mean() >= 0.9
    s, tf = align(P[meas], tree.positions[meas])
    assert abs(s - 1) < 0.02, "tree height reference should give the right scale"
    err = np.linalg.norm(tf(P[meas]) - tree.positions[meas], axis=1)
    span = np.ptp(tree.positions[:, 1])
    assert np.median(err) < 0.01 * span
    assert np.percentile(err, 95) < 0.03 * span
    # No LED placed where another LED is (identity error): every error well below a link length.
    assert (err > 1.5 * tree.link).sum() == 0
    assert m["quality"]["violations"] == 0
    assert not m["quality"]["activation_blocked"]


def test_dynamic_bounds_find_connectors_and_breaks(full):
    tree, _sc, _inp, m = full
    found = {l["from"] for l in m["chain"]["long_links"]}
    assert set(tree.long_links) <= found
    assert {b["from"] for b in m["chain"]["breaks"]} == {299}
    true_max = max(np.linalg.norm(tree.positions[i + 1] - tree.positions[i]) for i in range(599) if tree.link_bound(i) == tree.link)
    assert true_max <= m["chain"]["base_link"] <= true_max * 1.25
    assert m["chain"]["period"] == 50


def test_up_axis_and_orientation(full):
    _tree, _sc, _inp, m = full
    q = m["quality"]
    assert q["up_axis"]["disagree"] == []
    assert q["visibility_side"] > 0.55
    P = np.array([l["p"] for l in m["leds"]])
    assert P[:, 1].min() >= -1e-6  # origin at the lowest LED, y up
    top = P[P[:, 1] > np.percentile(P[:, 1], 90)]
    bottom = P[P[:, 1] < np.percentile(P[:, 1], 10)]
    assert np.hypot(top[:, 0], top[:, 2]).mean() < np.hypot(bottom[:, 0], bottom[:, 2]).mean()  # narrower at the top


def test_solve_is_deterministic(full):
    _tree, _sc, inp, m = full
    m2 = solve(inp)
    strip = lambda d: {k: v for k, v in d.items() if k not in ("id", "created")}  # noqa: E731
    assert strip(m2) == strip(m)


def test_one_sided_views_fill_back_and_flag():
    tree = make_tree(seed=2)
    sc = scene_with_views(tree, [-80, -40, 0, 40, 80], 2)  # tree against a wall: back never faces the camera
    obs, segs = synthetic_observations(sc, seed=2)
    span = float(np.ptp(tree.positions[:, 1]))
    m = solve(SolveInput(600, obs, segs, span, [], {}))
    st = np.array([l["status"] for l in m["leds"]])
    P = np.array([l["p"] if l["p"] else [np.nan] * 3 for l in m["leds"]])
    meas = st == "measured"
    s, tf = align(P[meas], tree.positions[meas])
    err_m = np.linalg.norm(tf(P[meas]) - tree.positions[meas], axis=1)
    assert np.median(err_m) < 0.01 * span
    est = st == "estimated"
    assert est.sum() > 0
    err_e = np.linalg.norm(tf(P[est]) - tree.positions[est], axis=1)
    # Estimated LEDs are plausible (same side, not invented across the tree), not exact.
    assert np.median(err_e) < 0.2 * span
    assert all(l["sigma"] is not None and l["sigma"] > 0 for l in m["leds"] if l["status"] == "estimated")


def test_led_count_larger_than_string_is_unknown_and_warned():
    tree = make_tree(seed=3)
    sc = scene_with_views(tree, range(0, 360, 60), 3)
    obs, segs = synthetic_observations(sc, seed=3)
    for s in obs:  # WLED configured for 620 LEDs; the last 20 don't exist
        for i in range(600, 620):
            obs[s][i] = []
    m = solve(SolveInput(620, obs, segs, None, [], {}))
    tail = [l for l in m["leds"] if l["i"] >= 600]
    assert all(l["status"] == "unknown" and l["p"] is None for l in tail)
    assert any("never seen" in w for w in m["quality"]["warnings"])
    assert m["units"] == "tree_heights"


def _nodes(points, cost=0.3, sigma=0.01):
    return [chain.Node(np.array(p, float), sigma, cost) for p in points]


def test_chain_rejects_isolated_outlier_and_bridges_gap():
    n = 30
    true = np.stack([np.arange(n) * 1.0, np.zeros(n), np.zeros(n)], axis=1)
    nodes = {i: _nodes([true[i]]) for i in range(n)}
    nodes[10] = _nodes([[10, 8, 0]], cost=0.1) + _nodes([true[10]], cost=0.6)  # cheaper but impossible jump
    for i in (20, 21, 22):
        del nodes[i]  # unseen run
    b = chain.Bounds(1.1, 1.0, np.full(n - 1, 1.1), ["normal"] * (n - 1), 25)
    sel = chain.select_chain(n, nodes, b)
    assert sel[10] == 1
    assert set(sel) == set(range(n)) - {20, 21, 22}


def test_chain_known_break_allows_free_jump_and_unknown_jump_needs_evidence():
    n = 20
    pts = np.stack([np.arange(n) * 1.0, np.zeros(n), np.zeros(n)], axis=1)
    pts[10:] += [0, 50, 0]  # second output starts far away
    nodes = {i: _nodes([pts[i]]) for i in range(n)}
    bnd = np.full(n - 1, 1.1)
    b = chain.Bounds(1.1, 1.0, bnd.copy(), ["normal"] * (n - 1), 15)
    sel = chain.select_chain(n, nodes, b)
    assert len(sel) == n  # strong evidence on both sides: a break edge is worth c_break
    b.bound[9] = np.inf
    assert len(chain.select_chain(n, nodes, b)) == n
    # A lone far point with no supporting neighbours is dropped rather than paying for two breaks.
    nodes2 = {i: _nodes([[i, 0, 0]]) for i in range(n)}
    nodes2[5] = _nodes([[5, 40, 0]])
    b2 = chain.Bounds(1.1, 1.0, np.full(n - 1, 1.1), ["normal"] * (n - 1), 15)
    assert 5 not in chain.select_chain(n, nodes2, b2)


def test_learn_bounds_fallback_and_never_below_trusted():
    n = 10
    pos = {i: np.array([i * 1.0, 0, 0]) for i in range(n)}
    b = chain.learn_bounds(n, pos, {}, set(range(n)), height=100.0)
    assert b.fallback and np.isclose(b.base_raw, 12.0)
    pos = {i: np.array([i * 1.0, 0, 0]) for i in range(80)}
    pos.update({i: pos[i - 1] + [0.8, 0, 0] for i in range(1, 80)})
    b = chain.learn_bounds(80, pos, {}, set(range(80)), height=100.0, user={"cap": 0.5})
    assert np.all(b.bound[np.isfinite(b.bound)] >= 0.8 - 1e-9)


def test_branch_glints_and_scattered_light_keep_accuracy():
    """Needle glints (view-dependent, sometimes brighter than a mostly hidden LED) and hidden
    LEDs seen only as light scattered by branches must not cost accuracy or identities."""
    tree = make_tree(seed=8)
    sc = scene_with_views(tree, range(0, 360, 45), 8)
    obs, segs = synthetic_observations(sc, glint_prob=0.25, scatter_prob=0.5, seed=8)
    span = float(np.ptp(tree.positions[:, 1]))
    m = solve(SolveInput(600, obs, segs, span, [], {}))
    st = np.array([l["status"] for l in m["leds"]])
    P = np.array([l["p"] if l["p"] else [np.nan] * 3 for l in m["leds"]])
    meas = st == "measured"
    assert meas.mean() >= 0.9
    s, tf = align(P[meas], tree.positions[meas])
    err = np.linalg.norm(tf(P[meas]) - tree.positions[meas], axis=1)
    assert np.median(err) < 0.01 * span and np.percentile(err, 95) < 0.03 * span
    assert (err > tree.link).sum() == 0  # no LED placed a link-length away (a glint or another LED)
    placed = (st == "single_view") | (st == "estimated")
    if placed.any():
        e2 = np.linalg.norm(tf(P[placed]) - tree.positions[placed], axis=1)
        assert np.median(e2) < 2 * tree.link


def test_coincident_positions_keep_the_led_that_fits_its_chain():
    """Two LEDs can't be at one spot: the one whose neighbours disagree loses that
    position (the case of two LEDs overlapping in the picture during a walk)."""
    from types import SimpleNamespace

    from treelights.reconstruction.solve import _drop_coincident

    xs = {i: np.array([10.0 * i, 0.0, 0.0]) for i in range(10)}
    hyps = {i: [SimpleNamespace(X=x, n=5)] for i, x in xs.items()}
    # LED 7's best position is LED 5's spot (seen under the wrong identity); its second
    # hypothesis is the right one.
    hyps[7] = [SimpleNamespace(X=xs[5] + 0.3, n=6), SimpleNamespace(X=xs[7], n=3)]
    dropped = _drop_coincident(hyps)
    assert dropped == [7]
    assert np.allclose(hyps[7][0].X, xs[7]) and len(hyps[5]) == 1
    # Neighbouring LEDs at their normal spacing are left alone.
    hyps2 = {i: [SimpleNamespace(X=x, n=5)] for i, x in xs.items()}
    assert _drop_coincident(hyps2) == []
