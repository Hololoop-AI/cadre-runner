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


class FakeReg:
    def stories(self):
        return {
            "nex-126": {
                "story_id": "NEX-126", "title": "Assembly stage", "repo": "o/r",
                "status": "active", "phase": "slices", "planning_pr": 31,
                "board": {"url": "https://linear.app/x"},
                "slices": {"draft-pr-gate": {"contract_merged": True, "tests_pr": 43}},
            },
        }


def test_write_and_shape():
    with tempfile.TemporaryDirectory() as root:
        cfg = FakeCfg(root)
        run = {"stage": "build", "story": "NEX-126", "slice": "draft-pr-gate",
               "pr": 50, "started": 1000.0}
        status.write_status(cfg, FakeReg(), run=run)
        snap = json.loads((cfg.data_dir / "status" / "status.json").read_text())
        assert snap["run"] == run
        assert snap["poll_interval"] == 45 and snap["ts"] > 0
        st = snap["stories"][0]
        assert st["story_id"] == "NEX-126" and st["phase"] == "slices"
        assert st["slices"]["draft-pr-gate"] == {"contract": True, "tests": False, "build": False}

        # idle write clears the run marker
        status.write_status(cfg, FakeReg())
        snap = json.loads((cfg.data_dir / "status" / "status.json").read_text())
        assert snap["run"] is None


def test_log_tail_bounded():
    with tempfile.TemporaryDirectory() as root:
        log = Path(root) / "daemon.log"
        log.write_text("\n".join(f"line {i}" for i in range(5000)))
        tail = status._log_tail(log)
        assert len(tail) == status.LOG_TAIL_LINES
        assert tail[-1] == "line 4999"
        assert status._log_tail(Path(root) / "missing.log") == []


def test_never_raises():
    class BrokenReg:
        def stories(self):
            raise RuntimeError("boom")
    with tempfile.TemporaryDirectory() as root:
        status.write_status(FakeCfg(root), BrokenReg())  # must not raise


# ------------------------------------------------------------- fleet page

NOW = 1_700_000_000.0


class FleetReg:
    """Synthetic registry: two repos, one of them holding the work that wants a
    human. Mirrors Registry.stories(status=...) so the escalated story is only
    visible to a caller that asks for it."""

    ALL = {
        "esc": {"story_id": "NEX-1", "title": "Escalated thing", "repo": "o/hot",
                "status": "escalated", "phase": "slices", "planning_pr": 11,
                "slices": {}},
        "verdict": {"story_id": "NEX-2", "title": "Awaiting a verdict", "repo": "o/hot",
                    "status": "active", "phase": "slices", "planning_pr": 12,
                    "slices": {"a": {"build_pr": 99, "build_merged": False}}},
        "fin": {"story_id": "NEX-3", "title": "Finished, unacknowledged", "repo": "o/hot",
                "status": "active", "phase": "done", "slices": {}},
        "busy": {"story_id": "NEX-4", "title": "Building right now", "repo": "o/hot",
                 "status": "active", "phase": "slices", "slices": {}},
        "quiet": {"story_id": "NEX-5", "title": "Nothing happening", "repo": "o/calm",
                  "status": "active", "phase": "slices", "slices": {}},
    }

    def stories(self, status="active"):
        return {k: v for k, v in self.ALL.items() if v["status"] == status}


