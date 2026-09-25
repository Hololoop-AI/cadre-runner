"""Workflow #3: a dialogue over the surface, expressed as data.

The seam this suite guards is the one the pipeline suite guards for workflow #1
— "the workflow IS the actions config" — plus the one claim this workflow makes
that no other does: it reaches GitHub nowhere. Board, surfaces and sessions
only. So the checks are: the config validates against the closed vocabulary, its
three triggers fire on synthetic events and on nothing else, `submit_task`
writes an event the engine actually consumes, and the CLI's `task` subcommand is
wired to that function rather than to a second, divergent write.

No claude, no gh, no network, no review-surface CLI.

Run directly: python3 tests/test_dialogue_actions.py
"""

import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import config as config_mod
from runnerlib import engine, engine_seam, preflight, surface, tasks
from runnerlib.blackboard import Board
from runnerlib.nodes import Nodes

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ROOT / "config" / "actions-dialogue.json"
PIPELINE_ACTIONS = ROOT / "config" / "actions-pipeline.json"
PROMPTS = ROOT / "prompts"


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


def board(d: Path) -> Board:
    return Board(d / "board.db")


def seeded(d: Path) -> Nodes:
    tasks.seed(d)
    return Nodes(d)


def actions() -> list[dict]:
    return engine.load_actions(ACTIONS)


def by_name(name: str) -> dict:
    return next(a for a in actions() if a["name"] == name)


class FakeCfg:
    """Enough config for `submit_task` and for a task spawn: a data dir, the
    node pins, and the runner dials the spawn path reads."""

    def __init__(self, d):
        self.data_dir = Path(d)
        self.claude = {**config_mod.DEFAULTS["claude"], "bin": "claude"}
        self.runner = {**config_mod.DEFAULTS["runner"], "max_concurrent_runs": 4}
        self.limits = {"max_rounds_per_stage": 5}

    def model_for(self, node):
        return "opus"

    def effort_for(self, node):
        return "high"


def request(b, task_id="task-demo", **payload):
    return b.write("cadre", "tasks", tasks.key_for(task_id), "command",
                   {"target": tasks.TARGET_REQUEST, "task": task_id,
                    "story": task_id, "title": "demo", "task_text": "do the thing",
                    "cwd": "/tmp/work", "iteration": "1", **payload})


# --------------------------------------------------------------------------- config


def test_actions_dialogue_loads_and_validates():
    loaded = actions()
    names = [a["name"] for a in loaded]

    assert len(names) == len(set(names)), "action names must be unique"
    for a in loaded:
        assert engine.validate_action(a) is a
    assert set(names) == {"task-requested", "task-feedback-resumes-the-session",
                          "task-approved-done"}

    # the whole workflow spawns exactly one node, and it is the one the seed
    # installs — a config naming a node nobody registers is a spawn that fails
    # minutes later inside the engine
    spawned = {a["body"]["node"] for a in loaded
               if (a.get("body") or {}).get("type") == "spawn_node"}
    assert spawned == {tasks.NODE}


def test_the_dialogue_reaches_github_nowhere():
    """The claim that makes this workflow the agnosticism proof: no gh, no PRs,
    no repo — not in the config, and not in the node's prompt either."""
    import json

    def executable(node):
        """The config minus its prose: `_comment` and `_why` may SAY github."""
        if isinstance(node, dict):
            return {k: executable(v) for k, v in node.items()
                    if not k.startswith("_")}
        if isinstance(node, list):
            return [executable(v) for v in node]
        return node

    text = json.dumps(executable(json.loads(ACTIONS.read_text()))).lower()
    for token in ("github", "gh ", "pull request", "merge", '"pr"', "[pr]", "repo"):
        assert token not in text, token

    # the prompt may say the word while ruling it out ("no GitHub anywhere"),
    # so what it is held to is the instruction: nothing tells the session to
    # reach a repo host
    prompt = (PROMPTS / "task.md").read_text().lower()
    for token in ("`gh ", "gh pr", "planning pr", "pr #", "open a pr"):
        assert token not in prompt, token

    # and nothing shells out at all: the only body type here is spawn_node
    assert {(a.get("body") or {}).get("type") for a in actions()} == {"spawn_node", None}


# --------------------------------------------------------------------------- the walk


