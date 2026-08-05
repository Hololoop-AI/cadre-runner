"""Smoke tests for board-driven intake: trigger predicate, dedup, status text.
Run directly: python3 tests/test_board.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import board as board_mod
from runnerlib.board import LinearBoard, status_text


def issue(ident="PIPE-3", labels=(), assignee=None):
    return {"id": f"id-{ident}", "identifier": ident, "title": "t", "description": "",
            "url": "https://linear.app/x", "labels": {"nodes": [{"name": l} for l in labels]},
            "assignee": assignee}


def board_with(cfg):
    b = LinearBoard({"team": "T", "trigger_state": "cadre", **cfg})
    b.api_key = "test"
    return b


def test_predicate():
    b = board_with({})
    assert b._passes(issue())  # no conditions -> everything in the column passes

    b = board_with({"require_labels": ["cadre"]})
    assert not b._passes(issue(labels=["Bug"]))
    assert b._passes(issue(labels=["Cadre", "Bug"]))  # case-insensitive

    b = board_with({"exclude_labels": ["spike"]})
    assert not b._passes(issue(labels=["Spike"]))
    assert b._passes(issue(labels=["Feature"]))

    b = board_with({"assignee": "brandon@x.com"})
    assert not b._passes(issue(assignee=None))
    assert not b._passes(issue(assignee={"name": "Yulia", "email": "y@x.com"}))
    assert b._passes(issue(assignee={"name": "Brandon", "email": "Brandon@x.com"}))


def test_dedup():
    class Reg:
        def stories(self):
            return {"pipe-2": {"board": {"issue_id": "id-PIPE-2"}},
                    "manual": {}}  # CLI-started story, no board info
    known = board_mod.known_issue_ids(Reg())
    assert known == {"id-PIPE-2"}


def test_status_text():
    story = {"story_id": "PIPE-3", "repo": "o/r", "phase": "slices", "status": "active",
             "planning_pr": 30, "iterations": {"interrogate": 2, "revise": {}},
             "slices": {"dispatch": {"contract_merged": True, "tests_pr": 31}}}
    t = status_text(story, 5)
    assert "Building slices" in t and "#30" in t
    assert "contract ✅" in t and "[tests #31]" in t and "build —" in t

    story["status"] = "escalated"
    assert status_text(story, 5).startswith("⚠️")

    # unchanged text means no write — mirror's change detection depends on determinism
    story["status"] = "active"
    assert status_text(story, 5) == t


if __name__ == "__main__":
    test_predicate()
    test_dedup()
    test_status_text()
    print("board smoke tests: all passed")
