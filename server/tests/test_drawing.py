"""Drawing from the phone: the brush in 3D and on the unwrapped surface, the drawing as an effect
under the output lease (captures first, brightness, power cap), saved drawings, the API and /ws/draw."""

import asyncio
import concurrent.futures
import time

import httpx
import numpy as np
import pytest

from treelights.app import AppState, create_app
from treelights.drawing import NoMap
from treelights.effects import LIBRARY, DrawingEffect, TreeGeometry, make_renderer
from treelights.effects.drawing import BrushSpace, Canvas, clamp_radius, parse_color, parse_points
from treelights.lights import LightsBusy
from treelights.output.manager import PowerLimiter

NO_POS = 3  # the LED without a position in _cone_map


def _cone_map(n, seed=0):
    """LEDs scattered through a cone 100 high, 40 wide at the bottom; LED 3 has no position."""
    rng = np.random.default_rng(seed)
    h = rng.uniform(0, 100, n)
    h[0], h[1] = 0.0, 100.0
    th = rng.uniform(0, 2 * np.pi, n)
    r = 40 * (1 - h / 110) * rng.uniform(0.6, 1.0, n)
    leds = [{"i": i, "p": [float(r[i] * np.cos(th[i])), float(h[i]), float(r[i] * np.sin(th[i]))],
             "status": "measured", "segments": 2, "err_px": 0.5, "sigma": 0.5} for i in range(n)]
    leds[NO_POS]["p"], leds[NO_POS]["status"] = None, "unknown"
    return {"id": "m1", "scan": "s1", "units": "cm", "leds": leds, "surface": {"h": [0, 50, 100], "r": [40, 22, 4]},
            "chain": {"base_link": 10, "long_links": [], "breaks": [], "period": None, "trusted_links": n - 1},
            "quality": {"activation_blocked": None, "warnings": []}, "wled": {}}


def _seg_dist(p, a, b):
    d = b - a
    t = np.clip(((p - a) @ d) / (d @ d), 0, 1) if d @ d > 0 else np.zeros(len(p))
    return np.linalg.norm(p - (a + t[:, None] * d), axis=1)


# ---- the brush ------------------------------------------------------------------------------------

def test_brush_3d_paints_leds_within_the_radius_of_the_stroke():
    tree = TreeGeometry.from_map(_cone_map(600))
    b = BrushSpace(tree)
    assert b.span == pytest.approx(100.0)
    a, c = tree.pos[10], tree.pos[20]
    mask = b.mask(np.array([a, c]), 0.08, "3d")
    want = tree.has_pos & (_seg_dist(tree.pos, a, c) <= 8.0)  # radius: a fraction of the tree's height
    assert np.array_equal(mask, want)
    assert mask[10] and mask[20] and not mask[NO_POS]
    # The segment between the points is painted too, not only around them: no gaps in a fast stroke.
    mid = tree.has_pos & (np.linalg.norm(tree.pos - (a + c) / 2, axis=1) < 4.0)
    assert mid.any() and mask[mid].all()
    # One point is a dab.
    dab = b.mask(np.array([a]), 0.05, "3d")
    assert np.array_equal(dab, tree.has_pos & (np.linalg.norm(tree.pos - a, axis=1) <= 5.0))
    # A brush right at the LED without a position never paints it.
    assert not b.mask(np.zeros((1, 3)), 0.5, "3d")[NO_POS]


def test_brush_on_the_surface_is_round_and_crosses_the_seam():
    tree = TreeGeometry.from_map(_cone_map(1500, seed=2))
    b = BrushSpace(tree)
    radius = 0.06
    # A short stroke across u = 0 at mid height.
    mask = b.mask(np.array([[0.99, 0.5], [0.01, 0.5]]), radius, "uv")
    assert (mask & (b.u < 0.1)).any() and (mask & (b.u > 0.9)).any()
    wrap = (b.u + 0.5) % 1.0 - 0.5
    arc = np.abs(wrap) * b.circ  # distance around the tree from the seam, in tree heights
    # Everything painted is near the stroke, measured along the surface...
    assert np.all(np.abs(b.v[mask] - 0.5) <= radius + 1e-9)
    assert np.all(arc[mask] <= radius + 0.01 * b.circ[mask] + 1e-9)
    # ...and everything close to its middle is painted.
    near = tree.has_pos & (np.hypot(arc, b.v - 0.5) < radius * 0.9)
    assert near.any() and mask[near].all()
    # Round on the tree: near the narrow top the same brush covers more of the angle than at the bottom.
    top = b.mask(np.array([[0.25, 0.9]]), radius, "uv")
    low = b.mask(np.array([[0.25, 0.1]]), radius, "uv")
    span_u = lambda m: np.ptp(b.u[m]) if m.sum() > 1 else 0.0  # noqa: E731
    assert span_u(top) > span_u(low)
    assert not mask[NO_POS]


