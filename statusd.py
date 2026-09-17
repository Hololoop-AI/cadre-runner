#!/usr/bin/env python3
"""Cadre page server: static status page + reverse proxy to Review Surface.

Replaces the bare `python -m http.server` in cadre-status.service, same port,
per cadre-context decision 2026-08-26-surface-as-driver-channel: ONE exposed
port on the tailnet; review-surface keeps its loopback default and every
surface session is reached through this proxy.

Routing rule: a path that resolves to a file in the status directory is
served statically; everything else is forwarded verbatim to the Review Surface
server (`surface.upstream()`, loopback by default) — its pages use root-relative
asset URLs, so prefix-free forwarding is the only shape that works. /shutdown is
blocked: nothing reachable from the network may stop the surface server.

The directory served, the bind address and the port come from the runner's
config (`[runner] data_dir / status_bind / status_port`), with env overrides,
so the page this serves is the page the daemon writes and a host with a network
policy can narrow the bind without a patch.

Streams responses chunk-by-chunk so the surface's SSE channel (/events/:key)
works through the proxy.
"""

import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runnerlib import config as config_mod
from runnerlib import surface as surface_mod


def _runner_config():
    """The daemon's config when there is one. This service is started by hand
    as often as by systemd and must come up either way, so a missing or broken
    config is a fallback to the shipped defaults, never a failure to serve."""
    try:
        return config_mod.load(os.environ.get("CADRE_CONFIG"))
    except SystemExit:
        return None


_CFG = _runner_config()
_DEFAULTS = config_mod.DEFAULTS["runner"]


def _runner(key):
    return _CFG.runner[key] if _CFG else _DEFAULTS[key]


# Env wins over config wins over the defaults: a unit file overrides one dial
# without a config edit, and the config is what keeps the served directory the
# SAME directory the daemon writes its status into (they drifted while this was
# a literal path).
STATUS_DIR = Path(os.environ.get("CADRE_STATUS_DIR") or
                  (_CFG.data_dir / "status" if _CFG
                   else Path(os.path.expanduser(_DEFAULTS["data_dir"])) / "status"))
SURFACE = surface_mod.upstream()
BIND = os.environ.get("CADRE_STATUS_BIND") or _runner("status_bind")
PORT = int(os.environ.get("CADRE_STATUS_PORT") or _runner("status_port"))
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
        # 1xx/204/304 (and HEAD) responses MUST NOT carry a body — chunked
        # framing on them puts stray bytes on the wire and the browser reports
        # ERR_INVALID_HTTP_RESPONSE on the next request (observed).
        bodyless = (resp.status in (204, 304) or resp.status < 200
                    or self.command == "HEAD")
        if bodyless:
            self.end_headers()
            resp.close()
            return
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
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"statusd: serving {STATUS_DIR} on {BIND}:{PORT}, "
          f"proxying the rest to {SURFACE}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
