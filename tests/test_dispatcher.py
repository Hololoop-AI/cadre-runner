"""Offline smoke tests for the dispatcher contract and branch classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib.dispatcher import AGENT_MARKER, all_built, dispatch
from runnerlib.registry import classify_branch

LIMITS = {"max_rounds_per_stage": 5, "allowed_actors": ["driver"]}


def story(**over):
    s = {
        "status": "active", "phase": "grill",
        "iterations": {"grill": 0, "revise": {}},
        "slices": {},
    }
    s.update(over)
    return s


def run():
    # branch classification (pipe/ prefix: git forbids nesting under feat/<slug>)
    assert classify_branch("pipe/eng-1/planning", "eng-1") == ("planning", None)
    assert classify_branch("pipe/eng-1/contract-api-create", "eng-1") == ("contract", "api-create")
    assert classify_branch("pipe/eng-1/tests-api-create", "eng-1") == ("tests", "api-create")
    assert classify_branch("pipe/eng-1/build-api-create", "eng-1") == ("build", "api-create")
    assert classify_branch("pipe/other/planning", "eng-1") == (None, None)
    assert classify_branch("feat/eng-1-planning", "eng-1") == (None, None)
    assert classify_branch("random-branch", "eng-1") == (None, None)

    # planning merge in grill phase -> contracts
    a = dispatch(story(), {"kind": "pr_merged", "pr": 1, "role": "planning", "slice": None}, LIMITS)
    assert a == {"type": "run_stage", "stage": "contracts", "slice": None, "pr": 1}, a

    # planning merge out of phase -> phase-locked noop
    a = dispatch(story(phase="slices"), {"kind": "pr_merged", "pr": 1, "role": "planning", "slice": None}, LIMITS)
    assert a["type"] == "noop" and "phase-lock" in a["reason"], a

    # contract merge -> tests for that slice
    a = dispatch(story(phase="slices"), {"kind": "pr_merged", "pr": 4, "role": "contract", "slice": "s1"}, LIMITS)
    assert a == {"type": "run_stage", "stage": "tests", "slice": "s1", "pr": 4}, a

    # tests merge -> build
    a = dispatch(story(phase="slices"), {"kind": "pr_merged", "pr": 5, "role": "tests", "slice": "s1"}, LIMITS)
    assert a["stage"] == "build" and a["slice"] == "s1", a

    # build merge -> noop with built flag
    a = dispatch(story(phase="slices"), {"kind": "pr_merged", "pr": 6, "role": "build", "slice": "s1"}, LIMITS)
    assert a["type"] == "noop" and a.get("built") == "s1", a

    # summon on planning PR in grill phase -> grill run
    a = dispatch(story(), {"kind": "summon", "pr": 1, "role": "planning", "slice": None,
                           "id": 9, "body": "@claude thoughts?", "actor": "driver"}, LIMITS)
    assert a == {"type": "run_stage", "stage": "grill", "slice": None, "pr": 1}, a

    # summon from unknown actor -> dropped
    a = dispatch(story(), {"kind": "summon", "pr": 1, "role": "planning", "slice": None,
                           "id": 9, "body": "@claude", "actor": "rando"}, LIMITS)
    assert a["type"] == "noop" and "allowed_actors" in a["reason"], a

    # agent-marker body -> loop guard
    a = dispatch(story(), {"kind": "summon", "pr": 1, "role": "planning", "slice": None,
                           "id": 9, "body": f"@claude hi {AGENT_MARKER}", "actor": "driver"}, LIMITS)
    assert a["type"] == "noop" and "loop guard" in a["reason"], a

    # grill iteration cap -> escalate
    s = story(); s["iterations"]["grill"] = 5
    a = dispatch(s, {"kind": "summon", "pr": 1, "role": "planning", "slice": None,
                     "id": 9, "body": "@claude", "actor": "driver"}, LIMITS)
    assert a["type"] == "escalate", a

    # summon on build PR -> revise with role
    a = dispatch(story(phase="slices"),
                 {"kind": "summon", "pr": 6, "role": "build", "slice": "s1",
                  "id": 9, "body": "@claude fix naming", "actor": "driver"}, LIMITS)
    assert a["stage"] == "revise" and a["role"] == "build" and a["pr"] == 6, a

    # escalated story ignores everything but /status
    s = story(status="escalated")
    a = dispatch(s, {"kind": "pr_merged", "pr": 1, "role": "planning", "slice": None}, LIMITS)
    assert a["type"] == "noop", a
    a = dispatch(s, {"kind": "command", "name": "status", "actor": "driver", "id": 1, "body": "/status"}, LIMITS)
    assert a["type"] == "post_status", a

    # commands
    a = dispatch(story(), {"kind": "command", "name": "escalate", "actor": "driver", "id": 1, "body": "/escalate"}, LIMITS)
    assert a["type"] == "escalate", a

    # all_built
    s = story(phase="slices", slices={"s1": {"build_merged": True}, "s2": {"build_merged": True}})
    assert all_built(s, open_pipeline_prs=0)
    assert not all_built(s, open_pipeline_prs=1)
    s["slices"]["s2"]["build_merged"] = False
    assert not all_built(s, open_pipeline_prs=0)
    assert not all_built(story(phase="slices"), 0)  # no slices known yet

    print("dispatcher smoke tests: all passed")


if __name__ == "__main__":
    run()
