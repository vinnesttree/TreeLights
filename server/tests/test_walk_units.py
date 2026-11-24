"""Unit tests for the walk-around capture modules (pattern, decoding, timing, gyro, tracking)."""

import numpy as np

from treelights.detection import binary
from treelights.walk import gyro, pattern
from treelights.walk.pattern import CYCLE_SLOTS, CYCLE_T, N_BITS, T_SLOT, WalkPattern, decode_votes, template
from treelights.walk.timing import estimate
from treelights.walk.tracking import Frame, track


def _votes(code, strength=0.6, flips=(), flip_strength=None, repeats=1):
    out = {}
    for b in range(N_BITS):
        v = bool(code[b])
        s = strength
        if b in flips:
            v = not v
            s = flip_strength if flip_strength is not None else strength
        out[b] = [(v, s)] * repeats
    return out


def test_pattern_frames():
    p = WalkPattern(40, level=48)
    assert (p.frame_for_slot(0)[:, 1] == 48).all()  # sync on
    assert (p.frame_for_slot(1) == 0).all()  # sync off
    for b in range(N_BITS):
        pat = p.frame_for_slot(4 + 2 * b)[:, 1]
        cmp = p.frame_for_slot(5 + 2 * b)[:, 1]
        assert set(np.unique(pat)) <= {12, 48} and (pat > 0).all()  # never off in code slots
        assert ((pat == 48) == (p.codes[:, b] == 1)).all()
        assert ((cmp == 48) == (p.codes[:, b] == 0)).all()


def test_pattern_level_changes_only_between_cycles():
    p = WalkPattern(10, level=48)
    r = p.renderer()
    assert r(100.0)[0, 1] == 48
    p.set_level(100)
    assert r(100.0 + 5 * T_SLOT)[:, 1].max() == 48  # same cycle
    assert r(100.0 + CYCLE_T)[0, 1] == 100  # next cycle (slot 0: sync on)


def test_decode_clean_and_with_weak_errors():
    codes = binary.code_table(600)
    for idx in (0, 1, 77, 341, 599):
        assert decode_votes(_votes(codes[idx]), 600)[0] == idx
        # A weak wrong vote in two bits, among three cycles' votes (a neighbour's glow,
        # partial occlusion), is outvoted.
        v = _votes(codes[idx], repeats=3)
        for b in (3, 9):
            v[b] = v[b][:2] + [(not v[b][0][0], 0.1)]
        assert decode_votes(v, 600)[0] == idx
        # Weak but consistent bits everywhere (a dim, distant LED), over two cycles:
        # soft decoding. From a single cycle that isn't accepted.
        assert decode_votes(_votes(codes[idx], strength=0.1, repeats=2), 600, n_cycles=2)[0] == idx
        assert decode_votes(_votes(codes[idx], strength=0.1), 600, n_cycles=1)[0] is None
        # A missing bit.
        v = _votes(codes[idx])
        del v[11]
        assert decode_votes(v, 600)[0] == idx


def test_decode_never_returns_a_wrong_led_under_strong_errors():
    """With 1-3 strong bit errors in one cycle the result is either right or nothing (hard
    decoding: minimum distance 4); with soft decoding over two cycles, 1-2 errors."""
    rng = np.random.default_rng(0)
    codes = binary.code_table(600)
    wrong = 0
    for _ in range(3000):
        idx = int(rng.integers(600))
        k = int(rng.integers(1, 4))
        flips = tuple(rng.choice(N_BITS, k, replace=False).tolist())
        r, _ = decode_votes(_votes(codes[idx], strength=float(rng.uniform(0.2, 0.9)), flips=flips), 600)
        if r is not None:
            wrong += r != idx
        if k <= 2:
            r, _ = decode_votes(_votes(codes[idx], strength=float(rng.uniform(0.2, 0.9)), flips=flips, repeats=2), 600,
                                n_cycles=2)
            if r is not None:
                wrong += r != idx
    assert wrong == 0


