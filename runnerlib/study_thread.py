"""Threads: a side chat beside a page, with an agent that stays awake.

The driver selects a passage on a dialogue page and presses Thread. The agent
talks about whatever they bring, teaches when they ask to learn, and carries a
note to the page when they settle something (`promote`), all inside the same
chat (prompts/thread.md). `mode` is only how it opens: `learn` starts out
teaching (`pipeline.py study-thread start`, or a note typed `learn:`). A Cadre
study *session* answers that in rounds of a minute or more, each one a cold
start; the driver wanted "an agent that then polled back to the study session
so it stayed awake and ready" (2026-10-23), without losing the review page.

So a thread is three things:

- **its own Review Surface page** (`page.html` in the thread's directory) —
  the chat lives in the page's Conversation panel, and the agent rewrites the
  page itself when a reply needs a diagram, a table or a quiz;
- **one Claude Code process kept running**, forked from the session that
  wrote the page the driver was reading (`--resume <that session>
  --fork-session`), so it already knows every reason behind that page. It may
  read the repo and write only its own page;
- **an engine loop** (`run`) that holds a real poll open on the page — the
  page shows the agent as listening — feeds each message to the process and
  posts the answer back as an agent reply, then polls again.

The daemon never polls a thread's page (poll delivery consumes, and would
steal the driver's message from the engine). It only wakes a thread whose
engine has gone — idle timeout, reboot — when a message is waiting: the new
engine resumes the thread's own forked session, so nothing is lost.

A thread outlives the dialogue it came from: approving that dialogue does not
end its threads. Only the driver ending the thread's page does.
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from string import Template

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "thread.md"
KIND = "study"                     # the session kind the daemon leaves alone
# How the agent opens: `talk` (the Thread button) discusses whatever the driver
# brings and offers options when they have not said; `learn` starts out
# teaching. Records written before 8 Nov 2026 say `study`, which is `learn`.
MODES = ("talk", "learn")
OPENING = {
    "talk": ("The driver selected a passage and started a thread on it: they want "
             "to talk about that passage, or the page, or whatever they bring up. "
             "Follow their lead. When they ask to learn or understand something, "
             "teach (see Teaching, below)."),
    "learn": ("The driver asked to learn about this: something on the page they do "
              "not know well enough yet. Teach from the first message (see "
              "Teaching, below)."),
}


def _mode(mode: str) -> str:
    return "learn" if mode == "study" else mode
# What the card sends when they typed nothing (REVIEW_SURFACE_ANNOTATION_ACTIONS
# `fallback`): the passage is the subject, so it names the thread instead.
FALLBACK_TOPIC = "this passage"
DEFAULT_MODEL = "opus"
# An engine with nothing to do for this long exits; the next message wakes it.
IDLE_LIMIT_S = 2 * 3600
POLL_CHUNK_MS = 10 * 60 * 1000     # one long poll; re-armed until the idle limit
ENDED = re.compile(r"session (has )?ended|session is over|was ended", re.IGNORECASE)
RETRY_LATER = "That answer failed. Please send your message again."
# `/model opus` typed in the thread switches who answers from then on; the
# engine handles it, the agent never sees it. An alias or a full model id.
MODEL_CMD = re.compile(r"\s*/model\s+([A-Za-z0-9][A-Za-z0-9.\-\[\]]{1,60})\s*")


def root(cfg) -> Path:
    return Path(cfg.data_dir) / "study-threads"


def thread_dir(cfg, thread_id: str) -> Path:
    if not re.fullmatch(r"thread-[0-9a-z-]{6,80}", thread_id or ""):
        raise ValueError(f"not a thread id: {thread_id!r}")
    return root(cfg) / thread_id


def load(cfg, thread_id: str) -> dict:
    return json.loads((thread_dir(cfg, thread_id) / "thread.json").read_text())


def save(cfg, rec: dict) -> None:
    d = thread_dir(cfg, rec["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "thread.json.tmp"
    tmp.write_text(json.dumps(rec, indent=2))
    tmp.replace(d / "thread.json")


def listing(cfg) -> list[dict]:
    out = []
    for f in sorted(root(cfg).glob("thread-*/thread.json")):
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


# --------------------------------------------------------------------------- start


def start(cfg, reg, topic: str, from_task: str, from_page: str, passage: str = "",
          cwd: str = "", log=print, spawn=None, model: str = "", mode: str = "learn") -> dict:
    """Open a thread on `topic` beside `from_task`'s page and start its engine.

    The thread forks the task's own Claude session; a task with none yet (it
    never ran) gets a fresh session told about the page instead. Raises
    ValueError when there is nothing to study in. `model` overrides
    `[learning] thread_model`; `mode` is how the agent opens (MODES)."""
    from . import surface, tasks as tasks_mod
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("name what you want to understand")
    mode = _mode(mode)
    if mode not in MODES:
        raise ValueError(f"not a thread mode: {mode!r}")
    task = tasks_mod.records(reg).get(from_task) or {}
    cwd = str(task.get("cwd") or cwd or "")
    if not cwd.startswith("/"):
        raise ValueError(f"task {from_task!r} has no working directory to study in")
    thread_id = f"thread-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    d = thread_dir(cfg, thread_id)
    d.mkdir(parents=True, exist_ok=True)
    page = d / "page.html"
    title = thread_title(mode, topic, passage)
    page.write_text(_first_page(title, mode, passage))
    rec = {"id": thread_id, "topic": topic, "passage": passage, "task": from_task,
           "mode": mode, "title": title,
           "from_page": from_page, "cwd": cwd, "page": str(page),
           "fork_from": task.get("session_id") or "", "session_id": "",
           "model": model or (getattr(cfg, "learning", {}) or {}).get("thread_model")
                    or DEFAULT_MODEL,
           "created": time.time(), "status": "open", "pid": 0}
    save(cfg, rec)
    surface.open_session(cfg, page, KIND, log, task=from_task, thread=thread_id,
                         cwd=cwd, title=title[:80],
                         role="learning thread" if mode == "learn" else "thread")
    _link_child(from_page, page, log)
    rec["pid"] = (spawn or launch)(cfg, thread_id)
    save(cfg, rec)
    log(f"{mode} thread {thread_id} on {topic[:60]!r} beside {from_task}")
    return rec


def thread_title(mode: str, topic: str, passage: str) -> str:
    """`Learn: …` or `Thread: …`, named by what they wrote — or, when they wrote
    nothing (the card's fallback), by the passage itself."""
    subject = topic
    if topic.strip().lower() == FALLBACK_TOPIC and passage.strip():
        subject = " ".join(passage.split())
        subject = subject if len(subject) <= 60 else subject[:59].rstrip() + "…"
    return f"{'Learn' if _mode(mode) == 'learn' else 'Thread'}: {subject}"


def promote(cfg, thread_id: str, text: str) -> dict:
    """Carry a note the driver settled in this thread to its page's next round
    (tasks.carry_note), and say so in that page's conversation, where they will
    see it before they send. Raises ValueError on an empty note."""
    from . import surface, tasks as tasks_mod
    rec = load(cfg, thread_id)
    name = (rec.get("title") or rec.get("topic") or "").split(": ", 1)[-1]
    note = tasks_mod.carry_note(cfg, rec["task"], text, anchor=rec.get("passage") or "",
                                source=name)
    key = (surface.sessions(cfg).get(rec.get("from_page") or "") or {}).get("key") or ""
    if key:
        _post(f"{surface.upstream()}/api/{key}/agent-reply",
              f"Queued for this page's next round, from the thread “{name[:80]}”: "
              f"{note['text']}")
    return {**note, "task": rec["task"]}


def _post(url: str, text: str) -> bool:
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def _first_page(title: str, mode: str, passage: str) -> str:
    from html import escape
    quote = (f"<blockquote><p>{escape(passage[:1200])}</p></blockquote>"
             if passage.strip() else "")
    study = ("" if _mode(mode) == "learn" else
             "<p>Ask it to teach you something here at any point. When you settle "
             "on something the page should change, ask it to send a note to the "
             "page: it rides that page's next round.</p>")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<link rel="stylesheet" href="/surface-theme.css">'
            f"<title>{escape(title[:80])}</title></head><body><main class=\"wrap\">"
            f"<h1>{escape(title)}</h1>{quote}"
            "<p>The answer arrives in the Conversation panel. Reply there; select "
            "anything on this page to ask about it. The agent rewrites this page when "
            f"an answer needs more than chat.</p>{study}<p>Send <code>/model sonnet</code> for "
            "a cheaper model, and <code>/model opus</code> to go "
            "back. The thread keeps its history either way.</p></main></body></html>")


