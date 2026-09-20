"""The action engine: trigger → optional body → emitter, over the blackboard.

An action is configuration, not code. The engine is generic: it knows the
three parts, a tiny closed condition vocabulary, two body types and four
emitter types — and nothing about stories, PRs, GitHub or any particular
node. External systems are reached by an action whose body or emitter runs a
CLI (`gh`, `git`); there is no adapter component.

There is no workflow file at runtime. The workflow IS the set of installed
actions, and nodes are unaware of each other — a routine is just the set of
actions someone installed together.

Why each piece is the way it is (cadre-context/blackboard/decisions/):

  2026-09-02-vocabulary-action-not-pipe
      The fixed words: action · trigger · body · emitter · firing. "Action"
      never names the emitter alone.

  2026-08-31-action-model-and-communication-types
      Three parts, with the body as the place for author logic (choose a
      target, run an agent or system). Emitters deliver: in-tray, a new
      event, an outbound call, or a human on HITL. Routing is per-action
      configuration, never a global rule.

  2026-09-02-engine-calls-round3
      Trigger conditions may read the board's CURRENT STATE, not just the
      event that fired them (the "summon on a closed PR" lesson: the kind
      matched, the current state made it meaningless). Hence the
      `no_event_for_key` condition. Writes are type-checked by the board, so
      a malformed emitter fails at the write.

  2026-09-02-engine-calls-round4
      The engine lives in the library so every consumer gets identical
      behaviour and one firing log. Loop-backs are not a special pattern:
      failure is a `signal: failed` event and triage is an ordinary action.
      Cycle safety is the firing log's causal depth cap, not a construct.

The `where` vocabulary is closed on purpose — there is no `eval`, no
expression language, and an unknown operator is rejected when the action is
loaded rather than the first time it fires:

    {"op": "payload_eq",       "field": F, "value": V}
        the trigger event's payload[F] == V
    {"op": "payload_in",       "field": F, "values": [...]}
        the trigger event's payload[F] is one of the values
    {"op": "no_event_for_key", "kind": K, "namespace"?, "topic"?, "key"?}
        the board carries no event matching that filter. `key` defaults to
        the trigger event's key; namespace/topic default to the trigger
        event's. This is the board-state check ("nobody has answered / claimed
        / finished this yet"). In v0 nothing is ever removed from the board,
        so "no open event" reduces to "no such event".

Conditions in a list are ANDed. That is the whole language.
"""

import json
import subprocess
import time
from collections import namedtuple
from pathlib import Path

from .nodes import NodeError

# Cycle safety: refuse to fire deeper than this in one causal chain.
DEFAULT_MAX_DEPTH = 16

WHERE_OPS = ("payload_eq", "payload_in", "no_event_for_key")
BODY_TYPES = ("spawn_node", "run_command")
EMITTER_TYPES = ("write_event", "in_tray", "run_command", "hitl")

HITL_OUTBOX = "hitl-outbox.jsonl"
DEFAULT_TIMEOUT = 300

TickResult = namedtuple("TickResult", "spawns firings")


class ActionError(Exception):
    """A bad action definition. Rejected at load, not at the first firing."""


class ActionFailed(Exception):
    """A body or emitter that failed at runtime. Becomes a `signal: failed`."""


# --------------------------------------------------------------------------- loading


