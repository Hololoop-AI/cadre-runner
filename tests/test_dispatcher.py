"""Offline smoke tests for the dispatcher contract and branch classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib.dispatcher import AGENT_MARKER, all_built, dispatch
from runnerlib.registry import classify_branch

LIMITS = {"max_rounds_per_stage": 5, "allowed_actors": ["driver"]}


def story(**over):
    s = {
        "status": "active", "phase": "interrogate",
        "iterations": {"interrogate": 0, "revise": {}},
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

    # planning merge in interrogate phase -> contracts
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

    # summon on planning PR in interrogate phase -> interrogate run
    a = dispatch(story(), {"kind": "summon", "pr": 1, "role": "planning", "slice": None,
                           "id": 9, "body": "@claude thoughts?", "actor": "driver"}, LIMITS)
    assert a == {"type": "run_stage", "stage": "interrogate", "slice": None, "pr": 1}, a

    # summon from unknown actor -> dropped
    a = dispatch(story(), {"kind": "summon", "pr": 1, "role": "planning", "slice": None,
                           "id": 9, "body": "@claude", "actor": "rando"}, LIMITS)
    assert a["type"] == "noop" and "allowed_actors" in a["reason"], a

    # agent-marker body -> loop guard
    a = dispatch(story(), {"kind": "summon", "pr": 1, "role": "planning", "slice": None,
                           "id": 9, "body": f"@claude hi {AGENT_MARKER}", "actor": "driver"}, LIMITS)
    assert a["type"] == "noop" and "loop guard" in a["reason"], a

    # interrogate iteration cap -> escalate
    s = story(); s["iterations"]["interrogate"] = 5
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

    # all_built measures the PLAN, not the records the runner has discovered.
    # A planned slice that has not opened a PR yet has no record at all, and
    # counting only records let assembly ship a story with a slice unbuilt.
    plan3 = [{"name": "s1", "nodes": ["contract", "tests", "build"]},
             {"name": "s2", "nodes": ["contract", "tests", "build"]},
             {"name": "s3", "nodes": ["contract", "tests", "build"]}]
    s = story(phase="slices", plan_slices=plan3,
              slices={"s1": {"build_merged": True}, "s2": {"build_merged": True}})
    assert not all_built(s, open_pipeline_prs=0)          # s3 planned, no record
    s["slices"]["s3"] = {"build_merged": True}
    assert all_built(s, open_pipeline_prs=0)
    # a slice whose flow ends before a build is complete without one
    s2 = story(phase="slices",
               plan_slices=[{"name": "a", "nodes": ["contract", "tests", "build"]},
                            {"name": "doc", "nodes": ["research"]}],
               slices={"a": {"build_merged": True}})
    assert all_built(s2, open_pipeline_prs=0)

    print("dispatcher smoke tests: all passed")


if __name__ == "__main__":
    run()

# -- ready_actions: flow-aware ready-set dispatch (manifest stories) ----------
from runnerlib.dispatcher import ready_actions
from runnerlib.poller import parse_manifest

def _story(plan, slices, phase="slices"):
    return {"plan_slices": plan, "slices": slices, "phase": phase, "planning_pr": 1}

_plan = [
    {"name": "a", "nodes": ["contract", "tests", "build"], "depends_on": []},
    {"name": "b", "nodes": ["build"], "depends_on": ["a"]},
]
# a's contract open, unmerged: nothing spawnable (contracts are story-wide; b blocked on a)
s = _story(_plan, {
    "a": {"contract_pr": 5, "contract_merged": False, "tests_pr": None, "tests_merged": False,
          "build_pr": None, "build_merged": False},
    "b": {"contract_pr": None, "contract_merged": True, "tests_pr": None, "tests_merged": True,
          "build_pr": None, "build_merged": False},
})
assert ready_actions(s) == []
# a's contract merged, no tests PR yet -> spawn tests for a; b still blocked
s["slices"]["a"]["contract_merged"] = True
acts = ready_actions(s)
assert [(x["stage"], x["slice"]) for x in acts] == [("tests", "a")]
# a fully built -> b (author-only flow) unblocks straight to build
s["slices"]["a"].update(tests_merged=True, build_merged=True)
acts = ready_actions(s)
assert [(x["stage"], x["slice"]) for x in acts] == [("build", "b")]
# b's build PR open -> nothing to spawn
s["slices"]["b"]["build_pr"] = 9
assert ready_actions(s) == []
# legacy story (no manifest) -> never touched
assert ready_actions({"plan_slices": None, "slices": {}, "phase": "slices"}) == []
# wrong phase -> nothing
assert ready_actions(_story(_plan, s["slices"], phase="interrogate")) == []

# -- parse_manifest -----------------------------------------------------------
body = "planning text\n```cadre-manifest\n{\"slices\": [{\"name\": \"x\", \"nodes\": [\"build\"]}]}\n```\n<!-- pipeline-run -->"
assert parse_manifest(body)[0]["name"] == "x"
assert parse_manifest("no block here") is None
assert parse_manifest("```cadre-manifest\nnot json\n```") is None
assert parse_manifest(None) is None
# -- summons only act on open PRs ---------------------------------------------
# "Closing, superseded by X" is ordinary human behavior and must not spawn a
# revise against a PR whose branch is usually already deleted.
_sum = {"kind": "summon", "pr": 88, "role": "contract", "slice": "s1",
        "id": 1, "body": "closing this", "actor": "driver", "source": "issue_comment"}
_closed = story(phase="slices", prs_cache={"88": {"role": "contract", "slice": "s1", "state": "closed"}})
assert dispatch(_closed, _sum, LIMITS)["type"] == "noop"
_open = story(phase="slices", prs_cache={"88": {"role": "contract", "slice": "s1", "state": "open"}})
_act = dispatch(_open, _sum, LIMITS)
assert _act["type"] == "run_stage" and _act["stage"] == "revise", _act
# no cache entry at all (unknown PR) still dispatches as before — the guard is
# about PRs we know to be closed, not about missing knowledge
assert dispatch(story(phase="slices"), _sum, LIMITS)["type"] == "run_stage"

# -- only the CURRENT plan opens the contracts gate ----------------------------
# Re-planning a story wipes its seen-event set, so the old planning PR's merge
# replays as fresh. That stale approval must not start contracts on a plan the
# driver has never read.
_pm = {"kind": "pr_merged", "pr": 73, "role": "planning", "slice": None}
_replan = story(phase="interrogate", planning_pr=117)
assert dispatch(_replan, _pm, LIMITS)["type"] == "noop"
_cur = dict(_pm, pr=117)
assert dispatch(_replan, _cur, LIMITS)["stage"] == "contracts"
# a story with no recorded plan yet still honors the merge (first intake)
assert dispatch(story(phase="interrogate"), _pm, LIMITS)["stage"] == "contracts"

print("ready-set + manifest tests: all passed")