def test_brush_input_is_checked():
    with pytest.raises(ValueError):
        parse_points([], "3d")
    with pytest.raises(ValueError):
        parse_points([[1, 2]], "3d")
    with pytest.raises(ValueError):
        parse_points([[1, 2, 3]], "uv")
    with pytest.raises(ValueError):
        parse_points([[0, float("nan")]], "uv")
    with pytest.raises(ValueError):
        parse_points([["a", 1, 2]], "3d")
    with pytest.raises(ValueError):
        parse_points([[0, 0]] * 1000, "uv")
    with pytest.raises(ValueError):
        parse_points([[0, 0]], "xy")
    assert parse_points([[0.1, 0.2]], "uv").shape == (1, 2)
    assert clamp_radius(0) == 0.005 and clamp_radius(9) == 0.5 and clamp_radius("0.1") == 0.1
    with pytest.raises(ValueError):
        clamp_radius("big")
    with pytest.raises(ValueError):
        parse_color("red")
    assert np.allclose(parse_color("#ff8000"), [1, 128 / 255, 0])


def test_drawing_effect_shows_the_canvas_with_brightness_and_leaves_unplaced_leds_off():
    tree = TreeGeometry.from_map(_cone_map(100))
    assert LIBRARY["drawing"] is DrawingEffect and DrawingEffect.describe()["category"] == "drawing"
    eff = DrawingEffect()
    eff.setup(tree)
    r = make_renderer(eff, 1.0)
    assert r(0.0).max() == 0  # no canvas attached: nothing
    canvas = Canvas(100)
    canvas.restore(np.tile([1.0, 0.5, 0.0], (100, 1)))
    eff.canvas = canvas
    r = make_renderer(eff, 0.5)
    f = r(0.0)
    assert f[NO_POS].max() == 0  # no position: off, as in every map effect
    assert tuple(f[0]) == (127, int(0.5 ** 2.2 * 255 * 0.5), 0)  # gamma 2.2, then brightness
    # Changes to the canvas show in the next frame: the effect reads it live.
    canvas.paint(np.arange(100) == 5, np.array([0.0, 0.0, 1.0]))
    assert tuple(r(0.1)[5]) == (0, 0, 127)
    # A canvas sized for another string is cut or padded, never an error.
    eff.canvas = Canvas(40)
    assert r(0.2).shape == (100, 3)


# ---- on the tree ------------------------------------------------------------------------------------

async def _frame_where(state, pred, timeout=3.0):
    """The next frame sent to the LEDs (after the power cap) for which pred(frame) holds."""
    got: list = []
    state.output.add_listener(got.append)
    try:
        t = time.monotonic()
        while time.monotonic() - t < timeout:
            for f in got[::-1]:
                if pred(f):
                    return f
            await asyncio.sleep(0.02)
        raise AssertionError("no matching frame")
    finally:
        state.output._listeners.remove(got.append)


