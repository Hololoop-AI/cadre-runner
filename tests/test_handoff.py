"""The handoff: approve a page AND send what it decided to an agent that does it.

Walked on the shape of the case that was blocked: a review page proposing
commits, approved with an annotation alongside. With `approve` the dialogue
ended and the proposed commits were never made; the annotation went to
`closing-annotations.json` and nothing read it. With the handoff:

    the task node's form offers it, rendered from the node registry
    -> the driver picks it, with annotations    -> `task:route` node=implement
    -> the LIVE action set's one generic action spawns `implement`, in the
       same directory, reading
       the approved page as its spec and the annotations as its last words
    -> its page opens in the same task, the same project, linked derived-from
       the approved page, and the fleet shows one conversation
    -> continue on it resumes `implement`; approve on it ends the task.

No claude, no network, no review-surface CLI.
"""

import html
from pathlib import Path

import pytest

from runnerlib import conversations, engine, engine_seam, surface, tasks
from runnerlib.nodes import Nodes
from tests.test_dialogue_actions import (FakeCfg, FakeReg, PROMPTS, board,
                                         commands, drive, jsonl_board, note,
                                         poll_json, request, scratch, seeded)

LIVE = engine.load_action_set(engine_seam.ACTIONS_PATHS)

REVIEW_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<link rel="stylesheet" href="/surface-theme.css"><title>Code review</title></head><body>
<h1>Code review of the uncommitted work</h1>
<p>The work is ready to commit as three commits.</p>
<pre>git apply --cached --recount .review-split/c1.patch
git commit            # message 1</pre>
</body></html>"""


def decision(verdict: str, typed: str = "") -> dict:
    return note("CADRE_DECISION gate=task story=task-demo task=task-demo "
                f"verdict={verdict}" + (f"\n\n{typed}" if typed else ""))


@pytest.fixture
def quiet_cli(monkeypatch):
    monkeypatch.setattr(surface, "_run_cli", lambda *a, **k: "")


@pytest.fixture(autouse=True)
def not_inside_a_session(monkeypatch):
    """These tests are the driver's side. Run from inside a Cadre session the
    environment says otherwise (CADRE_RUN_ID, CADRE_TASK), and the handoff
    command would take itself for an agent."""
    for var in ("CADRE_RUN_ID", "CADRE_TASK"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def reap(monkeypatch):
    """`_reap_tasks` with the run finished and the surface CLI faked: opening a
    session records it under the key given, as review-surface would."""
    import pipeline
    keys = iter(["bbbbbbbbbbbbbbb1", "bbbbbbbbbbbbbbb2", "bbbbbbbbbbbbbbb3"])

    def fake_open(cfg_, path, kind, log, **m):
        s = surface.sessions(cfg_)
        key = next(keys)
        s[str(path)] = {"kind": kind, "key": key, "path": f"/session/{key}",
                        "open": True, "opened": 2.0, **m}
        surface._save_sessions(cfg_, s)

    monkeypatch.setattr(pipeline.runs_mod, "finished", lambda r: True)
    monkeypatch.setattr(pipeline.runs_mod, "outcome", lambda r: (True, "ok", {}, {}))
    monkeypatch.setattr(pipeline.status_mod, "append_history", lambda *a: None)
    monkeypatch.setattr(pipeline.surface_mod, "available", lambda: True)
    monkeypatch.setattr(pipeline.surface_mod, "open_session", fake_open)

    def run(cfg, reg):
        rec = tasks.record(reg, "task-demo")
        rec["active_runs"] = {r: {**v, "run_dir": "/nonexistent"}
                              for r, v in rec["active_runs"].items()}
        pipeline._reap_tasks(cfg, reg)
    return run


# --------------------------------------------------------------------------- the offer


def test_a_task_page_offers_implement_and_an_implement_page_offers_task():
    d = scratch()
    n = seeded(d)
    listed = tasks.handoff_nodes(n, exclude="task")
    assert [x["name"] for x in listed] == ["implement"]
    # the page's own node is not offered: Continue already goes back to it
    assert [x["name"] for x in tasks.handoff_nodes(n, exclude="implement")] == ["task"]
    line = tasks.node_options(listed)
    about = html.escape(tasks.NODES["implement"]["about"], quote=False)
    assert line == ('<label><input type="radio" name="verdict" value="route:implement">'
                    f' Hand this to <strong>implement</strong> — {about}</label>\n')
    # nothing that could break the attribute contract
    assert '"' not in tasks.NODES["implement"]["about"]


def test_the_task_node_renders_the_handoff_inside_its_verdict_form(quiet_cli):
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    b, n = board(d), seeded(d)
    request(b, cwd=str(work))
    calls = []
    drive(cfg, reg, b, engine.tick(b, LIVE, n, d).spawns[0], calls)
    prompt = calls[0]["prompt"]
    form = prompt[prompt.index("<form data-review-surface-question"):]
    form = form[:form.index("</form>")]
    assert form.index("value=\"continue\"") < form.index("value=\"route:implement\"") \
        < form.index("<button")
    assert "$route_options" not in prompt and "$routes" not in prompt


# --------------------------------------------------------------------------- the walk


def test_approve_and_hand_off_runs_the_approved_page_through_implement(quiet_cli, reap):
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    b, n = board(d), seeded(d)

    # the review turn ran and wrote its page, filed by the driver under "cadre"
    request(b, cwd=str(work))
    calls = []
    drive(cfg, reg, b, engine.tick(b, LIVE, n, d).spawns[0], calls)
    review_page = d / "surfaces" / "task-task-demo.html"
    review_page.parent.mkdir(parents=True, exist_ok=True)
    review_page.write_text(REVIEW_PAGE)
    reap(cfg, reg)
    sess = surface.sessions(cfg)
    sess[str(review_page)]["project"] = "cadre"
    surface._save_sessions(cfg, sess)
    meta = sess[str(review_page)]

    # the driver approves with the handoff, and annotates as they do so
    with jsonl_board(d):
        surface._handle_poll(
            cfg, reg, None, lambda *a: None, str(review_page), meta,
            poll_json(note("drop commit 3, it is superseded", "Commit 3 is the guard"),
                      decision("route:implement")))
    cmds = commands(cfg)
    routed = cmds[-1]
    assert {k: routed[k] for k in ("target", "node", "from", "by", "cwd", "surface_prev")} == {
        "target": "task:route", "node": "implement", "from": "task", "by": "driver",
        "cwd": str(work), "surface_prev": str(review_page)}
    # the annotation is carried, quoted under the text it was attached to
    assert "> Commit 3 is the guard\n\ndrop commit 3, it is superseded" in routed["feedback"]
    assert not any(c["target"] == "task:verdict" for c in cmds), \
        "the handoff is the approval; no second close is written"
    assert surface.sessions(cfg)[str(review_page)]["open"] is False
    assert not (d / "logs" / "task-demo" / "closing-annotations.json").exists()

    # the LIVE action set spawns implement — only it — in the same directory
    spawns, _ = engine.tick(b, LIVE, n, d)
    assert [(s["action"], s["node"]) for s in spawns] == [
        ("task-handed-to-a-node", "implement")]
    drive(cfg, reg, b, spawns[0], calls)
    impl = calls[-1]
    assert impl["cwd"] == str(work)
    assert impl["resume"] is False and impl["session_id"] != calls[0]["session_id"]
    assert impl["env"]["CADRE_SURFACE_OUT"].endswith("surfaces/task-task-demo-implement.html")
    # its spec is the approved page and its last words are the driver's
    assert str(review_page) in impl["prompt"]
    assert "drop commit 3, it is superseded" in impl["prompt"]
    assert "Implement — carry out what the driver approved" in impl["prompt"]
    for var in ("$surface_prev", "$feedback", "$cwd", "$task ", "$route_options"):
        assert var not in impl["prompt"], var

    # its page opens in the same task, the same project, derived-from the review
    out = Path(impl["env"]["CADRE_SURFACE_OUT"])
    out.write_text("<!doctype html><title>Done: two commits</title>")
    reap(cfg, reg)
    new = surface.sessions(cfg)[str(out)]
    assert (new["task"], new["node"], new["project"]) == ("task-demo", "implement", "cadre")
    records, holders = conversations.load_links([d / "surfaces" / "panel-links.jsonl"])
    assert records == [{"type": "derived-from", "from": new["key"], "to": meta["key"]}]

    # ...and the fleet shows ONE conversation: the review page, closed, is an
    # earlier round of the implementing agent's row rather than a lost orphan
    pages = {meta["key"]: {"title": "Code review", "updated": 1.0, "ended": True},
             new["key"]: {"title": "Done: two commits", "updated": 2.0}}
    rows = conversations.collapse(surface.status_list(cfg), records, holders, pages)
    assert len(rows) == 1
    assert rows[0]["project"] == "cadre"
    assert [e["key"] for e in rows[0]["earlier"]] == [meta["key"]]

    # continue on the implement page resumes implement's session
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, str(out), new,
                             poll_json(decision("continue", "also run the linter")))
    back = commands(cfg)[-1]
    assert (back["target"], back["node"], back["resume"]) == (
        "task:feedback", "implement", "session")
    spawns, _ = engine.tick(b, LIVE, n, d)
    assert [(s["action"], s["node"]) for s in spawns] == [
        ("task-feedback-resumes-the-session", "implement")]
    drive(cfg, reg, b, spawns[0], calls)
    assert calls[-1]["resume"] is True and calls[-1]["session_id"] == impl["session_id"]
    assert calls[-1]["env"]["CADRE_SURFACE_OUT"] == impl["env"]["CADRE_SURFACE_OUT"]

    # approve on it ends the task, like any page
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, str(out), new,
                             poll_json(decision("approve")))
    assert commands(cfg)[-1] == {"target": "task:verdict", "task": "task-demo",
                                 "story": "task-demo", "verdict": "approve"}
    _, firings = engine.tick(b, LIVE, n, d)
    assert [f["action"] for f in firings if f["outcome"] == "fired"] == ["task-approved-done"]


# --------------------------------------------------------------------------- edges


def test_a_handoff_queued_with_a_plain_approve_wins(quiet_cli):
    """Approve clicked, then the handoff: the batch carries both. The plain
    close would drop the handoff and the notes with it."""
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    seeded(d)
    tasks.record(reg, "task-demo")["cwd"] = str(work)
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True}
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, str(d / "p.html"), meta,
                             poll_json(decision("approve"), note("use message 2 as written"),
                                       decision("route:implement")))
    assert [c["target"] for c in commands(cfg)] == ["task:route"]
    assert "use message 2 as written" in commands(cfg)[0]["feedback"]


def test_a_plain_approve_still_ends_the_dialogue_and_keeps_notes_with_their_anchor(quiet_cli):
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True}
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, str(d / "p.html"), meta,
                             poll_json(note("add this as a discussion point", "Option B"),
                                       decision("approve")))
    assert [c["target"] for c in commands(cfg)] == ["task:verdict"]
    kept = (d / "logs" / "task-demo" / "closing-annotations.json").read_text()
    assert "add this as a discussion point" in kept and '"anchor": "Option B"' in kept


def test_the_implement_prompt_is_seeded_and_holds_the_commit_guardrails():
    d = scratch()
    text = Nodes(d).active("implement")["prompt"] if tasks.seed(d) else ""
    assert text == (PROMPTS / "implement.md").read_text()
    # the node commits only what was proposed, and never pushes on its own
    assert "Never push" in text and "Commit only what the approved page" in text
    assert "$surface_prev" in text and "$feedback" in text
    assert "$CADRE_SURFACE_OUT" in text and "auto-surface" in text


def test_promote_activates_exactly_the_prompt_on_disk(capsys):
    """Seeding records an edited prompt and never activates it — which is how
    a live daemon ran a four-day-old task prompt that offered no handoff.
    `pipeline.py promote` is the explicit act, and it promotes the text in
    prompts/, not whatever version was recorded last."""
    import argparse
    import pipeline
    d = scratch()
    cfg = FakeCfg(d)
    tmp = scratch()
    (tmp / "task.md").write_text("an old task prompt")
    (tmp / "implement.md").write_text((PROMPTS / "implement.md").read_text())
    tasks.seed(d, prompts_dir=tmp)              # the old text is live
    tasks.seed(d)                               # today's is recorded, not live
    assert Nodes(d).active("task")["prompt"] == "an old task prompt"

    pipeline.cmd_promote(cfg, argparse.Namespace(node="task"))
    assert Nodes(d).active("task")["prompt"] == (PROMPTS / "task.md").read_text()
    assert "promoted" in capsys.readouterr().out
    pipeline.cmd_promote(cfg, argparse.Namespace(node="task"))
    assert "already active" in capsys.readouterr().out


def test_handoff_command_rescues_an_already_approved_page_and_its_kept_notes(capsys):
    """The two pages blocked today were approved before the handoff existed:
    their forms have no handoff line and their dialogues are closed. The
    command writes the same event the form would, and the notes the plain
    approve kept (and nothing read) ride along as the agent's last words."""
    import argparse
    import pipeline
    from runnerlib.registry import Registry
    d, work = scratch(), scratch()
    cfg = FakeCfg(d)
    reg = Registry(d / "registry.json")
    tasks.record(reg, "task-demo")["cwd"] = str(work)
    reg.save()
    page = d / "surfaces" / "task-task-demo.html"
    page.parent.mkdir(parents=True)
    page.write_text(REVIEW_PAGE)
    kept = d / "logs" / "task-demo" / "closing-annotations.json"
    kept.parent.mkdir(parents=True)
    kept.write_text('[{"text": "wrap this up", "anchor": "", "prompt": ""}]')

    tasks.seed(d)
    pipeline.cmd_handoff(cfg, argparse.Namespace(
        task_id="task-demo", node="implement", page=None,
        note=["cut message 2 as written"], no_kept=False))
    routed = commands(cfg)[-1]
    assert (routed["target"], routed["node"], routed["surface_prev"], routed["cwd"],
            routed["by"]) == ("task:route", "implement", str(page), str(work), "driver")
    assert "wrap this up" in routed["feedback"]
    assert "cut message 2 as written" in routed["feedback"]
    b, n = board(d), seeded(d)
    spawns, _ = engine.tick(b, LIVE, n, d)
    assert [s["node"] for s in spawns] == ["implement"]

    # a node nobody registered is refused before anything is written
    with pytest.raises(SystemExit, match="no node 'nowhere' is registered"):
        pipeline.cmd_handoff(cfg, argparse.Namespace(
            task_id="task-demo", node="nowhere", page=None, note=None, no_kept=False))
    assert len(commands(cfg)) == 1
