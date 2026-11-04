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
    assert "2 subagents · Bash pytest, just now" in html
    assert "not written yet" in html and "under" in html and "Home redesign" in html
    assert "/w/repo · 10 min of 120" in html and 'style="width:8%"' in html    # the clock bar
    assert "waiting for a slot" in html and "1 · 1 waiting for a slot" in html
    assert "implement" not in html
    assert "1 of 4 slots" in statusd.render_running(cards, NOW, slots=4)


def test_running_strip_says_so_when_nothing_runs():
    assert "No agent is running" in statusd.render_running([], NOW)


def test_home_footer_drops_the_legacy_dashboard_link():
    html = statusd.render_home({}, now=NOW, fleet_html="")
    assert "legacy dashboard" not in html and "/index.html" not in html


def test_the_home_draws_the_strip_above_the_work_list():
    data = {"instance": statusd.INSTANCE, "slots": 4, "running": [
        {"task": "t", "title": "On the strip", "item": "t", "started": NOW, "folder": "/w"}],
        "snap": {"surfaces": [_sf("p0", "t", NOW, title="In the list")]}, "projs": {}}
    running, work = statusd.fleet_parts([({"url": "http://m"}, data, None)], NOW)
    assert "On the strip" in running and "1 of 4 slots" in running
    assert "In the list" in work and "machine ·" not in work      # one machine: no heading
    html = statusd.render_home({}, now=NOW, fleet_html=(running, work))
    assert html.index("Running now") < html.index("On the strip") < html.index(
        'id="workbar"') < html.index("In the list")


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
    assert html.count('<details class="item"') == 1
    assert 'data-item="root"' in html and 'data-project="cadre"' in html
    assert "Where to take Cadre next" in html and "Agent working" in html
    assert 'href="/view/k1"' in html and 'href="/view/r1"' in html   # heads and their paths
    assert 'class="threads" data-item="root"' in html               # track B's slot
    assert "Pages, as the tree that made them" in html and "Track A helper" in html
    assert '<span class="badge work">running</span>' in html        # Track A is working
    assert 'class="repo">cadre<' not in html                        # no project sections
    bar = statusd.work_toolbar({"cadre": {"id": "cadre", "name": "Cadre", "dirs": []}})
    assert 'class="projfilter"' in bar and '<option value="cadre">Cadre</option>' in bar


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


def test_home_view_folds_idle_and_keeps_hidden_items_searchable():
    surfaces = [_sf(f"p{n}", f"t{n}", NOW - n * 60, title=f"Work {n}") for n in range(4)]
    store = {"t3": {"hidden_at": NOW}}
    html = statusd.render_fleet({"surfaces": surfaces}, NOW, by_item=True,
                                view={"limit": "2"}, item_store=store)
    assert '<span class="n">1</span> more' in html                    # t2 is past the limit
    assert html.index("Work 0") < html.index("Work 1") < html.index("idlefold") < html.index(
        "Work 2")
    assert 'class="item offlist" data-fold="t3"' in html               # rendered for search
    assert 'action="/items"' in html and "Hide from home" in html
    done = statusd.render_fleet({"surfaces": surfaces}, NOW, by_item=True,
                                view={"show": "hidden"}, item_store=store)
    assert '<details class="item" data-fold="t3"' in done and "Show on home" in done


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


def test_an_item_with_an_unseen_page_carries_the_dot_and_the_unopened_filter_finds_it():
    surfaces = [_sf("p0", "t0", NOW, title="Changed"), _sf("p1", "t1", NOW, title="Seen")]
    statuses = {"p0": {"unseen": True}, "p1": {"unseen": None}}
    html = statusd.render_fleet({"surfaces": surfaces}, NOW, statuses=statuses, by_item=True,
                                view={"show": "unopened"})
    assert html.count('class="unseen"') == 1 and 'data-unopened="1"' in html
    assert '<details class="item" data-fold="t0"' in html
    assert 'class="item offlist" data-fold="t1"' in html


# ------------------------------------------------------------- new work

def test_new_work_dialog_has_every_field_and_preselects_a_parent():
    html = statusd.render_home({}, now=NOW, fleet_html="",
                               parents=[("root", "Where to take Cadre next"), ("x", "Other")],
                               parent="root")
    form = html[html.index('<dialog id="newwork"'):]
    form = form[:form.index("</dialog>")]
    assert 'name="text"' in form and 'name="cwd"' in form and 'aria-controls="worksug"' in form
    assert '<option value="root" selected>Where to take Cadre next</option>' in form
    assert 'name="tags"' in form and 'name="run"' in form
    assert '<dialog id="newwork" data-open>' in html                # opens itself on load
    assert 'id="newworkbtn"' in html                                # the header's + New work


