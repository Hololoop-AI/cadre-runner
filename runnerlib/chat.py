"""Direct chat: the driver talks with a page's own session, and no page round runs.

The page server's "This page" chat window sends each Enter as one prompt
marked `channel: "chat"`. A poll that returns ONLY such prompts is a
conversation, not a review: the runner resumes the page's own session (the
main thread, the same session id its rounds use) with the message, posts the
session's answer into the chat with `POST /api/<page key>/agent-reply`, and
leaves the page and its round count alone. Any batch with other prompts in it
is a page round as before; that is the page server's Send page.

The same turn answers the other way an agent talks to the driver without a
page: a message thread. A running agent asks something quick with
`review-surface message <its page> "question"` and keeps working, reading the
answer with `review-surface poll <thread file>` when it needs it. If the turn
ends before the answer comes, nobody is polling that thread any more, so the
runner learns the page's threads when the turn is reaped (`watch_threads`)
and, once the driver answers, delivers the answer to the session as a chat
turn whose reply goes back into the thread.

One turn of a task at a time still holds. A chat that arrives while a round
(or another chat) is running waits on the task record (`chats`) and starts
when that turn is reaped; a page round that arrives during a chat waits the
way it always has (`engine_seam.run_task_spawn` defers it).

Everything here is the runner's half of the page server's message-threads
work; the page server owns the chat window, the thread files and the routes.
"""

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

from . import projects, runs, surface, tasks
from .engine_seam import pipeline_command
from .nodes import Nodes

CHANNEL = "chat"
ANSWERED = "answered.txt"   # in a run's directory: one reply key per chat-reply
STAGE = "chat"
REPLY_LIMIT = 20000

CHAT_PROMPT = """The driver is talking with you directly in the chat window of your page, \
{page}. This is a conversation, not a page round. Their message:

{text}

Answer them. Your final message is posted into the chat verbatim as your reply, \
so make it the answer itself, in plain words. Do not rewrite the page unless \
they ask you to: their full review of it arrives separately, when they press \
Send page. Do not commit, push or hand the work on unless they ask."""

PUSH_HEAD = "The driver sent this in your page's chat window while you work:\n\n"
PUSH_TAIL = "\n\n(Answer them right now"
PUSH_PROMPT = (PUSH_HEAD + "{text}" + PUSH_TAIL + ", before anything else, in a few "
               "sentences, with this command — they are waiting on it, and only "
               "what you send this way reaches the chat:\n\n"
               "    {pipeline} chat-reply {key} \"<your answer>\"\n\n"
               "Then carry on with what you were doing. Rewrite your page only if "
               "they ask.)")

THREAD_PROMPT = """The driver answered the question you asked them in the message \
thread "{title}" ({file}), after your last turn ended. Their answer:

{text}

Your final message is posted into that thread verbatim as your reply: \
acknowledge the answer and say in a line or two what it changes. Do not \
rewrite your page now; if the answer changes your work, it carries into your \
next page round. Do not commit, push or hand the work on unless they ask."""


# --------------------------------------------------------------------------- the page server

def is_direct_chat(payload: dict | None) -> bool:
    """Every prompt in the batch came from Enter in the page's chat window."""
    prompts = (payload or {}).get("prompts") or []
    return bool(prompts) and all(
        isinstance(p, dict) and p.get("channel") == CHANNEL for p in prompts)


def page_key(path) -> str:
    """The page server's session key for a file: sha256 of its canonical path,
    first 16 hex characters (review-surface `sessionKey`). Known before the
    page's session exists, which a first round's thinking needs."""
    return hashlib.sha256(os.path.realpath(str(path)).encode()).hexdigest()[:16]


def api(key: str, what: str) -> str:
    return f"{surface.upstream()}/api/{key}/{what}"


