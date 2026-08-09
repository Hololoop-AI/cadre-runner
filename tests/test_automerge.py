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
assert decide("build", pr("Confidence: low"), GREEN, POLICY, False)[0]    # confidence gate is contracts+tests only
skipped = [{"name": "lint", "status": "completed", "conclusion": "skipped"}] + GREEN
assert decide("tests", pr(), skipped, POLICY, False)[0]
print("automerge policy tests: all passed")