def test_a_task_request_spawns_the_task_node_once():
    d = scratch()
    b, n, acts = board(d), seeded(d), actions()

    request(b)
    spawns, firings = engine.tick(b, acts, n, d)

    assert [s["node"] for s in spawns] == ["task"]
    assert spawns[0]["key"] == "task:task-demo"
    assert spawns[0]["version"] == n.active("task")["version"]
    # the task id reaches the spawn through the event key, never as a claude
    # flag — claude has no --story, and the text rides inside the prompt
    assert "--story" not in spawns[0]["argv"]
    assert [f["outcome"] for f in firings] == ["fired"]

    started = b.peek(topic="tasks", kind="signal")
    assert len(started) == 1
    assert started[0]["payload"]["status"] == "started"
    assert started[0]["payload"]["cwd"] == "/tmp/work"

    # once-only: the started signal the emitter wrote closes the guard, so a
    # replayed or re-submitted ask cannot open a second session on this key
    request(b)
    assert engine.tick(b, acts, n, d).spawns == []

    # ...but a DIFFERENT task is its own key and its own dialogue
    request(b, task_id="task-other")
    assert [s["key"] for s in engine.tick(b, acts, n, d).spawns] == ["task:task-other"]


def test_surface_feedback_takes_the_next_turn_on_the_same_session():
    d = scratch()
    b, n, acts = board(d), seeded(d), actions()

    request(b)
    engine.tick(b, acts, n, d)                       # round 1 is running

    b.write("cadre", "tasks", tasks.key_for("task-demo"), "command",
            {"target": tasks.TARGET_FEEDBACK, "task": "task-demo",
             "story": "task-demo", "cwd": "/tmp/work", "iteration": "2",
             "feedback": "the second table is unreadable — fold it into prose",
             "resume": "session"})
    spawns, _ = engine.tick(b, acts, n, d)

    # the same node, on the same key: one dialogue, another turn
    assert [(s["node"], s["key"]) for s in spawns] == [("task", "task:task-demo")]

    # the driver's words and the round reach the prompt as $vars — without the
    # seam's passthrough they would be dropped and the session would restart
    # cold, which is the failure this workflow exists to avoid
    calls = []
    engine_seam.run_spawn_spec(FakeCfg(d), FakeReg(), None, b, lambda *a: None,
                               lambda *a: calls.append(a), spawns[0])
    action = calls[0][5]
    assert action["stage"] == "task"
    assert action["extra_vars"]["feedback"].startswith("the second table")
    assert action["extra_vars"]["iteration"] == "2"
    assert action["extra_vars"]["cwd"] == "/tmp/work"
    rendered = Template(action["prompt_template"]).safe_substitute(
        {"task": "task-demo", "max_rounds": 5, "task_text": "do the thing",
         **action["extra_vars"]})
    assert "the second table is unreadable" in rendered
    assert "$feedback" not in rendered and "$cwd" not in rendered


class FakeReg:
    """The registry a dialogue actually uses: no story row at all (the task
    spawn path never looks one up), and a `tasks` section it writes its session
    ids and run records into."""

    def __init__(self, stories=None):
        self.data = {"stories": dict(stories or {})}
        self.saves = 0

    def stories(self, status="active"):
        return {k: v for k, v in self.data["stories"].items()
                if v.get("status") == status}

    def save(self):
        self.saves += 1


def test_an_approve_verdict_marks_the_task_done():
    d = scratch()
    b, n, acts = board(d), seeded(d), actions()

    # a verdict that is not an approval is somebody else's business: continuing
    # IS a feedback turn, and the surface writes it as one
    b.write("cadre", "tasks", tasks.key_for("task-demo"), "command",
            {"target": tasks.TARGET_VERDICT, "task": "task-demo",
             "story": "task-demo", "verdict": "continue"})
    assert engine.tick(b, [by_name("task-approved-done")], n, d).firings == []

    b.write("cadre", "tasks", tasks.key_for("task-demo"), "command",
            {"target": tasks.TARGET_VERDICT, "task": "task-demo",
             "story": "task-demo", "verdict": "approve"})
    spawns, firings = engine.tick(b, acts, n, d)

    assert spawns == [], "approval ends the dialogue; nothing downstream fires"
    assert [f["outcome"] for f in firings] == ["fired"]
    done = [e for e in b.peek(topic="tasks", kind="signal")
            if e["payload"].get("phase") == "done"]
    assert len(done) == 1 and done[0]["payload"]["status"] == "finished"
    assert any(e["payload"]["message"] == "task task-demo approved — done"
               for e in b.peek(topic="tasks", kind="notify"))


