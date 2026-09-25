"""The library index: every agent, skill and workflow installed on this machine,
found by scanning the folders they already live in.

Phase 1 of the Cadre library (design approved 2026-09-25): index only. Nothing
is copied or owned yet — every entry is `linked`, pointing at where it lives.
The launcher reads this index, so a person picks what to run from a list
instead of having to know a path.

Where it looks, first match wins per (kind, name):

    <dir>/.claude/{agents,skills,workflows}   each directory asked about
    ~/.claude/{agents,skills,workflows}       the user's own
    runner.skills_source                      the skills tree the runner links from

How each kind is launched — no flag, a prefix on the prompt (verified headless
with `claude -p`, 2026-09-25):

    skill     `/name <ask>`         the skill loads with the ask as its arguments
    workflow  `/name <ask>`         the session calls the workflow and WAITS for
                                    it; needs permissions bypassed, otherwise it
                                    is queued for approval and never runs
    agent     `@agent-name <ask>`   the session hands the ask to that agent.
                                    `/name` does NOT work for agents: the session
                                    answers that no such command exists

Cadre flows (config/actions-*.json) are listed too, as `flow`. They are not a
prefix: every launch today runs as the dialogue flow.
"""

import json
import re
from pathlib import Path

KINDS = ("agent", "skill", "workflow", "flow")
LAUNCHABLE = ("skill", "workflow", "agent")
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
NAME = re.compile(r"^[\w][\w.:-]{0,79}$")
_FRONT = re.compile(r"^---\s*\n(.*?)\n---", re.S)
_META_FIELD = r"""\b%s\s*:\s*(['"`])(.*?)(?<!\\)\1"""


def _front_matter(text: str) -> dict:
    m = _FRONT.match(text)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        if re.match(r"^[A-Za-z_][\w-]*\s*:", line):
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            out[key] = val.strip("'\"") if val not in ("|", ">", "") else ""
        elif key and line.startswith((" ", "\t")) and isinstance(out.get(key), str):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def _read(p: Path, limit: int = 8192) -> str:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _entry(kind, name, desc, path, scope, broken=False) -> dict:
    desc = " ".join(str(desc or "").split())
    return {"kind": kind, "name": name, "description": desc[:300], "path": str(path),
            "scope": scope, "origin": "linked", "broken": broken,
            "invoke": invocation(kind, name)}


def _agents(d: Path, scope: str) -> list[dict]:
    out = []
    for p in sorted(d.glob("*.md")):
        if not p.exists():
            out.append(_entry("agent", p.stem, "", p, scope, broken=True))
            continue
        fm = _front_matter(_read(p))
        out.append(_entry("agent", fm.get("name") or p.stem, fm.get("description"), p, scope))
    return out


def _skills(d: Path, scope: str) -> list[dict]:
    out = []
    for p in sorted(d.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_symlink() and not p.exists():
            out.append(_entry("skill", p.name, "", p, scope, broken=True))
            continue
        md = p / "SKILL.md"
        if not (p.is_dir() and md.is_file()):
            continue
        fm = _front_matter(_read(md))
        out.append(_entry("skill", fm.get("name") or p.name, fm.get("description"), p, scope))
    return out


def _workflows(d: Path, scope: str) -> list[dict]:
    out = []
    for p in sorted([*d.glob("*.js"), *d.glob("*.mjs"), *d.glob("*.ts")]):
        text = _read(p)
        name = re.search(_META_FIELD % "name", text)
        desc = re.search(_META_FIELD % "description", text)
        out.append(_entry("workflow", name.group(2) if name else p.stem,
                          desc.group(2) if desc else "", p, scope))
    return out


def _flows(config_dir: Path) -> list[dict]:
    out = []
    for p in sorted(config_dir.glob("actions-*.json")):
        if ".example" in p.name:
            continue
        try:
            comment = json.loads(p.read_text()).get("_comment") or []
        except (OSError, ValueError):
            comment = []
        first = comment[0] if isinstance(comment, list) and comment else str(comment or "")
        out.append(_entry("flow", p.stem.removeprefix("actions-"), first, p, "cadre"))
    return out


_SCANNERS = {"agents": _agents, "skills": _skills, "workflows": _workflows}


def roots(dirs=(), skills_source=None, home=None) -> list[tuple[str, Path, str]]:
    """(folder kind, path, scope) in search order."""
    home = Path(home) if home else Path.home()
    out = []
    for d in dirs or ():
        for sub in _SCANNERS:
            out.append((sub, Path(d) / ".claude" / sub, str(d)))
    for sub in _SCANNERS:
        out.append((sub, home / ".claude" / sub, "user"))
    if skills_source:
        out.append(("skills", Path(skills_source).expanduser(), "skills_source"))
    return out


def scan(dirs=(), skills_source=None, home=None, config_dir=None) -> list[dict]:
    """The index. A name found in several places is listed once, from the first
    place in search order, with the others in `also`."""
    seen: dict[tuple, dict] = {}
    for sub, path, scope in roots(dirs, skills_source, home):
        if not path.is_dir():
            continue
        for e in _SCANNERS[sub](path, scope):
            k = (e["kind"], e["name"])
            if k in seen:
                seen[k].setdefault("also", []).append(e["path"])
            else:
                seen[k] = e
    for e in _flows(Path(config_dir or CONFIG_DIR)):
        seen.setdefault((e["kind"], e["name"]), e)
    return sorted(seen.values(), key=lambda e: (KINDS.index(e["kind"]), e["name"].lower()))


def invocation(kind: str, name: str) -> str:
    """The prefix that launches it at the front of a `claude -p` prompt."""
    if kind in ("skill", "workflow"):
        return f"/{name}"
    if kind == "agent":
        return f"@agent-{name}"
    return ""


def parse_run(run: str) -> tuple[str, str]:
    """`kind:name` -> (kind, name). A bare `/name` or `@agent-name` is accepted
    too, as the prefix a person would type."""
    run = (run or "").strip()
    if run.startswith("@agent-"):
        kind, name = "agent", run[len("@agent-"):]
    elif run.startswith("/"):
        kind, name = "", run[1:]
    else:
        kind, _, name = run.partition(":")
    if kind and kind not in LAUNCHABLE:
        raise ValueError(f"cannot launch a {kind!r}: pick one of {', '.join(LAUNCHABLE)}")
    if not NAME.match(name or ""):
        raise ValueError(f"{run!r} is not kind:name (e.g. skill:investigating)")
    return kind, name


def resolve(entries: list[dict], run: str) -> dict:
    """The index entry `run` names, or ValueError saying what exists instead."""
    kind, name = parse_run(run)
    hits = [e for e in entries if e["name"] == name and e["kind"] in LAUNCHABLE
            and (not kind or e["kind"] == kind)]
    if not hits:
        raise ValueError(f"nothing installed is called {name!r}"
                         + (f" (kind {kind})" if kind else "")
                         + " — `pipeline.py library` lists what is")
    if len(hits) > 1:
        raise ValueError(f"{name!r} is both " + " and ".join(h["kind"] for h in hits)
                         + ": say which, e.g. skill:" + name)
    if hits[0]["broken"]:
        raise ValueError(f"{hits[0]['kind']} {name!r} is a broken link ({hits[0]['path']})")
    return hits[0]
