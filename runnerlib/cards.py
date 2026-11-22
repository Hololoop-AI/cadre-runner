"""Cards: what the driver keeps from learning anything, on a spaced schedule.

The deck is Learn's (the study cadre): cards come from study sessions,
threads and debriefs on any material (a codebase, a recipe, a medical
question), and come back as review. `source` names the topic, not a repo.

A stand-in for Arboreus's cards, living inside Cadre so learning happens where
the work is read rather than in a second app (driver's call, 2026-10-22, on the
page "Keeping up with AI-written code": "make a vibed version in cadre
prototype now"). Arboreus is meant to fold into Qadre later, so the shape
here mirrors its schema (migrations 0001 cards, 0004 reviews) closely enough to
copy across; the scheduler is the same FSRS family (py-fsrs here, rs-fsrs
there).

A card:

    id         c-<8 hex>
    source     what it is about: a topic or work ("sourdough", "cadre-runner")
    anchor     where it came from: a session or task id, a page, a file, a
               source; "" if none. A study session anchors its cards to
               itself, which is how Learn counts the cards kept from it
    front      the prompt, seen cold months from now. Cards are front-only —
               the grader is the answer side, as in Arboreus
    kind       fact | concept | procedure | code — what sort of knowing it
               asks for, so the tutor can pick how to review it (recall a
               fact, explain a concept, apply a procedure, read code)
    reference  what a good answer holds, for the tutor to grade against and
               teach from (driver's call, 2026-11-08: "there should be a
               reference answer that the tutor has access to"): {"answer":
               the ideal answer, "points": what a good answer must hit,
               "mistakes": [{"wrong": a likely wrong idea, "fix": its
               correction}]}. Stored as JSON; read back as a dict
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
KINDS = ("fact", "concept", "procedure", "code")
SCOPES = ("personal", "team")
# Where a team backend's page server serves its deck (statusd.py).
API_PATH = "/api/cards"
# One declaration for the column, used by the schema and by the migration that
# adds it to an older deck, so a new kind is one edit to KINDS.
_KIND_COL = (f"TEXT NOT NULL DEFAULT 'fact' "
             f"CHECK (kind IN ({', '.join(repr(k) for k in KINDS)}))")

SCHEMA = f"""
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
    fsrs_last_review TEXT,
    kind             {_KIND_COL},
    reference        TEXT NOT NULL DEFAULT ''
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
    _migrate(db)
    return db


# Columns added after the first decks were made, as `ALTER TABLE` adds them
# to a deck that predates them. Cards made before them read as kind 'fact'
# with no reference.
_ADDED = (
    ("kind", _KIND_COL),
    ("reference", "TEXT NOT NULL DEFAULT ''"),
)


