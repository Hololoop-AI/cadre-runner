"""Cadre's notification center (runnerlib/notifications.py) and the two
statusd behaviours that make it the only one: it answers /api/notifications
itself, and a review page opened as a tab's own page goes into the fleet's
frame, where the center runs.
Run directly: python3 -m pytest -q tests/test_notifications.py"""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runnerlib import notifications as n  # noqa: E402


def ev(kind, key, ts, **data):
    return {"ts": ts, "source": "review-surface", "type": kind, "subject": key, "data": data}


EVENTS = [
    ev("page.opened", "aaaa", "2026-11-17T10:00:00.000Z", new=True),
    ev("page.version", "aaaa", "2026-11-17T10:01:00.000Z"),
    ev("page.version", "aaaa", "2026-11-17T10:02:00.000Z"),
    ev("page.opened", "bbbb", "2026-11-17T10:03:00.000Z", new=False),   # a reopen: not news
    ev("page.feedback_sent", "bbbb", "2026-11-17T10:04:00.000Z"),       # not a bell event
    ev("page.agent_reply", "thrd", "2026-11-17T10:05:00.000Z"),         # a thread: the footer counts it
    ev("page.agent_reply", "cccc", "2026-11-17T10:06:00.000Z"),
]


def test_one_row_per_page_its_latest_event_newest_first():
    rows = n.summarize(EVENTS, thread_keys=frozenset({"thrd"}))
    assert [(r["key"], r["type"], r["at"]) for r in rows] == [
        ("cccc", "page.agent_reply", "2026-11-17T10:06:00.000Z"),
        ("aaaa", "page.version", "2026-11-17T10:02:00.000Z"),
    ]


def test_a_foreign_line_is_not_news():
    assert n.summarize([{"type": [], "subject": "a"}, {"type": {"x": 1}}, {"subject": "b"}]) == []


def test_a_torn_tail_and_a_missing_log_read_as_what_is_whole(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in EVENTS) + '\n{"ts": "torn')
    assert len(n._recent_events(log)) == len(EVENTS)
    assert len(n._recent_events(log, max_bytes=200)) < len(EVENTS)  # starts mid-line: that line is skipped
    assert n._recent_events(tmp_path / "none.jsonl") == []


def state(tmp_path, events=EVENTS):
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (tmp_path / "links.jsonl").write_text(json.dumps({"type": "thread-of", "from": "thrd", "to": "aaaa"}) + "\n")
    page = tmp_path / "a.html"
    page.write_text("<title>Pantry plan</title>")
    # cccc is not a session the page server knows any more: dropped, as its own bell drops it
    (tmp_path / "state.json").write_text(json.dumps({"sessions": {"aaaa": {"file": str(page)}}}))
    return tmp_path


def env(monkeypatch, d):
    monkeypatch.setenv("REVIEW_SURFACE_STATE_DIR", str(d))
    monkeypatch.delenv("REVIEW_SURFACE_LINKS", raising=False)
    monkeypatch.delenv("REVIEW_SURFACE_EVENTS", raising=False)


def runner(tmp_path, ended="2026-11-17T10:02:00.500Z", ok=True, running=False):
    """Cadre's records: aaaa is task-a's newest page; its newest turn ended at `ended`."""
    d = tmp_path / "runner"
    (d / "surfaces").mkdir(parents=True, exist_ok=True)
    (d / "surfaces" / "sessions.json").write_text(json.dumps(
        {"/x/a.html": {"key": "aaaa", "task": "task-a", "path": "/session/aaaa", "opened": 2}}))
    (d / "history.jsonl").write_text(json.dumps({"story": "task-a", "ended": conv.epoch(ended), "ok": ok}) + "\n")
    (d / "registry.json").write_text(json.dumps({"tasks": {"task-a": {"active_runs": {"r1": {}} if running else {}}}}))
    return d


def test_payload_names_each_page_and_drops_unknown_ones(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path)
    assert n.payload(state(tmp_path), runner(tmp_path)) == {"notifications": [
        {"key": "aaaa", "type": "page.version", "label": "Your turn", "at": "2026-11-17T10:02:00.500Z",
         "actionable": True, "problem": False, "title": "Pantry plan", "url": "/session/aaaa"}]}


def test_the_event_log_is_read_where_the_page_server_keeps_it(tmp_path, monkeypatch):
    env(monkeypatch, state(tmp_path))
    moved = tmp_path / "elsewhere.jsonl"
    (tmp_path / "events.jsonl").rename(moved)
    assert n.payload(tmp_path, runner(tmp_path)) == {"notifications": []}
    monkeypatch.setenv("REVIEW_SURFACE_EVENTS", str(moved))
    assert [r["key"] for r in n.payload(tmp_path, runner(tmp_path))["notifications"]] == ["aaaa"]


