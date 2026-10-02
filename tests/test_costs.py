"""The costs view: history.jsonl added up by node, project and task, and shown
on the fleet. Run directly: python3 tests/test_costs.py"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import statusd
from runnerlib import costs

NOW = 1_700_000_000.0
DAY = 86_400.0


def _history(root: Path) -> None:
    lines = [
        {"ended": NOW - 10 * DAY, "story": "task-old-spec-20260901-000000-aaaaaa",
         "stage": "task", "ok": True, "seconds": 300, "model": "opus", "cost_usd": 10.0},
        {"ended": NOW - 2 * DAY, "story": "task-build-it-20260920-000000-bbbbbb",
         "stage": "implement", "ok": True, "seconds": 600, "model": "opus", "cost_usd": 4.5},
        {"ended": NOW - 3600, "story": "task-build-it-20260920-000000-bbbbbb",
         "stage": "task", "ok": True, "seconds": 120, "model": "opus", "cost_usd": 1.25},
        # lost turn: no reported cost — unpriced, never $0
        {"ended": NOW - 60, "story": "task-build-it-20260920-000000-bbbbbb",
         "stage": "task", "ok": False, "seconds": 15, "model": "opus", "cost_usd": None},
    ]
    (root / "history.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in lines) + "{not json\n")
    (root / "registry.json").write_text(json.dumps({
        "tasks": {"task-build-it-20260920-000000-bbbbbb": {"project": "cadre-runner"}},
        "stories": {}}))


def test_summarize_cuts_three_ways_and_keeps_unpriced_apart():
    with tempfile.TemporaryDirectory() as root:
        _history(Path(root))
        turns, owners = statusd.load_costs(Path(root))
        s = costs.summarize(turns, owners, now=NOW)
    assert s["runs"] == 4 and s["priced"] == 3 and s["unpriced"] == 1
    assert s["total_usd"] == 15.75
    assert s["last_7d_usd"] == 5.75 and s["last_24h_usd"] == 1.25
    nodes = {b["key"]: b for b in s["by_node"]}
    assert nodes["task"]["usd"] == 11.25 and nodes["task"]["unpriced"] == 1
    assert nodes["implement"]["usd"] == 4.5
    projects = {b["key"]: b["usd"] for b in s["by_project"]}
    assert projects == {"unknown": 10.0, "cadre-runner": 5.75}
    # most expensive first
    assert [b["key"] for b in s["by_task"]][0].startswith("task-old-spec")
    assert costs.per_task(turns) == {"task-old-spec-20260901-000000-aaaaaa": 10.0,
                                     "task-build-it-20260920-000000-bbbbbb": 5.75}


def test_old_task_without_a_project_is_filed_by_its_directory():
    reg = {"tasks": {"t-old": {"cwd": "/p/review-surface/.review-surface"},
                     "t-new": {"project": "cadre-runner", "cwd": "/elsewhere"},
                     "t-tmp": {"cwd": "/tmp/scratch"}}}
    projs = {"rs": {"name": "review-surface", "dirs": ["/p/review-surface"]},
             "hitl": {"name": "hitl", "dirs": ["/p/review-surface/.review-surface"]}}
    # the closest directory wins, a recorded project always wins
    assert costs.task_projects(reg, projs) == {"t-old": "hitl", "t-new": "cadre-runner"}


def test_missing_history_is_no_spend():
    with tempfile.TemporaryDirectory() as root:
        turns, owners = statusd.load_costs(Path(root))
    s = costs.summarize(turns, owners, now=NOW)
    assert s["runs"] == 0 and s["total_usd"] == 0.0 and s["by_task"] == []


def test_a_torn_log_or_odd_registry_is_skipped_not_fatal():
    """The home page reads the log on every refresh, so a line caught
    half-written, a non-finite cost or a registry that is not an object must
    never turn the fleet into a 500."""
    with tempfile.TemporaryDirectory() as root:
        r = Path(root)
        good = json.dumps({"ended": NOW, "story": "t", "stage": "task", "cost_usd": 2.0})
        (r / "history.jsonl").write_bytes(
            good.encode() + b"\n" + b'{"story": "t", "cost_usd": NaN}\n'
            + b'{"story": "t", "note": "caf\xc3')          # cut mid-character
        (r / "registry.json").write_text("[]")
        turns, owners = statusd.load_costs(r)
    assert owners == {}
    s = costs.summarize(turns, owners, now=NOW)
    assert s["total_usd"] == 2.0 and s["unpriced"] == 1
    json.dumps(s, allow_nan=False)                         # /costs.json stays valid JSON


def test_costs_page_reads_as_money_and_names_tasks():
    with tempfile.TemporaryDirectory() as root:
        _history(Path(root))
        page = statusd.render_costs(costs.summarize(*statusd.load_costs(Path(root)), now=NOW),
                                    now=NOW)
    assert page.startswith("<!doctype html>")
    assert "$15.75" in page and "build it" in page and "implement" in page
    assert "1 turn reported no cost" in page


def test_fleet_row_shows_what_its_task_spent():
    snap = {"surfaces": [{"kind": "task", "task": "task-build-it-20260920-000000-bbbbbb",
                          "title": "Build it", "path": "/session/abc", "opened": NOW - 60,
                          "project": "cadre-runner"}]}
    html = statusd.render_fleet(snap, now=NOW, spent={
        "task-build-it-20260920-000000-bbbbbb": 5.75})
    assert "$5.75" in html
    assert "$" not in statusd.render_fleet(snap, now=NOW)


def test_usd_format():
    assert costs.usd(0.414) == "$0.41" and costs.usd(12.3) == "$12.30"
    assert costs.usd(1176.24) == "$1,176"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
