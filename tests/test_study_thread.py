"""Study threads: a side chat beside a page, with an agent that stays awake.

What this guards (driver's calls 2026-10-23, on the page "Keeping up with
AI-written code"):

- pressing Study opens a thread instead of a slow dialogue round: "it would be
  better if it started up an agent that then polled back to the study session
  so it stayed awake and ready" — and the thread keeps a review page of its own;
- the thread's agent is "a copy of the page agent's own session" (forked), may
  read the repo and write only its own page;
- the daemon never consumes a thread's messages (the engine is the loop
  waiting on them), and wakes a thread whose engine has gone when a message
  is waiting.

No claude, no network, no review-surface CLI: the agent and the CLI are fakes.

Run directly: python3 -m pytest -q tests/test_study_thread.py
"""

import json

import pytest

from runnerlib import study_thread, surface, tasks
from tests.test_dialogue_actions import FakeCfg, FakeReg, scratch


@pytest.fixture
def quiet(monkeypatch):
    """No review-surface: opening records the session, linking is captured."""
    calls = []
    monkeypatch.setattr(surface, "_create_session_quietly",
                        lambda path, delivery=None: (_ for _ in ()).throw(OSError("no server")))
    monkeypatch.setattr(surface, "_run_cli",
                        lambda args, timeout=25: calls.append(args) or "")
    monkeypatch.setattr(surface.board_events, "emit", lambda *a, **k: None)
    return calls


def parent(work, session="sess-parent"):
    cfg, reg = FakeCfg(scratch()), FakeReg()
    rec = tasks.record(reg, "task-parent")
    rec.update(cwd=str(work), session_id=session)
    return cfg, reg


def test_study_opens_a_thread_page_beside_the_dialogue_and_starts_its_engine(quiet):
    work = scratch()
    cfg, reg = parent(work)
    t = study_thread.start(cfg, reg, "why the lock", "task-parent", "/pages/task-parent.html",
                           passage="with reg.lock(): reg.save()", spawn=lambda cfg, tid: 4242)

    assert t["pid"] == 4242 and t["fork_from"] == "sess-parent" and t["cwd"] == str(work)
    page = study_thread.thread_dir(cfg, t["id"]) / "page.html"
    assert "/surface-theme.css" in page.read_text() and "with reg.lock()" in page.read_text()
    meta = surface.sessions(cfg)[str(page)]
    assert meta["kind"] == study_thread.KIND and meta["thread"] == t["id"]
    assert ["link", "child-of", str(page), "/pages/task-parent.html"] in quiet
    assert ["link", "thread-of", str(page), "/pages/task-parent.html"] in quiet, \
        "the parent page's bell lists its threads"
    assert t["model"] == "sonnet", "a chat answers fast by default"
    assert study_thread.load(cfg, t["id"])["pid"] == 4242

    with pytest.raises(ValueError, match="no working directory"):
        study_thread.start(FakeCfg(scratch()), FakeReg(), "x", "task-gone", "", spawn=lambda *a: 1)


def test_the_agent_is_a_fork_that_reads_anything_and_writes_only_its_page():
    cfg = FakeCfg(scratch())
    rec = {"id": "thread-20261023-000000-abcdef", "page": "/t/page.html", "cwd": "/repo",
           "model": "sonnet", "topic": "why", "fork_from": "sess-parent", "session_id": ""}
    argv = study_thread.claude_argv(cfg, rec)
    assert argv[argv.index("--resume") + 1] == "sess-parent" and "--fork-session" in argv
    allowed = argv[argv.index("--allowedTools") + 1: argv.index("--append-system-prompt")]
    assert "Edit(//t/page.html)" in allowed and "Write(//t/page.html)" in allowed
    assert not any(a in ("Edit", "Write", "Bash") for a in allowed), "no blanket writes"
    assert [a for a in allowed if a.startswith("Bash(")][0].endswith(" cards propose:*)")
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "edits nothing in the repo" in prompt and "/t/page.html" in prompt

    # once it has its own session, a restart resumes THAT, not a new fork
    argv = study_thread.claude_argv(cfg, {**rec, "session_id": "sess-thread"})
    assert argv[argv.index("--resume") + 1] == "sess-thread" and "--fork-session" not in argv