def test_decode_rejects_two_merged_leds():
    """A track that is half one LED and half another must not decode as either (or a third)."""
    codes = binary.code_table(600)
    rng = np.random.default_rng(1)
    for _ in range(300):
        a, b = rng.choice(600, 2, replace=False)
        va, vb = _votes(codes[a], repeats=2), _votes(codes[b], repeats=2)
        merged = {k: va[k] + vb[k] for k in va}
        assert decode_votes(merged, 600, n_cycles=1)[0] is None
        assert decode_votes(merged, 600, n_cycles=4)[0] is None


def test_timing_lock_and_drift():
    rng = np.random.default_rng(2)
    offset, drift = 1.37, 5e-5  # pattern started 1.37 s into the stream; clock drift 50 ppm
    t = np.sort(np.arange(0, 40, 1 / 30) + rng.normal(0, 0.003, 1200))
    pt = t - (offset + drift * t)
    # Each frame integrates the pattern over its exposure (1/60 s), so frames at a slot
    # boundary mix the two slots.
    sub = pt[:, None] + np.linspace(0, 1 / 60, 9)[None, :]
    level = template(np.floor(sub / T_SLOT).astype(int)).mean(axis=1)
    pt = pt + 1 / 120  # the exposure's middle
    flux = 5000 * level * (1 + 0.3 * np.sin(t / 7)) + rng.normal(0, 80, len(t))  # framing changes
    tm = estimate(t, flux)
    assert tm.ok
    err = tm.pattern_time(t) - pt
    err -= np.round(err / CYCLE_T) * CYCLE_T  # the cycle itself is ambiguous; the phase isn't
    assert np.abs(err - np.median(err)).max() < 0.01  # consistent (any constant is latency)
    assert abs(np.median(err)) < 0.015


def test_timing_rejects_static_lights():
    rng = np.random.default_rng(3)
    t = np.arange(0, 30, 1 / 30)
    tm = estimate(t, 4000 + rng.normal(0, 50, len(t)))
    assert not tm.ok


def test_quaternion_roundtrip_and_candidates():
    rng = np.random.default_rng(4)
    for _ in range(50):
        q = rng.normal(size=4)
        q /= np.linalg.norm(q)
        R = gyro.quat_to_R(q)
        assert np.allclose(R @ R.T, np.eye(3), atol=1e-9)
        q2 = gyro.R_to_quat(R)
        assert min(np.abs(q - q2).max(), np.abs(q + q2).max()) < 1e-9
    assert len(gyro.CANDIDATES) == 24
    assert any(np.allclose(M, gyro.DEFAULT_M) for M in gyro.CANDIDATES)


def _blobs(xy, flux=500.0):
    b = np.zeros((len(xy), 6))
    b[:, 0:2] = xy
    b[:, 2] = flux
    b[:, 3] = 100
    b[:, 4] = 0.8
    return b


def test_tracking_follows_a_moving_constellation():
    """40 LEDs panning across the image (30 px/s plus jitter), blinking off every few frames."""
    rng = np.random.default_rng(5)
    base = rng.uniform([100, 100], [860, 440], (40, 2))
    frames = []
    for i in range(150):
        t = i / 30
        shift = np.array([30 * t + 3 * np.sin(4 * t), 5 * np.sin(2 * t)])
        xy = base + shift
        show = np.ones(40, bool) if i % 12 not in (6, 7) else np.zeros(40, bool)  # all-off slot
        frames.append(Frame(t, _blobs(xy[show])))
    ts = track(frames, None, use_gyro=False)
    by = ts.by_track()
    assert len(by) == 40  # every LED is one track across the gaps
    for rows in by.values():
        fi = rows[:, 1].astype(int)
        t = fi / 30
        shift = np.c_[30 * t + 3 * np.sin(4 * t), 5 * np.sin(2 * t)]
        start = rows[0, 2:4] - shift[0]
        assert np.abs(rows[:, 2:4] - shift - start).max() < 1.0  # never jumps to another LED


