"""The kanban board: cards in columns, for planning and tracking work visually.

One board, in `<data_dir>/kanban.json`:

    {"board": "dev", "columns": ["backlog", "ready", "doing", "done"],
     "cards": [{"id", "title", "note", "column", "item", "by", "created_at", "moved_at"}]}

A card's place in its column is its order in `cards`. `item` is the fleet
item key it was pulled from ("" for none); `by` is who added it: the task id
of the agent that did, else "driver". `board` names whose board this is, so
it can move into that cadre's own data later without being split apart.

The driver writes it from the fleet's Board screen (statusd POST /kanban) and
agents through `pipeline.py kanban`: two processes, so every write holds an
flock on `kanban.lock` across read, change and atomic replace. Every read
degrades to an empty board, never to an error. Nothing here knows about the
event board (runnerlib/blackboard.py): the two are unrelated.
"""

import fcntl
import json
import math
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

FILE = "kanban.json"
LOCK = "kanban.lock"
BOARD = "dev"
COLUMNS = ("backlog", "ready", "doing", "done")
ACTIONS = ("add", "move", "edit", "remove")
DRIVER = "driver"
TITLE_MAX = 200
REF_MAX = 200  # an item key or a task id
NOTE_MAX = 4000
ID = re.compile(r"^[0-9a-f]{6}$")


def label(column: str) -> str:
    """The column as the driver reads it: "doing" is "Doing"."""
    return column.capitalize()


def empty() -> dict:
    return {"board": BOARD, "columns": list(COLUMNS), "cards": []}