def test_who_answers_is_a_setting_a_start_flag_and_a_chat_command(quiet):
    # "can I choose the models for this? ... For parts I may want opus" (2026-10-23)
    work = scratch()
    cfg, reg = parent(work)
    cfg.learning = {"thread_model": "haiku"}
    assert study_thread.start(cfg, reg, "q", "task-parent", "", spawn=lambda *a: 1)["model"] == "haiku"
    t = study_thread.start(cfg, reg, "q", "task-parent", "", spawn=lambda *a: 1, model="opus")
    assert t["model"] == "opus"

    started = []

    def factory(argv, cwd):
        started.append(argv[argv.index("--model") + 1])
        return agents.pop(0)
    agents = [FakeAgent(["first"]), FakeAgent(["deep answer", "bye"])]
    cli = scripted_cli([
        {"status": "feedback", "prompts": [{"prompt": "/model sonnet", "tag": "message"},
                                           {"prompt": "and the second lock?", "tag": "message"}]},
        {"status": "ended", "prompts": []},
    ], [])
    study_thread.run(cfg, t["id"], cli=cli, agent_factory=factory, log=lambda *a: None)

    assert started == ["opus", "sonnet"], "a switch restarts the process on the new model"
    rec = study_thread.load(cfg, t["id"])
    assert rec["model"] == "sonnet"
    assert study_thread.claude_argv(cfg, rec)[study_thread.claude_argv(cfg, rec).index("--resume") + 1] \
        == "sess-thread", "the restart resumes the thread's own session"


def test_a_model_that_will_not_start_falls_back_to_the_one_that_worked(quiet):
    cfg, t = make_thread(quiet)

    class Broken(FakeAgent):
        def ask(self, text):
            raise RuntimeError("the agent process exited mid-answer")
    agents = [FakeAgent(["first"]), Broken([]), FakeAgent(["still here", "bye"])]
    calls = []
    cli = scripted_cli([
        {"status": "feedback", "prompts": [{"prompt": "/model opus-9", "tag": "message"}]},
        {"status": "feedback", "prompts": [{"prompt": "go on", "tag": "message"}]},
        {"status": "ended", "prompts": []},
    ], calls)
    study_thread.run(cfg, t["id"], cli=cli, agent_factory=lambda a, c: agents.pop(0),
                     log=lambda *a: None)

    assert calls[1][calls[1].index("--agent-reply") + 1].startswith("Now answering with opus-9")
    told = calls[2][calls[2].index("--agent-reply") + 1]
    assert told.startswith("opus-9 would not start, so this is sonnet again") and told.endswith("still here")
    assert study_thread.load(cfg, t["id"])["model"] == "sonnet"


class FakeAgent:
    def __init__(self, answers):
        self.asked, self.answers, self.session_id, self.closed = [], list(answers), "", False

    def ask(self, text):
        self.asked.append(text)
        self.session_id = "sess-thread"
        return self.answers.pop(0)

    def close(self):
        self.closed = True


def scripted_cli(responses, calls):
    def cli(args, timeout=25):
        calls.append(args)
        return json.dumps(responses.pop(0))
    return cli


def make_thread(quiet_calls):
    work = scratch()
    cfg, reg = parent(work)
    t = study_thread.start(cfg, reg, "why the lock", "task-parent", "/pages/p.html",
                           passage="with reg.lock(): reg.save()", spawn=lambda *a: 1)
    return cfg, t


