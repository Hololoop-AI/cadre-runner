"""Cadres: which cadre owns each run, and what a cadre's app reads.

A cadre is a group of work on the fleet's rail (Dev, Kitchen, Personal, …):
a live project that another live project names as its group, or a live
project with no folder (web/src/model/groups.ts `fleetGroups`, ported
here). Work run in a cadre is that cadre's: its runs show only in its app
(the fleet route /c/<id>). Holo, the fleet's home (/), is a cadre too:
it owns the runs that work on the cadres and the system themselves (HOLO),
and above its own runs it shows a card per cadre.

Ownership is fixed at launch and kept in `<data_dir>/cadres.json`:

    {"tasks": {task id: cadre id}, "also": {task id: [cadre id, …]}}

A run has one owner, and may also be shown in other cadres: a study session
is Learn's, and also shows in the cadre that asked for it (`also`, written
by `show_also`). Being shown is only being listed: costs and skills still
come from the owner alone (week_usd, skills_for).

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
from . import kanban, projects

FILE = "cadres.json"
LOCK = "cadres.lock"

# The rail's entry for work in no cadre; groups.ts calls it OTHER too.
OTHER = "~other"
OTHER_NAME = "Other"
# Holo (once called Hololoop), the fleet's home, is a cadre with runs of its own: the meta work
# on the cadres and the system. It is not a project, so it is fixed here.
HOLO = "~holo"
HOLO_NAME = "Holo"
SPECIAL = {OTHER: OTHER_NAME, HOLO: HOLO_NAME}

# What a cadre's header tabs show when its project record names no `nav`.
# Each is a screen of the fleet app under /c/<id>/; "home" is /c/<id> itself.
# The fleet-wide screens (runs, costs, the library) are not a cadre's: they
# stay on Holo until they can be cut to one cadre's runs.
DEFAULT_NAV = ("home", "blackboard", "records", "agents", "settings")
# Every cadre has a Blackboard (the events its agents write to each other,
# runnerlib/blackboard.py) and Records (its pages). Only the cadre that owns
# the kanban board (kanban.json's `board`, set per machine with
# `pipeline.py kanban own`) also gets the Kanban tab.
BOARD_NAV = ("home", "kanban", "blackboard", "records", "agents", "settings")
SCREENS = {
    "home": "Home",
    "kanban": "Kanban",
    "blackboard": "Blackboard",
    "records": "Records",
    "agents": "Agents",
    "settings": "Settings",
    "review": "Review",
}


# ------------------------------------------------------------- shipped cadres

# The cadres Cadre ships with, as whole cadres minus their folders
# (config/cadres.json). Holo is not one: it is built in (HOLO above).
DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "config" / "cadres.json"
# Which shipped cadres this machine has been given, so each arrives once.
SEEDED = "cadres-seeded.json"
# What a shipped cadre may carry: a project record's fields that are not
# this machine's (no folders, group, archived flag or context store path).
SHIPPED_FIELDS = ("name", "skills", "launch", "nav", "pinned", "agents", "owns")
# Learn, the study cadre, which ships owning the tutor node (its `owns`) and
# answers the study hand-off (runnerlib/learn.py).
LEARN = "learn"


def shipped(path=None) -> list[dict]:
    """The shipped cadres, in file order; [] if the file is missing or bad."""
    try:
        data = json.loads(Path(path or DEFAULTS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in data.get("cadres") or () if isinstance(c, dict) and c.get("id") and c.get("name")]


def seed_defaults(data_dir, path=None) -> list[str]:
    """Give this machine each shipped cadre it has never had, once, and each
    skill a shipped cadre lists that it has never been given, once.

    A cadre whose id is new here becomes a folderless project with the
    shipped config; one that already exists (made by hand, or seeded before)
    keeps its own edits, since the machine's own edits win, and only gains
    the shipped skills it has never been given, beside its own. `kanban`
    hands the kanban board to the cadre when no cadre owns it yet. Every
    shipped id, and every shipped skill per cadre, is then remembered in
    `cadres-seeded.json`, so archiving a default or removing one of its
    skills sticks, and a cadre or skill added to the file later still
    reaches an existing install. Returns the ids it created."""
    data_dir = Path(data_dir)
    seen_path = data_dir / SEEDED
    try:
        raw = json.loads(seen_path.read_text(encoding="utf-8"))
        seen = set(raw.get("seeded") or ())
        given = {k: list(v) for k, v in (raw.get("skills") or {}).items()}
    except (OSError, ValueError, AttributeError):
        seen, given = set(), {}
    projs = projects.load(data_dir)
    made, changed = [], False
    for c in shipped(path):
        new_skills = [s for s in c.get("skills") or () if s not in given.get(c["id"], ())]
        if c["id"] not in seen:
            if c["id"] not in projs:
                rec = projects.create(data_dir, c["name"])
                extra = {k: c[k] for k in SHIPPED_FIELDS if k in c and k != "name"}
                if extra:
                    projects.update(data_dir, rec["id"], **extra)
                made.append(rec["id"])
                new_skills = []                 # it arrived with them
            if c.get("kanban") and not kanban.load(data_dir).get("board"):
                kanban.own(data_dir, c["id"])
            seen.add(c["id"])
            changed = True
        if new_skills:
            rec = projects.load(data_dir).get(c["id"])
            if rec and not rec.get("archived"):
                have = list(rec.get("skills") or [])
                projects.update(data_dir, c["id"], skills=have + [s for s in new_skills if s not in have])
        remembered = list(dict.fromkeys([*given.get(c["id"], []), *(c.get("skills") or [])]))
        if remembered != given.get(c["id"], []):
            given[c["id"]] = remembered
            changed = True
    if changed:
        data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".cadres-seeded-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"seeded": sorted(seen), "skills": given}, f, indent=2, sort_keys=True)
        os.replace(tmp, seen_path)
    return made


# --------------------------------------------------------------------- which cadres


def cadre_list(projs: dict) -> list[dict]:
    """[{id, name}] of the cadres, in project order: live projects some live
    project belongs to, or with no folder (groups.ts fleetGroups)."""
    live = projects.live(projs)
    named = {p.get("group") for p in live if p.get("group")}
    return [{"id": p["id"], "name": p["name"]} for p in live
            if p["id"] in named or (isinstance(p.get("dirs"), list) and not p["dirs"])]


def owner_of_node(projs: dict, node: str) -> str | None:
    """The live cadre whose record says it owns every run on `node` (its
    `owns`, e.g. Learn owns `tutor`), or None."""
    for c in cadre_list(projs):
        if node in ((projs.get(c["id"]) or {}).get("owns") or ()):
            return c["id"]
    return None


def known(projs: dict, cadre: str) -> bool:
    return cadre in SPECIAL or any(c["id"] == cadre for c in cadre_list(projs))


def resolve(projs: dict, ref) -> str | None:
    """A cadre's id from its id or name (any case); OTHER for "other"."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref.lower() in (OTHER, "other"):
        return OTHER
    if ref.lower() in (HOLO, "~hololoop", "hololoop", "holo"):
        return HOLO
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


