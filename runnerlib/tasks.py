"""Workflow #3's entry point: put a task on the board and let the engine run it.

`config/actions-dialogue.json` is the workflow; this module is the one write
that starts one. It is deliberately tiny and UI-shaped — `pipeline.py task`
calls it today, and a surface-native inbox page calls the same function
tomorrow, because "the ask enters as a board event" is the whole contract.

The event it writes:

    namespace  cadre
    topic      tasks
    key        task:<id>
    kind       command
    payload    {target: "task", task, story, title, task_text, cwd, iteration}

`task` and `story` are the SAME id under two names — see the config's comment:
the spawn seam routes on `story` and the node's command substitutes it, while
this workflow's own vocabulary is `task`. The other two commands on this topic
(`task:feedback`, `task:verdict`) are written by whatever bridges the review
surface back to the board; their shapes are documented in the config.

The task node itself is seeded here rather than in `seed_nodes`, for the reason
`seed_eval_nodes` exists: a dialogue node is not a pipeline stage. Its prompt is
`prompts/task.md` ALONE — `_common.md` is the PR-gated pipeline's briefing (gh
CLI, never merge, never push to main) and prepending it to a node that has no
repo, no PR and no story would be instructions for a different world.
"""

import time
import uuid
from pathlib import Path

from . import engine_seam, seed_nodes
from .blackboard import Board
from .nodes import Nodes, version_id
from .registry import slugify

NAMESPACE = "cadre"
TOPIC = "tasks"

TARGET_REQUEST = "task"
TARGET_FEEDBACK = "task:feedback"
TARGET_VERDICT = "task:verdict"

NODE = "task"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "high"


def key_for(task_id: str) -> str:
    return f"task:{task_id}"


def new_id(title: str, now: float | None = None) -> str:
    """`task-<slug>-<HHMMSS>`. Readable in a log line and unique per second —
    the key is what the whole dialogue hangs off, so it must never collide with
    a task submitted earlier today."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    slug = slugify(title)[:24].strip("-")
    return f"task-{slug}-{stamp}" if slug else f"task-{stamp}"


# --------------------------------------------------------------------------- node


def prompt_text(prompts_dir=None) -> str:
    return (Path(prompts_dir or PROMPTS_DIR) / f"{NODE}.md").read_text()


def seed(data_dir, cfg=None, prompts_dir=None) -> dict:
    """Install the `task` node. Same timid-promotion contract as the other two
    seeds: register what is missing, re-pin the operator dials on what exists,
    record changed prompt text as a version WITHOUT promoting it."""
    text = prompt_text(prompts_dir)
    model = cfg.model_for(NODE) if cfg else DEFAULT_MODEL
    effort = cfg.effort_for(NODE) if cfg else DEFAULT_EFFORT
    command = seed_nodes.command_for(
        effort, cfg.claude["bin"] if cfg else seed_nodes.DEFAULT_BIN)
    meta = {"reads": ["command:task"], "emits": ["signal", "report"]}
    nodes = Nodes(data_dir)
    if NODE not in nodes.index["nodes"]:
        nodes.register(NODE, text, model, command, produced_by="seed", **meta)
        return {NODE: "registered"}
    rec = nodes.index["nodes"][NODE]
    rec.update(model=model, command=command,
               reads=list(meta["reads"]), emits=list(meta["emits"]))
    nodes._save()
    nodes._write_export(NODE)
    if any(v["id"] == version_id(text) for v in rec["versions"]):
        return {NODE: "unchanged"}
    nodes.new_version(NODE, text, produced_by="seed")
    return {NODE: "version-recorded"}


# --------------------------------------------------------------------------- submit


def submit_task(cfg, text: str, cwd=None, title=None) -> str:
    """Write the task-request event and return the new task id.

    Seeding the node is part of submitting: the action this event triggers
    spawns `task`, and an ask that lands on a board whose registry has no such
    node fails inside the engine minutes later instead of here, in front of
    whoever asked.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("a task needs text — what should the session do?")
    title = (title or text.splitlines()[0]).strip()[:80]
    cwd = str(Path(cwd or Path.cwd()).expanduser().resolve())
    task_id = new_id(title)

    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    seed(cfg.data_dir, cfg)
    board = Board(engine_seam.board_path(cfg))
    try:
        board.write(NAMESPACE, TOPIC, key_for(task_id), "command",
                    {"target": TARGET_REQUEST, "task": task_id, "story": task_id,
                     "title": title, "task_text": text, "cwd": cwd,
                     "iteration": "1"})
    finally:
        board.close()
    return task_id


