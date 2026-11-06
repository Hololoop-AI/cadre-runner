"""The statusd HTTP contract (web/contracts/statusd.openapi.json) holds on
both sides: statusd.Handler's real answers fit it, every operation in it is
exercised here, and the web app calls only declared paths and reads only
declared fields. Decision: web/context/decisions/0002-statusd-http-contract.md.

Not caught here: field TYPES as TypeScript declares them (only names and
optionality are compared), form field names the app's forms post, and a
peer at another version (only this checkout's answers are validated; run
web/scripts/contract_check.py against a live server for that).
Run directly: python3 -m pytest -q tests/test_web_contract.py"""

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web" / "scripts"))

import contract_check as cc
import statusd

CONTRACT = cc.load()
WEB_SRC = ROOT / "web" / "src"

GETS = ["/api/fleet/version", "/api/fleet", "/api/backends", "/api/newwork", "/fs", "/costs.json"]
POSTS = {"/tasks": "text=&cwd=/tmp",
         "/items": "item=task-a&action=tag&tag=cadre",
         "/repair": "action=move&key=nosuchkey&project=cadre",
         "/projects": "name=Shop&dir1=/tmp"}
STREAMS = ["/fleet/events"]
# payload.ts types the app builds itself, never read off the wire
CLIENT_ONLY_INTERFACES = {"Earlier"}

NOW = 1793990000.0
SURFACES = [
    {"kind": "task", "story": None, "pr": None, "path": "/session/aaaa", "opened": NOW - 600,
     "project": "research", "title": None, "role": None, "task": "task-a", "cwd": "/home/me/r",
     "stranded": [], "artifact": "/state/surfaces/task-a.html"},
    {"kind": "task", "path": "/session/bbbb", "opened": NOW - 9000, "project": "cadre-runner",
     "title": "Older round", "task": "task-b", "cwd": "/home/me/c", "stranded": None,
     "artifact": "/state/surfaces/task-b.html", "ended": True},
]
GRAPH = ([{"type": "supersedes", "from": "aaaa", "to": "bbbb"}], {"bbbb": "cadre-runner"},
         {"aaaa": {"file": "/state/surfaces/task-a.html", "title": "Pantry plan", "updated": NOW - 60,
                   "ended": False}})
PROJS = {"research": {"id": "research", "name": "Research", "dirs": ["/home/me/r"], "group": None,
                      "context": "", "skills": [], "launch": "", "archived": False, "created": NOW - 9e5}}
TURNS = [{"ended": NOW - 600, "story": "task-a", "stage": "task", "slice": None, "pr": None, "ok": True,
          "seconds": 156.0, "model": "m", "cost_usd": 3.5},
         {"ended": NOW - 90000, "story": "task-b", "stage": "implement", "ok": False, "seconds": 12.5,
          "model": "m", "cost_usd": None}]
OWNERS = {"task-a": "research", "task-b": "cadre-runner"}
PAGES = {"task-a": {"kind": "task", "path": "/session/aaaa", "key": "aaaa", "open": True,
                    "opened": NOW - 600, "project": "research", "task": "task-a", "cwd": "/home/me/r",
                    "artifact": "/state/surfaces/task-a.html", "title": "Pantry plan"}}
TREE = {"task-a": {"title": "Pantry plan", "project": "research", "since": NOW - 700, "parent": "",
                   "launcher": "", "working": True, "node": "task", "round": "1", "result": "",
                   "page": "/session/aaaa"}}
RUNNING = [{"task": "task-a", "title": "Pantry plan", "doing": "1 agent · Bash, just now", "agents": 1,
            "page": "/session/aaaa", "page_title": "Pantry plan", "item": "task-a",
            "item_title": "Pantry plan", "folder": "/home/me/r", "started": NOW - 30, "deadline": 0.0,
            "waiting": False}]
PAGELESS = [{"task": "task-c", "since": NOW - 5, "project": "research", "title": "Find a doctor",
             "cwd": "/home/me/r", "running": True, "ran": False, "started": True, "failed": ""}]
STATUSES = {"aaaa": {"status": "open", "ended_by": None, "presence": "waiting", "delivery": "push",
                     "pending_prompts": 0, "updated_at": "2026-11-06T18:39:07.044Z",
                     "last_agent_reply_at": None, "viewed_at": None, "unseen": True}}


