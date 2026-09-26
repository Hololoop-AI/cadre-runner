"""The daemon seam: one poll pass, driven by the board instead of by Python.

`CADRE_ENGINE` has three settings, and `mode()` is the only reader of it:

    off     (unset / 0)   nothing here runs; pipeline.py's legacy path is untouched
    shadow  (1)           BOTH run. The legacy dispatcher fires first each pass
                          and `runs.key_active` drops the engine's duplicate
                          spawn for the same (stage, slice, pr). The overlap is
                          deliberate — it is what lets the fixture suite put the
                          two side by side — and it is the thing that retires.
    only                  the ENGINE drives. pipeline.py skips legacy stage
                          SPAWNING (see `_poll_story`) and keeps everything the
                          engine has no equivalent for: reaping, registry
                          effects, surface, ask/park/revive, auto-merge.

`only` is where the round caps have to be re-found. In shadow they live in
`dispatcher.dispatch` (rounds >= limits.max_rounds_per_stage -> escalate), which
engine-only skips for spawn actions; the trigger vocabulary has no count
predicate, so the cap is enforced here instead, at `run_spawn_spec`, against the
firing log. That is an interim fix, not a design: a count predicate in the
trigger vocabulary is the later call.

A pass is four moves:

    adopt      every registered story gets a `command` on the board once, plus a
               state marker if it is already past intake (so the once-only
               guard on the intake action does not re-run S0 for a live story)
    heartbeat  one `heartbeat` event — the clock the gh-watch action runs on
    tick       engine.tick(board, actions, nodes, data_dir)
    spawn      each returned spawn spec goes through the EXISTING stage-spawn
               machinery; the engine never forks an agent

The engine hands back a spec, not a process, precisely so the worktree, the
branch guard, the concurrency cap, the session id, the surface out-path and the
cost record all stay in `_run_stage` — one spawn path, not two.
"""

import os
import shlex
import sys
import time
from pathlib import Path

from . import claude_run, engine, projects, seed_nodes
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
ACTIONS_PATHS = (CONFIG_DIR / "actions-pipeline.json",
                 CONFIG_DIR / "actions-dialogue.json",
                 # the node event: one action that starts whichever node a
                 # handoff names (config/actions-handoff.json)
                 CONFIG_DIR / "actions-handoff.json")

PIPELINE = Path(__file__).resolve().parent.parent / "pipeline.py"

NAMESPACE = "cadre"
STORIES_TOPIC = "stories"
STAGES_TOPIC = "stages"
TASKS_TOPIC = "tasks"
RUNNER_TOPIC = "runner"

MODES = ("off", "shadow", "only")

# Stages whose legacy cap is a per-story (interrogate) or per-PR (revise,
# reconcile) round count. In engine-only mode nothing else counts them.
ROUND_CAPPED = ("interrogate", "revise", "reconcile")
DEFAULT_MAX_ROUNDS = 5          # mirrors config.DEFAULTS["limits"]

# Payload fields a trigger event may hand to the rendered prompt as `$vars`.
# Closed on purpose — a prompt variable is part of a node's contract, so the
# list of what an event is allowed to inject is readable in one place rather
# than being "whatever the payload happened to carry". The first three belong
# to the pipeline; the rest are workflow #3's (config/actions-dialogue.json),
# whose node has no registry story to read a task, a cwd or a round from.
PROMPT_VARS = ("story_text", "story_url", "reconcile_base",
               "task", "task_text", "cwd", "feedback",
               "iteration", "surface_prev", "closing")

_state = {}


def mode() -> str:
    """off | shadow | only, from `CADRE_ENGINE`.

    An unrecognised truthy value reads as `shadow`, not `only`: shadow is the
    setting where a misconfiguration costs a duplicate spawn that gets dropped,
    and `only` is the one where it costs a story that nobody dispatches.
    """
    raw = (os.environ.get("CADRE_ENGINE") or "").strip().lower()
    if raw in ("", "0", "off", "false", "no"):
        return "off"
    return "only" if raw == "only" else "shadow"


def enabled() -> bool:
    return mode() != "off"


