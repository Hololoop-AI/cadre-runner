"""Smoke tests for board-driven intake: trigger predicate, dedup, status text.
Run directly: python3 tests/test_board.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import board as board_mod
from runnerlib.board import LinearBoard, issue_record, status_text


def issue(ident="PIPE-3", labels=(), assignee=None):
    """A neutral issue record — the shape every provider hands back."""
    return issue_record(id=f"id-{ident}", key=ident, title="t",
                        url="https://linear.app/x", labels=labels,
                        assignee=assignee)


def linear_node(ident="PIPE-3", labels=(), assignee=None):
    """The raw Linear GraphQL node, as the API returns it."""
    return {"id": f"id-{ident}", "identifier": ident, "title": "t",
            "description": "the body", "url": "https://linear.app/x",
            "labels": {"nodes": [{"name": l} for l in labels]},
            "assignee": assignee}


def board_with(cfg):
    b = LinearBoard({"team": "T", "trigger_state": "cadre", **cfg})
    b.api_key = "test"
    return b


def test_api_key_read_from_env(monkeypatch):
    # Regression: the provider must actually read its key env at construction —
    # dropping this line crashed the daemon at startup (`enabled` had nothing
    # to look at).
    monkeypatch.setenv("MY_KEY", "sekrit")
    b = LinearBoard({"team": "T", "trigger_state": "cadre", "api_key_env": "MY_KEY"})
    assert b.api_key == "sekrit" and b.enabled
    monkeypatch.delenv("MY_KEY")
    b = LinearBoard({"team": "T", "trigger_state": "cadre", "api_key_env": "MY_KEY"})
    assert not b.enabled


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


def test_normalization_is_the_provider_contract():
    """Linear's field names stop at the provider: `candidates()` hands back the
    neutral record, and that is all any caller may read."""
    rec = LinearBoard._normalize(linear_node(labels=["Cadre"],
                                             assignee={"name": "B", "email": "b@x"}))
    assert set(rec) == {"id", "key", "title", "body", "url", "labels", "assignee"}
    assert rec["key"] == "PIPE-3" and rec["id"] == "id-PIPE-3"
    assert rec["body"] == "the body"          # Linear calls it `description`
    assert rec["labels"] == ["Cadre"]         # a flat list, not {nodes: [{name}]}
    assert rec["assignee"]["email"] == "b@x"

    # a card with nothing filled in still produces every key
    bare = LinearBoard._normalize(
        {"id": "i", "identifier": "P-1", "title": "t", "description": None,
         "url": None, "labels": {"nodes": []}, "assignee": None})
    assert bare["body"] == "" and bare["url"] == "" and bare["labels"] == []
    assert bare["assignee"] is None


def test_provider_lookup_is_table_driven():
    assert board_mod.make_board({}) is None                     # no provider = disabled
    assert isinstance(board_mod.make_board({"provider": "linear", "linear": {"team": "T",
                                            "trigger_state": "cadre"}}), LinearBoard)
    # the sub-section is found by the provider's own name, so a second entry in
    # PROVIDERS is the whole cost of a second tracker
    assert set(board_mod.PROVIDERS) == {"linear"}
    try:
        board_mod.make_board({"provider": "jira"})
        assert False, "an unknown provider must be rejected"
    except SystemExit as e:
        assert "linear" in str(e)


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


def test_phase_states_are_configuration():
    """Which tracker state a phase means is workspace vocabulary — the mirror
    reads the operator's map, and falls back to config.py's defaults."""
    moves = []

    class FakeBoard:
        enabled = True

        def comment(self, issue_id, body):
            return "c1"

        def edit_comment(self, cid, body):
            pass

        def move(self, issue_id, state):
            moves.append(state)

    def story():
        return {"story_id": "PIPE-3", "repo": "o/r", "phase": "slices",
                "status": "active", "planning_pr": 30, "slices": {},
                "iterations": {"interrogate": 0, "revise": {}},
                "board": {"issue_id": "id-PIPE-3"}}

    board_mod.mirror_status(FakeBoard(), story(), 5, log=lambda *a: None)
    assert moves == ["In Progress"]                    # the shipped default

    moves.clear()
    board_mod.mirror_status(FakeBoard(), story(), 5, log=lambda *a: None,
                            phase_states={"slices": "Doing"})
    assert moves == ["Doing"]                          # this workspace's name

    # one source of truth for the defaults: config.py's, not a copy
    from runnerlib import config
    assert board_mod.DEFAULT_PHASE_STATES is config.DEFAULTS["intake"]["phase_states"]


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
    print("board smoke tests: all passed")