def _migrate(db):
    have = {r["name"] for r in db.execute("PRAGMA table_info(cards)")}
    for name, decl in _ADDED:
        if name not in have:
            try:
                db.execute(f"ALTER TABLE cards ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError as e:
                # Another process opened the same old deck at the same moment
                # and added the column first.
                if "duplicate column" not in str(e):
                    raise


@contextmanager
def _db(data_dir):
    """One transaction, then close: `with sqlite3.connect()` alone commits but
    leaves the file open."""
    with closing(connect(data_dir)) as db, db:
        yield db


def _row(r) -> dict:
    if r is None:
        return None
    d = dict(r)
    if "reference" in d:
        d["reference"] = json.loads(d["reference"]) if d["reference"] else None
    return d


def _text(v) -> str:
    """A field's text, stripped; '' for anything that is not a string."""
    return v.strip() if isinstance(v, str) else ""


def _front(front) -> str:
    if not _text(front):
        raise CardError("every card needs a non-empty front")
    return _text(front)


def _kind(kind) -> str:
    if kind not in KINDS:
        raise CardError(f"every card needs a kind: one of {', '.join(KINDS)}")
    return kind


def _reference(ref) -> str:
    """A proposal's reference, checked and stored as JSON: an ideal answer is
    required; points and mistakes are optional lists."""
    if not isinstance(ref, dict) or not _text(ref.get("answer")):
        raise CardError('every card needs a reference: {"answer": the ideal answer, '
                        '"points": [...], "mistakes": [{"wrong": ..., "fix": ...}]}')
    points = ref.get("points") or []
    mistakes = ref.get("mistakes") or []
    if not (isinstance(points, list) and all(_text(p) for p in points)):
        raise CardError("reference points are a list of non-empty strings")
    if not (isinstance(mistakes, list) and all(
            isinstance(m, dict) and _text(m.get("wrong")) and _text(m.get("fix"))
            for m in mistakes)):
        raise CardError('reference mistakes are a list of {"wrong": ..., "fix": ...}')
    return json.dumps({"answer": _text(ref["answer"]),
                       "points": [_text(p) for p in points],
                       "mistakes": [{"wrong": _text(m["wrong"]), "fix": _text(m["fix"])}
                                    for m in mistakes]})


def get(data_dir, card_id) -> dict:
    with _db(data_dir) as db:
        r = db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if r is None:
        raise CardError(f"no card {card_id!r}")
    return _row(r)


def propose(data_dir, cards, source, anchor="", now=None, scope="personal") -> list:
    """Land cards as `proposed` for the driver's curation pass. Each card is
    {"front", "kind", "reference"} (see the module docstring); any other key
    (a `why` for the curation pass) is read by the caller, not stored.
    `source` is the topic by name ("cadre-runner", "sourdough"), never a
    folder path: the Cards page groups and finds cards by it."""
    if scope not in SCOPES:
        raise CardError(f"scope is one of {', '.join(SCOPES)}")
    source = (source or "").strip()
    if not source:
        raise CardError("a card needs a source: the repo or topic it is about")
    if source.startswith(("/", "~", "./")):
        raise CardError(f"a card's source is a topic name, not a path: {source!r}")
    if not cards:
        raise CardError("a proposal needs at least one card, each with a front")
    fronts = [_front(c.get("front")) for c in cards]
    kinds = [_kind(c.get("kind")) for c in cards]
    refs = [_reference(c.get("reference")) for c in cards]
    at = _now(now).isoformat()
    ids = [f"c-{uuid.uuid4().hex[:8]}" for _ in fronts]
    with _db(data_dir) as db:
        db.executemany(
            "INSERT INTO cards (id, source, anchor, front, kind, reference, status, scope, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
            [(i, source, anchor or "", f, k, r, scope, at)
             for i, f, k, r in zip(ids, fronts, kinds, refs)])
    return [get(data_dir, i) for i in ids]


def revise(data_dir, card_id, front=None, kind=None, reference=None) -> dict:
    """Reframe a card rather than drop it: new wording, kind or reference, same
    id, status and schedule (driver's call, 2026-11-09: "a poorly written card
    might still have useful content that just needs to be reframed"). Only the
    fields given change; each is checked as `propose` checks it. A team card
    is the team's to reword, so a copy synced from one is refused."""
    card = get(data_dir, card_id)
    if card["origin"]:
        raise CardError(f"{card_id} is a team card; reword it on the team backend")
    changes = {}
    if front is not None:
        changes["front"] = _front(front)
    if kind is not None:
        changes["kind"] = _kind(kind)
    if reference is not None:
        changes["reference"] = _reference(reference)
    if not changes:
        raise CardError("nothing to revise: give a front, kind or reference")
    with _db(data_dir) as db:
        db.execute(f"UPDATE cards SET {', '.join(f'{k} = ?' for k in changes)} "
                   "WHERE id = ?", (*changes.values(), card_id))
    return get(data_dir, card_id)


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
            "SELECT id, source, anchor, front, kind, reference FROM cards "
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
            # A team backend older than kinds and references sends neither.
            kind = c.get("kind") if c.get("kind") in KINDS else "fact"
            ref = c.get("reference")
            ref = json.dumps(ref) if isinstance(ref, dict) else ""
            want = (front, c.get("source") or "team", c.get("anchor") or "", kind, ref)
            have = db.execute("SELECT * FROM cards WHERE origin = ? AND origin_id = ?",
                              (origin, oid)).fetchone()
            if have is None:
                cid = f"c-{uuid.uuid4().hex[:8]}"
                db.execute(
                    "INSERT INTO cards (id, source, anchor, front, kind, reference, status, "
                    "scope, origin, origin_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'accepted', 'team', ?, ?, ?)",
                    (cid, want[1], want[2], front, kind, ref, origin, oid,
                     _now(now).isoformat()))
                _store(db, cid, Card(due=_now(now)))
                out["added"] += 1
            elif (have["front"], have["source"], have["anchor"], have["kind"],
                  have["reference"]) != want:
                db.execute("UPDATE cards SET front = ?, source = ?, anchor = ?, kind = ?, "
                           "reference = ? WHERE id = ?", (*want, have["id"]))
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
        raise CardError('proposal is {"cards": [{"front": ..., "kind": ..., '
                        '"reference": {"answer": ...}}, ...]}')
    return cards


def parse_revision(text) -> dict:
    """`{"front": ..., "kind": ..., "reference": {...}}`, any subset — what an
    agent hands `cards revise`. Returns only those three keys."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise CardError(f"revision is not JSON: {e}") from None
    if not isinstance(data, dict):
        raise CardError('revision is {"front": ..., "kind": ..., "reference": {...}}')
    return {k: data[k] for k in ("front", "kind", "reference") if k in data}
