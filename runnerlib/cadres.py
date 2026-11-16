"""Cadres: which cadre owns each run, and what a cadre's app reads.

A cadre is a group of work on the fleet's rail (Dev, Kitchen, Personal, …):
a live project that another live project names as its group, or a live
project with no folder (web/src/model/groups.ts `fleetGroups`, ported
here). Work run in a cadre is that cadre's: its runs show only in its app
(the fleet route /c/<id>), and Hololoop (the fleet's home) sees a card per
cadre, never the runs.

Ownership is fixed at launch and kept in `<data_dir>/cadres.json`:

    {"tasks": {task id: cadre id}}

`pipeline.py task --cadre dev` and New work (from the chosen rail entry)
name it; without one a task joins its parent's cadre, else the group rule
below picks it. `migrate` fills the same file once for runs launched before
there was one. A run in no cadre belongs to OTHER, so every run has one.

The group rule, as groups.ts has it: a tag naming a cadre (by id or name)
wins; else the project's nearest group, walking up; else OTHER.

Two processes write the file (the CLI at launch, the fleet server's
migration), so every write holds an flock across read, change and replace.
"""

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import costs as costs_mod
from . import projects

FILE = "cadres.json"
LOCK = "cadres.lock"

# The rail's entry for work in no cadre; groups.ts calls it OTHER too.
OTHER = "~other"
OTHER_NAME = "Other"

# What a cadre's header tabs show when its project record names no `nav`.
# Each is a screen of the fleet app under /c/<id>/; "home" is /c/<id> itself.
# The fleet-wide screens (runs, costs, the library) are not a cadre's: they
# stay on Hololoop until they can be cut to one cadre's runs.
DEFAULT_NAV = ("home", "agents", "settings")
# The cadre that owns the kanban board (kanban.json's `board`) also gets its
# two system screens: the Kanban board (cards a person and agents move) and
# the Blackboard (the event store agents write to each other,
# runnerlib/blackboard.py). They are different things; no other cadre has
# either until it is given one through its record's `nav`.
BOARD_NAV = ("home", "kanban", "blackboard", "agents", "settings")
SCREENS = {
    "home": "Home",
    "kanban": "Kanban",
    "blackboard": "Blackboard",
    "agents": "Agents",
    "settings": "Settings",
}


# --------------------------------------------------------------------- which cadres


def cadre_list(projs: dict) -> list[dict]:
    """[{id, name}] of the cadres, in project order: live projects some live
    project belongs to, or with no folder (groups.ts fleetGroups)."""
    live = projects.live(projs)
    named = {p.get("group") for p in live if p.get("group")}
    return [{"id": p["id"], "name": p["name"]} for p in live
            if p["id"] in named or (isinstance(p.get("dirs"), list) and not p["dirs"])]


def known(projs: dict, cadre: str) -> bool:
    return cadre == OTHER or any(c["id"] == cadre for c in cadre_list(projs))


def resolve(projs: dict, ref) -> str | None:
    """A cadre's id from its id or name (any case); OTHER for "other"."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref.lower() in (OTHER, "other"):
        return OTHER
    for c in cadre_list(projs):
        if ref.lower() in (c["id"].lower(), c["name"].lower()):
            return c["id"]
    return None


def rule(projs: dict, project, tags=()) -> str:
    """The cadre the group rule gives a run: a tag naming a cadre, else the
    project's nearest cadre going up, else OTHER."""
    cads = cadre_list(projs)
    ids = {c["id"] for c in cads}
    by_word = {}
    for c in cads:
        by_word[c["id"].lower()] = c["id"]
        by_word[c["name"].lower()] = c["id"]
    for t in tags or ():
        if str(t).lower() in by_word:
            return by_word[str(t).lower()]
    node = (projects.resolve(projs, project) or {}).get("id") if project else None
    seen = set()
    while node and node not in seen:
        seen.add(node)
        if node in ids:
            return node
        node = (projs.get(node) or {}).get("group")
    return OTHER


# --------------------------------------------------------------------- the store


