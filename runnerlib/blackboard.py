"""The blackboard: one SQLite file holding every event agents write to each
other, plus the firing log the action engine appends to.

This is a Python semblance of the decided v0 semantics — the transaction layer
a generic action engine sits on top of. It is deliberately small: one file, one
connection, stdlib only. The Rust crate is the real implementation; this exists
so the runner can exercise the semantics now and so the shape is settled before
the engine is written against it.

Why each piece is the way it is (cadre-context/blackboard/decisions/):

  2026-08-31-v0-storage-partitioning-provenance
      SQLite as the reference store. Three partition fields — namespace (who
      owns the area) / topic (category) / key (the entity). Session id is
      provenance, not part of the key, and provenance comes from the session
      environment the daemon injects — never from a caller argument.

  2026-08-31-action-model-and-communication-types
      The eight communication types (ask/answer is one pair) are the only
      kinds that may be written. `ask` carries the originating session id so
      the answer can resume that session. `command` is delivered consumed-once.

  2026-09-02-engine-calls-round3
      Writes are type-checked at write time — header fields and each kind's
      declared payload fields — because free-form payloads surface their
      failures hours later inside an action instead of at the write. Claims
      use a lease with a deadline, not an explicit ack (a dead terminal agent
      never acks and the event is stuck forever). Three failures park loudly:
      a visible dead-letter row plus a human-facing notice, never a silent
      drop or an infinite retry. The firing log is a separate table in the
      same store, so the board stays readable and one file is still one replay.

  2026-09-02-engine-calls-round4
      Two ways to read, chosen by the reader: observed (a cursor per reader,
      nothing removed) and claimed (first reader takes it under a lease).
      Plus three header fields: key (targeted vs broadcast), visible_after
      (deferred), correlation_id (threads). The board keeps every reader's
      cursor — orchestrator-owned cursors were rejected after a replay caused
      by an orchestrator deleting its own marker. Retention: keep everything
      in v0, so there is no deletion path here. Cycle safety comes from the
      firing log's causal depth cap, not a dedicated construct.

  2026-09-02-vocabulary-action-not-pipe
      Fixed words used throughout: event · header fields · payload · kind ·
      action (trigger, body, emitter) · firing, firing log · cursor · claim +
      lease · in-tray · observed read / claimed read.
"""

import json
import os
import sqlite3
import threading
import time
import uuid

# The eight communication types, as kind -> required payload fields. Anything
# else is rejected at the write; raw output and progress lines stay in herdr.
KINDS = {
    "signal": ("status",),
    "ask": ("question", "session_id"),
    "answer": ("ticket", "text"),
    "command": ("target",),
    "report": ("pointer",),
    "escalate": ("reason",),
    "notify": ("message",),
    "heartbeat": (),
    "propose": ("proposal",),
}

SIGNAL_STATUSES = ("started", "finished", "blocked", "progress", "failed")

# Three failures on the same event and it is parked loudly, never retried again.
MAX_ATTEMPTS = 3

DEADLETTER_NAMESPACE = "blackboard"
DEADLETTER_TOPIC = "deadletter"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,   -- the monotonic read order
  id             TEXT    NOT NULL UNIQUE,
  ts             REAL    NOT NULL,
  namespace      TEXT    NOT NULL,
  topic          TEXT    NOT NULL,
  key            TEXT    NOT NULL,
  kind           TEXT    NOT NULL,
  provenance     TEXT    NOT NULL,                    -- JSON, from the environment
  payload        TEXT    NOT NULL,                    -- JSON object
  correlation_id TEXT,
  visible_after  REAL
);
CREATE INDEX IF NOT EXISTS events_route ON events (namespace, topic, key, kind);
CREATE INDEX IF NOT EXISTS events_corr  ON events (correlation_id);

