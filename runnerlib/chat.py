"""Direct chat: the driver talks with a page's own session, and no page round runs.

The page server's "This page" chat window sends each Enter as one prompt
marked `channel: "chat"`. A poll that returns ONLY such prompts is a
conversation, not a review: the runner resumes the page's own session (the
main thread, the same session id its rounds use) with the message, posts the
session's answer into the chat with `POST /api/<page key>/agent-reply`, and
leaves the page and its round count alone. Any batch with other prompts in it
is a page round as before; that is the page server's Send page.

A chat that arrives while a turn of the task runs is pushed into that turn
when it reads stdin (`runs.push`), and the session answers it with
`pipeline.py chat-reply`. Otherwise it waits on the task record (`chats`)
and gets a chat turn of its own once the task is free, so one turn of a task
at a time still holds; a page round that arrives during a chat waits the way
it always has (`engine_seam.run_task_spawn` defers it).

Every message has an id. A pushed message names it in its reply command, the
answer records it in the run's answered file, and the reaper hands on as a
chat turn every pushed message whose id was not answered. An answer the page
server does not take is kept on the task record (`replies`) and sent again
on later passes (`send_kept`), so neither a message nor its answer is lost.

Message threads, the other way an agent and the driver talk without a page,
are runnerlib/threads.py; their answers come back through `ask` as chat turns
that reply into the thread. The page server owns the chat window, the thread
files and the routes.
"""

import json
import os
import time
import urllib.request
import uuid
from pathlib import Path

from . import projects, runs, surface, tasks
from .engine_seam import pipeline_command
from .nodes import Nodes

CHANNEL = "chat"
# In a run's directory: one line per chat-reply, "<reply key> <message id>".
# A line with no id was written before messages had ids.
ANSWERED = "answered.txt"
STAGE = "chat"
REPLY_LIMIT = 20000

CHAT_PROMPT = """The driver is talking with you directly in the chat window of your page, \
{page}. This is a conversation, not a page round. Their message:

{text}

Answer them. Your final message is posted into the chat verbatim as your reply, \
so make it the answer itself, in plain words. Do not rewrite the page unless \
they ask you to: their full review of it arrives separately, when they press \
Send page. Take no step with effects outside this conversation, such as \
committing, sending or handing the work on, unless they ask."""

PUSH_PROMPT = ("The driver sent this in your page's chat window while you work:\n\n"
               "{text}\n\n(Answer them right now, before anything else, in a few "
               "sentences, with this command — they are waiting on it, and only "
               "what you send this way reaches the chat:\n\n"
               "    {pipeline} chat-reply {key} --id {id} \"<your answer>\"\n\n"
               "Then carry on with what you were doing. Rewrite your page only if "
               "they ask.)")

THREAD_PROMPT = """The driver answered the question you asked them in the message \
thread "{title}" ({file}), after your last turn ended. Their answer:

{text}

Your final message is posted into that thread verbatim as your reply: \
acknowledge the answer and say in a line or two what it changes. Do not \
rewrite your page now; if the answer changes your work, it carries into your \
next page round. Take no step with effects outside this conversation, such as \
committing, sending or handing the work on, unless they ask."""


# --------------------------------------------------------------------------- the page server

def is_direct_chat(payload: dict | None) -> bool:
    """Every prompt in the batch came from Enter in the page's chat window."""
    prompts = (payload or {}).get("prompts") or []
    return bool(prompts) and all(
        isinstance(p, dict) and p.get("channel") == CHANNEL for p in prompts)


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


def post_reply(key: str, text: str) -> bool:
    """The session's answer, into the chat it was asked in. Direct HTTP rather
    than `poll --agent-reply`: that rides a poll, which would consume whatever
    the driver queued meanwhile, and caps the text at 1000 characters."""
    return _post(api(key, "agent-reply"), {"text": text[:REPLY_LIMIT]})


def _save(reg) -> None:
    if hasattr(reg, "save"):
        reg.save()


def _reply(reg, log, task_id: str, key: str, text: str, msg_id=None) -> bool:
    """Post an answer the runner gives (a chat turn's, or an apology). When the
    page server does not take it, it is kept on the task record for
    `send_kept` rather than lost, and logged this once."""
    if post_reply(key, text):
        return True
    tasks.record(reg, task_id).setdefault("replies", []).append(
        {"key": key, "text": text, "id": msg_id, "at": time.time()})
    _save(reg)
    log(f"{task_id}: the page server did not take the answer for {key}; it is "
        f"kept and sent once it does")
    return False


def send_kept(reg, log, task_id: str) -> int:
    """Send the answers the page server missed, oldest first, stopping at the
    first it still refuses. Returns how many went."""
    kept = tasks.record(reg, task_id).get("replies") or []
    sent = 0
    while kept and post_reply(kept[0]["key"], kept[0]["text"]):
        kept.pop(0)
        sent += 1
    if sent:
        _save(reg)
        log(f"{task_id}: {sent} kept answer(s) delivered")
    return sent


# --------------------------------------------------------------------------- the turn

def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _queue(reg, task_id: str, text: str, reply_key: str, page: str,
           thread: dict | None, msg_id: str) -> None:
    tasks.record(reg, task_id).setdefault("chats", []).append({
        "id": msg_id, "text": text, "reply_key": reply_key, "page": str(page),
        "thread": thread or None, "at": time.time()})


