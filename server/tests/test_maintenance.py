"""Map lifecycle checks against the controller (architecture §11.4)."""

import concurrent.futures

from treelights.app import AppState
from treelights.wled.client import WledConfigView, WledInfo


def _info(mac="aa:bb:cc:00:11:22", n=50):
    return WledInfo.parse({"ver": "0.14.4", "mac": mac.replace(":", ""), "leds": {"count": n}})


def _cfg(start=0, n=50):
    return WledConfigView.parse({"hw": {"led": {"ins": [{"start": start, "len": n, "pin": [16]}]}}})


def test_active_map_checked_against_controller(tmp_path):
    state = AppState(tmp_path, executor=concurrent.futures.ThreadPoolExecutor(1))
    try:
        state.link.info, state.link.cfg = _info(), _cfg()
        assert state.map_check() is None  # no active map yet
        m = {"id": "m1", "scan": "s", "leds": [{"status": "measured", "p": [0, 0, 0], "sigma": 0.1}] * 50,
             "wled": {"mac": state.link.info.mac, "led_count": 50, "config_hash": state.fingerprint()}}
        state.maps.activate(state.maps.save(m))
        assert state.map_check() is None  # matches
        state.link.cfg = _cfg(start=5)  # output wiring changed in WLED
        assert "configuration" in state.map_check()
        state.link.info, state.link.cfg = _info(n=60), _cfg(n=60)
        assert "60" in state.map_check()  # LED count changed
        state.link.info = _info(mac="ff:ff:ff:00:00:01")
        assert "another WLED controller" in state.map_check()
    finally:
        state.output.stop()


async def test_brightness_boost_is_saved_restored_and_recovered(tmp_path):
    from treelights.sim.virtual_wled import VirtualWled
    from treelights.wled.brightness import BrightnessGuard
    from treelights.wled.link import WledLink

    wled = VirtualWled(30)
    await wled.start()
    link = WledLink("127.0.0.1", None, port=wled.http_port)
    try:
        await link.refresh(with_cfg=True)
        wled.state["bri"] = 40
        g = BrightnessGuard(tmp_path, link)
        assert await g.boost() and wled.state["bri"] == 255 and g.boosted
        assert not await g.boost()  # already at full; the saved original stays 40
        # The server stops here; a new instance (next start-up) restores the original.
        g2 = BrightnessGuard(tmp_path, link)
        assert g2.boosted and await g2.restore()
        assert wled.state["bri"] == 40 and not g2.boosted
        assert not await g2.restore()  # nothing left to restore
        # WLED set to force max brightness for realtime data: nothing to raise.
        link.cfg.live = {"maxbri": True}
        assert not await g2.boost() and wled.state["bri"] == 40
        # A saved state from another controller is not applied to this one.
        link.cfg.live = {}
        assert await g2.boost()
        import json
        saved = json.loads(g2.path.read_text())
        g2.path.write_text(json.dumps(dict(saved, mac="ff:ff:ff:ff:ff:ff")))
        assert not await g2.restore() and wled.state["bri"] == 255 and not g2.boosted
    finally:
        await link.close()
        await wled.stop()


async def test_dim_leds_raise_wled_brightness_during_a_view_and_restore_it(tmp_path):
    """§7.3 step 3: LEDs too dim even at full level -> master brightness raised for the
    view (original saved first), colors and levels re-checked, then restored."""
    from treelights.sim.harness import make_sim
    from treelights.wled.brightness import BrightnessGuard
    from treelights.wled.link import WledLink

    sim = await make_sim(tmp_path / "data", n=60, seed=2)
    sim.scene.led_gain *= 0.03  # dim LEDs for this camera
    sim.wled.apply_bri = True
    sim.wled.state["bri"] = 64
    link = WledLink("127.0.0.1", None, port=sim.wled.http_port)
    try:
        await link.refresh(with_cfg=True)
        guard = BrightnessGuard(tmp_path, link)
        seen_bri = []

        async def boost():
            ok = await guard.boost()
            seen_bri.append(sim.wled.state["bri"])
            return ok

        sim.session.boost_brightness, sim.session.restore_brightness = boost, guard.restore
        vid = await sim.run_view(0.0, "hybrid")
        assert sim.scan.view(vid)["status"] == "done"
        events = [e["type"] for e in sim.scan.events.read()]
        assert "wled_brightness_raised" in events
        assert seen_bri == [255]  # raised during the view
        assert sim.wled.state["bri"] == 64 and not guard.boosted  # and restored after it
        assert (sim.session.cal.levels < 255).all()  # re-checked at the higher brightness
        measured = {int(r["led"]) for r in sim.scan.valid_records() if r.get("candidates")}
        assert len(measured) > 20
    finally:
        await link.close()
        await sim.close()


