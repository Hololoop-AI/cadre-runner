"""Study threads: a side chat beside a page, with an agent that stays awake.

The driver selects a passage on a dialogue page and presses Study. A Cadre
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

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "study-thread.md"
KIND = "study"                     # the session kind the daemon leaves alone
DEFAULT_MODEL = "sonnet"
# An engine with nothing to do for this long exits; the next message wakes it.
IDLE_LIMIT_S = 2 * 3600
POLL_CHUNK_MS = 10 * 60 * 1000     # one long poll; re-armed until the idle limit
ENDED = re.compile(r"session (has )?ended|session is over|was ended", re.IGNORECASE)
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
          cwd: str = "", log=print, spawn=None, model: str = "") -> dict:
    """Open a thread on `topic` beside `from_task`'s page and start its engine.

    The thread forks the task's own Claude session; a task with none yet (it
    never ran) gets a fresh session told about the page instead. Raises
    ValueError when there is nothing to study in. `model` overrides
    `[learning] thread_model`."""
    from . import surface, tasks as tasks_mod
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("name what you want to understand")
    task = tasks_mod.records(reg).get(from_task) or {}
    cwd = str(task.get("cwd") or cwd or "")
    if not cwd.startswith("/"):
        raise ValueError(f"task {from_task!r} has no working directory to study in")
    thread_id = f"thread-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    d = thread_dir(cfg, thread_id)
    d.mkdir(parents=True, exist_ok=True)
    page = d / "page.html"
    page.write_text(_first_page(topic, passage))
    rec = {"id": thread_id, "topic": topic, "passage": passage, "task": from_task,
           "from_page": from_page, "cwd": cwd, "page": str(page),
           "fork_from": task.get("session_id") or "", "session_id": "",
           "model": model or (getattr(cfg, "learning", {}) or {}).get("thread_model")
                    or DEFAULT_MODEL,
           "created": time.time(), "status": "open", "pid": 0}
    save(cfg, rec)
    surface.open_session(cfg, page, KIND, log, task=from_task, thread=thread_id,
                         cwd=cwd, title=f"Study: {topic}"[:80], role="study thread")
    _link_child(from_page, page, log)
    rec["pid"] = (spawn or launch)(cfg, thread_id)
    save(cfg, rec)
    log(f"study thread {thread_id} on {topic[:60]!r} beside {from_task}")
    return rec


def _first_page(topic: str, passage: str) -> str:
    from html import escape
    quote = (f"<blockquote><p>{escape(passage[:1200])}</p></blockquote>"
             if passage.strip() else "")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<link rel="stylesheet" href="/surface-theme.css">'
            f"<title>Study: {escape(topic[:80])}</title></head><body><main class=\"wrap\">"
            f"<h1>Study: {escape(topic)}</h1>{quote}"
            "<p>The answer arrives in the Conversation panel. Reply there; select "
            "anything on this page to ask about it. The agent rewrites this page when "
            "an answer needs more than chat.</p><p>Send <code>/model opus</code> for "
            "deeper answers (it costs more), and <code>/model sonnet</code> to go "
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
    argv = [claude_bin, "-p", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--model", rec["model"]]
    if rec.get("session_id"):
        argv += ["--resume", rec["session_id"]]
    elif rec.get("fork_from"):
        argv += ["--resume", rec["fork_from"], "--fork-session"]
    # Read anything; write only the thread's own page; run only the cards CLI.
    argv += ["--allowedTools", "Read", "Grep", "Glob", f"Edit(/{page})", f"Write(/{page})",
             f"Bash({pipeline_command(cfg)} cards propose:*)",
             "--append-system-prompt", system_prompt(cfg, rec)]
    return argv


def system_prompt(cfg, rec: dict) -> str:
    from .engine_seam import pipeline_command
    passage = rec.get("passage") or "(none — they asked about the page as a whole)"
    return Template(PROMPT.read_text()).safe_substitute(
        cwd=rec["cwd"], page=rec["page"], from_page=rec.get("from_page") or "(unknown)",
        topic=rec["topic"], passage="> " + passage.replace("\n", "\n> "),
        pipeline=pipeline_command(cfg))


class Agent:
    """One Claude Code process held open across turns (stream-json in and out)."""

    def __init__(self, argv: list[str], cwd: str):
        self.proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self.session_id = ""

    def ask(self, text: str) -> str:
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("session_id"):
                self.session_id = ev["session_id"]
            if ev.get("type") == "result":
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
    parts = []
    for p in prompts:
        text = str(p.get("prompt") or "").strip()
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
    agent = factory(claude_argv(cfg, rec, claude_bin), rec["cwd"])
    fallback = ""        # the model before an untried `/model` switch

    def restart():
        nonlocal agent
        if agent.session_id:
            rec["session_id"] = agent.session_id
        agent.close()
        save(cfg, rec)
        agent = factory(claude_argv(cfg, rec, claude_bin), rec["cwd"])

    def answer(text):
        # A model name the CLI refuses only shows when the new process is
        # asked something; fall back to the model that worked.
        nonlocal fallback
        try:
            out = agent.ask(text)
        except RuntimeError:
            if not fallback:
                raise
            bad, rec["model"], fallback = rec["model"], fallback, ""
            restart()
            log(f"study thread {thread_id}: {bad} failed, back on {rec['model']}")
            return f"{bad} would not start, so this is {rec['model']} again.\n\n" + agent.ask(text)
        fallback = ""
        return out

    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    reply = ""
    try:
        if first_start:
            # The brief rides the first message, not only the system prompt: a
            # resumed (forked) session keeps the system prompt it was born with,
            # so an appended one never reached it (seen 2026-10-23: the fork
            # guessed a page path from the docs and its write was refused).
            reply = agent.ask(system_prompt(cfg, rec) + "\n\n---\n\nTheir first message:\n\n"
                              + message_text([{"prompt": rec["topic"],
                                               "text": rec.get("passage") or ""}]))
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
            ended = payload.get("status") == "ended" or bool(ENDED.search(raw))
            if model and not ended:
                # A process keeps the model it started with, so switching is
                # a restart that resumes this thread's own session.
                fallback = fallback or rec["model"]
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
                last = agent.ask("THREAD ENDED" + (f"\n\nTheir last words:\n{text}" if text else ""))
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