def _post(url: str, body: dict) -> bool:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def _get(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def post_reply(key: str, text: str) -> bool:
    """The session's answer, into the chat it was asked in. Direct HTTP rather
    than `poll --agent-reply`: that rides a poll, which would consume whatever
    the driver queued meanwhile, and caps the text at 1000 characters."""
    return _post(api(key, "agent-reply"), {"text": text[:REPLY_LIMIT]})


def conversations_dir() -> Path:
    """Where the page server writes thread placeholder files: `conversations/`
    in its state directory, resolved the way `surface.outbox_path` resolves it."""
    state = os.environ.get("REVIEW_SURFACE_STATE_DIR") or str(Path.home() / ".review-surface")
    return Path(state) / "conversations"


# --------------------------------------------------------------------------- the turn

def ask(cfg, reg, log, task_id: str, text: str, reply_key: str, page: str,
        thread: dict | None = None) -> None:
    """Give one chat message to the task's session. `reply_key` is the
    page-server session the answer goes to: the page itself, or a thread on it.

    A running turn gets it pushed in (`runs.push`): the session reads it at
    its next tool call, answers it inline and carries on, and the pump posts
    that answer to the chat. A free session gets a chat turn of its own. Only
    a running turn that cannot take it (it does not read stdin, or has
    stopped reading) makes it wait for the turn to end."""
    rec = tasks.record(reg, task_id)
    if not thread:
        for run in (rec.get("active_runs") or {}).values():
            if runs.push(run, PUSH_PROMPT.format(text=text, key=reply_key,
                                                 pipeline=pipeline_command(cfg)),
                         api(reply_key, "agent-reply")):
                log(f"{task_id}: chat pushed into the running "
                    f"{run.get('stage') or 'turn'}; its answer goes to {reply_key}")
                return
    rec.setdefault("chats", []).append({
        "text": text, "reply_key": reply_key, "page": str(page),
        "thread": thread or None, "at": time.time()})
    if hasattr(reg, "save"):
        reg.save()
    if rec.get("active_runs"):
        log(f"{task_id}: chat waits — the running turn cannot take it; it is "
            f"answered when that turn ends")
        return
    start_next(cfg, reg, log, task_id)


def answer(key: str, text: str) -> bool:
    """`pipeline.py chat-reply`: post a session's answer, and note it on the
    run it came from so the reaper knows the pushed message was answered."""
    sent = post_reply(key, text)
    out = os.environ.get("CADRE_RUN_OUT")
    if sent and out:
        with open(Path(out).parent / ANSWERED, "a") as f:
            f.write(key + "\n")
    return sent


def unanswered(run: dict) -> list[dict]:
    """Pushed chat lines of a run with no answer recorded: never read, or read
    and not answered with `chat-reply`. One answer covers one message, in order."""
    lines = [l for l in runs.inbox(run) if str(l.get("reply_url") or "").endswith("/agent-reply")]
    try:
        done = (Path(run["run_dir"]) / ANSWERED).read_text().split()
    except (OSError, KeyError):
        done = []
    left = []
    for line in lines:
        key = line["reply_url"].rsplit("/", 2)[-2]
        if key in done:
            done.remove(key)
        else:
            left.append(line)
    return left


def requeue_unread(cfg, reg, log, task_id: str, run: dict, page: str) -> int:
    """Pushed chat that got no answer (it arrived as the turn closed, the
    session died, or it read the message and never ran `chat-reply`): each
    becomes a chat turn of its own, so every message is answered."""
    rec = tasks.record(reg, task_id)
    n = 0
    for line in unanswered(run):
        url = str(line.get("reply_url") or "")
        if not url.endswith("/agent-reply"):
            continue
        key = url.rsplit("/", 2)[-2]
        text = str(line.get("text") or "")
        if text.startswith(PUSH_HEAD):
            text = text[len(PUSH_HEAD):].split(PUSH_TAIL)[0].strip()
        log(f"{task_id}: a chat message got no answer in the turn — it gets a chat turn")
        rec.setdefault("chats", []).append({
            "text": text, "reply_key": key, "page": str(page), "thread": None,
            "at": time.time()})
        n += 1
    if n:
        log(f"{task_id}: {n} chat message(s) arrived as the turn ended — "
            f"each gets a chat turn")
    return n


def start_next(cfg, reg, log, task_id: str) -> bool:
    """Start the oldest waiting chat as a turn of the task's own session.
    True when one started."""
    rec = tasks.record(reg, task_id)
    if rec.get("active_runs") or not rec.get("chats"):
        return False
    chat = rec["chats"].pop(0)
    session_id = rec.get("session_id")
    cwd = str(rec.get("cwd") or "")
    if not session_id or not cwd.startswith("/") or not Path(cwd).is_dir():
        post_reply(chat["reply_key"], "This page has no session to answer from "
                   "(it never ran, or its directory is gone). Send page starts a round.")
        log(f"{task_id}: chat dropped — no session or working directory to resume")
        if hasattr(reg, "save"):
            reg.save()
        return False
    try:
        return _spawn(cfg, reg, log, task_id, rec, chat, session_id, cwd)
    except Exception as e:
        # The message is already off the queue: say so where it was asked
        # rather than let it vanish into the daemon log.
        post_reply(chat["reply_key"], f"I could not start a turn to answer that ({e}).")
        log(f"{task_id}: chat turn could not start — {e}")
        return False


def _spawn(cfg, reg, log, task_id, rec, chat, session_id, cwd) -> bool:
    node_name = rec.get("node") or tasks.NODE
    nodes = Nodes(cfg.data_dir)
    node = nodes.active(node_name)
    argv = nodes.command_argv(node_name, {"payload": {"story": task_id, "task": task_id}},
                              node=node)
    thread = chat.get("thread")
    prompt = (THREAD_PROMPT.format(title=thread.get("title") or "a question",
                                   file=thread.get("file") or "", text=chat["text"])
              if thread else CHAT_PROMPT.format(page=chat["page"], text=chat["text"]))
    projs = projects.load(cfg.data_dir)
    pid = rec.get("project") if rec.get("project") in projs else None
    add_dirs = projects.read_dirs(projs, pid, cwd) if pid else []
    rid = f"{time.strftime('%Y%m%d-%H%M%S')}-{STAGE}"
    run_dir = Path(cfg.data_dir) / "runs" / task_id / rid
    proc = runs.spawn(cfg.claude["bin"], prompt, Path(cwd), node["model"], None,
                      cfg.claude["permission_mode"], cfg.claude["timeout_seconds"],
                      run_dir, session_id=session_id, resume=True, argv=argv,
                      **({"add_dirs": add_dirs} if add_dirs else {}),
                      thinking_url=api(chat["reply_key"], "agent-thinking"),
                      extra_env={"CADRE_STORY": task_id, "CADRE_TASK": task_id,
                                 "CADRE_STAGE": STAGE, "CADRE_SESSION_ID": session_id,
                                 "CADRE_RUN_ID": rid,
                                 "CADRE_SURFACE_OUT": chat["page"]})
    rec.setdefault("active_runs", {})[rid] = {
        "kind": STAGE, "stage": STAGE, "task": task_id, "slice": None, "pr": None,
        "pid": proc, "repo": "", "branch": "", "model": node["model"],
        "worktree": cwd, "run_dir": str(run_dir), "started": time.time(),
        "deadline": runs.deadline(cfg.claude["timeout_seconds"]),
        "session_id": session_id, "resumed": True,
        "iteration": rec.get("iteration"), "reply_key": chat["reply_key"],
        "thread": thread}
    if hasattr(reg, "save"):
        reg.save()
    log(f"{task_id}: chat turn spawned — pid {proc} (resumed session "
        f"{session_id[:8]}); the answer goes to {chat['reply_key']}")
    return True


def finish(log, task_id: str, run: dict, ok: bool, result: str) -> bool:
    """A chat turn ended: its answer goes into the chat it was asked in.
    Nothing else happens — no page opens and the round count stays."""
    text = (result or "").strip()
    if not ok or not text:
        why = "timed out" if run.get("timed_out_after") is not None else "failed"
        text = (f"I could not answer that: the session {why}"
                + (f" ({text[:300]})" if text else "") + ". Your message is in "
                "the session; ask again, or press Send page for a full round.")
    sent = post_reply(run["reply_key"], text)
    log(f"{task_id}: chat turn {'answered' if ok else 'FAILED'} — "
        f"{'posted to' if sent else 'COULD NOT POST to'} {run['reply_key']}: {text[:120]}")
    return sent


# --------------------------------------------------------------------------- threads

def watch_threads(cfg, reg, log, task_id: str, page) -> int:
    """After a turn ends: learn the message threads its agent started on its
    page, and deliver any answer the driver already gave that the agent did
    not read. Returns how many answers were delivered."""
    rec = tasks.record(reg, task_id)
    listed = (_get(api(page_key(page), "threads")) or {}).get("threads") or []
    here = {t.get("key"): t for t in listed if t.get("here") and t.get("key")}
    known = rec.setdefault("threads", {})
    if set(here) - set(known):
        cdir = conversations_dir()
        files = {page_key(f): str(f) for f in cdir.glob("*.html")} if cdir.is_dir() else {}
        for key in set(here) - set(known):
            if key in files:
                known[key] = {"file": files[key], "title": here[key].get("title") or "",
                              "page": str(page)}
    if hasattr(reg, "save"):
        reg.save()
    delivered = 0
    for key in list(known):
        st = surface.agent_status(key)
        if st and st.get("status") == "ended":
            known.pop(key)
            continue
        if st and int(st.get("pending_prompts") or 0):
            delivered += deliver_thread(cfg, reg, log, task_id, key)
    return delivered


def thread_owner(reg, key: str) -> str | None:
    """The task whose agent started thread `key`, if the runner knows it."""
    for task_id, rec in tasks.records(reg).items():
        if key in (rec.get("threads") or {}):
            return task_id
    return None


def deliver_thread(cfg, reg, log, task_id: str, key: str) -> int:
    """Read the driver's answer in a thread whose agent is not running, and
    give it to the session as a chat turn that replies into the thread. While
    a turn of the task runs the agent reads its threads itself, and polling
    here would take the answer from it, so this waits for the turn to end."""
    rec = tasks.record(reg, task_id)
    thread = (rec.get("threads") or {}).get(key)
    if not thread or rec.get("active_runs"):
        return 0
    try:
        raw = surface._run_cli(["poll", thread["file"], "--timeout-ms", "4000", "--json"],
                               timeout=30)
    except Exception as e:
        log(f"{task_id}: thread poll failed for {key}: {e}")
        return 0
    payload, _, _ = surface._parse_feedback(raw)
    if (payload or {}).get("status") == "ended":
        rec["threads"].pop(key, None)
    words = [str(p.get("prompt") or "").strip() if isinstance(p, dict) else str(p)
             for p in (payload or {}).get("prompts") or []]
    words = [w for w in words if w]
    if not words:
        return 0
    ask(cfg, reg, log, task_id, "\n\n".join(words), key, thread.get("page") or "",
        thread={"file": thread["file"], "title": thread.get("title") or ""})
    return 1
