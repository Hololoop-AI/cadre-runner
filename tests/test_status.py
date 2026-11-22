"""Smoke tests for the status snapshot writer and the fleet home page statusd
renders from it. Run directly: python3 tests/test_status.py"""

import json
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runnerlib
import statusd
from runnerlib import messages, status


class FakeCfg:
    def __init__(self, root):
        self.data_dir = Path(root)
        self.runner = {"poll_interval": 45}


def test_write_and_shape():
    with tempfile.TemporaryDirectory() as root:
        cfg = FakeCfg(root)
        status.write_status(cfg)
        snap = json.loads((cfg.data_dir / "status" / "status.json").read_text())
        assert snap["poll_interval"] == 45 and snap["ts"] > 0
        # the PR pipeline's story rows and stage runs are gone from the snapshot
        assert not {"stories", "run", "runs"} & set(snap)
        assert snap["surfaces"] == [] and snap["questions"] == []


def test_log_tail_bounded():
    with tempfile.TemporaryDirectory() as root:
        log = Path(root) / "daemon.log"
        log.write_text("\n".join(f"line {i}" for i in range(5000)))
        tail = status._log_tail(log)
        assert len(tail) == status.LOG_TAIL_LINES
        assert tail[-1] == "line 4999"
        assert status._log_tail(Path(root) / "missing.log") == []


def test_never_raises():
    class BrokenCfg:
        data_dir = Path("/nonexistent")

        @property
        def runner(self):
            raise RuntimeError("boom")
    status.write_status(BrokenCfg())  # must not raise


# ------------------------------------------------------------- fleet page

NOW = 1_700_000_000.0


def _fleet_snapshot(root):
    """A real snapshot: written by status.write_status, with surface sessions
    and a question seeded into the data dir the way the daemon seeds them."""
    cfg = FakeCfg(root)
    surf_dir = cfg.data_dir / "surfaces"
    surf_dir.mkdir(parents=True, exist_ok=True)
    (surf_dir / "sessions.json").write_text(json.dumps({
        "/b.html": {"kind": "notice", "story": "busy", "path": "/s/zzz",
                    "key": "zzz", "open": True, "opened": NOW - 10},
        "/c.html": {"kind": "ask", "story": "gone", "path": "/s/orph",
                    "key": "orph", "open": True, "opened": NOW - 5},
    }))
    messages.ask(cfg.data_dir, "esc", "Which way?")
    status.write_status(cfg)
    return json.loads((cfg.data_dir / "status" / "status.json").read_text())


def test_snapshot_survives_a_missing_or_broken_file():
    with tempfile.TemporaryDirectory() as root:
        assert statusd.read_snapshot(Path(root)) == {}
        d = Path(root) / "status"
        d.mkdir()
        (d / "status.json").write_text("{not json")
        assert statusd.read_snapshot(d.parent) == {}


# ------------------------------------------------------------- http surface

class _Server:
    """statusd's real handler on an ephemeral loopback port."""

    def __init__(self, status_dir):
        self.keep = statusd.STATUS_DIR, statusd.DATA_DIR
        statusd.STATUS_DIR = Path(status_dir)
        # the project store and session store beside it, never the live ones
        statusd.DATA_DIR = Path(status_dir).parent
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        statusd.STATUS_DIR, statusd.DATA_DIR = self.keep

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.read().decode()

    def post(self, path, fields, headers=None):
        req = urllib.request.Request(
            self.base + path, method="POST",
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     **(headers or {})})
        try:
            # no redirect following: the Location IS the result under test
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=5) as r:
                return r.status, r.headers.get("Location") or ""
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location") or ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _hide_tasks():
    """Make `from runnerlib import tasks` raise ImportError, whether or not the
    real module is installed — the page must degrade to a notice either way."""
    sys.modules["runnerlib.tasks"] = None
    if hasattr(runnerlib, "tasks"):
        del runnerlib.tasks


def _stub_tasks(calls, exc=None):
    """runnerlib.tasks is owned by another workstream — these tests must never
    depend on it being importable, nor call the real submitter (which writes a
    task onto the live board)."""
    mod = types.ModuleType("runnerlib.tasks")

    def submit_task(cfg, text, cwd):
        calls.append((cfg, text, cwd))
        if exc:
            raise exc
        return "task-1"

    mod.submit_task = submit_task
    sys.modules["runnerlib.tasks"] = mod
    runnerlib.tasks = mod


