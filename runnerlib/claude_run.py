"""Checkout management and headless `claude -p` invocation."""

import json
import subprocess
import time
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Skills the pipeline's sessions rely on; `install` links these into the target
# repo's .claude/skills (interrogate and auto-* exist only in the skills repo, not
# in ~/.claude/skills, so without this step sessions can't load them).
PIPELINE_SKILLS = [
    "auto-spec", "auto-interrogate", "auto-surface",
    "engineering", "slicing", "interrogate", "investigating",
    "test-planning", "auto-test-planning",
    "test-writer", "auto-test-writer",
    "build", "auto-build", "tdd", "refactor", "spec-review",
    "coding-standards", "git-ops", "commit-and-pr",
    "python-quality", "rust-quality", "pr-walkthrough",
    "verification", "systematic-debugging",
]


# Skills a task dialogue's session invokes by name (prompts/task.md says
# "invoke the auto-surface skill"). Dialogues run in whatever directory the ask
# names — none of the checkout preparation above happens for them, so a page
# written without this skill drops the whole authoring contract (the driver
# reported a round that lost the decided layer this way).
TASK_SKILLS = ["auto-surface"]


def install_task_skills(cwd, skills_source, extra=()) -> list[str]:
    """Best-effort symlink of TASK_SKILLS, plus `extra` (a project's default
    skills, the skill a launch picked), into `<cwd>/.claude/skills`.

    Unlike `install_skills` this never raises and never touches git config —
    the cwd is the driver's own directory, not a checkout the runner owns."""
    linked = []
    for name in dict.fromkeys([*TASK_SKILLS, *extra]):
        src = Path(skills_source).expanduser() / name
        if not src.is_dir():
            continue
        dest = Path(cwd) / ".claude" / "skills"
        dest.mkdir(parents=True, exist_ok=True)
        link = dest / name
        if link.is_symlink() and not link.exists():
            link.unlink()          # source tree moved; re-link below
        elif link.is_symlink() or link.exists():
            continue
        link.symlink_to(src)
        linked.append(name)
    return linked


def sh(args, cwd=None, check=True, capture=True):
    return subprocess.run(args, cwd=cwd, check=check, text=True,
                          capture_output=capture)


def ensure_checkout(repo: str, checkout: Path, default_branch: str, identity: dict | None = None):
    if not (checkout / ".git").is_dir():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        sh(["gh", "repo", "clone", repo, str(checkout)])
        sh(["gh", "auth", "setup-git"], cwd=checkout)
    if identity and identity.get("name"):
        # Commit author must match the acting account (gh auth controls push,
        # not authorship) — otherwise agent commits render as whoever's git
        # config leaks into the checkout.
        sh(["git", "config", "user.name", identity["name"]], cwd=checkout)
        sh(["git", "config", "user.email", identity["email"]], cwd=checkout)
    sh(["git", "fetch", "origin", "--prune"], cwd=checkout)


def install_skills(checkout: Path, skills_source: Path, exclude_from_git=True,
                   extra_sources: tuple = (), required: bool = True, log=None):
    """Link PIPELINE_SKILLS from skills_source plus every skill found in
    extra_sources (e.g. cadre-workflows/skills — community/registry skills,
    linked opportunistically).

    `required` is `runner.require_skills`. True (the deployment setting) refuses
    a half-wired checkout, because a stage session missing a skill fails hours
    later and obscurely. A fresh machine that has not cloned the skills tree yet
    sets it false and gets a warning, so the runner can be brought up in the
    order the operator chooses.
    """
    dest = checkout / ".claude" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    linked, missing = [], []

    def _link(src: Path):
        link = dest / src.name
        # Re-link dangling symlinks: the source tree can move out from under
        # us (branch switch, worktree removal) and a stale link silently
        # deprives every stage session of that skill.
        if link.is_symlink() and not link.exists():
            link.unlink()
        elif link.is_symlink() or link.exists():
            return False
        link.symlink_to(src)
        linked.append(src.name)
        return True

    for name in PIPELINE_SKILLS:
        src = skills_source / name
        if not src.is_dir():
            missing.append(name)
            continue
        _link(src)
    for extra in extra_sources:
        extra = Path(extra)
        if not extra.is_dir():
            continue
        for src in sorted(extra.iterdir()):
            if src.is_dir() and (src / "SKILL.md").exists():
                _link(src)
    if missing:
        detail = (f"skills missing from {skills_source}: {', '.join(missing)}\n"
                  "Stage sessions depend on these. Point `runner.skills_source` "
                  "in config.toml at a tree that contains them, or set "
                  "`runner.require_skills = false` to run without them.")
        if required:
            raise SystemExit(detail + "\nRefusing to install a half-wired checkout.")
        (log or print)(f"skills: {detail}")
    if exclude_from_git:
        # .git is a FILE in a worktree (gitdir pointer) — resolve the shared
        # common dir instead of assuming a directory layout.
        common = sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    cwd=checkout).stdout.strip()
        exclude = Path(common) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        line = ".claude/"
        content = exclude.read_text() if exclude.exists() else ""
        if line not in content.splitlines():
            exclude.write_text(content.rstrip("\n") + f"\n{line}\n")
    return linked, missing


def prepare(checkout: Path, base_branch: str):
    """Put the checkout on a clean local copy of origin/<base_branch>.
    Sessions create their own stage branches from there."""
    sh(["git", "fetch", "origin", "--prune"], cwd=checkout)
    sh(["git", "checkout", "-B", base_branch, f"origin/{base_branch}"], cwd=checkout)
    # -fd (not -fdx): respects .git/info/exclude, so skill symlinks survive
    sh(["git", "clean", "-fd"], cwd=checkout)


def render(template_name: str, variables: dict) -> str:
    text = (PROMPTS_DIR / f"{template_name}.md").read_text()
    common = (PROMPTS_DIR / "_common.md").read_text()
    return Template(common + "\n\n" + text).safe_substitute(variables)


def run_claude(prompt: str, cwd: Path, model: str, effort: str,
               permission_mode: str, timeout: int, log_path: Path, claude_bin="claude"):
    argv = [claude_bin, "-p", prompt, "--model", model, "--effort", effort,
            "--output-format", "json"]
    if permission_mode == "bypass":
        argv.append("--dangerously-skip-permissions")
    else:
        argv += ["--permission-mode", permission_mode]

    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired as e:
        proc, timed_out = e, True

    stdout = proc.stdout or ""
    record = {
        "argv": argv[:1] + ["<prompt omitted>"] + argv[3:],
        "cwd": str(cwd),
        "model": model,
        "seconds": round(time.time() - started, 1),
        "timed_out": timed_out,
        "returncode": getattr(proc, "returncode", None),
        "stderr": (proc.stderr or "")[-4000:],
        "prompt": prompt,
    }
    result_text, usage = "", None
    if stdout:
        try:
            payload = json.loads(stdout)
            result_text = payload.get("result", "")
            usage = {k: payload.get(k) for k in
                     ("total_cost_usd", "usage", "num_turns", "duration_ms") if k in payload}
            record["result"] = result_text
            record["usage"] = usage
        except json.JSONDecodeError:
            record["raw_stdout"] = stdout[-8000:]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(record, indent=2))

    ok = not timed_out and getattr(proc, "returncode", 1) == 0
    return ok, result_text, usage
