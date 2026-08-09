"""Async stage runs: every stage session is a detached process in its own git
worktree. The daemon spawns and keeps polling — it never blocks on a run — and
reaps completions on later passes. Per-story `active_runs` in the registry is
the source of truth and survives daemon restarts: runs are session-detached,
and their exit status lands in a file (never a waitpid the daemon could miss).

Concurrency safety rests on three guards, all enforced at spawn:
- one active run per (stage, slice, pr) target per story — duplicates drop;
- one active run per git branch per repo — two sessions must never share a
  writable branch (worktrees make the working trees disjoint; this guard makes
  the branches disjoint);
- a global cap (`runner.max_concurrent_runs`) — beyond it, RunsBusy requeues
  the event as backpressure, explicitly WITHOUT an attempt bump (a crowded
  machine must never dead-letter a legitimate event).
"""

import json
import os
import subprocess
import time
from pathlib import Path

from .claude_run import sh


class RunsBusy(Exception):
    """Cap reached or branch held — backpressure, not failure."""


# ------------------------------------------------------------------ inventory

def all_active(reg):
    """[(slug, run_id, run)] across every story, any status."""
    out = []
    for slug, story in reg.data["stories"].items():
        for rid, r in (story.get("active_runs") or {}).items():
            out.append((slug, rid, r))
    return out


def key_active(story, stage, slice_name, pr):
    return any((r["stage"], r.get("slice"), r.get("pr")) == (stage, slice_name, pr)
               for r in (story.get("active_runs") or {}).values())


def branch_held(reg, repo, branch):
    return any(r.get("repo") == repo and r.get("branch") == branch
               for _s, _rid, r in all_active(reg))


# ------------------------------------------------------------------ worktrees

def add_worktree(checkout: Path, wt_path: Path, branch: str, start_ref: str):
    """Dedicated working tree on its own branch. -B resets the local branch to
    start_ref; the branch guard above ensures no other worktree holds it."""
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "prune"], cwd=checkout, check=False, capture_output=True)
    sh(["git", "worktree", "add", "--force", "-B", branch, str(wt_path), start_ref], cwd=checkout)


def remove_worktree(checkout: Path, wt_path: Path) -> tuple[bool, str]:
    """(removed?, detail). Never raises — the caller decides how loudly a
    surviving worktree gets reported (silent failure hid a leak for hours)."""
    detail = ""
    try:
        p = subprocess.run(["git", "worktree", "remove", "--force", str(wt_path)],
                           cwd=checkout, capture_output=True, text=True)
        detail = (p.stderr or "").strip()[:200]
    except Exception as e:
        detail = f"git worktree remove raised: {e}"
    if wt_path.exists():
        try:
            p2 = subprocess.run(["rm", "-rf", str(wt_path)], capture_output=True, text=True)
            if wt_path.exists():
                return False, f"{detail} | rm -rf: {(p2.stderr or '').strip()[:200]}"
        except Exception as e:
            return False, f"{detail} | rm raised: {e}"
    subprocess.run(["git", "worktree", "prune"], cwd=checkout, check=False, capture_output=True)
    return True, detail


# ------------------------------------------------------------------ processes

def spawn(claude_bin, prompt, wt_path, model, effort, permission_mode,
          timeout, run_dir: Path) -> int:
    """Detached `claude -p` under a shell wrapper that writes stdout, stderr,
    and the exit code to files — the daemon can die and restart without losing
    the outcome. Returns the wrapper pid."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(prompt)
    argv = [claude_bin, "-p", prompt, "--model", model, "--effort", effort,
            "--output-format", "json"]
    if permission_mode == "bypass":
        argv.append("--dangerously-skip-permissions")
    else:
        argv += ["--permission-mode", permission_mode]
    argv = ["timeout", str(int(timeout))] + argv
    env = {**os.environ,
           "CADRE_RUN_OUT": str(run_dir / "out.json"),
           "CADRE_RUN_ERR": str(run_dir / "err.txt"),
           "CADRE_RUN_EXIT": str(run_dir / "exit")}
    proc = subprocess.Popen(
        ["/bin/sh", "-c",
         '"$@" > "$CADRE_RUN_OUT" 2> "$CADRE_RUN_ERR"; echo $? > "$CADRE_RUN_EXIT"',
         "sh"] + argv,
        cwd=wt_path, env=env, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def finished(run: dict) -> bool:
    if Path(run["run_dir"], "exit").exists():
        return True
    try:
        os.kill(run["pid"], 0)
        return False
    except (ProcessLookupError, PermissionError):
        return True  # dead with no exit file — host reboot / OOM killed the wrapper


def outcome(run: dict):
    """(ok, result_text, usage, record) — same parse as the old sync runner,
    read back from the run's files."""
    run_dir = Path(run["run_dir"])
    exit_f = run_dir / "exit"
    if exit_f.exists():
        try:
            rc = int(exit_f.read_text().strip() or "1")
        except ValueError:
            rc = 1
    else:
        rc = -1  # process gone, no exit file — the run was lost
    stdout = (run_dir / "out.json").read_text() if (run_dir / "out.json").exists() else ""
    record = {
        "cwd": run.get("worktree"), "model": run.get("model"),
        "seconds": round(time.time() - run.get("started", time.time()), 1),
        "timed_out": rc == 124,
        "lost": rc == -1,
        "returncode": rc,
        "stderr": (run_dir / "err.txt").read_text()[-4000:] if (run_dir / "err.txt").exists() else "",
        "prompt_file": str(run_dir / "prompt.txt"),
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
    return rc == 0, result_text, usage, record
