"""What keeps the fleet fast over a slow link: /api/fleet built once per
version (not per request), text answers gzipped for a browser that takes it,
and the home page carrying its first payload inside index.html.
Run directly: python3 -m pytest -q tests/test_fleet_speed.py"""

import gzip
import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import statusd


@pytest.fixture
def builds(monkeypatch):
    """fleet_payload counted, with a version the test moves by hand."""
    state = {"version": "v1", "builds": 0}

    def payload():
        state["builds"] += 1
        return {"version": state["version"], "snap": {"surfaces": [{"title": "a </script> <b>"}] * 40}}

    monkeypatch.setattr(statusd, "fleet_version", lambda: {"version": state["version"]})
    monkeypatch.setattr(statusd, "fleet_payload", payload)
    monkeypatch.setattr(statusd, "_PAYLOAD", {"version": None, "at": 0.0, "body": b"", "gz": b""})
    return state


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _get(url, gz=False):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"} if gz else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.headers, r.read()


def test_the_payload_is_built_once_per_version_and_again_when_it_moves_or_ages(builds):
    body, gz = statusd.fleet_payload_bytes(now=1000.0)
    assert json.loads(body)["version"] == "v1" and gzip.decompress(gz) == body
    statusd.fleet_payload_bytes(now=1001.0)
    assert builds["builds"] == 1, "same version, young copy: no rebuild"
    builds["version"] = "v2"
    assert json.loads(statusd.fleet_payload_bytes(now=1002.0)[0])["version"] == "v2"
    statusd.fleet_payload_bytes(now=1002.0 + statusd.PAYLOAD_TTL)
    assert builds["builds"] == 3, "a copy PAYLOAD_TTL old is rebuilt though the version held"
    statusd.forget_payload()
    statusd.fleet_payload_bytes(now=1003.0 + statusd.PAYLOAD_TTL)
    assert builds["builds"] == 4, "a write through this server drops the copy"


def test_api_fleet_is_gzipped_only_for_a_browser_that_takes_it(builds):
    srv, url = _serve()
    try:
        plain_headers, plain = _get(url + "/api/fleet")
        gz_headers, squeezed = _get(url + "/api/fleet", gz=True)
    finally:
        srv.shutdown()
        srv.server_close()
    assert plain_headers.get("Content-Encoding") is None
    assert gz_headers["Content-Encoding"] == "gzip" and "Accept-Encoding" in gz_headers["Vary"]
    assert gzip.decompress(squeezed) == plain and json.loads(plain)["version"] == "v1"
    assert builds["builds"] == 1, "two requests, one build"


def test_gzip_refused_by_q0_is_not_sent():
    class H:
        def __init__(self, ae):
            self.headers = {"Accept-Encoding": ae}

    take = statusd.Handler._accepts_gzip
    assert take(H("gzip, deflate, br")) and take(H("br;q=1, gzip;q=0.5")) and take(H("*"))
    assert not take(H("gzip;q=0")) and not take(H("br")) and not take(H(""))


def test_home_carries_its_first_payload_inside_index_html(builds, monkeypatch, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><head><title>x</title></head><body></body>")
    (tmp_path / "assets" / "index-abc.js").write_text("console.log(1);" * 200)
    monkeypatch.setattr(statusd, "APP_DIR", tmp_path)
    monkeypatch.setattr(statusd, "FLEET_UI", "react")
    srv, url = _serve()
    try:
        _, home = _get(url + "/")
        headers, other = _get(url + "/library")
        js_headers, js = _get(url + "/app/assets/index-abc.js", gz=True)
    finally:
        srv.shutdown()
        srv.server_close()
    tag = b'<script type="application/json" id="cadre-fleet">'
    assert tag in home and home.index(tag) < home.index(b"</head>")
    inline = home.split(tag, 1)[1].split(b"</script>", 1)[0]
    assert b"<" not in inline, "a title cannot close the tag"
    assert json.loads(inline)["snap"]["surfaces"][0]["title"] == "a </script> <b>"
    assert tag not in other, "only home carries it; the other screens fetch their own"
    assert js_headers["Content-Encoding"] == "gzip" and "immutable" in js_headers["Cache-Control"]
    assert gzip.decompress(js) == b"console.log(1);" * 200


def test_agent_statuses_are_asked_together_and_a_failed_key_is_left_out(monkeypatch):
    seen, both_in = [], threading.Barrier(2, timeout=5)

    class Resp:
        def __init__(self, key):
            self.key = key

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"status": "open", "key": self.key}).encode()

    def urlopen(url, timeout):
        key = url.rsplit("/", 2)[-2]
        seen.append(key)
        if key in ("a", "b"):
            both_in.wait()       # a and b are in flight at once, or this times out
        if key == "c":
            raise OSError("refused")
        return Resp(key)

    monkeypatch.setattr(statusd.urllib.request, "urlopen", urlopen)
    out = statusd.fetch_agent_statuses([{"path": "/session/a"}, {"path": "/session/b"},
                                        {"path": "/session/c"}, {"path": ""}], base="http://x")
    assert sorted(out) == ["a", "b"] and out["a"]["key"] == "a"
    assert sorted(seen) == ["a", "b", "c"]
