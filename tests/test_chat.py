"""The runner's half of the page server's message threads.

  * direct chat: a poll whose prompts ALL carry `channel: "chat"` resumes the
    page's own session and posts its answer with `agent-reply`; no page round
    runs and the page is left alone. Any other batch is a round, as before.
  * thinking: a run with a page posts its stream-json thinking blocks to
    `agent-thinking` while it works, and the run's exit code is still its own.
  * agent questions: threads an agent started on its page are learned when its
    turn ends, and an answer that arrives after that becomes a chat turn that
    replies in the thread.

No claude, no network beyond a loopback stand-in, no review-surface CLI.
"""

import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from runnerlib import chat, runs, session_pump, surface, tasks, threads
from tests.test_dialogue_actions import (FakeCfg, FakeReg, commands, jsonl_board,
                                         note, scratch, seeded)


def chat_note(text):
    return {"prompt": text, "text": "", "channel": "chat"}


def poll(*prompts) -> str:
    return json.dumps({"status": "open", "prompts": list(prompts)})


@pytest.fixture
def posts(monkeypatch):
    """Every POST the chat module makes, instead of a page server."""
    sent = []
    monkeypatch.setattr(chat, "_post", lambda url, body: sent.append((url, body)) or True)
    return sent


@pytest.fixture
def spawns(monkeypatch):
    calls = []

    def fake_spawn(claude_bin, prompt, wt_path, model, effort, permission_mode,
                   timeout, run_dir, **kw):
        calls.append({"prompt": prompt, "cwd": str(wt_path), "model": model, **kw})
        return 4242
    monkeypatch.setattr(chat.runs, "spawn", fake_spawn)
    return calls


def task_with_session(d, work):
    cfg, reg = FakeCfg(d), FakeReg()
    seeded(d)
    rec = tasks.record(reg, "task-demo")
    rec.update(cwd=str(work), session_id="sess-1234abcd", iteration=3)
    return cfg, reg, rec


# --------------------------------------------------------------------------- which batch is a chat


def test_only_a_batch_that_is_all_chat_is_a_direct_chat():
    assert chat.is_direct_chat({"prompts": [chat_note("why?"), chat_note("and?")]})
    assert not chat.is_direct_chat({"prompts": [chat_note("why?"), note("fix this")]})
    assert not chat.is_direct_chat({"prompts": []})
    assert not chat.is_direct_chat(None)


def test_the_page_key_is_the_page_servers():
    d = scratch()
    page = d / "p.html"
    page.write_text("x")
    want = hashlib.sha256(os.path.realpath(page).encode()).hexdigest()[:16]
    assert surface.page_key(page) == want


# --------------------------------------------------------------------------- direct chat


def test_enter_in_the_chat_resumes_the_session_and_runs_no_round(posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    page = str(d / "task-task-demo.html")
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True,
            "key": "aaaaaaaaaaaaaaa1"}
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, lambda *a: None, page, meta,
                             poll(chat_note("why the second table?")))

    assert commands(cfg) == [], "a chat writes no feedback command: no round"
    assert len(spawns) == 1
    s = spawns[0]
    assert s["session_id"] == "sess-1234abcd" and s["resume"] is True
    assert s["cwd"] == str(work)
    assert "why the second table?" in s["prompt"] and "not a page round" in s["prompt"]
    assert s["thinking_url"].endswith("/api/aaaaaaaaaaaaaaa1/agent-thinking")
    assert s["extra_env"]["CADRE_SURFACE_OUT"] == page
    assert "{session}" in s["argv"] and "stream-json" in s["argv"]  # spawn fills them
    run = next(iter(rec["active_runs"].values()))
    assert run["kind"] == "chat" and run["reply_key"] == "aaaaaaaaaaaaaaa1"
    assert rec["iteration"] == 3, "a chat is not a round"


