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

A fourth, `task:route`, is the node event: it names the node that reacts
next, and the driver's verdict form and an agent's own handoff write it the
same way. See "the node event" below.

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
from html import escape
from pathlib import Path

from . import engine_seam, library, projects, seed_nodes
from .blackboard import Board
from .nodes import Nodes, version_id
from .registry import slugify

NAMESPACE = "cadre"
TOPIC = "tasks"

TARGET_REQUEST = "task"
TARGET_FEEDBACK = "task:feedback"
TARGET_VERDICT = "task:verdict"
TARGET_HANDOFF = "task:handoff"
# What the handoff event was called before it was named for what it does. Still
# accepted on read — the action triggers on both, and pages written before the
# rename carry `route:<node>` in their forms. Never written.
TARGET_ROUTE_LEGACY = "task:route"
TARGET_HANDOFFS = (TARGET_HANDOFF, TARGET_ROUTE_LEGACY)

NODE = "task"
IMPLEMENT = "implement"

# What a node lists in its `reads` to say it reacts to the handoff event — and
# so to be offered on every page's verdict form and in every node's prompt.
# Seeding rewrites a seeded node's reads every pass, so changing this migrates
# `task` and `implement` on the next daemon pass; the legacy string stays
# recognised for any node registered by hand before the rename.
HANDOFF = "command:task:handoff"
HANDOFF_LEGACY = "command:task:route"

# The dialogue's own nodes. `about` is what the driver and the other nodes
# read when choosing who takes the work next.
NODES = {
    NODE: {"reads": ["command:task", "command:task:feedback", HANDOFF],
           "about": "the general agent: does what it is asked or handed "
                    "(research, a design, a review, a change) and brings you "
                    "a page to rule on"},
    IMPLEMENT: {"reads": [HANDOFF, "command:task:feedback"],
                "about": "carries out what the page decided: makes the change, "
                         "commits only what the page proposed, and brings you "
                         "a page reporting what it did"},
}
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "high"


def key_for(task_id: str) -> str:
    return f"task:{task_id}"


