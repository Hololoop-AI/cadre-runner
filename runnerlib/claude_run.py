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
    "auto-spec", "auto-interrogate",
    "engineering", "slicing", "interrogate", "investigating",
    "test-planning", "auto-test-planning",
    "test-writer", "auto-test-writer",
    "build", "auto-build", "tdd", "refactor", "code-review",
    "coding-standards", "git-ops", "commit-and-pr",
    "python-quality", "rust-quality", "pr-walkthrough",
    "verification", "systematic-debugging",
]


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


def install_skills(checkout: Path, skills_source: Path, exclude_from_git=True):
    dest = checkout / ".claude" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    linked, missing = [], []
    for name in PIPELINE_SKILLS:
        src = skills_source / name
        if not src.is_dir():
            missing.append(name)
            continue
        link = dest / name
        # Re-link dangling symlinks: the source tree can move out from under
        # us (branch switch, worktree removal) and a stale link silently
        # deprives every stage session of that skill.
        if link.is_symlink() and not link.exists():
            link.unlink()
        elif link.is_symlink() or link.exists():
            continue
        link.symlink_to(src)
        linked.append(name)
    if missing:
        raise SystemExit(
            f"skills missing from {skills_source}: {', '.join(missing)}\n"
            "Stage sessions depend on these; refusing to install a half-wired "
            "checkout. Check that skills_source points at a tree containing them."
        )
    if exclude_from_git:
        exclude = checkout / ".git" / "info" / "exclude"
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
