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
