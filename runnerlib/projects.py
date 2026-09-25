"""Projects: durable records you create first and launch work from.

The fleet used to group pages by a project STRING, so a project existed only
while one of its pages was open — rule on the last card and the section
vanished. A project here is a record the runner stores in
`<data_dir>/projects.json` and keeps until someone archives it, whether or not
anything runs in it (design approved 2026-09-25, "projects as durable objects
you launch work from").

A record:

    id        slug of the name; the key everything else uses
    name      what the fleet shows, and the string pages file under
    dirs      absolute directories; the first is where launched work runs
    group     id of the project this one belongs to, or None. A group is just
              a project whose members name it — one kind of object, not two
    context   path to the project's context store (a brief plus dated
              decision files), or "" to inherit the group's
    skills    extra skills linked into the working directory at launch
    launch    the default launch choice, `kind:name` (library.parse_run), or ""
    archived  hidden from the fleet; kept on disk

Crossover: work launched in a member may READ its siblings' directories (they
reach the session as extra directories). "Read the siblings, write only here"
is said in the prompt block below; nothing else enforces it.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from .conversations import PROJECT_NAME
from .registry import slugify

FILE = "projects.json"
FIELDS = ("name", "dirs", "group", "context", "skills", "launch", "archived")
# What a turn reads as `$project` when it was not launched from a project.
NO_PROJECT = "None. This task was not launched from a project."
BRIEF_NAMES = ("brief.md", "README.md")
BRIEF_CHARS = 4000
DECISIONS_SHOWN = 40


class ProjectError(ValueError):
    pass


def _path(data_dir) -> Path:
    return Path(data_dir) / FILE


def load(data_dir) -> dict:
    """{id: record}. Missing or unreadable is no projects: the fleet and the
    page server must render either way."""
    try:
        data = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("projects", {}) if isinstance(data, dict) else {}


def _save(data_dir, projs: dict) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".projects-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"projects": projs}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def _dirs(dirs) -> list[str]:
    out = []
    for d in dirs or ():
        d = str(d).strip()
        if not d:
            continue
        p = Path(d).expanduser()
        if not p.is_absolute():
            raise ProjectError(f"directory {d!r} is not an absolute path")
        if not p.is_dir():
            raise ProjectError(f"directory {d} does not exist")
        s = str(p.resolve())
        if s not in out:
            out.append(s)
    return out


def _check_group(projs: dict, pid: str, group) -> str | None:
    if not group:
        return None
    gid = slugify(str(group)) if str(group) not in projs else str(group)
    if gid not in projs:
        raise ProjectError(f"no project {group!r} to belong to")
    node, seen = gid, set()
    while node and node not in seen:
        if node == pid:
            raise ProjectError("a project cannot belong to itself or to one of its members")
        seen.add(node)
        node = (projs.get(node) or {}).get("group")
    return gid


def create(data_dir, name: str, dirs=(), group=None, context: str = "",
           skills=(), launch: str = "") -> dict:
    name = (name or "").strip()
    if not PROJECT_NAME.match(name):
        raise ProjectError("a project name is letters, digits, space, . _ - (max 64)")
    projs = load(data_dir)
    pid = slugify(name)
    if pid in projs:
        raise ProjectError(f"project {pid!r} already exists")
    rec = {"id": pid, "name": name, "dirs": _dirs(dirs),
           "group": _check_group(projs, pid, group),
           "context": _context(context), "skills": [s for s in skills or () if s],
           "launch": (launch or "").strip(), "archived": False,
           "created": time.time()}
    projs[pid] = rec
    _save(data_dir, projs)
    return rec


def _context(context) -> str:
    context = (context or "").strip()
    if not context:
        return ""
    p = Path(context).expanduser()
    if not p.is_absolute():
        raise ProjectError(f"context store {context!r} is not an absolute path")
    return str(p)


def update(data_dir, pid: str, **fields) -> dict:
    projs = load(data_dir)
    rec = projs.get(pid)
    if rec is None:
        raise ProjectError(f"no project {pid!r}")
    for k, v in fields.items():
        if k not in FIELDS or v is None:
            continue
        if k == "dirs":
            v = _dirs(v)
        elif k == "group":
            v = _check_group(projs, pid, v)
        elif k == "context":
            v = _context(v)
        elif k == "name" and not PROJECT_NAME.match(str(v)):
            raise ProjectError("a project name is letters, digits, space, . _ - (max 64)")
        rec[k] = v
    _save(data_dir, projs)
    return rec


def archive(data_dir, pid: str, archived: bool = True) -> dict:
    return update(data_dir, pid, archived=bool(archived))


# --------------------------------------------------------------------- queries


def resolve(projs: dict, ref) -> dict | None:
    """A project by id or by name (the fleet's grouping string)."""
    if not ref:
        return None
    ref = str(ref)
    if ref in projs:
        return projs[ref]
    return next((p for p in projs.values() if p.get("name") == ref), None) \
        or projs.get(slugify(ref))


def live(projs: dict) -> list[dict]:
    return sorted((p for p in projs.values() if not p.get("archived")),
                  key=lambda p: p["name"].lower())


def members(projs: dict, pid: str) -> list[dict]:
    return sorted((p for p in projs.values() if p.get("group") == pid),
                  key=lambda p: p["name"].lower())


def names_of(projs: dict, pid: str) -> set[str]:
    """Every grouping string a page of this project may carry."""
    rec = projs.get(pid) or {}
    return {pid, str(rec.get("name") or pid)}


def for_dir(projs: dict, cwd) -> dict | None:
    """The live project owning a directory: the one with the deepest listed
    directory that contains it."""
    if not cwd:
        return None
    cwd = Path(str(cwd))
    best, depth = None, -1
    for p in live(projs):
        for d in p.get("dirs") or ():
            dp = Path(d)
            if (cwd == dp or dp in cwd.parents) and len(dp.parts) > depth:
                best, depth = p, len(dp.parts)
    return best


def workdir(projs: dict, pid: str) -> str:
    """Where launched work runs: the project's first directory, else (a group
    with none of its own) its first member's."""
    rec = projs.get(pid) or {}
    for p in [rec, *members(projs, pid)]:
        if p.get("dirs"):
            return p["dirs"][0]
    raise ProjectError(f"project {pid!r} has no directory to run work in")


def read_dirs(projs: dict, pid: str, cwd: str = "") -> list[str]:
    """The directories a session launched in this project may read besides its
    working directory: its own other directories, its group's and its
    siblings' when it is a member, and every member's when it is a group."""
    rec = projs.get(pid)
    if rec is None:
        return []
    related = [rec]
    if rec.get("group") and rec["group"] in projs:
        related += [projs[rec["group"]], *members(projs, rec["group"])]
    related += members(projs, pid)
    out = []
    for p in related:
        for d in p.get("dirs") or ():
            if d != cwd and d not in out and Path(d).is_dir():
                out.append(d)
    return out


def context_path(projs: dict, pid: str) -> str:
    rec = projs.get(pid) or {}
    if rec.get("context"):
        return rec["context"]
    group = projs.get(rec.get("group") or "")
    return (group or {}).get("context") or ""


def brief(context: str) -> tuple[str, str]:
    """(path, text) of the context store's brief, or ("", "")."""
    for name in BRIEF_NAMES:
        p = Path(context) / name
        try:
            return str(p), p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return "", ""


def decisions(context: str) -> list[str]:
    """Decision files, newest first (they are named YYYY-MM-DD-slug.md)."""
    d = Path(context) / "decisions"
    try:
        return sorted((str(p) for p in d.glob("*.md")), reverse=True)
    except OSError:
        return []


def prompt_block(projs: dict, pid: str | None, cwd: str = "") -> str:
    """What a turn reads as `$project`: which project it runs in, what else it
    may read, and where the project's recorded intent lives. Decisions are
    LISTED, not pasted — a long history must not flood every prompt."""
    rec = projs.get(pid or "")
    if rec is None:
        return NO_PROJECT
    lines = [f"**{rec['name']}** (`{rec['id']}`)"]
    group = projs.get(rec.get("group") or "")
    if group:
        lines.append(f"Part of the group **{group['name']}**.")
    mem = members(projs, rec["id"])
    if mem:
        lines.append("A group of: " + ", ".join(m["name"] for m in mem) + ".")
    reads = read_dirs(projs, rec["id"], cwd)
    if reads:
        lines.append("You may READ these directories as well as your working "
                     "directory; write only under your working directory unless "
                     "the ask says otherwise:")
        lines += [f"- `{d}`" for d in reads]
    ctx = context_path(projs, rec["id"])
    if not ctx:
        lines.append("No context store is recorded for this project.")
        return "\n".join(lines)
    lines.append(f"Context store: `{ctx}` — what the project is for and the "
                 "decisions already made. Read the decisions that bear on this "
                 "ask before acting; do not re-decide them.")
    path, text = brief(ctx)
    if text:
        cut = text[:BRIEF_CHARS]
        more = f"\n…(truncated — read `{path}` for the rest)" if len(text) > BRIEF_CHARS else ""
        lines.append(f"The brief (`{path}`):\n\n{cut.strip()}{more}")
    dec = decisions(ctx)
    if dec:
        lines.append(f"Recorded decisions, newest first ({len(dec)}):")
        lines += [f"- `{d}`" for d in dec[:DECISIONS_SHOWN]]
        if len(dec) > DECISIONS_SHOWN:
            lines.append(f"- …and {len(dec) - DECISIONS_SHOWN} older in `{ctx}/decisions`")
    return "\n".join(lines)
