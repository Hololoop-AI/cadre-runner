"""Routes: the driver points a board event, and the action config decides what
it activates.

The node that built a page is told which routes listen for it and offers them
as extra radios on the SAME verdict form. Picking one writes `task:route`; an
action listening for that route fires; the node it spawns writes its own page,
linked `derived-from` the page the route was chosen on; and `continue` on that
page goes back through that node. The live config wires one route, the
handoff to `implement` (tests/test_handoff.py walks it); a node no route
listens for gets the form exactly as written.

No claude, no network, no review-surface CLI.
"""

import subprocess
from pathlib import Path

import pytest

from runnerlib import conversations, engine, engine_seam, surface, tasks
from runnerlib.nodes import Nodes
from tests.test_dialogue_actions import (ACTIONS, FakeCfg, FakeReg, PROMPTS,
                                         board, commands, drive, jsonl_board,
                                         note, poll_json, request, scratch,
                                         seeded)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config" / "actions-routes.example.json"


def form_of(text: str) -> str:
    return text[text.index("<form data-review-surface-question"):text.index("</form>") + 7]


def decision(verdict: str, typed: str = "") -> dict:
    return note("CADRE_DECISION gate=task story=task-demo task=task-demo "
                f"verdict={verdict}" + (f"\n\n{typed}" if typed else ""))


@pytest.fixture
def quiet_cli(monkeypatch):
    """Ending a session shells out to review-surface; never from a test."""
    monkeypatch.setattr(surface, "_run_cli", lambda *a, **k: "")


@pytest.fixture
def with_example_routes(monkeypatch):
    monkeypatch.setattr(engine_seam, "ACTIONS_PATHS", (ACTIONS, EXAMPLE))
    engine_seam.reset()
    yield engine.load_action_set((ACTIONS, EXAMPLE))
    engine_seam.reset()


# --------------------------------------------------------------------------- nothing listens


def test_with_no_route_actions_the_form_and_the_dialogue_are_unchanged(quiet_cli):
    # The form gained exactly one thing: the slot the route lines render into,
    # which renders to nothing when no route listens. This was written as a
    # diff against HEAD:prompts/task.md, which passes only until the change is
    # committed — after that the old form IS the new form and it compares the
    # file against itself. Assert the property directly instead.
    form = form_of((PROMPTS / "task.md").read_text())
    assert "$route_options<button type=submit".replace("=submit", '="submit"') in form
    assert form.count("$route_options") == 1
    for choice in ("value=\"approve\"", "value=\"continue\""):
        assert choice in form

    # a node no route listens for is told so, and its form has no extra line
    live = engine.load_action_set(engine_seam.ACTIONS_PATHS)
    assert tasks.routes_for(live, "implement") == []
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    b, n = board(d), seeded(d)
    request(b, cwd=str(work))
    calls = []
    drive(cfg, reg, b, engine.tick(b, engine.load_actions(ACTIONS), n, d).spawns[0], calls)
    assert calls[0]["env"]["CADRE_SURFACE_OUT"].endswith("surfaces/task-task-demo.html")
    assert "$route_options" not in calls[0]["prompt"]

    # a route nobody listens for writes nothing and is kept, not lost
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True}
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, str(d / "p.html"), meta,
                             poll_json(decision("route:review", "send it on")))
    assert [c["target"] for c in commands(cfg)] == ["task"]
    kept = d / "logs" / "task-demo" / "unrouted-verdicts.json"
    assert "send it on" in kept.read_text()


def test_routes_are_read_from_the_action_triggers_and_scoped_by_node(with_example_routes):
    routes = tasks.routes_for(with_example_routes, "task")
    assert routes == [{"route": "review", "to": "review",
                       "action": "task-routed-to-review",
                       "why": "a fresh reviewer reads the page this task produced "
                              "and judges it", "label": ""}]
    # the route is scoped `from = task`: a review page is not offered it
    assert tasks.routes_for(with_example_routes, "review") == []
    assert "`route:review` → activates **review**" in tasks.describe_routes(routes)


