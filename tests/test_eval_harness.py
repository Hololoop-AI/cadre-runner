"""The eval harness's deterministic parts run in the normal suite — the
scorer, stats, and tracking must be correct for free, since paid trials
inherit their honesty from here."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scoring import score_spec, passed
from stats import brier, expected_calibration_error, pass_power_k, wilson_interval

EXPECTED = json.load(open(Path(__file__).resolve().parent.parent
                          / "evals/stories/tag-notes.expected.json"))["claims"]

good = {"artifact_md": "Add tags.", "open_questions": [],
        "slices": [{"name": "t", "flow": "vertical-tdd",
                    "validation_path": "add --tag through storage to filtered list output",
                    "files": ["notes/cli.py"]}]}
assert passed(score_spec(good, EXPECTED))

# each claim must be independently violable — an eval that can't fail is decoration
bad_cases = {
    "fragmented": {**good, "slices": [dict(good["slices"][0], name=f"s{i}") for i in range(4)]},
    "no_validation_path": {**good, "slices": [dict(good["slices"][0], validation_path="x")]},
    "wrong_flow": {**good, "slices": [dict(good["slices"][0], flow="devops")]},
    "no_files": {**good, "slices": [dict(good["slices"][0], files=[])]},
    "blocking_question": {**good, "open_questions": [{"question": "?", "blocking": True}]},
    "world_leak": {**good, "artifact_md": "mirror what the daemon does"},
}
for label, case in bad_cases.items():
    assert not passed(score_spec(case, EXPECTED)), f"{label} should fail"

# stats: honesty at small n
lo, hi = wilson_interval(3, 3)
assert lo < 0.75, "3/3 must not read as certainty"
assert wilson_interval(0, 0) == (0.0, 1.0)
assert abs(pass_power_k(9, 10, 8) - 0.9 ** 8) < 1e-12
assert expected_calibration_error([(0.9, False)] * 10) > 0.8   # overconfidence is visible
assert brier([(1.0, True)]) == 0.0

# tracking: jsonl always lands, even with mlflow absent or broken
os.environ["CADRE_SIGNALS_JSONL"] = tempfile.mktemp()
os.environ["CADRE_MLFLOW_URI"] = "sqlite:////nonexistent-dir/x.db"  # force the failure path
import importlib
import tracking
importlib.reload(tracking)
rec = tracking.log_trial("spec-eval", "t", "sonnet", score_spec(good, EXPECTED), {"duration_s": 1})
assert rec["passed"]
assert Path(os.environ["CADRE_SIGNALS_JSONL"]).exists(), "jsonl must land regardless of mlflow"

print("eval harness tests: all passed")