# --------------------------------------------------------------------------- submit


def test_submit_task_writes_an_event_the_engine_consumes():
    d = scratch()
    cfg = FakeCfg(d)

    task_id = tasks.submit_task(cfg, "  tidy the exporter's flags  ",
                                cwd=d, title="tidy flags")
    assert task_id.startswith("task-tidy-flags-")

    # the node the request will spawn is installed by the submit itself: an ask
    # on a board whose registry has no `task` node fails inside the engine
    assert Nodes(d).active("task")["prompt"] == (PROMPTS / "task.md").read_text()

    b = Board(engine_seam.board_path(cfg))
    events = b.peek(namespace="cadre", topic="tasks", kind="command")
    assert len(events) == 1
    ev = events[0]
    assert ev["key"] == f"task:{task_id}"
    assert ev["payload"] == {"target": "task", "task": task_id, "story": task_id,
                             "title": "tidy flags",
                             "task_text": "tidy the exporter's flags",
                             "cwd": str(d), "iteration": "1"}

    # and it is a real trigger, not just a well-shaped row
    spawns, _ = engine.tick(b, actions(), Nodes(d), d)
    assert [s["node"] for s in spawns] == ["task"]

    try:
        tasks.submit_task(cfg, "   ")
        raise AssertionError("an empty ask must be refused at the CLI, not at spawn")
    except ValueError:
        pass


def test_seed_is_idempotent_and_prepends_no_pipeline_briefing():
    """_common.md is the PR-gated pipeline's briefing (gh CLI, never merge,
    never push to main). A dialogue node has no repo and no PR — prepending it
    would be instructions for a different world."""
    d = scratch()
    assert tasks.seed(d) == {"task": "registered", "implement": "registered"}
    assert tasks.seed(d) == {"task": "unchanged", "implement": "unchanged"}

    prompt = Nodes(d).active("task")["prompt"]
    assert (PROMPTS / "_common.md").read_text() not in prompt
    # the page path is freeform HTML via auto-surface — no component DSL
    assert "auto-surface" in prompt
    assert "$CADRE_SURFACE_OUT" in prompt

    # a changed prompt is RECORDED, never promoted (nodes.py's rule)
    tmp = scratch()
    (tmp / "task.md").write_text("a different task prompt")
    (tmp / "implement.md").write_text((PROMPTS / "implement.md").read_text())
    assert tasks.seed(d, prompts_dir=tmp) == {"task": "version-recorded",
                                              "implement": "unchanged"}
    assert Nodes(d).active("task")["prompt"] == prompt


# --------------------------------------------------------------------------- cli


def test_task_subcommand_submits_and_prints_where_to_look():
    import pipeline

    d = scratch()
    cfg = FakeCfg(d)

    class Args:
        text = "summarise the runner's config surface"
        cwd = str(d)
        title = None

    out = io.StringIO()
    with redirect_stdout(out):
        pipeline.cmd_task(cfg, Args())
    printed = out.getvalue().splitlines()

    task_id = printed[0].strip()
    assert task_id.startswith("task-summarise-the-runner")
    # the id, then where the driver will read the answer
    assert f"task-{task_id}.html" in printed[1]
    assert task_id in printed[2] and "cadre/tasks" in printed[2]

    b = Board(engine_seam.board_path(cfg))
    ev = b.peek(topic="tasks", kind="command")[0]
    assert ev["payload"]["task"] == task_id
    assert ev["payload"]["task_text"] == Args.text
    # the CLI is a caller of submit_task, not a second write path
    assert ev["payload"]["target"] == tasks.TARGET_REQUEST



