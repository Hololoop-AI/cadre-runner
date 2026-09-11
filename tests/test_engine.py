"""The action engine: loading, trigger matching, the closed where vocabulary,
bodies, emitters, the causal depth cap, and failure-as-a-signal.
Run directly: python3 tests/test_engine.py"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib.blackboard import Board
from runnerlib.engine import (ActionError, DEFAULT_MAX_DEPTH, load_actions, tick,
                              validate_action)
from runnerlib.nodes import Nodes

COMMAND = "claude -p @{prompt_path} --model {model} --key {event[key]}"


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


def board(d: Path) -> Board:
    return Board(d / "board.db")


def nodes(d: Path) -> Nodes:
    n = Nodes(d)
    n.register("reviewer", "review the diff", "opus", COMMAND, emits=["report"])
    return n


def signal(b, key="story:42", status="finished", **kw):
    return b.write("cadre", "stories", key, "signal", {"status": status}, **kw)


# --------------------------------------------------------------------------- loading


def test_actions_load_from_json():
    d = scratch()
    path = d / "actions.json"
    path.write_text(json.dumps({"actions": [{
        "name": "finished-to-review",
        "trigger": {"topic": "stories", "kind": "signal",
                    "where": [{"op": "payload_eq", "field": "status", "value": "finished"}]},
        "body": {"type": "spawn_node", "node": "reviewer"},
        "emitter": {"type": "write_event", "kind": "notify",
                    "payload": {"message": "review spawned"}},
    }]}))

    actions = load_actions(path)
    assert [a["name"] for a in actions] == ["finished-to-review"]
    assert actions[0]["trigger"]["kind"] == "signal"

    # a bare list is accepted too
    path.write_text(json.dumps([actions[0]]))
    assert len(load_actions(path)) == 1

    for bad in (d / "missing.json", None):
        try:
            load_actions(bad or d / "missing.json")
            assert False, "missing file should raise"
        except ActionError:
            pass


def test_unknown_vocabulary_is_rejected_at_load():
    base = {"name": "a", "trigger": {"kind": "signal"},
            "emitter": {"type": "write_event", "kind": "notify"}}

    rejected = [
        # the where vocabulary is closed: no eval, no expressions
        {**base, "trigger": {"kind": "signal",
                             "where": [{"op": "payload_matches", "field": "x", "value": 1}]}},
        {**base, "trigger": {"kind": "signal", "where": [{"op": "payload_eq"}]}},
        {**base, "trigger": {"kind": "signal",
                             "where": [{"op": "payload_in", "field": "x", "values": "abc"}]}},
        {**base, "trigger": {"kind": "signal", "where": [{"op": "no_event_for_key"}]}},
        {**base, "trigger": {"kind": "signal", "when": "later"}},
        {**base, "body": {"type": "invoke_webhook"}},
        {**base, "body": {"type": "run_command"}},
        {**base, "emitter": {"type": "telepathy"}},
        {**base, "emitter": {"type": "in_tray"}},
        {**base, "max_depth": 0},
        {"trigger": {"kind": "signal"}, "emitter": {"type": "hitl", "summary": "x"}},
    ]
    for action in rejected:
        try:
            validate_action(action)
            assert False, f"should have been rejected: {action}"
        except ActionError:
            pass

    # and the valid shapes load
    for ok in ({**base, "trigger": {"kind": "signal", "where": [
                    {"op": "payload_eq", "field": "status", "value": "finished"},
                    {"op": "payload_in", "field": "status", "values": ["finished"]},
                    {"op": "no_event_for_key", "kind": "report"}]}},
               {**base, "emitter": [{"type": "hitl", "summary": "look"},
                                    {"type": "write_event", "kind": "heartbeat"}]}):
        assert validate_action(ok)


# --------------------------------------------------------------------------- triggers


def test_trigger_matches_on_kind_topic_and_where():
    d = scratch()
    b = board(d)
    action = validate_action({
        "name": "on-finished",
        "trigger": {"topic": "stories", "kind": "signal",
                    "where": {"op": "payload_eq", "field": "status", "value": "finished"}},
        "emitter": {"type": "write_event", "topic": "review", "kind": "notify",
                    "payload": {"message": "picked up {event[key]}"}},
    })

    signal(b, "story:1", "started")                    # where excludes it
    hit = signal(b, "story:2", "finished")
    b.write("cadre", "chatter", "story:3", "signal", {"status": "finished"})  # topic
    b.write("cadre", "stories", "story:4", "heartbeat", {})                   # kind

    spawns, firings = tick(b, [action], None, d)
    assert spawns == []
    assert [f["event_id"] for f in firings] == [hit["id"]]

    notices = b.peek(topic="review")
    assert len(notices) == 1
    assert notices[0]["payload"]["message"] == "picked up story:2"

    # the cursor is per action: a second tick sees nothing new, then the new one
    assert tick(b, [action], None, d).firings == []
    signal(b, "story:5", "finished")
    assert len(tick(b, [action], None, d).firings) == 1


def test_where_reads_board_state():
    """The 'summon on a closed PR' lesson: the kind matched, the current state
    made it meaningless. no_event_for_key is the state join."""
    d = scratch()
    b = board(d)
    action = validate_action({
        "name": "ask-once",
        "trigger": {"kind": "ask",
                    "where": {"op": "no_event_for_key", "kind": "answer"}},
        "emitter": {"type": "hitl", "summary": "needs a human: {payload[question]}"},
    })

    b.write("cadre", "stories", "story:1", "ask",
            {"question": "which shape?", "session_id": "s1"})
    b.write("cadre", "stories", "story:2", "answer", {"ticket": "t", "text": "that one"})
    b.write("cadre", "stories", "story:2", "ask",
            {"question": "already answered", "session_id": "s2"})

    _, firings = tick(b, [action], None, d)
    assert len(firings) == 1

    lines = [json.loads(l) for l in (d / "hitl-outbox.jsonl").read_text().splitlines()]
    assert [l["key"] for l in lines] == ["story:1"]
    assert lines[0]["summary"] == "needs a human: which shape?"


# --------------------------------------------------------------------------- bodies


def test_spawn_node_returns_a_spec():
    d = scratch()
    b = board(d)
    n = nodes(d)
    action = validate_action({
        "name": "finished-to-review",
        "trigger": {"kind": "signal",
                    "where": {"op": "payload_eq", "field": "status", "value": "finished"}},
        "body": {"type": "spawn_node", "node": "reviewer", "cwd": "/tmp"},
        "emitter": {"type": "write_event", "kind": "notify",
                    "payload": {"message": "spawned reviewer"}},
    })

    ev = signal(b, "story:42", "finished")
    spawns, firings = tick(b, [action], n, d)

    assert len(spawns) == 1
    spec = spawns[0]
    active = n.active("reviewer")
    assert spec["node"] == "reviewer"
    assert spec["version"] == active["version"]      # every spawn records the version
    assert spec["model"] == "opus"
    assert spec["argv"] == ["claude", "-p", f"@{active['prompt_path']}",
                            "--model", "opus", "--key", "story:42"]
    assert spec["cwd"] == "/tmp" and spec["event_id"] == ev["id"]
    assert spec["correlation_id"] == ev["id"]        # this event starts the thread
    assert spec["firing_id"] == firings[0]["id"]
    assert firings[0]["outcome"] == "fired"


def test_run_command_body_and_emitter():
    d = scratch()
    b = board(d)
    action = validate_action({
        "name": "echo-edge",
        "trigger": {"kind": "signal"},
        "body": {"type": "run_command", "argv": ["/bin/echo", "pr for {event[key]}"]},
        "emitter": [{"type": "run_command", "argv": ["/bin/echo", "side effect"]},
                    {"type": "write_event", "topic": "results", "kind": "report",
                     "payload": {"pointer": "{body[stdout]}"}}],
    })

    signal(b, "story:42")
    tick(b, [action], None, d)

    reports = b.peek(topic="results")
    assert len(reports) == 1
    # the body's stdout reached the emitter, and the space survived as one token
    assert reports[0]["payload"]["pointer"] == "pr for story:42"


def test_in_tray_emitter_is_claimed_once():
    d = scratch()
    b = board(d)
    action = validate_action({
        "name": "assign-triage",
        "trigger": {"kind": "signal",
                    "where": {"op": "payload_eq", "field": "status", "value": "failed"}},
        "emitter": {"type": "in_tray", "agent": "triage-bot",
                    "payload": {"target": "skill:triage", "key": "{event[key]}"}},
    })

    ev = signal(b, "story:42", "failed", correlation_id="thread-1")
    tick(b, [action], None, d)

    got = b.in_tray("triage-bot")
    assert got and got["kind"] == "command"
    assert got["payload"] == {"target": "skill:triage", "key": "story:42"}
    assert got["correlation_id"] == "thread-1"       # the thread is carried
    assert got["topic"] == "in-tray"
    b.complete(got["claim_id"])
    assert b.in_tray("triage-bot") is None           # consumed once
    assert ev["id"] != got["id"]


# --------------------------------------------------------------------------- safety


def test_depth_cap_refuses_and_logs():
    d = scratch()
    b = board(d)
    # An action that triggers on its own output: exactly the cycle the cap exists for.
    action = validate_action({
        "name": "loop",
        "trigger": {"topic": "loop", "kind": "heartbeat"},
        "emitter": {"type": "write_event", "topic": "loop", "kind": "heartbeat"},
        "max_depth": 3,
    })

    b.write("cadre", "loop", "k", "heartbeat", {})
    outcomes = []
    for _ in range(6):
        outcomes += [f["outcome"] for f in tick(b, [action], None, d).firings]

    assert outcomes == ["fired", "fired", "fired", "capped"]
    rows = b.firings(action="loop")
    assert [r["depth"] for r in rows] == [1, 2, 3, 4]
    assert rows[-1]["outcome"] == "capped"
    assert "max_depth 3" in rows[-1]["detail"]
    assert rows[-1]["emitted_event_id"] is None      # the chain stopped here

    assert DEFAULT_MAX_DEPTH == 16                   # the default, when unset


def test_failure_writes_a_failed_signal():
    d = scratch()
    b = board(d)
    action = validate_action({
        "name": "broken-edge",
        "trigger": {"kind": "signal", "topic": "stories"},
        "body": {"type": "run_command", "argv": ["/bin/false"]},
        "emitter": {"type": "write_event", "kind": "notify",
                    "payload": {"message": "never reached"}},
    })

    ev = signal(b, "story:42", correlation_id="thread-1")
    spawns, firings = tick(b, [action], None, d)

    assert spawns == [] and firings[0]["outcome"] == "failed"
    # failure is a signal; triage is just another action (round 4)
    failed = [e for e in b.peek(kind="signal") if e["payload"]["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["payload"]["action"] == "broken-edge"
    assert failed[0]["payload"]["trigger_event"] == ev["id"]
    assert failed[0]["correlation_id"] == "thread-1"
    assert failed[0]["key"] == "story:42"
    # the emitter never ran, and the failure event is what this firing emitted
    assert b.peek(kind="notify") == []
    assert b.firings(action="broken-edge")[0]["emitted_event_id"] == failed[0]["id"]

    # a missing node registry fails the same way, as a signal rather than a crash
    b2 = board(scratch())
    spawn_action = validate_action({
        "name": "no-registry", "trigger": {"kind": "signal"},
        "body": {"type": "spawn_node", "node": "ghost"},
        "emitter": {"type": "write_event", "kind": "notify", "payload": {"message": "x"}}})
    signal(b2, "story:1")
    assert tick(b2, [spawn_action], nodes(scratch()), d).firings[0]["outcome"] == "failed"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("engine tests: all passed")
