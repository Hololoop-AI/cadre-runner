"""Cards: what the driver keeps from learning their own code.

A stand-in for Arboreus's cards, living inside Cadre so learning happens where
the work is read rather than in a second app (driver's call, 2026-10-22, on the
page "Keeping up with AI-written code": "make a vibed version in cadre
prototype now"). Arboreus is meant to fold into Picatrix later, so the shape
here mirrors its schema (migrations 0001 cards, 0004 reviews) closely enough to
copy across; the scheduler is the same FSRS family (py-fsrs here, rs-fsrs
there).

A card:

    id         c-<8 hex>
    source     what it is about: a repo or topic ("cadre-runner", "work-api")
    anchor     where it came from: a file, PR, commit, page or task id; "" if none
    front      the prompt, seen cold months from now. Cards are front-only —
               the grader is the answer side, as in Arboreus
    status     proposed | accepted | discarded. Only accepted cards schedule
    scope      personal | team. A team backend's deck is all team cards
    origin     for a team card copied into a personal deck: the team backend's
               URL, and `origin_id` its id there; "" for a card made here
    created_at ISO-8601 UTC
    fsrs_*     scheduler state; NULL until accepted

Personal and team decks (driver's call, 2026-10-22): every machine keeps its
own deck in a personal directory, outside every backend's data, so nothing
personal reaches a shared backend. A shared team backend keeps the team's deck
in its own data and serves the accepted cards at `/api/cards`; each teammate's
machine copies them in (`merge_team`) and schedules them for that person
alone. Teammates see team cards, never each other's personal ones.

A review is append-only and keeps both the model's grade and the grade the
driver settled on, so a disagreement stays visible. The schema enforces that,
not the callers.

`<data_dir>/cards.db` is owned by this module alone; the daemon, the page
server, agents (through `pipeline.py cards`) and tests all go through it.
"""

import json
import sqlite3
import urllib.request
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ._vendor.fsrs import Card, Rating, Scheduler, State

FILE = "cards.db"
STATUSES = ("proposed", "accepted", "discarded")
SCOPES = ("personal", "team")
# Where a team backend's page server serves its deck (statusd.py).
API_PATH = "/api/cards"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id               TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    anchor           TEXT NOT NULL DEFAULT '',
    front            TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'discarded')),
    scope            TEXT NOT NULL DEFAULT 'personal' CHECK (scope IN ('personal', 'team')),
    origin           TEXT NOT NULL DEFAULT '',
    origin_id        TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    fsrs_state       INTEGER,
    fsrs_step        INTEGER,
    fsrs_stability   REAL,
    fsrs_difficulty  REAL,
    fsrs_due         TEXT,
    fsrs_last_review TEXT
);
CREATE INDEX IF NOT EXISTS cards_by_due ON cards(status, fsrs_due);
CREATE UNIQUE INDEX IF NOT EXISTS cards_by_origin ON cards(origin, origin_id)
    WHERE origin != '';

