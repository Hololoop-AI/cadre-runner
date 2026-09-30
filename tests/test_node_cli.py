"""`node` CLI: a person or an agent creates and publishes nodes without Python.

A node added with --takes-handoff is on every page's form from the next turn,
with no action to write and no restart; recording a prompt never activates
it; refusals exit 2 and say what to do instead.

No claude, no network.
"""

import json

from runnerlib import node_cli, tasks
from runnerlib.nodes import Nodes
from tests.test_dialogue_actions import scratch, seeded


def run(d, *argv):
    lines = []
    code = node_cli.main(["--data-dir", str(d), *argv], out=lines.append)
    return code, "\n".join(lines)


def prompt_file(d, text="review $task. Nodes:\n$nodes"):
    p = d / "p.md"
    p.write_text(text)
    return str(p)


def test_an_added_specialist_is_offered_on_pages(monkeypatch):
    monkeypatch.setenv("CADRE_RUN_ID", "r1")
    monkeypatch.setenv("CADRE_STAGE", "task")
    d = scratch()
    seeded(d)
    code, said = run(d, "add", "review", "--prompt", prompt_file(d),
                     "--about", "reads the page cold and says what is wrong with it",
                     "--takes-handoff")
    assert code == 0 and "offered on pages" in said
    listed = [n["name"] for n in tasks.handoff_nodes(Nodes(d), exclude="task")]
    assert "review" in listed
    tasks.check_handoff(type("C", (), {"data_dir": d})(), "review")   # no raise
    rec = Nodes(d).history("review")[0]
    assert rec["produced_by"] == "agent:task"

    code, said = run(d, "list", "--json")
    rows = {r["name"]: r for r in json.loads(said)}
    assert rows["review"]["takes_handoff"] and rows["implement"]["takes_handoff"]


def test_any_harness_is_a_command_template():
    d = scratch()
    code, _ = run(d, "add", "codex-review", "--prompt", prompt_file(d),
                  "--about", "the same review on another harness",
                  "--command", "codex exec --model {model} {prompt}", "--model", "gpt-5")
    assert code == 0
    assert Nodes(d).command_argv("codex-review") == [
        "codex", "exec", "--model", "gpt-5", "{prompt}"]
    code, said = run(d, "list")
    assert "codex-review" in said and "* codex-review" not in said


def test_record_does_not_activate_and_promote_does():
    d = scratch()
    run(d, "add", "review", "--prompt", prompt_file(d, "one"), "--about", "x")
    first = Nodes(d).active("review")["version"]
    code, said = run(d, "record", "review", "--prompt", prompt_file(d, "two"))
    assert code == 0 and "NOT active" in said
    assert Nodes(d).active("review")["version"] == first
    new = [v["id"] for v in Nodes(d).history("review") if not v["active"]][0]
    code, said = run(d, "promote", "review", new[:10])
    assert code == 0 and Nodes(d).active("review")["prompt"] == "two"


def test_refusals_exit_2_and_change_nothing(capsys):
    d = scratch()
    assert run(d, "add", "review", "--prompt", prompt_file(d), "--about", " ")[0] == 2
    assert "--about is required" in capsys.readouterr().err
    run(d, "add", "review", "--prompt", prompt_file(d), "--about", "x")
    assert run(d, "add", "review", "--prompt", prompt_file(d), "--about", "y")[0] == 2
    assert "`node record`" in capsys.readouterr().err
    assert Nodes(d).active("review")["about"] == "x"
    assert run(d, "show", "ghost")[0] == 2
    assert run(d, "promote", "review", "zzz")[0] == 2
    assert run(d, "add", "other", "--prompt", str(d / "missing.md"), "--about", "x")[0] == 2