async def test_drawing_owns_the_output_like_an_effect(tmp_path):
    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
    try:
        n = state.output.led_count
        d = state.drawing
        with pytest.raises(NoMap):
            d.stroke([[0, 50, 0]])
        with pytest.raises(NoMap):
            await d.show()
        m = _cone_map(n)
        state.maps.activate(state.maps.save(m), None, force=True)
        p10 = m["leds"][10]["p"]
        # An effect is running; painting replaces it with the drawing, under the same lease.
        await state.lights.turn_on(effect="twinkle")
        assert d.stroke([p10], "3d", 0.02, "#ff0000") >= 1
        await d.show_if_free()
        assert d.live() and state.effect_state()["effect"] == "drawing" and state.output.holds("test")
        st = state.lights.status()
        assert st["on"] and st["effect"] == "drawing" and st["label"] == "Drawing"
        f = await _frame_where(state, lambda f: f[10, 0] > 0)
        assert f[10, 1] == 0 and f[10, 2] == 0
        # Brightness is the effect's, set like any other effect's.
        await state.lights.set_params(brightness=0.25)
        f = await _frame_where(state, lambda f: f[10, 0] == 63)
        # The LED without a position stays off even if the picture has a color for it.
        d.canvas.rgb[NO_POS] = 1.0
        f = await _frame_where(state, lambda f: True)
        assert f[NO_POS].max() == 0
        # The power cap applies to the drawing like to every frame.
        d.fill("#ffffff")
        await state.lights.set_params(brightness=1.0)
        cap = PowerLimiter(200 + n * 1.0, 20.0, 1.0)
        state.output.power = cap
        f = await _frame_where(state, lambda f: f.max() > 0 and f.max() < 255)
        assert cap.estimate_ma(f) <= cap.max_ma + 1
        state.output.power = PowerLimiter(None)
        # Undo goes back to before the fill.
        assert d.undo()
        assert d.canvas.to_hex([10])[0] == "#ff0000" and d.canvas.to_hex([11])[0] == "#000000"

        # A scan takes the LEDs: painting still changes the picture, but it isn't shown.
        ok, _ = state.output.acquire("walk", "calibration", ttl=30)
        assert ok and not d.live()
        assert d.stroke([m["leds"][20]["p"]], "3d", 0.02, "#00ff00") >= 1
        await d.show_if_free()
        assert state.output.holder().owner == "walk"
        with pytest.raises(LightsBusy):
            await d.show()
        st = d.status()
        assert st["busy"] == "calibration" and not st["live"]
        state.output.release("walk")
        await d.show()
        assert d.live()
        f = await _frame_where(state, lambda f: f[20, 1] > 0)

        # A restart: the picture is kept, and lights that were on show it again.
        d.flush()
        state.output.stop()
        state2 = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
        try:
            assert state2.drawing.canvas.to_hex() == d.canvas.to_hex()
            await state2.lights.restore()
            assert state2.drawing.live()
            await _frame_where(state2, lambda f: f[20, 1] > 0 and f[10, 0] > 0)
            # Off: the drawing stops like any effect.
            await state2.lights.turn_off()
            assert not state2.drawing.live() and state2.output.holder() is None
        finally:
            state2.output.stop()
    finally:
        state.output.stop()