def _raw(data_dir) -> dict:
    try:
        raw = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load(data_dir) -> dict:
    """{task id: cadre id}; a missing or malformed file is empty."""
    tasks = _raw(data_dir).get("tasks")
    if not isinstance(tasks, dict):
        return {}
    return {str(k): str(v) for k, v in tasks.items() if isinstance(v, str) and v}


def load_also(data_dir) -> dict:
    """{task id: [cadre id, …]}: the cadres a run is shown in beside its owner."""
    also = _raw(data_dir).get("also")
    if not isinstance(also, dict):
        return {}
    return {str(k): [str(c) for c in v if isinstance(c, str) and c]
            for k, v in also.items() if isinstance(v, list)}


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


def _save(data_dir, tasks: dict, also: dict | None = None) -> None:
    d = Path(data_dir)
    also = load_also(d) if also is None else also
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cadres-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks, **({"also": also} if also else {}), "updated": time.time()},
                  f, indent=1, sort_keys=True)
    os.replace(tmp, _path(d))


def show_also(data_dir, task_id: str, cadre: str) -> None:
    """Show `task_id` in `cadre` as well as in its owner (the cadre that asked
    for a study session). Showing it in its own owner records nothing."""
    if not cadre:
        return
    with _locked(data_dir):
        tasks, also = load(data_dir), load_also(data_dir)
        if tasks.get(str(task_id)) == cadre or cadre in also.get(str(task_id), []):
            return
        also.setdefault(str(task_id), []).append(str(cadre))
        _save(data_dir, tasks, also)


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
            raise ValueError(f"no cadre {cadre!r} (cadres: {names}, holo or other)")
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
    if cadre in SPECIAL:
        return [cadre]
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


