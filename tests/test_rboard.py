"""The Rust board's client (runnerlib/rboard.py) against a real
`blackboard serve` child; skipped where the binary is absent."""

import pytest

from runnerlib import rboard

WHO = rboard.who("cadre/test", "box-1")


def ask(c, key="ask:1", **kw):
    return c.emit("learn", "study", key, "ask",
                  {"question": "what is a lease", "expects": {"type": "text"}},
                  who=WHO, **kw)


def test_emit_returns_the_stored_event_with_its_data(rust_board):
    ev = ask(rust_board, data={"asker": "dev", "purpose": "decision"})
    assert ev["seq"] >= 1 and ev["kind"] == "ask"
    assert ev["data"] == {"asker": "dev", "purpose": "decision"}
    assert ev["who"]["source"] == "cadre/test"


def test_read_after_a_cursor_and_by_filter(rust_board):
    a = ask(rust_board, "ask:1")
    b = ask(rust_board, "ask:2")
    rust_board.emit("dev", "study", "ask:1", "answer", {"answer": "ready", "ask_seq": a["seq"]}, who=WHO)
    assert [e["seq"] for e in rust_board.read(after=a["seq"], namespace="learn")] == [b["seq"]]
    answers = rust_board.read(kind="answer", topic="study")
    assert len(answers) == 1 and answers[0]["ask_seq"] == a["seq"]


def test_a_claim_is_taken_once_and_completed(rust_board):
    a = ask(rust_board)
    got = rust_board.claim("learn", namespace="learn", topic="study", kind="ask")
    assert got["seq"] == a["seq"]
    assert rust_board.claim("learn", namespace="learn", topic="study", kind="ask") is None
    rust_board.renew("learn", a["seq"])
    rust_board.complete("learn", a["seq"])
    with pytest.raises(rboard.BoardRefused):
        rust_board.complete("learn", a["seq"])


def test_cursors_round_trip(rust_board):
    assert rust_board.cursor("cadre-runner:answers") == 0
    rust_board.set_cursor("cadre-runner:answers", 7)
    assert rust_board.cursor("cadre-runner:answers") == 7


def test_the_board_refuses_a_bad_event_in_its_own_words(rust_board):
    with pytest.raises(rboard.BoardRefused) as e:
        rust_board.emit("dev", "study", "k", "answer", {"answer": ""}, who=WHO)
    assert "answer" in str(e.value)


def test_no_board_is_a_board_error_not_a_crash(tmp_path):
    c = rboard.Client(tmp_path / "none.sock")
    assert c.up() is False
    with pytest.raises(rboard.BoardError):
        c.read()


def test_the_socket_defaults_to_the_boards_own(monkeypatch):
    monkeypatch.delenv("BLACKBOARD_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    assert str(rboard.default_socket()) == "/run/user/1234/picatrix/blackboard.sock"
    monkeypatch.setenv("BLACKBOARD_SOCKET", "/tmp/x.sock")
    assert str(rboard.default_socket()) == "/tmp/x.sock"
