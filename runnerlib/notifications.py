"""Cadre's notification center: what moved on review pages, one row per page,
newest first, for the bell on every Fleet screen and on every review page.

Review Surface only records what happened, as an append-only event log
(`events.jsonl` in its state directory: `page.opened`, `page.version`,
`page.agent_reply`, ...). Deciding what is news, and showing it, is Cadre's:
statusd answers `GET /api/notifications` itself from that log; the page
server has no summary or bell of its own. Read-only, like the
other readers of the page server's files (runnerlib/conversations.py).

One row per page, its latest event: three rewrites of one page are one row. Thread pages are left out (the page's threads footer counts their
replies). Leaving out the page the reader is on is the browser's to do: it
still chimes for that page.

The page server's events say a page changed, not why, and most changes are
an agent at work. Cadre's runner knows what happened: it stamps each turn's
end in its history log (runnerlib/recent.py). So each dialogue page is in
one of three tiers (the driver's call, 19 Nov 2026):

- needs you (`actionable`, counted, chimes): its task's newest turn came back
  and you have not answered since ("Your turn"; when the turn failed, what
  happened to it: "Run timed out", "Run interrupted" or "Run failed", which
  is `problem`, with its own icon and chime); or an agent
  replied in a page's chat ("Agent replied").
- working (listed quietly, never counted or chimed): the agent has the work
  and has not come back yet ("Working on it": its task has a turn in flight
  in the runner's registry), timed from your answer or its first write, so
  its rewrites while it works do not move it.
- not listed: rewrites mid-turn (folded into the working row), side pages an
  agent makes that no dialogue owns, an earlier round's page, and a page you
  answered that the agent has not touched since.
"""

import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from runnerlib import conversations as conv_mod
from runnerlib import costs as costs_mod
from runnerlib import recent as recent_mod

# the page server's events that can be news; what each row is called is decided in classify
NEWS_TYPES = frozenset({"page.version", "page.agent_reply", "page.opened"})
AGENT_REPLIED = "Agent replied"
YOUR_TURN = "Your turn"
# what a failed turn's own log says happened to it (runnerlib/runs.py outcome)
RUN_TIMED_OUT = "Run timed out"   # stopped at its deadline
RUN_INTERRUPTED = "Run interrupted"  # its process went away without an exit code
RUN_FAILED = "Run failed"          # it exited with an error
WORKING = "Working on it"
# how much of the log's tail is read: enough for LIMIT pages, without replaying a log that only grows
TAIL_BYTES = 256 * 1024
LIMIT = 30
# the page server logs a turn's last page write up to ~0.3 s after the runner stamps the
# turn's end (blue, 552 turns to 18 Nov 2026); a next turn's placeholder comes later than this
RETURN_SLACK_S = 2.0
# a page written after its last turn ended, with no turn in flight, reads as the runner catching
# up; after this long it is a run that died without a stamp, and is no longer listed
STALE_WORKING_S = 30 * 60


def _recent_events(log: Path, max_bytes: int = TAIL_BYTES) -> list[dict]:
    """The log's last `max_bytes`, oldest first. A start inside a line, or a
    line torn by a crash mid-append, is skipped. No log is no events."""
    try:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    out = []
    for line in data.decode("utf-8", errors="replace").split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _is_news(event: dict) -> bool:
    kind = event.get("type")
    if not isinstance(kind, str) or kind not in NEWS_TYPES:  # a foreign line may carry anything
        return False
    # a reopen of a page you already had is not news
    data = event.get("data")
    return kind != "page.opened" or (isinstance(data, dict) and data.get("new") is True)


def summarize(events: list[dict], thread_keys: frozenset = frozenset()) -> list[dict]:
    """One row per page that moved, its latest event, newest first."""
    latest: dict[str, dict] = {}
    for event in events:
        if not _is_news(event):
            continue
        key = str(event.get("subject") or "")
        if not key or key in thread_keys:
            continue
        latest.pop(key, None)  # re-insert: dict order is recency
        latest[key] = {"key": key, "type": event["type"], "at": str(event.get("ts") or "")}
    return list(reversed(latest.values()))


