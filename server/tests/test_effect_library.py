import asyncio
import concurrent.futures
import socket

import httpx
import numpy as np
import pytest
import uvicorn

from treelights.app import AppState, create_app
from treelights.effects import LIBRARY, PALETTE_IDS, Params, PaletteBank, TreeGeometry, catalogue, library, make_renderer
from treelights.effects.palettes import GRADIENTS
from treelights.sim.tree import make_tree


def tree_map(n=300, seed=1):
    t = make_tree(n=n, seed=seed)
    P = t.positions
    hs = np.linspace(P[:, 1].min(), P[:, 1].max(), 8)
    r = np.hypot(P[:, 0], P[:, 2])
    band = hs[1] - hs[0]
    rs = [float(np.percentile(r[np.abs(P[:, 1] - h) <= band], 95)) for h in hs]
    leds = [{"i": i, "p": [float(v) for v in P[i]], "status": "measured", "segments": 2, "err_px": 0.5, "sigma": 0.5}
            for i in range(n)]
    leds[7]["p"], leds[7]["status"] = None, "unknown"
    return {"id": "m1", "scan": "s1", "units": "cm", "leds": leds,
            "chain": {"base_link": 10, "long_links": [], "breaks": [], "period": None, "trusted_links": n - 1}, "surface": {"h": hs.tolist(), "r": rs},
            "quality": {"activation_blocked": None, "warnings": []}, "wled": {"config_hash": "sha256:a"}}


@pytest.mark.parametrize("name", list(LIBRARY))
def test_every_library_effect_renders_and_reacts_to_params(name):
    tree = TreeGeometry.from_map(tree_map())
    eff = LIBRARY[name]()
    eff.setup(tree)
    r = make_renderer(eff, 1.0)
    frames = [r(k / 40) for k in range(200)]
    f = frames[-1]
    assert f.shape == (tree.n, 3) and f.dtype == np.uint8
    if eff.needs_map:
        assert all(fr[7].max() == 0 for fr in frames)  # no position: always off
    assert any(fr.max() > 0 for fr in frames), "effect never lights anything"
    # Every slider extreme and every palette keeps the output finite and in range.
    for v in (0, 255):
        eff.set_params({k: v for k in ("speed", "intensity", "c1", "c2", "c3")})
        buf = np.zeros((tree.n, 3))
        eff.frame(5.0, buf)
        assert np.isfinite(buf).all() and buf.min() >= -1e-9 and buf.max() <= 1 + 1e-9
    for p in PALETTE_IDS:
        eff.set_params({"palette": p})
        r(3.0)


def test_effects_change_with_palette_and_colors():
    tree = TreeGeometry.from_map(tree_map())
    eff = LIBRARY["stripes"]({"palette": "colors12", "colors": ["#ff0000", "#0000ff", "#000000"], "speed": 0})
    eff.setup(tree)
    out = np.zeros((tree.n, 3))
    eff.frame(0, out)
    lit = tree.has_pos
    # Only the two chosen colors (up to blending at stripe edges).
    assert np.all(out[lit, 1] < 0.05)
    assert (out[lit, 0] > 0.9).any() and (out[lit, 2] > 0.9).any()
    eff.set_params({"colors": ["#00ff00"]})
    eff.frame(0.1, out)
    assert (out[lit, 1] > 0.9).any() and np.all(out[lit, 0] < 0.05)


def test_surface_depth_fade_dims_inner_leds():
    tree = TreeGeometry.from_map(tree_map())
    eff = LIBRARY["stripes"]({"palette": "color1", "colors": ["#ffffff"], "c3": 255})
    eff.setup(tree)
    out = np.zeros((tree.n, 3))
    eff.frame(0, out)
    inner = tree.has_pos & (tree.depth < 0.8)
    outer = tree.has_pos & (tree.depth > 1.0)
    assert inner.any() and outer.any()
    assert out[inner].max() == 0 and out[outer].min() > 0.99


def test_speed_change_does_not_jump_time():
    tree = TreeGeometry.from_map(tree_map())
    eff = LIBRARY["plasma"]()
    eff.setup(tree)
    out = np.zeros((tree.n, 3))
    for k in range(40):
        eff.frame(k / 40, out)
    before = eff.T
    eff.set_params({"speed": 255})
    eff.frame(1.0, out)
    assert abs(eff.T - before) < 0.3