def _link_child(parent: str, child: Path, log) -> None:
    """File the thread under the page it came from: `child-of` so the fleet
    nests it there, `thread-of` so that page's bell lists it and opens it in
    its chat window."""
    from . import surface
    if not parent:
        return
    for kind in ("child-of", "thread-of"):
        try:
            surface._run_cli(["link", kind, str(child), str(parent)])
        except Exception as e:
            log(f"study thread: {kind} link to {Path(parent).name} failed: {e}")


def launch(cfg, thread_id: str) -> int:
    """Start the engine detached, so it outlives the daemon tick that started it."""
    from .engine_seam import pipeline_command
    d = thread_dir(cfg, thread_id)
    argv = shlex.split(pipeline_command(cfg)) + ["study-thread", "run", thread_id]
    with open(d / "engine.log", "a") as out:
        # Unbuffered, so the log shows what the engine is doing while it runs,
        # not only once it exits.
        p = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, "PYTHONUNBUFFERED": "1"})
    return p.pid


def alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


# --------------------------------------------------------------------------- the engine


def claude_argv(cfg, rec: dict, claude_bin: str = "claude") -> list[str]:
    """The kept-alive process. Resumes the thread's own session once it has one;
    the first start forks the page agent's session instead."""
    from .engine_seam import pipeline_command
    page = rec["page"]
    # Thinking as readable text (`--thinking-display summarized`, the flag the
    # dialogue nodes carry, runnerlib/seed_nodes.py), streamed as it is written
    # (`--include-partial-messages`): without both, the window sat silent.
    effort = (getattr(cfg, "learning", {}) or {}).get("thread_effort") or "medium"
    argv = [claude_bin, "-p", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--model", rec["model"],
            "--effort", effort, "--thinking-display", "summarized",
            "--include-partial-messages"]
    if rec.get("session_id"):
        argv += ["--resume", rec["session_id"]]
    elif rec.get("fork_from"):
        argv += ["--resume", rec["fork_from"], "--fork-session"]
    # Read anything; write only the thread's own page; run only the cards CLI,
    # a study session beside the page when they ask for one, and the promote
    # that carries a note to THIS thread's page.
    argv += ["--allowedTools", "Read", "Grep", "Glob", f"Edit(/{page})", f"Write(/{page})",
             f"Bash({pipeline_command(cfg)} cards propose:*)",
             f"Bash({pipeline_command(cfg)} study:*)",
             f"Bash({pipeline_command(cfg)} study-thread promote {rec['id']}:*)",
             "--append-system-prompt", system_prompt(cfg, rec)]
    return argv


