"""Skill linking for dialogue turns, and the prompt-variable checks."""

import os
import subprocess
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Skills a task dialogue's session invokes by name (prompts/task.md says
# "invoke the auto-surface skill"). Dialogues run in whatever directory the ask
# names, so a page written without this skill drops the whole authoring
# contract (the driver reported a round that lost the decided layer this way).
TASK_SKILLS = ["auto-surface"]

# Skills that ship with Cadre itself. Found here first, so a machine gets them
# from this checkout rather than only if it also cloned someone's personal
# skills tree.
REPO_SKILLS = Path(__file__).resolve().parent.parent / ".agents" / "skills"


def install_task_skills(cwd, skills_source, extra=()) -> tuple[list[str], list[str]]:
    """Best-effort symlink of TASK_SKILLS, plus `extra` (a project's default
    skills, the skill a launch picked), into `<cwd>/.claude/skills`. Returns
    (linked, missing): `missing` is every TASK_SKILLS entry found neither in
    REPO_SKILLS nor in `skills_source`, for the caller to say so.

    A link that points anywhere other than where the skill resolves now is
    re-pointed, so a directory linked to an older copy picks up the new one.
    In a git repo each link is added to `.git/info/exclude`: it is an absolute
    path into this machine's home folder and must never be committed.

    This never raises and never touches git config — the cwd is the driver's
    own directory."""
    linked, missing = [], []
    for name in dict.fromkeys([*TASK_SKILLS, *extra]):
        src = next((d / name for d in (REPO_SKILLS, Path(skills_source).expanduser())
                    if (d / name).is_dir()), None)
        if src is None:
            if name in TASK_SKILLS:
                missing.append(name)
            continue
        dest = Path(cwd) / ".claude" / "skills"
        dest.mkdir(parents=True, exist_ok=True)
        link = dest / name
        if link.is_symlink():
            if os.readlink(link) == str(src):
                continue
            link.unlink()          # moved, or an older copy; re-link below
        elif link.exists():
            continue               # a real directory someone put there
        link.symlink_to(src)
        linked.append(name)
        _exclude_from_git(Path(cwd), link)
    return linked, missing


def _exclude_from_git(cwd: Path, link: Path) -> None:
    """Add `link` to its repo's `.git/info/exclude`; nothing when `cwd` is not
    in a git repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel",
             "--git-common-dir"], cwd=cwd, capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return
        top, common = out.stdout.strip().splitlines()[:2]
        # the link's directory resolved, never the link: that is its target
        line = (link.parent.resolve() / link.name).relative_to(
            Path(top).resolve()).as_posix()
        exclude = Path(common) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        content = exclude.read_text() if exclude.exists() else ""
        if line not in content.splitlines():
            exclude.write_text(content.rstrip("\n") + ("\n" if content else "") + line + "\n")
    except Exception:
        pass


# Names prompts used before a rename. A prompt version recorded before the
# rename still asks for them, and until someone promotes the new one the runner
# fills them under both names (engine_seam) — or says so when it cannot.
PROMPT_ALIASES = ("routes", "route_options")


def known_prompt_vars(prompts_dir=None) -> frozenset:
    """Every lowercase `$name` the shipped prompts ask for, plus the old aliases.

    Only these count as unfilled. A prompt also carries shell environment
    (`$CADRE_SURFACE_OUT`) that `safe_substitute` is right to leave alone, so
    "any `$` left over" would cry wolf on every spawn."""
    d = Path(prompts_dir or PROMPTS_DIR)
    names = set(PROMPT_ALIASES)
    for f in d.glob("*.md"):
        names |= {n for n in _identifiers(f.read_text()) if n.islower()}
    return frozenset(names)


def _identifiers(template: str) -> set:
    return {m.group("named") or m.group("braced")
            for m in Template.pattern.finditer(template)
            if m.group("named") or m.group("braced")}


def unfilled(template: str, variables: dict, known=None) -> list[str]:
    """The known prompt variables `template` asks for that `variables` does
    not supply — each one reaches the session as a literal `$name`.

    Read from the template, not the rendered text, so a driver's note that
    happens to quote `$feedback` is not mistaken for a hole."""
    known = known_prompt_vars() if known is None else known
    return sorted(n for n in _identifiers(template)
                  if n in known and n not in variables)


