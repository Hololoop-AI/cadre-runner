"""What came back, and when — the data behind the runs view.

A turn has RETURNED when the runner reaped it: the `ended` stamp pipeline.py
writes to history.jsonl. That is how new a piece of work truly is. It is not
the page server's `updated_at`, which moves whenever anything touches the
page — the driver opening it included — so it says when the page was last
handled, not when the agent last delivered. The fleet shows both; this module
owns the first, across every project at once.

Nothing new is recorded. This reads the same history log costs.py reads, plus
the runner's page store (surfaces/sessions.json) to know where each task's
page is and whether it is still open.
"""

from . import costs

# One row per view, in the order the view picker offers them.
VIEWS = {
    "latest": "latest return per task",
    "open": "returned, page still open",
    "turns": "every turn",
    "failed": "failed turns",
}
SORTS = {
    "newest": "newest return first",
    "oldest": "oldest return first",
    "cost": "most expensive first",
    "longest": "longest running first",
}


def last_returned(entries: list[dict]) -> dict:
    """{task id: epoch its newest turn ended} — the "returned" badge."""
    out = {}
    for e in entries:
        task = str(e.get("story") or "")
        ended = float(e.get("ended") or 0)
        if task and ended > out.get(task, 0.0):
            out[task] = ended
    return out


def pages_by_task(store: dict) -> dict:
    """{task id: its newest page's session meta} from sessions.json contents.
    A task that wrote several pages links to the one opened last."""
    out = {}
    for artifact, meta in (store.items() if isinstance(store, dict) else ()):
        if not isinstance(meta, dict) or not meta.get("task") or not meta.get("path"):
            continue
        task = str(meta["task"])
        if (meta.get("opened") or 0) >= (out.get(task, {}).get("opened") or 0):
            out[task] = {**meta, "artifact": artifact}
    return out


def _turn_row(e: dict, owners: dict, pages: dict) -> dict:
    task = str(e.get("story") or "")
    page = pages.get(task)
    return {"task": task, "project": owners.get(task) or (page or {}).get("project") or "",
            "node": str(e.get("stage") or ""), "ended": float(e.get("ended") or 0),
            "seconds": float(e.get("seconds") or 0), "cost": costs.cost_of(e),
            "ok": bool(e.get("ok")), "turns": 1, "page": page,
            "open": bool(page and page.get("open"))}


def _per_task(turns: list[dict]) -> list[dict]:
    """Fold turns into one row per task: the newest turn's node, end and
    outcome, with the task's total turns, time and spend."""
    by = {}
    for t in turns:
        cur = by.get(t["task"])
        if cur is None:
            by[t["task"]] = dict(t)
            continue
        newer = t if t["ended"] >= cur["ended"] else cur
        priced = [c for c in (cur["cost"], t["cost"]) if c is not None]
        by[t["task"]] = {**newer, "turns": cur["turns"] + 1,
                         "seconds": cur["seconds"] + t["seconds"],
                         "cost": sum(priced) if priced else None}
    return list(by.values())


def rows(entries: list[dict], owners: dict, pages: dict, view: str = "latest",
         sort: str = "newest", project: str = "", node: str = "",
         q: str = "") -> list[dict]:
    """The runs list for one view. `owners` is {task: project name}
    (costs.task_projects), `pages` is pages_by_task(). Unknown view or sort
    names fall back to the defaults rather than erroring — they come off a
    query string."""
    view = view if view in VIEWS else "latest"
    sort = sort if sort in SORTS else "newest"
    out = [_turn_row(e, owners, pages) for e in entries if e.get("story")]
    if view in ("latest", "open"):
        out = _per_task(out)
    if view == "open":
        out = [r for r in out if r["open"]]
    if view == "failed":
        out = [r for r in out if not r["ok"]]
    if project:
        out = [r for r in out if r["project"] == project]
    if node:
        out = [r for r in out if r["node"] == node]
    # a task id reads as words ("task fix it"), so a pasted id with its
    # hyphens has to be read the same way to match
    needle = q.strip().lower().replace("-", " ")
    if needle:
        out = [r for r in out
               if needle in r["task"].replace("-", " ").lower()
               or needle in r["project"].lower()
               or needle in str((r["page"] or {}).get("title") or "").lower()]
    key = {"newest": lambda r: -r["ended"], "oldest": lambda r: r["ended"],
           "cost": lambda r: (-(r["cost"] or 0.0), -r["ended"]),
           "longest": lambda r: (-r["seconds"], -r["ended"])}[sort]
    return sorted(out, key=key)