def test_params_parsing_and_palettes():
    p = Params().merged({"speed": 999, "intensity": -3, "colors": ["#0f0", [1, 2, 3]], "palette": "ocean"})
    assert p.speed == 255 and p.intensity == 0 and p.colors[0] == (0, 255, 0) and p.colors[1] == (1, 2, 3)
    assert p.colors[2] == (0, 0, 0) and p.to_dict()["colors"][0] == "#00ff00"
    with pytest.raises(ValueError):
        Params().merged({"colors": ["nope"]})
    bank = PaletteBank(seed=1)
    for gid in GRADIENTS:
        lut = bank.lut(gid, Params().colors)
        assert lut.shape == (256, 3) and lut.min() >= 0 and lut.max() <= 1
    # A hard-edged palette has no in-between colors away from its edges.
    c = bank.sample("candy_cane", Params().colors, 0, np.array([0.1, 0.4, 0.6, 0.9]))
    assert np.allclose(c, [[1, 0, 0], [1, 0, 0], [1, 1, 1], [1, 1, 1]])
    # Random Cycle blends to a new palette over time.
    a = bank.lut("random", Params().colors, 0.0).copy()
    b = bank.lut("random", Params().colors, 12.0)
    assert not np.allclose(a, b)
    ids = {p["id"] for p in catalogue()}
    assert ids == PALETTE_IDS and "default" in ids
    cats = {e["category"] for e in library()}
    assert cats == {"3d", "surface", "gift", "basic"}


@pytest.mark.timeout(60)
async def test_effects_api_start_live_update_and_stop(tmp_path):
    pool = concurrent.futures.ThreadPoolExecutor(2)
    state = AppState(tmp_path / "data", executor=pool)
    app = create_app(state)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as http:
            n = 300
            await http.put("/api/config", json={"wled": {"led_count": n}})
            cat = (await http.get("/api/effects")).json()
            assert cat["current"] is None and len(cat["effects"]) == len(LIBRARY)
            # Without a map, an effect that needs one falls back to the index chase.
            r = (await http.post("/api/test/start", json={"effect": "helix", "brightness": 0.5})).json()
            assert r["effect"] == "chase"
            assert (await http.post("/api/effects/params", json={"params": {"speed": 10}})).status_code == 409
            mid = state.maps.save(tree_map(n))
            state.maps.activate(mid, force=True)
            r = (await http.post("/api/test/start", json={
                "effect": "helix", "brightness": 0.5, "params": {"palette": "ocean", "intensity": 200}})).json()
            assert r["effect"] == "helix"
            cur = (await http.get("/api/effects")).json()["current"]
            assert cur["effect"] == "helix" and cur["params"]["palette"] == "ocean" and cur["params"]["intensity"] == 200
            u = (await http.post("/api/effects/params", json={
                "params": {"palette": "colors12", "colors": ["#ff0000", "#00ff00"], "c1": 3}, "brightness": 0.8})).json()
            assert u["current"]["params"]["c1"] == 3 and u["current"]["brightness"] == 0.8
            assert u["current"]["params"]["colors"][:2] == ["#ff0000", "#00ff00"]
            bad = await http.post("/api/effects/params", json={"params": {"palette": "nope"}})
            assert bad.status_code == 422
            await asyncio.sleep(0.2)
            assert state.output.current_frame().max() > 0
            await http.post("/api/test/stop", json={})
            assert (await http.get("/api/effects")).json()["current"] is None
    finally:
        server.should_exit = True
        await task
        state.close() if hasattr(state, "close") else None
        pool.shutdown()


def test_bouncing_balls_fall_bounce_lower_and_stay_inside():
    from treelights.effects.gift import BouncingBalls

    tree = TreeGeometry.from_map(tree_map())
    eff = BouncingBalls({"intensity": 0})  # one ball
    eff.setup(tree)
    out = np.zeros((tree.n, 3))
    heights, vys = [], []
    for k in range(240):
        eff.frame(k / 40, out)
        if len(eff.balls):
            heights.append(eff.balls[0, 1])
            vys.append(eff.balls[0, 4])
    h = np.array(heights)
    # Apexes after each bounce (vy changes sign from + to -) get lower.
    apex = [h[i] for i in range(1, len(vys)) if vys[i - 1] > 0 >= vys[i]]
    assert len(apex) >= 2 and all(b < a for a, b in zip(apex, apex[1:]))
    assert h.min() >= 0 and h[0] > 0.8


def test_morse_spells_merry_xmas():
    from treelights.effects.gift import MORSE, morse_timeline

    runs = morse_timeline("MERRY XMAS")
    letters: dict[int, str] = {}
    for on, length, li in runs:
        if on:
            letters[li] = letters.get(li, "") + ("." if length == 1 else "-")
    decode = {v: k for k, v in MORSE.items()}
    assert "".join(decode[letters[i]] for i in sorted(letters)) == "MERRYXMAS"
    assert any((not on) and length == 7 for on, length, _ in runs)  # the word gap
