"""Blackboard semantics: typed writes, observed cursors, claims under lease,
loud parking, in-tray, deferred visibility, correlation threads, firing log.
Run directly: python3 tests/test_blackboard.py"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib.blackboard import Board, BoardError, MAX_ATTEMPTS


def board() -> Board:
    d = tempfile.mkdtemp()
    return Board(Path(d) / "board.db")


def signal(b, key="story:42", status="started", **kw):
    return b.write("cadre", "stories", key, "signal", {"status": status}, **kw)


# --------------------------------------------------------------------------- writes


def test_write_is_type_checked():
    b = board()

    ev = signal(b)
    assert ev["seq"] == 1 and ev["kind"] == "signal"

    # only the eight communication types may be written
    for bad in ("progress", "note", "", "SIGNAL"):
        try:
            b.write("cadre", "stories", "story:42", bad, {"status": "started"})
            assert False, f"kind {bad!r} should be rejected"
        except BoardError:
            pass

    # each kind's declared payload fields are checked at the write, not later
    try:
        b.write("cadre", "stories", "story:42", "ask", {"question": "which shape?"})
        assert False, "ask without session_id should be rejected"
    except BoardError as e:
        assert "session_id" in str(e)

    try:
        signal(b, status="wandering")
        assert False, "signal status is a closed set"
    except BoardError:
        pass

    # header fields are required too
    for ns, topic, key in (("", "t", "k"), ("n", "", "k"), ("n", "t", "  ")):
        try:
            b.write(ns, topic, key, "heartbeat", {})
            assert False, "empty header field should be rejected"
        except BoardError:
            pass

    # heartbeat declares no payload fields
    assert b.write("cadre", "agents", "w1", "heartbeat")["kind"] == "heartbeat"


def test_provenance_comes_from_the_environment():
    b = board()
    os.environ["CADRE_SESSION_ID"] = "sess-7"
    os.environ["CADRE_STAGE"] = "build"
    try:
        ev = signal(b)
    finally:
        del os.environ["CADRE_SESSION_ID"], os.environ["CADRE_STAGE"]
    assert ev["provenance"]["session_id"] == "sess-7"
    assert ev["provenance"]["stage"] == "build"
    assert ev["provenance"]["machine"]

    # and there is no argument that can set it
    after = signal(b)
    assert "session_id" not in after["provenance"]


# --------------------------------------------------------------------------- observed read


def test_observed_cursor_advances_and_consumers_are_independent():
    b = board()
    signal(b, status="started")
    signal(b, status="progress")

    assert [e["payload"]["status"] for e in b.read_since("orchestrator")] == \
        ["started", "progress"]
    assert b.read_since("orchestrator") == []        # cursor advanced

    signal(b, status="finished")
    assert [e["payload"]["status"] for e in b.read_since("orchestrator")] == ["finished"]

    # a second reader has its own cursor and sees the whole history
    assert len(b.read_since("auditor")) == 3

    # a filtered read still advances past the events it skipped
    b.write("cadre", "stories", "story:42", "escalate", {"reason": "stuck"})
    signal(b, status="blocked")
    assert [e["kind"] for e in b.read_since("watcher", kind="escalate")] == ["escalate"]
    assert b.read_since("watcher") == []


def test_visible_after_hides_an_event_until_its_time():
    b = board()
    later = signal(b, status="progress", visible_after=2e9)   # far future
    signal(b, status="finished")

    assert b.read_since("orchestrator") == []      # deferred head blocks the stream
    # peek is a state read, not a stream: the deferred event is invisible, the
    # one behind it is not
    assert [e["payload"]["status"] for e in b.peek(topic="stories")] == ["finished"]
    assert len(b.peek(topic="stories", include_deferred=True)) == 2

    # once its time arrives, it and everything behind it are delivered in order
    got = b.read_since("orchestrator", now=2e9 + 1)
    assert [e["seq"] for e in got] == [later["seq"], later["seq"] + 1]


def test_correlation_id_threads_ask_and_answer():
    b = board()
    b.write("cadre", "asks", "story:42", "ask",
            {"question": "which shape?", "session_id": "sess-7"}, correlation_id="corr-1")
    b.write("cadre", "asks", "story:42", "answer", {"ticket": "tk-1", "text": "yes, sequence behind 165"},
            correlation_id="corr-1")
    signal(b)                                       # unrelated traffic

    thread = b.peek(correlation_id="corr-1")
    assert [e["kind"] for e in thread] == ["ask", "answer"]
    assert b.read_since("worker", correlation_id="corr-1") == thread
    assert thread[0]["payload"]["session_id"] == "sess-7"  # ask carries the session


# --------------------------------------------------------------------------- claimed read


def test_claim_is_consumed_once_and_the_lease_returns_it_to_the_pool():
    b = board()
    ev = b.write("cadre", "work", "w1", "command", {"target": "skill:triage"})

    c = b.claim("w1", lease=60)
    assert c["id"] == ev["id"] and c["claim_id"]
    assert b.claim("w2", lease=60) is None          # first reader took it

    # an unfinished claim returns to the pool at the deadline — no ack needed
    again = b.claim("w2", lease=60, now=c["ts"] + 61)
    assert again["id"] == ev["id"] and again["claim_id"] != c["claim_id"]

    assert b.complete(again["claim_id"]) is True
    assert b.claim("w3", lease=60, now=c["ts"] + 1e6) is None   # done stays done
    assert b.complete(again["claim_id"]) is False               # completing twice is a no-op


def test_three_failures_park_loudly():
    b = board()
    ev = b.write("cadre", "work", "w1", "command", {"target": "skill:triage"})

    for i in range(MAX_ATTEMPTS):
        c = b.claim("w1", lease=0, topic="work")
        assert c is not None, f"attempt {i} should still be offered"
        b.fail(c["claim_id"], reason="boom")

    assert b.claim("w1", lease=0, topic="work") is None  # never offered a fourth time

    parked = b.dead_letters()
    assert len(parked) == 1 and parked[0]["id"] == ev["id"]
    assert parked[0]["attempts"] == MAX_ATTEMPTS

    # loud, not silent: a notify event on the board says so
    notices = b.peek(namespace="blackboard", topic="deadletter")
    assert len(notices) == 1 and notices[0]["kind"] == "notify"
    assert ev["id"] in notices[0]["payload"]["event_id"]
    assert notices[0]["provenance"]["agent"] == "blackboard"

    b.sweep()
    assert len(b.dead_letters()) == 1 and len(b.peek(topic="deadletter")) == 1  # idempotent


def test_in_tray_is_a_claimed_read_on_the_agents_own_key():
    b = board()
    b.write("cadre", "work", "worker-a", "command", {"target": "skill:triage"})
    b.write("cadre", "work", "worker-b", "command", {"target": "skill:other"})

    got = b.in_tray("worker-a")
    assert got["key"] == "worker-a" and got["payload"]["target"] == "skill:triage"
    assert b.in_tray("worker-a") is None            # consumed once
    b.complete(got["claim_id"])
    assert b.in_tray("worker-b")["payload"]["target"] == "skill:other"

    # a deferred command is not in the tray yet
    ev = b.write("cadre", "work", "worker-a", "command", {"target": "later"},
                 visible_after=2e9)
    assert b.in_tray("worker-a") is None
    assert b.in_tray("worker-a", now=2e9 + 1)["id"] == ev["id"]


# --------------------------------------------------------------------------- firing log


def test_firing_log_records_causal_depth():
    b = board()
    trigger = signal(b, status="failed")
    emitted = b.write("cadre", "work", "triage", "command", {"target": "skill:triage"})

    assert b.causal_depth(trigger["id"]) == 0       # a session wrote it
    b.log_firing("failure-to-triage", trigger["id"], depth=1,
                 emitted_event_id=emitted["id"], detail="fan-out to triage")
    assert b.causal_depth(emitted["id"]) == 1       # the cycle cap reads this

    b.log_firing("failure-to-triage", emitted["id"], depth=2, outcome="capped")
    rows = b.firings(action="failure-to-triage")
    assert [r["depth"] for r in rows] == [1, 2]
    assert [r["outcome"] for r in rows] == ["fired", "capped"]
    assert b.firings(event_id=trigger["id"])[0]["emitted_event_id"] == emitted["id"]

    # the firing log lives beside the events, never on the board itself
    assert b.peek(topic="stories") and all(
        e["kind"] != "firing" for e in b.peek(namespace="cadre"))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("blackboard tests: all passed")
