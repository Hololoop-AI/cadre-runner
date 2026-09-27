"""What a running turn is actually doing, read off its transcript.

The problem this solves: a turn that fans out background agents goes silent.
The parent session writes "waiting on the agents" and then nothing for half an
hour, because the parent IS blocked — all the work is happening in child
sessions. From the outside that is indistinguishable from a dead process, and
the driver has twice now assumed a healthy run had died.

Nothing here asks the agent to report anything. Claude Code already writes a
JSONL transcript per session and one per background subagent, appending as
tool calls happen, so liveness is a file mtime and "what it is doing right now"
is the last `tool_use` in the file that moved most recently. A signal derived
from the work itself cannot drift from the work, which is the failure mode of
every self-reported heartbeat: the agent under load is exactly the agent that
forgets to say it is alive.

The cost of that choice is a dependency on Claude Code's on-disk layout, which
is not a published contract. So every read here is best-effort and every
failure is silence: a missing directory, a renamed field or a format that moved
must degrade to "no detail available" and never to a traceback on the fleet
page. `snapshot` returning None means "nothing to add", not "nothing running" —
the caller already knows a process is alive and only asks this for colour.

Cheapness matters because the fleet page renders this on every request while
the transcripts are megabytes each and there can be a dozen of them. So the
sweep stats nothing but mtimes, and only the newest file is read at all — from
its tail, not its start.
"""

import json
import os
import re
import time
from pathlib import Path

#: Where Claude Code keeps per-directory session transcripts.
PROJECTS = Path.home() / ".claude" / "projects"

#: A file untouched for longer than this is not counted as an active agent.
#: Generous on purpose: a single long web fetch or a model call on a big
#: context can leave a genuinely busy agent quiet for a minute or two, and
#: calling that one dead is the mistake this module exists to stop making.
ACTIVE_WINDOW = 600.0

#: How much of the newest transcript to read. The last tool call is within a
#: few KB of the end; reading the whole megabyte to find it would put a
#: multi-megabyte parse on every page render.
TAIL_BYTES = 65536


def project_dir(cwd: str | os.PathLike) -> Path:
    """The transcript directory for a working directory.

    Claude Code slugs the absolute path by replacing every `/` and `.` with a
    dash, so `a/b/research` under the root becomes `-a-b-research`. Derived
    rather than searched for: guessing by glob would match a sibling directory
    whose name happens to share a prefix.
    """
    return PROJECTS / re.sub(r"[/.]", "-", str(Path(cwd)))


def subagent_dir(cwd: str | os.PathLike, session_id: str) -> Path:
    """Where the background agents of one session write their transcripts."""
    return project_dir(cwd) / str(session_id) / "subagents"


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _last_tool_use(path: Path, tail: int = TAIL_BYTES) -> tuple[str, str] | None:
    """The (tool, target) of the last tool call in a transcript.

    Reads the tail only, and drops the first line of it — a window into the
    middle of a file almost always opens mid-line, and a torn JSON object is
    not worth guessing at.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail:
                f.seek(size - tail)
                f.readline()
            chunk = f.read()
    except OSError:
        return None
    found = None
    for line in chunk.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = ((rec.get("message") or {}).get("content"))
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            target = str(args.get("query") or args.get("url")
                         or args.get("description") or args.get("pattern") or "")
            found = (name, target)
    return found


def _tidy(tool: str, target: str, limit: int = 58) -> str:
    """The last call as a driver reads it: a host for a fetch, the words for a
    search, the description for anything else."""
    tool = re.sub(r"^mcp__[^_]+__", "", tool)
    if target.startswith(("http://", "https://")):
        target = re.sub(r"^https?://(www\.)?", "", target).split("/")[0]
    target = " ".join(target.split())
    if len(target) > limit:
        target = target[:limit - 1].rstrip() + "…"
    return f"{tool} {target}".strip()


def snapshot(cwd: str | os.PathLike, session_id: str, now: float | None = None,
             window: float = ACTIVE_WINDOW) -> dict | None:
    """What this session's turn is doing, or None if the transcripts say
    nothing useful.

    Returns `agents` (background agents touched inside the window), `last_at`
    (the newest write anywhere in the session) and `last` (that file's final
    tool call, rendered for a badge).
    """
    if not session_id:
        return None
    now = time.time() if now is None else now
    parent = project_dir(cwd) / f"{session_id}.jsonl"
    try:
        children = sorted(subagent_dir(cwd, session_id).glob("agent-*.jsonl"))
    except OSError:
        children = []
    stamped = [(p, _mtime(p)) for p in [parent, *children]]
    stamped = [(p, t) for p, t in stamped if t]
    if not stamped:
        return None
    fresh = [(p, t) for p, t in stamped
             if p != parent and now - t <= window]
    newest, last_at = max(stamped, key=lambda pair: pair[1])
    call = _last_tool_use(newest)
    return {"agents": len(fresh), "last_at": last_at,
            "last": _tidy(*call) if call else ""}


def phrase(snap: dict | None, now: float | None = None) -> str:
    """The snapshot as one clause to hang off a "working" badge.

    Silent when there is nothing to say — an empty string appends nothing,
    which keeps the caller from having to know whether this module found
    anything.
    """
    if not snap:
        return ""
    now = time.time() if now is None else now
    bits = []
    if snap.get("agents"):
        n = snap["agents"]
        bits.append(f"{n} agent{'s' if n != 1 else ''}")
    if snap.get("last"):
        bits.append(snap["last"])
    if not bits:
        return ""
    quiet = max(0.0, now - (snap.get("last_at") or now))
    if quiet < 90:
        when = "just now"
    elif quiet < 3600:
        when = f"{int(quiet // 60)}m ago"
    else:
        when = f"{int(quiet // 3600)}h ago"
    return f"{' · '.join(bits)}, {when}"
