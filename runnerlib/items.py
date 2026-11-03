"""The home page's item store: tags, done and hidden, per item.

An item is one line of work — the root task of a session tree, or a lone
page (see statusd.fleet_items). What the driver says about an item that no
agent writes lives here, in `<data_dir>/items.json`:

    {item_key: {"tags": [str, ...], "done_at": epoch | None, "hidden_at": epoch | None}}

Done and hidden are separate on purpose: done says the work is finished,
hidden only says "not on my home page". Both stay reachable through their
filters and through search, so nothing becomes unfindable.

Pure functions plus load/apply, so the page server and the tests share one
reading of the file. Every read degrades to empty, never to an error page.
"""

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

FILE = "items.json"
TAG = re.compile(r"^[\w .-]{1,32}$")
ACTIONS = ("done", "undone", "hide", "unhide", "tag", "untag")
_LOCK = threading.Lock()


def load(data_dir) -> dict:
    """{item key: {tags, done_at, hidden_at}}, cleaned: a malformed entry is
    dropped, never fatal."""
    try:
        raw = json.loads((Path(data_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, rec in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(rec, dict):
            continue
        tags = [t for t in rec.get("tags") or () if isinstance(t, str) and TAG.match(t)]
        out[str(key)] = {"tags": tags,
                         "done_at": _epoch(rec.get("done_at")),
                         "hidden_at": _epoch(rec.get("hidden_at"))}
    return out


def _epoch(v):
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def apply(data_dir, key: str, action: str, tag: str = "", now: float | None = None) -> str:
    """Change one item and say what happened, in words for the driver.
    Raises ValueError for an action or tag it refuses."""
    if not key:
        raise ValueError("no item named")
    if action not in ACTIONS:
        raise ValueError(f"unknown item action {action!r}")
    tag = tag.strip()
    if action in ("tag", "untag") and not TAG.match(tag):
        raise ValueError("a tag is letters, digits, space, . _ - (max 32)")
    now = time.time() if now is None else now
    with _LOCK:
        store = load(data_dir)
        rec = store.setdefault(key, {"tags": [], "done_at": None, "hidden_at": None})
        if action == "done":
            rec["done_at"], msg = now, "Marked done. Find it under the Done filter."
        elif action == "undone":
            rec["done_at"], msg = None, "Back in the work list."
        elif action == "hide":
            rec["hidden_at"], msg = now, "Hidden from home. Search and the Hidden filter still find it."
        elif action == "unhide":
            rec["hidden_at"], msg = None, "Back on the home page."
        elif action == "tag":
            if tag not in rec["tags"]:
                rec["tags"].append(tag)
            msg = f"Tagged {tag}."
        else:
            rec["tags"] = [t for t in rec["tags"] if t != tag]
            msg = f"Removed tag {tag}."
        if not rec["tags"] and not rec["done_at"] and not rec["hidden_at"]:
            store.pop(key)
        _write(Path(data_dir) / FILE, store)
    return msg


def _write(path: Path, store: dict):
    """Atomic: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".items-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ------------------------------------------------------------------ states

WORKING, YOUR_TURN, IDLE, DONE, HIDDEN = "working", "your-turn", "idle", "done", "hidden"
LABELS = {WORKING: "Agent working", YOUR_TURN: "Your turn", IDLE: "Idle",
          DONE: "Done", HIDDEN: "Hidden"}
HOME_LIMIT = 15
SHOWS = {"": "on my home", "unopened": "unopened", "done": "done",
         "hidden": "hidden", "all": "everything"}


def assign_states(items: list[dict], store: dict, limit: int = HOME_LIMIT) -> list[dict]:
    """Set each item's `state`, `tags` and `unopened`, in place, and return
    the items. Hidden and done come from the store. Of the rest, the `limit`
    most recently touched stay on the home page as working or your turn; only
    what falls past that number is idle — never age alone. An item an agent
    is working on is never idle: it is the thing the driver most wants to see.

    `unopened` is True when any of the item's pages reports unseen: True from
    the page server; None (opened before the page server kept the time) counts
    as seen."""
    live = []
    for it in items:
        rec = store.get(it["key"]) or {}
        it["tags"] = list(rec.get("tags") or [])
        it["unopened"] = any(u is True for u in it.get("unseen") or ())
        if rec.get("hidden_at"):
            it["state"] = HIDDEN
        elif rec.get("done_at"):
            it["state"] = DONE
        else:
            it["state"] = WORKING if it.get("working") else YOUR_TURN
            live.append(it)
    quiet = sorted((it for it in live if not it.get("working")),
                   key=lambda it: -(it.get("touched") or 0))
    room = max(0, limit - (len(live) - len(quiet)))
    for it in quiet[room:]:
        it["state"] = IDLE
    return items


def visible(it: dict, show: str = "", tag: str = "") -> bool:
    """Whether a filter shows this item. The default ("") is the home page:
    working, your turn and idle. Search reaches everything regardless."""
    if tag and tag not in (it.get("tags") or ()):
        return False
    state = it.get("state")
    if show == "all":
        return True
    if show == "done":
        return state == DONE
    if show == "hidden":
        return state == HIDDEN
    if show == "unopened":
        return bool(it.get("unopened"))
    return state in (WORKING, YOUR_TURN, IDLE)
