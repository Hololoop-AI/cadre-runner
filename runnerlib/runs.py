"""Detached agent runs: every turn is a process the daemon spawns and keeps
polling — it never blocks on one — and reaps on a later pass. The task's
`active_runs` in the registry is the source of truth and survives daemon
restarts: runs are session-detached, and their exit status lands in a file
(never a waitpid the daemon could miss).

A global cap (`runner.max_concurrent_runs`) bounds them; beyond it a spawn
raises RunsBusy and the turn waits for a slot (engine_seam.run_deferred).
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


class RunsBusy(Exception):
    """Cap reached — backpressure, not failure."""


# Feeds a session its messages and posts its thinking to the page
# server (runnerlib/session_pump.py).
SESSION_PUMP = Path(__file__).resolve().parent / "session_pump.py"
INBOX = "in.jsonl"


def stdin_fed(argv: list[str]) -> bool:
    """The command reads its messages as stream-json on stdin."""
    return any(a == "--input-format" and b == "stream-json"
               for a, b in zip(argv, argv[1:]))


def user_message(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def push(run: dict, text: str, reply_url: str = "", **fields) -> bool:
    """Give a running session one more message, read at its next tool call.
    `fields` ride along on the inbox line for whoever reads it back (`inbox`).
    False when the run does not read stdin or has already stopped reading:
    the caller keeps the message for a turn of its own."""
    inbox = Path(run.get("run_dir") or "/nonexistent") / INBOX
    if not inbox.exists() or Path(str(inbox) + ".closed").exists():
        return False
    with open(inbox, "a") as f:
        f.write(json.dumps({"message": user_message(text), "text": text,
                            "reply_url": reply_url, **fields}) + "\n")
    return True


def inbox(run: dict) -> list[dict]:
    """Every line a run's inbox holds: its prompt first, then what was pushed."""
    try:
        text = (Path(run.get("run_dir") or "/nonexistent") / INBOX).read_text()
    except OSError:
        return []
    return [json.loads(l) for l in text.splitlines() if l.strip()]


# ------------------------------------------------------------------ processes

def session_args(session_id: str | None, resume: bool) -> list[str]:
    return ([] if not session_id else
            ["--resume", session_id] if resume else ["--session-id", session_id])


def dir_args(add_dirs) -> list[str]:
    """Extra directories a session may read — a project's siblings. Last on
    the command line: `--add-dir` takes every argument after it."""
    return ["--add-dir", *add_dirs] if add_dirs else []


def permission_args(permission_mode: str) -> list[str]:
    return (["--dangerously-skip-permissions"] if permission_mode == "bypass"
            else ["--permission-mode", permission_mode])


def expand_spawn_argv(argv: list[str], values: dict) -> list[str]:
    """Fill a node command's spawn-time placeholders (`nodes.SPAWN_VARS`).

    A value may be a string, substituted inside the token, or a LIST, in which
    case a token that is exactly `{name}` becomes those arguments — zero of them
    when there are none. Flag pairs (`--resume <id>`) and flags that sometimes
    do not exist at all are the reason the list form is needed; a string could
    only ever produce exactly one argument.
    """
    out = []
    for tok in argv:
        name = tok[1:-1] if tok.startswith("{") and tok.endswith("}") else None
        if name is not None and isinstance(values.get(name), list):
            out.extend(values[name])
            continue
        for key, val in values.items():
            if isinstance(val, str):
                tok = tok.replace("{%s}" % key, val)
        out.append(tok)
    return out


