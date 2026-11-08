"""When the fleet counts as changed, and what one fleet build reads: a
finished turn moves the version (so open fleets re-fetch), and the board's
task requests are read once until the board changes.
Run directly: python3 -m pytest -q tests/test_fleet_changes.py"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import statusd


def test_a_finished_turn_moves_the_fleet_version(monkeypatch, tmp_path):
    monkeypatch.setattr(statusd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(statusd, "STATUS_DIR", tmp_path / "status")
    before = statusd._local_signature()
    with (tmp_path / "history.jsonl").open("a") as f:
        f.write(json.dumps({"ended": 1.0, "story": "task-a", "ok": True}) + "\n")
    assert statusd._local_signature() != before


def _board(path: Path, tasks: list[str]):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, id TEXT, ts REAL, "
                 "topic TEXT, kind TEXT, payload TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS firings (event_id TEXT, outcome TEXT, detail TEXT)")
    for t in tasks:
        conn.execute("INSERT INTO events (id, ts, topic, kind, payload) VALUES (?, ?, ?, 'command', ?)",
                     (f"e-{t}", time.time(), statusd.TASK_TOPIC,
                      json.dumps({"target": statusd.TASK_REQUEST, "task": t})))
    conn.commit()
    conn.close()


def test_task_requests_are_read_once_until_the_board_changes(monkeypatch, tmp_path):
    db = tmp_path / "board.db"
    _board(db, ["task-a"])
    reads = []
    real = sqlite3.connect
    def connect(target, *a, **k):
        if "mode=ro" in str(target):             # the fleet's reads, not this test's writes
            reads.append(target)
        return real(target, *a, **k)

    monkeypatch.setattr(statusd.sqlite3, "connect", connect)
    first = statusd.requested_tasks(tmp_path)
    first["task-a"]["active_runs"]["x"] = 1          # a caller's change stays its own
    again = statusd.requested_tasks(tmp_path)
    assert list(again) == ["task-a"] and again["task-a"]["active_runs"] == {}
    assert len(reads) == 1, "the unchanged board is not queried again"
    _board(db, ["task-b"])
    os.utime(db, ns=(time.time_ns() + 10**9,) * 2)   # a write in the same clock tick still moves it
    assert set(statusd.requested_tasks(tmp_path)) == {"task-a", "task-b"}
    assert len(reads) == 2
