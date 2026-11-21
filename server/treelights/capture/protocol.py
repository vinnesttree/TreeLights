"""Capture WebSocket protocol (architecture §7.6).

Server -> camera (JSON text):
  welcome   {session, scan, settings}
  capture   {step, kind, wait_ms}           answer with exactly one frame
  probe_mark{id, k, on}                     latency probe: LEDs just changed
  status    {...}                           progress for the UI
Camera -> server:
  hello     {role:"camera", session?, camera:{width,height,deviceId,locks}, up_cam:[x,y,z]|null}
  hb        {up_cam?}
  motion    {magnitude}                     rotation-rate spike: phone bumped
  probe_result {id, delays_ms:[...]}
  frame     binary: u32 big-endian header length, JSON header {step, kind}, JPEG bytes
Control clients (any browser) send {type:"start_view"|"stop"|"resume"} on /ws/control.
"""

from __future__ import annotations

import json
import struct


def pack_frame(header: dict, jpeg: bytes) -> bytes:
    h = json.dumps(header).encode()
    return struct.pack(">I", len(h)) + h + jpeg


def unpack_frame(data: bytes) -> tuple[dict, bytes]:
    if len(data) < 4:
        raise ValueError("short frame message")
    (n,) = struct.unpack_from(">I", data)
    if n > 4096 or 4 + n > len(data):
        raise ValueError("bad frame header")
    return json.loads(data[4 : 4 + n]), data[4 + n :]
