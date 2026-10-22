"""Study sessions: the tutor node, started on its own or from inside a dialogue.

What this guards (driver's calls 2026-10-22, on the page "Keeping up with
AI-written code"):

- a study session can start from INSIDE another dialogue, now, on the `tutor`
  node — the driver may need the topic to rule on that page, so waiting for
  the dialogue to end does not work — and the driver's answers come back
  through the tutor, not through `task`.

No claude, no network, no review-surface CLI.

Run directly: python3 -m pytest -q tests/test_study.py
"""

import argparse

import pytest

import pipeline
from runnerlib import engine, tasks
from runnerlib.registry import Registry
from tests.test_dialogue_actions import (FakeCfg, FakeReg, actions, board, drive,
                                         scratch, seeded)


# --------------------------------------------------------------------------- tutor


def test_a_study_session_starts_on_the_tutor_and_answers_come_back_to_it():
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    b, n = board(d), seeded(d)
    task_id = tasks.submit_task(cfg, "Study session: how a dialogue round works",
                                cwd=str(work), node=tasks.TUTOR)

    spec = engine.tick(b, actions(), n, d).spawns[0]
    assert spec["node"] == "tutor"
    calls = []
    drive(cfg, reg, b, spec, calls)
    prompt = calls[0]["prompt"]
    assert prompt.startswith("# Tutor")
    assert "how a dialogue round works" in prompt
    # the tutor drives the cards CLI through the runner's own command line
    assert "$pipeline" not in prompt and "pipeline.py cards due" in prompt
    assert calls[0]["env"]["CADRE_SURFACE_OUT"].endswith(f"task-{task_id}.html")
    assert tasks.records(reg)[task_id]["node"] == "tutor"

    # the driver's answers resume the TUTOR's session, not a `task` turn
    tasks.write_feedback(cfg, reg, task_id, str(work),
                         [{"text": "STUDY ANSWERS\nq1: the board serialises turns"}])
    again = engine.tick(b, actions(), n, d).spawns[0]
    assert again["node"] == "tutor"


def test_only_a_seeded_dialogue_node_can_start_a_task():
    d = scratch()
    with pytest.raises(ValueError, match="no dialogue node 'nowhere'"):
        tasks.submit_task(FakeCfg(d), "do the thing", cwd=str(scratch()), node="nowhere")


def test_study_from_a_dialogue_runs_in_its_directory_and_points_back(capsys):
    d, work = scratch(), scratch()
    reg = Registry(d / "registry.json")
    tasks.record(reg, "task-parent")["cwd"] = str(work)
    reg.save()

    pipeline.cmd_study(FakeCfg(d), argparse.Namespace(
        topic=["the", "FSRS", "scheduler"], from_task="task-parent", cwd=None))
    study_id = capsys.readouterr().out.splitlines()[0]

    ev = [e for e in board(d).read_since(0) if e["key"] == tasks.key_for(study_id)][0]
    p = ev["payload"]
    assert p["target"] == tasks.TARGET_START and p["node"] == "tutor"
    assert p["cwd"] == str(work)
    assert "the FSRS scheduler" in p["task_text"]
    assert "task-task-parent.html" in p["task_text"], "the session links back"

    with pytest.raises(SystemExit, match="no task 'task-gone'"):
        pipeline.cmd_study(FakeCfg(d), argparse.Namespace(
            topic=["x"], from_task="task-gone", cwd=None))


def test_the_task_agent_is_told_how_to_start_one():
    text = tasks.prompt_text(node="task")
    assert '$pipeline study "<the topic, in their words>" --from $task' in text
    tutor = tasks.prompt_text(node="tutor")
    assert "edits nothing in the repo" in tutor
    assert "queueKey:'study-answers'" in tutor and "verdict=continue" in tutor
