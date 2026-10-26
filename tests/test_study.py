"""Study sessions: the tutor node, started on its own or from inside a dialogue.

What this guards (driver's calls 2026-10-22, on the page "Keeping up with
AI-written code"):

- a study session can start from INSIDE another dialogue, now, on the `tutor`
  node — the driver may need the topic to rule on that page, so waiting for
  the dialogue to end does not work — and the driver's answers come back
  through the tutor, not through `task`;
- the driver starts one from the page itself: select the passage, annotate it
  `study: …`, send. It studies that passage and costs the dialogue no round
  ("I'd trigger this with … the annotation pop up … select the exact thing");
- every page that changed code teaches the change and proposes cards, and the
  Cards page is where the driver keeps or drops them ("After it builds should
  probably always happen").

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
    # after a build, always; before one, only when a call needs it
    for node in ("task", "implement"):
        text = tasks.prompt_text(node=node)
        assert "Understand what changed" in text
        assert "$pipeline cards propose --source <repo>" in text
    assert "Before you decide" in tasks.prompt_text(node="task")
    tutor = tasks.prompt_text(node="tutor")
    assert "edits nothing in the repo" in tutor
    assert "queueKey:'study-answers'" in tutor and "verdict=continue" in tutor


# --------------------------------------------------------------------------- from an annotation


def test_a_study_annotation_opens_a_thread_on_the_passage_and_costs_no_round(monkeypatch):
    from runnerlib import study_thread, surface
    from tests.test_dialogue_actions import commands, jsonl_board, note, poll_json
    d, work = scratch(), scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    tasks.record(reg, "task-parent").update(cwd=str(work), session_id="sess-parent")
    replies, started = [], []
    monkeypatch.setattr(surface, "agent_reply",
                        lambda cfg, reg, ghc, log, path, msg: replies.append(msg))
    monkeypatch.setattr(study_thread, "start",
                        lambda cfg, reg, topic, task, page, passage="", cwd="", log=None:
                        started.append((topic, task, page, passage)) or {"id": "thread-x"})
    art = str(d / "task-task-parent.html")
    meta = {"kind": "task", "task": "task-parent", "cwd": str(work), "open": True}

    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, art, meta, poll_json(
            note("Study: why is the lock taken here?", "with reg.lock(): reg.save()")))
    assert started == [("why is the lock taken here?", "task-parent", art,
                        "with reg.lock(): reg.save()")]
    assert commands(cfg) == [], "no dialogue round, and no slow tutor session"
    assert replies[0].startswith("Study thread open")

    # beside an ordinary annotation: the thread opens, the round gets only the rest
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, art, meta, poll_json(
            note("study: the board", "Board"), note("drop the second table")))
    assert [c["feedback"] for c in commands(cfg)] == ["drop the second table"]
    assert len(started) == 2

    # a thread that cannot start says why, on the page
    monkeypatch.setattr(study_thread, "start", lambda *a, **k: (_ for _ in ()).throw(
        ValueError("task 'task-parent' has no working directory to study in")))
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, None, lambda *a: None, art, meta,
                             poll_json(note("study: x")))
    assert replies[-1].startswith("Study not started: task 'task-parent' has no working")


# --------------------------------------------------------------------------- from the UI


def ui(monkeypatch, snapshot=None):
    """The page server over loopback, on a scratch config, reading `snapshot`."""
    import threading
    from http.server import ThreadingHTTPServer

    import statusd
    from runnerlib import config as config_mod
    tmp = scratch()
    cfg = config_mod.Config({"runner": {"data_dir": str(tmp / "state")},
                             "limits": {"allowed_actors": ["driver"]},
                             "learning": {"cards_dir": str(tmp / "deck")}}, tmp / "c.toml")
    monkeypatch.setattr(statusd, "_CFG", cfg)
    monkeypatch.setattr(statusd, "read_snapshot", lambda *a: snapshot or {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), statusd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return cfg, srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(url, **fields):
    import urllib.error
    import urllib.parse
    import urllib.request

    class NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    try:
        urllib.request.build_opener(NoFollow).open(req, timeout=5)
    except urllib.error.HTTPError as e:
        return e.code, urllib.parse.unquote_plus(e.headers.get("Location") or "")
    raise AssertionError("expected a redirect")


def get(url):
    import urllib.request
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def test_the_bar_above_a_dialogue_page_starts_a_study_session_beside_it(monkeypatch):
    work = scratch()
    page = "/state/surfaces/task-task-parent.html"
    snap = {"surfaces": [{"kind": "task", "task": "task-parent", "cwd": str(work),
                          "path": "/session/d0b0498018615008", "artifact": page}]}
    cfg, srv, url = ui(monkeypatch, snap)
    try:
        bar = get(url + "/view/d0b0498018615008")
        assert 'action="/study"' in bar and 'name="from" value="task-parent"' in bar
        assert 'action="/study"' not in get(url + "/view/aaaaaaaaaaaaaaaa"), \
            "a page that is not a dialogue's gets no study box"

        code, where = post(url + "/study", topic="the FSRS scheduler", **{
            "from": "task-parent", "back": "/view/d0b0498018615008"})
        assert code == 303 and where.startswith("/view/d0b0498018615008?notice=Study started")
        (ev,) = [e for e in board(cfg.data_dir).read_since(0) if e["kind"] == "command"]
        p = ev["payload"]
        assert p["target"] == tasks.TARGET_START and p["node"] == "tutor"
        assert p["cwd"] == str(work) and page in p["task_text"]

        # a page the fleet does not know, and no directory: refused, nothing written
        code, where = post(url + "/study", topic="x", **{"from": "task-gone",
                                                           "back": "/view/d0b0498018615008"})
        assert "Study not started: no directory" in where
        assert len([e for e in board(cfg.data_dir).read_since("after-the-refusal")
                    if e["kind"] == "command"]) == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_deck_page_shows_your_cards_and_team_cards_and_starts_a_study(monkeypatch):
    from runnerlib import cards
    cfg, srv, url = ui(monkeypatch)
    try:
        (mine,) = cards.propose(cfg.cards_dir, [{"front": "In cadre-runner, why WAL?"}],
                                "cadre-runner")
        cards.accept(cfg.cards_dir, mine["id"])
        cards.merge_team(cfg.cards_dir, "http://team:8183",
                         [{"id": "c-t1", "source": "work-api", "anchor": "",
                           "front": "In work-api, what retries a webhook?"}])
        page = get(url + "/cards")
        assert "In cadre-runner, why WAL?" in page and "what retries a webhook?" in page
        assert "<b>2</b> due now" in page and "· team card" in page
        assert 'action="/study"' in page

        work = scratch()
        code, where = post(url + "/study", topic="how sync works", cwd=str(work))
        assert where.startswith("/cards?notice=Study started")

        # a proposed card is kept or dropped right on the page
        keep, drop = cards.propose(cfg.cards_dir, [{"front": "In cadre-runner, why rowid?"},
                                                   {"front": "a weak card"}], "cadre-runner")
        page = get(url + "/cards")
        assert f'name="id" value="{keep["id"]}"' in page and 'value="drop"' in page
        assert "Kept" in post(url + "/cards", id=keep["id"], do="keep")[1]
        assert "Dropped" in post(url + "/cards", id=drop["id"], do="drop")[1]
        assert cards.get(cfg.cards_dir, keep["id"])["status"] == "accepted"
        assert cards.get(cfg.cards_dir, drop["id"])["status"] == "discarded"
        assert "Nothing done: no card" in post(url + "/cards", id="c-nope", do="keep")[1]
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_project_launches_a_study_as_well_as_a_task(monkeypatch):
    from runnerlib import projects
    cfg, srv, url = ui(monkeypatch)
    try:
        work = scratch()
        rec = projects.create(cfg.data_dir, "Cadre", dirs=[str(work)])
        code, where = post(url + f"/projects/{rec['id']}/tasks",
                           text="how a dialogue round works", cwd=str(work), node="tutor")
        assert code == 303 and "Launched" in where
        (ev,) = [e for e in board(cfg.data_dir).read_since(0) if e["kind"] == "command"]
        assert ev["payload"]["node"] == "tutor"
        assert ev["payload"]["task_text"].startswith("Study session: how a dialogue round works")
    finally:
        srv.shutdown()
        srv.server_close()