def _path(data_dir) -> Path:
    return Path(data_dir) / FILE


def load(data_dir) -> dict:
    """{task id: cadre id}; a missing or malformed file is empty."""
    try:
        raw = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, dict):
        return {}
    return {str(k): str(v) for k, v in tasks.items() if isinstance(v, str) and v}


@contextmanager
def _locked(data_dir):
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / LOCK, "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _save(data_dir, tasks: dict) -> None:
    d = Path(data_dir)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cadres-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks, "updated": time.time()}, f, indent=1, sort_keys=True)
    os.replace(tmp, _path(d))


def assign(data_dir, task_id: str, cadre: str) -> None:
    """Give `task_id` to `cadre`, replacing any earlier owner."""
    with _locked(data_dir):
        tasks = load(data_dir)
        tasks[str(task_id)] = str(cadre)
        _save(data_dir, tasks)


def at_launch(data_dir, projs: dict, *, cadre=None, parent=None, project=None) -> str:
    """The cadre a task being launched belongs to: the one named (refused
    when no such cadre), else its parent's, else the group rule."""
    if cadre:
        got = resolve(projs, cadre)
        if got is None:
            names = ", ".join(c["id"] for c in cadre_list(projs)) or "none yet"
            raise ValueError(f"no cadre {cadre!r} (cadres: {names}, or other)")
        return got
    if parent:
        mine = load(data_dir).get(str(parent))
        if mine:
            return mine
    return rule(projs, project)


def migrate(data_dir, task_records: dict, items_store: dict, projs: dict) -> int:
    """Give every task in `task_records` ({task id: record with `project`})
    that has no cadre yet the one today's group rule gives it, with the tags
    its item carries (`items_store`, runnerlib/items.py). Returns how many it
    filled; running it again fills none."""
    with _locked(data_dir):
        tasks = load(data_dir)
        filled = 0
        for tid, rec in (task_records or {}).items():
            if not tid or tid in tasks:
                continue
            tags = (items_store.get(tid) or {}).get("tags") or ()
            tasks[tid] = rule(projs, (rec or {}).get("project"), tags)
            filled += 1
        if filled:
            _save(data_dir, tasks)
        return filled


# --------------------------------------------------------------------- reading


def chain(projs: dict, cadre: str) -> list[str]:
    """`cadre` and every cadre above it: a run in a nested cadre shows in
    the cadres holding it too, as the fleet's groups always have."""
    if cadre == OTHER:
        return [OTHER]
    ids = {c["id"] for c in cadre_list(projs)}
    out, node = [], cadre
    while node and node not in out:
        if node in ids or node == cadre:
            out.append(node)
        node = (projs.get(node) or {}).get("group")
    return out


def owner(tasks: dict, projs: dict, task: str, project="", tags=()) -> str:
    """The cadre a run (or a page with no task) belongs to."""
    return tasks.get(task) if task and tasks.get(task) else rule(projs, project, tags)


def payload_for(payload: dict, cadre: str) -> dict:
    """The fleet payload (statusd.fleet_payload) with only `cadre`'s runs:
    its pages, its page-less launches, its running agents, its spend and its
    recent turns. A page with no task is filed by the group rule, with its
    item's tags. Everything that is not a run (projects, the tree, the code
    stamp) is left as it is."""
    tasks = payload.get("cadres") or {}
    projs = payload.get("projs") or {}
    store = payload.get("items") or {}

    def mine_task(t) -> bool:
        return cadre in chain(projs, owner(tasks, projs, str(t or "")))

    def mine_page(sf) -> bool:
        task = str(sf.get("task") or "")
        if task:
            return mine_task(task)
        key = "page:" + str(sf.get("path") or "").rsplit("/", 1)[-1]
        tags = (store.get(key) or {}).get("tags") or ()
        return cadre in chain(projs, rule(projs, sf.get("project"), tags))

    out = dict(payload)
    snap = dict(payload.get("snap") or {})
    snap["surfaces"] = [sf for sf in snap.get("surfaces") or [] if mine_page(sf)]
    out["snap"] = snap
    for k in ("pageless", "running", "recent"):
        out[k] = [r for r in payload.get(k) or [] if mine_task(r.get("task"))]
    for k in ("doing", "spent", "returned", "endings"):
        out[k] = {t: v for t, v in (payload.get(k) or {}).items() if mine_task(t)}
    out["starting"] = {pid: [r for r in rows if mine_task(r.get("task"))]
                       for pid, rows in (payload.get("starting") or {}).items()}
    out["week_usd"] = (payload.get("cadre_week_usd") or {}).get(cadre, 0.0)
    out["cadre"] = cadre
    return out


