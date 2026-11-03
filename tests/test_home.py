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


def test_running_strip_shows_each_card_without_node_names():
    cards = [{"task": "t", "title": "Build the strip", "doing": "Bash pytest, just now",
              "agents": 2, "page": "", "page_title": "", "item": "root",
              "item_title": "Home redesign", "folder": "/w/repo", "started": NOW - 600,
              "deadline": NOW + 6600, "waiting": False, "node": "implement"},
             {"task": "q", "title": "Queued one", "waiting": True, "item": "q",
              "folder": "/w", "started": NOW - 5}]
    html = statusd.render_running(cards, NOW)
    assert "Running now" in html and "Build the strip" in html
    assert "2 agents · Bash pytest, just now" in html
    assert "not written yet" in html and "Home redesign" in html and "/w/repo" in html
    assert "10m of 120m" in html
    assert "waiting for a slot" in html
    assert "implement" not in html


def test_running_strip_says_so_when_nothing_runs():
    assert "No agent is running" in statusd.render_running([], NOW)


def test_home_footer_drops_the_legacy_dashboard_link():
    html = statusd.render_home({}, now=NOW, fleet_html="")
    assert "legacy dashboard" not in html and "/index.html" not in html


def test_backend_section_draws_the_strip_above_the_work_list():
    data = {"instance": "x", "running": [
        {"task": "t", "title": "On the strip", "item": "t", "started": NOW, "folder": "/w"}],
        "snap": {}, "projs": {}}
    html = statusd.render_backend({"url": "http://m"}, data, None, NOW)
    assert html.index("Running now") < html.index("On the strip")


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


def test_home_draws_items_with_pages_threads_slot_and_started_agents():
    snap = {"surfaces": [_sf("r1", "root", NOW - 50, title="Root page"),
                         _sf("k1", "kid", NOW - 10, title="Kid page")]}
    html = statusd.render_fleet(snap, NOW, tree=TREE, by_item=True)
    assert html.count('class="rowline item fold"') == 1
    assert 'data-item="root"' in html and 'data-project="cadre"' in html
    assert "Where to take Cadre next" in html and "2 pages" in html and "2 agents" in html
    assert html.count('href="/view/k1"') == 1                       # never linked twice
    assert "Message threads" in html and 'class="threads" data-item="root"' in html
    assert "Agents started from this" in html and "Track A helper" in html
    assert 'class="projfilter"' in html and '<option value="cadre">' in html
    assert 'class="repo">cadre<' not in html                        # no project sections
