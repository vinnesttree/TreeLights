"""Local network helpers."""

from __future__ import annotations

import fcntl
import ipaddress
import os
import socket
import struct

SIOCGIFADDR = 0x8915


def local_ipv4_addresses() -> list[str]:
    """IPv4 addresses of this host's interfaces (Linux), loopback excluded.

    TREELIGHTS_HOST_IPS (comma separated) is added, for setups where the
    container can't see the host's interfaces.
    """
    ips: list[str] = []
    try:
        names = [n for _i, n in socket.if_nameindex()]
    except OSError:
        names = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            try:
                packed = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
                ip = socket.inet_ntoa(packed[20:24])
            except OSError:
                continue
            if not ipaddress.ip_address(ip).is_loopback and ip not in ips:
                ips.append(ip)
    finally:
        s.close()
    for extra in os.environ.get("TREELIGHTS_HOST_IPS", "").split(","):
        extra = extra.strip()
        if extra:
            try:
                ipaddress.ip_address(extra)
            except ValueError:
                continue
            if extra not in ips:
                ips.append(extra)
    return ips


def source_ip_for(dest: str) -> str | None:
    """The local address the kernel would use to reach dest (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False
