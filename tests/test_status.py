"""Smoke tests for the status snapshot writer.
Run directly: python3 tests/test_status.py"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import status


class FakeCfg:
    def __init__(self, root):
        self.data_dir = Path(root)
        self.runner = {"poll_interval": 45}


class FakeReg:
    def stories(self):
        return {
            "nex-126": {
                "story_id": "NEX-126", "title": "Assembly stage", "repo": "o/r",
                "status": "active", "phase": "slices", "planning_pr": 31,
                "board": {"url": "https://linear.app/x"},
                "slices": {"draft-pr-gate": {"contract_merged": True, "tests_pr": 43}},
            },
        }


def test_write_and_shape():
    with tempfile.TemporaryDirectory() as root:
        cfg = FakeCfg(root)
        run = {"stage": "build", "story": "NEX-126", "slice": "draft-pr-gate",
               "pr": 50, "started": 1000.0}
        status.write_status(cfg, FakeReg(), run=run)
        snap = json.loads((cfg.data_dir / "status" / "status.json").read_text())
        assert snap["run"] == run
        assert snap["poll_interval"] == 45 and snap["ts"] > 0
        st = snap["stories"][0]
        assert st["story_id"] == "NEX-126" and st["phase"] == "slices"
        assert st["slices"]["draft-pr-gate"] == {"contract": True, "tests": False, "build": False}

        # idle write clears the run marker
        status.write_status(cfg, FakeReg())
        snap = json.loads((cfg.data_dir / "status" / "status.json").read_text())
        assert snap["run"] is None


def test_log_tail_bounded():
    with tempfile.TemporaryDirectory() as root:
        log = Path(root) / "daemon.log"
        log.write_text("\n".join(f"line {i}" for i in range(5000)))
        tail = status._log_tail(log)
        assert len(tail) == status.LOG_TAIL_LINES
        assert tail[-1] == "line 4999"
        assert status._log_tail(Path(root) / "missing.log") == []


def test_never_raises():
    class BrokenReg:
        def stories(self):
            raise RuntimeError("boom")
    with tempfile.TemporaryDirectory() as root:
        status.write_status(FakeCfg(root), BrokenReg())  # must not raise


if __name__ == "__main__":
    test_write_and_shape()
    test_log_tail_bounded()
    test_never_raises()
    print("status smoke tests: all passed")