# --------------------------------------------------------------------------- records
#
# Where a dialogue's operational state lives: `registry.json`, beside the
# stories, under its own `tasks` section.
#
# It is not a story. A story record means a repo, a feature branch, PR caches
# and a phase, and `_poll_story` would take a task for one and start asking
# GitHub about a repo that is the empty string. But the registry IS where run
# records already persist — `active_runs`, `pid`, `session_id`, `run_dir` —
# and a dialogue needs exactly those fields for exactly the same reason (the
# daemon can die between spawn and reap). So it gets the same file and the
# same shape, one level over.
#
# The session id is the part that matters: workflow #3's whole claim is that
# the driver's next annotation continues the SAME conversation, and a session
# id that is not written down before the process starts is a conversation that
# cannot be re-entered after a restart.


def records(reg) -> dict:
    return reg.data.setdefault("tasks", {})


def record(reg, task_id: str) -> dict:
    return records(reg).setdefault(
        task_id, {"task": task_id, "session_id": None, "iteration": 1,
                  "cwd": "", "active_runs": {}, "since": time.time()})


def active_count(reg) -> int:
    return sum(len(r.get("active_runs") or {}) for r in records(reg).values())


def session_for(reg, task_id: str, resume: bool) -> tuple[str, bool]:
    """(session id, resume?) for the turn about to spawn.

    The first turn MINTS an id and records it; every turn after it that asks to
    resume gets that same id back with resume=True, which `runs.session_args`
    turns into `--resume <id>` instead of `--session-id <id>`.

    A resume asked for on a task with nothing recorded (a board replayed into a
    fresh data dir) mints a fresh one rather than failing: a cold new session is
    a worse answer than a continued one, and no answer at all is worse still.
    """
    rec = record(reg, task_id)
    if resume and rec.get("session_id"):
        return rec["session_id"], True
    rec["session_id"] = str(uuid.uuid4())
    return rec["session_id"], False


# --------------------------------------------------------------------------- the driver's turn


def format_feedback(notes: list[dict]) -> str:
    """The driver's words as the node will read them in `$feedback`.

    Each note is quoted under the text it was attached to, because an
    annotation without its anchor is a floating sentence the session has to
    guess the target of (the same finding that put anchors on surface feedback
    in the first place).
    """
    out = []
    for note in notes:
        anchor = (note.get("anchor") or "").strip()
        text = (note.get("text") or "").strip()
        if not text:
            continue
        quoted = "> " + anchor.replace("\n", "\n> ") + "\n\n" if anchor else ""
        out.append(quoted + text)
    return "\n\n---\n\n".join(out)


def write_feedback(cfg, reg, task_id: str, cwd: str, notes: list[dict]) -> dict | None:
    """The driver annotated the page: write the `task:feedback` command.

    This is the board-side half of "a surface annotation is the next turn". The
    payload shape is the config's, not this function's — `target` tells it from
    the other two commands on the topic, `resume` is what the spawn seam reads
    to continue the session rather than start one, and the bumped `iteration`
    is what the node renders as "round N of M".
    """
    text = format_feedback(notes)
    if not text:
        return None
    rec = record(reg, task_id)
    rec["iteration"] = int(rec.get("iteration") or 1) + 1
    if hasattr(reg, "save"):
        # The bumped round has to survive a restart between this write and the
        # spawn it triggers, or the next turn renders as the round before it.
        reg.save()
    return _command(cfg, task_id,
                    {"target": TARGET_FEEDBACK, "task": task_id, "story": task_id,
                     "cwd": cwd or rec.get("cwd") or "",
                     "iteration": str(rec["iteration"]),
                     "feedback": text, "resume": "session"})


def write_verdict(cfg, task_id: str, verdict: str) -> dict:
    """The driver ruled on the page: write the `task:verdict` command.

    Only `approve` ends the dialogue (config: `task-approved-done`). A
    `continue` verdict is not written here at all — continuing IS a feedback
    turn, so the bridge writes it as one.
    """
    return _command(cfg, task_id,
                    {"target": TARGET_VERDICT, "task": task_id, "story": task_id,
                     "verdict": verdict})


def _command(cfg, task_id: str, payload: dict) -> dict:
    board = Board(engine_seam.board_path(cfg))
    try:
        return board.write(NAMESPACE, TOPIC, key_for(task_id), "command", payload)
    finally:
        board.close()