-- One row per reader. The board owns cursors; readers never write them.
CREATE TABLE IF NOT EXISTS cursors (
  consumer TEXT PRIMARY KEY,
  seq      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  claim_id   TEXT PRIMARY KEY,
  event_seq  INTEGER NOT NULL,
  consumer   TEXT NOT NULL,
  claimed_at REAL NOT NULL,
  deadline   REAL NOT NULL,
  state      TEXT NOT NULL,                           -- open | done | failed
  reason     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS claims_event ON claims (event_seq);

CREATE TABLE IF NOT EXISTS dead_letters (
  event_seq INTEGER PRIMARY KEY,
  parked_at REAL NOT NULL,
  attempts  INTEGER NOT NULL,
  reason    TEXT NOT NULL
);

-- Separate table, same store: the board stays readable, one file is one replay.
CREATE TABLE IF NOT EXISTS firings (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                REAL NOT NULL,
  action            TEXT NOT NULL,
  event_id          TEXT NOT NULL,                    -- the event that triggered it
  emitted_event_id  TEXT,                             -- what the emitter wrote, if any
  depth             INTEGER NOT NULL,                 -- causal depth, for the cycle cap
  outcome           TEXT NOT NULL,
  detail            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS firings_action  ON firings (action);
CREATE INDEX IF NOT EXISTS firings_emitted ON firings (emitted_event_id);
"""


class BoardError(Exception):
    """A rejected write. Bad writes fail here, not hours later in an action."""


def provenance_from_env(env=None) -> dict:
    """Identity comes from the session environment the daemon injects. There is
    deliberately no way to pass this in: tool arguments never carry identity."""
    env = os.environ if env is None else env
    p = {"machine": env.get("CADRE_MACHINE") or os.uname().nodename}
    for field, var in (("session_id", "CADRE_SESSION_ID"), ("stage", "CADRE_STAGE"),
                       ("agent", "CADRE_AGENT")):
        if env.get(var):
            p[field] = env[var]
    return p


def validate(namespace: str, topic: str, key: str, kind: str, payload: dict) -> None:
    """Header fields and the kind's declared payload fields, checked at write."""
    for name, value in (("namespace", namespace), ("topic", topic), ("key", key)):
        if not isinstance(value, str) or not value.strip():
            raise BoardError(f"{name} is required and must be a non-empty string")
    if kind not in KINDS:
        raise BoardError(f"unknown kind {kind!r} (the eight types: {', '.join(KINDS)})")
    if not isinstance(payload, dict):
        raise BoardError("payload must be a JSON object")
    missing = [f for f in KINDS[kind] if not payload.get(f)]
    if missing:
        raise BoardError(f"kind {kind!r} requires payload fields: {', '.join(missing)}")
    if kind == "signal" and payload["status"] not in SIGNAL_STATUSES:
        raise BoardError(f"signal status must be one of {', '.join(SIGNAL_STATUSES)}")


class Board:
    def __init__(self, path, busy_timeout: float = 5.0):
        self.path = str(path)
        # One connection shared across the daemon's threads, serialized by a
        # lock: SQLite's own busy handling covers other processes, the lock
        # covers our own threads sharing this handle.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False,
                                  timeout=busy_timeout, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(f"PRAGMA busy_timeout={int(busy_timeout * 1000)}")
        with self._lock:
            self.db.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- writing --------------------------------------------------------------

    def write(self, namespace: str, topic: str, key: str, kind: str,
              payload: dict | None = None, correlation_id: str | None = None,
              visible_after: float | None = None, env=None) -> dict:
        """Append one event. Raises BoardError if the write does not type-check."""
        payload = {} if payload is None else payload
        validate(namespace, topic, key, kind, payload)
        return self._append(namespace, topic, key, kind, payload,
                            provenance_from_env(env), correlation_id, visible_after)

    def _append(self, namespace, topic, key, kind, payload, provenance,
                correlation_id, visible_after) -> dict:
        ev = {"id": uuid.uuid4().hex, "ts": time.time(), "namespace": namespace,
              "topic": topic, "key": key, "kind": kind, "provenance": provenance,
              "payload": payload, "correlation_id": correlation_id,
              "visible_after": visible_after}
        with self._lock:
            cur = self.db.execute(
                """INSERT INTO events (id, ts, namespace, topic, key, kind, provenance,
                                       payload, correlation_id, visible_after)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (ev["id"], ev["ts"], namespace, topic, key, kind,
                 json.dumps(provenance), json.dumps(payload),
                 correlation_id, visible_after))
            ev["seq"] = cur.lastrowid
        return ev

    def get(self, event_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None

    # -- reading --------------------------------------------------------------

    def peek(self, *, namespace=None, topic=None, key=None, kind=None,
             correlation_id=None, after_seq: int = 0, limit: int | None = None,
             now: float | None = None, include_deferred: bool = False) -> list[dict]:
        """A read that advances nothing. Trigger conditions use this: they may
        read the board's current state, not just the event that fired them."""
        now = time.time() if now is None else now
        where, args = _filter_sql(namespace, topic, key, kind, correlation_id)
        where.append("seq > ?")
        args.append(after_seq)
        if not include_deferred:
            where.append("(visible_after IS NULL OR visible_after <= ?)")
            args.append(now)
        sql = "SELECT * FROM events e WHERE " + " AND ".join(where) + " ORDER BY seq"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [_row_to_event(r) for r in rows]

    def cursor(self, consumer: str) -> int:
        with self._lock:
            row = self.db.execute("SELECT seq FROM cursors WHERE consumer = ?",
                                  (consumer,)).fetchone()
        return row["seq"] if row else 0

    def consumers(self) -> set[str]:
        with self._lock:
            return {r["consumer"] for r in self.db.execute("SELECT consumer FROM cursors")}

    def start_at_head(self, consumer: str) -> int:
        """Put a consumer that has no cursor yet at the board's head, so its
        first read sees only what is written from now on. A consumer that
        already has a cursor is left where it is. Returns its cursor."""
        with self._lock:
            self.db.execute(
                "INSERT INTO cursors (consumer, seq) "
                "SELECT ?, COALESCE(MAX(seq), 0) FROM events WHERE true "
                "ON CONFLICT(consumer) DO NOTHING", (consumer,))
        return self.cursor(consumer)

    def read_since(self, consumer: str, *, namespace=None, topic=None, key=None,
                   kind=None, correlation_id=None, limit: int | None = None,
                   now: float | None = None) -> list[dict]:
        """Observed read: everything this consumer has not seen yet, and the
        board advances its cursor. Consumers are independent — nothing is
        removed and one reader never hides an event from another.

        The cursor only crosses the contiguous visible prefix: a deferred event
        (visible_after in the future) stops the advance so it is still delivered
        when its time comes. A far-future deferred event therefore holds up the
        events behind it for that consumer — deliberate, since losing the
        deferred event entirely is the worse failure.
        """
        now = time.time() if now is None else now
        start = self.cursor(consumer)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq", (start,)).fetchall()
        out, advance = [], start
        for row in rows:
            if row["visible_after"] is not None and row["visible_after"] > now:
                break  # do not step over an event that is not visible yet
            advance = row["seq"]
            if _matches(row, namespace, topic, key, kind, correlation_id):
                out.append(_row_to_event(row))
                if limit and len(out) >= limit:
                    break
        if advance != start:
            with self._lock:
                self.db.execute(
                    "INSERT INTO cursors (consumer, seq) VALUES (?,?) "
                    "ON CONFLICT(consumer) DO UPDATE SET seq = excluded.seq",
                    (consumer, advance))
        return out

    # -- claimed read ---------------------------------------------------------

    def claim(self, consumer: str, *, lease: float = 60.0, namespace=None, topic=None,
              key=None, kind=None, correlation_id=None,
              now: float | None = None) -> dict | None:
        """Claimed read: the first reader takes the oldest matching event under a
        lease. Returns the event with a `claim_id`, or None if nothing is free.

        No ack is required — an unfinished claim returns to the pool when its
        deadline passes, so a terminal agent that dies does not wedge the event.
        """
        now = time.time() if now is None else now
        self.sweep(now=now)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                where, args = _filter_sql(namespace, topic, key, kind, correlation_id)
                where.append("(e.visible_after IS NULL OR e.visible_after <= ?)")
                args.append(now)
                row = self.db.execute(
                    "SELECT e.* FROM events e WHERE " + " AND ".join(where) + """
                       AND NOT EXISTS (SELECT 1 FROM dead_letters d WHERE d.event_seq = e.seq)
                       AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.event_seq = e.seq
                                        AND (c.state = 'done'
                                             OR (c.state = 'open' AND c.deadline > ?)))
                     ORDER BY e.seq LIMIT 1""",
                    args + [now]).fetchone()
                if row is None:
                    self.db.execute("COMMIT")
                    return None
                claim_id = uuid.uuid4().hex
                self.db.execute(
                    """INSERT INTO claims (claim_id, event_seq, consumer, claimed_at,
                                           deadline, state)
                       VALUES (?,?,?,?,?,'open')""",
                    (claim_id, row["seq"], consumer, now, now + lease))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        ev = _row_to_event(row)
        ev["claim_id"] = claim_id
        return ev

    def in_tray(self, agent: str, *, lease: float = 60.0, namespace=None, topic=None,
                kind=None, now: float | None = None) -> dict | None:
        """Targeted work: a claimed read on the agent's own key. Consumed once —
        the agent's runner wakes the agent with what it finds here."""
        return self.claim(agent, lease=lease, namespace=namespace, topic=topic,
                          key=agent, kind=kind, now=now)

    def complete(self, claim_id: str) -> bool:
        """Finish a claim. The event is done and never offered again."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE claims SET state = 'done' WHERE claim_id = ? AND state = 'open'",
                (claim_id,))
        return cur.rowcount > 0

    def fail(self, claim_id: str, reason: str = "") -> bool:
        """Give up on a claim now rather than waiting out the lease. Counts as
        one of the three attempts, exactly like a lease that ran out."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE claims SET state = 'failed', reason = ? "
                "WHERE claim_id = ? AND state = 'open'", (reason, claim_id))
        return cur.rowcount > 0

    def attempts(self, event_seq: int, now: float | None = None) -> int:
        """Failed attempts so far: explicit failures plus expired leases."""
        now = time.time() if now is None else now
        with self._lock:
            row = self.db.execute(
                """SELECT COUNT(*) n FROM claims WHERE event_seq = ?
                     AND (state = 'failed' OR (state = 'open' AND deadline <= ?))""",
                (event_seq, now)).fetchone()
        return row["n"]

    def sweep(self, now: float | None = None) -> list[dict]:
        """Park every event that has burned its three attempts: a dead-letter row
        plus a notify event, so it is loud rather than a silent drop. Returns the
        events parked by this call."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self.db.execute(
                f"""SELECT e.*, COUNT(c.claim_id) n FROM events e
                      JOIN claims c ON c.event_seq = e.seq
                     WHERE (c.state = 'failed' OR (c.state = 'open' AND c.deadline <= ?))
                       AND NOT EXISTS (SELECT 1 FROM claims d WHERE d.event_seq = e.seq
                                        AND d.state = 'done')
                       AND NOT EXISTS (SELECT 1 FROM dead_letters x WHERE x.event_seq = e.seq)
                     GROUP BY e.seq HAVING n >= {MAX_ATTEMPTS}""",
                (now,)).fetchall()
        parked = []
        for row in rows:
            reason = f"{row['n']} failed attempts (max {MAX_ATTEMPTS})"
            with self._lock:
                self.db.execute(
                    "INSERT OR IGNORE INTO dead_letters (event_seq, parked_at, attempts, reason)"
                    " VALUES (?,?,?,?)", (row["seq"], now, row["n"], reason))
            # The human-facing half of "park loudly": a notice on the board
            # itself, written by the board rather than by any session.
            self._append(DEADLETTER_NAMESPACE, DEADLETTER_TOPIC, row["key"], "notify",
                         {"message": f"parked {row['kind']} event after {reason}",
                          "event_id": row["id"], "attempts": row["n"]},
                         {"machine": os.uname().nodename, "agent": "blackboard"},
                         row["correlation_id"], None)
            parked.append(_row_to_event(row))
        return parked

    def dead_letters(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                """SELECT d.*, e.* FROM dead_letters d JOIN events e ON e.seq = d.event_seq
                   ORDER BY d.parked_at""").fetchall()
        out = []
        for row in rows:
            ev = _row_to_event(row)
            ev.update(parked_at=row["parked_at"], attempts=row["attempts"],
                      reason=row["reason"])
            out.append(ev)
        return out

    # -- firing log -----------------------------------------------------------

    def log_firing(self, action: str, event_id: str, *, depth: int = 0,
                   outcome: str = "fired", emitted_event_id: str | None = None,
                   detail: str = "") -> int:
        """One row per action firing. `depth` is causal depth — the engine caps
        it to stop cycles; loop-backs are ordinary actions, not a construct."""
        with self._lock:
            cur = self.db.execute(
                """INSERT INTO firings (ts, action, event_id, emitted_event_id, depth,
                                        outcome, detail) VALUES (?,?,?,?,?,?,?)""",
                (time.time(), action, event_id, emitted_event_id, depth, outcome, detail))
        return cur.lastrowid

    def firings(self, *, action: str | None = None, event_id: str | None = None,
                key: str | None = None, limit: int = 100) -> list[dict]:
        """Rows from the firing log. `key` filters by the KEY OF THE TRIGGER
        EVENT — "how many times has this action fired for this story" is the
        question a round cap asks, and the firing log is the only place that
        history lives (the engine keeps no counters of its own)."""
        where, args = [], []
        if action:
            where.append("action = ?")
            args.append(action)
        if event_id:
            where.append("event_id = ?")
            args.append(event_id)
        if key:
            where.append("event_id IN (SELECT id FROM events WHERE key = ?)")
            args.append(key)
        sql = "SELECT * FROM firings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY id LIMIT {int(limit)}"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def causal_depth(self, event_id: str) -> int:
        """How deep in a causal chain this event sits: 0 if a session wrote it,
        otherwise the depth of the firing that emitted it. The engine's cycle cap
        compares `causal_depth(trigger) + 1` against its limit before firing."""
        with self._lock:
            row = self.db.execute(
                "SELECT MAX(depth) d FROM firings WHERE emitted_event_id = ?",
                (event_id,)).fetchone()
        return row["d"] or 0


# --------------------------------------------------------------------------- helpers


def _filter_sql(namespace, topic, key, kind, correlation_id, prefix="e."):
    where, args = ["1=1"], []
    for column, value in (("namespace", namespace), ("topic", topic), ("key", key),
                          ("kind", kind), ("correlation_id", correlation_id)):
        if value is not None:
            where.append(f"{prefix}{column} = ?")
            args.append(value)
    return where, args


def _matches(row, namespace, topic, key, kind, correlation_id) -> bool:
    return all(v is None or row[c] == v for c, v in
               (("namespace", namespace), ("topic", topic), ("key", key),
                ("kind", kind), ("correlation_id", correlation_id)))


def _row_to_event(row) -> dict:
    return {"seq": row["seq"], "id": row["id"], "ts": row["ts"],
            "namespace": row["namespace"], "topic": row["topic"], "key": row["key"],
            "kind": row["kind"], "provenance": json.loads(row["provenance"]),
            "payload": json.loads(row["payload"]),
            "correlation_id": row["correlation_id"],
            "visible_after": row["visible_after"]}