def test_parent_choices_are_task_items_only():
    data = {"snap": {"surfaces": [_sf("r1", "root", NOW), {"path": "/session/z", "key": "z",
                                                            "opened": NOW}]},
            "tree": TREE}
    assert statusd.parent_choices(data) == [("root", "Where to take Cadre next")]


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

def test_catch_me_up_shows_only_on_idle_items_filed_under_it():
    surfaces = [{**_sf(f"p{n}", f"t{n}", NOW - n * 60, title=f"Work {n}"), "cwd": "/w/repo"}
                for n in range(3)]
    statuses = {"p2": {"viewed_at": "2026-11-01T10:00:00Z"}}
    html = statusd.render_fleet({"surfaces": surfaces}, NOW, statuses=statuses, by_item=True,
                                view={"limit": "2"})
    # Every item with a folder carries the button; only an idle one shows it,
    # because the page script moves items in and out of Idle as the limit changes.
    assert html.count(">Catch me up</button>") == 3
    assert ".items details.item:not([data-state=idle]) .cmu{display:none}" in statusd._HOME_CSS
    assert 'data-fold="t2" data-item="t2" data-project="cadre" data-projname="cadre" ' \
           'data-state="idle"' in html
    form = html[html.index('data-fold="t2"'):]
    form = form[:form.index(">Catch me up</button>")]
    assert 'name="parent" value="t2"' in form and 'name="cwd" value="/w/repo"' in form
    assert "Work 2" in form and "/a/p2.html" in form and "2026-11-01" in form


def test_track_hooks_are_empty_slots():
    assert statusd.render_inbox({}) == "" and statusd.render_footer_hooks() == ""


def test_sorted_by_spend_each_item_shows_its_total():
    surfaces = [_sf("p0", "t0", NOW), _sf("p1", "t1", NOW - 60)]
    html = statusd.render_fleet({"surfaces": surfaces}, NOW, by_item=True,
                                spent={"t0": 1.0, "t1": 7.5}, view={"sort": "spent"})
    assert html.index('data-fold="t1"') < html.index('data-fold="t0"')
    assert "$7.50" in html and "$1.00" in html


# ------------------------------------------- runs that end without a page

def test_a_handoff_that_ends_without_writing_a_page_is_flagged_on_its_item():
    """The driver hands a page off; the implement run dies before writing its
    page. The item must say so, or it reads as the last round, untouched."""
    import os
    with tempfile.TemporaryDirectory() as d:
        page = Path(d) / "round1.html"
        page.write_text("<p>round 1</p>")
        os.utime(page, (NOW - 3600, NOW - 3600))
        surfaces = [{**_sf("p0", "t0", NOW - 3600, title="Round 1"), "artifact": str(page)}]
        died = {"story": "t0", "stage": "implement", "ended": NOW - 60, "seconds": 300,
                "ok": True}
        # the handoff's own page, folded out of the fleet's rows, still counts
        handoff = Path(d) / "round1-implement.html"
        store = {str(page): {"task": "t0"}, str(handoff): {"task": "t0"}}
        endings = statusd.bare_endings([died], store, set())
        assert endings == {"t0": {"ended": NOW - 60, "ok": True, "node": "implement"}}
        assert statusd.bare_endings([died], store, {"t0"}) == {}         # still running
        handoff.write_text("<p>implemented</p>")
        os.utime(handoff, (NOW - 100, NOW - 100))                         # it wrote one
        assert statusd.bare_endings([died], store, set()) == {}
        assert statusd.bare_endings([{**died, "ok": False}], store, set())["t0"]["ok"] is False
    html = statusd.render_fleet({"surfaces": surfaces}, NOW, by_item=True, endings=endings)
    assert "implement run ended without a page 1m ago" in html
    assert 'data-needs="0"' in html                                    # it wants the driver


