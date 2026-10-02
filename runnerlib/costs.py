"""What the fleet has spent — the data behind the costs view.

Every finished turn already lands one line in <data_dir>/history.jsonl with
the `total_cost_usd` Claude Code reported for it (pipeline.py writes it on
reap). Nothing new is recorded here; this only reads that log back and adds
it up, by node, by project and by task, so the driver can see where the money
goes instead of doing the sum by hand.

A turn with no reported cost (lost, timed out before writing its result,
paused on a usage limit) is counted as unpriced rather than as $0 — a free
turn and an unknown one must never read the same.
"""

import json
import math
import time
from pathlib import Path

DAY = 86_400.0


def read(path: Path) -> list[dict]:
    """Every finished turn, oldest first. A missing log is no spend. A line
    caught half-written (a multibyte character cut off) is skipped, never
    allowed to take the fleet page down with a decode error."""
    try:
        lines = Path(path).read_bytes().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            # NaN/Infinity read as no cost: they would make /costs.json invalid
            e = json.loads(line, parse_constant=lambda _: None)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def task_projects(registry: dict, projects: dict | None = None) -> dict:
    """{task or story id: project name} from the registry file's contents.
    Dialogue tasks carry their project; pipeline stories carry their repo.
    Tasks from before tasks recorded a project are filed by their working
    directory: the project whose directory holds it most closely."""
    dirs = sorted(((str(d).rstrip("/"), str(p.get("name") or pid))
                   for pid, p in (projects or {}).items() if isinstance(p, dict)
                   for d in p.get("dirs") or ()), key=lambda x: -len(x[0]))
    out = {}
    for tid, rec in (registry.get("tasks") or {}).items():
        if not isinstance(rec, dict):
            continue
        if rec.get("project"):
            out[tid] = str(rec["project"])
            continue
        cwd = str(rec.get("cwd") or "").rstrip("/")
        name = next((n for d, n in dirs if cwd == d or cwd.startswith(d + "/")), None)
        if name:
            out[tid] = name
    for sid, rec in (registry.get("stories") or {}).items():
        if isinstance(rec, dict) and rec.get("repo"):
            out[sid] = str(rec["repo"])
    return out


def cost_of(e: dict):
    c = e.get("cost_usd")
    if not isinstance(c, (int, float)) or isinstance(c, bool):
        return None
    return float(c) if math.isfinite(c) else None


def _bucket(acc: dict, key: str, e: dict, cost):
    b = acc.setdefault(key, {"key": key, "usd": 0.0, "runs": 0, "unpriced": 0,
                             "seconds": 0.0, "last": 0.0})
    b["runs"] += 1
    if cost is None:
        b["unpriced"] += 1
    else:
        b["usd"] += cost
    b["seconds"] += float(e.get("seconds") or 0)
    b["last"] = max(b["last"], float(e.get("ended") or 0))


def _ranked(acc: dict) -> list[dict]:
    return sorted(acc.values(), key=lambda b: (-b["usd"], -b["last"]))


def summarize(entries: list[dict], projects: dict | None = None,
              now: float | None = None) -> dict:
    """Totals plus the same spend cut three ways. `projects` maps a task id to
    its project; a task it does not know is filed under "unknown"."""
    now = time.time() if now is None else now
    projects = projects or {}
    by_node, by_project, by_task = {}, {}, {}
    total = day = week = 0.0
    priced = 0
    for e in entries:
        cost = cost_of(e)
        task = str(e.get("story") or "?")
        _bucket(by_node, str(e.get("stage") or "?"), e, cost)
        _bucket(by_project, projects.get(task, "unknown"), e, cost)
        _bucket(by_task, task, e, cost)
        by_task[task]["project"] = projects.get(task, "unknown")
        if cost is None:
            continue
        priced += 1
        total += cost
        age = now - float(e.get("ended") or 0)
        day += cost if age <= DAY else 0.0
        week += cost if age <= 7 * DAY else 0.0
    return {
        "total_usd": total, "runs": len(entries), "priced": priced,
        "unpriced": len(entries) - priced,
        "last_24h_usd": day, "last_7d_usd": week,
        "by_node": _ranked(by_node), "by_project": _ranked(by_project),
        "by_task": _ranked(by_task),
        "recent": list(reversed(entries[-30:])),
    }


def per_task(entries: list[dict]) -> dict:
    """{task id: dollars spent so far} — what a fleet row shows beside it."""
    out = {}
    for e in entries:
        cost = cost_of(e)
        if cost is not None:
            task = str(e.get("story") or "")
            out[task] = out.get(task, 0.0) + cost
    return out


def usd(x: float) -> str:
    """$0.41, $12.30, $1,176 — cents stop mattering past a hundred dollars."""
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"
