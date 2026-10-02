"""Actions are re-read while the daemon runs, like the node registry.

Editing an action file used to need a daemon restart: the set was loaded once
per process. Now a pass re-reads the files when they changed on disk. An edit
that does not load keeps the last good set running and says so, once; a new
action starts at the board's head so it never replays history.

No claude, no network.
"""

import json
import shutil

import pytest

from runnerlib import engine, engine_seam
from tests.test_dialogue_actions import FakeCfg, board, scratch, seeded

ROOT = engine_seam.CONFIG_DIR


def watch(b, name, where_value):
    return {"name": name,
            "trigger": {"namespace": "cadre", "topic": "probe", "kind": "command",
                        "where": [{"op": "payload_eq", "field": "x", "value": where_value}]},
            "emitter": {"type": "write_event", "kind": "signal",
                        "payload": {"seen": "{payload[x]}"}}}


@pytest.fixture
def live(monkeypatch):
    d = scratch()
    files = []
    for p in engine_seam.ACTIONS_PATHS:
        dst = d / p.name
        shutil.copy(p, dst)
        files.append(dst)
    monkeypatch.setattr(engine_seam, "ACTIONS_PATHS", tuple(files))
    engine_seam.reset()
    cfg = FakeCfg(d)
    seeded(d)
    st = engine_seam.state(cfg)
    yield st, files[-1]
    engine_seam.reset()


def add_action(path, action):
    raw = json.loads(path.read_text())
    raw["actions"].append(action)
    path.write_text(json.dumps(raw))


def test_an_unchanged_set_is_not_reread(live):
    st, _ = live
    logs = []
    before = st["actions"]
    assert engine_seam.current_actions(st, logs.append) is before
    assert logs == []


def test_an_added_action_runs_without_a_restart_and_does_not_replay_history(live):
    st, path = live
    b = st["board"]
    engine_seam.heartbeat(b)                            # the daemon has been running
    engine.tick(b, st["actions"], None, None)
    b.write("cadre", "probe", "k", "command", {"target": "probe", "x": "old"})
    add_action(path, watch(b, "probe-watch", "old"))
    add_action(path, {**watch(b, "probe-watch-new", "new")})
    logs = []
    acts = engine_seam.current_actions(st, logs.append)
    assert {"probe-watch", "probe-watch-new"} <= {a["name"] for a in acts}
    assert "added probe-watch, probe-watch-new" in logs[0]
    # the event written before the action existed is history: not fired
    fired = [f["action"] for f in engine.tick(b, acts, None, None).firings]
    assert "probe-watch" not in fired
    b.write("cadre", "probe", "k", "command", {"target": "probe", "x": "new"})
    fired = [f["action"] for f in engine.tick(b, acts, None, None).firings]
    assert fired == ["probe-watch-new"]


def test_a_broken_edit_keeps_the_last_good_set_and_says_so_once(live):
    st, path = live
    good = st["actions"]
    path.write_text(path.read_text() + "{ not json")
    logs = []
    assert engine_seam.current_actions(st, logs.append) is good
    assert engine_seam.current_actions(st, logs.append) is good
    assert len(logs) == 1 and logs[0].startswith("engine: ACTIONS NOT RELOADED")
    assert "still running the previous" in logs[0]

    # a name used twice is refused the same way, never half-applied
    shutil.copy(engine_seam.CONFIG_DIR / path.name, path)
    add_action(path, {**good[0]})
    logs.clear()
    assert engine_seam.current_actions(st, logs.append) is good
    assert "duplicate action name" in logs[0]

    # fixed: it loads on the next pass
    shutil.copy(engine_seam.CONFIG_DIR / path.name, path)
    logs.clear()
    fresh = engine_seam.current_actions(st, logs.append)
    assert [a["name"] for a in fresh] == [a["name"] for a in good]
    assert logs[0].startswith("engine: actions reloaded") and st["actions_error"] is None
