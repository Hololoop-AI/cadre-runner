"""Which session launched which — the tree behind orchestrator sessions.

A session that orchestrates others launches them with `pipeline.py task`
from inside its own turn. Each launch is one task-request event on the board,
and the board stamps every write with the writer's identity from the session
environment (`blackboard.provenance_from_env`: `session_id` is
CADRE_SESSION_ID). So the parent of every task launched from a session is
already on disk, as a session id; this module turns it into a task id.

Two sources, used together:

- the request's own `parent` field: `tasks.submit_task` writes CADRE_TASK
  there when it runs inside a session. Explicit, and it wins.
- the request's provenance session id, matched to the task that owned that
  session: the registry's current `session_id` per task, plus every finished
  turn's `out.json` under `runs/<task>/` (a task that re-minted its session
  keeps the old ids there). This is what makes the tree work for every launch
  made before the explicit field existed.

A launch whose session matches no task here (a session on another backend, an
interactive terminal) is a root that remembers the session it came from.

Pure functions over plain dicts, plus the explicit loader at the bottom, so
the page server stays read-only and the tests stay hermetic.
"""

import json
import re
import sqlite3
from pathlib import Path

from . import recent
from .tasks import NAMESPACE, TARGET_REQUEST, TOPIC

_SESSION_ID = re.compile(r'"session_id"\s*:\s*"([0-9a-f-]{36})"')
RESULT_CHARS = 1500   # what the tree page shows of a last result; the API ships no more
_SCANNED: dict = {}  # out.json path -> (mtime, session id); re-read only if rewritten


def link(requests: list[dict], session_task: dict) -> dict:
    """{child task: parent task}. `requests` carry `task`, and `parent`
    and/or `session` (the launcher's session id); `session_task` maps session
    id to the task that ran it. A task never parents itself, and a link that would
    close a loop is dropped rather than drawn forever."""
    parents = {}
    for r in requests:
        task = r.get("task")
        parent = r.get("parent") or session_task.get(r.get("session") or "")
        if not task or not parent or parent == task:
            continue
        node, seen = parent, {task}
        while node in parents and node not in seen:
            seen.add(node)
            node = parents[node]
        if node in seen:
            continue
        parents[task] = parent
    return parents


def children(parents: dict, order: dict | None = None) -> dict:
    """{parent: [children, oldest launch first]}."""
    order = order or {}
    out: dict[str, list] = {}
    for child, parent in parents.items():
        out.setdefault(parent, []).append(child)
    for kids in out.values():
        kids.sort(key=lambda t: (order.get(t) or 0, t))
    return out


def descendants(task: str, kids: dict) -> int:
    """How many sessions sit anywhere under `task`."""
    n, stack, seen = 0, list(kids.get(task) or ()), {task}
    while stack:
        t = stack.pop()
        if t in seen:
            continue
        seen.add(t)
        n += 1
        stack.extend(kids.get(t) or ())
    return n


def ancestors(task: str, parents: dict) -> list[str]:
    """Root first, ending at `task`'s parent."""
    out, node, seen = [], parents.get(task), {task}
    while node and node not in seen:
        seen.add(node)
        out.append(node)
        node = parents.get(node)
    return out[::-1]


# ------------------------------------------------------------------ loader

def _requests(db: Path) -> list[dict]:
    """Every task request on the board, oldest first. Read-only and
    best-effort, like the fleet's other board reads: anything unreadable is
    no requests, never an error page."""
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT ts, provenance, payload FROM events WHERE namespace=? "
                "AND topic=? AND kind='command' ORDER BY seq",
                (NAMESPACE, TOPIC)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out = []
    for ts, prov, payload in rows:
        try:
            p, v = json.loads(payload), json.loads(prov or "{}")
        except ValueError:
            continue
        if not isinstance(p, dict) or p.get("target") != TARGET_REQUEST or not p.get("task"):
            continue
        parent = p.get("parent")
        out.append({"task": str(p["task"]), "title": str(p.get("title") or ""),
                    "project": str(p.get("project") or ""), "since": ts,
                    "parent": parent if isinstance(parent, str) else "",
                    "session": str((v if isinstance(v, dict) else {}).get("session_id") or "")})
    return out


