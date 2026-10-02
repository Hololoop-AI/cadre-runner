"""statusd's proxy to review-surface: what passes through it reaches the
browser when the page server sends it, not when a buffer fills.

No claude, no network, no review-surface CLI."""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import statusd


def test_the_proxy_delivers_a_small_live_event_without_waiting_for_more():
    """review-surface's live channel is a server-sent event stream. The proxy
    read 8 KB before forwarding anything, so a reload or an agent reply — a
    few hundred bytes — never reached a page opened through the fleet."""

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: reloaded\n\n")
            self.wfile.flush()
            time.sleep(3)              # the stream stays open, like the real one

        def log_message(self, *a):
            pass

    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    keep = statusd.SURFACE
    statusd.SURFACE = f"http://127.0.0.1:{up.server_address[1]}"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = socket.create_connection(srv.server_address, timeout=2)
        s.sendall(b"GET /events/k HTTP/1.1\r\nHost: x\r\n\r\n")
        got, start = b"", time.time()
        while b"data: reloaded" not in got and time.time() - start < 2:
            got += s.recv(4096)
        assert b"data: reloaded" in got
        s.close()
    finally:
        statusd.SURFACE = keep
        srv.shutdown()
        up.shutdown()