def load_actions(path) -> list[dict]:
    """Load and validate actions from a JSON file (a list, or {"actions": [...]}).

    JSON rather than YAML: stdlib only, and an installed action is written by
    an agent as often as by a human.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise ActionError(f"no actions file at {path}")
    except json.JSONDecodeError as e:
        raise ActionError(f"{path}: not valid JSON ({e})") from e
    if isinstance(raw, dict):
        raw = raw.get("actions", [])
    if not isinstance(raw, list):
        raise ActionError(f"{path}: expected a list of actions")
    return [validate_action(a) for a in raw]


def load_action_set(paths) -> list[dict]:
    """Several action FILES, one action SET — which is what a tick consumes.

    A workflow is a file (`actions-pipeline.json`, `actions-dialogue.json`);
    the engine runs all of them at once, because the board is the only coupling
    and an action does not know which file it came from. Concatenation, not a
    merge: nothing is combined, overridden or reordered, so the set is exactly
    the files read in order.

    The one thing concatenation can break is the consumer cursor, which the
    board keeps per ACTION NAME. Two files using the same name would share one
    cursor and hide events from each other — silently, and only under load. So
    a name collision across files is refused here, at load, naming both files.
    """
    out, seen = [], {}
    for path in paths:
        for action in load_actions(path):
            name = action["name"]
            if name in seen:
                raise ActionError(
                    f"duplicate action name {name!r} in {Path(path).name} — "
                    f"already defined in {seen[name]}; action names are the "
                    f"board's consumer cursors and must be unique across files")
            seen[name] = Path(path).name
            out.append(action)
    return out


def validate_action(action: dict) -> dict:
    """Everything checkable without a board, checked once, up front."""
    if not isinstance(action, dict):
        raise ActionError("an action must be an object")
    name = action.get("name")
    if not name or not isinstance(name, str):
        raise ActionError("action name is required")
    trigger = action.get("trigger")
    if not isinstance(trigger, dict):
        raise ActionError(f"action {name!r}: trigger is required")
    for field in trigger:
        if field not in ("namespace", "topic", "key", "kind", "correlation_id", "where"):
            raise ActionError(f"action {name!r}: unknown trigger field {field!r}")
    for cond in _conditions(trigger.get("where")):
        if not isinstance(cond, dict) or cond.get("op") not in WHERE_OPS:
            raise ActionError(
                f"action {name!r}: unknown where operator "
                f"{(cond or {}).get('op')!r} (the closed set: {', '.join(WHERE_OPS)})")
        if cond["op"] in ("payload_eq", "payload_in") and not cond.get("field"):
            raise ActionError(f"action {name!r}: {cond['op']} needs a field")
        if cond["op"] == "payload_in" and not isinstance(cond.get("values"), list):
            raise ActionError(f"action {name!r}: payload_in needs a values list")
        if cond["op"] == "no_event_for_key" and not cond.get("kind"):
            raise ActionError(f"action {name!r}: no_event_for_key needs a kind")

    body = action.get("body")
    if body is not None:
        if not isinstance(body, dict) or body.get("type") not in BODY_TYPES:
            raise ActionError(
                f"action {name!r}: unknown body type {(body or {}).get('type')!r} "
                f"(the set: {', '.join(BODY_TYPES)})")
        if body["type"] == "spawn_node" and not body.get("node"):
            raise ActionError(f"action {name!r}: spawn_node needs a node name")
        if body["type"] == "run_command" and not isinstance(body.get("argv"), list):
            raise ActionError(f"action {name!r}: run_command needs an argv list")

    emitters = action.get("emitter")
    emitters = emitters if isinstance(emitters, list) else [emitters]
    for em in emitters:
        if not isinstance(em, dict) or em.get("type") not in EMITTER_TYPES:
            raise ActionError(
                f"action {name!r}: unknown emitter type {(em or {}).get('type')!r} "
                f"(the set: {', '.join(EMITTER_TYPES)})")
        if em["type"] == "write_event" and not em.get("kind"):
            raise ActionError(f"action {name!r}: write_event needs a kind")
        if em["type"] == "in_tray" and not em.get("agent"):
            raise ActionError(f"action {name!r}: in_tray needs an agent")
        if em["type"] == "run_command" and not isinstance(em.get("argv"), list):
            raise ActionError(f"action {name!r}: run_command needs an argv list")
        if em["type"] == "hitl" and not em.get("summary"):
            raise ActionError(f"action {name!r}: hitl needs a summary")

    depth = action.get("max_depth", DEFAULT_MAX_DEPTH)
    if not isinstance(depth, int) or depth < 1:
        raise ActionError(f"action {name!r}: max_depth must be a positive integer")
    return action


# --------------------------------------------------------------------------- the tick


def tick(board, actions: list[dict], nodes=None, data_dir=None) -> TickResult:
    """One pass: for every action, read what it has not seen and fire on matches.

    Returns the spawn specs produced this pass (the daemon's runner executes
    them — the engine never forks an agent) and the firing rows written.

    Each action is its own consumer on the board: one observed cursor per
    action name, kept by the board. Actions are independent, so a new action
    added later starts from the board's current head and one action can never
    hide an event from another.
    """
    spawns, firings = [], []
    for action in actions:
        trigger = action["trigger"]
        events = board.read_since(
            action["name"], namespace=trigger.get("namespace"),
            topic=trigger.get("topic"), key=trigger.get("key"),
            kind=trigger.get("kind"), correlation_id=trigger.get("correlation_id"))
        for event in events:
            if not _where_holds(board, trigger.get("where"), event):
                continue
            spawn, firing = _fire(board, action, event, nodes, data_dir)
            if spawn:
                spawns.append(spawn)
            firings.append(firing)
    return TickResult(spawns, firings)


def _fire(board, action: dict, event: dict, nodes, data_dir):
    name = action["name"]
    depth = board.causal_depth(event["id"]) + 1
    cap = action.get("max_depth", DEFAULT_MAX_DEPTH)
    if depth > cap:
        # Cycle safety per round 4: the chain is cut here and the refusal is
        # visible in the firing log, rather than a loop nobody can see.
        fid = board.log_firing(name, event["id"], depth=depth, outcome="capped",
                               detail=f"causal depth {depth} exceeds max_depth {cap}")
        return None, {"id": fid, "action": name, "event_id": event["id"],
                      "depth": depth, "outcome": "capped"}

    try:
        result = _run_body(action.get("body"), event, nodes, action)
        emitted_id = None
        for em in _emitters(action):
            written = _run_emitter(board, em, event, result, data_dir)
            emitted_id = emitted_id or written
    except Exception as e:                       # a failure is just a signal
        failure = _write_failure(board, action, event, e)
        fid = board.log_firing(name, event["id"], depth=depth, outcome="failed",
                               emitted_event_id=failure["id"],
                               detail=f"{type(e).__name__}: {e}")
        return None, {"id": fid, "action": name, "event_id": event["id"],
                      "depth": depth, "outcome": "failed", "error": str(e),
                      "emitted_event_id": failure["id"]}

    fid = board.log_firing(name, event["id"], depth=depth, outcome="fired",
                           emitted_event_id=emitted_id)
    firing = {"id": fid, "action": name, "event_id": event["id"], "depth": depth,
              "outcome": "fired", "emitted_event_id": emitted_id}
    spawn = result if isinstance(result, dict) and result.get("kind") == "spawn" else None
    if spawn:
        spawn["firing_id"] = fid
    return spawn, firing


def _write_failure(board, action: dict, event: dict, error: Exception) -> dict:
    """Failure is a `signal: failed` on the board — triage is another action
    (round 4). Written on the trigger event's route so the failure lands where
    the work was, carrying the same correlation thread."""
    return board.write(event["namespace"], event["topic"], event["key"], "signal",
                       {"status": "failed", "action": action["name"],
                        "error": f"{type(error).__name__}: {error}",
                        "trigger_event": event["id"]},
                       correlation_id=_correlation(event))


# --------------------------------------------------------------------------- trigger


def _conditions(where):
    if where is None:
        return []
    return where if isinstance(where, list) else [where]


def _where_holds(board, where, event: dict) -> bool:
    for cond in _conditions(where):
        op = cond.get("op")
        if op == "payload_eq":
            if event["payload"].get(cond["field"]) != cond.get("value"):
                return False
        elif op == "payload_in":
            if event["payload"].get(cond["field"]) not in cond["values"]:
                return False
        elif op == "no_event_for_key":
            found = board.peek(namespace=cond.get("namespace", event["namespace"]),
                               topic=cond.get("topic", event["topic"]),
                               key=cond.get("key", event["key"]),
                               kind=cond["kind"], limit=1)
            if found:
                return False
        else:
            # Unreachable via load_actions; reachable if an action was built
            # in-process without validation.
            raise ActionError(f"unknown where operator {op!r}")
    return True


# --------------------------------------------------------------------------- body


def _run_body(body, event: dict, nodes, action: dict):
    if body is None:
        return None
    if body["type"] == "spawn_node":
        if nodes is None:
            raise ActionFailed("spawn_node needs a node registry")
        try:
            node = nodes.active(body["node"])
            argv = nodes.command_argv(body["node"], event=event, node=node)
        except NodeError as e:
            raise ActionFailed(str(e)) from e
        return {"kind": "spawn", "action": action["name"], "node": node["name"],
                "version": node["version"], "model": node["model"],
                "prompt_path": node["prompt_path"], "argv": argv,
                "cwd": body.get("cwd"), "event_id": event["id"],
                "key": event["key"], "correlation_id": _correlation(event)}
    if body["type"] == "run_command":
        return _run_command(body, event, None)
    raise ActionFailed(f"unknown body type {body['type']!r}")


def _run_command(spec: dict, event: dict, result) -> dict:
    """A saved argv list — this is how the `gh` / `git` edges work. Never a
    shell string: the argv is substituted token by token, so a payload value
    with a space stays one argument and there is nothing for a shell to parse."""
    argv = [_fmt(tok, event, result) for tok in spec["argv"]]
    proc = subprocess.run(argv, cwd=spec.get("cwd"), text=True, capture_output=True,
                          timeout=spec.get("timeout", DEFAULT_TIMEOUT))
    out = {"kind": "command", "argv": argv, "returncode": proc.returncode,
           "stdout": (proc.stdout or "").strip(), "stderr": (proc.stderr or "").strip()}
    if proc.returncode != 0 and not spec.get("allow_failure"):
        # A failed `gh pr merge` must not read as a successful firing; it
        # becomes a failed signal like any other body error.
        raise ActionFailed(
            f"{argv[0]} exited {proc.returncode}: {out['stderr'][-500:] or out['stdout'][-500:]}")
    return out


# --------------------------------------------------------------------------- emitters


def _emitters(action: dict) -> list[dict]:
    em = action["emitter"]
    return em if isinstance(em, list) else [em]


def _run_emitter(board, em: dict, event: dict, result, data_dir) -> str | None:
    """Returns the id of the event it wrote, if it wrote one."""
    kind = em["type"]
    if kind == "write_event":
        written = board.write(
            _fmt(em.get("namespace", event["namespace"]), event, result),
            _fmt(em.get("topic", event["topic"]), event, result),
            _fmt(em.get("key", event["key"]), event, result),
            em["kind"], _fmt(em.get("payload", {}), event, result),
            correlation_id=_correlation(event),
            visible_after=(time.time() + em["delay"]) if em.get("delay") else None)
        return written["id"]
    if kind == "in_tray":
        # Targeted work: an event on the agent's own key, which the agent takes
        # with a claimed read. `command` locks in a prompt or skill so the
        # action can invoke another agent without giving it a choice.
        agent = _fmt(em["agent"], event, result)
        written = board.write(
            _fmt(em.get("namespace", event["namespace"]), event, result),
            _fmt(em.get("topic", "in-tray"), event, result),
            agent, em.get("kind", "command"),
            _fmt(em.get("payload", {}), event, result),
            correlation_id=_correlation(event))
        return written["id"]
    if kind == "run_command":
        _run_command(em, event, result)
        return None
    if kind == "hitl":
        # The surface channel picks this up later; the engine only appends.
        path = Path(data_dir or ".") / HITL_OUTBOX
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "summary": _fmt(em["summary"], event, result),
                  "key": event["key"], "event_id": event["id"],
                  "correlation_id": _correlation(event),
                  "detail": _fmt(em.get("detail", {}), event, result)}
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
        return None
    raise ActionFailed(f"unknown emitter type {kind!r}")


# --------------------------------------------------------------------------- helpers


def _correlation(event: dict) -> str:
    """Threads are carried, never dropped: an event already in a thread keeps
    its correlation_id, and one that starts a thread becomes its own root."""
    return event.get("correlation_id") or event["id"]


def _fmt(value, event: dict, result):
    """Substitute `{event[key]}`, `{payload[story]}`, `{body[stdout]}` into
    strings, recursively through lists and dicts. Non-strings pass through."""
    if isinstance(value, str):
        try:
            return value.format(event=event, payload=event.get("payload", {}),
                                body=result or {})
        except (KeyError, IndexError) as e:
            raise ActionFailed(f"template wants {e}, which this event does not carry")
    if isinstance(value, list):
        return [_fmt(v, event, result) for v in value]
    if isinstance(value, dict):
        return {k: _fmt(v, event, result) for k, v in value.items()}
    return value
