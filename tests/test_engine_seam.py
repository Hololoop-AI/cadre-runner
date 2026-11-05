"""The daemon seam: the engine flag, preflight's gate, and the agent command.

What is left here after the PR pipeline was removed is the seam's own
contract, independent of any one workflow: how `CADRE_ENGINE` parses, that
preflight refuses a registry missing a node the loaded actions spawn, and that
the command a node's registry entry names is the command that runs.

Run directly: python3 tests/test_engine_seam.py
"""

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import config as config_mod
from runnerlib import engine, engine_seam, preflight, seed_nodes, tasks
from runnerlib.blackboard import Board
from runnerlib.nodes import Nodes

ROOT = Path(__file__).resolve().parent.parent
DIALOGUE = ROOT / "config" / "actions-dialogue.json"


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


@contextmanager
def engine_mode(value):
    """CADRE_ENGINE for the duration, restored afterwards. The seam caches a
    board handle per data dir, so the cache is dropped on the way out too."""
    was = os.environ.get("CADRE_ENGINE")
    if value is None:
        os.environ.pop("CADRE_ENGINE", None)
    else:
        os.environ["CADRE_ENGINE"] = value
    try:
        yield
    finally:
        engine_seam.reset()
        os.environ.pop("CADRE_ENGINE", None)
        if was is not None:
            os.environ["CADRE_ENGINE"] = was


class FakeCfg:
    """Enough config for a task spawn: a data dir, the node pins, the dials."""

    def __init__(self, d):
        self.data_dir = Path(d)
        self.limits = {"max_rounds_per_stage": 5}
        # the real defaults, so a new config dial does not silently read as
        # absent in every seam test
        self.runner = {**config_mod.DEFAULTS["runner"], "max_concurrent_runs": 4}
        self.claude = {**config_mod.DEFAULTS["claude"]}
        self.skills_source = Path(d) / "skills"

    def model_for(self, node):
        return "opus"

    def effort_for(self, node):
        return "high"


class FakeReg:
    def __init__(self):
        self.data = {"stories": {}}

    def save(self):
        pass


# --------------------------------------------------------------------------- the flag


def test_mode_parses_off_shadow_and_only():
    for value, expected in ((None, "off"), ("", "off"), ("0", "off"), ("off", "off"),
                            ("1", "shadow"), ("shadow", "shadow"),
                            ("only", "only"), ("ONLY", "only"), (" only ", "only")):
        with engine_mode(value):
            assert engine_seam.mode() == expected, value
            assert engine_seam.enabled() is (expected != "off")

    # an unrecognised truthy value still turns the engine on
    with engine_mode("yes-please"):
        assert engine_seam.enabled()


# --------------------------------------------------------------------------- preflight


def test_preflight_detects_a_missing_node():
    d = scratch()
    checks = {c.name: c for c in preflight.run(d, seed=True, skip_tools=True)}
    assert all(c.ok for c in checks.values()), \
        [(c.name, c.detail) for c in checks.values() if not c.ok]
    # the eval judge + the dialogue nodes (`task`, `implement`, `tutor`): a
    # deployment that has never had a task submitted has never run
    # `tasks.seed`, so without these rows engine-only mode reads as ready while
    # the first dialogue command spawns a node that does not exist.
    expected = len(tasks.NODES) + 1
    assert f"nodes active ({expected})" in checks
    assert "actions actions-dialogue.json" in checks
    assert "actions actions-handoff.json" in checks
    assert checks["engine action set"].detail.endswith(
        "actions-dialogue.json, actions-handoff.json")

    # drop one node from the registry and re-check WITHOUT re-seeding: a
    # deployment whose registry lost a node must not read as ready
    nodes = Nodes(d)
    del nodes.index["nodes"]["task"]
    nodes._save()
    again = preflight.run(d, seed=False, skip_tools=True)
    failed = [c for c in again if not c.ok]
    assert [c.name for c in failed] == [f"nodes active ({expected})",
                                        "actions actions-dialogue.json"]
    assert "missing: task" in failed[0].detail
    assert "task" in failed[1].detail          # an action spawns a node that is gone
    assert "NOT safe to run" in preflight.render(again)

    # a node that exists but has no ACTIVE version is the other failure shape
    # a seed can leave behind (a recorded version is not a promoted one)
    d2 = scratch()
    preflight.run(d2, seed=True, skip_tools=True)
    n2 = Nodes(d2)
    del n2.index["nodes"]["implement"]["active_version"]
    n2._save()
    rows = {c.name: c for c in preflight.run(d2, seed=False, skip_tools=True)}
    assert "no active version: implement" in rows[f"nodes active ({expected})"].detail


def test_the_served_theme_does_not_drift_from_the_source():
    css = (ROOT / "surface-theme.css").read_text()
    assert (ROOT / ".review-surface" / "surface-theme.css").read_text() == css


# --------------------------------------------------------------------------- agent command