def test_a_launch_that_died_with_no_page_at_all_is_an_item_and_a_child_row():
    pageless = [{"task": "lone", "since": NOW - 900, "project": "", "title": "Lone run",
                 "cwd": "/w", "running": False, "ran": False, "started": True, "failed": ""},
                {"task": "grandkid", "since": NOW - 900, "project": "cadre", "title": "",
                 "cwd": "/w", "running": False, "ran": True, "started": True, "failed": ""}]
    snap = {"surfaces": [_sf("r1", "root", NOW - 50, title="Root page")]}
    html = statusd.render_fleet(snap, NOW, tree=TREE, by_item=True, pageless=pageless)
    lone = html[html.index('data-fold="lone"'):]
    assert "Lone run" in lone and "started, then stopped before writing a page" in lone
    assert "No page yet." in lone
    root = html[html.index('data-fold="root"'):html.index('data-fold="lone"')
                if html.index('data-fold="lone"') > html.index('data-fold="root"') else None]
    # the helper is drawn under the agent that started it, saying what became of it
    assert "Track A helper" in root and "ran, but wrote no page" in root
    assert [it["key"] for it in statusd.fleet_items(snap["surfaces"], TREE, {},
                                                    pageless=pageless)] == ["root", "lone"]


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


def _graph_rows(html):
    inside = html[html.index('<div class="graph">'):]
    rows = inside.split('<div class="page grow')[1:]
    title = lambda r: r.split('class="t"', 1)[1].split("</a>")[0].rsplit(">", 1)[-1]
    dot = lambda r: r.split('<circle class="n ', 1)[1].split('"')[0]
    return [(title(r), dot(r), r) for r in rows]


GRAPH_TREE = {"root": {"title": "Root", "parent": "", "since": 1},
              "a": {"title": "A", "parent": "root", "since": NOW - 950},
              "b": {"title": "B", "parent": "root", "since": NOW - 700, "working": True},
              "a1": {"title": "A1", "parent": "a", "since": NOW - 850}}
GRAPH_PAGES = [_sf("r1", "root", NOW - 900, title="Root round 1"),
               _sf("r2", "root", NOW - 600, title="Root round 2"),
               _sf("a1p", "a", NOW - 800, title="A round 1"),
               _sf("a2", "a", NOW - 100, title="A round 2"),
               _sf("x", "a1", NOW - 300, title="A1 page")]


def test_an_item_draws_its_tree_source_first_each_task_under_what_started_it():
    html = statusd.render_fleet({"surfaces": GRAPH_PAGES}, NOW, tree=GRAPH_TREE, by_item=True,
                                graph=([], {}, {}))
    rows = _graph_rows(html)
    # preorder, launch order: root, then a and everything a started, then b
    assert [(t, d) for t, d, _ in rows] == [("Root round 2", "you"), ("A round 2", "you"),
                                           ("A1 page", "you"), ("B", "work")]
    assert '<span class="rn" title="2 rounds">r2</span>' in rows[0][2]
    # the lines: root's lane runs from its dot down to b, its last child;
    # a's lane runs from its dot to a1 and ends there
    assert 'd="M7,12 V24"' in rows[0][2]                        # root: dot down
    assert 'd="M7,0 V24"' in rows[2][2]                         # root's lane passes a1
    assert 'd="M21,0 V7 Q21,12 26,12 H31"' in rows[2][2]         # a's elbow into a1
    assert 'd="M7,0 V7 Q7,12 12,12 H17"' in rows[3][2]           # root's elbow into b
    assert "M7,12 V24" not in rows[3][2]                         # b is the last: lane ends


def test_a_branch_with_everything_answered_draws_as_one_row_and_its_pages_stay_in_it():
    statuses = {k: {"status": "ended"} for k in ("a2", "x")}
    html = statusd.render_fleet({"surfaces": GRAPH_PAGES}, NOW, tree=GRAPH_TREE, by_item=True,
                                graph=([], {}, {}), statuses=statuses)
    rows = _graph_rows(html)
    assert [(t, d) for t, d, _ in rows] == [("Root round 2", "you"), ("A round 2", "done"),
                                           ("B", "work")]
    assert ">+1</span>" in rows[1][2] and rows[1][2].startswith(' done"')
    assert html.count("A1 page") == 0                  # folded into a, not set loose on top


def test_rows_never_wrap_the_title_gives_way():
    css = statusd.render_home({"surfaces": []}, fleet_html=("", ""), now=NOW)
    assert "flex-wrap:nowrap" in css and "text-overflow:ellipsis" in css
    surfaces = [_sf("r", "root", NOW - 900, title="A long title")]
    html = statusd.render_fleet({"surfaces": surfaces}, NOW, tree={}, by_item=True)
    assert 'class="t"' in html and 'title="A long title"' in html     # full title on hover


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