def test_a_one_word_task_is_refused_and_writes_nothing():
    """`pipeline.py task list` read as "list the tasks" to the agent that typed
    it, and started a real agent session named "list" (2026-09-22). One word is
    refused before anything reaches the board, with a pointer to what exists."""
    import pipeline
    from contextlib import redirect_stderr

    d = scratch()
    cfg = FakeCfg(d)

    class Args:
        text, cwd, title, yes = "list", str(d), None, False

    err = io.StringIO()
    with redirect_stderr(err):
        try:
            pipeline.cmd_task(cfg, Args())
            raise AssertionError("a one-word task was accepted")
        except SystemExit as e:
            assert e.code == 2
    assert "pipeline.py status" in err.getvalue()
    assert "--yes" in err.getvalue()
    assert Board(engine_seam.board_path(cfg)).peek(topic="tasks", kind="command") == []


def test_a_one_word_task_goes_through_with_yes():
    import pipeline

    d = scratch()
    cfg = FakeCfg(d)

    class Args:
        text, cwd, title, yes = "lint", str(d), None, True

    with redirect_stdout(io.StringIO()):
        pipeline.cmd_task(cfg, Args())
    ev = Board(engine_seam.board_path(cfg)).peek(topic="tasks", kind="command")[0]
    assert ev["payload"]["task_text"] == "lint"


def test_status_lists_dialogue_tasks_on_a_runner_with_no_stories():
    """A dialogue-only runner used to print "no stories registered" — which is
    what sent an agent guessing at `task list` in the first place."""
    import pipeline
    from runnerlib.registry import Registry

    d = scratch()
    cfg = FakeCfg(d)
    reg = Registry(d / "registry.json")
    rec = tasks.record(reg, "task-fix-the-docs-20260924-101010-abcdef")
    rec.update(iteration=3, cwd="/home/u/proj")
    reg.save()

    out = io.StringIO()
    with redirect_stdout(out):
        pipeline.cmd_status(cfg, None)
    assert out.getvalue().splitlines() == [
        "task-fix-the-docs-20260924-101010-abcdef  [round 3, idle]  /home/u/proj"]


# --------------------------------------------------------------------------- the action SET


def test_the_engine_loads_every_workflow_as_one_set():
    """Two workflows, two files, one tick. Before this the seam loaded
    actions-pipeline.json alone, so the dialogue config was a file nothing
    read."""
    loaded = engine.load_action_set(engine_seam.ACTIONS_PATHS)
    names = [a["name"] for a in loaded]

    routes = ROOT / "config" / "actions-routes.json"
    assert set(engine_seam.ACTIONS_PATHS) == {PIPELINE_ACTIONS, ACTIONS, routes}
    assert names == [a["name"] for a in engine.load_actions(PIPELINE_ACTIONS)] + \
                    [a["name"] for a in engine.load_actions(ACTIONS)] + \
                    [a["name"] for a in engine.load_actions(routes)], \
        "the set is the files concatenated in order — no merge, no reordering"
    assert "task-requested" in names and len(names) == len(set(names))

    # the eval workflow is deliberately NOT in the daemon's set: its nodes are
    # seeded by a different seed, so loading it would spawn nodes that are not
    # installed. Preflight still validates the file on its own.
    assert ROOT / "config" / "actions-eval.json" not in engine_seam.ACTIONS_PATHS
    assert ROOT / "config" / "actions-eval.json" in preflight.ACTION_FILES


def test_a_name_used_in_two_files_fails_the_load_loudly():
    """An action name IS its cursor on the board. Two actions sharing one would
    hide events from each other silently, so the collision is refused at load
    and both files are named."""
    d = scratch()
    clash = d / "actions-clash.json"
    clash.write_text(json.dumps({"actions": [
        {"name": "task-requested",
         "trigger": {"namespace": "cadre", "topic": "tasks", "kind": "command"},
         "emitter": {"type": "write_event", "namespace": "cadre", "topic": "tasks",
                     "kind": "notify", "payload": {"message": "hi"}}}]}))

    try:
        engine.load_action_set((ACTIONS, clash))
        raise AssertionError("a duplicate action name across files must not load")
    except engine.ActionError as e:
        assert "task-requested" in str(e)
        assert "actions-clash.json" in str(e) and "actions-dialogue.json" in str(e)

    # and preflight says so rather than passing a deployment that cannot tick
    bad = preflight.check_action_set((ACTIONS, clash))
    assert not bad.ok and "task-requested" in bad.detail
    assert preflight.check_action_set().ok