async def test_drawing_api_and_saved_drawings(tmp_path):
    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
    app = create_app(state)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            n = state.output.led_count
            assert (await http.post("/api/drawing/stroke", json={"points": [[0, 50, 0]]})).status_code == 422
            m = _cone_map(n)
            state.maps.activate(state.maps.save(m), None, force=True)
            b = BrushSpace(TreeGeometry.from_map(m))
            uv10 = [float(b.u[10]), float(b.v[10])]
            r = await http.post("/api/drawing/stroke", json={"space": "uv", "points": [uv10], "radius": 0.02,
                                                              "color": "#00ff00"})
            body = r.json()
            assert r.status_code == 200 and body["live"] and body["changed"] >= 1 and body["undo"] == 1
            g = (await http.get("/api/drawing")).json()
            assert len(g["colors"]) == n and g["colors"][10] == "#00ff00" and g["colors"][NO_POS] == "#000000"
            assert g["live"] and g["lights_on"] and g["map"] == state.maps.active_id()
            # Bad input.
            assert (await http.post("/api/drawing/stroke", json={"points": [[1, 2]]})).status_code == 422
            assert (await http.post("/api/drawing/stroke", json={"space": "xy", "points": [[1, 2]]})).status_code == 422
            assert (await http.post("/api/drawing/stroke", json={"points": [[1, 2, 3]], "color": "nope"})).status_code == 422
            assert (await http.post("/api/drawing/paint", json={})).status_code == 404
            # Saved drawings: save, clear, load back, delete.
            s = (await http.post("/api/drawings", json={"name": "  Green   dot "})).json()
            assert s["name"] == "Green dot" and [x["id"] for x in s["drawings"]] == [s["id"]]
            assert (await http.get("/api/drawings")).json()[0]["led_count"] == n
            await http.post("/api/drawing/clear", json={})
            assert set((await http.get("/api/drawing")).json()["colors"]) == {"#000000"}
            r = await http.post(f"/api/drawings/{s['id']}/load", json={})
            assert r.status_code == 200 and r.json()["live"]
            assert (await http.get("/api/drawing")).json()["colors"][10] == "#00ff00"
            assert (await http.post("/api/drawings/nope/load", json={})).status_code == 404
            assert (await http.post("/api/drawings/..%2Fdrawing/load", json={})).status_code == 404
            assert (await http.delete(f"/api/drawings/{s['id']}")).json()["drawings"] == []
            assert (await http.delete(f"/api/drawings/{s['id']}")).status_code == 404
            # Fill: every LED with a position.
            await http.post("/api/drawing/fill", json={"color": "#0000ff"})
            cols = (await http.get("/api/drawing")).json()["colors"]
            assert cols[NO_POS] == "#000000" and set(cols[:NO_POS] + cols[NO_POS + 1:]) == {"#0000ff"}
            # Undo (twice: the fill, then the load).
            await http.post("/api/drawing/undo", json={})
            await http.post("/api/drawing/undo", json={})
            assert set((await http.get("/api/drawing")).json()["colors"]) == {"#000000"}
            # A scan holds the LEDs: showing is refused (409), painting is kept but not shown.
            state.output.acquire("walk", "calibration", ttl=30)
            assert (await http.post("/api/drawing/show", json={})).status_code == 409
            r = (await http.post("/api/drawing/stroke", json={"points": [m["leds"][10]["p"]], "color": "#ffffff"})).json()
            assert r["busy"] == "calibration" and not r["live"] and r["changed"] >= 1
            state.output.release("walk")
            assert (await http.post("/api/drawing/show", json={})).json()["live"]
            # The drawing is an effect like the others: listed, and switched off by the lights.
            fx = (await http.get("/api/effects")).json()
            assert fx["current"]["effect"] == "drawing"
            assert any(e["name"] == "drawing" and e["category"] == "drawing" for e in fx["effects"])
            assert (await http.post("/api/lights", json={"on": False})).json()["on"] is False
            assert not (await http.get("/api/drawing")).json()["live"]
            # show: false paints without taking the LEDs.
            await http.post("/api/drawing/stroke", json={"points": [m["leds"][10]["p"]], "show": False})
            assert not (await http.get("/api/drawing")).json()["live"]
    finally:
        state.output.stop()


@pytest.mark.timeout(60)
def test_draw_websocket_streams_the_picture_to_every_page(tmp_path):
    from fastapi.testclient import TestClient

    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
    n = state.output.led_count
    m = _cone_map(n)
    state.maps.activate(state.maps.save(m), None, force=True)

    def until(ws, kind):
        for _ in range(50):
            msg = ws.receive_json()
            if msg["type"] == kind:
                return msg
        raise AssertionError(f"no {kind} message")

    try:
        with TestClient(create_app(state)) as c:
            with c.websocket_connect("/ws/draw") as a, c.websocket_connect("/ws/draw") as b:
                first = a.receive_json()
                assert first["type"] == "state" and len(first["colors"]) == n and not first["live"]
                assert b.receive_json()["type"] == "state"
                a.send_json({"type": "stroke", "space": "3d", "points": [m["leds"][10]["p"], m["leds"][20]["p"]],
                             "radius": 0.02, "color": "#ffffff", "start": True})
                ua, ub = until(a, "update"), until(b, "update")
                assert 10 in ua["leds"] and 20 in ua["leds"] and ua == ub
                assert ua["colors"][ua["leds"].index(10)] == "#ffffff"
                assert until(a, "status")["live"]  # painting put the drawing on the tree
                assert until(b, "status")["live"]  # the other page hears it too
                assert state.drawing.live() and state.output.holds("test")
                # Mistakes are reported, and the connection stays usable.
                a.send_json({"type": "stroke", "points": []})
                assert "point" in until(a, "error")["message"]
                a.send_text("not json")
                until(a, "error")
                a.send_json({"type": "undo"})
                cv = until(b, "canvas")
                assert cv["colors"][10] == "#000000" and cv["undo"] == 0
    finally:
        state.output.stop()