@pytest.fixture
def statusd_server(tmp_path, monkeypatch):
    import statusd
    env(monkeypatch, tmp_path)
    monkeypatch.setattr(statusd, "surface_state_dir", lambda: state(tmp_path))
    monkeypatch.setattr(statusd, "DATA_DIR", runner(tmp_path))
    app = tmp_path / "dist"
    app.mkdir()
    (app / "index.html").write_text("<!doctype html>")
    monkeypatch.setattr(statusd, "APP_DIR", app)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_statusd_answers_the_bell_itself(statusd_server):
    with urllib.request.urlopen(statusd_server + "/api/notifications", timeout=5) as r:
        assert [x["key"] for x in json.loads(r.read())["notifications"]] == ["aaaa"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.mark.parametrize("asked,landing", [("/session/aaaa", "/view/aaaa"),
                                            ("/session/aaaa?round=2", "/view/aaaa?round=2")])
def test_a_review_page_opened_as_a_tab_goes_into_the_fleet_frame(statusd_server, asked, landing):
    opener = urllib.request.build_opener(_NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as hit:
        opener.open(urllib.request.Request(statusd_server + asked,
                                           headers={"Sec-Fetch-Dest": "document"}), timeout=5)
    assert hit.value.code == 302 and hit.value.headers["Location"] == landing


def test_without_the_app_built_review_pages_are_not_redirected(statusd_server, monkeypatch, tmp_path):
    import statusd
    monkeypatch.setattr(statusd, "APP_DIR", tmp_path / "no-build")
    monkeypatch.setattr(statusd, "SURFACE", "http://127.0.0.1:9")
    with pytest.raises(urllib.error.HTTPError) as hit:
        urllib.request.urlopen(urllib.request.Request(statusd_server + "/session/aaaa",
                                                      headers={"Sec-Fetch-Dest": "document"}), timeout=5)
    assert hit.value.code == 502  # forwarded to the page server


@pytest.mark.parametrize("path,headers", [
    ("/session/aaaa", {"Sec-Fetch-Dest": "iframe"}),             # the frame's own load
    ("/session/aaaa?embed=thread", {"Sec-Fetch-Dest": "document"}),  # a thread window
    ("/session/aaaa?embed=", {"Sec-Fetch-Dest": "document"}),
    ("/session/aaaa", {}),                                       # curl, the CLI
    ("/session/aaaa/x", {"Sec-Fetch-Dest": "document"}),          # not a page address
])
def test_everything_else_is_forwarded_to_the_page_server(statusd_server, monkeypatch, path, headers):
    import statusd
    monkeypatch.setattr(statusd, "SURFACE", "http://127.0.0.1:9")  # nothing listens: the proxy answers 502
    with pytest.raises(urllib.error.HTTPError) as hit:
        urllib.request.urlopen(urllib.request.Request(statusd_server + path, headers=headers), timeout=5)
    assert hit.value.code == 502


# ---- the three tiers: needs you (chimes), working (quiet), not listed
from runnerlib import conversations as conv  # noqa: E402
T = conv.epoch
AT = "2026-11-17T10:02:00.000Z"


def row(key="aaaa", kind="page.version", at=AT, label="New version"):
    return {"key": key, "type": kind, "label": label, "at": at}


NOW = T("2026-11-17T10:10:00.000Z")


def tier(rows=None, tasks=None, returned=None, answered=None, since=None, running=frozenset(), now=NOW):
    out = n.classify(rows or [row()], {"aaaa": "task-a"} if tasks is None else tasks, returned or {},
                     answered or {}, since or {}, running, now)
    return [(r["key"], r["label"], r["actionable"], r["at"]) for r in out]


def test_a_turn_that_came_back_is_your_turn_timed_when_it_came_back():
    # the runner reaps the turn about when its last write lands
    assert tier(returned={"task-a": (T("2026-11-17T10:02:01.000Z"), True)}) == [
        ("aaaa", "Your turn", True, "2026-11-17T10:02:01.000Z")]


def test_a_turn_that_failed_still_needs_you_and_is_marked_a_problem():
    assert tier(returned={"task-a": (T("2026-11-17T10:02:01.000Z"), False)})[0][1:3] == ("Run failed", True)
    [r] = n.classify([row()], {"aaaa": "task-a"}, {"task-a": (T("2026-11-17T10:02:01.000Z"), False)}, {}, {}, now=NOW)
    assert r["problem"] is True
    [r] = n.classify([row()], {"aaaa": "task-a"}, {"task-a": (T("2026-11-17T10:02:01.000Z"), True)}, {}, {}, now=NOW)
    assert r["problem"] is False


def test_an_agent_reply_is_always_for_you():
    assert tier([row(kind="page.agent_reply", label="Agent replied")], tasks={}) == [("aaaa", "Agent replied", True, AT)]


def test_an_agent_at_work_is_one_quiet_row_timed_from_when_it_started():
    # the placeholder at 10:00, rewrites since: one row, at the start, not counted or chimed
    assert tier(running=frozenset({"task-a"}), since={"aaaa": "2026-11-17T10:00:00.000Z"}) == [
        ("aaaa", "Working on it", False, "2026-11-17T10:00:00.000Z")]


def test_right_after_you_answer_the_page_shows_working_from_your_answer():
    answered = {"aaaa": T("2026-11-17T10:05:00.000Z")}
    assert tier(answered=answered, running=frozenset({"task-a"}))[0][1:] == ("Working on it", False, "2026-11-17T10:05:00.000Z")


def test_a_placeholder_written_just_after_the_last_turn_ended_is_working_not_your_turn():
    assert tier(returned={"task-a": (T("2026-11-17T10:01:56.000Z"), True)})[0][1:3] == ("Working on it", False)


@pytest.mark.parametrize("why,kwargs", [
    ("a side page no dialogue owns", {"tasks": {}, "returned": {"task-a": (T("2026-11-17T10:02:03.000Z"), True)}}),
    ("you answered and the agent has not written since, nor started",
     {"returned": {"task-a": (T("2026-11-17T10:02:01.000Z"), True)}, "answered": {"aaaa": T("2026-11-17T10:03:00.000Z")}}),
])
def test_noise_is_not_listed(why, kwargs):
    assert tier(**kwargs) == [], why


def test_a_working_row_with_no_run_in_flight_ages_out():
    late = {"returned": {"task-a": (T("2026-11-17T09:00:00.000Z"), True)}}
    assert tier(**late)[0][1] == "Working on it"
    assert tier(**late, now=T("2026-11-17T11:00:00.000Z")) == []


def test_times_are_compared_as_times_not_text():
    # an event without milliseconds, and a turn that ended half a second later
    rows = [row(at="2026-11-17T10:02:00Z")]
    assert tier(rows, returned={"task-a": (T("2026-11-17T10:02:00.500Z"), True)})[0][3] == "2026-11-17T10:02:00.500Z"
    assert tier([row(at="not a time")], returned={"task-a": (T("2026-11-17T10:02:00.500Z"), True)}) == []


def test_payload_reads_the_runner_records(tmp_path, monkeypatch):
    env(monkeypatch, state(tmp_path))
    [r] = n.payload(tmp_path, runner(tmp_path, ended="2026-11-17T10:02:04.000Z"))["notifications"]
    assert (r["key"], r["label"], r["actionable"]) == ("aaaa", "Your turn", True)
    [r] = n.payload(tmp_path, runner(tmp_path, running=True))["notifications"]
    assert (r["label"], r["actionable"]) == ("Working on it", False)


def test_an_earlier_rounds_page_of_the_same_task_is_not_listed(tmp_path, monkeypatch):
    events = [ev("page.version", "cccc", "2026-11-17T10:00:00.000Z"),   # round 1's page
              ev("page.feedback_sent", "cccc", "2026-11-17T10:01:00.000Z"),
              ev("page.version", "aaaa", "2026-11-17T10:05:00.000Z")]   # round 2's page
    env(monkeypatch, state(tmp_path, events))
    (tmp_path / "state.json").write_text(json.dumps({"sessions": {"aaaa": {"file": "a"}, "cccc": {"file": "c"}}}))
    d = runner(tmp_path, ended="2026-11-17T10:05:00.500Z")
    (d / "surfaces" / "sessions.json").write_text(json.dumps(
        {"/x/a-r1.html": {"key": "cccc", "task": "task-a", "path": "/session/cccc", "opened": 1},
         "/x/a.html": {"key": "aaaa", "task": "task-a", "path": "/session/aaaa", "opened": 2}}))
    with open(d / "history.jsonl", "a") as fh:
        fh.write('{"story": "task-a", "ended": "not a time"}\n')
    assert {r["key"]: r["label"] for r in n.payload(tmp_path, d)["notifications"]} == {"aaaa": "Your turn"}


def test_a_page_beyond_the_newest_thirty_still_becomes_your_turn():
    rows = [row(f"p{i:02}", at=f"2026-11-17T11:{i:02}:00.000Z", kind="page.agent_reply") for i in range(40)]
    old = row("old", at="2026-11-17T09:00:00.000Z")
    marked = n.classify(rows + [old], {"old": "task-o"}, {"task-o": (T("2026-11-17T12:00:00.000Z"), True)}, {}, {},
                        now=T("2026-11-17T12:00:00.000Z"))[:n.LIMIT]
    assert marked[0]["key"] == "old" and marked[0]["label"] == "Your turn"


def test_working_since_restarts_at_each_answer():
    events = [ev("page.opened", "aaaa", "2026-11-17T10:00:00.000Z", new=True),  # the placeholder
              ev("page.version", "aaaa", "2026-11-17T10:01:00.000Z"),           # a rewrite mid-turn
              ev("page.feedback_sent", "aaaa", "2026-11-17T10:02:00.000Z"),
              ev("page.version", "aaaa", "2026-11-17T10:03:00.000Z"),
              ev("page.version", "aaaa", "2026-11-17T10:04:00.000Z")]
    assert n._working_since(events[:2]) == {"aaaa": "2026-11-17T10:00:00.000Z"}
    assert n._working_since(events) == {"aaaa": "2026-11-17T10:03:00.000Z"}


@pytest.mark.parametrize("record,label", [
    ({"timed_out": True, "lost": False, "returncode": 124}, "Run timed out"),
    ({"timed_out": False, "lost": True, "returncode": -1}, "Run interrupted"),
    ({"timed_out": False, "lost": False, "returncode": 1}, "Run failed"),
])
def test_a_failed_turn_says_what_happened_from_its_own_log(tmp_path, record, label):
    logs = tmp_path / "logs" / "task-a"
    logs.mkdir(parents=True)
    (logs / "20261117-090000-task.json").write_text(json.dumps({"timed_out": False, "lost": False, "returncode": 0}))
    (logs / "20261117-100000-task.json").write_text(json.dumps(record))  # the newest turn
    assert n.failure_label(tmp_path, "task-a") == label


def test_a_failed_turn_with_no_log_just_failed(tmp_path):
    assert n.failure_label(tmp_path, "task-a") == "Run failed"
    assert n.failure_label(None, "task-a") == "Run failed"


def test_payload_labels_a_failure_from_the_runner_log(tmp_path, monkeypatch):
    env(monkeypatch, state(tmp_path))
    d = runner(tmp_path, ok=False)
    (d / "logs" / "task-a").mkdir(parents=True)
    (d / "logs" / "task-a" / "20261117-100000-task.json").write_text(json.dumps({"timed_out": True}))
    [r] = n.payload(tmp_path, d)["notifications"]
    assert (r["label"], r["problem"], r["actionable"]) == ("Run timed out", True, True)


def test_a_failure_is_read_from_the_newest_run_record_not_other_files_beside_it(tmp_path):
    logs = tmp_path / "logs" / "task-a"
    logs.mkdir(parents=True)
    (logs / "20261117-100000-task.json").write_text(json.dumps({"timed_out": True}))
    (logs / "closing-annotations.json").write_text(json.dumps([{"note": "kept feedback, not a run"}]))
    assert n.failure_label(tmp_path, "task-a") == "Run timed out"


def test_the_page_servers_state_is_reread_only_when_it_changes_and_a_torn_read_keeps_the_last(tmp_path):
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"sessions": {"aaaa": {"file": "a"}}}))
    assert list(n._sessions(tmp_path)) == ["aaaa"]
    f.write_text('{"sessions": {"aaaa": {"fi')  # caught mid-write
    assert list(n._sessions(tmp_path)) == ["aaaa"]
    f.write_text(json.dumps({"sessions": {"aaaa": {"file": "a"}, "bbbb": {"file": "b"}}}))
    assert sorted(n._sessions(tmp_path)) == ["aaaa", "bbbb"]