CREATE TABLE IF NOT EXISTS reviews (
    id              TEXT PRIMARY KEY,
    card_id         TEXT NOT NULL REFERENCES cards(id),
    answer          TEXT NOT NULL,
    model_grade     INTEGER CHECK (model_grade BETWEEN 1 AND 4),
    model_reasoning TEXT NOT NULL DEFAULT '',
    final_grade     INTEGER NOT NULL CHECK (final_grade BETWEEN 1 AND 4),
    reviewed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_by_card ON reviews(card_id);

CREATE TRIGGER IF NOT EXISTS reviews_are_never_changed BEFORE UPDATE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews is append-only'); END;
CREATE TRIGGER IF NOT EXISTS reviews_are_never_deleted BEFORE DELETE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews is append-only'); END;
"""

# Fuzzing spreads due dates so a batch accepted together does not come due
# together; tests pass their own scheduler to stay deterministic.
_SCHEDULER = Scheduler()


class CardError(ValueError):
    pass


def _now(now=None) -> datetime:
    return now or datetime.now(timezone.utc)


def connect(data_dir) -> sqlite3.Connection:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(Path(data_dir) / FILE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


@contextmanager
def _db(data_dir):
    """One transaction, then close: `with sqlite3.connect()` alone commits but
    leaves the file open."""
    with closing(connect(data_dir)) as db, db:
        yield db


def _row(r) -> dict:
    return dict(r) if r is not None else None


def get(data_dir, card_id) -> dict:
    with _db(data_dir) as db:
        r = db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if r is None:
        raise CardError(f"no card {card_id!r}")
    return _row(r)


def propose(data_dir, cards, source, anchor="", now=None, scope="personal") -> list:
    """Land cards as `proposed` for the driver's curation pass. `cards` is a
    list of {"front": ...}; any other key (a `why` for the curation pass) is
    read by the caller, not stored."""
    if scope not in SCOPES:
        raise CardError(f"scope is one of {', '.join(SCOPES)}")
    source = (source or "").strip()
    if not source:
        raise CardError("a card needs a source: the repo or topic it is about")
    fronts = [(c.get("front") or "").strip() for c in cards]
    if not fronts or not all(fronts):
        raise CardError("every card needs a non-empty front")
    at = _now(now).isoformat()
    ids = [f"c-{uuid.uuid4().hex[:8]}" for _ in fronts]
    with _db(data_dir) as db:
        db.executemany(
            "INSERT INTO cards (id, source, anchor, front, status, scope, created_at) "
            "VALUES (?, ?, ?, ?, 'proposed', ?, ?)",
            [(i, source, anchor or "", f, scope, at) for i, f in zip(ids, fronts)])
    return [get(data_dir, i) for i in ids]


def _store(db, card_id, fc: Card):
    db.execute(
        "UPDATE cards SET fsrs_state = ?, fsrs_step = ?, fsrs_stability = ?, "
        "fsrs_difficulty = ?, fsrs_due = ?, fsrs_last_review = ? WHERE id = ?",
        (int(fc.state), fc.step, fc.stability, fc.difficulty, fc.due.isoformat(),
         fc.last_review.isoformat() if fc.last_review else None, card_id))


def _load(row) -> Card:
    return Card(
        state=State(row["fsrs_state"]), step=row["fsrs_step"],
        stability=row["fsrs_stability"], difficulty=row["fsrs_difficulty"],
        due=datetime.fromisoformat(row["fsrs_due"]),
        last_review=(datetime.fromisoformat(row["fsrs_last_review"])
                     if row["fsrs_last_review"] else None))


def accept(data_dir, card_id, now=None) -> dict:
    """Accept a proposed card: it starts scheduling and is due at once."""
    card = get(data_dir, card_id)
    if card["status"] != "proposed":
        raise CardError(f"{card_id} is {card['status']}; only a proposed card is accepted")
    with _db(data_dir) as db:
        db.execute("UPDATE cards SET status = 'accepted' WHERE id = ?", (card_id,))
        _store(db, card_id, Card(due=_now(now)))
    return get(data_dir, card_id)


def discard(data_dir, card_id) -> dict:
    """Discard a card. Its reviews stay; it never comes due again."""
    get(data_dir, card_id)
    with _db(data_dir) as db:
        db.execute("UPDATE cards SET status = 'discarded' WHERE id = ?", (card_id,))
    return get(data_dir, card_id)


def listing(data_dir, status=None, source=None) -> list:
    q, args = "SELECT * FROM cards WHERE 1=1", []
    if status:
        q, args = q + " AND status = ?", args + [status]
    if source:
        q, args = q + " AND source = ?", args + [source]
    with _db(data_dir) as db:
        return [_row(r) for r in db.execute(q + " ORDER BY created_at, rowid", args)]


def due(data_dir, now=None, source=None, limit=None) -> list:
    """Accepted cards due at `now`, most overdue first."""
    q = "SELECT * FROM cards WHERE status = 'accepted' AND fsrs_due <= ?"
    args = [_now(now).isoformat()]
    if source:
        q, args = q + " AND source = ?", args + [source]
    q += " ORDER BY fsrs_due"
    if limit:
        q, args = q + " LIMIT ?", args + [int(limit)]
    with _db(data_dir) as db:
        return [_row(r) for r in db.execute(q, args)]


def review(data_dir, card_id, answer, grade, model_grade=None, reasoning="",
           now=None, scheduler=None) -> dict:
    """Record one review and reschedule. `grade` is the driver's settled grade,
    1-4 (wrong, partial, correct-ish, precise — Arboreus's rubric)."""
    if int(grade) not in (1, 2, 3, 4):
        raise CardError("grade is 1-4")
    card = get(data_dir, card_id)
    if card["status"] != "accepted":
        raise CardError(f"{card_id} is {card['status']}; only an accepted card is reviewed")
    at = _now(now)
    fc, _ = (scheduler or _SCHEDULER).review_card(_load(card), Rating(int(grade)), at)
    with _db(data_dir) as db:
        _store(db, card_id, fc)
        db.execute(
            "INSERT INTO reviews (id, card_id, answer, model_grade, model_reasoning, "
            "final_grade, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"r-{uuid.uuid4().hex[:8]}", card_id, answer or "",
             int(model_grade) if model_grade else None, reasoning or "", int(grade),
             at.isoformat()))
    return get(data_dir, card_id)


def reviews(data_dir, card_id) -> list:
    with _db(data_dir) as db:
        return [_row(r) for r in db.execute(
            "SELECT * FROM reviews WHERE card_id = ? ORDER BY reviewed_at", (card_id,))]


def export_team(data_dir) -> list:
    """What a team backend serves at /api/cards: its accepted cards, and only
    what a teammate's machine needs to copy one in — no schedule, no reviews."""
    with _db(data_dir) as db:
        return [_row(r) for r in db.execute(
            "SELECT id, source, anchor, front FROM cards "
            "WHERE status = 'accepted' AND scope = 'team' ORDER BY created_at, rowid")]


def fetch_team(url, timeout=5.0) -> list:
    """A team backend's accepted cards, over HTTP from its page server."""
    with urllib.request.urlopen(url.rstrip("/") + API_PATH, timeout=timeout) as r:
        data = json.load(r)
    return data.get("cards") or [] if isinstance(data, dict) else []


def merge_team(data_dir, origin, team_cards, now=None) -> dict:
    """Copy a team backend's accepted cards into this (personal) deck. New ones
    are accepted and due at once — the team already curated them; a reworded
    one takes the new wording and keeps this person's schedule; one the team
    discarded stops coming due here. Idempotent: syncing twice changes
    nothing the second time."""
    origin = (origin or "").rstrip("/")
    if not origin:
        raise CardError("a team card's origin is the team backend's URL")
    seen, out = set(), {"added": 0, "updated": 0, "retired": 0}
    with _db(data_dir) as db:
        for c in team_cards:
            oid, front = str(c.get("id") or ""), (c.get("front") or "").strip()
            if not oid or not front:
                continue
            seen.add(oid)
            have = db.execute("SELECT * FROM cards WHERE origin = ? AND origin_id = ?",
                              (origin, oid)).fetchone()
            if have is None:
                cid = f"c-{uuid.uuid4().hex[:8]}"
                db.execute(
                    "INSERT INTO cards (id, source, anchor, front, status, scope, origin, "
                    "origin_id, created_at) VALUES (?, ?, ?, ?, 'accepted', 'team', ?, ?, ?)",
                    (cid, c.get("source") or "team", c.get("anchor") or "", front,
                     origin, oid, _now(now).isoformat()))
                _store(db, cid, Card(due=_now(now)))
                out["added"] += 1
            elif (have["front"], have["source"], have["anchor"]) != (
                    front, c.get("source") or "team", c.get("anchor") or ""):
                db.execute("UPDATE cards SET front = ?, source = ?, anchor = ? WHERE id = ?",
                           (front, c.get("source") or "team", c.get("anchor") or "",
                            have["id"]))
                out["updated"] += 1
        for r in db.execute("SELECT id, origin_id FROM cards WHERE origin = ? "
                            "AND status = 'accepted'", (origin,)).fetchall():
            if r["origin_id"] not in seen:
                db.execute("UPDATE cards SET status = 'discarded' WHERE id = ?", (r["id"],))
                out["retired"] += 1
    return out


def parse_proposal(text) -> list:
    """`{"cards": [...]}` or a bare list — what an agent hands `cards propose`."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise CardError(f"proposal is not JSON: {e}") from None
    cards = data.get("cards") if isinstance(data, dict) else data
    if not isinstance(cards, list) or not all(isinstance(c, dict) for c in cards):
        raise CardError('proposal is {"cards": [{"front": ...}, ...]}')
    return cards
