"""Several WLED controllers per tree and several trees (architecture §5.5): the global
index space and its slices, DDP slicing and send order, per-controller power caps, chain
breaks at controller boundaries, the WLED fingerprint of a tree, the migration of a
single-tree install, per-tree leases, and the tree and controller API."""

import asyncio
import concurrent.futures
import json
import socket
import time

import httpx
import numpy as np
import pytest

from treelights import storage
from treelights.app import AppState, create_app
from treelights.config import ConfigStore, ServerSettings, find_tree, migrate, tree_dir
from treelights.lights import LightsBusy
from treelights.output.manager import OutputManager, PowerLimiter
from treelights.reconstruction import chain
from treelights.sim.virtual_wled import VirtualWled, VirtualWledGroup
from treelights.wled import ddp
from treelights.wled.client import WledConfigView, WledInfo, config_fingerprint
from treelights.wled.group import IndexMap, MultiSender, SlicedPower, tree_fingerprint


# ---- index mapping -------------------------------------------------------------------------
def test_index_map_global_and_local_indices():
    m = IndexMap([600, 600])
    assert m.total == 1200 and m.offsets == [0, 600]
    assert m.to_local(0) == (0, 0) and m.to_local(599) == (0, 599)
    assert m.to_local(600) == (1, 0) and m.to_local(1199) == (1, 599)
    assert m.to_global(1, 0) == 600 and m.to_global(0, 599) == 599
    for i in range(0, 1200, 37):
        assert m.to_global(*m.to_local(i)) == i
    assert m.boundaries() == [599]  # LED 599 -> 600 has no wire
    m3 = IndexMap([100, 50, 30])
    assert m3.boundaries() == [99, 149]
    assert [(s.controller, s.start, s.count) for s in m3.slices()] == [(0, 0, 100), (1, 100, 50), (2, 150, 30)]
    # A controller's own GPIO-output breaks, shifted into the global index space.
    assert m3.shift_breaks(1, [24, 49, 70]) == [124]
    with pytest.raises(IndexError):
        m3.to_local(180)
    with pytest.raises(IndexError):
        m3.to_global(1, 50)
    with pytest.raises(ValueError):
        IndexMap([10, 0])
    single = IndexMap([600])
    assert single.boundaries() == [] and single.to_local(599) == (0, 599)


class LoggingSender:
    """Records packets in the order the output thread sends them, across controllers."""

    def __init__(self, k, log, protocol="ddp"):
        self.k, self.log, self.protocol = k, log, protocol
        self.packets_sent = 0

    def encode(self, rgb):
        return ddp.encode_frame(rgb) if self.protocol == "ddp" else ddp.encode_dnrgb(rgb)

    def send_raw(self, p):
        self.log.append((self.k, p))
        self.packets_sent += 1

    def close(self):
        pass


