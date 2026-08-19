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
import re
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
    """Paused runs count: their worktree still has the branch checked out, so
    handing it to a new run would move the branch under a session that is
    coming back to it."""
    held = [r for _s, _rid, r in all_active(reg)]
    for story in reg.data["stories"].values():
        held += list((story.get("paused_runs") or {}).values())
    return any(r.get("repo") == repo and r.get("branch") == branch for r in held)


# ------------------------------------------------------------------ worktrees

def add_worktree(checkout: Path, wt_path: Path, branch: str, start_ref: str):
    """Dedicated working tree on its own branch. -B resets the local branch to
    start_ref; the branch guard above ensures no other worktree holds it.

    Verified before returning: sessions have repeatedly been handed a directory
    with no checkout in it, and an agent that starts in an empty tree either
    rebuilds it by hand or builds the wrong thing entirely. `git worktree add`
    can report success and still leave nothing usable, so success is defined
    here as "the tree is actually there"."""
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "prune"], cwd=checkout, check=False, capture_output=True)
    sh(["git", "worktree", "add", "--force", "-B", branch, str(wt_path), start_ref], cwd=checkout)
    if not _worktree_ready(wt_path):
        # One retry: prune again in case stale metadata blocked the checkout.
        subprocess.run(["git", "worktree", "remove", "--force", str(wt_path)],
                       cwd=checkout, check=False, capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=checkout, check=False, capture_output=True)
        sh(["git", "worktree", "add", "--force", "-B", branch, str(wt_path), start_ref], cwd=checkout)
        if not _worktree_ready(wt_path):
            raise RuntimeError(
                f"worktree {wt_path} is empty after two attempts (branch {branch} "
                f"from {start_ref}) — refusing to start a session in it")


def _worktree_ready(wt_path: Path) -> bool:
    """Git's own view decides: a usable worktree has its gitdir pointer AND is
    recognized as a work tree from inside. Counting files would reject a valid
    checkout of an empty tree."""
    if not (wt_path / ".git").exists():
        return False
    p = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                       cwd=wt_path, capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "true"


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
          timeout, run_dir: Path, session_id: str | None = None,
          resume: bool = False) -> int:
    """Detached `claude -p` under a shell wrapper that writes stdout, stderr,
    and the exit code to files — the daemon can die and restart without losing
    the outcome. Returns the wrapper pid.

    session_id is chosen by the caller rather than read back afterwards: a run
    that dies (usage limit, reboot) never prints its id, and without it the
    session's transcript — the actual work — is unreachable. resume=True
    continues that session instead of starting a new one."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(prompt)
    argv = [claude_bin, "-p", prompt, "--model", model, "--effort", effort,
            "--output-format", "json"]
    if session_id:
        argv += ["--resume", session_id] if resume else ["--session-id", session_id]
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
    text = result_text or stdout
    record["rate_limited"] = rc != 0 and is_rate_limited(text)
    record["transient"] = rc != 0 and not record["rate_limited"] and is_transient(text)
    return rc == 0, result_text, usage, record


# "You've hit your session limit · resets 8pm (America/Chicago)" — the account
# ran out of inference, which is not the run failing. Treated as a pause so the
# session can be resumed rather than re-run from nothing.
_LIMIT_RE = re.compile(r"(session|usage|rate)\s+limit|limit\s*·\s*resets|too many requests",
                       re.IGNORECASE)
_RESET_RE = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)


def is_rate_limited(text: str) -> bool:
    return bool(text) and bool(_LIMIT_RE.search(text))


# A stage that died because the API was briefly unavailable has the same shape
# as one that ran out of inference: the work is fine, the world wasn't ready.
_TRANSIENT_RE = re.compile(
    r"\b(429|500|502|503|504|529)\b|overloaded|service unavailable|"
    r"internal server error|connection reset|timed? out reading|temporarily unavailable",
    re.IGNORECASE)


def is_transient(text: str) -> bool:
    return bool(text) and bool(_TRANSIENT_RE.search(text))


def retry_after(text: str, now: float | None = None) -> float:
    """Epoch seconds to wait until, parsed from the limit message's reset hour
    when it states one; otherwise a plain backoff. Always at least a minute out
    so a resumed run cannot hot-loop against a limit that is still in force."""
    now = time.time() if now is None else now
    m = _RESET_RE.search(text or "")
    if not m:
        return now + 900
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        hour += 12
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, int(m.group(2) or 0),
                          0, 0, 0, -1))
    if target <= now:
        target += 86400  # the stated hour already passed today — it means tomorrow
    return max(target, now + 60)