def key_for(slug: str) -> str:
    return f"story:{slug}"


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
        # The gh-watch action shells out; a subprocess inherits the environment
        # and nothing else. Without this it would re-load config.toml and aim at
        # the DEFAULT data dir — a watcher writing to a different board than the
        # engine reads is silent, total failure (and, from a test, a write into
        # the operator's live state).
        os.environ["CADRE_DATA_DIR"] = str(cfg.data_dir)
        seed_nodes.seed(cfg.data_dir, cfg)      # idempotent; see seed_nodes
        tasks.seed(cfg.data_dir, cfg)           # workflow #3's nodes
        board = Board(path)
        actions = engine.load_action_set(ACTIONS_PATHS)
        start_new_actions_at_head(board, actions)
        _state[path] = {"board": board, "nodes": Nodes(cfg.data_dir),
                        "actions": actions}
    return _state[path]


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


def tick_pass(cfg, reg, ghc, log, run_stage) -> dict:
    """One engine pass. `run_stage` is passed in rather than imported: the seam
    must not import pipeline.py (which imports the seam), and the daemon owns
    the spawn machinery either way."""
    st = state(cfg)
    board, actions = st["board"], st["actions"]
    # The node registry is re-opened every pass, never cached: `Nodes.__init__`
    # reads index.json once and holds it, so a cached handle in a daemon that
    # runs for weeks would pin every node's command and active prompt version
    # at daemon-start — a CLI re-seed or an explicit `promote` would not reach
    # traffic until a restart. One small JSON read per pass buys the registry's
    # own contract back: promotion is what puts a version in front of traffic.
    nodes = Nodes(cfg.data_dir)

    # Turns that were handed work while a turn of theirs was still running.
    run_deferred(cfg, reg, board, log, run_stage)

    for slug, story in reg.data["stories"].items():
        adopt_story(board, slug, story)
    heartbeat(board)

    spawns, firings = engine.tick(board, actions, nodes, cfg.data_dir)
    for firing in firings:
        if firing["outcome"] != "fired":
            log(f"engine: action {firing['action']} {firing['outcome']}"
                + (f" — {firing.get('error')}" if firing.get("error") else ""))
    for spec in spawns:
        try:
            run_spawn_spec(cfg, reg, ghc, board, log, run_stage, spec)
        except Exception as e:
            # A spawn that cannot start is a failed signal like any other, so
            # the stage-failed action sees it instead of it dying in a log line.
            ev = board.get(spec["event_id"]) or {}
            # The failure belongs on the topic the spawn came from, so the
            # workflow that owns the key is the one that can see it.
            topic = ev.get("topic") if ev.get("topic") == TASKS_TOPIC else STAGES_TOPIC
            board.write(NAMESPACE, topic, spec["key"], "signal",
                        {"status": "failed", "stage": spec["node"],
                         "story": (ev.get("payload") or {}).get("story", ""),
                         "error": f"{type(e).__name__}: {e}"[:300]},
                        correlation_id=spec.get("correlation_id"))
            log(f"engine: spawn of {spec['node']} failed — {e}")
    return {"spawns": spawns, "firings": firings}


def heartbeat(board: Board) -> dict:
    """The daemon's per-pass tick. The gh-watch action triggers on it — polling
    frequency is therefore the daemon's business, not the action's."""
    return board.write(NAMESPACE, RUNNER_TOPIC, "daemon", "heartbeat",
                       {"at": time.time()})


def adopt_story(board: Board, slug: str, story: dict) -> list[dict]:
    """Put a registered story on the board exactly once.

    A story the legacy path has already carried past intake is ADOPTED, not
    restarted: the state marker is written first so the intake action's
    `no_event_for_key` guard sees a stage signal and stays quiet. Without it,
    turning the flag on mid-story would re-run S0 on a live feature branch.
    """
    key = key_for(slug)
    if board.peek(namespace=NAMESPACE, topic=STORIES_TOPIC, key=key,
                  kind="command", limit=1):
        return []
    out = []
    past_intake = bool(story.get("planning_pr")) or story.get("status") != "intaking"
    if past_intake:
        out.append(board.write(
            NAMESPACE, STAGES_TOPIC, key, "signal",
            {"status": "progress", "stage": "intake", "story": slug,
             "phase": story.get("phase", "interrogate"),
             "note": "adopted from the registry — intake already ran"}))
    out.append(board.write(
        NAMESPACE, STORIES_TOPIC, key, "command",
        {"target": "stage:intake", "story": slug, "repo": story.get("repo", ""),
         "title": story.get("title", ""), "variant": story.get("variant", ""),
         "slice": "", "pr": story.get("planning_pr") or ""}))
    return out