# ---- behind an HTTPS proxy: the page server's origin guard needs the browser's scheme
def test_the_browser_scheme_from_a_local_https_proxy_reaches_the_page_server(statusd_server, monkeypatch):
    import statusd
    from http.server import BaseHTTPRequestHandler
    seen = {}

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.update({k.lower(): v for k, v in self.headers.items()})
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(statusd, "SURFACE", f"http://127.0.0.1:{up.server_address[1]}")
        req = urllib.request.Request(statusd_server + "/api/aaaa/agent-status",
                                     headers={"X-Forwarded-Proto": "https"})
        urllib.request.urlopen(req, timeout=5).close()
        assert seen["x-forwarded-proto"] == "https"
    finally:
        up.shutdown()
        up.server_close()


@pytest.mark.parametrize("ip,claimed,proto", [
    ("127.0.0.1", "https", "https"),
    ("::1", "https", "https"),
    ("::ffff:127.0.0.1", "https", "https"),
    ("127.0.0.1", None, "http"),             # no proxy: plain HTTP
    ("127.0.0.1", "gopher", "http"),         # not a web scheme
    ("100.64.0.5", "https", "http"),         # a remote client cannot claim a scheme
])
def test_only_a_proxy_on_this_machine_may_say_the_scheme(ip, claimed, proto):
    import statusd
    headers = {"X-Forwarded-Proto": claimed} if claimed else {}
    assert statusd.forwarded_proto(ip, headers) == proto
