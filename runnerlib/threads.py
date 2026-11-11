"""Message threads: an agent's quick questions to the driver, answered without a page.

A running agent asks something quick with `review-surface message <its page>
"question"` and keeps working, reading the answer with `review-surface poll
<thread file>` when it needs it. If the turn ends before the answer comes,
nobody is polling that thread any more, so the runner learns the page's
threads when a turn of its task is reaped (`watch_threads`) and, once the
driver answers, gives the answer to the session as a chat turn whose reply
goes back into the thread (runnerlib/chat.py).

The threads a task's agent started are kept on its task record (`threads`:
key -> file, title, page) until the page server says the thread has ended.
The page server owns the thread files and the routes.
"""

import json
import urllib.request

from . import chat, surface, tasks


def _get(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def watch_threads(cfg, reg, log, task_id: str, page) -> int:
    """After a turn ends: learn the message threads its agent started on its
    page, and deliver any answer the driver gave that the agent did not read.
    Returns how many answers were delivered."""
    rec = tasks.record(reg, task_id)
    listed = (_get(chat.api(surface.page_key(page), "threads")) or {}).get("threads") or []
    here = {t.get("key"): t for t in listed if t.get("here") and t.get("key")}
    known = rec.setdefault("threads", {})
    if set(here) - set(known):
        cdir = surface.conversations_dir()
        files = ({surface.page_key(f): str(f) for f in cdir.glob("*.html")}
                 if cdir.is_dir() else {})
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
    here would take the answer from it, so this returns 0 and the reap of
    that turn delivers it (`watch_threads`)."""
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
    words = [surface.with_images(str(p.get("prompt") or ""), p) if isinstance(p, dict) else str(p)
             for p in (payload or {}).get("prompts") or []]
    words = [w for w in words if w]
    if not words:
        return 0
    chat.ask(cfg, reg, log, task_id, "\n\n".join(words), key, thread.get("page") or "",
             thread={"file": thread["file"], "title": thread.get("title") or ""})
    return 1
