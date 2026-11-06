"""What the React fleet app (web/) needs from the Python side: the fleet
page's own stylesheet, copied verbatim; GET /api/newwork; archived rows left
out of /api/fleet; JSON answers to form posts; and how /app/ is served.
Run directly: python3 -m pytest -q tests/test_web_app.py"""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web" / "scripts"))

import statusd
import sync_fleet_css


def test_the_react_app_uses_the_fleet_pages_stylesheet_verbatim():
    have = (ROOT / "web" / "src" / "ui" / "fleet.css").read_text(encoding="utf-8")
    assert have == sync_fleet_css.css(), (
        "web/src/ui/fleet.css is stale: run python3 web/scripts/sync_fleet_css.py")


def test_run_library_is_what_the_picker_offers_broken_entries_left_out():
    entries = [{"kind": "skill", "name": "Zed", "invoke": "/zed", "description": "z" * 200},
               {"kind": "agent", "name": "build", "invoke": "@agent-build"},
               {"kind": "skill", "name": "gone", "invoke": "/gone", "broken": True}]
    lib = statusd.run_library(entries)
    assert [e["v"] for e in lib] == ["agent:build", "skill:Zed"]
    assert lib[1] == {"v": "skill:Zed", "i": "/zed", "k": "skill", "d": "z" * 120}
    # the Python page's picker inlines exactly this list
    assert json.dumps(lib, ensure_ascii=False) in statusd._run_picker(entries)


def test_api_newwork_serves_home_and_the_library(monkeypatch):
    monkeypatch.setattr(statusd.library_mod, "scan", lambda **kw: [
        {"kind": "skill", "name": "review", "invoke": "/review", "description": "d"}])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}{statusd.NEWWORK_API_PATH}"
        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.headers["Content-Type"] == "application/json"
            body = json.loads(r.read())
    finally:
        srv.shutdown()
        srv.server_close()
    assert body == {"home": str(Path.home()),
                    "library": [{"v": "skill:review", "i": "/review", "k": "skill", "d": "d"}]}


def test_fleet_payload_leaves_out_rows_archived_here(monkeypatch):
    monkeypatch.setattr(statusd, "_ARCHIVED", {"aaaa"})
    monkeypatch.setattr(statusd, "read_snapshot", lambda *a: {"surfaces": [
        {"path": "/session/aaaa", "title": "archived"}, {"path": "/session/bbbb", "title": "kept"}]})
    for name in ("load_projects", "starting_tasks", "live_turns", "closed_pages", "fetch_agent_statuses"):
        monkeypatch.setattr(statusd, name, lambda *a, **k: {})
    monkeypatch.setattr(statusd, "pageless_tasks", lambda *a, **k: [])
    monkeypatch.setattr(statusd, "load_graph", lambda: ([], {}, {}))
    monkeypatch.setattr(statusd, "load_runs", lambda **k: ([], {}, {}))
    monkeypatch.setattr(statusd, "load_tree", lambda *a: {})
    monkeypatch.setattr(statusd, "running_agents", lambda **k: [])
    monkeypatch.setattr(statusd, "fleet_version", lambda: {})
    surfaces = statusd.fleet_payload()["snap"]["surfaces"]
    assert [sf["title"] for sf in surfaces] == ["kept"]


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_a_form_post_asking_for_json_gets_the_notice_instead_of_a_redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(statusd, "DATA_DIR", tmp_path)
    srv, url = _serve()
    try:
        body = b"item=task-x&action=tag&tag=cadre"
        plain = urllib.request.Request(url + "/items", data=body, method="POST")
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                                  {"redirect_request": lambda *a, **k: None}))
        try:
            opener.open(plain, timeout=5)
            raise AssertionError("expected a 303")
        except urllib.error.HTTPError as e:
            assert e.code == 303 and e.headers["Location"] == "/?notice=Tagged+cadre."
        asked = urllib.request.Request(url + "/items", data=b"item=task-x&action=untag&tag=cadre",
                                       method="POST", headers={"Accept": "application/json"})
        with urllib.request.urlopen(asked, timeout=5) as r:
            assert json.loads(r.read()) == {"notice": "Removed tag cadre.", "location": "/?notice=Removed+tag+cadre."}
    finally:
        srv.shutdown()
        srv.server_close()


def test_app_serves_its_routes_but_404s_a_missing_file_and_caches_hashed_assets(monkeypatch, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html>app")
    (tmp_path / "assets" / "index-abc.js").write_text("js")
    monkeypatch.setattr(statusd, "APP_DIR", tmp_path)
    srv, url = _serve()
    try:
        with urllib.request.urlopen(url + "/app/some/route", timeout=5) as r:
            assert r.read() == b"<!doctype html>app" and r.headers["Cache-Control"] == "no-store"
        with urllib.request.urlopen(url + "/app/assets/index-abc.js", timeout=5) as r:
            assert "immutable" in r.headers["Cache-Control"]
        try:
            urllib.request.urlopen(url + "/app/assets/index-OLD.js", timeout=5)
            raise AssertionError("expected a 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_configured_machines_page_may_read_and_post_here_and_no_other_origin(monkeypatch, tmp_path):
    """The React app switches between machines and talks to each directly
    (decided 2026-11-06), so a configured backend's origin gets CORS on the
    API and may post forms; every other origin is refused as before."""
    monkeypatch.setattr(statusd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(statusd, "CORS_ORIGINS", {"http://laptop.tailnet:8183"})
    srv, url = _serve()
    try:
        def get(origin):
            req = urllib.request.Request(url + statusd.BACKENDS_API_PATH, headers={"Origin": origin})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.headers.get("Access-Control-Allow-Origin"), json.loads(r.read())

        allowed, body = get("http://laptop.tailnet:8183")
        assert allowed == "http://laptop.tailnet:8183" and body["backends"][0]["url"] == ""
        assert get("http://evil.example")[0] is None

        def post(origin):
            req = urllib.request.Request(url + "/items", data=b"item=task-x&action=tag&tag=cadre", method="POST",
                                         headers={"Origin": origin, "Accept": "application/json"})
            return urllib.request.urlopen(req, timeout=5)

        with post("http://laptop.tailnet:8183") as r:
            assert json.loads(r.read())["notice"] == "Tagged cadre."
        try:
            post("http://evil.example")
            raise AssertionError("expected a 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_backend_origins_come_from_the_configured_urls():
    from runnerlib import backends as backends_mod
    assert backends_mod.origins([{"url": None}, {"url": "http://box.ts.net:8183/"},
                                 {"url": "https://team.example/fleet"}, {"url": "ftp://x"}]) == \
        {"http://box.ts.net:8183", "https://team.example"}