def _unstub_tasks():
    sys.modules.pop("runnerlib.tasks", None)
    if hasattr(runnerlib, "tasks"):
        del runnerlib.tasks


def test_new_task_endpoint_calls_submit_task():
    calls = []
    _stub_tasks(calls)
    with tempfile.TemporaryDirectory() as root:
        srv = _Server(Path(root) / "status")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            req = urllib.request.Request(
                srv.base + statusd.TASKS_PATH, method="POST",
                data=urllib.parse.urlencode(
                    {"text": "ship the thing", "cwd": "/tmp/repo"}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                opener.open(req, timeout=5)
                raise AssertionError("expected a redirect")
            except urllib.error.HTTPError as e:
                assert e.code == 303
                assert e.headers["Location"].startswith("/?notice=")
                assert "task-1" in urllib.parse.unquote_plus(e.headers["Location"])
            assert len(calls) == 1
            _cfg, text, cwd = calls[0]
            assert text == "ship the thing" and cwd == "/tmp/repo"
        finally:
            srv.close()
            _unstub_tasks()


def test_new_task_blank_text_and_missing_module_and_failures_are_notices():
    with tempfile.TemporaryDirectory() as root:
        srv = _Server(Path(root) / "status")
        try:
            _hide_tasks()
            code, loc = srv.post(statusd.TASKS_PATH, {"text": "   ", "cwd": ""})
            assert code == 303 and "needs+some+text" in loc

            code, loc = srv.post(statusd.TASKS_PATH, {"text": "do it"})
            assert code == 303 and "not+installed" in loc

            calls = []
            _stub_tasks(calls, exc=ValueError("no such directory"))
            code, loc = srv.post(statusd.TASKS_PATH, {"text": "do it", "cwd": "/nope"})
            assert code == 303 and "no+such+directory" in loc
            assert calls and calls[0][2] == "/nope"

            # cwd is optional: an empty field reaches submit_task as None
            calls.clear()
            _stub_tasks(calls)
            srv.post(statusd.TASKS_PATH, {"text": "do it", "cwd": ""})
            assert calls[0][2] is None
        finally:
            srv.close()
            _unstub_tasks()


def test_cross_origin_post_is_refused():
    calls = []
    _stub_tasks(calls)
    with tempfile.TemporaryDirectory() as root:
        srv = _Server(Path(root) / "status")
        try:
            code, _ = srv.post(statusd.TASKS_PATH, {"text": "x"},
                               headers={"Origin": "http://evil.example"})
            assert code == 403 and not calls
        finally:
            srv.close()
            _unstub_tasks()


def test_legacy_dashboard_is_still_static():
    with tempfile.TemporaryDirectory() as root:
        d = Path(root) / "status"
        d.mkdir(parents=True)
        (d / "index.html").write_text("<p>legacy</p>")
        srv = _Server(d)
        try:
            assert srv.get("/index.html") == (200, "<p>legacy</p>")
            try:                                       # / is the fleet app now
                home = srv.get("/")[1]
            except urllib.error.HTTPError as e:        # (no React build in this checkout)
                home = e.read().decode()
            assert "<p>legacy</p>" not in home
        finally:
            srv.close()


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("status smoke tests: all passed")


def test_register_external_without_a_page_is_presence_only():
    """A terminal session has no artifact: the record still lands, flagged
    kind=external so no runner consumer ever polls (poll delivery consumes —
    stealing the registrar's feedback is the review-surface loss bug again)."""
    from runnerlib import surface as surface_mod

    with tempfile.TemporaryDirectory() as d:
        cfg = FakeCfg(d)
        surface_mod.register_external(cfg, None, "hitl", "orchestrator",
                                      role="orchestrator", log=lambda *a: None)
        rows = surface_mod.status_list(cfg)
        assert [(r["kind"], r["project"], r["title"], r["role"]) for r in rows] == [
            ("external", "hitl", "orchestrator", "orchestrator")]


def test_tick_never_consumes_an_external_session(monkeypatch=None):
    """The outbox wake for an external key must fall through: the session
    belongs to whoever registered it, and a poll here would consume the
    driver's feedback out from under that owner's own loop."""
    from runnerlib import surface as surface_mod

    with tempfile.TemporaryDirectory() as d:
        cfg = FakeCfg(d)
        surface_mod._save_sessions(cfg, {
            "/x/ext.html": {"kind": "external", "key": "extkey", "open": True},
            "/x/task.html": {"kind": "task", "key": "taskkey", "open": True},
        })
        consumed = []
        orig_consume = surface_mod._consume
        orig_outbox = surface_mod._read_outbox
        orig_sweep = surface_mod._sweep_stale
        orig_server = surface_mod._ensure_server
        surface_mod._consume = lambda *a: consumed.append(a[3])
        surface_mod._read_outbox = lambda cfg: [{"key": "extkey"},
                                                {"key": "taskkey"}]
        surface_mod._sweep_stale = lambda *a: None
        surface_mod._ensure_server = lambda *a: None
        try:
            surface_mod._tick(cfg, types.SimpleNamespace(data={}), lambda *a: None)
        finally:
            surface_mod._consume = orig_consume
            surface_mod._read_outbox = orig_outbox
            surface_mod._sweep_stale = orig_sweep
            surface_mod._ensure_server = orig_server
        assert consumed == ["/x/task.html"]


def test_sweep_leaves_a_pageless_external_alone():
    """The orchestrator row is keyed synthetically (external:project:title) —
    the sweep's file-existence test read that key as a deleted artifact and
    closed the row on the daemon's first pass (observed live, 2026-09-20)."""
    from runnerlib import surface as surface_mod

    with tempfile.TemporaryDirectory() as d:
        cfg = FakeCfg(d)
        surface_mod.register_external(cfg, None, "hitl", "orchestrator",
                                      role="orchestrator", log=lambda *a: None)
        surface_mod._sweep_stale(cfg, None, lambda *a: None)
        assert [r["title"] for r in surface_mod.status_list(cfg)] == ["orchestrator"]


def test_history_renders_journal_batches_newest_first():
    with tempfile.TemporaryDirectory() as d:
        artifact = str(Path(d) / "art.html")
        # the journal lives in review-surface's STATE dir, one file for all
        # sessions, records filtered by their artifact path
        journal = Path(d) / "feedback-journal.jsonl"
        journal.write_text(
            json.dumps({"at": "2026-09-20T01:00:00Z", "file": artifact,
                        "prompts": [{"tag": "decision", "prompt": "round one answer"}]}) + "\n"
            + json.dumps({"at": "2026-09-20T02:00:00Z", "file": str(Path(d) / "other.html"),
                          "prompts": [{"prompt": "different surface"}]}) + "\n"
            + json.dumps({"at": "2026-09-20T03:00:00Z", "file": artifact, "end_session": True,
                          "prompts": [{"tag": "note", "prompt": "final word"}]}) + "\n")
        batches = statusd.journal_batches(artifact, state_dir=Path(d))
        assert [b["at"] for b in batches] == ["2026-09-20T01:00:00Z", "2026-09-20T03:00:00Z"]

        html = statusd.render_history({"title": "d2 — store", "path": "/session/k1"}, batches)
        # newest first, rounds numbered, the other surface's batch excluded
        assert html.index("round 2") < html.index("round 1")
        assert "final word" in html and "round one answer" in html
        assert "different surface" not in html
        assert "session ended" in html


def test_journal_is_parsed_once_per_change_and_per_concurrent_burst():
    with tempfile.TemporaryDirectory() as d:
        journal = Path(d) / "feedback-journal.jsonl"
        journal.write_text(json.dumps({"file": "a.html", "at": "1"}) + "\n"
                           + json.dumps({"file": "b.html", "at": "2"}) + "\n")
        real_read, reads = Path.read_text, []

        def counting_read(self, *a, **k):
            if self == journal:
                reads.append(1)
                time.sleep(0.05)        # long enough for the burst to overlap
            return real_read(self, *a, **k)

        Path.read_text = counting_read
        try:
            threads = [threading.Thread(target=statusd.journal_batches,
                                        args=("a.html", Path(d))) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert [b["at"] for b in statusd.journal_batches("b.html", Path(d))] == ["2"]
            assert len(reads) == 1

            # an accepted batch shows up on the next read
            with journal.open("a") as f:
                f.write(json.dumps({"file": "a.html", "at": "3"}) + "\n")
            assert [b["at"] for b in statusd.journal_batches("a.html", Path(d))] == ["1", "3"]
            assert len(reads) == 2

            # a caller's list is its own, and a vanished journal reads as empty
            statusd.journal_batches("a.html", Path(d)).clear()
            assert len(statusd.journal_batches("a.html", Path(d))) == 2
            journal.unlink()
            assert statusd.journal_batches("a.html", Path(d)) == []
        finally:
            Path.read_text = real_read


def test_dead_letter_page_serves_the_batch_verbatim_and_refuses_other_paths():
    with tempfile.TemporaryDirectory() as root:
        _fleet_snapshot(root)
        sdir = Path(root) / "surfaces" / "stranded"
        sdir.mkdir(parents=True)
        long_answer = "keep the fold per project " + "y" * 2500
        (sdir / "20260921-1-d7.json").write_text(json.dumps({
            "at": 0, "artifact": "/x/hitl-d7.html",
            "reason": "external session with no task id",
            "session": {"title": "d7 — workspace", "path": "/session/d7"},
            "prompts": [{"prompt": long_answer, "tag": "p", "text": "Which grouping?"}],
            "raw": '{"prompts": []}'}))
        (Path(root) / "secret.json").write_text("{}")
        keep = statusd.STRANDED_DIR
        statusd.STRANDED_DIR = sdir
        srv = _Server(Path(root) / "status")
        try:
            code, body = srv.get("/stranded/20260921-1-d7.json")
            assert code == 200 and long_answer in body       # nothing truncated
            assert "Which grouping?" in body and "no task id" in body
            assert 'href="/view/d7"' in body
            for bad in ("/stranded/..%2Fsecret.json", "/stranded/nope.json",
                        "/stranded/.hidden.json", "/stranded/x.txt"):
                try:
                    srv.get(bad)
                    assert False, bad
                except urllib.error.HTTPError as e:
                    assert e.code == 404, bad
        finally:
            srv.close()
            statusd.STRANDED_DIR = keep


# ------------------------------------------------------------- surface view
# A surface opens inside the fleet's frame, with the way back in the frame's
# bar. Opening review-surface's page as the whole tab left only the browser's
# back button.

def test_session_links_point_at_the_framed_view():
    assert statusd.view_href("/session/k1") == "/view/k1"
    assert statusd.view_href("/page/k1") == "/page/k1"      # not a surface
    assert statusd.view_href(None) == ""


def test_the_frame_reads_its_session_and_title_and_refuses_a_bad_key():
    """/view/<key> is the React app's screen; what it reads is /api/view/<key>."""
    assert statusd.REACT_ROUTE.match("/view/ef6c7a101f9b55b3")
    with tempfile.TemporaryDirectory() as root:
        d = Path(root) / "status"
        d.mkdir(parents=True)
        (d / "status.json").write_text(json.dumps({"surfaces": [
            {"kind": "task", "task": "task-x", "path": "/session/ef6c7a101f9b55b3",
             "title": "Pantry plan"}]}))
        srv = _Server(d)
        try:
            code, body = srv.get("/api/view/ef6c7a101f9b55b3")
            assert code == 200 and json.loads(body) == {
                "key": "ef6c7a101f9b55b3", "session": "/session/ef6c7a101f9b55b3",
                "title": "Pantry plan", "task": "task-x",
                # no project and no cadre on record: the run falls under Other
                "cadre": "~other", "cadre_name": "Other"}
            # a page the snapshot does not know still frames, with no study box,
            # and with no cadre: its way up is to the fleet
            unknown = json.loads(srv.get("/api/view/aaaaaaaaaaaaaaaa")[1])
            assert unknown["task"] is None and unknown["cadre"] == "" and unknown["cadre_name"] == ""
            for bad in ("/api/view/..%2Fetc", "/api/view/a%22b", "/api/view/"):
                try:
                    srv.get(bad)
                    raise AssertionError(f"{bad} was served")
                except urllib.error.HTTPError as e:
                    assert e.code == 404
        finally:
            srv.close()


def test_a_framed_page_names_the_cadre_its_run_belongs_to(monkeypatch):
    """The frame's way up goes to the home of the page's cadre: the run's own
    cadre when one is on record, else the one its project falls under."""
    projs = {"dev": {"id": "dev", "name": "Dev", "dirs": []},
             "site": {"id": "site", "name": "Site", "group": "dev", "dirs": ["/home/dev/site"]}}
    monkeypatch.setattr(statusd, "load_projects", lambda *a, **k: projs)
    monkeypatch.setattr(statusd.cadres_mod, "load", lambda _d: {"task-k": "dev"})
    assert statusd.view_cadre({"task": "task-k"}) == {"cadre": "dev", "cadre_name": "Dev"}
    assert statusd.view_cadre({"task": "", "project": "site"}) == {"cadre": "dev", "cadre_name": "Dev"}
    assert statusd.view_cadre({}) == {"cadre": "", "cadre_name": ""}
    # a cadre no longer known (archived, removed) is not linked to
    monkeypatch.setattr(statusd.cadres_mod, "load", lambda _d: {"task-k": "gone"})
    assert statusd.view_cadre({"task": "task-k"}) == {"cadre": "", "cadre_name": ""}
    # a broken record leaves the way back to the fleet, never fails the view
    monkeypatch.setattr(statusd, "load_projects", lambda *a, **k: {"dev": "not a record"})
    assert statusd.view_cadre({"task": "task-k"}) == {"cadre": "", "cadre_name": ""}


def test_proxy_lets_only_the_fleet_frame_a_session():
    deny = "frame-ancestors 'none'"
    assert statusd.framable_header("/session/k", "X-Frame-Options", "DENY") is None
    assert statusd.framable_header(
        "/session/k", "Content-Security-Policy", deny) == "frame-ancestors 'self'"
    # every other route keeps review-surface's headers untouched
    assert statusd.framable_header("/api/k/prompts", "X-Frame-Options", "DENY") == "DENY"
    assert statusd.framable_header("/artifact/k", "Content-Security-Policy", deny) == deny
    assert statusd.framable_header("/session/k", "Content-Type", "text/html") == "text/html"


def test_a_configured_machines_app_may_frame_a_session_too(monkeypatch):
    # the React app on another machine frames this machine's page when that
    # machine's app has this one chosen; nobody else may
    monkeypatch.setattr(statusd, "CORS_ORIGINS", {"http://box-2:8183"})
    assert statusd.framable_header("/session/k", "Content-Security-Policy",
                                   "default-src 'self'; frame-ancestors 'none'") == \
        "default-src 'self'; frame-ancestors 'self' http://box-2:8183"


def _runs_fixture(root: Path):
    (root / "surfaces").mkdir(parents=True)
    (root / "history.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"ended": 1000.0, "story": "task-old-one-20261001-000000-aaaaaa", "stage": "task",
         "ok": True, "seconds": 60, "cost_usd": 1.0},
        {"ended": 3000.0, "story": "task-new-one-20261002-000000-bbbbbb", "stage": "implement",
         "ok": False, "seconds": 120, "cost_usd": 2.0},
    ]) + "\n")
    (root / "registry.json").write_text(json.dumps({"tasks": {
        "task-old-one-20261001-000000-aaaaaa": {"project": "alpha"},
        "task-new-one-20261002-000000-bbbbbb": {"project": "beta"}}}))
    (root / "surfaces" / "sessions.json").write_text(json.dumps({
        "/a.html": {"kind": "task", "path": "/session/aaa", "key": "aaa", "open": True,
                    "opened": 900.0, "task": "task-old-one-20261001-000000-aaaaaa"}}))


def test_the_fleet_api_ships_only_the_recent_rows_the_card_shows():
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        _runs_fixture(root)
        with (root / "history.jsonl").open("a") as f:
            for i in range(statusd.RECENT_LIMIT + 3):
                f.write(json.dumps({"ended": 5000.0 + i, "story": f"task-extra-{i}",
                                    "stage": "task", "ok": True, "seconds": 1,
                                    "cost_usd": None}) + "\n")
        srv = _Server(root / "status")
        try:
            _, body = srv.get("/api/fleet")
            recent = json.loads(body)["recent"]
            # newest first, cut to what the card draws; every task still has
            # its returned time
            assert len(recent) == statusd.RECENT_LIMIT
            assert recent[0]["task"] == f"task-extra-{statusd.RECENT_LIMIT + 2}"
            assert len(json.loads(body)["returned"]) == statusd.RECENT_LIMIT + 5
        finally:
            srv.close()