def intake_command(cfg, slug: str, story: dict, story_text: str,
                   story_url: str = "") -> dict | None:
    """Story INTAKE, board-driven: the story's own text rides the `command`
    event that S0 triggers on.

    This is the write `adopt_story` cannot make. Adoption reconstructs a command
    from the registry, and the registry has never held the story text — it lives
    in the tracker card or in `start --story`, so an adopted story's intake prompt
    renders with an empty `$story_text`. Writing the command at the point the
    story ENTERS (cmd_start / _board_intake) is what closes that gap, and it also
    shuts `adopt_story` for this story: the command already exists.

    Returns None with the flag off, so the intake paths can call it flatly.
    """
    if not enabled():
        return None
    board = state(cfg)["board"]
    key = key_for(slug)
    if board.peek(namespace=NAMESPACE, topic=STORIES_TOPIC, key=key,
                  kind="command", limit=1):
        return None
    return board.write(
        NAMESPACE, STORIES_TOPIC, key, "command",
        {"target": "stage:intake", "story": slug, "repo": story.get("repo", ""),
         "title": story.get("title", ""), "variant": story.get("variant", ""),
         "slice": "", "pr": "", "story_text": story_text,
         "story_url": story_url or (story.get("board") or {}).get("url", "")})


# --------------------------------------------------------------------------- spawn


def run_spawn_spec(cfg, reg, ghc, board: Board, log, run_stage, spec: dict):
    """Execute one engine spawn spec through `_run_stage`.

    The spec names the node and the version; the triggering event carries the
    story, slice and PR. `prompt_template` and `argv` are what make the registry
    authoritative at runtime: `_run_stage` renders THAT text with the run's
    variables instead of re-reading prompts/*.md, and runs THAT command instead
    of composing one, so a promoted prompt version reaches the session, the
    firing can be traced back to the exact text, and the agent CLI is the node's
    business rather than the runner's.
    """
    event = board.get(spec["event_id"]) or {}
    payload = event.get("payload") or {}
    if event.get("topic") == TASKS_TOPIC:
        # Workflow #3. Told apart by the TOPIC the trigger fired on rather than
        # by the node's name, because "which workflow is this" is a property of
        # the board, and a second dialogue node later must not need a second
        # branch here.
        return run_task_spawn(cfg, reg, board, log, run_stage, spec, payload)
    slug = payload.get("story") or spec["key"].split(":", 1)[-1]
    story = reg.data["stories"].get(slug)
    if story is None:
        raise RuntimeError(f"spawn for unknown story {slug!r}")
    pr = payload.get("pr")
    if refuse_past_cap(cfg, board, log, spec, slug, pr):
        return
    action = {
        "type": "run_stage",
        "stage": spec["node"],
        "slice": payload.get("slice") or None,
        "pr": int(pr) if str(pr or "").isdigit() else None,
        "role": payload.get("role", ""),
        "model": spec["model"],
        "effort": None,          # the node's command carries it; config still wins
        "prompt_template": Path(spec["prompt_path"]).read_text(),
        # The node's own command, formatted by the registry. `_run_stage` hands
        # it to `runs.spawn` as the argv to execute, so what the registry says
        # invokes this node is what invokes it — swapping the agent CLI is an
        # edit to the node, not to this repo.
        "argv": spec.get("argv"),
        "node_version": spec["version"],
        "firing_id": spec.get("firing_id"),
        # Whatever prompt variables the triggering event chose to carry. NOTE:
        # `story_text` is one the registry cannot supply — it lives in the
        # tracker card or the `start --story` argument, never in the story
        # record — so an ADOPTED story's intake prompt would render without it.
        # Only a story whose intake command was written with the text attached
        # gets a complete S0 prompt on this path.
        "extra_vars": {k: v for k, v in payload.items()
                       if k in PROMPT_VARS},
    }
    log(f"engine: spawning {spec['node']} for {slug} "
        f"(node version {spec['version'][:12]}, firing {spec.get('firing_id')})")
    run_stage(cfg, reg, ghc, slug, story, action)


def run_task_spawn(cfg, reg, board: Board, log, run_stage, spec: dict, payload: dict):
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
    first = payload.get("target") == tasks.TARGET_REQUEST
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
    # `story` is None on purpose: there is no story record and inventing one
    # would put a repo-less row in front of `_poll_story` every pass.
    run_stage(cfg, reg, None, task_id, None, action)