def _fleet_snapshot(root):
    """A real snapshot: written by status.write_status off the synthetic
    registry, with a surface session, a question and an in-flight run seeded
    into the data dir the way the daemon seeds them."""
    cfg = FakeCfg(root)
    surf_dir = cfg.data_dir / "surfaces"
    surf_dir.mkdir(parents=True, exist_ok=True)
    (surf_dir / "sessions.json").write_text(json.dumps({
        "/a.html": {"kind": "final_review", "story": "verdict", "pr": 99,
                    "path": "/s/abc", "key": "abc", "open": True, "opened": NOW - 30},
        "/b.html": {"kind": "notice", "story": "busy", "path": "/s/zzz",
                    "key": "zzz", "open": True, "opened": NOW - 10},
        "/c.html": {"kind": "ask", "story": "gone", "path": "/s/orph",
                    "key": "orph", "open": True, "opened": NOW - 5},
    }))
    messages.ask(cfg.data_dir, "esc", "Which way?")
    runs = [{"stage": "build", "story": "busy", "slice": "a", "pr": 77,
             "started": NOW - 5}]
    status.write_status(cfg, FleetReg(), run=runs[0], runs=runs)
    return json.loads((cfg.data_dir / "status" / "status.json").read_text())


def test_escalated_stories_reach_the_snapshot():
    with tempfile.TemporaryDirectory() as root:
        snap = _fleet_snapshot(root)
        assert {s["slug"] for s in snap["stories"]} == set(FleetReg.ALL)
        verdict = next(s for s in snap["stories"] if s["slug"] == "verdict")
        assert verdict["prs"] == [12, 99]          # planning + slice PRs, flattened


def test_hierarchy_groups_and_sorts():
    with tempfile.TemporaryDirectory() as root:
        projects = statusd.fleet(_fleet_snapshot(root))
        assert [p["repo"] for p in projects] == ["o/hot", "o/calm"]
        hot = projects[0]
        assert hot["attention"] == 2               # escalated + awaiting a verdict
        # needs-human first (escalated / open verdict / pending question),
        # then finished-but-unacknowledged, then quiet in-progress.
        assert [s["rank"] for s in hot["stories"]] == [0, 0, 1, 2]
        assert {s["slug"] for s in hot["stories"][:2]} == {"esc", "verdict"}
        assert [s["slug"] for s in hot["stories"][2:]] == ["fin", "busy"]
        assert [s["slug"] for s in projects[1]["stories"]] == ["quiet"]


def test_finished_outranks_running_and_activity_breaks_ties():
    with tempfile.TemporaryDirectory() as root:
        snap = _fleet_snapshot(root)
        by_slug = {s["slug"]: s for p in statusd.fleet(snap) for s in p["stories"]}
        assert by_slug["esc"]["rank"] == statusd.RANK_NEEDS_HUMAN
        assert by_slug["verdict"]["why"] == "awaiting final review"
        assert by_slug["fin"]["rank"] == statusd.RANK_FINISHED
        # "busy" is the most recently active story and still sorts below "fin"
        assert by_slug["busy"]["rank"] == statusd.RANK_RUNNING
        assert by_slug["busy"]["activity"] > by_slug["fin"]["activity"]
        hot = statusd.fleet(snap)[0]["stories"]
        assert hot.index(by_slug["fin"]) < hot.index(by_slug["busy"])
        # within the needs-human bucket, most recent first
        needs = [s for s in hot if s["rank"] == statusd.RANK_NEEDS_HUMAN]
        assert [s["activity"] for s in needs] == sorted(
            (s["activity"] for s in needs), reverse=True)


def test_rendering_links_surfaces_and_prs():
    with tempfile.TemporaryDirectory() as root:
        html = statusd.render_home(_fleet_snapshot(root), now=NOW)
        assert 'href="/s/abc"' in html                 # deep link to the live surface
        assert 'https://github.com/o/hot/pull/99' in html
        assert 'href="/s/orph"' in html                # orphan session still reachable
        assert "Loose sessions" in html
        assert "o/hot" in html and "o/calm" in html
        assert f'action="{statusd.TASKS_PATH}"' in html


def test_page_renders_with_zero_stories():
    html = statusd.render_home({})
    assert "No stories in flight" in html
    assert 'name="text"' in html and 'name="cwd"' in html
    assert statusd.render_fleet({}).strip() != ""


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
        self.keep = statusd.STATUS_DIR
        statusd.STATUS_DIR = Path(status_dir)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        statusd.STATUS_DIR = self.keep

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