def test_a_batch_with_page_feedback_in_it_is_a_round_as_before(posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, _ = task_with_session(d, work)
    meta = {"kind": "task", "task": "task-demo", "cwd": str(work), "open": True}
    with jsonl_board(d):
        surface._handle_poll(cfg, reg, lambda *a: None, str(d / "p.html"), meta,
                             poll(chat_note("why?"), note("drop the table", "Table")))
    assert spawns == []
    assert [p["target"] for p in commands(cfg)] == ["task:feedback"]
    assert "why?" in commands(cfg)[0]["feedback"]


def test_a_chat_during_a_running_turn_waits_for_it(posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    rec["active_runs"] = {"r1": {"stage": "task"}}
    chat.ask(cfg, reg, lambda *a: None, "task-demo", "hello", "k1", "/p.html")
    assert spawns == [] and len(rec["chats"]) == 1
    rec["active_runs"] = {}
    assert chat.start_next(cfg, reg, lambda *a: None, "task-demo")
    assert len(spawns) == 1 and rec["chats"] == []


def test_a_finished_chat_posts_its_answer_and_opens_no_page(posts, monkeypatch):
    import pipeline
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    rec["active_runs"] = {"r-chat": {"kind": "chat", "stage": "chat", "pid": 1,
                                     "run_dir": "/nonexistent", "started": time.time(),
                                     "reply_key": "aaaaaaaaaaaaaaa1"}}
    opened = []
    monkeypatch.setattr(pipeline.runs_mod, "finished", lambda r: True)
    monkeypatch.setattr(pipeline.runs_mod, "outcome",
                        lambda r: (True, "Because it repeats the first.", {}, {}))
    monkeypatch.setattr(pipeline.status_mod, "append_history", lambda *a: None)
    monkeypatch.setattr(pipeline.surface_mod, "open_session",
                        lambda *a, **k: opened.append(a))
    pipeline._reap_tasks(cfg, reg)
    assert posts == [(f"{surface.upstream()}/api/aaaaaaaaaaaaaaa1/agent-reply",
                      {"text": "Because it repeats the first."})]
    assert opened == [] and rec["active_runs"] == {} and rec["iteration"] == 3


def test_a_failed_chat_says_so_in_the_chat(posts):
    chat.finish(FakeReg(), lambda *a: None, "task-demo", {"reply_key": "k1"}, False, "")
    assert "could not answer" in posts[0][1]["text"]


# --------------------------------------------------------------------------- thinking


def test_thinking_blocks_are_read_from_a_stream_line():
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "Checking the table first."},
        {"type": "thinking", "thinking": ""},
        {"type": "text", "text": "not thinking"}]}})
    assert session_pump.thinking_texts(line) == ["Checking the table first."]
    assert session_pump.thinking_texts('{"type":"result","result":"x"}') == []
    assert session_pump.thinking_texts("not json") == []


class _Capture(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        _Capture.got.append((self.path, json.loads(self.rfile.read(n))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"sent"}')

    def log_message(self, *a):
        pass


def test_a_run_posts_its_thinking_while_it_works_and_keeps_its_exit_code():
    """A real spawn: a stand-in agent prints stream-json, thinks once, and
    exits 3. The relay posts the thinking to a loopback page server; the
    wrapper still records the agent's own exit code and its result."""
    _Capture.got = []
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    d = scratch()
    agent = d / "agent"
    agent.write_text("#!/bin/sh\n"
                     "echo '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"s1\"}'\n"
                     "sleep 0.3\n"
                     "echo '{\"type\":\"assistant\",\"message\":{\"content\":"
                     "[{\"type\":\"thinking\",\"thinking\":\"Reading the page first.\"}]}}'\n"
                     "sleep 0.3\n"
                     "echo '{\"type\":\"result\",\"subtype\":\"success\",\"result\":\"done\","
                     "\"total_cost_usd\":0.5}'\n"
                     "exit 3\n")
    agent.chmod(0o755)
    url = f"http://127.0.0.1:{server.server_port}/api/k1/agent-thinking"
    run_dir = d / "run"
    pid = runs.spawn("unused", "p", d, "m", None, "bypass", 60, run_dir,
                     argv=[str(agent)], thinking_url=url)
    run = {"pid": pid, "run_dir": str(run_dir), "started": time.time()}
    for _ in range(100):
        if runs.finished(run) and _Capture.got:
            break
        time.sleep(0.1)
    server.shutdown()
    assert _Capture.got == [("/api/k1/agent-thinking", {"text": "Reading the page first."})]
    ok, result, usage, record = runs.outcome(run)
    assert record["returncode"] == 3 and not ok
    assert result == "done" and usage["total_cost_usd"] == 0.5


def test_the_closing_result_of_a_stream_is_the_runs_payload():
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "We hit a rate limit in the logs we read"}]}}),
        json.dumps({"type": "result", "result": "page written", "num_turns": 4})])
    assert runs.final_payload(stream)["result"] == "page written"
    assert runs.final_payload(json.dumps({"result": "old shape"}))["result"] == "old shape"
    assert runs.final_payload("crashed before saying anything") is None
    d = scratch()
    (d / "exit").write_text("1\n")
    (d / "out.json").write_text(stream)
    ok, result, _, record = runs.outcome({"pid": 1, "run_dir": str(d), "started": time.time()})
    assert result == "page written"
    assert not record["rate_limited"], "the transcript's words are not the run's error"


