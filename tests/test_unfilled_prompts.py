"""An unfilled prompt placeholder is loud.

`safe_substitute` leaves a `$name` it has no value for exactly where it was.
After the handoff rename, the `implement` node kept running a version recorded
before it: its prompt asked for `$route_options`, nothing supplied that name,
and the session was handed a literal placeholder where the driver's form lines
should have been — with nothing in any log to say so.

No claude, no network, no review-surface CLI.
"""

from runnerlib import claude_run, engine, tasks
from runnerlib.nodes import Nodes
from tests.test_dialogue_actions import (FakeCfg, FakeReg, actions, board, drive,
                                         request, scratch, seeded)


def _promote_text(d, node, text):
    n = Nodes(d)
    n.promote(node, n.new_version(node, text, produced_by="test"))
    return n


def test_an_old_prompt_version_still_gets_the_form_lines_under_their_old_name():
    d, work = scratch(), scratch()
    seeded(d)
    old = tasks.prompt_text(node="task").replace("$handoff_options", "$route_options")
    assert "$route_options" in old
    n = _promote_text(d, "task", old)
    b = board(d)
    request(b, cwd=str(work))

    calls = []
    drive(FakeCfg(d), FakeReg(), b, engine.tick(b, actions(), n, d).spawns[0], calls)
    assert "$route_options" not in calls[0]["prompt"]
    assert "<select" in calls[0]["prompt"]


def test_a_known_variable_nobody_filled_is_logged_with_both_versions(capsys):
    d, work = scratch(), scratch()
    seeded(d)
    # a task prompt asking for a pipeline variable a dialogue turn never has
    n = _promote_text(d, "task", tasks.prompt_text(node="task")
                      + "\nbranch: $feature_branch, home: $HOME\n")
    active = n.active("task")["version"]
    # ...and the prompt file moved on after it: a newer version recorded, unpromoted
    latest = n.new_version("task", "a newer prompt $task", produced_by="seed")
    b = board(d)
    request(b, cwd=str(work))

    calls = []
    drive(FakeCfg(d), FakeReg(), b, engine.tick(b, actions(), n, d).spawns[0], calls)
    out = capsys.readouterr().out
    assert (f"prompt for task left $feature_branch unfilled — active version "
            f"{active[:12]}, latest {latest[:12]}") in out
    # shell in a prompt is not a hole
    assert "$HOME" not in out


def test_the_check_reads_the_template_not_the_rendered_text():
    # a driver's note quoting a placeholder is not a placeholder
    assert claude_run.unfilled("$feedback", {"feedback": "say $task"}) == []
    assert claude_run.unfilled("$task $CADRE_SURFACE_OUT", {}) == ["task"]
    assert claude_run.unfilled("${route_options}", {}) == ["route_options"]


def test_startup_names_every_node_running_an_older_prompt(capsys):
    import pipeline
    d = scratch()
    n = seeded(d)
    newer = n.new_version("implement", "edited on disk $task", produced_by="seed")
    was = n.active("implement")["version"]

    pipeline._log_stale_nodes(FakeCfg(d))
    out = capsys.readouterr().out
    assert (f"node implement: active version {was[:12]} is not the latest recorded "
            f"{newer[:12]} — run `pipeline.py promote implement`") in out
    assert "node task:" not in out

    n.promote("implement", newer)
    pipeline._log_stale_nodes(FakeCfg(d))
    assert capsys.readouterr().out == ""
