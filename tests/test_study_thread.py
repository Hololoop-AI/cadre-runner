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
    assert t["model"] == "opus", "Opus at medium effort by default (driver, 8 Nov 2026)"
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
    assert told.startswith("opus-9 would not start, so this is opus again") and told.endswith("still here")
    assert study_thread.load(cfg, t["id"])["model"] == "opus"


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
    assert agent.asked[0].startswith("# Thread"), "the brief rides the first message"
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
    monkeypatch.setattr(surface, "_consume", lambda *a: consumed.append(a[3]))
    try:
        surface._tick(cfg, FakeReg(), lambda *a: None)
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


# --------------------------------------------------------------------------- talk, then study


def test_a_thread_started_to_talk_opens_on_the_passage_and_offers_study(quiet):
    """"select a passage, start a message thread about it ... Study is an option
    inside the thread: I just tell it I'd like to study, or it offers me some
    options to pick from" (driver, 7 Nov 2026)."""
    work = scratch()
    cfg, reg = parent(work)
    t = study_thread.start(cfg, reg, "this passage", "task-parent", "/pages/p.html",
                           passage="Three clinics quote a price that includes aftercare.",
                           spawn=lambda *a: 1, mode="talk")
    # nothing typed: the thread is named by the passage, not by the fallback
    assert t["mode"] == "talk"
    assert t["title"] == "Thread: Three clinics quote a price that includes aftercare."
    page = study_thread.thread_dir(cfg, t["id"]) / "page.html"
    assert "<h1>Thread: Three clinics" in page.read_text()
    assert "teach you" in page.read_text() and "send a note" in page.read_text(), \
        "the page says what else the thread does"
    assert surface.sessions(cfg)[str(page)]["title"].startswith("Thread: Three clinics")

    argv = study_thread.claude_argv(cfg, t)
    allowed = argv[argv.index("--allowedTools") + 1: argv.index("--append-system-prompt")]
    assert any(a.endswith(" study:*)") for a in allowed), "it can start a study session"
    assert any(a.endswith(f" study-thread promote {t['id']}:*)") for a in allowed), \
        "it can send a note to its own page, and no other"
    assert not any(a in ("Edit", "Write", "Bash") for a in allowed), "still no blanket writes"
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert study_thread.OPENING["talk"] in prompt
    assert "edits nothing in the repo" in prompt
    assert "study \"<the topic, in their words>\" --from task-parent" in prompt
    assert "numbered options" in prompt and "One of them is always to learn about" in prompt
    assert f"study-thread promote {t['id']}" in prompt and "Never send one they have not seen" in prompt

    # a long passage is cut for the title; a typed note names it as typed
    long = study_thread.thread_title("talk", "this passage", "word " * 40)
    assert long.startswith("Thread: word word") and long.endswith("…") and len(long) <= 70
    assert study_thread.thread_title("learn", "why aftercare", "x") == "Learn: why aftercare"
    assert study_thread.thread_title("study", "why aftercare", "x") == "Learn: why aftercare", \
        "records from before the rename say study"
    with pytest.raises(ValueError, match="not a thread mode"):
        study_thread.start(cfg, reg, "q", "task-parent", "", spawn=lambda *a: 1, mode="chat")


def test_a_learning_thread_teaches_explanation_first_and_an_old_study_record_still_runs(quiet):
    """"Study sounds too much like you're reviewing content that you know ... The
    idea would usually be to actually learn about an unknown thing on the page
    via explanation" (driver, 8 Nov 2026)."""
    cfg, t = make_thread(quiet)
    assert t["mode"] == "learn" and t["title"] == "Learn: why the lock"
    prompt = study_thread.system_prompt(cfg, t)
    assert study_thread.OPENING["learn"] in prompt
    flat = " ".join(prompt.split())
    assert "Explain first" in flat and "do not quiz them on it cold" in flat
    assert "Tie it to the decision" in flat and "Leave out trivia" in flat
    old = {**t, "mode": "study"}
    assert study_thread.OPENING["learn"] in study_thread.system_prompt(cfg, old)


def test_a_settled_note_is_carried_to_the_page_and_rides_its_next_round(quiet, monkeypatch):
    """"We will need a way to promote a conversation conclusion to the main
    agent, possibly adding something to the queue" (driver, 8 Nov 2026)."""
    posted = []
    monkeypatch.setattr(study_thread, "_post", lambda url, text: posted.append((url, text)) or True)
    work = scratch()
    cfg, reg = parent(work)
    _record(cfg, "/pages/p.html", "pagekey")
    t = study_thread.start(cfg, reg, "this passage", "task-parent", "/pages/p.html",
                           passage="Clinic B includes aftercare", spawn=lambda *a: 1, mode="talk")

    note = study_thread.promote(cfg, t["id"], "Make clinic B the pick: aftercare is included.")
    assert note["task"] == "task-parent"
    assert posted[-1][0].endswith("/api/pagekey/agent-reply")
    assert posted[-1][1].startswith("Queued for this page's next round, from the thread")
    with pytest.raises(ValueError):
        study_thread.promote(cfg, t["id"], "   ")

    # it rides the driver's next round, after their own words, then is gone
    tasks.record(reg, "task-parent")["iteration"] = 2
    ev = tasks.write_feedback(cfg, reg, "task-parent", str(work),
                              [{"text": "tighten the table", "anchor": ""}])
    fb = ev["payload"]["feedback"]
    assert fb.index("tighten the table") < fb.index("Make clinic B the pick")
    assert "> Clinic B includes aftercare" in fb and "From the side thread" in fb
    assert tasks.carried(cfg, "task-parent") == []