def _run_sessions(runs_dir: Path, tasks: set) -> dict:
    """{session id: task} from every finished turn's out.json of `tasks`.
    Story stages write runs/<slug>/ too, but a slug is no task, so a launch
    from one of those sessions stays a root instead of naming a parent that
    has no tree page."""
    global _SCANNED
    out, scanned = {}, {}
    for f in runs_dir.glob("*/*/out.json"):
        if f.parent.parent.name not in tasks:
            continue
        try:
            mtime = f.stat().st_mtime
            hit = _SCANNED.get(f)
            if not hit or hit[0] != mtime:
                m = _SESSION_ID.search(f.read_text(encoding="utf-8", errors="replace"))
                hit = (mtime, m.group(1) if m else "")
        except OSError:
            continue
        scanned[f] = hit
        if hit[1]:
            out[hit[1]] = f.parent.parent.name
    _SCANNED = scanned   # rebuilt each scan, so deleted runs drop out
    return out


def load(data_dir) -> dict:
    """{task: node} for every task that has a parent or children — the only
    tasks the tree has anything to say about. JSON-shaped, so a fleet on
    another machine renders it from the API without touching this disk. A
    node carries title, project, since, parent, launcher (an unresolved
    launching session's short id), working, node, round, result (the last
    turn's summary) and page (its newest page's /session path)."""
    d = Path(data_dir)
    try:
        reg = json.loads((d / "registry.json").read_text(encoding="utf-8"))
        tasks = (reg if isinstance(reg, dict) else {}).get("tasks")
    except (OSError, ValueError):
        tasks = None
    tasks = tasks if isinstance(tasks, dict) else {}
    try:
        store = json.loads((d / "surfaces" / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        store = {}
    reqs = _requests(d / "board.db")
    known = {r["task"] for r in reqs}
    # A parent that is no task here (its request on another board, a reset
    # data dir) would be a link to a tree page that does not exist: fall back
    # to the launching session, which is at worst a root.
    reqs = [{**r, "parent": r["parent"] if r["parent"] in known else ""} for r in reqs]
    session_task = _run_sessions(d / "runs", known)
    session_task.update({str(r["session_id"]): t for t, r in tasks.items()
                         if isinstance(r, dict) and r.get("session_id")})
    parents = link(reqs, session_task)
    kids = children(parents, {r["task"]: r["since"] for r in reqs})
    pages = recent.pages_by_task(store)
    out = {}
    for r in reqs:
        t = r["task"]
        if t not in parents and t not in kids:
            continue
        rec = tasks.get(t) if isinstance(tasks.get(t), dict) else {}
        out[t] = {"title": r["title"] or t, "project": r["project"] or rec.get("project") or "",
                  "since": r["since"], "parent": parents.get(t, ""),
                  "launcher": "" if t in parents else r["session"][:8],
                  "working": bool(rec.get("active_runs")),
                  "node": str(rec.get("node") or "task"),
                  "round": str(rec.get("iteration") or ""),
                  "result": str(rec.get("last_result") or "")[:RESULT_CHARS],
                  "page": str((pages.get(t) or {}).get("path") or "")}
    return out


def clean(nodes) -> dict:
    """load()'s nodes as a renderer can trust them: another machine's payload
    is version-skewed at best, and one bad node must not take its section
    down. Drops non-dict nodes, and blanks a parent that is not a string and
    a `since` that is not a number."""
    out = {}
    for t, n in (nodes.items() if isinstance(nodes, dict) else ()):
        if not isinstance(n, dict):
            continue
        parent, since = n.get("parent"), n.get("since")
        out[str(t)] = {**n, "parent": parent if isinstance(parent, str) else "",
                       "since": since if isinstance(since, (int, float)) else 0}
    return out


def parents_of(nodes: dict) -> dict:
    """{child: parent} rebuilt from load()'s nodes — what a renderer holding
    only the JSON (another machine's payload) works from."""
    return {t: n["parent"] for t, n in nodes.items() if n.get("parent")}


def kids_of(nodes: dict) -> dict:
    """children() rebuilt from load()'s nodes, as parents_of()."""
    return children(parents_of(nodes), {t: n.get("since") for t, n in nodes.items()})
