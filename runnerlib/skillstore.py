"""Skills Cadre holds, and how each run gets the ones it needs.

A skill is a folder with a SKILL.md (Claude Code's format). Cadre finds a
skill by name in these places, first match wins:

1. **The machine's own store**, `<data_dir>/skills/<name>/`: skills added
   here with `pipeline.py skills add`, held by the app itself and owned by
   no repository. A copy here overrides every other place.
2. **The skills Cadre ships**, this checkout's `.agents/skills` (the page
   authoring skill, and Learn's five learning skills).
3. **Every configured source, in order**: `[runner] skills_sources`, a list
   of folders (a clone of a team's skills repository, a personal tree), then
   the older single `skills_source`. A machine can be hooked to any number.

How a run gets them: Claude Code loads a plugin for one session with
`--plugin-dir <folder>` (`claude --help`: "Load a plugin from a directory or
.zip for this session only"), and a plugin's `skills/` folder holds skills.
So each task gets its own plugin, `<data_dir>/plugins/<task>/cadre/`, whose
`skills/` links to the skills its cadre and project name, resolved as above,
and every turn, thread and chat of that task starts with `--plugin-dir`
pointing at it. Nothing is written into the folder the work runs in.

Skills from a plugin carry its name as a prefix when typed as a command
(`/cadre:learning`); an agent that is told to load a skill finds it by name
either way.
"""

import json
import re
import shutil
from pathlib import Path

REPO_SKILLS = Path(__file__).resolve().parent.parent / ".agents" / "skills"
# Skills every dialogue turn gets, whatever its cadre: prompts/task.md says
# "invoke the auto-surface skill", and a page written without it drops the
# whole authoring contract (the driver reported a round that lost the
# decided layer this way).
TASK_SKILLS = ["auto-surface"]
STORE = "skills"
PLUGINS = "plugins"
PLUGIN_NAME = "cadre"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class SkillError(ValueError):
    pass


def store_dir(cfg) -> Path:
    return Path(cfg.data_dir) / STORE


def sources(cfg) -> list[tuple[str, Path]]:
    """(label, folder) in the order a name is looked up."""
    runner = getattr(cfg, "runner", None) or {}
    out = [("store", store_dir(cfg)), ("cadre", REPO_SKILLS)]
    listed = runner.get("skills_sources") or []
    if isinstance(listed, str):
        listed = [listed]
    single = getattr(cfg, "skills_source", None) or runner.get("skills_source") or ""
    for p in [*listed, single]:
        if p:
            path = Path(str(p)).expanduser()
            if all(path != q for _, q in out):
                out.append(("source", path))
    return out


def paths(cfg) -> list[Path]:
    """The folders of `sources`, for the library's scan (what a launch can pick)."""
    return [p for _, p in sources(cfg)]


def resolve(cfg, name: str) -> tuple[str, Path] | None:
    for label, d in sources(cfg):
        if (d / name / "SKILL.md").is_file():
            return label, d / name
    return None


def listing(cfg) -> list[dict]:
    """Every skill Cadre can hand out: {name, where, path, shadowed}, by
    name. `shadowed` names the places a first match hides."""
    seen: dict = {}
    for label, d in sources(cfg):
        if not d.is_dir():
            continue
        for sk in sorted(p for p in d.iterdir() if (p / "SKILL.md").is_file()):
            if sk.name in seen:
                seen[sk.name]["shadowed"].append(str(d))
            else:
                seen[sk.name] = {"name": sk.name, "where": label, "path": str(sk), "shadowed": []}
    return [seen[k] for k in sorted(seen)]


def _frontmatter_name(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return ""
    got = re.search(r"^name:\s*(.+?)\s*$", m.group(1), re.M)
    return got.group(1).strip().strip("'\"") if got else ""


def add(cfg, folder, name: str = "") -> dict:
    """Copy a skill folder into the machine's store; replaces an earlier copy."""
    src = Path(folder).expanduser().resolve()
    if not (src / "SKILL.md").is_file():
        raise SkillError(f"{src} has no SKILL.md")
    name = name or _frontmatter_name(src / "SKILL.md") or src.name
    if not NAME.fullmatch(name):
        raise SkillError(f"a skill name is lowercase letters, digits and -, not {name!r}")
    dest = store_dir(cfg) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{name}.new")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, symlinks=False)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return {"name": name, "where": "store", "path": str(dest), "shadowed": []}


def remove(cfg, name: str) -> None:
    dest = store_dir(cfg) / name
    if not NAME.fullmatch(name or "") or not dest.is_dir():
        raise SkillError(f"no skill {name!r} in this machine's store")
    shutil.rmtree(dest)


def plugin_dir(cfg, task_id: str) -> Path:
    return Path(cfg.data_dir) / PLUGINS / task_id / PLUGIN_NAME


def build_plugin(cfg, task_id: str, names) -> tuple[Path, list[str], list[str]]:
    """Make (or refresh) the task's plugin with these skills; returns
    (plugin folder, linked, missing). A skill no place holds is left out and
    reported; one already linked to where it resolves now is left alone."""
    root = plugin_dir(cfg, task_id)
    skills = root / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    manifest = root / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": PLUGIN_NAME, "version": "1.0.0",
                                        "description": "The skills Cadre gives this run"}, indent=2))
    # a name is a folder name, never a path: a crafted `../x` stays out
    wanted = list(dict.fromkeys(n for n in names if n and NAME.fullmatch(n)))
    linked, missing = [], []
    for name in wanted:
        got = resolve(cfg, name)
        link = skills / name
        if got is None:
            missing.append(name)
            continue
        target = got[1]
        if link.is_symlink():
            if Path(link.readlink()) == target:
                continue
            link.unlink()
        elif link.exists():
            shutil.rmtree(link)
        link.symlink_to(target)
        linked.append(name)
    # a skill the run no longer gets (its cadre dropped it) leaves the plugin
    for old in skills.iterdir():
        if old.name not in wanted and old.is_symlink():
            old.unlink()
    return root, linked, missing


def plugin_args(cfg, task_id: str) -> list[str]:
    """The task's plugin for a process that is not a turn (a thread, a
    chat): the one its turns built, when there is one."""
    root = plugin_dir(cfg, task_id)
    return [str(root)] if (root / ".claude-plugin" / "plugin.json").is_file() else []