def system_prompt(cfg, rec: dict) -> str:
    from .engine_seam import pipeline_command
    passage = rec.get("passage") or "(none — they asked about the page as a whole)"
    return Template(PROMPT.read_text()).safe_substitute(
        cwd=rec["cwd"], page=rec["page"], from_page=rec.get("from_page") or "(unknown)",
        topic=rec["topic"], passage="> " + passage.replace("\n", "\n> "),
        opening=OPENING.get(_mode(rec.get("mode") or "learn"), OPENING["learn"]),
        task=rec.get("task") or "", thread_id=rec["id"], pipeline=pipeline_command(cfg))


SENTENCE_END = re.compile(r"(?<=[.!?:])\s+")


class ThinkingStream:
    """Thinking arrives in pieces of a few words (`thinking_delta`). Each piece
    posted alone would be a line of its own in the chat, so they are joined
    and sent a sentence at a time, the rest when the block ends."""

    def __init__(self):
        self.buffer, self.seen = "", False

    def feed(self, event: dict) -> list[str]:
        if event.get("type") != "stream_event":
            return []
        inner = event.get("event") or {}
        delta = inner.get("delta") or {}
        if delta.get("type") == "thinking_delta" and delta.get("thinking"):
            self.seen = True
            self.buffer += delta["thinking"]
            *done, self.buffer = SENTENCE_END.split(self.buffer)
            return [s.strip() for s in done if s.strip()]
        if inner.get("type") == "content_block_stop" and self.buffer.strip():
            rest, self.buffer = self.buffer.strip(), ""
            return [rest]
        return []


def progress_lines(event: dict, thinking: bool = True) -> list[str]:
    """What the agent is doing, from one stream-json event, for the chat window
    to show while the driver waits (dimmed, as thinking): its thinking, and a
    short line per tool it reaches for. `thinking=False` when the thinking was
    already streamed piece by piece (ThinkingStream)."""
    if event.get("type") != "assistant":
        return []
    out = []
    for b in (event.get("message") or {}).get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "thinking" and str(b.get("thinking") or "").strip():
            if not thinking:
                continue
            out.append(str(b["thinking"]).strip()[:4000])
        elif b.get("type") == "tool_use":
            name, args = b.get("name") or "", b.get("input") or {}
            if name == "Read":
                out.append(f"Reading {Path(str(args.get('file_path') or '')).name}…")
            elif name in ("Grep", "Glob"):
                out.append(f"Searching for {str(args.get('pattern') or '')[:80]}…")
            elif name in ("Edit", "Write"):
                out.append("Writing this thread's page…")
            elif name:
                out.append(f"Using {name}…")
    return out