# --------------------------------------------------------------------------- the spawn


class Spawned(dict):
    """A recorded `runs.spawn` call — the real one is never made."""


@contextmanager
def no_real_spawn(calls):
    """Replace the two things a task spawn must and must not do: record the
    spawn, and make any worktree attempt an outright failure."""
    import pipeline
    from runnerlib import runs as runs_mod

    real_spawn, real_wt = runs_mod.spawn, runs_mod.add_worktree

    def fake_spawn(claude_bin, prompt, wt_path, model, effort, permission_mode,
                   timeout, run_dir, session_id=None, resume=False,
                   extra_env=None, argv=None):
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        calls.append(Spawned(prompt=prompt, cwd=str(wt_path), model=model,
                             session_id=session_id, resume=resume,
                             env=dict(extra_env or {}), argv=list(argv or []),
                             run_dir=str(run_dir)))
        return 4242

    def forbidden_worktree(*a, **k):
        raise AssertionError("a task spawn must never create a worktree")

    runs_mod.spawn, runs_mod.add_worktree = fake_spawn, forbidden_worktree
    pipeline.runs_mod.spawn = fake_spawn
    pipeline.runs_mod.add_worktree = forbidden_worktree
    try:
        yield calls
    finally:
        runs_mod.spawn, runs_mod.add_worktree = real_spawn, real_wt
        pipeline.runs_mod.spawn, pipeline.runs_mod.add_worktree = real_spawn, real_wt


def drive(cfg, reg, b, spec, calls):
    import pipeline
    with no_real_spawn(calls):
        engine_seam.run_spawn_spec(cfg, reg, None, b, lambda *a: None,
                                   pipeline._run_stage, spec)


def test_a_task_turn_runs_in_the_ask_s_cwd_with_no_worktree_and_a_recorded_session():
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    b, n, acts = board(d), seeded(d), actions()
    request(b, cwd=str(work))

    calls = []
    drive(cfg, reg, b, engine.tick(b, acts, n, d).spawns[0], calls)

    assert len(calls) == 1
    spawn = calls[0]
    # the ask's directory IS the working directory — no checkout, no worktree
    # (no_real_spawn makes an attempt at one an assertion failure)
    assert spawn["cwd"] == str(work)
    # round 1 MINTS a session id and writes it down, because the driver's next
    # annotation has to be able to find it after a daemon restart
    assert spawn["resume"] is False
    assert spawn["session_id"] and spawn["env"]["CADRE_SESSION_ID"] == spawn["session_id"]
    assert tasks.records(reg)["task-demo"]["session_id"] == spawn["session_id"]
    assert reg.saves >= 1, "the session id must be persisted, not just held"

    # the page the driver will read is named for the task, and the session is
    # told where to write it
    assert spawn["env"]["CADRE_SURFACE_OUT"].endswith("surfaces/task-task-demo.html")
    assert spawn["env"]["CADRE_TASK"] == "task-demo"

    # the run record lives in the registry's `tasks` section, NOT in a story
    run = list(tasks.records(reg)["task-demo"]["active_runs"].values())[0]
    assert run["pid"] == 4242 and run["session_id"] == spawn["session_id"]
    assert run["worktree"] == str(work) and run["branch"] == ""
    assert reg.data["stories"] == {}, "a task must never become a story row"