@pytest.fixture
def server(monkeypatch, tmp_path):
    """statusd.Handler on port 0, every data source answering realistic
    records, so fleet_payload and the handlers compose the answers for real."""
    monkeypatch.setattr(statusd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(statusd, "_CFG", None)
    monkeypatch.setattr(statusd, "_ARCHIVED", set())
    monkeypatch.setattr(statusd, "_review_events", lambda: None)
    monkeypatch.setattr(statusd, "read_snapshot", lambda *a: {
        "ts": NOW, "iso": "2026-11-06 12:49:00", "poll_interval": 15, "surfaces": SURFACES,
        "code": {"running": "55df4ec", "tree": "55df4ec", "since": NOW - 3600, "head": "5daaeaf"}})
    monkeypatch.setattr(statusd, "load_graph", lambda: GRAPH)
    monkeypatch.setattr(statusd, "load_projects", lambda *a, **k: PROJS)
    monkeypatch.setattr(statusd, "load_runs", lambda **k: (TURNS, OWNERS, PAGES))
    monkeypatch.setattr(statusd, "load_costs", lambda *a: (TURNS, OWNERS))
    monkeypatch.setattr(statusd, "load_tree", lambda *a: TREE)
    monkeypatch.setattr(statusd, "running_agents", lambda **k: RUNNING)
    monkeypatch.setattr(statusd, "pageless_tasks", lambda *a, **k: PAGELESS)
    monkeypatch.setattr(statusd, "fetch_agent_statuses", lambda *a, **k: STATUSES)
    monkeypatch.setattr(statusd, "closed_pages", lambda *a, **k: {"research": [{}, {}]})
    monkeypatch.setattr(statusd, "live_turns", lambda: {"task-a": "1 agent · Bash, just now"})
    monkeypatch.setattr(statusd, "journal_batches", lambda a, *r: [{}, {}])
    monkeypatch.setattr(statusd, "_known_keys", lambda: (set(), [], {}))
    monkeypatch.setattr(statusd.library_mod, "scan", lambda **kw: [
        {"kind": "skill", "name": "review", "invoke": "/review", "description": "d"}])
    statusd.items_mod.apply(tmp_path, "task-a", "hide")
    (tmp_path / "Projects").mkdir()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    srv.shutdown()
    srv.server_close()


def _fits(body, schema):
    errs = cc.validate(body, schema, CONTRACT)
    assert not errs, "\n".join(errs[:30])


def test_the_contract_uses_only_keywords_the_check_enforces():
    assert cc.schema_keywords_ok(CONTRACT) == []


def test_every_operation_in_the_contract_has_a_case_here():
    here = {("GET", p) for p in GETS + STREAMS} | {("POST", p) for p in POSTS}
    assert cc.operations(CONTRACT) == here, (
        "an endpoint was added to (or dropped from) the contract without a case in this file")


@pytest.mark.parametrize("path", GETS)
def test_get_answers_fit_the_contract(server, path):
    url, tmp = server
    query = "?q=" + urllib.parse.quote(str(tmp) + "/") if path == "/fs" else ""
    with urllib.request.urlopen(url + path + query, timeout=5) as r:
        assert r.headers["Content-Type"] == "application/json"
        assert r.headers["Cache-Control"] == "no-store"
        body = json.loads(r.read())
    _fits(body, cc.response_schema(CONTRACT, path))
    if path == "/fs":
        assert body["dirs"] == [str(tmp / "Projects") + "/"]


@pytest.mark.parametrize("path", sorted(POSTS))
def test_form_posts_asking_for_json_get_a_landing(server, path):
    url, _ = server
    req = urllib.request.Request(url + path, data=POSTS[path].encode(), method="POST",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.headers["Content-Type"] == "application/json"
        body = json.loads(r.read())
    _fits(body, cc.response_schema(CONTRACT, path, "post"))
    assert body["location"].partition("?")[2].startswith("notice=")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.mark.parametrize("path", sorted(POSTS))
def test_form_posts_answer_the_declared_statuses_otherwise(server, path):
    url, _ = server
    declared = set(cc.operation(CONTRACT, path, "post")["responses"])
    opener = urllib.request.build_opener(_NoRedirect)
    for headers, want in (({}, 303), ({"Origin": "http://evil.example"}, 403)):
        with pytest.raises(urllib.error.HTTPError) as hit:
            opener.open(urllib.request.Request(url + path, data=POSTS[path].encode(),
                                               method="POST", headers=headers), timeout=5)
        assert hit.value.code == want and str(want) in declared
        if want == 303:
            assert "?notice=" in hit.value.headers["Location"]


def test_the_fleet_stream_sends_only_declared_events(server, monkeypatch):
    url, _ = server
    sigs = iter([("a", True)] + [("b", False)] * 1000)
    monkeypatch.setattr(statusd.backends_mod, "signature", lambda b: next(sigs))
    monkeypatch.setattr(statusd, "STREAM_TICK", 0.02)
    events = cc.sse_events(CONTRACT)
    seen = []
    with urllib.request.urlopen(url + "/fleet/events", timeout=5) as r:
        assert r.headers["Content-Type"] == "text/event-stream"
        name = None
        while len(seen) < 3:
            line = r.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                assert name in events, f"undeclared event {name!r}"
                _fits(json.loads(line[6:]), events[name])
                seen.append(name)
    assert seen == ["hello", "source", "change"]


def _schemas_by_ts():
    return {s["x-ts"]: s for s in CONTRACT["components"]["schemas"].values() if "x-ts" in s}


def test_the_app_reads_only_fields_the_contract_declares():
    ifcs = cc.ts_interfaces((WEB_SRC / "model" / "payload.ts").read_text(encoding="utf-8"))
    mapped = _schemas_by_ts()
    assert set(ifcs) - set(mapped) == CLIENT_ONLY_INTERFACES, (
        "a payload.ts interface no contract schema names in x-ts (or one that went away)")
    problems = []
    for name, members in ifcs.items():
        schema = mapped.get(name)
        if schema is None:
            continue
        own = set(schema.get("x-ts-client-only", []))
        for prop, optional in members.items():
            if prop in own:
                continue
            if prop not in schema.get("properties", {}):
                problems.append(f"{name}.{prop}: read by the app, not in the contract")
            elif not optional and prop not in schema.get("required", []):
                problems.append(f"{name}.{prop}: the app relies on it, the contract does not require it")
    assert not problems, "\n".join(problems)


def _app_calls():
    """Root-relative paths the app fetches or posts to: quoted paths in the
    api module, and form actions and postForm / server.post targets in the ui."""
    def code(f):
        text = re.sub(r"/\*.*?\*/", "", f.read_text(encoding="utf-8"), flags=re.S)
        return re.sub(r"(?m)^\s*//.*$", "", text)

    found = set(re.findall(r"""['"](/[a-z][\w./-]*)""", code(WEB_SRC / "api" / "index.ts")))
    for f in (WEB_SRC / "ui").glob("*.tsx"):
        text = code(f)
        found |= set(re.findall(r'action="(/[^"]*)"', text))
        found |= set(re.findall(r"(?:postForm|\.post)\('(/[^']*)'", text))
        for lst in re.findall(r"_POSTS\s*=\s*\[([^\]]*)\]", text):
            found |= set(re.findall(r"'(/[^']*)'", lst))
    return found


def test_the_app_calls_only_declared_paths():
    calls = _app_calls()
    # "/" is the Python page's GET toolbar form the app keeps for its markup
    # and never submits; it is HTML, not part of this contract.
    assert {"/api/fleet", "/api/newwork", "/fs", "/fleet/events", "/tasks", "/items"} <= calls
    assert calls - {"/"} - set(CONTRACT["paths"]) == set()


@pytest.mark.parametrize("fixture", sorted((ROOT / "web" / "parity").glob("fixture*.json")),
                         ids=lambda p: p.name)
def test_captured_live_payloads_fit(fixture):
    """Parity fixtures are real payloads frozen off a live backend (git-ignored,
    so absent on a fresh clone: then this does not run)."""
    data = json.loads(fixture.read_text(encoding="utf-8"))
    _fits(data["fleet"], cc.response_schema(CONTRACT, "/api/fleet"))
    _fits(data["newwork"], cc.response_schema(CONTRACT, "/api/newwork"))


def test_the_check_refuses_broken_payloads():
    fleet = cc.response_schema(CONTRACT, "/api/fleet")
    good = {"version": "v", "instance": "i", "machine": "m", "graph": [[], {}, {}],
            "running": [{"task": "t", "waiting": False}], "slots": 8}
    assert cc.validate(good, fleet, CONTRACT) == []
    mutants = [
        {k: v for k, v in good.items() if k != "machine"},          # required field gone
        {**good, "slots": "8"},                                      # retyped
        {**good, "slots": True},                                     # bool is not a number
        {**good, "graph": [[], {}, {}, {}]},                         # older readers unpack three
        {**good, "running": [{"waiting": False}]},                   # the app keys cards by task
        {**good, "surprise": 1},                                     # undeclared top-level field
    ]
    for m in mutants:
        assert cc.validate(m, fleet, CONTRACT), m
    with pytest.raises(ValueError):
        cc.validate(1, {"type": "integer", "minimum": 0}, CONTRACT)  # unenforced keyword
