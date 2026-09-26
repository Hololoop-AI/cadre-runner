"""The node event: routing is one board event naming the node that reacts.

`task:handoff` carries `node`. One action starts whichever node it names, so a
specialist registered AFTER the daemon loaded its actions is offered on every
page and started by that same action — no new action, no restart. The
driver's verdict form and an agent handing on by itself write the same event
through the same function, and a node that is not registered to take it is
refused loudly, never skipped.

No claude, no network, no review-surface CLI.
"""

import argparse
import json

import pytest

from runnerlib import engine, engine_seam, surface, tasks
from runnerlib.nodes import Nodes
from tests.test_dialogue_actions import (FakeCfg, FakeReg, board, commands, drive,
                                         jsonl_board, note, poll_json, request,
                                         scratch, seeded)

LIVE = engine.load_action_set(engine_seam.ACTIONS_PATHS)
REVIEW_ABOUT = "reads the page cold and says what is wrong with it"


def decision(verdict: str, typed: str = "") -> dict:
    return note("CADRE_DECISION gate=task story=task-demo task=task-demo "
                f"verdict={verdict}" + (f"\n\n{typed}" if typed else ""))


def register_review(d):
    Nodes(d).register("review", "review $task, handed $surface_prev. Nodes:\n$nodes\n"
                      "form: $handoff_options", "opus",
                      "claude -p {prompt} --model {model} {session} {permission}",
                      reads=[tasks.HANDOFF], about=REVIEW_ABOUT)


@pytest.fixture(autouse=True)
def not_inside_a_session(monkeypatch):
    for var in ("CADRE_RUN_ID", "CADRE_TASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(surface, "_run_cli", lambda *a, **k: "")


def first_turn(d, work):
    cfg, reg = FakeCfg(d), FakeReg()
    b, n = board(d), seeded(d)
    request(b, cwd=str(work))
    calls = []
    drive(cfg, reg, b, engine.tick(b, LIVE, n, d).spawns[0], calls)
    return cfg, reg, b, calls


# --------------------------------------------------------------------------- one action, any node


def test_the_action_names_no_node_the_event_does():
    handed = [a for a in LIVE if a["name"] == "task-handed-to-a-node"]
    assert len(handed) == 1
    assert handed[0]["body"] == {"type": "spawn_node", "node": "{payload[node]}"}
    # and nothing else in the set is a per-edge route
    assert not [a for a in LIVE if "route" in json.dumps(a.get("trigger"))
                and a is not handed[0]]


def test_a_node_registered_after_the_actions_loaded_is_offered_started_and_resumed():
    d, work = scratch(), scratch()
    cfg, reg, b, calls = first_turn(d, work)
    assert '<option value="review"' not in calls[0]["prompt"]

    # a new specialist: registering it is the whole of adding it
    register_review(d)
    tasks.record(reg, "task-demo")["active_runs"] = {}
    b.write("cadre", "tasks", tasks.key_for("task-demo"), "command",
            {"target": "task:feedback", "task": "task-demo", "story": "task-demo",
             "node": "task", "cwd": str(work), "iteration": "2",
             "feedback": "go on", "resume": "session"})
    drive(cfg, reg, b, engine.tick(b, LIVE, Nodes(d), d).spawns[0], calls)
    prompt = calls[-1]["prompt"]
    # the prompt is told who exists and what for, and the form offers it
    assert f"- **review** — {REVIEW_ABOUT}" in prompt
    assert f'<option value="review">review — {REVIEW_ABOUT}</option>' in prompt
    # pipeline stages are registered too, but do not take a handoff
    assert "**build**" not in prompt and '<option value="build"' not in prompt
    for var in ("$nodes", "$handoff", "$handoff_options"):
        assert var not in prompt, var

    # the driver picks it: the SAME loaded action set starts it
    tasks.record(reg, "task-demo")["active_runs"] = {}
    page1 = str(d / "surfaces" / "task-task-demo.html")
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True}
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, page1, meta,
                             poll_json(decision("handoff:review", "check the edge cases")))
    ev = commands(cfg)[-1]
    assert (ev["target"], ev["node"], ev["from"]) == ("task:handoff", "review", "task")
    spawns, _ = engine.tick(b, LIVE, Nodes(d), d)
    assert [(s["action"], s["node"]) for s in spawns] == [("task-handed-to-a-node", "review")]
    drive(cfg, reg, b, spawns[0], calls)
    assert calls[-1]["env"]["CADRE_SURFACE_OUT"].endswith("task-task-demo-review.html")
    assert page1 in calls[-1]["prompt"]
    # the review page offers task and implement, not itself
    later = calls[-1]["prompt"]
    assert '<option value="task"' in later and '<option value="implement"' in later
    assert '<option value="review"' not in later

    # Continue on the review page resumes review, through the one feedback action
    tasks.record(reg, "task-demo")["active_runs"] = {}
    tasks.write_feedback(cfg, reg, "task-demo", str(work), [{"text": "look again"}])
    spawns, _ = engine.tick(b, LIVE, Nodes(d), d)
    assert [(s["action"], s["node"]) for s in spawns] == [
        ("task-feedback-resumes-the-session", "review")]


