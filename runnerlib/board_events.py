"""v0 blackboard stub: append-only JSONL with read-since-N, per the
review-surface decision (2026-08-23) — enough to learn the event shape;
the real board (Picatrix's engine) replaces it. Events, not state: the
review inbox, the notification feed, and stage feedback all read this."""

import json
import os
import time
from pathlib import Path

from . import config as config_mod

BOARD_ENV = "CADRE_BOARD_JSONL"


def board_path() -> Path:
    """Where the stub board lives, resolved per call rather than at import.

    Precedence is the one the rest of the runner uses: an explicit path, else
    the data dir the daemon exported for its subprocesses, else the config's
    data dir. A hardcoded home path here meant a runner moved to another
    data_dir wrote its events where nobody was reading them.
    """
    explicit = os.environ.get(BOARD_ENV)
    if explicit:
        return Path(os.path.expanduser(explicit))
    data_dir = os.environ.get("CADRE_DATA_DIR")
    if not data_dir:
        try:
            data_dir = str(config_mod.load(os.environ.get("CADRE_CONFIG")).data_dir)
        except SystemExit:
            data_dir = config_mod.DEFAULTS["runner"]["data_dir"]
    return Path(os.path.expanduser(data_dir)) / "board.jsonl"


def emit(kind: str, **fields) -> dict:
    """Append one event. Never raises — the board must not take the daemon down."""
    ev = {"ts": time.time(), "kind": kind, **fields}
    try:
        board = board_path()
        board.parent.mkdir(parents=True, exist_ok=True)
        with open(board, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception as e:
        ev["_emit_error"] = str(e)[:200]
    return ev


def read_since(n: int) -> tuple[list[dict], int]:
    """(events after line n, new cursor). Line-count cursor — crude and honest
    for a stub; scoped cursors are a requirement on the real engine."""
    try:
        lines = board_path().read_text().splitlines()
    except OSError:
        return [], n
    out = []
    for line in lines[n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out, len(lines)