def test_home_and_partial_are_served():
    with tempfile.TemporaryDirectory() as root:
        _fleet_snapshot(root)
        srv = _Server(Path(root) / "status")
        try:
            code, body = srv.get("/")
            assert code == 200 and "agent fleet" in body and "o/hot" in body
            code, frag = srv.get("/?partial=1")
            assert code == 200 and "<html" not in frag and "o/hot" in frag
        finally:
            srv.close()


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
            home = srv.get("/")[1]                     # / is the fleet page now
            assert "<p>legacy</p>" not in home and "agent fleet" in home
        finally:
            srv.close()


def test_rel_time_reads_as_activity():
    assert statusd._rel_time(0) == ""
    assert statusd._rel_time(NOW - 5, NOW) == "5s ago"
    assert statusd._rel_time(NOW - 300, NOW) == "5m ago"
    assert statusd._rel_time(NOW - 7200, NOW) == "2h ago"
    assert statusd._rel_time(NOW - 2 * 86400, NOW) == "2d ago"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("status smoke tests: all passed")


def test_registered_externals_group_under_their_project():
    """Driver ask 2026-09-20: sessions the runner did not spawn — the
    orchestrator terminal session, design discussions — must appear on the
    fleet page under a project hierarchy, orchestrator first, instead of the
    driver holding session URLs in their head."""
    snap = {"surfaces": [
        {"kind": "external", "path": "/session/d2", "opened": 200.0,
         "project": "hitl", "title": "d2 — store × blackboard",
         "role": "discussion"},
        {"kind": "external", "path": "", "opened": 100.0,
         "project": "hitl", "title": "design orchestrator",
         "role": "orchestrator"},
        {"kind": "ask", "path": "/session/loose", "opened": 50.0,
         "story": "nex-1"},
    ]}
    ext = statusd.external_projects(snap)
    assert [p["project"] for p in ext] == ["hitl"]
    assert [r["title"] for r in ext[0]["rows"]] == [
        "design orchestrator", "d2 — store × blackboard"]

    # a project row never doubles as a loose session
    assert [sf.get("story") for sf in statusd.orphan_surfaces(snap)] == ["nex-1"]

    html = statusd.render_fleet(snap, now=300.0)
    assert "hitl" in html and "design orchestrator" in html
    assert 'href="/session/d2"' in html
    # the page-less orchestrator row renders without a dead link
    assert 'href=""' not in html
    assert "No stories in flight" not in html


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
        surface_mod._consume = lambda *a: consumed.append(a[4])
        surface_mod._read_outbox = lambda cfg: [{"key": "extkey"},
                                                {"key": "taskkey"}]
        surface_mod._sweep_stale = lambda *a: None
        surface_mod._ensure_server = lambda *a: None
        try:
            surface_mod._tick(cfg, types.SimpleNamespace(data={"stories": {}}),
                              None, lambda *a: None)
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


def test_agent_state_badges_name_the_drivers_states():
    """Driver ask 2026-09-20: 'unclear whether an agent has started
    processing, is working, or has errored out.' The badge states are named
    from the driver's side of the loop, and the stall case — feedback queued
    with no agent listening — is the one that alarms."""
    B = statusd.agent_state_badge
    assert B(None) == ("", "")
    assert B({"status": "ended"}) == ("ended", "")
    assert B({"status": "open", "pending_prompts": 2, "presence": "waiting"}) == (
        "queued — no agent listening", "needs")
    assert B({"status": "open", "pending_prompts": 1, "presence": "listening"}) == (
        "delivering to agent", "running")
    assert B({"status": "open", "pending_prompts": 0, "presence": "working"}) == (
        "agent working", "running")
    assert B({"status": "open", "pending_prompts": 0, "presence": "waiting",
              "last_agent_reply_at": "2026-09-20T08:00:00Z"}) == (
        "agent replied — your turn", "finished")
    assert B({"status": "open", "pending_prompts": 0, "presence": "listening"}) == (
        "awaiting you", "")


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


