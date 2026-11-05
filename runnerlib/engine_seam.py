"""The daemon seam: one pass, driven by the board instead of by Python.

`CADRE_ENGINE` has three settings, and `mode()` is the only reader of it:

    off     (unset / 0)   nothing here runs, so nothing starts a turn
    shadow  (1)           the engine runs; kept as a truthy alias of `only`
                          from when a legacy dispatcher ran beside it
    only                  the engine runs (what every deployment sets)

A pass is three moves:

    deferred   turns parked because their task was busy or the run cap full
    tick       engine.tick(board, actions, nodes, data_dir)
    spawn      each returned spawn spec goes through the EXISTING spawn
               machinery; the engine never forks an agent

The engine hands back a spec, not a process, precisely so the concurrency
cap, the session id, the surface out-path and the cost record all stay in
pipeline.py's `_run_task` — one spawn path, not two.
"""

import os
import shlex
import sys
from pathlib import Path

from . import claude_run, engine, projects
from . import runs as runs_mod
from .blackboard import Board
from .nodes import Nodes

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# The action files the daemon runs, as a LIST — one workflow per file, all of
# them live in one tick. The board is the only coupling between them, so
# running two workflows is reading two files; `engine.load_action_set` refuses
# a name collision across them rather than letting two actions share a cursor.
#
# `actions-eval.json` is deliberately NOT here. Its actions spawn the eval
# judge nodes, which `state()` does not seed (it runs `seed_nodes` only), so
# adding the file would put spawns of unregistered nodes into every pass of
# every deployment. The eval workflow runs from its own harness against its own
# data dir; including it here is a seeding change, not a list change.
ACTIONS_PATHS = (CONFIG_DIR / "actions-dialogue.json",
                 # the node event: one action that starts whichever node a
                 # handoff names (config/actions-handoff.json)
                 CONFIG_DIR / "actions-handoff.json")

PIPELINE = Path(__file__).resolve().parent.parent / "pipeline.py"

NAMESPACE = "cadre"
TASKS_TOPIC = "tasks"

MODES = ("off", "shadow", "only")

# Payload fields a trigger event may hand to the rendered prompt as `$vars`.
# Closed on purpose — a prompt variable is part of a node's contract, so the
# list of what an event is allowed to inject is readable in one place rather
# than being "whatever the payload happened to carry". A dialogue node has
# no registry record to read a task, a cwd or a round from, so they ride here.
PROMPT_VARS = ("task", "task_text", "cwd", "feedback",
               "iteration", "surface_prev", "closing")

_state = {}


def mode() -> str:
    """off | shadow | only, from `CADRE_ENGINE`.

    With the legacy dispatcher gone, `shadow` and `only` behave the same;
    both names still parse so an existing unit's environment keeps working.
    """
    raw = (os.environ.get("CADRE_ENGINE") or "").strip().lower()
    if raw in ("", "0", "off", "false", "no"):
        return "off"
    return "only" if raw == "only" else "shadow"


def enabled() -> bool:
    return mode() != "off"


def board_path(cfg) -> Path:
    return cfg.data_dir / "board.db"


def state(cfg) -> dict:
    """Board + node registry + loaded actions, opened once per process. The
    board is one SQLite file and one connection by design; reopening it every
    pass would just churn WAL."""
    from . import tasks            # workflow #3 imports this module; late-bind
    path = str(board_path(cfg))
    if path not in _state:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        # A subprocess inherits the environment and nothing else. Without
        # this, board_events in a child would re-load config.toml and aim at
        # the DEFAULT data dir — a writer on a different board than the engine
        # reads is silent, total failure (and, from a test, a write into the
        # operator's live state).
        os.environ["CADRE_DATA_DIR"] = str(cfg.data_dir)
        tasks.seed(cfg.data_dir, cfg)           # workflow #3's nodes
        board = Board(path)
        actions = engine.load_action_set(ACTIONS_PATHS)
        start_new_actions_at_head(board, actions)
        _state[path] = {"board": board, "nodes": Nodes(cfg.data_dir),
                        "actions": actions, "actions_seen": _actions_stamp(),
                        "actions_error": None}
    return _state[path]


def _actions_stamp() -> tuple:
    """What the action files look like on disk: (path, mtime, size) each. A
    change here is the only reason to re-read them."""
    out = []
    for p in ACTIONS_PATHS:
        try:
            st = Path(p).stat()
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(p), None, None))
    return tuple(out)