def test_tracking_with_gyro_rotation():
    """Fast pure rotation (60 deg/s) is followed using the gyro prediction."""
    rng = np.random.default_rng(6)
    f = 750.0
    K = np.array([[f, 0, 479.5], [0, f, 269.5], [0, 0, 1.0]])
    P = np.c_[rng.uniform(-60, 60, (50, 2)), rng.uniform(250, 350, 50)]  # points in front of the camera
    frames = []
    for i in range(60):
        a = np.radians(60 * i / 30)
        R = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
        X = P @ R.T
        uv = X[:, :2] / X[:, 2:3] * f + K[:2, 2]
        ok = (X[:, 2] > 0) & (uv[:, 0] > 0) & (uv[:, 0] < 960) & (uv[:, 1] > 0) & (uv[:, 1] < 540)
        fr = Frame(i / 30, _blobs(uv[ok]), R=R)
        fr.meta["truth"] = np.flatnonzero(ok)
        frames.append(fr)
    ts = track(frames, K, use_gyro=True)
    # Each track stays on one point.
    for rows in ts.by_track().values():
        labels = set()
        for r in rows:
            fr = frames[int(r[1])]
            j = int(np.argmin(np.hypot(*(fr.blobs[:, :2] - r[2:4]).T)))
            labels.add(int(fr.meta["truth"][j]))
        assert len(labels) == 1


def test_timing_locks_while_walking_closer_and_further():
    """Walking closer and further changes the total LED brightness ~10x within seconds;
    the timing must still lock (the code level is followed by a running median)."""
    rng = np.random.default_rng(7)
    offset = 2.3
    t = np.sort(np.arange(0, 45, 1 / 30) + rng.normal(0, 0.003, 1350))
    pt = t - offset
    sub = pt[:, None] + np.linspace(0, 1 / 60, 9)[None, :]
    level = template(np.floor(sub / T_SLOT).astype(int)).mean(axis=1)
    distance = 1 + 0.45 * np.sin(2 * np.pi * t / 11) + 0.15 * np.sin(2 * np.pi * t / 3.7)
    flux = 5000 * level / distance**2 + rng.normal(0, 40, len(t))  # brightness ~ 1/distance^2
    assert (5000 / distance**2).max() / (5000 / distance**2).min() > 8
    tm = estimate(t, flux)
    assert tm.ok and tm.contrast < 0.2
    err = tm.pattern_time(t) - (pt + 1 / 120)
    err -= np.round(err / CYCLE_T) * CYCLE_T
    assert np.abs(err - np.median(err)).max() < 0.012 and abs(np.median(err)) < 0.015


def test_tree_axis_from_the_led_outline_when_walking():
    from treelights.reconstruction.orient import outline_axis_xz, tree_axis_xz
    from treelights.sim.tree import make_tree

    tree = make_tree(n=300, seed=4, height=130, base_radius=46, bottom=18)
    P = tree.positions  # true axis at x = z = 0
    # A circular walk centred 40 cm off the axis: a perfect circle, but not round the tree.
    ang = np.linspace(0, 2 * np.pi, 40, endpoint=False)
    path = np.c_[40 + 180 * np.cos(ang), np.full(40, 80.0), 180 * np.sin(ang)]
    c, src = tree_axis_xz(path, P, walk=True)
    assert src == "led_outline" and np.hypot(*c) < 0.06 * 46
    # Centred on the tree: the (more precise) camera circle is used.
    c, src = tree_axis_xz(path - [40, 0, 0], P, walk=True)
    assert src == "walk_circle" and np.hypot(*c) < 0.01 * 46
    # The tripod mode keeps trusting the turning circle.
    assert tree_axis_xz(path, P, walk=False)[1] == "turning_axis"
    # One side poorly measured (an arc of the tree missing): the outline barely moves.
    az = np.arctan2(P[:, 2], P[:, 0])
    thin = P[np.cos(az - 1.0) < 0.4]
    assert np.hypot(*outline_axis_xz(thin)) < 0.15 * 46
    assert np.hypot(*np.median(thin[:, [0, 2]], axis=0)) > 2 * np.hypot(*outline_axis_xz(thin))


