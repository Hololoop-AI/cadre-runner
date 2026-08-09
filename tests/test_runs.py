"""Smoke tests for async run management. Run: python3 tests/test_runs.py"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runnerlib import runs

tmp = Path(tempfile.mkdtemp(prefix="cadre-runs-test-"))


class FakeReg:
    def __init__(self, stories):
        self.data = {"stories": stories}


# -- inventory guards ---------------------------------------------------------
story_a = {"active_runs": {"r1": {"stage": "tests", "slice": "s1", "pr": 7,
                                  "repo": "o/r", "branch": "pipe/x/tests-s1"}}}
story_b = {"active_runs": {}}
reg = FakeReg({"a": story_a, "b": story_b})

assert runs.key_active(story_a, "tests", "s1", 7)
assert not runs.key_active(story_a, "tests", "s2", 7)
assert not runs.key_active(story_b, "tests", "s1", 7)
assert runs.branch_held(reg, "o/r", "pipe/x/tests-s1")
assert not runs.branch_held(reg, "o/r", "pipe/x/tests-s2")
assert not runs.branch_held(reg, "other/repo", "pipe/x/tests-s1")
assert len(runs.all_active(reg)) == 1

# -- spawn/finished/outcome via a stand-in binary -----------------------------
run_dir = tmp / "run1"
wt = tmp / "wt1"
wt.mkdir()
# claude stand-in: `true` exits 0 ignoring args; out.json stays empty
pid = runs.spawn("true", "prompt text", wt, "sonnet", "high", "bypass", 60, run_dir)
run = {"pid": pid, "run_dir": str(run_dir), "worktree": str(wt),
       "model": "sonnet", "started": time.time()}
for _ in range(50):
    if runs.finished(run):
        break
    time.sleep(0.1)
assert runs.finished(run), "exit file never appeared"
ok, result, usage, record = runs.outcome(run)
assert ok and result == "" and record["returncode"] == 0
assert (run_dir / "prompt.txt").read_text() == "prompt text"

# -- outcome parses claude-style json ----------------------------------------
run_dir2 = tmp / "run2"
run_dir2.mkdir()
(run_dir2 / "exit").write_text("0\n")
(run_dir2 / "out.json").write_text(json.dumps(
    {"result": "did the thing", "total_cost_usd": 1.5, "num_turns": 3}))
ok, result, usage, _ = runs.outcome({"pid": 1, "run_dir": str(run_dir2),
                                     "started": time.time()})
assert ok and result == "did the thing" and usage["total_cost_usd"] == 1.5

# -- failure + timeout classification ----------------------------------------
run_dir3 = tmp / "run3"
run_dir3.mkdir()
(run_dir3 / "exit").write_text("124\n")
ok, _, _, record = runs.outcome({"pid": 1, "run_dir": str(run_dir3), "started": time.time()})
assert not ok and record["timed_out"]

# dead pid, no exit file -> finished (lost), not ok
lost = {"pid": 999999999, "run_dir": str(tmp / "nope"), "started": time.time()}
assert runs.finished(lost)
ok, _, _, record = runs.outcome(lost)
assert not ok and record["lost"]

# -- worktree lifecycle -------------------------------------------------------
repo = tmp / "repo"
repo.mkdir()
subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-q", "--allow-empty", "-m", "root"], cwd=repo, check=True)
wt2 = tmp / "wt2"
runs.add_worktree(repo, wt2, "pipe/x/tests-s1", "main")
head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt2,
                      capture_output=True, text=True).stdout.strip()
assert head == "pipe/x/tests-s1", head
runs.remove_worktree(repo, wt2)
assert not wt2.exists()

print("runs smoke tests: all passed")