def skills_for(data_dir, projs: dict, task: str, project="") -> list[str]:
    """The skills a run is given at launch: its project's own, then those of
    every live cadre it is in (its owner and the cadres holding that one),
    each once. They reach the run as its own Claude Code plugin
    (runnerlib/skillstore.py build_plugin), so a cadre's skills reach its own
    runs rather than every session on the machine, and nothing is written
    into the folder the work runs in."""
    names = list((projs.get(project) or {}).get("skills") or [])
    for cadre in chain(projs, owner(load(data_dir), projs, task, project)):
        rec = projs.get(cadre) or {}
        if not rec.get("archived"):             # an archived cadre hands out nothing
            names += rec.get("skills") or []
    return list(dict.fromkeys(n for n in names if n))


def payload_for(payload: dict, cadre: str) -> dict:
    """The fleet payload (statusd.fleet_payload) with only `cadre`'s runs:
    its pages, its page-less launches, its running agents, its spend and its
    recent turns. A page with no task is filed by the group rule, with its
    item's tags. Everything that is not a run (projects, the tree, the code
    stamp) is left as it is."""
    tasks = payload.get("cadres") or {}
    also = payload.get("cadres_also") or {}
    projs = payload.get("projs") or {}
    store = payload.get("items") or {}

    def mine_task(t) -> bool:
        t = str(t or "")
        return any(cadre in chain(projs, c) for c in [owner(tasks, projs, t), *also.get(t, ())])

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
    # The cadre's week is its own runs' spend: a run only shown here (asked
    # for here, owned by Learn) counts in its owner's week, not this one's.
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


def name_of(projs: dict, cadre: str) -> str:
    """A cadre's display name: Other's and Holo's own, else its record's, else its id."""
    return SPECIAL.get(cadre) or (projs.get(cadre) or {}).get("name") or cadre


def nav(projs: dict, cadre: str, *, board: str = "") -> dict:
    """What the shell draws for `cadre` (GET /api/nav?cadre=<id>): its rail
    entry, its screens as header tabs (from the project record's `nav`, else
    BOARD_NAV for the cadre owning the kanban board `board`, else
    DEFAULT_NAV; Settings is always there), its second column — batches
    (none until the board has them) and pinned pages (the record's
    `pinned`: [{label, href}]) — and what its Settings tab shows."""
    rec = projs.get(cadre) or {}
    name = name_of(projs, cadre)
    base = "/c/" + {OTHER: "other", HOLO: "holo"}.get(cadre, cadre)
    home = "/" if cadre == HOLO else base
    default = BOARD_NAV if board and cadre == board else DEFAULT_NAV
    wanted = [s for s in (rec.get("nav") or default) if s in SCREENS]
    if "settings" not in wanted:
        wanted.append("settings")
    screens = [{"id": s, "label": SCREENS[s], "href": home if s == "home" else f"{base}/{s}"}
               for s in wanted]
    pinned = [{"label": str(p.get("label") or p.get("href")), "href": str(p["href"])}
              for p in rec.get("pinned") or () if isinstance(p, dict) and p.get("href")]
    return {"cadre": {"id": cadre, "name": name}, "screens": screens,
            "batches": [], "pinned": pinned,
            "agents": [{k: str(a.get(k) or "") for k in ("id", "name", "skill", "does")}
                       for a in rec.get("agents") or () if isinstance(a, dict) and a.get("name")],
            "cadres": [*cadre_list(projs), {"id": OTHER, "name": OTHER_NAME}],
            "settings": settings(projs, cadre)}


def settings(projs: dict, cadre: str) -> dict:
    """What the Settings tab shows: the cadre's own setup, read from its
    project record — the folders and projects whose runs it gets, the tabs
    it asked for (`nav`, empty for the default) — not its work."""
    if cadre in SPECIAL:
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
