"""fill.solve_gap in Rust (native/src/fill.rs) against the scipy version it replaces."""

import numpy as np
import pytest

from treelights import native
from treelights.reconstruction import fill

pytestmark = pytest.mark.skipif(not native.available("solve_gap"), reason="native module not installed")


def _gap(m: int, seed: int, tight: float):
    """A gap of m LEDs between a and b along a bent path, links `tight` times the spacing
    of the path (below 1: the path is too long and links pull), a few single-view rays."""
    rng = np.random.default_rng(seed)
    s = np.linspace(0, 1, m + 2)
    curve = np.stack([np.cos(3 * s) * 40, 150 * s, np.sin(3 * s) * 40], axis=1) + rng.normal(0, 2, (m + 2, 3))
    a, b, path = curve[0], curve[-1], curve[1:-1]
    spacing = np.linalg.norm(np.diff(curve, axis=0), axis=1)
    links = list(spacing * tight * rng.uniform(0.9, 1.1, len(spacing)))
    rays = {}
    for k in rng.choice(m, size=max(1, m // 8), replace=False):
        C = np.array([0.0, 80.0, 300.0])
        d = path[k] + rng.normal(0, 3, 3) - C
        rays[int(k)] = (C, d / np.linalg.norm(d))
    return a, b, links, path, rays, 180.0


def _cost(a, b, links, path, rays, s, P):
    chain = [a] + list(P) + [b]
    c = sum((max(0.0, np.linalg.norm(chain[k + 1] - chain[k]) - links[k]) / (0.01 * s)) ** 2 for k in range(len(chain) - 1))
    for k, (C, dv) in rays.items():
        v = P[k] - C
        c += np.sum(((v - (v @ dv) * dv) / (0.005 * s)) ** 2)
    return c + np.sum(((P - path) / (0.3 * s)) ** 2)


@pytest.mark.parametrize("m,tight,seed", [(5, 0.8, 0), (30, 0.7, 1), (40, 1.2, 2), (60, 0.6, 3)])
def test_solve_gap_matches_scipy(monkeypatch, m, tight, seed):
    g = _gap(m, seed, tight)
    Pn = fill.solve_gap(*g)
    monkeypatch.setattr(native, "lib", None)
    Pp = fill.solve_gap(*g)
    cn, cp = _cost(*g, Pn), _cost(*g, Pp)
    # The problem is convex, with one minimum. scipy stops after 200 evaluations, often short
    # of it (links switching between slack and taut make the last part slow); the native
    # solver runs on to the minimum, so its cost is never higher.
    assert cn <= cp * (1 + 1e-9) + 1e-9
    # Where scipy did converge, the positions are the same (the minimum is unique).
    if cp - cn <= 1e-6 * max(cp, 1.0):
        assert np.abs(Pn - Pp).max() < 1e-3 * g[5]


@pytest.mark.parametrize("m,tight,seed", [(30, 0.7, 1), (40, 1.2, 2)])
def test_solve_gap_reaches_the_minimum(m, tight, seed):
    """Against a long plain Levenberg run (2000 iterations) of the same problem."""
    g = _gap(m, seed, tight)
    ref = {(30, 0.7, 1): 10.75838, (40, 1.2, 2): 1.65413}[(m, tight, seed)]  # from that run
    assert _cost(*g, fill.solve_gap(*g)) <= ref * (1 + 1e-5)


def test_solve_gap_is_deterministic():
    g = _gap(50, 4, 0.7)
    assert np.array_equal(fill.solve_gap(*g), fill.solve_gap(*g))
