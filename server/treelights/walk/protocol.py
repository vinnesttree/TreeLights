"""Walk-around WebSocket protocol (/ws/walk, architecture §7.9).

Phone -> server (JSON text unless noted):
  hello     {role:"walk", scan?, view?, camera:{width, height, device}}
            (view: reconnecting to a walk in progress)
  start     {camera?}                         start the LED pattern; a new view
  motion    {t:[s...], q:[[x,y,z,w]...]}      orientation samples, phone clock (s)
  rec_start {t}                               phone time the video recording started
  frame     binary: u32 big-endian header length, JSON header {t, q}, JPEG bytes
  stop      {reason?}
  ping      {t}
Server -> phone:
  welcome   {scan, view, n, slot_s, cycle_s, resumed}
  started   {scan, view}
  walk_status {state, locked, identified, sectors[12], covered, heading_deg, warnings[], level, ...}
  stopped   {scan, view, upload, process}   upload the recording (PUT chunks), then POST process
  pong      {t}
  error     {message}

Phone times are seconds on one clock (performance.timeOrigin + performance.now()),
for frames, sensor samples and the recording start alike.
"""

from __future__ import annotations

from ..capture.protocol import pack_frame, unpack_frame  # same binary framing
from .pattern import CYCLE_T, T_SLOT

RECONNECT_GRACE_S = 15.0

__all__ = ["pack_frame", "unpack_frame", "timing_info", "RECONNECT_GRACE_S"]


def timing_info() -> dict:
    return {"slot_s": T_SLOT, "cycle_s": CYCLE_T}
