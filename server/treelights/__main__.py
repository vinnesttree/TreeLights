"""Run TreeLights: the HTTPS app (default port 8443) plus the plain-HTTP setup page
(default 8080). Ports: --https-port/--http-port, else TREELIGHTS_HTTPS_PORT /
TREELIGHTS_HTTP_PORT, else the config file, else the defaults."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from pathlib import Path

import uvicorn

from . import bootstrap, netutil, tls
from .app import AppState, create_app
from .config import data_dir


def _env_port(name: str) -> int | None:
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    if not v.isdigit() or not 1 <= int(v) <= 65535:
        raise SystemExit(f"{name}={v!r} is not a port number (1-65535).")
    return int(v)


def _check_free(host: str, port: int, what: str) -> None:
    """Fail clearly if the port is taken (with host networking, by another service on the host)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            raise SystemExit(f"Port {port} for the {what} is already in use on this host ({e.strerror}). "
                             f"Choose another one: TREELIGHTS_{'HTTPS' if what == 'app' else 'HTTP'}_PORT "
                             f"(in Docker Compose / Dockge: HTTPS_PORT / HTTP_PORT).")


async def serve(args: argparse.Namespace) -> None:
    root = Path(args.data or data_dir())
    root.mkdir(parents=True, exist_ok=True)
    state = AppState(root)
    s = state.config.settings
    web = Path(args.web or os.environ.get("TREELIGHTS_WEB", Path(__file__).resolve().parents[2] / "web" / "dist"))
    app = create_app(state, web)
    ips = netutil.local_ipv4_addresses()
    https_port = args.https_port or _env_port("TREELIGHTS_HTTPS_PORT") or s.https_port
    http_port = args.http_port or _env_port("TREELIGHTS_HTTP_PORT") or s.http_port
    if not args.no_tls and https_port == http_port:
        raise SystemExit(f"The app and the setup page need different ports (both are {https_port}).")
    _check_free(args.host, https_port, "app")
    if not args.no_tls:
        _check_free(args.host, http_port, "setup page")
    servers = []
    if args.no_tls:
        servers.append(uvicorn.Server(uvicorn.Config(app, host=args.host, port=https_port, log_level="info", ws_max_size=16 * 2**20)))
        urls = [f"http://{ip}:{https_port}/" for ip in ips]
    else:
        cert, key = tls.ensure_server_cert(root / "tls", ips, [socket.gethostname()])
        servers.append(uvicorn.Server(uvicorn.Config(app, host=args.host, port=https_port, ssl_certfile=str(cert),
                                                     ssl_keyfile=str(key), log_level="info", ws_max_size=16 * 2**20)))
        boot = bootstrap.create_bootstrap_app(root / "tls", ips, https_port)
        servers.append(uvicorn.Server(uvicorn.Config(boot, host=args.host, port=http_port, log_level="warning")))
        urls = bootstrap.app_urls(ips, https_port)
    print("\nTreeLights is running. Open on your phone:")
    for u in urls:
        print("  " + u)
    if urls and not args.no_tls:
        print(f"\nSetup help (no HTTPS needed): http://{ips[0]}:{http_port}/\n")
        print(bootstrap.qr_terminal(urls[0]))
    await asyncio.gather(*(srv.serve() for srv in servers))


def main() -> None:
    p = argparse.ArgumentParser(prog="treelights")
    p.add_argument("--data", help="data directory (default $TREELIGHTS_DATA or /data)")
    p.add_argument("--web", help="built web app directory")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--https-port", type=int)
    p.add_argument("--http-port", type=int)
    p.add_argument("--no-tls", action="store_true", help="plain HTTP on the app port (development only)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
