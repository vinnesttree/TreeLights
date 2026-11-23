"""Walk-around capture: from frames (live and/or video) to observations for the solver.

Per source (the frames streamed live during the walk, and the phone's recording):
  timing sync -> tracking without gyro -> gyro self-calibration -> tracking with
  gyro -> decoding LED identities from tracks (the tracking that identifies more
  detections is kept).
Then keyframes are chosen across both sources (the video wherever it covers a time
window; the live frames fill gaps and also work alone) and turned into segments for the existing solver, with the gyro's
gravity direction ("up") and focal length as priors.

Times: live frames carry the phone's clock (seconds); the video carries its own
timeline. Both are mapped onto the LED pattern's timeline by in-band sync, which
also gives each video frame its phone-clock time for reading the orientation log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import gyro as gyromod
from .blobs import FLUX
from .decode import Identity, decode_tracks, identity_at
from .pattern import CYCLE_T
from .timing import Timing, estimate
from .tracking import Frame, TrackSet, stitch, track

MAX_KEYFRAMES = 48
MIN_KEYFRAME_GAP_S = 0.8
MIN_IDENTIFIED = 10
MAX_RATE_DEG_S = 45.0
POINT_SHARP = 0.5  # sharpness = sqrt(6 / core area): below this, a blob counts as scattered light
# Experimental, off by default (walk/diag.py switches them on to compare on real walks):
POINT_RELATIVE = False  # when LEDs are big, judge against the frame's typical LED instead...
POINT_REL = 0.7  # ...: a point's core is at most ~2x the typical LED's
STITCH = False  # join track pieces that continue each other before decoding


@dataclass
class Source:
    name: str  # "live" | "video"
    frames: list[Frame]
    width: int
    height: int
    phone_t: np.ndarray | None = None  # phone-clock time of each frame (s), if known


@dataclass
class SourceResult:
    source: Source
    timing: Timing
    tracks: TrackSet
    ids: dict[int, list[Identity]]
    gyro: gyromod.GyroModel | None
    notes: list[str] = field(default_factory=list)
    gyro_cal: dict = field(default_factory=dict)  # calibration diagnostics, also when rejected

    @property
    def frame_t(self) -> np.ndarray:
        return np.array([f.t for f in self.source.frames])

    def identity(self, track_id: int, frame_index: int) -> Identity | None:
        """The LED a track's detection in a frame belongs to (tracks can change LED)."""
        ids = self.ids.get(track_id)
        if not ids:
            return None
        return identity_at(ids, float(self.timing.pattern_time(self.source.frames[frame_index].t)))

    def decoded_leds(self) -> set[int]:
        return {i.index for lst in self.ids.values() for i in lst}