def spawn(claude_bin, prompt, wt_path, model, effort, permission_mode,
          timeout, run_dir: Path, session_id: str | None = None,
          resume: bool = False, extra_env: dict | None = None,
          argv: list[str] | None = None, add_dirs=None,
          thinking_url: str | None = None) -> int:
    """Detached agent session under a shell wrapper that writes stdout, stderr,
    and the exit code to files — the daemon can die and restart without losing
    the outcome. Returns the wrapper pid.

    `argv` is the node registry's command, already formatted by
    `nodes.command_argv`. When it is given it IS the command — the claim "swap
    the agent CLI by editing the node's command" is only true if nothing here
    second-guesses it, so this function adds the run-time values the template
    asked for and nothing else. Without it (the legacy path: no engine, revived
    sessions from before nodes carried the command) the Claude Code flags below
    are composed as they always were.

    session_id is chosen by the caller rather than read back afterwards: a run
    that dies (usage limit, reboot) never prints its id, and without it the
    session's transcript — the actual work — is unreachable. resume=True
    continues that session instead of starting a new one.

    `timeout` is not enforced here. The caller records `deadline(timeout)` on
    the run and the reap pass enforces it (`enforce_deadline`): the GNU
    `timeout` wrapper this used to prepend does not exist on macOS, where it
    made every turn exit 127.

    A command that reads stream-json on stdin (`--input-format stream-json`,
    the seeded node command) gets its prompt as the first message of the run's
    inbox instead of as an argument, and `push` can add more while it runs
    (runnerlib/session_pump.py). `thinking_url` is a page server's
    `/api/<key>/agent-thinking`: the pump posts each thinking block there while
    the session works. The wrapper waits for the agent alone, so the exit code
    is still the agent's."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(prompt)
    fed = bool(argv) and stdin_fed(argv)
    if fed:
        # A revived run reuses its directory: last time's closed marker would
        # make this one look finished before it starts.
        (run_dir / (INBOX + ".closed")).unlink(missing_ok=True)
        with open(run_dir / INBOX, "w") as f:
            f.write(json.dumps({"message": user_message(prompt), "text": prompt}) + "\n")
    if argv:
        argv = expand_spawn_argv(argv, {
            "prompt": [] if fed else prompt,
            "session": session_args(session_id, resume),
            "permission": permission_args(permission_mode),
            "dirs": dir_args(add_dirs)})
    else:
        argv = ([claude_bin, "-p", prompt, "--model", model, "--effort", effort,
                 "--output-format", "json"]
                + session_args(session_id, resume)
                + permission_args(permission_mode)
                + dir_args(add_dirs))
    env = {**os.environ,
           "CADRE_RUN_OUT": str(run_dir / "out.json"),
           "CADRE_RUN_ERR": str(run_dir / "err.txt"),
           "CADRE_RUN_EXIT": str(run_dir / "exit"),
           # `claude -p` waits ten minutes for background subagents and then
           # KILLS them, exiting 0 with whatever the session had said before
           # it delegated. One research round fanned out twelve agents, had
           # ten shot out from under it at the ceiling, and reported success
           # with the text "Waiting on the agents now." — no page, $49.92.
           # 0 means wait; the run's deadline (`enforce_deadline`) is the real
           # ceiling and the only one that should be deciding when a round has
           # gone long.
           "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
           **(extra_env or {})}
    script = '"$@" > "$CADRE_RUN_OUT" 2> "$CADRE_RUN_ERR"; echo $? > "$CADRE_RUN_EXIT"'
    pump = {"CADRE_PUMP_PY": sys.executable or "python3", "CADRE_PUMP": str(SESSION_PUMP),
            "CADRE_THINKING_URL": thinking_url or ""}
    if fed:
        # The session reads its messages from the pump through a named pipe:
        # the prompt first, then whatever the driver says while it works
        # (`push`). Only the agent is waited for, so its exit code is the run's.
        env |= pump | {"CADRE_RUN_IN": str(run_dir / INBOX)}
        script = ('rm -f "$CADRE_RUN_IN.fifo" && mkfifo "$CADRE_RUN_IN.fifo" && '
                  '{ "$CADRE_PUMP_PY" "$CADRE_PUMP" > "$CADRE_RUN_IN.fifo" 2> /dev/null & } && '
                  '"$@" < "$CADRE_RUN_IN.fifo" > "$CADRE_RUN_OUT" 2> "$CADRE_RUN_ERR"; '
                  'echo $? > "$CADRE_RUN_EXIT"')
    elif thinking_url:
        env |= pump
        script = ('"$@" > "$CADRE_RUN_OUT" 2> "$CADRE_RUN_ERR" & agent=$!; '
                  '"$CADRE_PUMP_PY" "$CADRE_PUMP" > /dev/null 2>&1 & '
                  'wait "$agent"; echo $? > "$CADRE_RUN_EXIT"')
    proc = subprocess.Popen(
        ["/bin/sh", "-c", script, "sh"] + argv,
        cwd=wt_path, env=env, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def _reap(pid: int) -> bool:
    """Collect a wrapper's exit status if it has one waiting. Explicit reaping
    is what lets the daemon keep SIGCHLD at default — an ignored SIGCHLD makes
    CPython report rc=0 for every failing subprocess, which once turned a
    failed `git worktree add` into a session spawned in an empty directory.
    True only when this call reaped the pid."""
    try:
        p, _ = os.waitpid(pid, os.WNOHANG)
        return p == pid
    except ChildProcessError:
        return False  # a prior daemon's child, or already collected


# ------------------------------------------------------------------ the ceiling

# Seconds between asking an overdue run to stop and making it.
KILL_GRACE = 30


def deadline(timeout) -> float | None:
    """When a run spawned now has gone on too long: the value to store as the
    run record's `deadline`. A timeout of 0 (or unset) means no ceiling: None,
    which `enforce_deadline` leaves alone."""
    return time.time() + int(timeout) if timeout and int(timeout) > 0 else None


def _signal_group(pgid: int, sig) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def enforce_deadline(run: dict, now: float | None = None) -> str | None:
    """Stop a run that is past its `deadline`: SIGTERM its process group, then
    SIGKILL it KILL_GRACE seconds later. Marks the run record so `outcome`
    reports it as timed out; returns a line to log when it signalled.

    Spawns start a new session, so the wrapper's pid is the process group and
    the agent under it goes with it. A run with no deadline (spawned before
    runs carried one) is left alone."""
    now = time.time() if now is None else now
    due = run.get("deadline")
    if not due or now < due or run.get("kill_sent"):
        return None
    if "term_sent" not in run:
        if Path(run["run_dir"], "exit").exists():
            return None             # finished on its own; the reap collects it
        run["term_sent"] = now
        run["timed_out_after"] = round(now - run.get("started", now))
        _signal_group(run["pid"], signal.SIGTERM)
        return f"timed out after {run['timed_out_after']} s — SIGTERM to pid {run['pid']}"
    if now - run["term_sent"] >= KILL_GRACE:
        run["kill_sent"] = now
        if _group_alive(run["pid"]):
            _signal_group(run["pid"], signal.SIGKILL)
            return f"still running {KILL_GRACE} s after SIGTERM — SIGKILL to pid {run['pid']}"
    return None


def finished(run: dict) -> bool:
    if run.get("term_sent") and not run.get("kill_sent"):
        # Stopping: the wrapper may be gone while the agent under it is still
        # shutting down. Not finished until the whole group is, or SIGKILL
        # has gone out.
        _reap(run["pid"])
        if _group_alive(run["pid"]):
            return False
    if Path(run["run_dir"], "exit").exists():
        _reap(run["pid"])  # exit file is the outcome; this just clears the zombie
        return True
    if _reap(run["pid"]):
        return True  # exited without writing an exit file — killed / lost
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
    if run.get("timed_out_after") is not None:
        rc = 124    # stopped at its deadline, whatever the wrapper managed to write
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
    payload = final_payload(stdout) if stdout else None
    if payload is not None:
        result_text = payload.get("result", "")
        usage = {k: payload.get(k) for k in
                 ("total_cost_usd", "usage", "num_turns", "duration_ms") if k in payload}
        record["result"] = result_text
        record["usage"] = usage
    elif stdout:
        record["raw_stdout"] = stdout[-8000:]
    # A stream's every line is the transcript, so only its non-JSON lines (an
    # error banner) may be read for a limit — the work itself can say "rate
    # limit" without the run having hit one.
    text = result_text or "\n".join(
        l for l in stdout.splitlines() if not l.startswith("{"))[-8000:]
    if record["timed_out"]:
        # a timeout is the run's own failure, never a pause to resume
        record["rate_limited"] = record["transient"] = False
        result_text = result_text or (
            f"timed out after {run.get('timed_out_after', record['seconds'])} s")
        return False, result_text, usage, record
    record["rate_limited"] = rc != 0 and is_rate_limited(text)
    record["transient"] = rc != 0 and not record["rate_limited"] and is_transient(text)
    return rc == 0, result_text, usage, record


def final_payload(stdout: str) -> dict | None:
    """The run's closing record: the whole output under `--output-format
    json`, or the `result` event that ends a `stream-json` transcript (one
    JSON event per line). None when neither is there — a run that died before
    it could say anything."""
    try:
        payload = json.loads(stdout)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


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
