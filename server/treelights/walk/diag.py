"""Diagnose walk processing on a stored walk, without changing anything.

Run inside the container (Dockge: the stack's terminal, "bash"):

    python -m treelights.walk.diag                  # the most recent walk
    python -m treelights.walk.diag <scan> <view>    # a given one
    python -m treelights.walk.diag --variants base,point

Progress is printed as it goes, each line with the time since the start. The detected
blobs are cached in /tmp inside the container (not in your data), so running it again
for the same walk skips the slow detection; --no-cache detects again.

The walk's live frames and recording are decoded once; then each variant runs the
rest of the pipeline and the solve, and a short report per stage is printed: is the
pattern found, how many LEDs are decoded, how many are identified per keyframe, how
many of those count as point sources, how many cameras are placed and LEDs measured.
Nothing is written: maps, observations and the scan stay as they are.

Variants (experimental changes, off in normal processing):
  base    the processing as it is
  point   point/scattered light judged against the frame's typical LED when LEDs are big
  flux    decoding brightness over a 9x9 px window instead of 5x5
  gate    tracking match gate 8 px instead of 5
  local   tracking corrected by the neighbours' motion
  stitch  track pieces that continue each other joined before decoding
  all     all of the above together
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np

from . import blobs as B
from . import gyro as GY
from . import pipeline as PL
from . import process as PR
from . import tracking as TR
from .job import _live_images, _video_images, load_motion
from .process import analyze
from .tracking import Frame

VARIANTS = {
    "base": set(),
    "point": {"point"},
    "flux": {"flux"},
    "gate": {"gate"},
    "local": {"local"},
    "stitch": {"stitch"},
    "all": {"point", "flux", "gate", "local", "stitch"},
}


_T0 = time.time()


def _say(*a) -> None:
    e = int(time.time() - _T0)
    print(f"[{e // 60:3d}:{e % 60:02d}]", *a, flush=True)


class _Progress(list):
    """A frame list that reports how far a pass over it has got (every 10%)."""

    def __init__(self, items, label: str):
        super().__init__(items)
        self.label = label

    def __iter__(self):
        n = len(self)
        step = max(1, n // 10)
        t = time.time()
        for i, x in enumerate(super().__iter__()):
            if i and i % step == 0:
                left = (time.time() - t) / i * (n - i)
                _say(f"      {self.label}: {100 * i // n}% ({i}/{n} frames, about {left / 60:.1f} min left)")
            yield x


def _hook_progress() -> None:
    """Report each stage of the pipeline as it runs (wrapping the functions it calls)."""
    src = {"name": "?"}
    orig_ps, orig_track, orig_est = PL.process_source, PL.track, PL.estimate
    orig_dec, orig_cal, orig_stitch = PL.decode_tracks, GY.calibrate, PL.stitch

    def process_source(s, *a, **k):
        src["name"] = s.name
        _say(f"    {s.name}: {len(s.frames)} frames")
        return orig_ps(s, *a, **k)

    def estimate(*a, **k):
        t = time.time()
        r = orig_est(*a, **k)
        _say(f"    {src['name']}: pattern timing {'found' if r.ok else 'NOT found'} ({time.time() - t:.0f} s)")
        return r

    def track(frames, K=None, use_gyro=True):
        how = "with the gyro" if use_gyro and K is not None else "without the gyro"
        _say(f"    {src['name']}: tracking {how}...")
        t = time.time()
        r = orig_track(_Progress(frames, f"tracking {src['name']} ({how})"), K, use_gyro)
        _say(f"    {src['name']}: tracking {how} done: {r.n_tracks} tracks ({time.time() - t:.0f} s)")
        return r

    def stitch(ts, t_):
        t = time.time()
        r = orig_stitch(ts, t_)
        _say(f"    {src['name']}: stitching: {ts.n_tracks} -> {r.n_tracks} tracks ({time.time() - t:.0f} s)")
        return r

    def calibrate(*a, **k):
        _say(f"    {src['name']}: calibrating the gyro...")
        t = time.time()
        r = orig_cal(*a, **k)
        _say(f"    {src['name']}: gyro {'calibrated' if r.ok else 'rejected'} ({time.time() - t:.0f} s)")
        return r

    def decode_tracks(ts, *a, **k):
        t = time.time()
        r = orig_dec(ts, *a, **k)
        leds = len({i.index for lst in r.values() for i in lst})
        _say(f"    {src['name']}: decoded {leds} LEDs from {ts.n_tracks} tracks ({time.time() - t:.0f} s)")
        return r

    PL.process_source, PL.estimate, PL.track, PL.stitch = process_source, estimate, track, stitch
    PL.decode_tracks, GY.calibrate = decode_tracks, calibrate
    PR.process_source = process_source  # process.analyze calls it by its own imported name


def _detect(images) -> tuple[list[tuple[float, np.ndarray]], int, int]:
    """Blobs per frame with both brightness windows: columns 0-5 as usual (5x5 flux), 6 = 9x9 flux."""
    orig = B.measure_all

    def both(Db, g, peaks):
        out = orig(Db, g, peaks)
        P = np.pad(Db, B._R)
        xs, ys = peaks[:, 0], peaks[:, 1]
        W = P[ys[:, None, None] + B._R + B._dy[None], xs[:, None, None] + B._R + B._dx[None]]
        f9 = np.clip(W, 0, None).sum(axis=(1, 2)).astype(np.float32)
        return np.c_[out, f9]

    B.measure_all = both
    try:
        out, w, h = [], 0, 0
        t0 = time.time()
        for k, (t, img) in enumerate(images):
            if k and k % 1000 == 0:
                _say(f"      {k} frames ({k / (time.time() - t0):.1f} frames/s)")
            h, w = img.shape[:2]
            b = B.detect_blobs(img)
            if b.shape[1] == 6:  # no peaks: an empty (0, 6) array
                b = np.zeros((0, 7), np.float32)
            out.append((float(t), b))
        return out, w, h
    finally:
        B.measure_all = orig


def _frames(det, flux9: bool) -> list[Frame]:
    cols = [0, 1, 6, 3, 4, 5] if flux9 else [0, 1, 2, 3, 4, 5]
    return [Frame(t, b[:, cols]) for t, b in det]


def _apply(opts: set[str]) -> None:
    TR.GATE_PX = 8.0 if "gate" in opts else 5.0
    TR.LOCAL_FIELD = "local" in opts
    PL.STITCH = "stitch" in opts
    PL.POINT_RELATIVE = "point" in opts


def _blob_stats(name: str, det) -> None:
    if not det:
        return
    sh = np.concatenate([b[:, 4] for _t, b in det[::10] if len(b)] or [np.zeros(0)])
    fl = np.concatenate([b[:, 2] for _t, b in det[::10] if len(b)] or [np.zeros(0)])
    nb = np.mean([len(b) for _t, b in det])
    if len(sh):
        bright = fl >= np.percentile(fl, 60)
        _say(f"  {name}: {len(det)} frames, {nb:.0f} blobs/frame; bright blobs: sharpness median "
             f"{np.median(sh[bright]):.2f}, share >= 0.5 (counted as points by default) {np.mean(sh[bright] >= 0.5):.0%}")


def run(data: Path, scan_id: str | None, vid: str | None, variants: list[str], use_cache: bool = True) -> None:
    from ..capture.store import ScanStore
    from ..config import ConfigStore
    from ..reconstruction import sfm
    from ..reconstruction.solve import SolveInput, solve

    store = ScanStore(data)
    if scan_id is None:
        for s in sorted(store.list(), key=lambda m: m.get("created") or "", reverse=True):
            scan = store.get(s["id"])
            walks = [v for v in scan.meta.get("views", []) if v.get("mode") == "walk"]
            if walks:
                scan_id, vid = scan.id, walks[-1]["id"]
                break
        if scan_id is None:
            sys.exit("No walk found.")
    scan = store.get(scan_id)
    if vid is None:
        vid = [v for v in scan.meta.get("views", []) if v.get("mode") == "walk"][-1]["id"]
    view = scan.view(vid)
    n = int(scan.meta["led_count"])
    d = scan.root / "walk" / vid
    video = scan.root / "videos" / f"{vid}.upload"
    try:
        height = ConfigStore(data).settings.tree_height_cm
    except Exception:  # noqa: BLE001
        height = None
    _say(f"Walk {scan_id}/{vid}: {n} LEDs, tree height {height} cm, recording "
         f"{video.stat().st_size / 1e6:.0f} MB" if video.exists() else f"Walk {scan_id}/{vid}: {n} LEDs, no recording")
    log_t, log_q = load_motion(d)
    t0 = time.time()
    cache = Path(f"/tmp/treelights-diag-{scan_id}-{vid}.pkl")
    if use_cache and cache.exists():
        _say(f"Using the blobs detected before ({cache}; --no-cache detects again)")
        with open(cache, "rb") as fh:
            live, vid_det = pickle.load(fh)
    else:
        _say("Detecting blobs in the live frames...")
        live = _detect(_live_images(d)) if (d / "live.jsonl").exists() else ([], 0, 0)
        _say("Detecting blobs in the recording (this takes a while)...")
        vid_det = _detect(_video_images(video)) if video.exists() and video.stat().st_size > 0 else ([], 0, 0)
        if use_cache:
            try:
                with open(cache, "wb") as fh:
                    pickle.dump((live, vid_det), fh)
            except OSError as e:
                _say(f"  (couldn't cache the blobs: {e})")
    _say(f"  done in {time.time() - t0:.0f} s; live {live[1]}x{live[2]}, recording {vid_det[1]}x{vid_det[2]}")
    _blob_stats("live", live[0])
    _blob_stats("recording", vid_det[0])
    overrides = scan.overrides()
    _hook_progress()
    for name in variants:
        opts = VARIANTS[name]
        _apply(opts)
        t1 = time.time()
        _say(f"\n=== {name} ===")
        lv = (_frames(live[0], "flux" in opts), live[1], live[2]) if live[0] else None
        vv = (_frames(vid_det[0], "flux" in opts), vid_det[1], vid_det[2]) if vid_det[0] else None
        _say("  decoding...")
        try:
            wr = analyze(n, lv, vv, log_t, log_q, view.get("rec_start"))
        except Exception as e:  # noqa: BLE001
            _say(f"  decoding failed: {e!r}")
            continue
        for r in wr.results:
            per = PL.identified_detections(r)
            med = float(np.median([len(v) for v in per.values()])) if per else 0.0
            _say(f"  {r.source.name}: pattern {'found' if r.timing.ok else 'NOT found'} (contrast {r.timing.contrast:.2f}), "
                 f"gyro {'used' if r.gyro is not None else 'not used'}, tracking {r.gyro_cal.get('tracking')}, "
                 f"tracks {r.tracks.n_tracks}, LEDs decoded {len(r.decoded_leds())}, identified per frame (median) {med:.0f}")
        dec = set().union(*[r.decoded_leds() for r in wr.results]) if wr.results else set()
        obs = wr.obs
        ident = [len(o) for o in obs.values()]
        unamb = [sum(1 for c in o.values() if sfm.unambiguous(c) is not None) for o in obs.values()]
        _say(f"  LEDs decoded (either source): {len(dec)} of {n}; keyframes {len(obs)}; per keyframe (median): "
             f"identified {np.median(ident) if ident else 0:.0f}, usable for the cameras {np.median(unamb) if unamb else 0:.0f}")
        if not obs:
            continue
        segs = {s: dict(m, view=vid) for s, m in wr.segments.items()}
        _say("  solving...")
        try:
            m = solve(SolveInput(n, obs, segs, height, list(scan.meta.get("output_breaks", [])), overrides, None,
                                 wr.info.get("focal_prior"), scan_id, None), progress=lambda msg: _say(f"    {msg}"))
        except Exception as e:  # noqa: BLE001
            _say(f"  solve failed: {e}")
            continue
        q = m["quality"]
        counts = q.get("counts", {})
        _say(f"  cameras placed {q.get('registered_segments')} of {q.get('segments_total')}, camera fit {q.get('camera_rms_px')} px; "
             f"LEDs: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        if q.get("activation_blocked"):
            _say(f"  blocked: {q['activation_blocked']}")
        for w in (q.get("warnings") or [])[:3]:
            _say(f"  warning: {w}")
        _say(f"  ({time.time() - t1:.0f} s)")
    _apply(set())


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m treelights.walk.diag", description=__doc__.split("\n\n")[0])
    p.add_argument("scan", nargs="?")
    p.add_argument("view", nargs="?")
    p.add_argument("--data", default=os.environ.get("TREELIGHTS_DATA", "/data"))
    p.add_argument("--variants", default=",".join(VARIANTS))
    p.add_argument("--no-cache", action="store_true", help="detect the blobs again instead of using /tmp's copy")
    a = p.parse_args()
    names = [v.strip() for v in a.variants.split(",") if v.strip()]
    bad = [v for v in names if v not in VARIANTS]
    if bad:
        sys.exit(f"unknown variant(s): {', '.join(bad)}; choose from {', '.join(VARIANTS)}")
    run(Path(a.data), a.scan, a.view, names, use_cache=not a.no_cache)


if __name__ == "__main__":
    main()