def process_source(src: Source, n_leds: int, log_t: np.ndarray | None, log_q: np.ndarray | None,
                   gyro_model: gyromod.GyroModel | None = None, f_prior: float | None = None,
                   phone_time_fn=None, timing_hint: Timing | None = None) -> SourceResult:
    """phone_time_fn(t, timing) -> phone-clock times, for sources (the video) whose frames
    don't carry phone time; called once the source's own pattern timing is known."""
    frames = src.frames
    t = np.array([f.t for f in frames])
    flux = np.array([float(f.blobs[:, FLUX].sum()) if len(f.blobs) else 0.0 for f in frames])
    timing = estimate(t, flux)
    if not timing.ok and timing_hint is not None and timing_hint.ok:
        timing = timing_hint  # the pattern's phase doesn't drift: a timing found earlier still holds
    if src.phone_t is None and phone_time_fn is not None:
        src.phone_t = phone_time_fn(t, timing)
    notes = []
    if not timing.ok:
        notes.append(f"{src.name}: the LED pattern wasn't found in the frames.")
    ts = track(frames, None, use_gyro=False)
    if STITCH:
        ts = stitch(ts, t)
    tracking = "image"
    model = gyro_model
    have_log = log_t is not None and log_q is not None and len(log_t) >= 10 and src.phone_t is not None
    if have_log and model is None:
        model = gyromod.calibrate(ts.rows, src.phone_t, log_t, log_q, src.width, src.height, f_prior)
        if not model.ok:
            notes.append(f"{src.name}: the orientation sensor didn't match the camera motion; not used.")
    if have_log and model is not None and model.ok:
        scale = max(src.width, src.height) / model.size if model.size else 1.0
        q = gyromod.slerp_log(log_t, log_q, src.phone_t + model.delta)
        for fr, qq in zip(frames, q):
            fr.R = model.R_cam(qq)
            fr.meta["q"] = qq
        f_px = model.f * scale
        K = np.array([[f_px, 0, (src.width - 1) / 2], [0, f_px, (src.height - 1) / 2], [0, 0, 1.0]])
        ts_g = track(frames, K, use_gyro=True)
        if STITCH:
            ts_g = stitch(ts_g, t)
        ids_g = decode_tracks(ts_g, t, timing, n_leds)
        ids = decode_tracks(ts, t, timing, n_leds)
        # The gyro's prediction helps most with hand jitter at full frame rate; with
        # frames far apart (the live stream) the walk's own motion dominates and it
        # can do worse. Keep whichever tracking identifies more detections.
        if _identified_rows(ts_g, ids_g, timing, t) >= _identified_rows(ts, ids, timing, t):
            ts, ids = ts_g, ids_g
            tracking = "gyro"
    else:
        ids = decode_tracks(ts, t, timing, n_leds)
    cal = {} if model is None else dict(model.info, ok=model.ok, f=round(model.f, 1), delta=round(model.delta, 3))
    cal["tracking"] = tracking
    return SourceResult(src, timing, ts, ids, model if (model and model.ok) else None, notes, cal)


def _identified_rows(ts: TrackSet, ids: dict, timing: Timing, frame_t: np.ndarray) -> int:
    if not ids or len(ts.rows) == 0:
        return 0
    ptm = timing.pattern_time(frame_t[ts.rows[:, 1].astype(int)])
    return sum(1 for r, p in zip(ts.rows, ptm) if int(r[0]) in ids and identity_at(ids[int(r[0])], float(p)) is not None)


def video_phone_times(video_t: np.ndarray, video_timing: Timing, live: SourceResult | None, rec_start: float | None) -> np.ndarray:
    """Phone-clock time of each video frame, via the shared LED pattern timeline."""
    guess = (rec_start or 0.0) + video_t
    if live is None or not live.timing.ok or not video_timing.ok:
        return guess
    pt = video_timing.pattern_time(video_t)
    base = live.timing.source_time(pt)
    # Pattern timelines agree modulo one cycle: pick the cycle shift closest to the guess.
    m = np.round((guess - base) / CYCLE_T)
    m = np.median(m) if rec_start is not None else 0.0
    return live.timing.source_time(pt + m * CYCLE_T)


@dataclass
class Keyframe:
    source: str
    index: int
    t_phone: float
    count: int


def identified_detections(res: SourceResult) -> dict[int, list[tuple]]:
    """frame index -> [(led, x, y, flux, sharp)]"""
    out: dict[int, list[tuple]] = {}
    for r in res.tracks.rows:
        ident = res.identity(int(r[0]), int(r[1]))
        if ident is None:
            continue
        out.setdefault(int(r[1]), []).append((ident.index, r[2], r[3], r[4], r[5]))
    return out


def _rate(frames: list[Frame], i: int) -> float:
    """Angular speed (deg/s) around frame i from the gyro, 0 if unknown."""
    if frames[i].R is None:
        return 0.0
    j = i + 1 if i + 1 < len(frames) else i - 1
    if j < 0 or frames[j].R is None:
        return 0.0
    dR = frames[j].R @ frames[i].R.T
    ang = np.degrees(np.arccos(np.clip((np.trace(dR) - 1) / 2, -1, 1)))
    return float(ang / max(abs(frames[j].t - frames[i].t), 1e-3))


def _priority(source: str) -> int:
    return 1 if source == "video" else 0