class Agent:
    """One Claude Code process held open across turns (stream-json in and out).
    `progress`, when set, gets each line of what it is doing while it answers."""

    def __init__(self, argv: list[str], cwd: str):
        self.proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self.session_id = ""
        self.progress = None

    def ask(self, text: str) -> str:
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        stream = ThinkingStream()
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("session_id"):
                self.session_id = ev["session_id"]
            if self.progress:
                for line in stream.feed(ev) + progress_lines(ev, thinking=not stream.seen):
                    self.progress(line)
            if ev.get("type") == "result":
                if ev.get("is_error"):
                    raise RuntimeError("the agent answered with an error: "
                                       + str(ev.get("result") or ev.get("subtype") or "")[:200])
                return str(ev.get("result") or "").strip() or "(no answer)"
        raise RuntimeError("the agent process exited mid-answer")

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()


def model_switch(prompts: list[dict]) -> tuple[str, list[dict]]:
    """Peel a `/model <name>` message off the batch: (the model or "", the rest)."""
    model, rest = "", []
    for p in prompts:
        m = MODEL_CMD.fullmatch(str(p.get("prompt") or ""))
        if m:
            model = m.group(1)
        else:
            rest.append(p)
    return model, rest


def message_text(prompts: list[dict]) -> str:
    """The driver's batch as one chat message, each with what it was attached to."""
    from . import surface
    parts = []
    for p in prompts:
        text = surface.with_images(str(p.get("prompt") or ""), p)
        if not text:
            continue
        anchor = str(p.get("text") or "").strip()
        if anchor and p.get("tag") not in ("message",):
            parts.append("> " + anchor[:400].replace("\n", "\n> ") + "\n\n" + text)
        else:
            parts.append(text)
    return "\n\n---\n\n".join(parts)


