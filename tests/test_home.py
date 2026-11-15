"""The home page redesign: the Running now strip, items as agent threads,
their states and filters, the New work dialog and Catch me up.
Run directly: python3 -m pytest -q tests/test_home.py"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import statusd

NOW = 1_800_000_000.0


def _registry(root: Path, tasks: dict):
    (root / "registry.json").write_text(json.dumps({"stories": {}, "tasks": tasks}))


def _board(root: Path, requests: list[dict]):
    """A board.db holding task-request events, the way tasks.submit_task
    writes them."""
    conn = sqlite3.connect(root / "board.db")
    conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, id TEXT, ts REAL, "
                 "namespace TEXT, topic TEXT, kind TEXT, provenance TEXT, payload TEXT)")
    conn.execute("CREATE TABLE firings (event_id TEXT, outcome TEXT, detail TEXT)")
    for i, r in enumerate(requests):
        conn.execute("INSERT INTO events (id, ts, namespace, topic, kind, provenance, payload) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (f"e{i}", r.pop("ts"), "cadre", statusd.TASK_TOPIC, "command", "{}",
                      json.dumps({"target": statusd.TASK_REQUEST, **r})))
    conn.commit()
    conn.close()


def _active(task, cwd="/w/repo", started=NOW - 600, deadline=NOW + 6600):
    return {"task": task, "cwd": cwd, "session_id": "", "active_runs": {
        "r1": {"stage": "task", "worktree": cwd, "started": started,
               "deadline": deadline, "session_id": ""}}}


# ------------------------------------------------------------ running now

def test_running_agents_reads_active_runs_with_page_item_and_clock():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _registry(root, {"child": _active("child"), "idle": {"task": "idle", "cwd": "/w"}})
        titles = {"child": {"path": "/session/k1", "title": "Child page"}}
        tree = {"root": {"title": "Where to take Cadre next", "parent": "", "since": 1},
                "child": {"title": "child task", "parent": "root", "since": 2}}
        cards = statusd.running_agents(root, NOW, titles=titles, tree=tree)
    assert [c["task"] for c in cards] == ["child"]
    c = cards[0]
    assert c["title"] == "Child page" and c["page"] == "/session/k1"
    assert c["item"] == "root" and c["item_title"] == "Where to take Cadre next"
    assert c["folder"] == "/w/repo" and not c["waiting"]
    assert c["deadline"] - c["started"] == 7200


def test_a_launch_with_no_slot_yet_is_a_waiting_card_and_a_lost_one_is_not():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _registry(root, {})
        _board(root, [{"task": "fresh", "title": "Fresh ask", "cwd": "/w", "ts": NOW - 20},
                      {"task": "lost", "title": "Old ask", "cwd": "/w", "ts": NOW - 4000}])
        cards = statusd.running_agents(root, NOW)
    assert [(c["task"], c["title"], c["waiting"]) for c in cards] == [
        ("fresh", "Fresh ask", True)]


# ------------------------------------------------------------------ items

TREE = {"root": {"title": "Where to take Cadre next", "parent": "", "since": 1},
        "kid": {"title": "Track A", "parent": "root", "since": 2, "working": True},
        "grandkid": {"title": "Track A helper", "parent": "kid", "since": 3}}


def _sf(key, task, opened, project="cadre", title=None):
    return {"kind": "task", "path": f"/session/{key}", "key": key, "task": task,
            "project": project, "opened": opened, "title": title or f"page {key}",
            "artifact": f"/a/{key}.html"}


def test_items_group_pages_under_the_root_of_their_session_tree():
    surfaces = [_sf("r1", "root", NOW - 50), _sf("k1", "kid", NOW - 10),
                _sf("g1", "grandkid", NOW - 30), _sf("lone", "solo", NOW - 5, project="")]
    items = statusd.fleet_items(surfaces, TREE, {})
    assert [it["key"] for it in items] == ["solo", "root"]          # newest touch first
    root = items[1]
    assert root["title"] == "Where to take Cadre next"
    assert root["head"]["key"] == "r1"                              # opens the root's own page
    assert {sf["key"] for sf in root["pages"]} == {"r1", "k1", "g1"}
    assert root["touched"] == NOW - 10


def test_a_page_with_no_task_is_an_item_of_its_own():
    items = statusd.fleet_items([{"path": "/session/x", "key": "x", "title": "Loose",
                                  "opened": NOW}], {}, {})
    assert [(it["key"], it["title"]) for it in items] == [("page:x", "Loose")]


# ------------------------------------------------- states, filters, store

from runnerlib import items as items_mod


def _it(key, touched, working=False, unseen=()):
    return {"key": key, "touched": touched, "working": working, "unseen": list(unseen)}


def test_idle_is_by_count_never_by_age_and_working_is_never_idle():
    items = [_it(f"i{n}", NOW - n * 86400 * 30) for n in range(5)]   # months apart
    items.append(_it("busy", 0, working=True))                        # oldest, but running
    items_mod.assign_states(items, {}, limit=3)
    state = {it["key"]: it["state"] for it in items}
    assert state["busy"] == "working"
    assert [k for k in ("i0", "i1", "i2", "i3", "i4") if state[k] == "your-turn"] == ["i0", "i1"]
    assert state["i2"] == state["i3"] == state["i4"] == "idle"
    # with room for everything, nothing is idle however old
    items_mod.assign_states(items, {}, limit=15)
    assert "idle" not in {it["state"] for it in items}


def test_done_and_hidden_come_from_the_store_and_leave_the_home_view():
    items = [_it("a", NOW), _it("b", NOW), _it("c", NOW)]
    store = {"a": {"done_at": NOW, "tags": ["x"]}, "b": {"hidden_at": NOW}}
    items_mod.assign_states(items, store)
    assert [it["state"] for it in items] == ["done", "hidden", "your-turn"]
    assert [it["key"] for it in items if items_mod.visible(it)] == ["c"]
    assert [it["key"] for it in items if items_mod.visible(it, "done")] == ["a"]
    assert [it["key"] for it in items if items_mod.visible(it, "hidden")] == ["b"]
    assert [it["key"] for it in items if items_mod.visible(it, "all", "x")] == ["a"]


def test_unopened_is_true_only_for_an_unseen_true_page():
    items = [_it("seen", NOW, unseen=[False]), _it("new", NOW, unseen=[None, True]),
             _it("unknown", NOW, unseen=[None])]
    items_mod.assign_states(items, {})
    assert [it["key"] for it in items if items_mod.visible(it, "unopened")] == ["new"]


def test_sorts():
    items = [{"key": "a", "touched": 3, "returned": 1, "spent": 5, "needs": 1},
             {"key": "b", "touched": 1, "returned": 3, "spent": 1, "needs": 0},
             {"key": "c", "touched": 2, "returned": 2, "spent": 9, "needs": 1, "working": True}]
    keys = lambda s: [it["key"] for it in items_mod.order(items, s)]
    assert keys("needs") == ["b", "c", "a"]
    assert keys("result") == ["b", "c", "a"]
    assert keys("untouched") == ["b", "c", "a"]
    assert keys("spent") == ["c", "a", "b"]


def test_store_round_trip_and_refusals():
    with tempfile.TemporaryDirectory() as d:
        assert "Tagged" in items_mod.apply(d, "root", "tag", "cadre", now=NOW)
        items_mod.apply(d, "root", "hide", now=NOW)
        assert items_mod.load(d) == {"root": {"tags": ["cadre"], "done_at": None,
                                              "hidden_at": NOW}}
        items_mod.apply(d, "root", "unhide")
        items_mod.apply(d, "root", "untag", "cadre")
        assert items_mod.load(d) == {}                       # empty records are dropped
        for bad in [("", "done", ""), ("k", "explode", ""), ("k", "tag", "<b>")]:
            try:
                items_mod.apply(d, *bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad}")


def test_post_items_writes_the_store_and_lands_home():
    import test_status
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "status").mkdir()
        srv = test_status._Server(Path(root) / "status")
        try:
            code, loc = srv.post("/items", {"item": "root", "action": "done"})
            assert code == 303 and loc.startswith("/?") and "Marked+done" in loc
            assert items_mod.load(root)["root"]["done_at"]
            code, loc = srv.post("/items", {"item": "root", "action": "tag", "tag": "<x>"})
            assert "Refused" in loc
        finally:
            srv.close()


# ------------------------------------------------------------- new work


def test_post_tasks_passes_parent_and_skill_and_tags_the_item():
    import test_status
    calls = []

    def fake_submit(cfg, text, cwd, **kw):
        calls.append((text, cwd, kw))
        return "task-new"
    import types
    mod = types.ModuleType("runnerlib.tasks")
    mod.submit_task = fake_submit
    sys.modules["runnerlib.tasks"] = mod
    import runnerlib
    keep = getattr(runnerlib, "tasks", None)
    runnerlib.tasks = mod
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "status").mkdir()
        srv = test_status._Server(Path(root) / "status")
        try:
            code, loc = srv.post("/tasks", {"text": "brief me", "cwd": "/w", "parent": "root",
                                            "run": "skill:x", "tags": "cadre, <bad>"})
            assert code == 303 and "task-new" in loc and "Not+tagged" in loc
            assert calls == [("brief me", "/w", {"parent": "root", "run": "skill:x"})]
            assert items_mod.load(root)["root"]["tags"] == ["cadre"]
            srv.post("/tasks", {"text": "alone", "tags": "solo"})
            assert calls[-1] == ("alone", None, {})
            assert items_mod.load(root)["task-new"]["tags"] == ["solo"]
        finally:
            srv.close()
            sys.modules.pop("runnerlib.tasks", None)
            if keep is not None:
                runnerlib.tasks = keep
                sys.modules["runnerlib.tasks"] = keep
            else:
                del runnerlib.tasks


# ------------------------------------------------------------ catch me up


# ------------------------------------------- runs that end without a page


def test_pageless_tasks_keep_launches_with_no_project():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _registry(root, {"t-proj": {"task": "t-proj", "cwd": "/w", "since": NOW,
                                    "project": "p", "session_id": "s"},
                         "t-none": {"task": "t-none", "cwd": "/nowhere", "since": NOW - 5,
                                    "session_id": "s", "last_result": "done"}})
        rows = statusd.pageless_tasks({}, data_dir=root)
        assert [(r["task"], r["project"], r["ran"]) for r in rows] == [
            ("t-proj", "p", False), ("t-none", "", True)]
        assert list(statusd.starting_tasks({}, data_dir=root)) == ["p"]   # project pages


GRAPH_TREE = {"root": {"title": "Root", "parent": "", "since": 1},
              "a": {"title": "A", "parent": "root", "since": NOW - 950},
              "b": {"title": "B", "parent": "root", "since": NOW - 700, "working": True},
              "a1": {"title": "A1", "parent": "a", "since": NOW - 850}}
GRAPH_PAGES = [_sf("r1", "root", NOW - 900, title="Root round 1"),
               _sf("r2", "root", NOW - 600, title="Root round 2"),
               _sf("a1p", "a", NOW - 800, title="A round 1"),
               _sf("a2", "a", NOW - 100, title="A round 2"),
               _sf("x", "a1", NOW - 300, title="A1 page")]


def test_needs_me_first_puts_a_fresh_result_on_top_even_while_an_agent_works_in_the_item():
    items = [{"key": "opened", "touched": NOW - 60, "returned": NOW - 9 * 86400, "needs": 1},
             {"key": "busy", "touched": NOW - 300, "returned": NOW - 120, "needs": 1,
              "working": True},
             {"key": "quiet", "touched": NOW - 3600, "returned": 0, "needs": 1},
             {"key": "stuck", "touched": NOW - 30 * 86400, "returned": 0, "needs": 0}]
    keys = [it["key"] for it in items_mod.order(items, "needs")]
    # stuck wants the driver; busy just returned; opened was only opened
    # recently, its result is nine days old; quiet has no result yet.
    assert keys == ["stuck", "busy", "quiet", "opened"]