def test_a_feedback_turn_resumes_the_recorded_session_at_round_two():
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    b, n, acts = board(d), seeded(d), actions()

    request(b, cwd=str(work), task_text="tidy the flags")
    calls = []
    drive(cfg, reg, b, engine.tick(b, acts, n, d).spawns[0], calls)
    first_session = calls[0]["session_id"]
    # round 1 has to be out of the way, or the one-turn-at-a-time guard drops
    # the next one (which is the correct behaviour, not the one under test)
    tasks.records(reg)["task-demo"]["active_runs"].clear()

    b.write("cadre", "tasks", tasks.key_for("task-demo"), "command",
            {"target": tasks.TARGET_FEEDBACK, "task": "task-demo",
             "story": "task-demo", "cwd": str(work), "iteration": "2",
             "feedback": "the second table is unreadable — fold it into prose",
             "resume": "session"})
    drive(cfg, reg, b, engine.tick(b, acts, n, d).spawns[0], calls)

    assert len(calls) == 2
    turn = calls[1]
    # the SAME session, continued — this is the whole workflow
    assert turn["session_id"] == first_session and turn["resume"] is True
    assert turn["cwd"] == str(work)
    # ...and it reaches the process as `--resume <id>`: the node's command
    # carries the `{session}` placeholder and the spawner fills it from
    # (session_id, resume), which is the only difference between continuing the
    # dialogue and starting it over
    from runnerlib import runs as runs_mod
    assert "{session}" in turn["argv"]
    expanded = runs_mod.expand_spawn_argv(
        turn["argv"], {"prompt": "P", "permission": [],
                       "session": runs_mod.session_args(turn["session_id"],
                                                        turn["resume"])})
    assert expanded[expanded.index("--resume") + 1] == first_session
    assert "--session-id" not in expanded
    assert runs_mod.session_args(calls[0]["session_id"], calls[0]["resume"]) == \
        ["--session-id", first_session]

    # the driver's words and the round reached the RENDERED prompt
    assert "the second table is unreadable" in turn["prompt"]
    assert "round **2 of 5**" in turn["prompt"]
    assert "$feedback" not in turn["prompt"] and "$cwd" not in turn["prompt"]
    assert str(work) in turn["prompt"]


def test_a_task_spawn_refuses_a_cwd_that_is_not_an_absolute_path():
    d = scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    b, n, acts = board(d), seeded(d), actions()
    request(b, cwd="relative/dir")

    try:
        drive(cfg, reg, b, engine.tick(b, acts, n, d).spawns[0], [])
        raise AssertionError("a relative cwd must be refused before the spawn")
    except RuntimeError as e:
        assert "absolute" in str(e)


# --------------------------------------------------------------------------- the bridge


@contextmanager
def jsonl_board(d: Path):
    """board_events writes to a JSONL stub; point it somewhere disposable so a
    test can assert on what the pipeline path still emits."""
    was = os.environ.get("CADRE_BOARD_JSONL")
    os.environ["CADRE_BOARD_JSONL"] = str(d / "events.jsonl")
    try:
        yield d / "events.jsonl"
    finally:
        os.environ.pop("CADRE_BOARD_JSONL", None)
        if was is not None:
            os.environ["CADRE_BOARD_JSONL"] = was


def poll_json(*prompts) -> str:
    return json.dumps({"status": "open", "prompts": list(prompts)})


def note(text, anchor=""):
    return {"prompt": text, "text": anchor}


def commands(cfg) -> list[dict]:
    b = Board(engine_seam.board_path(cfg))
    try:
        return [e["payload"] for e in b.peek(topic="tasks", kind="command")]
    finally:
        b.close()


def test_annotations_on_a_task_surface_become_a_feedback_command():
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    tasks.record(reg, "task-demo")["cwd"] = str(work)
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True}

    with jsonl_board(d):
        surface._handle_poll(
            cfg, reg, None, lambda *a: None, str(d / "task-task-demo.html"), meta,
            poll_json(note("fold this into prose", "The second table"),
                      note("and say what you could not check")))

    payloads = commands(cfg)
    assert len(payloads) == 1
    # the exact shape config/actions-dialogue.json documents — nothing more
    assert payloads[0] == {
        "target": "task:feedback", "task": "task-demo", "story": "task-demo",
        "cwd": str(work), "iteration": "2", "resume": "session",
        "feedback": "> The second table\n\nfold this into prose"
                    "\n\n---\n\nand say what you could not check"}

    # and it is a real trigger: the engine takes the next turn from it
    b, n = Board(engine_seam.board_path(cfg)), seeded(d)
    assert [s["node"] for s in engine.tick(b, actions(), n, d).spawns] == ["task"]