def run(cfg, thread_id: str, cli=None, agent_factory=None, clock=time.time,
        log=print) -> str:
    """The engine: answer, listen, answer again — until the driver ends the
    page or nothing arrives for IDLE_LIMIT_S. Returns why it stopped."""
    from . import surface
    cli = cli or surface._run_cli
    rec = load(cfg, thread_id)
    rec.update(pid=os.getpid(), status="open")
    save(cfg, rec)
    first_start = not rec.get("session_id")
    factory = agent_factory or (lambda argv, cwd: Agent(argv, cwd))
    claude_bin = (getattr(cfg, "claude", {}) or {}).get("bin") or "claude"
    # While it answers, the chat window shows what it is doing: the driver
    # waited on a silent window before (8 Nov 2026: "Does it show thinking? Or
    # some kind of you're waiting indicator?").
    key = (surface.sessions(cfg).get(rec["page"]) or {}).get("key") or ""
    relay = ((lambda line: _post(f"{surface.upstream()}/api/{key}/agent-thinking", line))
             if key else None)

    def make():
        a = factory(claude_argv(cfg, rec, claude_bin), rec["cwd"])
        a.progress = relay
        return a

    agent = make()

    def restart():
        nonlocal agent
        if agent.session_id:
            rec["session_id"] = agent.session_id
        agent.close()
        save(cfg, rec)
        agent = make()

    def answer(text):
        # The poll has consumed the message, so it always gets a reply. A model
        # name the CLI refuses only shows when the new process is asked
        # something; fall back to the model that last answered (kept on the
        # record, so a later engine run can fall back too). Any other failure
        # gets one restart on the thread's own session.
        note = ""
        for retry in (True, False):
            try:
                out = agent.ask(text)
            except (RuntimeError, OSError) as e:
                log(f"study thread {thread_id}: answer failed on {rec['model']}: {e}")
                good = rec.get("good_model") or ""
                if good and good != rec["model"]:
                    bad, rec["model"] = rec["model"], good
                    note = f"{bad} would not start, so this is {good} again.\n\n"
                    log(f"study thread {thread_id}: {bad} failed, back on {good}")
                elif not retry:
                    break
                try:
                    restart()
                except OSError as e:
                    log(f"study thread {thread_id}: restart failed: {e}")
                    break
                continue
            rec["good_model"] = rec["model"]
            save(cfg, rec)
            return note + out
        save(cfg, rec)
        return note + RETRY_LATER

    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    reply = ""
    try:
        if first_start:
            # Loading the page's session takes a few seconds before any
            # thinking arrives; say so at once rather than show a silent window.
            if relay:
                relay("Opening a copy of the page's own session to answer from…")
            # The brief rides the first message, not only the system prompt: a
            # resumed (forked) session keeps the system prompt it was born with,
            # so an appended one never reached it (seen 2026-10-23: the fork
            # guessed a page path from the docs and its write was refused).
            reply = answer(system_prompt(cfg, rec) + "\n\n---\n\nTheir first message:\n\n"
                           + message_text([{"prompt": rec["topic"],
                                            "text": rec.get("passage") or ""}]))
            if agent.session_id:
                rec["session_id"] = agent.session_id
            save(cfg, rec)
        idle_since = clock()
        while True:
            args = ["poll", rec["page"], "--json", "--timeout-ms", str(POLL_CHUNK_MS)]
            if reply:
                args += ["--agent-reply", reply]
            raw = cli(args, timeout=POLL_CHUNK_MS // 1000 + 60)
            reply = ""
            payload = _payload(raw)
            model, prompts = model_switch(payload.get("prompts") or [])
            ended = payload.get("status") == "ended" or _says_ended(raw)
            if model and not ended:
                # A process keeps the model it started with, so switching is
                # a restart that resumes this thread's own session.
                if not rec.get("good_model") and rec.get("session_id"):
                    rec["good_model"] = rec["model"]
                rec["model"] = model
                restart()
                reply = f"Now answering with {model}. The thread keeps its history."
                idle_since = clock()
                log(f"study thread {thread_id}: model is now {model}")
            text = message_text(prompts)
            if text and not ended:
                reply = answer(text)
                idle_since = clock()
            if ended:
                try:
                    last = agent.ask("THREAD ENDED"
                                     + (f"\n\nTheir last words:\n{text}" if text else ""))
                except (RuntimeError, OSError) as e:
                    last = f"closing pass failed: {e}"
                # The page is closed, so the closing line (which cards it
                # proposed) has nowhere to go but the engine log.
                log(f"study thread {thread_id}: {last.strip()[:300]}")
                rec["status"] = "ended"
                return "ended"
            if not text and clock() - idle_since >= IDLE_LIMIT_S:
                rec["status"] = "asleep"
                return "idle"
            if rec.get("session_id") != agent.session_id and agent.session_id:
                rec["session_id"] = agent.session_id
                save(cfg, rec)
    finally:
        agent.close()
        rec["pid"] = 0
        if agent.session_id:
            rec["session_id"] = agent.session_id
        save(cfg, rec)
        log(f"study thread {thread_id}: engine stopped ({rec['status']})")


def _says_ended(raw: str) -> bool:
    """The CLI's own session-over lines, never the JSON payload: that carries
    the driver's words, which may well talk about a session that ended."""
    from . import surface
    return any(ENDED.search(l) or re.search(surface.SESSION_OVER, l, re.IGNORECASE)
               for l in (raw or "").splitlines() if not l.lstrip().startswith("{"))


def _payload(raw: str) -> dict:
    line = next((l for l in (raw or "").splitlines() if l.startswith("{")), "")
    try:
        return json.loads(line) if line else {}
    except json.JSONDecodeError:
        return {}


# --------------------------------------------------------------------------- waking


def wake(cfg, log, sessions: dict, status=None, spawn=None) -> list[str]:
    """Daemon side: restart the engine of any open thread that has a message
    waiting and no engine. Read-only status — never consumes the message."""
    from . import surface
    status = status or surface.agent_status
    woken = []
    for path, meta in sessions.items():
        if meta.get("kind") != KIND or not meta.get("open") or not meta.get("thread"):
            continue
        try:
            rec = load(cfg, meta["thread"])
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if rec.get("status") == "ended" or alive(rec.get("pid") or 0):
            continue
        st = status(meta.get("key") or "") if meta.get("key") else None
        if not st or st.get("status") == "ended" or not st.get("pending_prompts"):
            continue
        rec["pid"] = (spawn or launch)(cfg, rec["id"])
        rec["status"] = "open"
        save(cfg, rec)
        woken.append(rec["id"])
        log(f"study thread {rec['id']}: woke for a waiting message")
    return woken
