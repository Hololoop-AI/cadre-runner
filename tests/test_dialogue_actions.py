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
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import engine, engine_seam, tasks
from runnerlib.blackboard import Board
from runnerlib.nodes import Nodes

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ROOT / "config" / "actions-dialogue.json"
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
    """Enough config for `submit_task`: a data dir and the node pins."""

    def __init__(self, d):
        self.data_dir = Path(d)
        self.claude = {"bin": "claude"}

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
    # the node's command is formatted from the event, like any other node's
    assert "--story" in spawns[0]["argv"] and "task-demo" in spawns[0]["argv"]
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
    """A dialogue has no repo and no plan; the seam still wants a story row."""

    data = {"stories": {"task-demo": {"repo": "", "status": "active"}}}

    def stories(self, status="active"):
        return self.data["stories"]


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
    assert tasks.seed(d) == {"task": "registered"}
    assert tasks.seed(d) == {"task": "unchanged"}

    prompt = Nodes(d).active("task")["prompt"]
    assert (PROMPTS / "_common.md").read_text() not in prompt
    # the page path is freeform HTML via auto-surface — no component DSL
    assert "auto-surface" in prompt
    assert "$CADRE_SURFACE_OUT" in prompt

    # a changed prompt is RECORDED, never promoted (nodes.py's rule)
    tmp = scratch()
    (tmp / "task.md").write_text("a different task prompt")
    assert tasks.seed(d, prompts_dir=tmp) == {"task": "version-recorded"}
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("dialogue-as-actions tests: all passed")