def current_actions(st: dict, log) -> list[dict]:
    """The action set for this pass, re-read when a file changed on disk —
    the same contract the node registry already keeps, so adding or editing
    an action no longer needs a daemon restart.

    An edit that does not load (bad JSON, an unknown operator, a name used
    twice) does not take the daemon down and does not half-apply: the last set
    that loaded keeps running, and the refusal is logged once per broken
    version of the files — loudly, because an edit that silently does nothing
    is the failure this project keeps paying for. A newly added action starts
    at the board's head, exactly as at startup."""
    stamp = _actions_stamp()
    if stamp == st["actions_seen"]:
        return st["actions"]
    st["actions_seen"] = stamp
    try:
        fresh = engine.load_action_set(ACTIONS_PATHS)
    except engine.ActionError as e:
        st["actions_error"] = str(e)
        log(f"engine: ACTIONS NOT RELOADED — {e}; still running the previous "
            f"{len(st['actions'])} action(s) until the files load")
        return st["actions"]
    old = {a["name"] for a in st["actions"]}
    new = start_new_actions_at_head(st["board"], fresh)
    names = {a["name"] for a in fresh}
    st["actions"], st["actions_error"] = fresh, None
    log(f"engine: actions reloaded — {len(fresh)} action(s)"
        + (f"; added {', '.join(sorted(names - old))}" if names - old else "")
        + (f"; removed {', '.join(sorted(old - names))}" if old - names else "")
        + (f" (new ones start at the board's head: {', '.join(new)})" if new else ""))
    return fresh


def start_new_actions_at_head(board: Board, actions: list[dict]) -> list[str]:
    """An action installed into a board that has already been running starts
    at the board's head, not at its first event.

    The board keeps one cursor per action name, and a name it has never seen
    reads from seq 0 — so a new action would fire on every matching event in
    the board's history. For the node-event action that meant re-spawning
    `implement` for a handoff a driver made days ago. A board with no cursors
    at all is a fresh deployment, where reading from the start is right (a
    task submitted before the daemon's first pass must still run)."""
    known = board.consumers()
    if not known:
        return []
    new = [a["name"] for a in actions if a["name"] not in known]
    for name in new:
        board.start_at_head(name)
    return new


def reset():
    """Drop cached handles — tests open many temp data dirs in one process."""
    for st in _state.values():
        st["board"].close()
    _state.clear()


# --------------------------------------------------------------------------- pass


def tick_pass(cfg, reg, log, run_turn) -> dict:
    """One engine pass. `run_turn` is passed in rather than imported: the seam
    must not import pipeline.py (which imports the seam), and the daemon owns
    the spawn machinery either way."""
    st = state(cfg)
    board, actions = st["board"], current_actions(st, log)
    # The node registry is re-opened every pass, never cached: `Nodes.__init__`
    # reads index.json once and holds it, so a cached handle in a daemon that
    # runs for weeks would pin every node's command and active prompt version
    # at daemon-start — a CLI re-seed or an explicit `promote` would not reach
    # traffic until a restart. One small JSON read per pass buys the registry's
    # own contract back: promotion is what puts a version in front of traffic.
    nodes = Nodes(cfg.data_dir)

    # Turns that were handed work while a turn of theirs was still running.
    run_deferred(cfg, reg, board, log, run_turn)


    spawns, firings = engine.tick(board, actions, nodes, cfg.data_dir)
    for firing in firings:
        if firing["outcome"] != "fired":
            log(f"engine: action {firing['action']} {firing['outcome']}"
                + (f" — {firing.get('error')}" if firing.get("error") else ""))
    for spec in spawns:
        try:
            run_spawn_spec(cfg, reg, board, log, run_turn, spec)
        except Exception as e:
            # A spawn that cannot start is a failed signal like any other, so
            # the stage-failed action sees it instead of it dying in a log line.
            ev = board.get(spec["event_id"]) or {}
            board.write(NAMESPACE, TASKS_TOPIC, spec["key"], "signal",
                        {"status": "failed", "stage": spec["node"],
                         "story": (ev.get("payload") or {}).get("story", ""),
                         "error": f"{type(e).__name__}: {e}"[:300]},
                        correlation_id=spec.get("correlation_id"))
            log(f"engine: spawn of {spec['node']} failed — {e}")
    return {"spawns": spawns, "firings": firings}


# --------------------------------------------------------------------------- spawn


def run_spawn_spec(cfg, reg, board: Board, log, run_turn, spec: dict):
    """Execute one engine spawn spec through `run_turn` (pipeline._run_task).

    Every workflow the daemon loads is a dialogue one, so every spawn fires on
    the tasks topic; anything else is a definition error and fails loudly
    rather than starting a session with nowhere to work.
    """
    event = board.get(spec["event_id"]) or {}
    if event.get("topic") != TASKS_TOPIC:
        raise RuntimeError(f"spawn of {spec['node']} fired on topic "
                           f"{event.get('topic')!r}, not {TASKS_TOPIC!r}")
    return run_task_spawn(cfg, reg, board, log, run_turn, spec,
                          event.get("payload") or {})


