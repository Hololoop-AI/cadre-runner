"""A turn that ends without rewriting its page is asked once for the page.

The case: round 2 of a dialogue started a fifteen-minute background pass, said
"I'll write the page once they finish" and ended its turn. `claude -p` exits
when the turn ends, the background pass was killed, and the reap — which only
checked that a page file existed — reopened round 1's page as the answer.

No claude, no network, no review-surface CLI.
"""

import os

import pytest

from runnerlib import tasks
from tests.test_dialogue_actions import FakeCfg, FakeReg, scratch


@pytest.fixture
def daemon(monkeypatch):
    import pipeline
    spawned, opened = [], []
    monkeypatch.setattr(pipeline.runs_mod, "finished", lambda r: True)
    monkeypatch.setattr(pipeline.runs_mod, "outcome", lambda r: (True, "ok", {}, {}))
    monkeypatch.setattr(pipeline.runs_mod, "spawn",
                        lambda *a, **k: spawned.append(a[1]) or 4242)
    monkeypatch.setattr(pipeline.status_mod, "append_history", lambda *a: None)
    monkeypatch.setattr(pipeline.surface_mod, "available", lambda: True)
    monkeypatch.setattr(pipeline.surface_mod, "open_session",
                        lambda cfg, art, *a, **k: opened.append(str(art)))
    monkeypatch.setattr(pipeline, "_link_handoff_page", lambda *a: None)
    d = scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    page = pipeline.task_page(cfg, "task-demo")
    page.parent.mkdir(parents=True, exist_ok=True)
    action = {"type": "run_task", "stage": "task", "task": "task-demo",
              "cwd": str(scratch()), "session_id": "s" * 36, "resume": True,
              "prompt_template": "round two", "extra_vars": {"iteration": "2"}}

    def start(page_before: str | None):
        if page_before is not None:
            page.write_text(page_before)
            os.utime(page, (1_000, 1_000))
        pipeline._run_task(cfg, reg, "task-demo", action)

    def reap():
        pipeline._reap_tasks(cfg, reg)

    return pipeline, cfg, reg, page, start, reap, spawned, opened


def test_a_turn_that_rewrote_its_page_opens_it(daemon):
    pipeline, cfg, reg, page, start, reap, spawned, opened = daemon
    start("<p>round one</p>")
    page.write_text("<p>round two</p>")
    reap()
    assert opened == [str(page)] and len(spawned) == 1


def test_a_turn_that_left_last_rounds_page_is_resumed_for_the_page(daemon):
    pipeline, cfg, reg, page, start, reap, spawned, opened = daemon
    start("<p>round one</p>")
    reap()                       # the page was never touched
    assert opened == [], "round one's page must not be reopened as the answer"
    assert len(spawned) == 2
    assert "ended without rewriting the page" in spawned[1]
    assert "$CADRE_SURFACE_OUT" in spawned[1], "the $$ escape renders as one $"
    run = next(iter(tasks.record(reg, "task-demo")["active_runs"].values()))
    assert run["action"]["page_nudge"] and run["action"]["resume"]


def test_a_first_turn_that_wrote_no_page_is_resumed_too(daemon):
    pipeline, cfg, reg, page, start, reap, spawned, opened = daemon
    start(None)
    reap()
    assert len(spawned) == 2 and opened == []


def test_the_nudge_is_asked_once_then_the_reap_falls_through(daemon):
    pipeline, cfg, reg, page, start, reap, spawned, opened = daemon
    start("<p>round one</p>")
    reap()
    reap()                       # the nudged turn also left the page alone
    assert len(spawned) == 2, "one nudge, not a loop"
    assert opened == [str(page)]
    assert not tasks.record(reg, "task-demo")["active_runs"]


def test_a_nudged_turn_that_writes_the_page_opens_it(daemon):
    pipeline, cfg, reg, page, start, reap, spawned, opened = daemon
    start("<p>round one</p>")
    reap()
    page.write_text("<p>round two, finally</p>")
    reap()
    assert opened == [str(page)] and len(spawned) == 2


def test_a_run_from_before_page_mtime_was_recorded_is_taken_at_its_word(daemon):
    pipeline, cfg, reg, page, start, reap, spawned, opened = daemon
    start("<p>round one</p>")
    for run in tasks.record(reg, "task-demo")["active_runs"].values():
        del run["page_mtime"]
    reap()
    assert opened == [str(page)] and len(spawned) == 1