def _record(cfg, path, key):
    sess = surface.sessions(cfg)
    sess[path] = {"key": key, "open": True, "kind": "task", "task": "task-parent"}
    surface._save_sessions(cfg, sess)


def test_the_chat_window_shows_what_the_agent_is_doing_while_it_answers(quiet, monkeypatch):
    """"It is quite slow though isn't it. Does it show thinking? Or some kind of
    you're waiting indicator?" (driver, 8 Nov 2026)."""
    ev = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "They want the trade-off, not the history."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/w/notes/clinics.md"}},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "aftercare"}},
        {"type": "text", "text": "the answer itself is not progress"}]}}
    assert study_thread.progress_lines(ev) == [
        "They want the trade-off, not the history.", "Reading clinics.md…",
        "Searching for aftercare…"]
    assert study_thread.progress_lines({"type": "result"}) == []

    # the engine hands each agent a relay to the thread page's thinking route
    cfg, t = make_thread(quiet)
    sess = surface.sessions(cfg)
    sess[t["page"]]["key"] = "threadkey"
    surface._save_sessions(cfg, sess)
    posted = []
    monkeypatch.setattr(study_thread, "_post", lambda url, text: posted.append((url, text)) or True)
    agent = FakeAgent(["first", "closing"])
    study_thread.run(cfg, t["id"], cli=scripted_cli([{"status": "ended", "prompts": []}], []),
                     agent_factory=lambda a, c: agent, log=lambda *a: None)
    agent.progress("Reading clinics.md…")
    url = f"{surface.upstream()}/api/threadkey/agent-thinking"
    assert posted == [(url, "Opening a copy of the page's own session to answer from…"),
                      (url, "Reading clinics.md…")], "the first start says so at once"


def test_a_thread_page_never_replaces_its_dialogue_page():
    """A thread's page carries its task id, but it sits beside the dialogue's
    page: marking it a replacement made the dialogue read "This is an older
    page. The newest in this line of work is Thread: …" (seen 8 Nov 2026)."""
    store = {"/s/task-a.html": {"task": "task-a", "key": "dialogue", "kind": "task", "opened": 1},
             "/t/page.html": {"task": "task-a", "key": "thread", "kind": "study", "opened": 2}}
    assert surface._replaced_page(store, "/t/other.html", "task-a", "study") == ""
    # ...and a real next page of the task (a handoff) replaces the dialogue, not the thread
    assert surface._replaced_page(store, "/s/task-a-implement.html", "task-a", "task") == "dialogue"


def test_thinking_streams_into_the_chat_a_sentence_at_a_time_and_only_once():
    """"It really does need better indicators (and ideally some level of
    thinking tokens actually being streamed)" (driver, 8 Nov 2026). The agent
    runs with summarized thinking streamed in pieces; the window gets whole
    sentences as they finish, and the full thinking block that follows is not
    posted a second time."""
    import sys
    argv = study_thread.claude_argv(FakeCfg(scratch()), {
        "id": "thread-20261108-000000-abcdef", "page": "/t/page.html", "cwd": "/w",
        "model": "sonnet", "topic": "t", "fork_from": "", "session_id": ""})
    assert "--include-partial-messages" in argv
    assert argv[argv.index("--thinking-display") + 1] == "summarized"
    assert argv[argv.index("--effort") + 1] == "medium"

    def delta(text):
        return {"type": "stream_event", "event": {"type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": text}}}
    events = [delta("They want the trade-off"), delta(", not the history. Then I"),
              delta(" check the page"), {"type": "stream_event", "event": {"type": "content_block_stop"}},
              {"type": "assistant", "message": {"content": [
                  {"type": "thinking", "thinking": "They want the trade-off, not the history. "
                                                   "Then I check the page"},
                  {"type": "tool_use", "name": "Read", "input": {"file_path": "/w/page.html"}}]}},
              {"type": "result", "result": "the answer", "session_id": "s1"}]
    script = ("import sys, json; sys.stdin.readline()\n"
              f"for e in {events!r}: print(json.dumps(e), flush=True)")
    agent = study_thread.Agent([sys.executable, "-c", script], cwd=str(scratch()))
    lines = []
    agent.progress = lines.append
    try:
        assert agent.ask("hello") == "the answer"
    finally:
        agent.close()
    assert lines == ["They want the trade-off, not the history.", "Then I check the page",
                     "Reading page.html…"]