def week_usd(turns: list[dict], tasks: dict, projs: dict, now: float | None = None) -> dict:
    """{cadre id: dollars its runs spent in the last 7 days}; a nested
    cadre's spend counts in the cadres holding it too."""
    now = time.time() if now is None else now
    out: dict = {}
    for e in turns or ():
        cost = costs_mod.cost_of(e)
        if cost is None or now - float(e.get("ended") or 0) > 7 * costs_mod.DAY:
            continue
        for c in chain(projs, owner(tasks, projs, str(e.get("story") or ""))):
            out[c] = out.get(c, 0.0) + cost
    return out


def nav(projs: dict, cadre: str, *, board: str = "") -> dict:
    """What the shell draws for `cadre` (GET /api/nav?cadre=<id>): its rail
    entry, its screens as header tabs (from the project record's `nav`, else
    BOARD_NAV for the cadre owning the kanban board `board`, else
    DEFAULT_NAV; Settings is always there), its second column — batches
    (none until the board has them) and pinned pages (the record's
    `pinned`: [{label, href}]) — and what its Settings tab shows."""
    rec = projs.get(cadre) or {}
    name = OTHER_NAME if cadre == OTHER else rec.get("name") or cadre
    base = f"/c/{cadre}"
    default = BOARD_NAV if board and cadre == board else DEFAULT_NAV
    wanted = [s for s in (rec.get("nav") or default) if s in SCREENS]
    if "settings" not in wanted:
        wanted.append("settings")
    screens = [{"id": s, "label": SCREENS[s], "href": base if s == "home" else f"{base}/{s}"}
               for s in wanted]
    pinned = [{"label": str(p.get("label") or p.get("href")), "href": str(p["href"])}
              for p in rec.get("pinned") or () if isinstance(p, dict) and p.get("href")]
    return {"cadre": {"id": cadre, "name": name}, "screens": screens,
            "batches": [], "pinned": pinned,
            "cadres": [*cadre_list(projs), {"id": OTHER, "name": OTHER_NAME}],
            "settings": settings(projs, cadre)}


def settings(projs: dict, cadre: str) -> dict:
    """What the Settings tab shows: the cadre's own setup, read from its
    project record — the folders and projects whose runs it gets, the tabs
    it asked for (`nav`, empty for the default) — not its work."""
    if cadre == OTHER:
        return {"project": "", "folders": [], "members": [], "nav": []}
    rec = projs.get(cadre) or {}
    live = projects.live(projs)
    members = [{"id": p["id"], "name": p["name"]} for p in live if p.get("group") == cadre]
    return {"project": cadre,
            "folders": [str(d) for d in rec.get("dirs") or () if isinstance(d, str)],
            "members": members,
            "nav": [s for s in rec.get("nav") or () if s in SCREENS]}


def migrate_dir(data_dir) -> int:
    """`migrate` over this data directory: the runner's registry, the items'
    tags and the projects. The fleet server runs it at start; it is also
    `pipeline.py cadres migrate`."""
    from . import items as items_mod
    try:
        reg = json.loads((Path(data_dir) / "registry.json").read_text(encoding="utf-8"))
        recs = dict(reg.get("tasks") or {})
    except (OSError, ValueError, AttributeError):
        recs = {}
    return migrate(data_dir, recs, items_mod.load(data_dir), projects.load(data_dir))
