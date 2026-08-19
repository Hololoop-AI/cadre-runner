"""Smoke tests for the NEX-150 auto-merge policy. Run: python3 tests/test_automerge.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runnerlib.automerge import decide

GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]
RED = [{"name": "test", "status": "completed", "conclusion": "failure"}]
POLICY = {"tests": True, "build": True, "contracts": True}

def pr(body="", labels=(), draft=False, mergeable=True):
    return {"body": body, "labels": [{"name": l} for l in labels], "draft": draft, "mergeable": mergeable}

assert decide("tests", pr(), GREEN, POLICY, False)[0]
assert not decide("tests", pr(), RED, POLICY, False)[0]
assert not decide("tests", pr(), [], POLICY, False)[0]                    # no checks yet
assert not decide("tests", pr(labels=["hold"]), GREEN, POLICY, False)[0]  # hold label
assert not decide("tests", pr(), GREEN, POLICY, True)[0]                  # human had last word
assert not decide("planning", pr(), GREEN, POLICY, False)[0]              # never planning
assert decide("build", pr(), GREEN, POLICY, False)[0]
assert not decide("build", pr(draft=True), GREEN, POLICY, False)[0]
assert not decide("contract", pr("stuff"), GREEN, POLICY, False)[0]       # no confidence line
assert decide("contract", pr("Confidence: high — direct from spec"), GREEN, POLICY, False)[0]
assert not decide("contract", pr("Confidence: medium — mock boundary unclear"), GREEN, POLICY, False)[0]
assert not decide("tests", pr(), GREEN, {"tests": False, "build": True, "contracts": True}, False)[0]
assert decide("tests", pr("Confidence: high — clean first pass"), GREEN, POLICY, False)[0]
assert not decide("tests", pr("Confidence: low — driver call on error case"), GREEN, POLICY, False)[0]
assert not decide("tests", pr("Confidence: medium — fixes re-verified"), GREEN, POLICY, False)[0]
assert not decide("build", pr("Confidence: low — reviewer flagged wiring gap"), GREEN, POLICY, False)[0]
assert decide("build", pr("Confidence: high — review clean"), GREEN, POLICY, False)[0]
assert decide("build", pr("no line here"), GREEN, POLICY, False)[0]       # legacy path: green suffices
skipped = [{"name": "lint", "status": "completed", "conclusion": "skipped"}] + GREEN
assert decide("tests", pr(), skipped, POLICY, False)[0]

# -- title lint (NEX-166): grammar enforced when slug provided ----------------
from runnerlib.automerge import title_ok

assert title_ok("tests", "[nex-1][tests][2/3] flow-dispatch", "nex-1")
assert title_ok("tests", "[nex-1][tests] flow-dispatch", "nex-1")          # order token optional
assert title_ok("contract", "[nex-1][contracts][1/3] roster", "nex-1")     # role contract -> stage contracts
assert not title_ok("build", "[build] adversarial-review", "nex-1")        # the old drifted format
assert not title_ok("tests", "[nex-2][tests][2/3] x", "nex-1")             # wrong story
assert title_ok("planning", "[planning] NEX-158: Relocate state", "x")
assert not title_ok("planning", "NEX-158 planning", "x")
assert title_ok("final", "[story] NEX-128: Locked CLI tests", "x")
tpr = pr("Confidence: high — review clean")
tpr["title"] = "[nex-1][build][1/2] dispatch"
assert decide("build", tpr, GREEN, POLICY, False, slug="nex-1")[0]
tpr["title"] = "[build] dispatch"
merged, why = decide("build", tpr, GREEN, POLICY, False, slug="nex-1")
assert not merged and why.startswith("malformed title")
assert decide("build", tpr, GREEN, POLICY, False)[0]                       # no slug = lint off (legacy)
print("automerge policy tests: all passed")