# --------------------------------------------------------------------------- loud refusal


def test_an_unregistered_node_is_refused_at_every_writer_and_at_the_engine():
    import pipeline
    d, work = scratch(), scratch()
    cfg, reg, b, _ = first_turn(d, work)
    page = d / "surfaces" / "task-task-demo.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<!doctype html><title>p</title>")
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True,
            "key": "k1"}
    surface._save_sessions(cfg, {str(page): meta})

    # the driver's form: nothing written, the batch stranded where the fleet
    # badges it, and the page left open so they can pick again
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, str(page), meta,
                             poll_json(decision("handoff:ghost", "send it on")))
    assert [c["target"] for c in commands(cfg)] == ["task"]
    sess = surface.sessions(cfg)[str(page)]
    assert sess["open"] is True and len(sess["stranded"]) == 1
    kept = json.loads((surface.stranded_dir(cfg) / sess["stranded"][0]).read_text())
    assert "no node 'ghost' is registered" in kept["reason"]
    assert "send it on" in kept["raw"]

    # the function all writers share, and the command an agent runs
    with pytest.raises(tasks.UnknownNode, match="nodes that take a handoff: implement, task"):
        tasks.write_handoff(cfg, reg, "task-demo", "ghost", str(page), [])
    # registered, but a pipeline stage: it does not listen for the event
    from runnerlib import seed_nodes
    seed_nodes.seed(d)
    with pytest.raises(tasks.UnknownNode, match="'build' is registered but does not take"):
        tasks.write_handoff(cfg, reg, "task-demo", "build", str(page), [])
    from runnerlib.registry import Registry
    real = Registry(d / "registry.json")
    tasks.record(real, "task-demo")["cwd"] = str(work)
    real.save()
    with pytest.raises(SystemExit, match="refused, nothing written: no node 'ghost'"):
        pipeline.cmd_handoff(cfg, argparse.Namespace(
            task_id="task-demo", node="ghost", page=None, note=None, no_kept=True))
    assert [c["target"] for c in commands(cfg)] == ["task"]

    # an event written around those guards still fails loudly in the engine
    b.write("cadre", "tasks", tasks.key_for("task-demo"), "command",
            {"target": "task:handoff", "node": "ghost", "from": "task", "task": "task-demo",
             "story": "task-demo", "cwd": str(work)})
    spawns, firings = engine.tick(b, LIVE, Nodes(d), d)
    assert spawns == []
    assert [(f["action"], f["outcome"]) for f in firings] == [
        ("task-handed-to-a-node", "failed")]
    failed = [e for e in b.peek(topic="tasks", kind="signal")
              if e["payload"].get("status") == "failed"]
    assert "unknown node 'ghost'" in failed[-1]["payload"]["error"]


# --------------------------------------------------------------------------- an agent hands on


def test_an_agent_writes_the_same_event_and_the_node_starts_when_its_turn_ends(
        monkeypatch, capsys):
    import pipeline
    from runnerlib.registry import Registry
    d, work = scratch(), scratch()
    cfg, b = FakeCfg(d), board(d)
    reg = Registry(d / "registry.json")
    seeded(d)
    request(b, cwd=str(work))
    calls = []
    drive(cfg, reg, b, engine.tick(b, LIVE, Nodes(d), d).spawns[0], calls)
    reg.save()
    page = d / "surfaces" / "task-task-demo.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<!doctype html><title>design</title>")
    surface._save_sessions(cfg, {str(page): {"kind": "task", "task": "task-demo",
                                             "open": True, "key": "k1"}})

    # the prompt carries the exact command, with this task in it
    assert "handoff task-demo --node <node>" in calls[0]["prompt"]

    # the agent, mid-turn, runs it
    monkeypatch.setenv("CADRE_RUN_ID", "r1")
    monkeypatch.setenv("CADRE_TASK", "task-demo")
    pipeline.cmd_handoff(cfg, argparse.Namespace(
        task_id="", node="implement", page=None, note=["build option B"], no_kept=False))
    assert "after this turn ends" in capsys.readouterr().out
    agent_ev = commands(cfg)[-1]

    # ...the same event a driver's pick writes, bar who wrote it
    driver_ev = tasks.write_handoff(cfg, reg, "task-demo", "implement", str(page),
                                  [{"text": "build option B"}])["payload"]
    assert agent_ev.pop("by") == "agent" and driver_ev.pop("by") == "driver"
    assert agent_ev == driver_ev

    # its own turn is still running, so the node waits instead of vanishing
    reg = Registry(d / "registry.json")
    spawns, _ = engine.tick(b, LIVE, Nodes(d), d)
    assert [s["node"] for s in spawns] == ["implement", "implement"]
    before = len(calls)
    drive(cfg, reg, b, spawns[0], calls)
    assert len(calls) == before, "nothing may start while the turn is running"
    assert len(tasks.record(reg, "task-demo")["deferred"]) == 1

    # the turn ends and is reaped: the node starts, the handed-on page closes
    tasks.record(reg, "task-demo")["active_runs"] = {}
    import pipeline as pl
    from tests.test_dialogue_actions import no_real_spawn
    with no_real_spawn(calls):
        assert engine_seam.run_deferred(cfg, reg, b, lambda *a: None, pl._run_stage) == 1
    assert calls[-1]["env"]["CADRE_SURFACE_OUT"].endswith("task-task-demo-implement.html")
    assert "build option B" in calls[-1]["prompt"] and str(page) in calls[-1]["prompt"]
    assert surface.sessions(cfg)[str(page)]["open"] is False
    assert tasks.record(reg, "task-demo")["deferred"] == []


