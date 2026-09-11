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
import time
from pathlib import Path

from . import engine, seed_nodes
from .blackboard import Board
from .nodes import Nodes

ACTIONS_PATH = Path(__file__).resolve().parent.parent / "config" / "actions-pipeline.json"

NAMESPACE = "cadre"
STORIES_TOPIC = "stories"
STAGES_TOPIC = "stages"
RUNNER_TOPIC = "runner"

MODES = ("off", "shadow", "only")

# Stages whose legacy cap is a per-story (interrogate) or per-PR (revise,
# reconcile) round count. In engine-only mode nothing else counts them.
ROUND_CAPPED = ("interrogate", "revise", "reconcile")
DEFAULT_MAX_ROUNDS = 5          # mirrors config.DEFAULTS["limits"]

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
        _state[path] = {
            "board": Board(path),
            "nodes": Nodes(cfg.data_dir),
            "actions": engine.load_actions(ACTIONS_PATH),
        }
    return _state[path]


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
    board, nodes, actions = st["board"], st["nodes"], st["actions"]

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
            board.write(NAMESPACE, STAGES_TOPIC, spec["key"], "signal",
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
    in the Linear card or in `start --story`, so an adopted story's intake prompt
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
    story, slice and PR. `prompt_template` is what makes the registry
    authoritative at runtime: `_run_stage` renders THAT text with the run's
    variables instead of re-reading prompts/*.md, so a promoted prompt version
    reaches the session and the firing can be traced back to the exact text.
    """
    event = board.get(spec["event_id"]) or {}
    payload = event.get("payload") or {}
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
        "node_version": spec["version"],
        "firing_id": spec.get("firing_id"),
        # Whatever prompt variables the triggering event chose to carry. NOTE:
        # `story_text` is one the registry cannot supply — it lives in the
        # Linear card or the `start --story` argument, never in the story
        # record — so an ADOPTED story's intake prompt would render without it.
        # Only a story whose intake command was written with the text attached
        # gets a complete S0 prompt on this path.
        "extra_vars": {k: v for k, v in payload.items()
                       if k in ("story_text", "story_url", "reconcile_base")},
    }
    log(f"engine: spawning {spec['node']} for {slug} "
        f"(node version {spec['version'][:12]}, firing {spec.get('firing_id')})")
    run_stage(cfg, reg, ghc, slug, story, action)


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