def ask(cfg, reg, log, task_id: str, text: str, reply_key: str, page: str,
        thread: dict | None = None) -> None:
    """Give one chat message to the task's session. `reply_key` is the
    page-server session the answer goes to: the page itself, or a thread on it.

    A running turn gets it pushed in (`runs.push`): the session reads it at
    its next tool call, answers it with `chat-reply` and carries on. A free
    session gets a chat turn of its own. Only a running turn that cannot take
    it (it does not read stdin, or has stopped reading) makes it wait for the
    turn to end."""
    rec = tasks.record(reg, task_id)
    msg_id = new_id()
    if not thread:
        prompt = PUSH_PROMPT.format(text=text, key=reply_key, id=msg_id,
                                    pipeline=pipeline_command(cfg))
        for run in (rec.get("active_runs") or {}).values():
            if runs.push(run, prompt, api(reply_key, "agent-reply"), id=msg_id, said=text):
                log(f"{task_id}: chat {msg_id} pushed into the running "
                    f"{run.get('stage') or 'turn'}; its answer goes to {reply_key}")
                return
    _queue(reg, task_id, text, reply_key, page, thread, msg_id)
    _save(reg)
    if rec.get("active_runs"):
        log(f"{task_id}: chat waits — the running turn cannot take it; it is "
            f"answered when that turn ends")
        return
    start_next(cfg, reg, log, task_id)


def answer(key: str, text: str, msg_id: str = "") -> bool:
    """`pipeline.py chat-reply`: post a session's answer, and note it on the
    run it came from so the reaper knows which pushed message was answered."""
    sent = post_reply(key, text)
    out = os.environ.get("CADRE_RUN_OUT")
    if sent and out:
        with open(Path(out).parent / ANSWERED, "a") as f:
            f.write(f"{key} {msg_id}".strip() + "\n")
    return sent


def _reply_key(line: dict) -> str:
    return str(line.get("reply_url") or "").rsplit("/", 2)[-2]


def unanswered(run: dict) -> list[dict]:
    """Pushed chat lines of a run with no answer recorded: never read, or read
    and not answered with `chat-reply`. An answer with an id covers the
    message with that id. One without (a session still using the reply
    command from before ids) covers the oldest uncovered message to its key."""
    lines = [l for l in runs.inbox(run) if str(l.get("reply_url") or "").endswith("/agent-reply")]
    try:
        done = [l.split() for l in (Path(run["run_dir"]) / ANSWERED).read_text().splitlines()
                if l.strip()]
    except (OSError, KeyError):
        done = []
    ids = {d[1] for d in done if len(d) > 1}
    keys = [d[0] for d in done if len(d) == 1]
    left = []
    for line in lines:
        if line.get("id") and line["id"] in ids:
            continue
        if _reply_key(line) in keys:
            keys.remove(_reply_key(line))
        else:
            left.append(line)
    return left


def requeue_unread(cfg, reg, log, task_id: str, run: dict, page: str) -> int:
    """Pushed chat that got no answer (it arrived as the turn closed, the
    session died, or it read the message and never ran `chat-reply`): each
    becomes a chat turn of its own, with the driver's own words, so every
    message is answered. A line pushed before lines carried those words is
    handed on as the session saw it."""
    n = 0
    for line in unanswered(run):
        _queue(reg, task_id, str(line.get("said") or line.get("text") or ""),
               _reply_key(line), page, None, line.get("id") or new_id())
        n += 1
    if n:
        log(f"{task_id}: {n} chat message(s) got no answer in the turn — "
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
        log(f"{task_id}: chat dropped — no session or working directory to resume")
        _reply(reg, log, task_id, chat["reply_key"],
               "This page has no session to answer from (it never ran, or its "
               "directory is gone). Send page starts a round.", chat.get("id"))
        _save(reg)
        return False
    try:
        return _spawn(cfg, reg, log, task_id, rec, chat, session_id, cwd)
    except Exception as e:
        # The message is already off the queue: say so where it was asked
        # rather than let it vanish into the daemon log.
        log(f"{task_id}: chat turn could not start — {e}")
        _reply(reg, log, task_id, chat["reply_key"],
               f"I could not start a turn to answer that ({e}).", chat.get("id"))
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
    # Straight to runs.spawn, past the runner's max_concurrent_runs cap, on
    # purpose: the driver is waiting on this answer, and a chat must not queue
    # behind other tasks' rounds.
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
        "chat_id": chat.get("id"), "thread": thread}
    _save(reg)
    log(f"{task_id}: chat turn spawned — pid {proc} (resumed session "
        f"{session_id[:8]}); the answer goes to {chat['reply_key']}")
    return True


def finish(reg, log, task_id: str, run: dict, ok: bool, result: str) -> bool:
    """A chat turn ended: its answer goes into the chat it was asked in, or
    is kept until the page server takes it. Nothing else happens — no page
    opens and the round count stays."""
    text = (result or "").strip()
    if not ok or not text:
        why = "timed out" if run.get("timed_out_after") is not None else "failed"
        text = (f"I could not answer that: the session {why}"
                + (f" ({text[:300]})" if text else "") + ". Your message is in "
                "the session; ask again, or press Send page for a full round.")
    sent = _reply(reg, log, task_id, run["reply_key"], text, run.get("chat_id"))
    log(f"{task_id}: chat turn {'answered' if ok else 'FAILED'} — "
        f"{'posted to' if sent else 'not yet posted to'} {run['reply_key']}: {text[:120]}")
    return sent