def run_task_spawn(cfg, reg, board: Board, log, run_turn, spec: dict, payload: dict):
    """One turn of a dialogue (config/actions-dialogue.json).

    Everything the pipeline spawn does to give a session somewhere to work —
    find the story in the registry, ensure a checkout, cut a branch, add a
    worktree — means nothing here. A task has no repo. The ask names a `cwd`
    and the session runs in THAT directory, the one the driver is looking at,
    with nothing checked out and nothing to tear down afterwards.

    What replaces all of it is one value: the session id. It is minted on the
    first turn and recorded against the task, and a turn carrying
    `resume: "session"` gets it back — so the driver's annotation continues the
    conversation that read their page instead of starting a cold one that has
    to be told everything again. That is the entire workflow.
    """
    from . import tasks
    task_id = payload.get("task") or payload.get("story") or spec["key"].split(":", 1)[-1]
    rec = tasks.record(reg, task_id)
    if rec.get("active_runs"):
        # A turn of this task is still running — typically the very agent that
        # just handed the work on, before it has written its page and exited.
        # Starting now would be refused by `_run_task` (one turn at a time),
        # and dropping it is the silent no-op this project has paid for. So it
        # waits, on the record, and `run_deferred` starts it once the running
        # turn has been reaped and its page opened.
        rec.setdefault("deferred", []).append(spec)
        if hasattr(reg, "save"):
            reg.save()
        log(f"engine: {spec['node']} for {task_id} waits — a turn is still "
            f"running; it starts when that turn ends")
        return None
    cwd = str(payload.get("cwd") or "").strip()
    if not cwd.startswith("/"):
        # The ask is the only thing that names a working directory, so a
        # missing or relative one is a spawn that would land wherever the
        # daemon happens to be — refuse it here rather than find out from the
        # session's transcript.
        raise RuntimeError(f"task {task_id}: cwd {cwd!r} is not an absolute path")
    first = payload.get("target") in (tasks.TARGET_REQUEST, tasks.TARGET_START)
    if payload.get("target") == tasks.TARGET_START:
        # Started on a node of its own (a study session): `continue` on its
        # page comes back through that node, not through `task`.
        rec["node"] = spec["node"]
    if first:
        # The project and the launch choice ride only the ask; every later
        # turn (feedback, a route) reads them back from the record.
        for k in ("project", "run"):
            if payload.get(k):
                rec[k] = payload[k]
    projs = projects.load(cfg.data_dir)
    pid = rec.get("project") if rec.get("project") in projs else None
    skills = list((projs.get(pid) or {}).get("skills") or [])
    if str(rec.get("run") or "").startswith("skill:"):
        skills.append(rec["run"].split(":", 1)[1])
    try:
        linked, missing = claude_run.install_task_skills(cwd, cfg.skills_source,
                                                         extra=skills)
        if linked:
            log(f"engine: linked skills into {cwd}: {', '.join(linked)}")
        if missing:
            log(f"engine: skills not found for {cwd}: {', '.join(missing)} — looked in "
                f"{claude_run.REPO_SKILLS} and {cfg.skills_source}")
    except Exception as e:
        log(f"engine: task skill install failed for {cwd}: {e}")
    if payload.get("target") == tasks.TARGET_HANDOFF:
        # The node event handed this task to a node. Its turns get their own
        # page and a session of their own; the reap links the new page
        # derived-from the one it was handed from, and from here `continue`
        # on it goes back through THIS node.
        rec["node"] = spec["node"]
        rec["page"] = str(Path(cfg.data_dir) / "surfaces"
                          / f"task-{task_id}-{spec['node']}.html")
        rec["handoff_from"] = str(payload.get("surface_prev") or "")
    session_id, resume = tasks.session_for(
        reg, task_id, payload.get("resume") == "session")
    extra = {k: v for k, v in payload.items() if k in PROMPT_VARS}
    # Who can take this work next, live from the registry: every node that
    # listens for the node event. The prompt is told who they are and what
    # each is for, and gets the exact form lines to offer the driver.
    listed = tasks.handoff_nodes(Nodes(cfg.data_dir), exclude=spec["node"])
    extra["nodes"] = tasks.describe_nodes(listed)
    # `$routes` is the same text under the name prompts before the node
    # event used; an unpromoted prompt version still renders the live list.
    extra["routes"] = extra["nodes"]
    extra["handoff_options"] = tasks.node_options(listed)
    # ...and `$route_options` is the form lines under their name from before
    # the handoff rename, for the same reason.
    extra["route_options"] = extra["handoff_options"]
    # ...and the command that writes the same event the form does, for a
    # node that hands on by itself.
    extra["handoff"] = handoff_command(cfg, task_id)
    # ...and the bare command line, for a node that drives other subcommands
    # (the tutor's `cards`, a turn starting a `study` session).
    extra["pipeline"] = pipeline_command(cfg)
    # The project this task runs in: what else it may read, and where the
    # project's brief and recorded decisions live.
    extra["project"] = projects.prompt_block(projs, pid, cwd)
    action = {
        "type": "run_task",
        "stage": spec["node"],
        "task": task_id,
        "cwd": cwd,
        "session_id": session_id,
        "resume": resume,
        "model": spec["model"],
        "effort": None,          # the node's command carries it; config still wins
        "prompt_template": Path(spec["prompt_path"]).read_text(),
        "argv": spec.get("argv"),
        "node_version": spec["version"],
        "firing_id": spec.get("firing_id"),
        "correlation_id": spec.get("correlation_id"),
        "extra_vars": extra,
        # Group crossover: sibling directories reach the session as extra
        # readable directories (`{dirs}` in the node's command).
        "add_dirs": projects.read_dirs(projs, pid, cwd) if pid else [],
        # What the ask picked to run, as the prefix of the FIRST prompt only:
        # a resumed turn already has it loaded, and a route starts a
        # different node's work.
        "invoke": str(payload.get("invoke") or "") if first else "",
    }
    log(f"engine: spawning {spec['node']} turn for {task_id} in {cwd} "
        f"({'resuming' if resume else 'new'} session {session_id[:8]}, "
        f"node version {spec['version'][:12]}, firing {spec.get('firing_id')})")
    try:
        run_turn(cfg, reg, task_id, action)
    except runs_mod.RunsBusy as e:
        # The machine is full, not the task broken. Dropping the turn here was
        # the silent loss of a driver's handoff; it waits at the FRONT of the
        # task's queue instead, and `run_deferred` retries it every pass until
        # a slot frees. Logged once, when it starts waiting.
        if not spec.get("waiting_for_slot"):
            log(f"engine: {spec['node']} for {task_id} waits — {e}; "
                f"it starts when a slot frees")
        spec["waiting_for_slot"] = True
        rec.setdefault("deferred", []).insert(0, spec)
        if hasattr(reg, "save"):
            reg.save()
        return False