def test_recovery_after_restart(tmp_path):
    """§7.6: captures running when the server stopped. A walk is ended and the phone,
    reconnecting, is told to upload; a tripod view is reopened to be continued."""
    from fastapi.testclient import TestClient

    from treelights.app import create_app
    from treelights.capture.store import ScanStore

    store = ScanStore(tmp_path)
    tripod = store.create(30)
    v_done = tripod.add_view({"mode": "hybrid"})
    tripod.set_view(v_done, status="done")
    v_cut = tripod.add_view({"mode": "hybrid"})  # status "capturing" when the server died
    walk = store.create(30)
    w = walk.add_view({"mode": "walk", "status": "walking"})

    state = AppState(tmp_path, executor=concurrent.futures.ThreadPoolExecutor(1),
                     capture_executor=concurrent.futures.ThreadPoolExecutor(1))
    try:
        with TestClient(create_app(state)) as c:  # start-up runs the recovery
            assert state.session is not None and state.session.scan.id == tripod.id
            assert state.session.interrupted_view == v_cut
            assert "interrupted" in c.get("/api/state").json()["session"]["message"]
            assert store.get(walk.id).view(w)["status"] == "awaiting_upload"
            with c.websocket_connect("/ws/walk") as ws:
                ws.send_json({"type": "hello", "role": "walk", "scan": walk.id, "view": w})
                assert ws.receive_json()["type"] == "welcome"
                m = ws.receive_json()
                assert m["type"] == "stopped" and m["reason"] == "server restarted"
                assert m["process"] == f"/api/scans/{walk.id}/walk/{w}/process"
            assert state.walk is None  # no new walk was started
    finally:
        state.output.stop()


async def test_continuing_an_interrupted_view_keeps_its_captures(tmp_path):
    import asyncio

    from treelights.sim.harness import make_sim

    sim = await make_sim(tmp_path, n=50, seed=3)
    try:
        sim.scene.add_view(0.0)
        sim.phone.view_index = 0
        vid = sim.session.start_view("sequential")

        def seq_records():
            segs = set(sim.scan.view(vid)["segments"])
            return [r for r in sim.scan.detections.read() if r.get("segment") in segs and r.get("kind") == "seq"]

        while len(seq_records()) < 15:  # interrupted partway through the view
            await asyncio.sleep(0.2)
        await sim.session.stop()
        before = {int(r["led"]) for r in seq_records() if r.get("candidates")}
        n_before = len(seq_records())
        sim.session.start_view("sequential", view_id=vid)  # what "start view" does after a restart
        await sim.session.wait_done()
        assert sim.scan.view(vid)["status"] == "done"
        recs = seq_records()
        later = recs[n_before:]
        # Continuing re-checks the scene in a new segment, then captures only what was
        # missing. (The retry pass may capture ambiguous LEDs again, by design: §7.3 step 8.)
        def ambiguous(r):
            c = r.get("candidates") or []
            return len(c) >= 2 and c[1]["score"] >= 0.6 * c[0]["score"]

        amb = {int(r["led"]) for r in recs if ambiguous(r)}
        again = before & {int(r["led"]) for r in later}
        assert again <= amb, (again - amb)
        assert {int(r["led"]) for r in recs if r.get("candidates")} >= before
        assert len({int(r["led"]) for r in recs}) == 50
    finally:
        await sim.close()