def test_multisender_slices_frames_and_sends_the_push_packets_together():
    index = IndexMap([600, 200, 100])
    log: list = []
    sender = MultiSender(index, [LoggingSender(k, log) for k in range(3)])
    rgb = (np.arange(900 * 3) % 251).astype(np.uint8).reshape(900, 3)
    sender.send(rgb, repeats=2)
    # Each controller gets exactly its slice, at local offsets starting from 0.
    for k, sl in enumerate(index.slices()):
        got = np.zeros((sl.count, 3), np.uint8)
        for kk, p in log[: len(log) // 2]:
            if kk == k:
                _flags, off, data, _push = ddp.decode_packet(p)
                px = np.frombuffer(data, np.uint8).reshape(-1, 3)
                got[off // 3: off // 3 + len(px)] = px
        assert np.array_equal(got, rgb[sl.start: sl.stop])
    # Per repeat: every data packet first, then one PUSH packet per controller, back to back.
    for rep in (log[: len(log) // 2], log[len(log) // 2:]):
        push = [ddp.decode_packet(p)[3] for _k, p in rep]
        assert push == [False] + [True] * 3  # 600 LEDs: 2 packets; 200 and 100: 1 each
        assert sorted(k for (k, _p), ps in zip(rep, push) if ps) == [0, 1, 2]
    # Controllers without an address are skipped; the rest still get their slices.
    log.clear()
    MultiSender(index, [LoggingSender(0, log), None, LoggingSender(2, log)]).send(rgb)
    assert sorted({k for k, _p in log}) == [0, 2]


def test_multisender_single_controller_matches_one_sender():
    """One controller: the same packets, in the same order, as before there were several."""
    index = IndexMap([600])
    log: list = []
    MultiSender(index, [LoggingSender(0, log)]).send(np.full((600, 3), 7, np.uint8))
    assert [p for _k, p in log] == ddp.encode_frame(np.full((600, 3), 7, np.uint8))


def test_multisender_over_udp_reaches_each_virtual_controller():
    async def run():
        group = VirtualWledGroup([30, 20], seed=1)
        for w in group:
            w.faults.delay_ms = (0.0, 0.0)
        await group.start()
        try:
            index = IndexMap([30, 20])
            sender = MultiSender(index, [ddp.UdpFrameSender("127.0.0.1", port=w.udp_port) for w in group])
            rgb = np.zeros((50, 3), np.uint8)
            rgb[29] = (255, 0, 0)  # last LED of controller 1
            rgb[30] = (0, 0, 255)  # first LED of controller 2
            sender.send(rgb)
            await asyncio.sleep(0.2)
            shown = group.state_at(time.monotonic())
            assert np.array_equal(shown, rgb)
            assert group[0].state_at(time.monotonic())[29].tolist() == [255, 0, 0]
            assert group[1].state_at(time.monotonic())[0].tolist() == [0, 0, 255]
            sender.close()
        finally:
            await group.stop()

    asyncio.run(run())


def test_power_cap_applies_per_controller():
    index = IndexMap([100, 100])
    # Controller 1 has a small supply, controller 2 none set.
    power = SlicedPower(index, [PowerLimiter(1000, 20, 1), PowerLimiter(None, 20, 1)])
    full = np.full((200, 3), 255, np.uint8)
    out = power.apply(full)
    assert PowerLimiter(1000, 20, 1).estimate_ma(out[:100]) <= 1000 + 1e-6
    assert np.array_equal(out[100:], full[100:])  # untouched: its own limit isn't reached
    assert power.max_ma is None
    both = SlicedPower(index, [PowerLimiter(1000, 20, 1), PowerLimiter(1500, 20, 1)])
    assert both.max_ma == 2500
    out = both.apply(full)
    assert PowerLimiter(1500, 20, 1).estimate_ma(out[100:]) <= 1500 + 1e-6
    assert out[100:].astype(int).sum() > out[:100].astype(int).sum()


def test_output_thread_sends_every_slice_of_each_frame():
    index = IndexMap([40, 60])
    log: list = []
    out = OutputManager(100)
    out.set_sender(MultiSender(index, [LoggingSender(0, log), LoggingSender(1, log)]))
    try:
        assert out.acquire("x", "calibration")[0]
        frame = np.zeros((100, 3), np.uint8)
        frame[45] = (0, 9, 0)
        out.show("x", frame, repeats=2).result(timeout=2)
        ks = [k for k, _p in log[:4]]
        assert ks == [0, 1, 0, 1]  # both controllers, twice, each frame complete
        _f, off, data, push = ddp.decode_packet(log[1][1])
        assert push and np.frombuffer(data, np.uint8).reshape(-1, 3)[5].tolist() == [0, 9, 0]
    finally:
        out.stop()


# ---- chain breaks and fingerprint ------------------------------------------------------------------
def test_controller_boundary_is_a_chain_break():
    n = 60
    pos = {i: np.array([i * 1.0, 0, 0]) for i in range(n)}
    pos.update({i: np.array([i * 1.0, 50.0, 0]) for i in range(30, n)})  # string 2 starts far away
    b = chain.learn_bounds(n, pos, {i: 0.01 for i in range(n)}, set(range(n)), 100.0,
                           output_breaks=[29], controller_breaks=[29])
    assert b.is_break(29) and b.kind[29] == "break_controller"
    assert not b.is_break(28) and not b.is_break(30)
    # Without knowing about the boundary, the 50 cm jump would be a data break at best.
    b2 = chain.learn_bounds(n, pos, {i: 0.01 for i in range(n)}, set(range(n)), 100.0)
    assert b2.kind[29] != "break_controller"


def _info(mac="aa:bb:cc:00:00:01", n=50):
    return WledInfo.parse({"ver": "0.15.0", "mac": mac, "leds": {"count": n}})


def _cfg(n=50, start=0):
    return WledConfigView.parse({"hw": {"led": {"ins": [{"start": start, "len": n}]}}})


def test_tree_fingerprint_covers_every_controller():
    a = (_info("aa:00:00:00:00:01", 30), _cfg(30))
    b = (_info("aa:00:00:00:00:02", 20), _cfg(20))
    assert tree_fingerprint([a]) == config_fingerprint(*a)  # one controller: unchanged
    fp = tree_fingerprint([a, b])
    assert fp.startswith("sha256:") and fp != config_fingerprint(*a)
    assert tree_fingerprint([b, a]) != fp  # swapped order: indices address other LEDs
    assert tree_fingerprint([a, (_info("aa:00:00:00:00:02", 21), _cfg(21))]) != fp
    assert tree_fingerprint([a, (b[0], _cfg(20, start=2))]) != fp
    assert tree_fingerprint([a, (None, None)]) is None


# ---- configuration migration ---------------------------------------------------------------------
LEGACY = {"tree_height_cm": 150.0, "pin": None, "raw_frame_retention_scans": 3,
          "wled": {"host": "10.0.0.5", "mac": "aabbccddeeff", "led_count": 600},
          "power": {"max_current_ma": 4000}, "mqtt": {"enabled": False, "base_topic": "mytree"},
          "schedule": {"timezone": "Europe/Berlin", "timers": [{"action": "on", "time": "17:00"}]}}


def _legacy_install(root):
    storage.atomic_write_json(root / "config.json", LEGACY)
    storage.atomic_write_json(root / "scans" / "s1" / "scan.json", {"id": "s1", "led_count": 600, "views": [], "segments": {}})
    storage.atomic_write_json(root / "maps" / "m1" / "map.json", {"id": "m1", "scan": "s1", "leds": []})
    storage.atomic_write_json(root / "maps" / "active.json", {"id": "m1", "previous": None})
    storage.atomic_write_json(root / "lights.json", {"on": True, "effect": "solid"})
    storage.atomic_write_json(root / "wled_restore.json", {"mac": "aabbccddeeff", "bri": 80})


def test_migration_moves_a_single_tree_install_into_tree1(tmp_path):
    _legacy_install(tmp_path)
    cfg = ConfigStore(tmp_path)
    t1 = tree_dir(tmp_path, "tree1")
    for name in ("scans/s1/scan.json", "maps/m1/map.json", "maps/active.json", "lights.json", "wled_restore.json"):
        assert (t1 / name).exists(), name
        assert not (tmp_path / name).exists()
    assert storage.read_json(tmp_path / "config.v1.json") == LEGACY  # the original is kept
    raw = storage.read_json(tmp_path / "config.json")
    assert raw["format"] == 2 and raw["default_tree"] == "tree1" and len(raw["trees"]) == 1
    s = cfg.settings  # the default tree's view keeps the single-tree shape
    assert s.wled.host == "10.0.0.5" and s.wled.led_count == 600 and s.tree_height_cm == 150.0
    assert s.power.max_current_ma == 4000 and s.mqtt.base_topic == "mytree" and s.raw_frame_retention_scans == 3
    assert s.schedule.timezone == "Europe/Berlin" and s.schedule.timers[0].time == "17:00"
    assert [c.host for c in s.controllers] == ["10.0.0.5"]
    # Running it again changes nothing.
    before = (tmp_path / "config.json").read_bytes()
    assert migrate(tmp_path)["format"] == 2
    assert (tmp_path / "config.json").read_bytes() == before
    # Offline tools find the tree without migrating.
    assert find_tree(tmp_path)[0] == t1


def test_migration_resumes_after_a_crash_without_overwriting(tmp_path):
    _legacy_install(tmp_path)
    storage.atomic_write_json(tmp_path / "scans" / "s2" / "scan.json", {"id": "s2", "led_count": 600, "views": [], "segments": {}})
    # A crash after scans/s1 had moved: s1 is already in tree1, s2 isn't, config.json is still format 1.
    t1 = tree_dir(tmp_path, "tree1")
    (t1 / "scans").mkdir(parents=True)
    (tmp_path / "scans" / "s1").rename(t1 / "scans" / "s1")
    # And a lights.json that exists on both sides (kept in tree1, the original left in place).
    storage.atomic_write_json(t1 / "lights.json", {"on": False})
    ConfigStore(tmp_path)
    assert (t1 / "scans" / "s1" / "scan.json").exists() and (t1 / "scans" / "s2" / "scan.json").exists()
    assert not (tmp_path / "scans").exists()
    assert storage.read_json(t1 / "lights.json") == {"on": False}
    assert (tmp_path / "lights.json").exists()
    assert storage.read_json(tmp_path / "config.json")["format"] == 2


def test_new_install_and_legacy_app_state_keep_working(tmp_path):
    fresh = ConfigStore(tmp_path / "fresh")
    assert fresh.tree_ids() == ["tree1"] and fresh.settings.wled.led_count == 600
    _legacy_install(tmp_path / "old")
    state = AppState(tmp_path / "old", executor=concurrent.futures.ThreadPoolExecutor(1))
    try:
        assert state.maps.active_id() == "m1" and state.scans.get("s1").meta["led_count"] == 600
        assert state.output.led_count == 600 and state.link.host == "10.0.0.5"
        assert state.lights.on and state.lights.effect == "solid"
        assert state.brightness.boosted  # the saved WLED state is restored at start-up
    finally:
        for t in state.trees.values():
            t.output.stop()


def test_config_validation_rejects_shared_controllers_and_topics(tmp_path):
    cfg = ConfigStore(tmp_path)
    tid = cfg.add_tree("Garden")
    assert cfg.view(tid).mqtt.base_topic != cfg.settings.mqtt.base_topic
    cfg.update({"wled": {"host": "10.0.0.5"}})
    with pytest.raises(Exception, match="one tree only"):
        cfg.update_view(tid, {"controllers": [{"host": "10.0.0.5"}]})
    with pytest.raises(Exception, match="one tree only"):
        cfg.update_view(tid, {"controllers": [{"host": "10.0.0.6"}, {"host": "10.0.0.6"}]})
    with pytest.raises(Exception, match="own Home Assistant topic"):
        cfg.update_view(tid, {"mqtt": {"base_topic": cfg.settings.mqtt.base_topic}})
    cfg.update_view(tid, {"controllers": [{"host": "10.0.0.6", "led_count": 100}, {"host": "10.0.0.7", "led_count": 50}]})
    assert cfg.view(tid).led_count == 150 and cfg.settings.led_count == 600
    with pytest.raises(ValueError):
        cfg.remove_tree("tree1")
    cfg.remove_tree(tid)
    assert cfg.tree_ids() == ["tree1"]
    ServerSettings.model_validate(storage.read_json(tmp_path / "config.json"))


# ---- trees: independent outputs and leases --------------------------------------------------------
def _state(path, trees=2):
    state = AppState(path, executor=concurrent.futures.ThreadPoolExecutor(1))
    for _ in range(trees - 1):
        tid = state.config.add_tree()
        from treelights.tree import Tree

        state.trees[tid] = Tree(state, tid)
    return state


def _stop(state):
    for t in state.trees.values():
        t.output.stop()


async def test_leases_are_per_tree(tmp_path):
    state = _state(tmp_path)
    t1, t2 = state.trees["tree1"], state.trees["tree2"]
    try:
        assert t1.output is not t2.output
        ok, _ = t1.output.acquire("walk", "calibration", ttl=30)  # tree 1 is being scanned
        assert ok
        with pytest.raises(LightsBusy):
            await t1.lights.turn_on()
        st = await t2.lights.turn_on(effect="solid")  # tree 2 isn't blocked
        assert st["on"] and t2.output.holds("test") and t1.output.holds("walk")
        assert t1.effect_state() is None and t2.effect_state()["effect"] == "solid"
        # Two scans at once, one per tree.
        assert t2.output.acquire("walk", "calibration", ttl=30)[0]
        assert t1.busy() == "calibration" and t2.busy() == "calibration"
    finally:
        _stop(state)


async def test_tree_and_controller_api(tmp_path):
    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
    app = create_app(state)
    group = VirtualWledGroup([30, 20], seed=2)
    other = VirtualWled(40, mac="02:00:00:00:99:01")
    await group.start()
    await other.start()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            r = await http.post("/api/trees", json={"name": "Garden"})
            assert r.status_code == 200 and r.json()["id"] == "tree2" and r.json()["name"] == "Garden"
            assert [t["id"] for t in (await http.get("/api/trees")).json()] == ["tree1", "tree2"]
            assert (await http.put("/api/trees/tree2", json={"name": "Garden tree"})).json()["name"] == "Garden tree"
            assert (await http.put("/api/trees/tree2", json={"name": ""})).status_code == 422
            # Tree 1: two controllers, in index order.
            r = await http.put("/api/controllers", json=group.settings())
            assert r.status_code == 200, r.text
            ctl = r.json()
            assert [(c["start"], c["last"]) for c in ctl] == [(0, 29), (30, 49)]
            st = (await http.get("/api/state")).json()
            assert st["tree"]["id"] == "tree1" and st["led_count"] == 50 and len(st["controllers"]) == 2
            checks = (await http.get("/api/wled/checks")).json()
            assert checks[0]["name"] == "controllers" and "after LEDs 29" in checks[0]["detail"]
            for k in (0, 1):
                mine = {c["name"]: c for c in checks if c.get("controller") == k}
                assert mine["reachable"]["status"] == "ok" and mine["led_count"]["status"] == "ok"
                assert mine["realtime"]["status"] == "ok", mine["realtime"]
            # Tree 2 through the path prefix and the query parameter; a controller can't be in both trees.
            r = await http.post("/api/trees/tree2/wled/select", json={"host": "127.0.0.1", "http_port": group[0].http_port})
            assert r.status_code == 409 and "one tree only" in r.json()["detail"]
            await http.put("/api/trees/tree2/config", json={"wled": {"udp_port": other.udp_port}})
            r = await http.post("/api/trees/tree2/wled/select", json={"host": "127.0.0.1", "http_port": other.http_port})
            assert r.status_code == 200 and r.json()["led_count"] == 40
            st2 = (await http.get("/api/state", params={"tree": "tree2"})).json()
            assert st2["tree"]["name"] == "Garden tree" and st2["led_count"] == 40
            assert (await http.get("/api/config", headers={"X-TreeLights-Tree": "tree2"})).json()["wled"]["led_count"] == 40
            assert (await http.get("/api/state", params={"tree": "nope"})).status_code == 404
            # Lights per tree: tree 2 on, tree 1 off; each reported on its own.
            assert (await http.post("/api/trees/tree2/lights", json={"on": True, "effect": "solid"})).json()["on"]
            assert not (await http.get("/api/lights")).json()["on"]
            await asyncio.sleep(0.3)
            assert other.live and state.trees["tree1"].output.holder() is None
            # Adding a third controller to tree 1 with select(add).
            third = VirtualWled(10, mac="02:00:00:00:98:01")
            await third.start()
            try:
                await http.put("/api/config", json={"controllers": group.settings() + [
                    {"host": "127.0.0.1", "http_port": third.http_port, "udp_port": third.udp_port, "led_count": 5}]})
                r = await http.post("/api/wled/select", json={"host": "127.0.0.1", "http_port": third.http_port,
                                                              "controller": 2})
                assert r.status_code == 200 and r.json()["controller"] == 2
                assert state.trees["tree1"].index.counts == [30, 20, 10]
                assert state.trees["tree1"].output_breaks() == [29, 49]
            finally:
                await third.stop()
            # A tree is removed, its folder kept aside.
            assert (await http.delete("/api/trees/tree1")).status_code == 409
            assert (await http.delete("/api/trees/tree2")).json()["ok"]
            assert "tree2" not in state.trees and list((tmp_path / "data" / "trees" / ".removed").iterdir())
    finally:
        await state.stop()
        await group.stop()
        await other.stop()


def test_websockets_act_on_the_tree_they_name(tmp_path):
    from fastapi.testclient import TestClient

    cfg = ConfigStore(tmp_path)
    tid = cfg.add_tree("Second")
    cfg.update_view(tid, {"controllers": [{"led_count": 40}, {"led_count": 25}]})
    state = AppState(tmp_path, executor=concurrent.futures.ThreadPoolExecutor(1),
                     capture_executor=concurrent.futures.ThreadPoolExecutor(1))
    with TestClient(create_app(state)) as c:  # every tree in the settings is served
        assert state.trees[tid].index.counts == [40, 25]
        for url in (f"/ws/trees/{tid}/walk", f"/ws/walk?tree={tid}"):
            with c.websocket_connect(url) as ws:
                ws.send_json({"type": "hello", "role": "walk"})
                welcome = ws.receive_json()
                assert welcome["type"] == "welcome" and welcome["n"] == 65
        with c.websocket_connect("/ws/walk") as ws:  # no tree named: the first tree
            ws.send_json({"type": "hello", "role": "walk"})
            assert ws.receive_json()["n"] == 600
        assert state.trees[tid].walk is not None and state.trees["tree1"].walk is not None
        assert state.trees[tid].walk is not state.trees["tree1"].walk
        assert state.trees[tid].scans.list() and state.trees["tree1"].scans.list()
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/walk?tree=nope") as ws:
                ws.send_json({"type": "hello"})
                ws.receive_json()


def test_map_check_names_the_controller_that_changed(tmp_path):
    state = AppState(tmp_path, executor=concurrent.futures.ThreadPoolExecutor(1))
    try:
        t = state.default
        state.config.update({"controllers": [{"host": "10.0.0.1", "led_count": 30}, {"host": "10.0.0.2", "led_count": 20}]})
        t.reconfigure()
        a, b = t.links
        a.info, a.cfg = _info("aa:00:00:00:00:01", 30), _cfg(30)
        b.info, b.cfg = _info("aa:00:00:00:00:02", 20), _cfg(20)
        leds = [{"i": i, "p": [0, i, 0], "status": "measured"} for i in range(50)]
        t.maps.save({"id": "m1", "scan": "s", "leds": leds, "quality": {}, "wled": t.wled_meta()})
        t.maps.activate("m1", t.fingerprint())
        assert t.map_check() is None
        b.info = _info("aa:00:00:00:00:03", 20)  # controller 2 replaced
        assert "Controller 2" in t.map_check() and "another WLED controller" in t.map_check()
        b.info, b.cfg = _info("aa:00:00:00:00:02", 20), _cfg(20, start=1)  # its outputs changed
        assert "Controller 2" in t.map_check() and "configuration" in t.map_check()
        b.cfg = _cfg(20)
        state.config.update({"controllers": [{"host": "10.0.0.1", "led_count": 50}]})
        t.reconfigure()
        t.link.info, t.link.cfg = _info("aa:00:00:00:00:01", 50), _cfg(50)
        assert "2 WLED controller(s); this tree now has 1" in t.map_check()
    finally:
        t.output.stop()


def test_map_reports_a_controller_that_was_not_seen():
    leds = [{"i": i, "status": "measured" if i < 30 else "unknown"} for i in range(50)]
    m = {"leds": leds, "quality": {"warnings": []}}
    from treelights.tree import Tree

    Tree.annotate_controllers(m, {"controllers": [{"start": 0, "count": 30}, {"start": 30, "count": 20}]})
    assert [r["placed"] for r in m["quality"]["controllers"]] == [1.0, 0.0]
    assert any("Controller 2 (LEDs 30–49)" in w for w in m["quality"]["warnings"])
    one = {"leds": leds, "quality": {"warnings": []}}
    Tree.annotate_controllers(one, {})
    assert "controllers" not in one["quality"]