def load(data_dir) -> dict:
    """The board, cleaned: a card without an id, a title or a known column,
    or with an id already seen, is dropped; a missing or malformed file is an
    empty board."""
    try:
        raw = json.loads((Path(data_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    cards = raw.get("cards") if isinstance(raw, dict) else None
    board, seen = empty(), set()
    if isinstance(raw, dict) and isinstance(raw.get("board"), str) and raw["board"].strip():
        board["board"] = raw["board"].strip()
    for c in cards if isinstance(cards, list) else ():
        card = _clean(c)
        if card and card["id"] not in seen:
            seen.add(card["id"])
            board["cards"].append(card)
    return board


def _clean(c) -> dict | None:
    if not isinstance(c, dict):
        return None
    cid, title, column = c.get("id"), _text(c.get("title")), c.get("column")
    if not (isinstance(cid, str) and ID.match(cid) and title and column in COLUMNS):
        return None
    return {"id": cid, "title": title, "note": str(c.get("note") or ""), "column": column,
            "item": _text(c.get("item"))[:REF_MAX], "by": _text(c.get("by"))[:REF_MAX] or DRIVER,
            "created_at": _epoch(c.get("created_at")), "moved_at": _epoch(c.get("moved_at"))}


def _text(v) -> str:
    """A title on one line, its runs of whitespace one space."""
    return " ".join(v.split()) if isinstance(v, str) else ""


def _ref(v, what: str) -> str:
    """An item key or a task id: one word, refused when longer than any real one."""
    v = _text(v)
    if len(v) > REF_MAX or " " in v:
        raise ValueError(f"{what} is not an item key or task id")
    return v


def _epoch(v) -> float:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
    return float(v) if ok else 0.0


def apply(data_dir, action: str, *, card: str = "", title: str | None = None,
          note: str | None = None, column: str = "", item: str = "", before: str = "",
          by: str = DRIVER, now: float | None = None) -> tuple[str, dict]:
    """Change the board and say what happened, in words for the driver, with
    the card it happened to.

    add (title, column = backlog, note, item, by) · move (card, column,
    before: a card id in that column, or "" for its end) · edit (card, title
    and/or note; None leaves it) · remove (card).

    Raises ValueError for an unknown action, card or column, a `before` not
    in the column, or an empty title; the file is then left as it was."""
    if action not in ACTIONS:
        raise ValueError(f"unknown board action {action!r}")
    column = column or (COLUMNS[0] if action == "add" else "")
    if action in ("add", "move") and column not in COLUMNS:
        raise ValueError(f"no column {column!r}; the columns are {', '.join(COLUMNS)}")
    now = time.time() if now is None else now
    with _locked(data_dir):
        _keep_unreadable(Path(data_dir) / FILE)
        board = load(data_dir)
        cards = board["cards"]
        if action == "add":
            new = {"id": _new_id(cards), "title": _title(_cut(title)), "note": _note(note),
                   "column": column, "item": _ref(item, "item"), "by": _ref(by, "by") or DRIVER,
                   "created_at": now, "moved_at": now}
            cards.append(new)
            msg = f"Added “{new['title']}” to {label(column)}."
            hit = new
        else:
            hit = _find(cards, card)
            if action == "move":
                msg = _move(cards, hit, column, before, now)
            elif action == "edit":
                if title is not None:
                    hit["title"] = _title(title)
                if note is not None:
                    hit["note"] = _note(note)
                msg = f"Saved “{hit['title']}”."
            else:
                cards.remove(hit)
                msg = f"Removed “{hit['title']}” from the board."
        _write(Path(data_dir) / FILE, board)
    return msg, hit


def _move(cards: list, hit: dict, column: str, before: str, now: float) -> str:
    if before == hit["id"]:
        if hit["column"] == column:
            return f"“{hit['title']}” stays where it is."
        before = ""                 # dropped on itself in another column: its end
    anchor = _find(cards, before) if before else None
    if anchor and anchor["column"] != column:
        raise ValueError(f"card {before} is not in {label(column)}")
    cards.remove(hit)
    cards.insert(cards.index(anchor) if anchor else len(cards), hit)
    hit["column"], hit["moved_at"] = column, now
    return f"Moved “{hit['title']}” to {label(column)}."


def _find(cards: list, cid: str) -> dict:
    for c in cards:
        if c["id"] == cid:
            return c
    raise ValueError(f"no card {cid!r} on the board" if cid else "no card named")


def _title(v) -> str:
    title = _text(v)
    if not title:
        raise ValueError("a card needs a title")
    if len(title) > TITLE_MAX:
        raise ValueError(f"a title is at most {TITLE_MAX} characters")
    return title


def _cut(v) -> str:
    """A title pulled from elsewhere (a fleet item's) fits: cut at the limit with an ellipsis."""
    title = _text(v)
    return title if len(title) <= TITLE_MAX else title[:TITLE_MAX - 1].rstrip() + "…"


def _keep_unreadable(path: Path):
    """A board file that exists but is not a board (not JSON, or no list of
    cards) is moved to kanban.json.bad before the next write replaces it, so
    a hand edit gone wrong loses nothing."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not (isinstance(raw, dict) and isinstance(raw.get("cards", []), list)):
            raise ValueError("not a board")
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        try:
            path.replace(path.with_name(path.name + ".bad"))
        except OSError:
            pass


def _note(v) -> str:
    note = (v or "").strip()
    if len(note) > NOTE_MAX:
        raise ValueError(f"a note is at most {NOTE_MAX} characters")
    return note


def _new_id(cards: list) -> str:
    taken = {c["id"] for c in cards}
    while (cid := secrets.token_hex(3)) in taken:
        pass
    return cid


@contextmanager
def _locked(data_dir):
    """One writer at a time across processes: the page server and every
    `pipeline.py kanban` hold this for read, change and replace."""
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    with open(path / LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _write(path: Path, board: dict):
    """Atomic: a reader never sees half a file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".kanban-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(board, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def listing(board: dict) -> str:
    """The board as text: each column in order, its cards under it."""
    lines = []
    for col in board["columns"]:
        cards = [c for c in board["cards"] if c["column"] == col]
        lines.append(f"{label(col)} · {len(cards)}")
        for c in cards:
            extra = f"  ({c['item']})" if c["item"] else ""
            lines.append(f"  {c['id']}  {c['title']}{extra}")
            if c["note"]:
                lines.append(f"          {c['note'].splitlines()[0]}")
    return "\n".join(lines)