# --------------------------------------------------------------------------- a routed flow


def test_a_route_points_the_event_the_next_node_writes_its_own_page_and_back_goes_through_it(
        with_example_routes, quiet_cli, monkeypatch):
    import pipeline
    acts = with_example_routes
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    b, n = board(d), seeded(d)
    Nodes(d).register("review", "review $task. Routes:\n$routes", "opus",
                      "claude -p {prompt} --model {model} {session} {permission}")
    n = Nodes(d)

    # the task node's turn is told where its work can go
    request(b, cwd=str(work))
    calls = []
    drive(cfg, reg, b, engine.tick(b, acts, n, d).spawns[0], calls)
    assert "`route:review` → activates **review**" in calls[0]["prompt"]
    rec = tasks.record(reg, "task-demo")
    rec["active_runs"] = {}
    page1 = str(d / "surfaces" / "task-task-demo.html")

    # the driver points it at review, with a note
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True}
    surface._save_sessions(cfg, {page1: {**meta, "key": "aaaaaaaaaaaaaaa1"}})
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, page1, meta,
                             poll_json(decision("route:review", "check the edge cases")))
    routed = commands(cfg)[-1]
    assert {k: routed[k] for k in ("target", "route", "from", "surface_prev")} == {
        "target": "task:route", "route": "review", "from": "task", "surface_prev": page1}
    assert "check the edge cases" in routed["feedback"]
    assert not any(c["target"] == "task:verdict" for c in commands(cfg)), \
        "a route hands the work off; it does not end the task"
    assert surface.sessions(cfg)[page1]["open"] is False

    # the action listening for that route fires, and ONLY it
    spawns, _ = engine.tick(b, acts, n, d)
    assert [(s["action"], s["node"]) for s in spawns] == [("task-routed-to-review", "review")]
    drive(cfg, reg, b, spawns[0], calls)
    review = calls[1]
    assert review["env"]["CADRE_SURFACE_OUT"].endswith("task-task-demo-review.html")
    assert review["resume"] is False and review["session_id"] != calls[0]["session_id"]
    assert rec["node"] == "review"

    # its page, once written and reaped, is derived-from the page it came from
    art = Path(rec["page"])
    art.write_text("<!doctype html><title>review</title>")
    run = list(rec["active_runs"].values())[0]
    rec["active_runs"] = {"r": {**run, "run_dir": "/nonexistent"}}

    def fake_open(cfg_, path, kind, log, **m):
        s = surface.sessions(cfg_)
        s[str(path)] = {"kind": kind, "key": "aaaaaaaaaaaaaaa2", "open": True, **m}
        surface._save_sessions(cfg_, s)

    monkeypatch.setattr(pipeline.runs_mod, "finished", lambda r: True)
    monkeypatch.setattr(pipeline.runs_mod, "outcome", lambda r: (True, "ok", {}, {}))
    monkeypatch.setattr(pipeline.status_mod, "append_history", lambda *a: None)
    monkeypatch.setattr(pipeline.surface_mod, "available", lambda: True)
    monkeypatch.setattr(pipeline.surface_mod, "open_session", fake_open)
    pipeline._reap_tasks(cfg, reg)
    assert surface.sessions(cfg)[str(art)]["node"] == "review"
    records, _ = conversations.load_links([d / "surfaces" / "panel-links.jsonl"])
    assert records == [{"type": "derived-from", "from": "aaaaaaaaaaaaaaa2",
                        "to": "aaaaaaaaaaaaaaa1"}]

    # BACK: continue on the review page goes through review, not task
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, str(art),
                             {**meta, "node": "review"},
                             poll_json(decision("continue", "look again")))
    back = commands(cfg)[-1]
    assert back["target"] == "task:feedback" and back["node"] == "review"
    spawns, _ = engine.tick(b, acts, n, d)
    assert [(s["action"], s["node"]) for s in spawns] == [
        ("review-feedback-resumes-review", "review")]