def test_the_engine_answers_listens_and_answers_again_until_the_page_ends(quiet):
    cfg, t = make_thread(quiet)
    agent, calls = FakeAgent(["first answer", "second answer", "proposed 1 card"]), []
    cli = scripted_cli([
        {"status": "feedback", "prompts": [{"prompt": "so who takes it?", "tag": "text",
                                            "text": "reg.lock()"}]},
        {"status": "ended", "prompts": []},
    ], calls)

    assert study_thread.run(cfg, t["id"], cli=cli, agent_factory=lambda argv, cwd: agent,
                            log=lambda *a: None) == "ended"

    # the question from the Study button first, with its passage
    assert agent.asked[0].startswith("# Study thread"), "the brief rides the first message"
    assert agent.asked[0].endswith("Their first message:\n\n> with reg.lock(): reg.save()"
                                   "\n\nwhy the lock")
    assert t["page"] in agent.asked[0]
    # each answer rides the next poll, which then waits for the driver
    assert calls[0][calls[0].index("--agent-reply") + 1] == "first answer"
    assert agent.asked[1] == "> reg.lock()\n\nso who takes it?"
    assert calls[1][calls[1].index("--agent-reply") + 1] == "second answer"
    # ending the page gets the cards pass, then the engine stops
    assert agent.asked[2].startswith("THREAD ENDED") and agent.closed
    rec = study_thread.load(cfg, t["id"])
    assert (rec["status"], rec["pid"], rec["session_id"]) == ("ended", 0, "sess-thread")


def test_an_idle_engine_goes_to_sleep_and_keeps_its_session(quiet, monkeypatch):
    cfg, t = make_thread(quiet)
    now = [1000.0]
    monkeypatch.setattr(study_thread, "IDLE_LIMIT_S", 60)

    def cli(args, timeout=25):
        now[0] += 61                         # a poll chunk passes with nothing sent
        return json.dumps({"status": "timeout", "prompts": []})

    agent = FakeAgent(["first answer"])
    assert study_thread.run(cfg, t["id"], cli=cli, agent_factory=lambda a, c: agent,
                            clock=lambda: now[0], log=lambda *a: None) == "idle"
    rec = study_thread.load(cfg, t["id"])
    assert rec["status"] == "asleep" and rec["session_id"] == "sess-thread" and rec["pid"] == 0


def test_the_daemon_wakes_a_sleeping_thread_only_when_a_message_waits(quiet):
    cfg, t = make_thread(quiet)
    page = t["page"]
    sess = {page: {**surface.sessions(cfg)[page], "key": "k1"}}
    woke = []
    spawn = lambda cfg, tid: woke.append(tid) or 777

    rec = study_thread.load(cfg, t["id"])
    rec.update(pid=0, status="asleep")
    study_thread.save(cfg, rec)
    assert study_thread.wake(cfg, lambda *a: None, sess,
                             status=lambda k: {"status": "open", "pending_prompts": 0},
                             spawn=spawn) == []
    assert study_thread.wake(cfg, lambda *a: None, sess,
                             status=lambda k: {"status": "open", "pending_prompts": 1},
                             spawn=spawn) == [t["id"]]
    assert study_thread.load(cfg, t["id"])["pid"] == 777

    # a live engine, or an ended thread, is left alone
    import os
    rec = study_thread.load(cfg, t["id"])
    rec["pid"] = os.getpid()
    study_thread.save(cfg, rec)
    assert study_thread.wake(cfg, lambda *a: None, sess,
                             status=lambda k: {"status": "open", "pending_prompts": 1},
                             spawn=spawn) == []
    rec.update(pid=0, status="ended")
    study_thread.save(cfg, rec)
    assert study_thread.wake(cfg, lambda *a: None, sess,
                             status=lambda k: {"status": "open", "pending_prompts": 1},
                             spawn=spawn) == []


def test_the_daemon_never_consumes_a_thread_page(quiet, monkeypatch):
    cfg, t = make_thread(quiet)
    s = surface.sessions(cfg)
    s[t["page"]]["key"] = "k1"
    # beside it, an ordinary dialogue page with a send waiting: that one IS consumed
    s["/pages/task-other.html"] = {"kind": "task", "task": "task-other", "key": "k2",
                                   "open": True}
    surface._save_sessions(cfg, s)
    monkeypatch.setattr(surface, "_read_outbox", lambda cfg: [{"key": "k1"}, {"key": "k2"}])
    monkeypatch.setattr(surface, "_sweep_stale", lambda *a: None)
    monkeypatch.setattr(surface, "_ensure_server", lambda *a: None)
    monkeypatch.setattr(surface, "_adopt_unowned", lambda *a: None)
    monkeypatch.setattr(surface, "_attach_requested", lambda *a: None)
    monkeypatch.setattr(study_thread, "wake", lambda *a, **k: [])
    consumed = []
    monkeypatch.setattr(surface, "_consume", lambda *a: consumed.append(a[4]))
    try:
        surface._tick(cfg, FakeReg(), None, lambda *a: None)
    except Exception:
        pass                                # later tick stages need more world than this
    assert consumed == ["/pages/task-other.html"]


