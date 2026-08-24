"""v0 blackboard stub: append-only JSONL with read-since-N, per the
review-surface decision (2026-08-23) — enough to learn the event shape;
the real board (Picatrix's engine) replaces it. Events, not state: the
review inbox, the notification feed, and stage feedback all read this."""

import json
import os
import time
from pathlib import Path

BOARD = Path(os.environ.get("CADRE_BOARD_JSONL",
                            str(Path.home() / ".local/state/pipeline-runner/board.jsonl")))


def emit(kind: str, **fields) -> dict:
    """Append one event. Never raises — the board must not take the daemon down."""
    ev = {"ts": time.time(), "kind": kind, **fields}
    try:
        BOARD.parent.mkdir(parents=True, exist_ok=True)
        with open(BOARD, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception as e:
        ev["_emit_error"] = str(e)[:200]
    return ev


def read_since(n: int) -> tuple[list[dict], int]:
    """(events after line n, new cursor). Line-count cursor — crude and honest
    for a stub; scoped cursors are a requirement on the real engine."""
    try:
        lines = BOARD.read_text().splitlines()
    except OSError:
        return [], n
    out = []
    for line in lines[n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out, len(lines)
