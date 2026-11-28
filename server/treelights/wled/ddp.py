"""DDP (Distributed Display Protocol) encoding and sending.

Header (10 bytes):
  0  flags: version 1 (0x40), PUSH (0x01)
  1  sequence number (low 4 bits); 0 = not used
  2  data type: 0x0B = RGB, 8 bits per channel
  3  destination id: 1 = default output device
  4  data offset in bytes (uint32, big endian)
  8  data length in bytes (uint16, big endian)

WLED renders when it receives a packet with PUSH set, so the last packet of a
frame carries it. Sequence numbers are left at 0 ("not used") on purpose:
calibration frames are sent twice, and a receiver that drops repeated
sequence numbers would discard the duplicate we rely on.
"""

from __future__ import annotations

import socket
import struct
import time

import numpy as np

DDP_PORT = 4048
FLAG_VERSION_1 = 0x40
FLAG_PUSH = 0x01
DATATYPE_RGB8 = 0x0B
DEST_DEFAULT = 0x01
MAX_PIXELS_PER_PACKET = 480  # 1440 data bytes, the size WLED uses
HEADER = struct.Struct(">BBBBIH")


def encode_frame(rgb: np.ndarray) -> list[bytes]:
    """Split an (N, 3) uint8 frame into DDP packets."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    if rgb.ndim != 2 or rgb.shape[1] != 3:
        raise ValueError("frame must have shape (N, 3)")
    data = rgb.tobytes()
    step = MAX_PIXELS_PER_PACKET * 3
    packets = []
    n_packets = max(1, (len(data) + step - 1) // step)
    for k in range(n_packets):
        chunk = data[k * step : (k + 1) * step]
        flags = FLAG_VERSION_1 | (FLAG_PUSH if k == n_packets - 1 else 0)
        packets.append(HEADER.pack(flags, 0, DATATYPE_RGB8, DEST_DEFAULT, k * step, len(chunk)) + chunk)
    return packets


def decode_packet(packet: bytes) -> tuple[int, int, bytes, bool]:
    """Return (flags, offset, data, push). Used by the simulator and tests."""
    if len(packet) < HEADER.size:
        raise ValueError("short packet")
    flags, _seq, _dtype, _dest, offset, length = HEADER.unpack_from(packet)
    if flags & 0xC0 != FLAG_VERSION_1:
        raise ValueError("unsupported DDP version")
    data = packet[HEADER.size : HEADER.size + length]
    if len(data) != length:
        raise ValueError("truncated packet")
    return flags, offset, data, bool(flags & FLAG_PUSH)


def encode_dnrgb(rgb: np.ndarray, timeout_s: int = 2) -> list[bytes]:
    """WLED UDP realtime DNRGB (port 21324): byte 0 = 4, byte 1 = timeout,
    bytes 2-3 = start index, then RGB triplets; up to 489 LEDs per packet."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    per = 489
    out = []
    for start in range(0, len(rgb), per):
        chunk = rgb[start : start + per].tobytes()
        out.append(bytes([4, max(1, min(255, timeout_s))]) + struct.pack(">H", start) + chunk)
    return out


class UdpFrameSender:
    """Sends frames to one controller. Not thread-safe; owned by the output thread."""

    def __init__(self, host: str, protocol: str = "ddp", port: int | None = None):
        self.host = host
        self.protocol = protocol
        self.port = port or (DDP_PORT if protocol == "ddp" else 21324)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.packets_sent = 0

    def encode(self, rgb: np.ndarray) -> list[bytes]:
        return encode_frame(rgb) if self.protocol == "ddp" else encode_dnrgb(rgb)

    def send_raw(self, packet: bytes) -> None:
        try:
            self.sock.sendto(packet, (self.host, self.port))
            self.packets_sent += 1
        except OSError:
            pass  # network errors surface through the monitor

    def send(self, rgb: np.ndarray, repeats: int = 1, gap_s: float = 0.002) -> None:
        packets = self.encode(rgb)
        for r in range(repeats):
            if r:
                time.sleep(gap_s)
            for i, p in enumerate(packets):
                if i == len(packets) - 1 and len(packets) > 1:
                    # Give the earlier packets a head start so the PUSH packet
                    # is unlikely to overtake them.
                    time.sleep(0.0005)
                self.send_raw(p)

    def close(self) -> None:
        self.sock.close()