def run_deferred(cfg, reg, board: Board, log, run_turn) -> int:
    """Start the turns `run_task_spawn` parked because their task was busy or
    the machine was at its run cap, for every task whose running turn has since
    been reaped. One per task per pass: the first one started makes the task
    busy again. A turn that still finds the cap full ends the pass."""
    from . import surface, tasks
    started = 0
    for task_id, rec in list(tasks.records(reg).items()):
        if rec.get("active_runs") or not rec.get("deferred"):
            continue
        spec = rec["deferred"].pop(0)
        payload = (board.get(spec["event_id"]) or {}).get("payload") or {}
        prev = str(payload.get("surface_prev") or "")
        if payload.get("target") == tasks.TARGET_HANDOFF and prev:
            # The page the work was handed on from is finished: the agent
            # that wrote it moved the work on. Close it like a driver's
            # handoff closes it, so Continue on it cannot reach the next node.
            if (surface.sessions(cfg).get(prev) or {}).get("open"):
                surface.end_session(cfg, prev, log)
        try:
            if run_task_spawn(cfg, reg, board, log, run_turn, spec, payload) is False:
                break  # no free slot: it went back to the front; nothing else will fit
            started += 1
        except Exception as e:
            board.write(NAMESPACE, TASKS_TOPIC, spec["key"], "signal",
                        {"status": "failed", "stage": spec["node"], "story": task_id,
                         "error": f"{type(e).__name__}: {e}"[:300]},
                        correlation_id=spec.get("correlation_id"))
            log(f"engine: deferred {spec['node']} for {task_id} failed — {e}")
    return started


def pipeline_command(cfg) -> str:
    """The shell prefix that runs this runner's CLI against this config — what
    a session puts in front of a subcommand (`handoff`, `cards`, `study`)."""
    conf = getattr(cfg, "path", None)
    parts = [sys.executable, str(PIPELINE)]
    if conf:
        parts += ["--config", str(conf)]
    return " ".join(shlex.quote(p) for p in parts)


def handoff_command(cfg, task_id: str) -> str:
    """The shell line a session runs to hand its task to another node: the
    same `task:handoff` event the verdict form writes, through the same
    `tasks.write_handoff`. `<node>` is the one placeholder the agent fills."""
    return (f"{pipeline_command(cfg)} handoff {shlex.quote(task_id)} --node"
            ' <node> --note "what the next node should do with this"')