def test_the_seeded_command_streams_with_thinking_text():
    from runnerlib import seed_nodes
    cmd = seed_nodes.command_for("high")
    assert cmd.startswith("claude -p --input-format stream-json --output-format stream-json ")
    assert "--replay-user-messages --thinking-display summarized" in cmd
    assert "{prompt}" not in cmd, "the prompt is the first message on stdin"


# --------------------------------------------------------------------------- agent questions


def test_the_task_prompt_tells_the_agent_how_to_ask_and_read_the_answer():
    text = tasks.prompt_text()
    assert 'review-surface message "$CADRE_SURFACE_OUT"' in text
    assert "review-surface poll <thread file> --timeout-ms 0 --json" in text
    assert "Keep working; do not wait on it." in text


def _threads_env(monkeypatch, d, page, pending=0, status="open"):
    conv = d / "state" / "conversations"
    conv.mkdir(parents=True)
    thread = conv / "2026-11-05-abc.html"
    thread.write_text("<title>Which port?</title>")
    key = surface.page_key(thread)
    monkeypatch.setenv("REVIEW_SURFACE_STATE_DIR", str(d / "state"))
    monkeypatch.setattr(threads, "_get", lambda url: {"threads": [
        {"key": key, "here": True, "title": "Which port?"},
        {"key": "elsewhere0000000", "here": False, "title": "not ours"}]}
        if url.endswith(f"/api/{surface.page_key(page)}/threads") else None)
    monkeypatch.setattr(surface, "agent_status",
                        lambda k: {"status": status, "pending_prompts": pending})
    return thread, key


