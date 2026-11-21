"""Plain-HTTP setup page on port 8080 (architecture §6.3).

It only shows how to reach the HTTPS app, offers the optional CA certificate,
and redirects everything else to HTTPS.
"""

from __future__ import annotations

import html
from pathlib import Path

import segno
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import tls


def app_urls(ips: list[str], port: int) -> list[str]:
    return [f"https://{ip}:{port}/" for ip in ips]


def qr_svg(text: str) -> str:
    return segno.make(text, error="m").svg_inline(scale=5, dark="#000", light="#fff")


def qr_terminal(text: str) -> str:
    import io

    buf = io.StringIO()
    segno.make(text, error="m").terminal(out=buf, compact=True)
    return buf.getvalue()


def create_bootstrap_app(tls_dir: Path, ips: list[str], https_port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    urls = app_urls(ips, https_port)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        items = "".join(
            f'<li><a href="{html.escape(u)}">{html.escape(u)}</a><div class="qr">{qr_svg(u)}</div></li>' for u in urls
        ) or "<li>No LAN address found. Set TREELIGHTS_HOST_IPS.</li>"
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TreeLights setup</title>
<style>body{{font-family:system-ui,sans-serif;max-width:40rem;margin:1rem auto;padding:0 1rem;line-height:1.5}}
.qr svg{{width:12rem;height:auto}} li{{margin-bottom:1rem}} code{{background:#eee;padding:0 .2rem}}</style></head>
<body><h1>TreeLights</h1>
<p>Open the app over HTTPS. Chrome needs HTTPS to use the camera.</p>
<ol>{items}</ol>
<h2>The certificate warning</h2>
<p>Chrome will warn that the connection isn't private because this server's certificate comes from
a local authority, not a public one. Tap <b>Advanced</b>, then <b>Proceed</b>. Chrome asks again
when the page is opened later, not while the camera is running.</p>
<h2>Optional: remove the warning</h2>
<p>Download the <a href="/ca.crt">TreeLights CA certificate</a> and install it on the phone:
Settings → Security → Encryption &amp; credentials → Install a certificate → CA certificate
(the wording varies by Android version). It only covers private LAN addresses.</p>
</body></html>"""

    @app.get("/ca.crt")
    def ca() -> Response:
        return Response(tls.ca_der(tls_dir), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="treelights-ca.crt"'})

    @app.get("/{path:path}")
    def redirect(path: str, request: Request) -> RedirectResponse:
        host = request.url.hostname or (ips[0] if ips else "localhost")
        return RedirectResponse(f"https://{host}:{https_port}/{path}")

    return app
