"""Every round of a dialogue is on its row, and every task's item is drawn
the same way, tree or not (driver, 14 Nov 2026).

A dialogue page is one review-surface session rewritten every round; the
rounds the driver already read survive only as `<page>-r<N>.html` copies
(tasks.archive_round). The row's earlier rounds used to list sessions only, so
a fifth round showed one earlier page. And an item whose task started nothing
fell back to a plain list without the graph's dot, round count or timestamp.

No claude, no network, no review-surface CLI.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import conversations as conv

NOW = 1_800_000_000.0


def test_kept_rounds_are_the_pages_own_copies_only():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for name in ("t.html", "t-r1.html", "t-r2.html", "t-r10.html",
                     "t-implement.html", "t-implement-r1.html", "t-r2-media"):
            (root / name).write_text("<title>x</title>")
        got = conv.kept_rounds(str(root / "t.html"))
        assert [r["round"] for r in got] == [1, 2, 10]
        assert [r["round"] for r in conv.kept_rounds(str(root / "t-implement.html"))] == [1]
        assert conv.kept_rounds("") == []


def test_load_pages_carries_each_pages_kept_rounds():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        page = root / "t.html"
        page.write_text("<title>Now</title>")
        (root / "t-r1.html").write_text("<title>Then</title>")
        (root / "state.json").write_text(json.dumps({"sessions": {
            "a" * 16: {"file": str(page), "updated_at": "2026-11-14T10:00:00Z"}}}))
        pages = conv.load_pages(root)
        assert [(r["round"], r["title"]) for r in pages["a" * 16]["rounds"]] == [(1, "Then")]


def test_a_fifth_round_lists_all_four_before_it():
    head, plan = "b" * 16, "a" * 16
    pages = {plan: {"file": "/s/t.html", "title": "plan", "updated": 10, "ended": True},
             head: {"file": "/s/t-implement.html", "title": "impl", "updated": 90,
                    "rounds": [{"round": n, "file": f"/s/t-implement-r{n}.html",
                                "at": 10.0 * n} for n in (1, 2, 3, 4)]}}
    [row] = conv.collapse([{"path": f"/session/{head}", "task": "t", "opened": 90}],
                          [{"type": "derived-from", "from": head, "to": plan}], {}, pages)
    keys = [e["key"] for e in row["earlier"]]
    # the head's own rounds, newest first, then the page it carried on
    assert keys == [f"{head}-r4", f"{head}-r3", f"{head}-r2", f"{head}-r1", plan]
    assert row["earlier"][0]["path"] == "/open?file=/s/t-implement-r4.html"
