"""Driver messages: an agent asks, keeps working, and picks up the answer later.

The pipeline's normal channel is a PR — durable, reviewable, and slow. Some
questions are taste, not correctness ("which of these two shapes do you
actually want"), and waiting for PR review to answer them means the agent
either guesses or stops. This is the fast channel for exactly those: the agent
records a question and carries on, the driver answers whenever, and the answer
reaches the run if it is still going.

Two rules keep the fast channel honest:

- **Asking never blocks.** `ask` returns immediately. Waiting is a separate,
  explicit act (`wait`), taken only when the agent has run out of work the
  answer does not gate.
- **The PR is still the record.** A message is transport. Whatever the driver
  decides has to end up on the PR like any other decision, or the project's
  history silently moves into a chat log.

Files, not a database: one JSON per message under <data_dir>/messages/, so a
daemon restart, a crashed run, and a human with a text editor all see the same
state.
"""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path


def _dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "messages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, payload: dict) -> None:
    """Atomic — a run reading mid-write would otherwise see half a message."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".msg-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def ask(data_dir: Path, story: str, question: str, recommendation: str = "",
        stage: str = "", slice_name: str = "", pr=None) -> str:
    """Record a question. Returns the ticket id; never waits."""
    ticket = f"{story}-{uuid.uuid4().hex[:8]}"
    _write(_dir(data_dir) / f"{ticket}.json", {
        "ticket": ticket, "story": story, "stage": stage, "slice": slice_name,
        "pr": pr, "question": question, "recommendation": recommendation,
        "asked_at": time.time(), "answer": None, "answered_at": None,
    })
    return ticket


def get(data_dir: Path, ticket: str) -> dict | None:
    p = _dir(data_dir) / f"{ticket}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def answer(data_dir: Path, ticket: str, text: str) -> dict | None:
    """Attach the driver's answer. Answering twice overwrites — the last word
    from the human is the one that counts."""
    m = get(data_dir, ticket)
    if m is None:
        return None
    m["answer"] = text
    m["answered_at"] = time.time()
    _write(_dir(data_dir) / f"{ticket}.json", m)
    return m


def wait(data_dir: Path, ticket: str, timeout: float, poll: float = 5.0) -> dict | None:
    """Block until answered or the window closes. Deliberately a plain poll of
    the filesystem: the alternative is holding a socket open for a human, and
    the whole design says an agent should only be here when it has nothing else
    to do."""
    deadline = time.time() + max(0.0, timeout)
    while True:
        m = get(data_dir, ticket)
        if m is None or m.get("answer") is not None:
            return m
        if time.time() >= deadline:
            return m
        time.sleep(min(poll, max(0.1, deadline - time.time())))


def pending(data_dir: Path, story: str = "") -> list[dict]:
    """Unanswered questions, oldest first — what the driver owes the pipeline."""
    out = []
    for p in sorted(_dir(data_dir).glob("*.json")):
        try:
            m = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("answer") is None and (not story or m.get("story") == story):
            out.append(m)
    return sorted(out, key=lambda m: m.get("asked_at", 0))


def recent(data_dir: Path, limit: int = 20) -> list[dict]:
    """Newest first, answered or not — the message log for the status page."""
    out = []
    for p in _dir(data_dir).glob("*.json"):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda m: m.get("asked_at", 0), reverse=True)[:limit]