def test_threads_are_learned_when_a_turn_ends(monkeypatch, posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    page = d / "task-task-demo.html"
    page.write_text("x")
    thread, key = _threads_env(monkeypatch, d, page)
    assert threads.watch_threads(cfg, reg, lambda *a: None, "task-demo", page) == 0
    assert rec["threads"] == {key: {"file": str(thread), "title": "Which port?",
                                    "page": str(page)}}
    assert threads.thread_owner(reg, key) == "task-demo"
    assert threads.thread_owner(reg, "elsewhere0000000") is None


def test_an_answer_after_the_turn_ended_becomes_a_chat_that_replies_in_the_thread(
        monkeypatch, posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    page = d / "task-task-demo.html"
    page.write_text("x")
    thread, key = _threads_env(monkeypatch, d, page, pending=1)
    polled = []
    monkeypatch.setattr(surface, "_run_cli", lambda args, timeout=25: polled.append(args)
                        or poll({"prompt": "Use 4390."}))
    assert threads.watch_threads(cfg, reg, lambda *a: None, "task-demo", page) == 1
    assert polled == [["poll", str(thread), "--timeout-ms", "4000", "--json"]]
    s = spawns[0]
    assert "Use 4390." in s["prompt"] and "Which port?" in s["prompt"]
    assert s["thinking_url"].endswith(f"/api/{key}/agent-thinking")
    assert next(iter(rec["active_runs"].values()))["reply_key"] == key


def test_an_image_answered_in_a_thread_reaches_the_agent(monkeypatch, posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    page = d / "task-task-demo.html"
    page.write_text("x")
    _threads_env(monkeypatch, d, page, pending=1)
    img = {"type": "image", "path": "/att/k/a.png", "name": "a.png", "width": 4, "height": 3}
    monkeypatch.setattr(surface, "_run_cli", lambda args, timeout=25: poll({"prompt": "", "attachments": [img]}))
    assert threads.watch_threads(cfg, reg, lambda *a: None, "task-demo", page) == 1
    assert "[attached image a.png, 4x3: /att/k/a.png]" in spawns[0]["prompt"]


def test_a_thread_is_left_to_the_agent_while_its_turn_runs(monkeypatch, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    rec["threads"] = {"k1": {"file": "/t.html", "title": "", "page": "/p.html"}}
    rec["active_runs"] = {"r1": {"stage": "task"}}
    monkeypatch.setattr(surface, "_run_cli", lambda *a, **k: pytest.fail("polled"))
    assert threads.deliver_thread(cfg, reg, lambda *a: None, "task-demo", "k1") == 0


def test_the_outbox_signal_for_a_known_thread_delivers_its_answer(monkeypatch):
    d = scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    tasks.record(reg, "task-demo")["threads"] = {"k1": {"file": "/t.html"}}
    delivered = []
    monkeypatch.setattr(surface, "_read_outbox", lambda c: [{"key": "k1"}])
    monkeypatch.setattr(surface, "_sweep_stale", lambda *a: None)
    monkeypatch.setattr(surface, "_ensure_server", lambda *a: None)
    monkeypatch.setattr(surface, "_adopt_unowned", lambda *a: None)
    monkeypatch.setattr(surface, "_attach_requested", lambda *a: None)
    monkeypatch.setattr(threads, "deliver_thread",
                        lambda c, r, log, task, key: delivered.append((task, key)))
    surface._tick(cfg, reg, lambda *a: None)
    assert delivered == [("task-demo", "k1")]


# --------------------------------------------------------------------------- mid-turn push


def _pump_files(d, *pushed):
    inbox = d / runs.INBOX
    lines = [{"message": runs.user_message("do the round"), "text": "do the round"}]
    lines += [{"message": runs.user_message(t), "text": t, "reply_url": u} for t, u in pushed]
    inbox.write_text("".join(json.dumps(l) + "\n" for l in lines))
    return inbox


def _event(**e):
    return json.dumps(e) + "\n"


def test_the_pump_forwards_a_pushed_message_and_closes_stdin_once_it_is_read():
    import io
    d = scratch()
    inbox = _pump_files(d, ("what is 2+2?", "http://x/api/k1/agent-reply"))
    out = d / "out.json"
    out.write_text("")
    sent, stdin = [], io.StringIO()
    stdin.close = lambda: None
    pump = session_pump.Pump(str(out), str(d / "exit"), inbox=str(inbox),
                             thinking_url="http://x/api/k1/agent-thinking",
                             stdin=stdin, sender=lambda url, text: sent.append((url, text)))
    pump.feed()
    assert [json.loads(l)["message"]["content"] for l in stdin.getvalue().splitlines()] == [
        "do the round", "what is 2+2?"]
    out.write_text(
        _event(type="user", isReplay=True, message={"role": "user", "content": "what is 2+2?"})
        + _event(type="assistant", message={"content": [
            {"type": "thinking", "thinking": "Simple sum."},
            {"type": "text", "text": "Page written."}]})
        + _event(type="result", result="Page written."))
    pump.relay()
    assert sent == [("http://x/api/k1/agent-thinking", "Simple sum.")], \
        "the pump posts thinking; the answer is the session's own chat-reply"
    assert not pump.open and Path(str(inbox) + ".closed").exists()
    run = {"run_dir": str(d)}
    assert not runs.push(run, "too late"), "a closed inbox takes nothing more"


def test_chat_reply_without_an_id_covers_one_message_in_order(posts, monkeypatch):
    """The way an answer was matched before messages had ids: a session
    still running the old reply command when the runner restarts."""
    d = scratch()
    _pump_files(d, ("q1", "http://x/api/k1/agent-reply"), ("q2", "http://x/api/k1/agent-reply"))
    monkeypatch.setenv("CADRE_RUN_OUT", str(d / "out.json"))
    assert chat.answer("k1", "4")
    assert posts == [(f"{surface.upstream()}/api/k1/agent-reply", {"text": "4"})]
    assert [l["text"] for l in chat.unanswered({"run_dir": str(d)})] == ["q2"], \
        "one answer covers one message, in order"


def _pushed(run_dir, *texts, key="k1"):
    """Push each text into a run the way `ask` does; their ids, in order."""
    cfg, reg = FakeCfg(scratch()), FakeReg()
    rec = tasks.record(reg, "task-demo")
    for t in texts:
        rec["active_runs"] = {"r1": {"stage": "task", "run_dir": str(run_dir)}}
        chat.ask(cfg, reg, lambda *a: None, "task-demo", t, key, "/p.html")
    return [l["id"] for l in runs.inbox({"run_dir": str(run_dir)})[1:]]


def test_an_answer_is_matched_to_its_message_by_id(posts, monkeypatch):
    d = scratch()
    _pump_files(d)
    first, second = _pushed(d, "same words", "same words")
    assert first != second, "identical messages are still two messages"
    monkeypatch.setenv("CADRE_RUN_OUT", str(d / "out.json"))
    assert chat.answer("k1", "about the second", second)
    left = chat.unanswered({"run_dir": str(d)})
    assert [l["id"] for l in left] == [first], "the second was answered, not the first"


def test_chat_reply_on_the_command_line_takes_the_message_id(monkeypatch):
    import pipeline
    got = []
    monkeypatch.setattr(pipeline.config_mod, "load", lambda path: None)
    monkeypatch.setattr(pipeline.chat_mod, "answer",
                        lambda key, text, msg_id="": got.append((key, text, msg_id)) or True)
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "chat-reply", "k1", "--id", "m-1", "yes"])
    pipeline.main()
    assert got == [("k1", "yes", "m-1")]


def test_the_pushed_message_names_the_reply_command_and_its_key(posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    run_dir = d / "run"
    run_dir.mkdir()
    _pump_files(run_dir)
    rec["active_runs"] = {"r1": {"stage": "task", "run_dir": str(run_dir)}}
    chat.ask(cfg, reg, lambda *a: None, "task-demo", "what now?", "k1", "/p.html")
    line = runs.inbox({"run_dir": str(run_dir)})[-1]
    pushed = line["text"]
    assert "what now?" in pushed and "pipeline.py" in pushed
    assert f" chat-reply k1 --id {line['id']} " in pushed, "the reply names its message"
    assert line["said"] == "what now?"


def test_a_pushed_message_not_yet_read_keeps_stdin_open_past_the_turn():
    import io
    d = scratch()
    inbox = _pump_files(d, ("one more thing", "http://x/api/k1/agent-reply"))
    out = d / "out.json"
    out.write_text(_event(type="result", result="done"))
    stdin = io.StringIO()
    stdin.close = lambda: None
    pump = session_pump.Pump(str(out), str(d / "exit"), inbox=str(inbox), stdin=stdin,
                             sender=lambda *a: True)
    pump.feed()
    pump.relay()
    assert pump.open, "the unread message is its own turn after this one"


def test_a_pushed_line_with_no_answer_becomes_a_chat_turn_with_the_drivers_own_words(
        posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    run_dir = d / "run"
    run_dir.mkdir()
    _pump_files(run_dir)
    rec["active_runs"] = {"r1": {"stage": "task", "run_dir": str(run_dir)}}
    chat.ask(cfg, reg, lambda *a: None, "task-demo", "are you there?", "k1", "/p.html")
    pushed = runs.inbox({"run_dir": str(run_dir)})[-1]
    rec["active_runs"] = {}
    assert chat.requeue_unread(cfg, reg, lambda *a: None, "task-demo",
                               {"run_dir": str(run_dir)}, "/p.html") == 1
    queued = rec["chats"][0]
    assert queued["text"] == "are you there?" and queued["reply_key"] == "k1"
    assert queued["id"] == pushed["id"], "the message keeps its id into its own turn"


def test_a_line_pushed_before_ids_existed_is_requeued_whole():
    """An inbox written by the previous runner (a deployment restarted
    mid-turn) has no raw text or id on its lines: the rendered message is
    handed on as it is rather than lost."""
    d = scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    _pump_files(d, ("The driver sent this: are you there?", "http://x/api/k1/agent-reply"))
    assert chat.requeue_unread(cfg, reg, lambda *a: None, "task-demo",
                               {"run_dir": str(d)}, "/p.html") == 1
    queued = tasks.record(reg, "task-demo")["chats"][0]
    assert queued["text"] == "The driver sent this: are you there?"
    assert queued["reply_key"] == "k1"


def test_a_chat_during_a_round_is_pushed_into_it_not_kept_waiting(posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    run_dir = d / "run"
    run_dir.mkdir()
    _pump_files(run_dir)
    rec["active_runs"] = {"r1": {"stage": "task", "run_dir": str(run_dir)}}
    chat.ask(cfg, reg, lambda *a: None, "task-demo", "why the table?", "k1", "/p.html")
    assert spawns == [] and not rec.get("chats")
    pushed = json.loads((run_dir / runs.INBOX).read_text().splitlines()[-1])
    assert "why the table?" in pushed["text"]
    assert pushed["reply_url"].endswith("/api/k1/agent-reply")


def test_a_stdin_fed_spawn_sends_the_prompt_as_the_first_message_and_takes_pushes():
    """A real spawn with a stand-in agent that reads stream-json on stdin the
    way claude does: it answers the prompt, then answers a pushed message, and
    the pump closes stdin after the turn so the agent exits on its own."""
    d = scratch()
    agent = d / "agent.py"
    agent.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)['message']['content']\n"
        "    print(json.dumps({'type':'user','isReplay':True,'message':{'role':'user','content':m}}), flush=True)\n"
        "    print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'heard: '+m[:40]}]}}), flush=True)\n"
        "    time.sleep(1.5)\n"
        "    print(json.dumps({'type':'result','result':'heard: '+m[:40]}), flush=True)\n")
    _Capture.got = []
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/api/k1"
    run_dir = d / "run"
    pid = runs.spawn("unused", "the round prompt", d, "m", None, "bypass", 60, run_dir,
                     argv=[sys.executable, str(agent), "--input-format", "stream-json", "{prompt}"],
                     thinking_url=f"{base}/agent-thinking")
    run = {"pid": pid, "run_dir": str(run_dir), "started": time.time()}
    time.sleep(0.7)
    assert runs.push(run, "quick question", f"{base}/agent-reply")
    for _ in range(100):
        if runs.finished(run):
            break
        time.sleep(0.1)
    time.sleep(0.5)
    server.shutdown()
    assert runs.finished(run), "the agent exits once the pump closes its stdin"
    ok, result, _, record = runs.outcome(run)
    assert ok and record["returncode"] == 0
    assert [l["text"] for l in chat.unanswered(run)] == ["quick question"], \
        "the stand-in never ran chat-reply, so the reaper would give it a chat turn"


def test_a_turn_opens_its_page_before_it_runs(monkeypatch):
    import pipeline
    d = scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    rec = tasks.record(reg, "task-demo")
    rec.update(cwd="/tmp", iteration=1, title="demo")
    opened = []
    monkeypatch.setattr(pipeline.surface_mod, "available", lambda: True)
    monkeypatch.setattr(pipeline.surface_mod, "open_session",
                        lambda c, path, kind, log, **m: opened.append((str(path), kind, m)))
    page = d / "surfaces" / "task-task-demo.html"
    pipeline._open_page_now(cfg, rec, "task-demo", "task", page)
    assert surface.is_placeholder(page) and "/surface-theme.css" in page.read_text()
    assert opened == [(str(page), "task", {"task": "task-demo", "cwd": "/tmp"})]
    page.write_text("<!doctype html><title>the real page</title>")
    assert not surface.is_placeholder(page)


# --------------------------------------------------------------------------- stdin always closes


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _pump(d, inbox, clock):
    import io
    out = d / "out.json"
    out.write_text("")
    stdin = io.StringIO()
    stdin.close = lambda: None
    return out, session_pump.Pump(str(out), str(d / "exit"), inbox=str(inbox), stdin=stdin,
                                  sender=lambda *a: True, clock=clock)


def _replay(text):
    return _event(type="user", isReplay=True, message={"role": "user", "content": text})


def test_two_identical_pushed_messages_are_each_waited_for():
    d, clock = scratch(), _Clock()
    inbox = _pump_files(d, ("again?", "http://x/api/k1/agent-reply"),
                        ("again?", "http://x/api/k1/agent-reply"))
    out, pump = _pump(d, inbox, clock)
    pump.feed()
    out.write_text(_replay("again?") + _event(type="result", result="one"))
    pump.relay()
    assert pump.open, "the second copy has not been read yet"
    with open(out, "a") as f:
        f.write(_replay("again?") + _event(type="result", result="two"))
    pump.relay()
    assert not pump.open


def test_stdin_closes_a_grace_period_after_the_turn_even_if_an_echo_never_matches():
    """With no run deadline, a session whose stdin never closes holds its
    slot forever. A replay that does not echo the pushed text exactly (or
    never comes) must not keep it open past the grace period."""
    d, clock = scratch(), _Clock()
    inbox = _pump_files(d, ("one more thing", "http://x/api/k1/agent-reply"))
    out, pump = _pump(d, inbox, clock)
    pump.feed()
    out.write_text(_replay("one more thing, reworded") + _event(type="result", result="done"))
    pump.relay()
    clock.now += session_pump.RESULT_GRACE_SECONDS - 1
    pump.settle()
    assert pump.open, "a moment after the turn the message may still be read"
    clock.now += 2
    pump.settle()
    assert not pump.open and Path(str(inbox) + ".closed").exists()


def test_a_new_turn_after_the_result_restarts_the_grace_period():
    d, clock = scratch(), _Clock()
    inbox = _pump_files(d, ("later", "http://x/api/k1/agent-reply"))
    out, pump = _pump(d, inbox, clock)
    pump.feed()
    out.write_text(_event(type="result", result="done"))
    pump.relay()
    clock.now += session_pump.RESULT_GRACE_SECONDS - 1
    with open(out, "a") as f:     # the session took the message as a turn of its own
        f.write(_event(type="assistant", message={"content": [{"type": "text", "text": "hm"}]}))
    pump.relay()
    clock.now += 5
    pump.settle()
    assert pump.open, "a turn is running: its stdin is not closed under it"


# --------------------------------------------------------------------------- reaping a chat turn


def _reap_chat_turn(monkeypatch, cfg, reg, rec, ok=True, result="Because."):
    import pipeline
    rec["active_runs"] = {"r-chat": {"kind": "chat", "stage": "chat", "pid": 1,
                                     "run_dir": "/nonexistent", "started": time.time(),
                                     "reply_key": "aaaaaaaaaaaaaaa1"}}
    monkeypatch.setattr(pipeline.runs_mod, "finished", lambda r: True)
    monkeypatch.setattr(pipeline.runs_mod, "outcome",
                        lambda r: (ok, result, {}, {"returncode": 0 if ok else 1}))
    pipeline._reap_tasks(cfg, reg)


def test_a_chat_turn_writes_the_log_record_every_turn_gets(posts, monkeypatch):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    monkeypatch.setattr(surface, "available", lambda: False)
    _reap_chat_turn(monkeypatch, cfg, reg, rec)
    assert json.loads((d / "logs" / "task-demo" / "r-chat.json").read_text()) == {
        "returncode": 0}


def test_a_thread_answered_during_a_chat_turn_is_delivered_when_it_is_reaped(
        monkeypatch, posts, spawns):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    rec["threads"] = {"k1": {"file": "/t.html", "title": "Which port?", "page": "/p.html"}}
    monkeypatch.setattr(surface, "available", lambda: True)
    monkeypatch.setattr(threads, "_get", lambda url: {"threads": []})
    monkeypatch.setattr(surface, "agent_status",
                        lambda k: {"status": "open", "pending_prompts": 1})
    monkeypatch.setattr(surface, "_run_cli", lambda args, timeout=25: poll({"prompt": "4390"}))
    _reap_chat_turn(monkeypatch, cfg, reg, rec)
    assert len(spawns) == 1 and "4390" in spawns[0]["prompt"]
    assert next(iter(rec["active_runs"].values()))["reply_key"] == "k1"


# --------------------------------------------------------------------------- answers the page server missed


def test_an_answer_the_page_server_missed_is_kept_and_sent_on_a_later_pass(monkeypatch):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    up, sent, logged = [False], [], []
    monkeypatch.setattr(chat, "_post", lambda url, body: up[0] and (sent.append(body) or True))
    monkeypatch.setattr(surface, "available", lambda: False)
    log = logged.append
    chat.finish(reg, log, "task-demo", {"reply_key": "k1"}, True, "The answer.")
    assert sent == [] and rec["replies"][0]["text"] == "The answer."
    assert chat.send_kept(reg, log, "task-demo") == 0, "still down"
    assert sum("kept" in m for m in logged) == 1, "a missed reply is logged once"
    up[0] = True
    assert chat.send_kept(reg, log, "task-demo") == 1
    assert sent == [{"text": "The answer."}] and rec["replies"] == []


def test_a_chat_that_is_gone_holds_up_only_its_own_answers(monkeypatch):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    sent = []
    monkeypatch.setattr(chat, "_post", lambda url, body: "/dead/" not in url and (sent.append(body) or True))
    rec["replies"] = [{"key": "dead", "text": "first", "at": time.time()},
                      {"key": "dead", "text": "second", "at": time.time()},
                      {"key": "k1", "text": "for a live chat", "at": time.time()}]
    assert chat.send_kept(reg, lambda m: None, "task-demo") == 1
    assert sent == [{"text": "for a live chat"}]
    assert [r["text"] for r in rec["replies"]] == ["first", "second"], "kept, in order"


def test_a_kept_answer_is_given_up_after_a_while(monkeypatch):
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    logged = []
    monkeypatch.setattr(chat, "_post", lambda url, body: False)
    rec["replies"] = [{"key": "k1", "text": "stale", "at": time.time() - chat.KEEP_SECONDS - 1},
                      {"key": "k1", "text": "fresh", "at": time.time()}]
    chat.send_kept(reg, logged.append, "task-demo")
    assert [r["text"] for r in rec["replies"]] == ["fresh"]
    assert any("gave up" in m for m in logged)


def test_a_reap_pass_tries_each_kept_answer_once(monkeypatch, posts):
    import pipeline
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    for other in ("task-b", "task-c"):
        tasks.record(reg, other).update({"id": other})
    tries = []
    monkeypatch.setattr(chat, "_post", lambda url, body: tries.append(url) and False)
    rec["replies"] = [{"key": "k1", "text": "waiting", "at": time.time()}]
    pipeline._reap_tasks(cfg, reg)
    assert len(tries) == 1


def test_the_reap_pass_retries_a_kept_answer(monkeypatch, posts):
    import pipeline
    d, work = scratch(), scratch()
    cfg, reg, rec = task_with_session(d, work)
    rec["replies"] = [{"key": "k1", "text": "late but here", "id": "m1"}]
    pipeline._reap_tasks(cfg, reg)
    assert posts == [(f"{surface.upstream()}/api/k1/agent-reply", {"text": "late but here"})]
    assert rec["replies"] == []


def test_the_apology_for_a_chat_that_cannot_start_is_kept_when_it_cannot_post(monkeypatch):
    d = scratch()
    cfg, reg = FakeCfg(d), FakeReg()
    rec = tasks.record(reg, "task-demo")
    rec["chats"] = [{"text": "hi", "reply_key": "k1", "page": "/p.html", "id": "m1"}]
    monkeypatch.setattr(chat, "_post", lambda url, body: False)
    assert not chat.start_next(cfg, reg, lambda *a: None, "task-demo")
    assert rec["chats"] == [] and rec["replies"][0]["key"] == "k1"
    assert rec["replies"][0]["id"] == "m1"