def test_agents_alone_cannot_hand_a_task_on_forever(monkeypatch):
    import pipeline
    from runnerlib.registry import Registry
    d, work = scratch(), scratch()
    cfg = FakeCfg(d)
    cfg.limits = {"max_rounds_per_stage": 2}
    seeded(d)
    reg = Registry(d / "registry.json")
    tasks.record(reg, "task-demo")["cwd"] = str(work)
    reg.save()
    page = d / "surfaces" / "task-task-demo.html"
    page.parent.mkdir(parents=True)
    page.write_text("x")
    monkeypatch.setenv("CADRE_RUN_ID", "r1")
    args = argparse.Namespace(task_id="task-demo", node="implement", page=None,
                              note=None, no_kept=False)
    pipeline.cmd_handoff(cfg, args)
    pipeline.cmd_handoff(cfg, args)
    with pytest.raises(SystemExit, match="already been handed on 2 times by agents"):
        pipeline.cmd_handoff(cfg, args)
    assert len(commands(cfg)) == 2


# --------------------------------------------------------------------------- no replay


def test_a_new_action_on_a_running_board_does_not_replay_its_history():
    """The generic action is a new name, so the board has no cursor for it and
    would read from the first event: every handoff a driver ever made would
    fire again. On a board that has been running, a new action starts at head."""
    d = scratch()
    b, n = board(d), seeded(d)
    old = [a for a in LIVE if a["name"] != "task-handed-to-a-node"]
    engine.tick(b, old, n, d)                      # the daemon has been running
    b.write("cadre", "tasks", "task:old", "command",
            {"target": "task:handoff", "node": "implement", "from": "task",
             "task": "old", "story": "old", "cwd": "/tmp"})
    engine.tick(b, old, n, d)
    assert engine_seam.start_new_actions_at_head(b, LIVE) == ["task-handed-to-a-node"]
    assert engine.tick(b, LIVE, n, d).spawns == []
    # a fresh board (no cursors at all) is left alone: nothing to replay
    fresh = board(scratch())
    assert engine_seam.start_new_actions_at_head(fresh, LIVE) == []


def test_a_page_written_before_the_rename_still_hands_off():
    """The hand-off verdict was `route:<node>` until it was renamed for what
    it does. Fifteen pages carrying the old token were open on the driver's
    own fleet at the moment of the rename, and a page whose hand-off line
    quietly did nothing would send his chosen specialist into the void — the
    exact silent no-op this project keeps paying for. Both tokens are read;
    only the new one is written."""
    d, work = scratch(), scratch()
    cfg, reg, b, _ = first_turn(d, work)
    register_review(d)
    page = d / "surfaces" / "task-task-demo.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<!doctype html><title>p</title>")
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True,
            "key": "k1"}
    surface._save_sessions(cfg, {str(page): meta})

    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, str(page), meta,
                             poll_json(decision("route:review", "take this on")))

    handoffs = [c for c in commands(cfg) if c["target"] in tasks.TARGET_HANDOFFS]
    assert len(handoffs) == 1, [c["target"] for c in commands(cfg)]
    assert handoffs[0]["node"] == "review"
    assert handoffs[0]["target"] == tasks.TARGET_HANDOFF, \
        "an old token is understood, but what gets written is the new name"
    assert "take this on" in handoffs[0]["feedback"]


def test_the_form_offers_the_new_token_and_the_action_accepts_both():
    d = scratch()
    seeded(d)
    register_review(d)
    listed = tasks.handoff_nodes(Nodes(d))
    form = tasks.node_options(listed)
    assert '<option value="review"' in form and "route:" not in form

    spec = json.loads((engine_seam.CONFIG_DIR / "actions-handoff.json").read_text())
    where = spec["actions"][0]["trigger"]["where"][0]
    assert set(where["values"]) == set(tasks.TARGET_HANDOFFS), \
        "the action must still fire for events written before the rename"