def test_a_continue_verdict_is_a_feedback_turn_and_approve_ends_the_dialogue():
    d = scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    meta = {"kind": "task", "task": "task-demo", "cwd": "/tmp/work", "open": True}
    art = str(d / "task-task-demo.html")

    # `continue` is not a verdict the config handles — continuing IS a feedback
    # turn, so the bridge writes one
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, art, meta, poll_json(
            note("CADRE_DECISION gate=task story=task-demo task=task-demo "
                 "verdict=continue\n\nkeep going, drop the second table",
                 "Verdict")))
    payloads = commands(cfg)
    assert [p["target"] for p in payloads] == ["task:feedback"]
    assert "drop the second table" in payloads[0]["feedback"]

    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, art, meta, poll_json(
            note("CADRE_DECISION gate=task story=task-demo task=task-demo "
                 "verdict=approve")))
    payloads = commands(cfg)
    assert payloads[-1] == {"target": "task:verdict", "task": "task-demo",
                            "story": "task-demo", "verdict": "approve"}

    # ...and the approval is what the config's done action reads
    b, n = Board(engine_seam.board_path(cfg)), seeded(d)
    spawns, firings = engine.tick(b, [by_name("task-approved-done")], n, d)
    assert spawns == [] and [f["outcome"] for f in firings] == ["fired"]


def test_approve_with_annotations_keeps_them_in_full_on_disk():
    """Approve closes the dialogue and the notes ride along — they must land
    verbatim in the task's log dir, not only truncated into a log line."""
    d = scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    meta = {"kind": "task", "task": "task-demo", "cwd": "/tmp/work", "open": True}
    long_note = "styling thread: " + "x" * 300
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None,
                             str(d / "task-task-demo.html"), meta, poll_json(
            note(long_note, "Styling"),
            note("CADRE_DECISION gate=task story=task-demo task=task-demo "
                 "verdict=approve")))
    kept = Path(cfg.data_dir) / "logs" / "task-demo" / "closing-annotations.json"
    assert kept.exists()
    saved = json.loads(kept.read_text())
    assert any(long_note in (r.get("text") or "") + (r.get("prompt") or "")
               for r in saved)


def test_the_bridge_never_touches_a_surface_it_does_not_own():
    """Scope: one session kind. A pipeline surface keeps mirroring to its PR
    and writing `surface_feedback`, and writes NOTHING on the tasks topic."""
    d = scratch()
    cfg, reg = FakeCfg(d), FakeReg({"nex-1": {"repo": "o/r", "status": "active"}})
    mirrored = []

    class FakeGh:
        def comment(self, repo, pr, body):
            mirrored.append((repo, pr, body))

    meta = {"kind": "spec_review", "story": "nex-1", "pr": 7, "repo": "o/r",
            "open": True}
    with jsonl_board(d) as events:
        surface._handle_poll(cfg, reg, FakeGh(), lambda *a: None,
                             str(d / "spec-nex-1.html"), meta,
                             poll_json(note("this slice is too big", "Slice 2")))
        emitted = [json.loads(l) for l in events.read_text().splitlines()]

    assert [e["kind"] for e in emitted] == ["surface_feedback"]
    assert emitted[0]["story"] == "nex-1" and emitted[0]["pr"] == 7
    assert mirrored and mirrored[0][1] == 7        # still the PR's record
    assert commands(cfg) == [], "the bridge must not write for a pipeline surface"
    assert tasks.records(reg) == {}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("dialogue-as-actions tests: all passed")


def test_tick_pass_reopens_the_node_registry_every_pass():
    """A daemon runs for weeks; the CLI re-seeds and `promote` retargets nodes
    from other processes. The engine pass must read the registry as it is NOW
    — a handle cached at daemon start pins every command and active prompt at
    boot, which turns 'promotion puts a version in front of traffic' into
    'promotion waits for a restart' (first hit live: a re-seeded task command
    was invisible until the daemon bounced)."""
    d = scratch()
    cfg = FakeCfg(d)
    engine_seam.reset()
    engine_seam.state(cfg)  # daemon start: seeds and warms the cache

    # another process edits the node after the daemon is up
    Nodes(cfg.data_dir).register(
        "task", "new prompt", "opus",
        "claude -p {prompt} --model {model} --flag-added-later "
        "{session} {permission}", replace=True)

    class FakeReg:
        data = {"stories": {}}

    seen = {}
    real_tick = engine.tick
    def spy(board, actions, nodes, data_dir):
        seen["command"] = nodes.active("task")["command"]
        return [], []
    engine.tick = spy
    try:
        engine_seam.tick_pass(cfg, FakeReg(), None, lambda *a: None, None)
    finally:
        engine.tick = real_tick
        engine_seam.reset()

    assert "--flag-added-later" in seen["command"]
