#!/usr/bin/env python3
"""Cadre page server: static status page + reverse proxy to Review Surface.

Replaces the bare `python -m http.server` in cadre-status.service, same port,
per cadre-context decision 2026-08-26-surface-as-driver-channel: ONE exposed
port on the tailnet; review-surface keeps its loopback default and every
surface session is reached through this proxy.

Routing rule: a path that resolves to a file in the status directory is
served statically; everything else is forwarded verbatim to the local Review
Surface server (127.0.0.1:4387) — its pages use root-relative asset URLs, so
prefix-free forwarding is the only shape that works. /shutdown is blocked:
nothing reachable from the network may stop the surface server.

Streams responses chunk-by-chunk so the surface's SSE channel (/events/:key)
works through the proxy.
"""

import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATUS_DIR = Path(os.environ.get(
    "CADRE_STATUS_DIR", str(Path.home() / ".local/state/pipeline-runner/status")))
SURFACE = os.environ.get("CADRE_SURFACE_UPSTREAM", "http://127.0.0.1:4387")
PORT = int(os.environ.get("CADRE_STATUS_PORT", "8181"))
BLOCKED = {"/shutdown"}
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "host",
               "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "upgrade"}
TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json",
         ".css": "text/css", ".js": "text/javascript", ".png": "image/png",
         ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # journald noise control
        pass

    def _static_path(self):
        rel = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        p = (STATUS_DIR / rel).resolve()
        if p.is_relative_to(STATUS_DIR.resolve()) and p.is_file():
            return p
        return None

    def _serve_static(self, p: Path):
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(p.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _proxy(self):
        if self.path.split("?", 1)[0] in BLOCKED:
            self.send_error(403, "blocked at the proxy")
            return
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)
        req = urllib.request.Request(SURFACE + self.path, data=body,
                                     method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in HOP_HEADERS:
                req.add_header(k, v)
        # Standard proxy identity: review-surface's origin guard validates the
        # browser's Origin against X-Forwarded-Host when that hostname is in
        # its REVIEW_SURFACE_ALLOWED_HOSTS list (set in the daemon's env).
        if self.headers.get("Host"):
            req.add_header("X-Forwarded-Host", self.headers["Host"])
            req.add_header("X-Forwarded-Proto", "http")
        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            resp = e
        except OSError:
            self.send_error(502, "review-surface is not running")
            return
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() not in HOP_HEADERS:
                self.send_header(k, v)
        chunked = "content-length" not in {k.lower() for k in resp.headers}
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()  # SSE: deliver events as they arrive
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()

    def _route(self):
        p = self._static_path()
        if p is not None:
            self._serve_static(p)
        else:
            self._proxy()

    do_GET = do_POST = do_PUT = do_DELETE = _route


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"statusd: serving {STATUS_DIR} on :{PORT}, proxying the rest to {SURFACE}",
          file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
