"""The runs view's data: when work came back, one row per task or per turn.
Run directly: python3 -m pytest tests/test_recent.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import recent

TURNS = [
    {"ended": 100.0, "story": "t-a", "stage": "task", "ok": True, "seconds": 10, "cost_usd": 1.0},
    {"ended": 300.0, "story": "t-b", "stage": "task", "ok": True, "seconds": 50, "cost_usd": 0.5},
    {"ended": 200.0, "story": "t-a", "stage": "implement", "ok": False, "seconds": 30,
     "cost_usd": None},
]
OWNERS = {"t-a": "alpha", "t-b": "beta"}


def test_last_returned_is_the_newest_turn_end():
    assert recent.last_returned(TURNS) == {"t-a": 200.0, "t-b": 300.0}


def test_latest_view_folds_turns_into_one_row_per_task():
    rows = recent.rows(TURNS, OWNERS, {})
    assert [r["task"] for r in rows] == ["t-b", "t-a"]
    a = rows[1]
    # the newest turn's node and outcome, the task's totals
    assert (a["node"], a["ok"], a["turns"], a["seconds"], a["cost"]) == (
        "implement", False, 2, 40.0, 1.0)


def test_turns_view_sorts_and_filters():
    assert len(recent.rows(TURNS, OWNERS, {}, view="turns")) == 3
    oldest = recent.rows(TURNS, OWNERS, {}, view="turns", sort="oldest")
    assert [r["ended"] for r in oldest] == [100.0, 200.0, 300.0]
    assert [r["task"] for r in recent.rows(TURNS, OWNERS, {}, sort="longest")] == ["t-b", "t-a"]
    assert [r["task"] for r in recent.rows(TURNS, OWNERS, {}, project="alpha")] == ["t-a"]
    assert [r["node"] for r in recent.rows(TURNS, OWNERS, {}, view="failed")] == ["implement"]
    assert [r["task"] for r in recent.rows(TURNS, OWNERS, {}, q="beta")] == ["t-b"]
    # names off a query string that mean nothing fall back to the defaults
    assert recent.rows(TURNS, OWNERS, {}, view="bogus", sort="bogus") == recent.rows(
        TURNS, OWNERS, {})


def test_pages_by_task_links_the_newest_page_and_open_view_uses_it():
    store = {
        "/x1.html": {"task": "t-a", "path": "/session/k1", "key": "k1", "open": False,
                     "opened": 1.0},
        "/x2.html": {"task": "t-a", "path": "/session/k2", "key": "k2", "open": True,
                     "opened": 2.0},
        "external:y": {"kind": "external", "path": "", "key": ""},
    }
    pages = recent.pages_by_task(store)
    assert pages["t-a"]["key"] == "k2"
    assert [r["task"] for r in recent.rows(TURNS, OWNERS, pages, view="open")] == ["t-a"]


def test_search_matches_the_page_title_too():
    pages = {"t-b": {"task": "t-b", "path": "/session/k", "key": "k", "title": "Kitchen playbook"}}
    assert [r["task"] for r in recent.rows(TURNS, OWNERS, pages, q="kitchen")] == ["t-b"]


def test_search_matches_a_pasted_task_id_with_its_hyphens():
    turns = [{"ended": 1.0, "story": "task-fix-it-20261004-000000-abcdef", "stage": "task",
              "ok": True, "seconds": 1, "cost_usd": None}]
    rows = recent.rows(turns, {}, {}, q="task-fix-it-20261004")
    assert [r["task"] for r in rows] == ["task-fix-it-20261004-000000-abcdef"]