def test_the_nodes_command_is_the_command_that_runs():
    """The portability claim: swap the agent CLI by editing the node.

    A node whose command names `my-agent` is followed all the way to the
    process — engine body -> spawn spec -> _run_task -> runs.spawn — with no
    Claude Code flag surviving anywhere on the way.
    """
    import pipeline
    from runnerlib import runs as runs_mod

    d, work = scratch(), scratch()
    b = Board(d / "board.db")
    tasks.seed(d)
    n = Nodes(d)
    # the ONE edit: this node is invoked by another CLI, with its own grammar
    n.register("task", n.active("task")["prompt"], "opus",
               "my-agent --file {prompt} {session} {permission} "
               "--task {payload[story]}", replace=True)
    b.write("cadre", "tasks", tasks.key_for("task-demo"), "command",
            {"target": tasks.TARGET_REQUEST, "task": "task-demo",
             "story": "task-demo", "title": "demo", "task_text": "do the thing",
             "cwd": str(work), "iteration": "1"})
    spawns, _ = engine.tick(b, engine.load_actions(DIALOGUE), n, d)
    # the registry's command is in the spec, with the spawn-time placeholders
    # still unresolved — they name values that do not exist until a process does
    assert spawns[0]["argv"][:3] == ["my-agent", "--file", "{prompt}"]
    assert "{session}" in spawns[0]["argv"] and "{permission}" in spawns[0]["argv"]

    executed = []

    class RecordingPopen:
        pid = 4242

        def __init__(self, args, **kw):
            # skill linking asks git about the cwd on the way; only the
            # agent's own process is under test
            if args[:2] == ["/bin/sh", "-c"]:
                executed.append(args)

        def communicate(self, *a, **k):
            return "", ""

        def wait(self, *a, **k):
            return 1

        def poll(self):
            return 1

        returncode = 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = runs_mod.subprocess.Popen
    runs_mod.subprocess.Popen = RecordingPopen
    cfg, reg = FakeCfg(d), FakeReg()
    cfg.claude = {**cfg.claude, "timeout_seconds": 60}   # a ceiling set, so one is recorded
    try:
        engine_seam.run_spawn_spec(cfg, reg, b, lambda *a: None,
                                   pipeline._run_task, spawns[0])
    finally:
        runs_mod.subprocess.Popen = real
    run = list(tasks.record(reg, "task-demo")["active_runs"].values())[0]
    # the wrapper shell that captures stdout/stderr/exit is the daemon's,
    # not the node's — the node's command starts after it
    assert executed[0][:2] == ["/bin/sh", "-c"] and executed[0][3] == "sh"
    argv = executed[0][4:]
    # no `timeout` prefix: the ceiling is the run's deadline, enforced by
    # the daemon (GNU timeout does not exist on macOS)
    assert argv[:2] == ["my-agent", "--file"]
    assert abs(run["deadline"] - run["started"] - cfg.claude["timeout_seconds"]) < 5
    # the rendered prompt travels as ONE argument, whatever is in it
    assert argv[2] == (Path(run["run_dir"]) / "prompt.txt").read_text()
    # {session} became the flag pair this run's session needs, {permission}
    # the config's permission mode — expansions, not one token each
    assert argv[3:5] == ["--session-id", run["session_id"]]
    assert argv[5:] == ["--dangerously-skip-permissions", "--task", "task-demo"]
    # nothing Claude Code-shaped survives in the invocation itself (the
    # prompt is prose and may say anything)
    assert not any("claude" in a for a in argv[:2] + argv[3:]), argv


def test_only_spawns_on_the_tasks_topic_start_a_turn():
    """Every workflow the daemon loads is a dialogue one; a spawn that fired
    anywhere else is a definition error and must not start a session."""
    d = scratch()
    b = Board(d / "board.db")
    ev = b.write("cadre", "stages", "story:x", "signal", {"status": "finished"})
    started = []
    try:
        engine_seam.run_spawn_spec(FakeCfg(d), FakeReg(), b, lambda *a: None,
                                   lambda *a: started.append(a),
                                   {"event_id": ev["id"], "node": "build"})
    except RuntimeError as e:
        assert "'tasks'" in str(e)
    else:
        raise AssertionError("a spawn off the tasks topic must be refused")
    assert started == []


def test_a_revived_session_re_enters_through_the_same_command():
    """Revival is the other half of the contract: the node's command carries
    `{session}`, so resuming a stopped session is the same command with
    `--resume` instead of `--session-id` — not a second, claude-shaped argv."""
    from runnerlib import runs as runs_mod

    argv = ["my-agent", "--file", "{prompt}", "{session}", "{permission}"]
    fresh = runs_mod.expand_spawn_argv(argv, {
        "prompt": "do the thing", "session": runs_mod.session_args("sid-1", False),
        "permission": runs_mod.permission_args("bypass")})
    revived = runs_mod.expand_spawn_argv(argv, {
        "prompt": "carry on", "session": runs_mod.session_args("sid-1", True),
        "permission": runs_mod.permission_args("acceptEdits")})

    assert fresh == ["my-agent", "--file", "do the thing",
                     "--session-id", "sid-1", "--dangerously-skip-permissions"]
    assert revived == ["my-agent", "--file", "carry on",
                       "--resume", "sid-1", "--permission-mode", "acceptEdits"]


def test_seed_command_uses_only_flags_the_claude_cli_accepts():
    """The seeded command IS a real `claude` invocation once the engine spawns
    it (spawn executes node argv verbatim). A template token claude does not
    recognize is fatal at spawn time and invisible to every fake-bin test —
    the first real dialogue-task spawn died on a vestigial `--story`. Task
    text travels inside the rendered prompt, never as a flag."""
    cmd = seed_nodes.command_for("high")
    allowed = {"-p", "--model", "--effort", "--output-format"}
    flags = {tok for tok in cmd.split()
             if tok.startswith("-") and not tok.startswith("{")}
    assert flags <= allowed, f"unknown claude flags in seed command: {flags - allowed}"
    assert "--story" not in cmd


def test_task_ids_do_not_collide_within_one_second():
    ids = {tasks.new_id("you own this design disc", now=1789000000.0)
           for _ in range(50)}
    assert len(ids) == 50


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("engine seam tests: all passed")