def test_fleet_rows_carry_live_state_and_history_links():
    with tempfile.TemporaryDirectory() as d:
        artifact = str(Path(d) / "art.html")
        (Path(d) / "feedback-journal.jsonl").write_text(
            json.dumps({"at": "2026-09-20T01:00:00Z", "file": artifact,
                        "prompts": [{"prompt": "x"}]}) + "\n")
        snap = {"surfaces": [
            {"kind": "external", "path": "/session/k1", "opened": 100.0,
             "project": "hitl", "title": "d2", "artifact": artifact},
        ]}
        import os
        os.environ["REVIEW_SURFACE_STATE_DIR"] = d
        html = statusd.render_fleet(snap, now=200.0, statuses={
            "k1": {"status": "open", "pending_prompts": 0, "presence": "working",
                   "updated_at": "1970-01-01T00:02:30+00:00"}})
        assert "agent working" in html
        assert 'href="/history/k1"' in html and "history (1)" in html
        assert "upd " in html
        # and with no statuses (surface server down) the page still renders
        assert "agent working" not in statusd.render_fleet(snap, now=200.0)
        del os.environ["REVIEW_SURFACE_STATE_DIR"]


def test_decided_title_overrides_presence_badge():
    snap = {"stories": {}, "tasks": {}, "surfaces": [
        {"path": "/session/abc", "title": "d8 — report anatomy (DECIDED: adopted)",
         "project": "hitl", "kind": "external", "role": "discussion",
         "artifact": "", "opened": 1789900000.0},
    ]}
    from statusd import render_fleet
    html = render_fleet(snap, now=0, statuses={
        "abc": {"presence": "waiting", "pending_prompts": 0,
                "last_agent_reply_at": "2026-09-20T00:00:00Z"}})
    assert "decided — nothing needs you" in html
    assert "your turn" not in html


def test_decided_surfaces_fold_and_active_ones_stay_in_the_scan():
    """The fleet was becoming every decision ever made: settled surfaces never
    leave. Decided rows collapse into a per-project fold; active rows render
    outside it; the filter input ships on the page."""
    snap = {"stories": {}, "tasks": {}, "surfaces": [
        {"path": "/session/aaa", "title": "framework choice (DECIDED: axum)",
         "project": "hitl", "kind": "external", "role": "discussion",
         "artifact": "", "opened": 1789900000.0},
        {"path": "/session/bbb", "title": "workspace organization",
         "project": "hitl", "kind": "external", "role": "discussion",
         "artifact": "", "opened": 1789900000.0},
    ]}
    from statusd import render_fleet, render_home
    html = render_fleet(snap, now=0)
    fold = html[html.index('<details class="fold"'):html.index('</details>')]
    assert "1 decided" in fold and "DECIDED: axum" in fold
    assert "workspace organization" not in fold          # active row outside
    assert "workspace organization" in html
    page = render_home(snap, now=0)
    assert 'id="filter"' in page and "applyFilter" in page


def test_registered_checkout_rows_are_dispatch_targets():
    """A pathless project row used to render as an inert 'session' the driver
    could not click (reported as broken). With a cwd it becomes a dispatch
    target: clickable, carrying the directory the task box should prefill."""
    snap = {"stories": {}, "tasks": {}, "surfaces": [
        {"path": "", "title": "code checkout", "project": "cadre",
         "kind": "external", "role": "checkout",
         "cwd": "/home/x/Projects/cadre/cadre",
         "artifact": "", "opened": 1789900000.0},
    ]}
    from statusd import render_fleet, render_home
    html = render_fleet(snap, now=0)
    assert 'data-cwd="/home/x/Projects/cadre/cadre"' in html
    assert 'class="story dispatch"' in html and 'new task' in html
    assert "dispatchTo" in render_home(snap, now=0)