def test_motion_log_on_another_clock_is_lined_up():
    """A phone stamped its orientation readings on another clock than its camera frames
    (52.8 hours ahead), so the gyro couldn't be used. The log is lined up by comparing the
    camera's rotation between frames (from the images) with the sensor's, to well within
    the gyro calibration's own +-0.1 s search."""
    from treelights.reconstruction.camera import rodrigues
    from treelights.walk.pipeline import motion_log_offset

    rng = np.random.default_rng(4)
    X = rng.uniform([-60, -60, 250], [660, 60, 450], (120, 3))  # LEDs in front of the camera's path
    f, w, h = 700.0, 960, 540
    T = np.arange(0, 40, 1 / 30)
    # Irregular turning: a slow sweep plus hand jitter at a few Hz.
    yaw = 0.15 * np.sin(0.4 * T) + 0.02 * np.sin(7.0 * T) + 0.01 * np.sin(11.0 * T)
    pitch = 0.05 * np.sin(0.7 * T) + 0.015 * np.sin(5.0 * T)
    Rs = [rodrigues(np.array([p, y, 0.0])) for y, p in zip(yaw, pitch)]
    rows = []
    for fi, R in enumerate(Rs):
        C = np.array([15 * T[fi], 2 * np.sin(0.5 * T[fi]), 0.0])  # walking sideways (the images need parallax)
        Xc = (X - C) @ R.T
        uv = Xc[:, :2] / Xc[:, 2:3] * f + [(w - 1) / 2, (h - 1) / 2] + rng.normal(0, 0.3, (len(X), 2))
        for i, (x, y) in enumerate(uv):
            rows.append((i, fi, x, y, 100.0, 0.8, 0.0))
    rows = np.array(rows)
    frame_t = 1795397020.0 + T
    log_t = np.arange(-0.6, 40.3, 1 / 60)  # the sensor started 0.6 s before the camera
    yl, pl = np.interp(log_t, T, yaw), np.interp(log_t, T, pitch)
    log_q = np.array([gyro.R_to_quat(rodrigues(np.array([p, y, 0.0]))) for y, p in zip(yl, pl)])
    log_t = log_t + 1795397020.0 + 190252.2  # on another clock
    off = motion_log_offset(rows, frame_t, log_t, log_q, w, h)
    assert abs(off - (-190252.2)) < 0.06


def test_recording_lands_on_the_live_frames_with_a_real_phone_clock():
    """Phone clocks are near 1.8e9 s. With the live stream's timing drift fit at 85 ppm, the
    recording's frames were put 42 hours from the live frames (2 keyframes over a 5.5 minute
    walk). They must land on the phone clock where they were taken, with or without the
    recording's start time."""
    from treelights.walk.pipeline import Source, SourceResult, video_phone_times
    from treelights.walk.timing import Timing
    from treelights.walk.tracking import TrackSet

    t0 = 1795397020.0  # phone clock when the walk started
    b = 8.5e-5  # drift between the phone's clock and the LED pattern
    live_t = t0 + np.arange(0, 330, 0.134)
    # Live timing: pattern_time = t - (a + b t), with the pattern's zero somewhere before the walk.
    a = -b * t0 + 1.234 - 50 * CYCLE_T
    live_timing = Timing(a, b, 0.1, 40, True)
    live = SourceResult(Source("live", [Frame(float(t), np.zeros((0, 6))) for t in live_t], 720, 1280, live_t),
                        live_timing, TrackSet(np.zeros((0, 7)), 0, np.zeros((len(live_t), 2))), {}, None)
    rec_start = t0 + 0.35
    video_t = np.arange(0, 330, 1 / 30)
    true_phone = rec_start + video_t
    # The recording's own timing starts near 0 (its frame times do): its pattern time is the
    # live one's minus a huge whole number of cycles.
    k = np.round(live_timing.pattern_time(rec_start) / CYCLE_T)
    video_pattern = live_timing.pattern_time(true_phone) - k * CYCLE_T
    vb = 2e-5
    va = float(np.mean(video_t * (1 - vb) - video_pattern))
    video_timing = Timing(va, vb, 0.1, 40, True)
    for rs in (rec_start, None):
        got = video_phone_times(video_t, video_timing, live, rs)
        assert np.abs(got - true_phone).max() < 0.05, (rs, float(np.abs(got - true_phone).max()))