def run_deferred(cfg, reg, board: Board, log, run_stage) -> int:
    """Start the turns `run_task_spawn` parked because their task was busy,
    for every task whose running turn has since been reaped. One per task per
    pass: the first one started makes the task busy again."""
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
            run_task_spawn(cfg, reg, board, log, run_stage, spec, payload)
            started += 1
        except Exception as e:
            board.write(NAMESPACE, TASKS_TOPIC, spec["key"], "signal",
                        {"status": "failed", "stage": spec["node"], "story": task_id,
                         "error": f"{type(e).__name__}: {e}"[:300]},
                        correlation_id=spec.get("correlation_id"))
            log(f"engine: deferred {spec['node']} for {task_id} failed — {e}")
    return started


def handoff_command(cfg, task_id: str) -> str:
    """The shell line a session runs to hand its task to another node: the
    same `task:handoff` event the verdict form writes, through the same
    `tasks.write_handoff`. `<node>` is the one placeholder the agent fills."""
    conf = getattr(cfg, "path", None)
    parts = [sys.executable, str(PIPELINE)]
    if conf:
        parts += ["--config", str(conf)]
    parts += ["handoff", task_id, "--node"]
    return " ".join(shlex.quote(p) for p in parts) + \
        ' <node> --note "what the next node should do with this"'


# --------------------------------------------------------------------------- round caps


def max_rounds(cfg) -> int:
    return (getattr(cfg, "limits", None) or {}).get(
        "max_rounds_per_stage", DEFAULT_MAX_ROUNDS)


def prior_firings(board: Board, action_name: str, key: str, pr=None,
                  exclude_firing=None) -> int:
    """How many times this action has already fired for this story — and, when
    the stage's cap is per-PR, for this PR.

    The firing log IS the counter. The engine keeps no state of its own, so
    "have we done this N times" can only be asked of the log, and asking it of
    the log means a restarted daemon counts the same rounds a running one does.
    """
    rows = [r for r in board.firings(action=action_name, key=key, limit=1000)
            if r["outcome"] == "fired" and r["id"] != exclude_firing]
    if not pr:
        return len(rows)
    n = 0
    for r in rows:
        ev = board.get(r["event_id"]) or {}
        if str((ev.get("payload") or {}).get("pr", "")) == str(pr):
            n += 1
    return n


def refuse_past_cap(cfg, board: Board, log, spec: dict, slug: str, pr) -> bool:
    """The interim round cap (engine-only mode). True when the spawn is refused.

    `dispatcher.dispatch` turns "rounds >= max_rounds_per_stage" into an
    `escalate` action, and engine-only mode does not run it for spawns. The
    closed `where` vocabulary has no count predicate, so the check lands here —
    the one place every engine spawn passes through — and the refusal is an
    `escalate` event on the board rather than a silent drop, because a stage
    that stops looping without saying so is the failure this cap exists to
    prevent.
    """
    if mode() != "only" or spec["node"] not in ROUND_CAPPED:
        return False
    cap = max_rounds(cfg)
    # interrogate's cap is per story; revise and reconcile are per PR.
    scope_pr = None if spec["node"] == "interrogate" else pr
    rounds = prior_firings(board, spec["action"], spec["key"], scope_pr,
                           exclude_firing=spec.get("firing_id"))
    if rounds < cap:
        return False
    board.write(NAMESPACE, STAGES_TOPIC, spec["key"], "escalate",
                {"reason": f"{spec['node']} hit {rounds} rounds (cap {cap})"
                           + (f" on PR #{pr}" if scope_pr else ""),
                 "story": slug, "stage": spec["node"], "pr": pr or "",
                 "action": spec["action"]},
                correlation_id=spec.get("correlation_id"))
    log(f"engine: {spec['node']} for {slug}"
        + (f" PR #{pr}" if scope_pr else "")
        + f" REFUSED — {rounds} prior rounds (cap {cap}); escalated to the driver")
    return True


# --------------------------------------------------------------------------- completion


def stage_finished(cfg, slug: str, run: dict, ok: bool) -> dict | None:
    """The reap-side write: a stage session that ended puts its own completion
    on the board. This is the event the transition actions wait on — the whole
    stage graph hangs off it, so it is written for BOTH outcomes (`failed` is
    what the stage-failed action reads).

    Returns None when the flag is off, so pipeline.py's reap loop can call it
    unconditionally.
    """
    if not enabled():
        return None
    board = state(cfg)["board"]
    return board.write(
        NAMESPACE, STAGES_TOPIC, key_for(slug), "signal",
        {"status": "finished" if ok else "failed",
         "stage": run["stage"], "story": slug,
         "slice": run.get("slice") or "", "pr": run.get("pr") or "",
         "run_id": run.get("run_dir", "").rsplit("/", 1)[-1],
         "node_version": (run.get("action") or {}).get("node_version", "")})