def new_id(title: str, now: float | None = None) -> str:
    """`task-<slug>-<HHMMSS>-<suffix>`. Readable in a log line and unique even
    when several tasks with the same opening words are submitted in the same
    second — five batch dispatches collided on slug+second once, and the board
    key is what the whole dialogue hangs off, so the id carries real entropy."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    suffix = uuid.uuid4().hex[:6]
    slug = slugify(title)[:24].strip("-")
    base = f"task-{slug}-{stamp}" if slug else f"task-{stamp}"
    return f"{base}-{suffix}"


# --------------------------------------------------------------------------- node


def prompt_text(prompts_dir=None, node: str = NODE) -> str:
    return (Path(prompts_dir or PROMPTS_DIR) / f"{node}.md").read_text()


def seed(data_dir, cfg=None, prompts_dir=None) -> dict:
    """Install the dialogue's nodes: `task` and `implement`. Same
    timid-promotion contract as the other two seeds: register what is missing,
    re-pin the operator dials (and what the node listens for, and its `about`)
    on what exists, record changed prompt text as a version WITHOUT promoting
    it."""
    nodes = Nodes(data_dir)
    out = {}
    for node, spec in NODES.items():
        text = prompt_text(prompts_dir, node)
        model = cfg.model_for(node) if cfg else DEFAULT_MODEL
        effort = cfg.effort_for(node) if cfg else DEFAULT_EFFORT
        command = seed_nodes.command_for(
            effort, cfg.claude["bin"] if cfg else seed_nodes.DEFAULT_BIN)
        meta = {"reads": list(spec["reads"]), "emits": ["signal", "report"],
                "about": spec["about"]}
        if node not in nodes.index["nodes"]:
            nodes.register(node, text, model, command, produced_by="seed", **meta)
            out[node] = "registered"
            continue
        rec = nodes.index["nodes"][node]
        rec.update(model=model, command=command, about=meta["about"],
                   reads=list(meta["reads"]), emits=list(meta["emits"]))
        nodes._save()
        nodes._write_export(node)
        if any(v["id"] == version_id(text) for v in rec["versions"]):
            out[node] = "unchanged"
            continue
        nodes.new_version(node, text, produced_by="seed")
        out[node] = "version-recorded"
    return out


# --------------------------------------------------------------------------- submit


def submit_task(cfg, text: str, cwd=None, title=None, project=None, run=None) -> str:
    """Write the task-request event and return the new task id.

    Seeding the node is part of submitting: the action this event triggers
    spawns `task`, and an ask that lands on a board whose registry has no such
    node fails inside the engine minutes later instead of here, in front of
    whoever asked.

    `project` (id or name) launches from a project: its directory is the
    default cwd and its default launch choice the default `run`. Without one,
    a cwd inside a registered project's directory still files under it.
    `run` is what to launch, `kind:name` from the library index; it becomes
    a prefix on the session's first prompt (`library.invocation`), so it is
    checked against what is installed HERE, not discovered missing by the
    session.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("a task needs text — what should the session do?")
    title = (title or text.splitlines()[0]).strip()[:80]
    projs = projects.load(cfg.data_dir)
    proj = None
    if project:
        proj = projects.resolve(projs, project)
        if proj is None:
            raise ValueError(f"no project {project!r} — `pipeline.py project list`")
        if proj.get("archived"):
            raise ValueError(f"project {proj['name']!r} is archived")
        run = run or proj.get("launch") or None
        cwd = cwd or projects.workdir(projs, proj["id"])
    cwd = str(Path(cwd or Path.cwd()).expanduser().resolve())
    proj = proj or projects.for_dir(projs, cwd)
    if proj is None:
        # Running work in a directory IS the act of starting a project there.
        # Without this, a dispatch into an unregistered checkout drew a
        # section that existed only while something was open in it and
        # vanished when the last page was ruled on — the driver's report was
        # that his projects "collapsed off the page". Registering here means
        # every project on the fleet is durable, whether it was created
        # deliberately or by being worked in. A deliberate one is still
        # better: it can carry directories, a group and a context store.
        try:
            proj = projects.create(cfg.data_dir, Path(cwd).name, dirs=[cwd])
            projs = projects.load(cfg.data_dir)
        except Exception:
            proj = None  # never block a task on bookkeeping
    picked = {}
    if run:
        entry = library.resolve(library.scan(dirs=[cwd], skills_source=cfg.skills_source), run)
        picked = {"run": f"{entry['kind']}:{entry['name']}", "invoke": entry["invoke"]}
    task_id = new_id(title)

    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    seed(cfg.data_dir, cfg)
    board = Board(engine_seam.board_path(cfg))
    try:
        board.write(NAMESPACE, TOPIC, key_for(task_id), "command",
                    {"target": TARGET_REQUEST, "task": task_id, "story": task_id,
                     "title": title, "task_text": text, "cwd": cwd,
                     "iteration": "1",
                     **({"project": proj["id"]} if proj else {}), **picked})
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
    # The feedback goes BACK through the node that built the page, whichever
    # one that is: `node` names it, and the one feedback action starts it.
    return _command(cfg, task_id,
                    {"target": TARGET_FEEDBACK, "task": task_id, "story": task_id,
                     "node": rec.get("node") or NODE,
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


# --------------------------------------------------------------------------- the node event
#
# A handoff is one event: `task:handoff` naming the node that reacts next. One
# action (config/actions-handoff.json) starts whichever node it names, so a new
# specialist needs no new action and no restart — registering it with
# HANDOFF in its `reads` is what puts it on every page's form and in every
# node's prompt. What connects to what is which nodes listen, not a list here.
#
# Three writers, one event: the driver's verdict form (surface bridge), an
# agent handing off mid-dialogue (`pipeline.py handoff`), and the same command
# typed by a person. All three go through `write_handoff`, which refuses a node
# that is not registered to hear the event — loudly, before anything is
# written. `by` records which of them wrote it; no action reads it.


class UnknownNode(ValueError):
    """A handoff to a node that is not registered to take one."""


def handoff_nodes(nodes: Nodes, exclude: str | None = None) -> list[dict]:
    """The nodes a page may hand to, live from the registry: every node that
    listens for the node event, minus the one that built the page (Continue
    already sends it back there)."""
    listed = {n["name"]: n for n in nodes.listening(HANDOFF)}
    for n in nodes.listening(HANDOFF_LEGACY):
        listed.setdefault(n["name"], n)
    return [n for n in listed.values() if n["name"] != exclude]


def describe_nodes(listed: list[dict]) -> str:
    """The nodes as a prompt reads them in `$nodes`: who exists, what for."""
    if not listed:
        return ("None. No other registered node takes a handoff, so the form "
                "offers no hand-off lines and there is nobody to hand to.")
    return "\n".join(f"- **{n['name']}** — {n['about'] or '(no description registered)'}"
                     for n in listed)


def node_options(listed: list[dict]) -> str:
    """The verdict-form lines, one per node that can take the work
    (`$handoff_options`). Rendered here rather than by the page author, so the
    choice reads the same on every page. Double quotes are stripped: the page
    contract forbids them inside attribute values."""
    lines = []
    for n in listed:
        about = escape((n["about"] or "").replace('"', ""), quote=False)
        name = escape(n["name"], quote=False)
        lines.append(f'<label><input type="radio" name="verdict" '
                     f'value="handoff:{n["name"]}"> Hand this to <strong>{name}</strong>'
                     + (f" — {about}" if about else "") + "</label>\n")
    return "".join(lines)


def check_handoff(cfg, node: str) -> None:
    """Raise UnknownNode unless `node` is registered AND listens for the node
    event. Read from the registry on disk at call time — no cache to go stale."""
    nodes = Nodes(cfg.data_dir)
    takers = [n["name"] for n in handoff_nodes(nodes)]
    if node in takers:
        return
    if node in nodes.names():
        raise UnknownNode(
            f"node {node!r} is registered but does not take a handoff (its reads "
            f"lack {HANDOFF!r}); nodes that do: {', '.join(takers) or 'none'}")
    raise UnknownNode(f"no node {node!r} is registered; nodes that take a "
                      f"handoff: {', '.join(takers) or 'none'}")


def write_handoff(cfg, reg, task_id: str, node: str, page: str, notes: list[dict],
                  by: str = "driver") -> dict:
    """Write the handoff event: `node` reacts next. The annotations ride along as
    `feedback` and the page the handoff was made from as `surface_prev`, so
    the node starts from what was seen. `from` is the node that built that
    page. Refuses an unregistered node before writing anything."""
    check_handoff(cfg, node)
    rec = record(reg, task_id)
    return _command(cfg, task_id,
                    {"target": TARGET_HANDOFF, "node": node,
                     "from": rec.get("node") or NODE, "by": by,
                     "task": task_id, "story": task_id, "cwd": rec.get("cwd") or "",
                     "iteration": "1", "feedback": format_feedback(notes),
                     "surface_prev": page})


def _command(cfg, task_id: str, payload: dict) -> dict:
    board = Board(engine_seam.board_path(cfg))
    try:
        return board.write(NAMESPACE, TOPIC, key_for(task_id), "command", payload)
    finally:
        board.close()