def test_the_driver_talking_about_an_ended_session_does_not_end_the_thread(quiet):
    cfg, t = make_thread(quiet)
    agent = FakeAgent(["first", "it stops polling", "bye"])
    cli = scripted_cli([
        {"status": "feedback", "prompts": [{"prompt": "what happens after the session has ended?",
                                            "tag": "message"}]},
        {"status": "ended", "prompts": []},
    ], [])
    study_thread.run(cfg, t["id"], cli=cli, agent_factory=lambda a, c: agent, log=lambda *a: None)
    assert agent.asked[1] == "what happens after the session has ended?"
    assert agent.asked[2].startswith("THREAD ENDED")

    # the CLI's own session-over line, outside the JSON, still ends it
    agent = FakeAgent(["first", "bye"])
    cfg, t = make_thread(quiet)
    assert study_thread.run(cfg, t["id"], cli=lambda args, timeout=25: "The driver ended the session.",
                            agent_factory=lambda a, c: agent, log=lambda *a: None) == "ended"


class Broken(FakeAgent):
    def ask(self, text):
        self.asked.append(text)
        raise BrokenPipeError("the agent process is gone")


def test_a_bad_model_left_by_an_earlier_engine_still_falls_back(quiet):
    cfg, t = make_thread(quiet)
    rec = study_thread.load(cfg, t["id"])
    rec.update(model="opsu", good_model="sonnet", session_id="sess-thread")
    study_thread.save(cfg, rec)
    started, calls = [], []

    def factory(argv, cwd):
        started.append(argv[argv.index("--model") + 1])
        return agents.pop(0)
    agents = [Broken([]), FakeAgent(["the answer", "bye"])]
    cli = scripted_cli([
        {"status": "feedback", "prompts": [{"prompt": "go on", "tag": "message"}]},
        {"status": "ended", "prompts": []},
    ], calls)
    study_thread.run(cfg, t["id"], cli=cli, agent_factory=factory, log=lambda *a: None)

    assert started == ["opsu", "sonnet"]
    told = calls[1][calls[1].index("--agent-reply") + 1]
    assert told == "opsu would not start, so this is sonnet again.\n\nthe answer"
    assert study_thread.load(cfg, t["id"])["model"] == "sonnet"


class DiesSecond(FakeAgent):
    """Answers the first question, then its process dies."""
    def ask(self, text):
        if self.asked:
            self.asked.append(text)
            raise RuntimeError("the agent process exited mid-answer")
        return super().ask(text)


def test_an_agent_that_dies_mid_answer_is_restarted_once_then_the_driver_is_told(quiet):
    for after_restart, told in ((FakeAgent(["recovered", "bye"]), "recovered"),
                                (Broken([]), study_thread.RETRY_LATER)):
        cfg, t = make_thread(quiet)
        agents = [DiesSecond(["first"]), after_restart, FakeAgent(["bye"])]
        calls = []
        cli = scripted_cli([
            {"status": "feedback", "prompts": [{"prompt": "why?", "tag": "message"}]},
            {"status": "ended", "prompts": []},
        ], calls)
        assert study_thread.run(cfg, t["id"], cli=cli, agent_factory=lambda a, c: agents.pop(0),
                                log=lambda *a: None) == "ended"
        assert calls[1][calls[1].index("--agent-reply") + 1] == told


def test_an_error_result_from_the_cli_is_a_failure_not_an_answer():
    import sys
    script = ("import sys, json; sys.stdin.readline(); print(json.dumps({'type': 'result', "
              "'is_error': True, 'result': 'model not found', 'session_id': 's1'}), flush=True)")
    agent = study_thread.Agent([sys.executable, "-c", script], cwd=str(scratch()))
    try:
        with pytest.raises(RuntimeError, match="model not found"):
            agent.ask("hello")
    finally:
        agent.close()
