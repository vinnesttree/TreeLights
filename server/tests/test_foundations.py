import json
import time
from pathlib import Path

import numpy as np
import pytest
from cryptography import x509

from treelights import storage, tls
from treelights.output.manager import OutputManager, PowerLimiter
from treelights.wled import ddp
from treelights.wled.client import WledConfigView, WledInfo, config_fingerprint


def test_ddp_600_leds_is_two_packets_with_push_on_last():
    rgb = np.arange(600 * 3, dtype=np.uint32).reshape(600, 3) % 256
    pkts = ddp.encode_frame(rgb.astype(np.uint8))
    assert len(pkts) == 2
    f0, off0, d0, push0 = ddp.decode_packet(pkts[0])
    f1, off1, d1, push1 = ddp.decode_packet(pkts[1])
    assert (off0, len(d0), push0) == (0, 1440, False)
    assert (off1, len(d1), push1) == (1440, 360, True)
    assert pkts[0][1] == 0  # sequence number unused on purpose
    assert pkts[0][2] == ddp.DATATYPE_RGB8
    assert d0 + d1 == rgb.astype(np.uint8).tobytes()


def test_ddp_small_frame_single_push_packet():
    pkts = ddp.encode_frame(np.zeros((10, 3), np.uint8))
    assert len(pkts) == 1 and ddp.decode_packet(pkts[0])[3]


def test_dnrgb_splits_at_489():
    pkts = ddp.encode_dnrgb(np.zeros((600, 3), np.uint8))
    assert len(pkts) == 2 and pkts[1][2:4] == (489).to_bytes(2, "big")


def test_atomic_json_and_jsonl_tolerates_truncation(tmp_path: Path):
    p = tmp_path / "a" / "x.json"
    storage.atomic_write_json(p, {"a": np.float32(1.5), "b": np.arange(3)})
    assert storage.read_json(p) == {"a": 1.5, "b": [0, 1, 2]}
    log = storage.JsonlLog(tmp_path / "l.jsonl")
    log.append({"k": 1})
    log.append({"k": 2})
    with open(tmp_path / "l.jsonl", "a") as f:
        f.write('{"k": 3')  # crash mid-write
    assert [r["k"] for r in log.read()] == [1, 2]


class FakeSender:
    def __init__(self):
        self.frames = []

    def send(self, rgb, repeats=1):
        for _ in range(repeats):
            self.frames.append((time.monotonic(), rgb.copy()))

    def close(self):
        pass


def test_output_lease_priority_keepalive_and_release():
    om = OutputManager(5)
    s = FakeSender()
    om.set_sender(s)
    try:
        ok, _ = om.acquire("test-anim", "test")
        assert ok
        ok, holder = om.acquire("other", "test")
        assert not ok and holder.owner == "test-anim"
        ok, _ = om.acquire("cal", "calibration")  # higher priority preempts
        assert ok
        with pytest.raises(PermissionError):
            om.show("test-anim", np.zeros((5, 3), np.uint8)).result(1)
        frame = np.full((5, 3), 7, np.uint8)
        om.show("cal", frame, repeats=2).result(1)
        n = len(s.frames)
        assert n == 2
        time.sleep(1.2)
        assert len(s.frames) >= n + 2  # keepalive every 0.5 s
        assert np.array_equal(s.frames[-1][1], frame)
        om.release("cal")
        time.sleep(0.1)
        n = len(s.frames)
        time.sleep(0.8)
        assert len(s.frames) == n  # nothing sent without a lease
    finally:
        om.stop()


def test_output_lease_expires():
    om = OutputManager(3)
    om.set_sender(FakeSender())
    try:
        om.acquire("a", "test", ttl=0.2)
        time.sleep(0.4)
        assert om.holder() is None
    finally:
        om.stop()


def test_power_limiter():
    pl = PowerLimiter(max_ma=1000, ma_per_channel=20, idle_ma_per_led=1)
    full = np.full((100, 3), 255, np.uint8)  # 6000 mA + 100 idle
    out = pl.apply(full)
    assert pl.estimate_ma(out) <= 1000 + 1e-6
    small = np.full((100, 3), 5, np.uint8)
    assert np.array_equal(pl.apply(small), small)
    assert np.array_equal(PowerLimiter(None).apply(full), full)


def test_info_parsing_is_defensive():
    info = WledInfo.parse({"ver": "0.15.0-b3", "mac": "AA:bb:cc:dd:ee:ff", "leds": {"count": 600, "maxpwr": 5000},
                           "live": True, "lip": "192.168.1.5", "uptime": 100})
    assert info.mac == "aabbccddeeff" and info.led_count == 600 and info.version_ok()
    weird = WledInfo.parse({"leds": "nope", "live": "yes"})
    assert weird.led_count is None and weird.live is None and weird.version_ok() is None
    assert WledInfo.parse({"ver": "0.13.3"}).version_ok() is False


def test_cfg_buses_give_output_breaks_and_fingerprint_changes():
    raw = {"hw": {"led": {"ins": [{"start": 300, "len": 300, "order": 0}, {"start": 0, "len": 300}]}}, "if": {"live": {"en": True}}}
    cfg = WledConfigView.parse(raw)
    assert cfg.output_breaks() == [299]
    info = WledInfo.parse({"leds": {"count": 600}})
    fp1 = config_fingerprint(info, cfg)
    raw2 = json.loads(json.dumps(raw))
    raw2["hw"]["led"]["ins"][0]["len"] = 299
    assert fp1 != config_fingerprint(info, WledConfigView.parse(raw2))
    assert WledConfigView.parse(None).output_breaks() == []


def test_tls_reissues_on_new_address(tmp_path: Path):
    c1, k1 = tls.ensure_server_cert(tmp_path, ["192.168.1.10"], ["tree"])
    pem1 = c1.read_bytes()
    tls.ensure_server_cert(tmp_path, ["192.168.1.10"], ["tree"])
    assert c1.read_bytes() == pem1  # unchanged when nothing changed
    tls.ensure_server_cert(tmp_path, ["192.168.1.11"], ["tree"])
    cert = x509.load_pem_x509_certificate(c1.read_bytes())
    ips = {str(v) for v in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)}
    assert "192.168.1.11" in ips
    ca = x509.load_pem_x509_certificate((tmp_path / "ca.crt").read_bytes())
    ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, __import__("cryptography.hazmat.primitives.asymmetric.ec", fromlist=["ECDSA"]).ECDSA(cert.signature_hash_algorithm))