def build_observations(results: list[SourceResult]) -> tuple[dict, dict, dict]:
    """Choose keyframes across sources; return (obs, segments, info) for the solver."""
    cands: list[tuple[float, float, str, int]] = []  # (phone time, score, source name, frame index)
    dets: dict[str, dict[int, list[tuple]]] = {}
    by_name = {r.source.name: r for r in results}
    for res in results:
        d = identified_detections(res)
        dets[res.source.name] = d
        pt = res.source.phone_t if res.source.phone_t is not None else res.frame_t
        for fi, lst in d.items():
            if len(lst) < MIN_IDENTIFIED:
                continue
            rate = _rate(res.source.frames, fi)
            if rate > MAX_RATE_DEG_S:
                continue  # likely blurred
            sharp = float(np.mean([x[4] for x in lst]))
            score = len({x[0] for x in lst}) * (0.5 + 0.5 * sharp) / (1 + rate / 30.0)
            cands.append((float(pt[fi]), score, res.source.name, fi))
    if not cands:
        return {}, {}, {"keyframes": 0}
    cands.sort()
    t0, t1 = cands[0][0], cands[-1][0]
    win = max(MIN_KEYFRAME_GAP_S, (t1 - t0) / MAX_KEYFRAMES)
    # One keyframe per time window. The recording wins wherever it covers the window
    # (full frame rate and resolution, and one camera for the solver to calibrate);
    # live frames fill windows it doesn't cover, or stand alone if it wasn't uploaded.
    chosen: dict[int, tuple] = {}
    for c in cands:
        w = int((c[0] - t0) // win)
        cur = chosen.get(w)
        better = cur is None or (_priority(c[2]), c[1]) > (_priority(cur[2]), cur[1])
        if better:
            chosen[w] = c
    obs: dict[str, dict[int, list[dict]]] = {}
    segs: dict[str, dict] = {}
    for k, (tp, _score, name, fi) in enumerate(sorted(chosen.values())):
        res = by_name[name]
        fr = res.source.frames[fi]
        sid = f"w{k + 1:03d}"
        per: dict[int, list[tuple]] = {}
        for led, x, y, fl, sh in dets[name][fi]:
            per.setdefault(led, []).append((x, y, fl, sh))
        # Point or diffuse: a blob whose core covers more than ~24 px is scattered light. But
        # close up, or with a high-resolution camera, every LED covers that much, and with the
        # fixed size most direct views of LEDs were taken for scattered light: too few were
        # left to place the cameras (a simulated walk with realistic LED sizes failed outright).
        # When the fixed size calls most of a frame's LEDs diffuse, judge against the frame's
        # typical LED instead. Otherwise keep it: then it separates glows on branches better.
        best = [max(z[3] for z in lst) for lst in per.values()]
        thr = POINT_SHARP
        if POINT_RELATIVE and best and np.mean(np.array(best) >= POINT_SHARP) < 0.5:
            thr = POINT_REL * float(np.median(best))
        o = {}
        for led, lst in per.items():
            lst.sort(key=lambda z: -z[2])
            top = max(lst[0][2], 1e-6)
            cl = []
            for x, y, fl, sh in lst[:3]:
                point = sh >= thr
                cl.append({"x": float(x), "y": float(y), "score": float(fl / top), "snr": 30.0,
                           "kind": "point" if point else "diffuse",
                           "sigma_px": round(0.4 / max(sh, 0.3), 2) if point else round(max(2.0, float(np.sqrt(6.0 / max(sh, 0.05) ** 2 / np.pi))), 2)})
            o[led] = cl
        obs[sid] = o
        up = None
        if fr.R is not None:
            up = (fr.R @ np.array([0.0, 0.0, 1.0])).tolist()
        segs[sid] = {"view": "walk", "width": res.source.width, "height": res.source.height, "up_cam": up,
                     "device": f"walk-{name}", "source": name, "frame": fi, "t_phone": tp}
    info = {"keyframes": len(segs), "window_s": win,
            "by_source": {n: sum(1 for s in segs.values() if s["source"] == n) for n in by_name}}
    return obs, segs, info


def focal_prior(results: list[SourceResult]) -> float | None:
    """Prefer the video's calibration (more frames, full frame rate), then the best residual."""
    cands = [r for r in results if r.gyro is not None]
    if not cands:
        return None
    cands.sort(key=lambda r: (r.source.name != "video", r.gyro.residual_px))
    g = cands[0].gyro
    return g.f / (g.size or max(cands[0].source.width, cands[0].source.height))