def _thread_keys() -> frozenset:
    """Pages that are conversation threads on another page (`thread-of` links
    in the page server's link store, wherever conversations.link_files says
    it is). conversations.parse_links keeps only the graph's own link types."""
    try:
        text = conv_mod.link_files(None)[0].read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    keys = set()
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("type") == "thread-of" and r.get("from"):
            keys.add(str(r["from"]))
    return frozenset(keys)


_SESSIONS_CACHE: dict = {}


def _sessions(state_dir: Path) -> dict:
    """The page server's sessions (state.json), key -> its record. The file is
    large and is rewritten in place, so it is read again only when it changed,
    and a read that catches it half-written keeps the last good copy."""
    f = state_dir / "state.json"
    try:
        st = f.stat()
    except OSError:
        return {}
    stamp = (str(f), st.st_mtime_ns, st.st_size)
    hit = _SESSIONS_CACHE.get(str(f))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        sessions = json.loads(f.read_text(encoding="utf-8")).get("sessions")
    except (OSError, ValueError, AttributeError):
        return hit[1] if hit else {}
    sessions = sessions if isinstance(sessions, dict) else {}
    _SESSIONS_CACHE[str(f)] = (stamp, sessions)
    return sessions


def _iso(epoch: float) -> str:
    """The page server's own timestamp format, so the browser compares them as text."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _answered(events: list[dict]) -> dict:
    """{page key: epoch the driver last sent it feedback}."""
    out = {}
    for event in events:
        if event.get("type") == "page.feedback_sent" and event.get("subject"):
            out[str(event["subject"])] = conv_mod.epoch(event.get("ts") or "")
    return out


def _newest_pages(runner_dir: Path | None) -> dict:
    """{page key: its task}, for each dialogue task's newest page only: an
    earlier round's page of the same task is history, never "Your turn"."""
    if runner_dir is None:
        return {}
    try:
        store = json.loads((runner_dir / "surfaces" / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(m["key"]): task for task, m in recent_mod.pages_by_task(store).items() if m.get("key")}


def _returned(turns: list[dict]) -> dict:
    """{task: (epoch its newest turn ended, whether that turn succeeded)}; a line with a bad stamp is skipped."""
    out: dict[str, tuple[float, bool]] = {}
    for t in turns:
        try:
            task, ended = str(t.get("story") or ""), float(t.get("ended") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if task and math.isfinite(ended) and 0 < ended < 1e11 and ended > out.get(task, (0.0, True))[0]:
            out[task] = (ended, bool(t.get("ok")))  # as the runs screen reads it (recent._turn_row)
    return out


def _working_since(events: list[dict]) -> dict:
    """{page key: ISO time of its first change since you last answered it}:
    when the agent's current spell of work on it began."""
    out: dict[str, str] = {}
    for event in events:
        key = str(event.get("subject") or "")
        if event.get("type") == "page.feedback_sent":
            out.pop(key, None)
        elif key and _is_news(event) and key not in out:
            out[key] = str(event.get("ts") or "")
    return out


_RUN_RECORD = re.compile(r"\d{8}-\d{6}-.+\.json")


def failure_label(runner_dir: Path | None, task: str) -> str:
    """What happened to a task's newest turn, which failed: read off the record
    the runner writes for each turn (logs/<task>/<run>.json, newest last)."""
    try:
        # run records are named <yyyymmdd>-<hhmmss>-<stage>.json; the folder holds other files too
        newest = max((f for f in (runner_dir / "logs" / task).glob("*.json") if _RUN_RECORD.fullmatch(f.name)),
                     key=lambda f: f.name)
        record = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):  # no log (or no runner dir): say only that it failed
        return RUN_FAILED
    if not isinstance(record, dict):
        return RUN_FAILED
    return RUN_TIMED_OUT if record.get("timed_out") else RUN_INTERRUPTED if record.get("lost") else RUN_FAILED


def _running(runner_dir: Path | None) -> frozenset:
    """Tasks with a turn in flight, from the runner's registry."""
    if runner_dir is None:
        return frozenset()
    try:
        tasks = json.loads((runner_dir / "registry.json").read_text(encoding="utf-8")).get("tasks") or {}
    except (OSError, ValueError, AttributeError):
        return frozenset()
    return frozenset(k for k, rec in tasks.items() if isinstance(rec, dict) and rec.get("active_runs"))


def classify(rows: list[dict], page_tasks: dict, returned: dict, answered: dict, since: dict,
             running: frozenset = frozenset(), now: float | None = None,
             failure=lambda task: RUN_FAILED) -> list[dict]:
    """The listed rows, each with `actionable`, newest first; see the module's
    three tiers. `page_tasks` names each dialogue task's newest page;
    `running`, the tasks with a turn in flight. Times are compared as epochs
    and written back in the page server's format."""
    now = time.time() if now is None else now
    out: list[tuple[float, dict]] = []
    for row in rows:
        at = conv_mod.epoch(row["at"])
        if not at:
            continue  # a time that does not parse cannot be placed
        if row["type"] == "page.agent_reply":
            out.append((at, {**row, "actionable": True, "problem": False, "label": AGENT_REPLIED}))
            continue
        task = page_tasks.get(row["key"])
        if task is None:
            continue  # a side page, or an earlier round's page
        ended, ok = returned.get(task, (0.0, True))
        last_answer = answered.get(row["key"], 0.0)
        first_write = conv_mod.epoch(since.get(row["key"], "")) or at
        if task in running:
            # at work: from your answer if it has not written since, else from its first write since
            started = last_answer if last_answer >= at else first_write
            out.append((started, {**row, "actionable": False, "problem": False, "label": WORKING}))
        elif ended and ended >= at - RETURN_SLACK_S and last_answer < ended:
            when = max(at, ended)
            out.append((when, {**row, "actionable": True, "problem": not ok, "label": YOUR_TURN if ok else failure(task)}))
        elif last_answer >= at or now - at > STALE_WORKING_S:
            continue  # you answered and the agent has not written since; or a run that died unstamped
        else:  # written after its last turn ended, the run not yet in the registry
            out.append((first_write, {**row, "actionable": False, "problem": False, "label": WORKING}))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return [{**r, "at": _iso(when)} for when, r in out]


def payload(state_dir: Path, runner_dir: Path | None = None) -> dict:
    """GET /api/notifications: the listed rows with their page's title and
    address, and whether each needs the driver. A page the page server no
    longer knows is dropped. The event log is where the page server keeps it
    (REVIEW_SURFACE_EVENTS, else beside state.json); `runner_dir` is Cadre's
    data dir (its page store and history log); only the listed rows' titles
    are read."""
    log = Path(os.environ.get("REVIEW_SURFACE_EVENTS") or state_dir / "events.jsonl")
    events = _recent_events(log)
    rows = summarize(events, thread_keys=_thread_keys())
    turns = costs_mod.read(runner_dir / "history.jsonl") if runner_dir is not None else []
    rows = classify(rows, _newest_pages(runner_dir), _returned(turns), _answered(events), _working_since(events),
                    _running(runner_dir), failure=lambda task: failure_label(runner_dir, task))[:LIMIT]
    sessions = _sessions(state_dir)
    out = []
    for row in rows:
        known = sessions.get(row["key"])
        if not isinstance(known, dict):
            continue
        out.append({**row, "title": conv_mod.page_title(str(known.get("file") or "")),
                    "url": f"/session/{row['key']}"})
    return {"notifications": out}
