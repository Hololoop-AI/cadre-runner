"""Learn's study hand-off, over the Rust blackboard (runnerlib/rboard.py).

Any cadre that needs something learned asks Learn on the board, and Learn
answers on the board:

1. **The ask.** A thread, a page or `pipeline.py study` writes an `ask` in
   namespace `learn`, topic `study`, key `ask:<id>`, with `data`:

       asker     the cadre that asked (dev, kitchen, personal, …)
       page      the page the driver was reading, if any
       thread    the thread that asked, if one did
       material  what to learn, in the driver's words
       purpose   decision (to rule on a page) | skill (to keep) | course
                 (a whole subject)

   plus what starting a session needs (`from_task`, `cwd`, `passage`) and
   the `machine` it was asked on: the folders it names are that machine's.

2. **Learn takes it** (`take`, every daemon pass): it claims the ask under a
   lease, then either folds it into a session still open on the same
   material (that session's next round is told, and the asker's cadre lists
   it too) or starts a session for it (tasks.submit_study: a tutor run,
   owned by the cadre that owns the tutor node, listed in the asker's).
   Only then is the claim completed. Every step is safe to repeat: the
   session's id is chosen and recorded before it starts, and starting the
   same id twice starts one session, so a crash anywhere before the
   complete just means the ask is taken again when its lease runs out.

3. **The answer.** A session ends when the driver ends it (the approve
   verdict on its page, read off the runner's own board), or when it has
   sat open for `OPEN_FOR` without one. Learn then writes an `answer` into
   each asker's namespace, topic `study`, the ask's own key, `ask_seq` set,
   with `data` {ready, cards_kept, session_page}: `ready` is what the tutor
   recorded (`pipeline.py study-ready <session> yes|no`), `cards_kept` the
   cards anchored to the session that the driver kept. An ask that could
   not start a session is answered too, saying why. Answers carry an
   idempotency key, so a retry never writes one twice.

4. **Delivery** (`deliver`): this machine reads the answers in its own
   cadres' namespaces after its cursor and posts each into the asking
   page's conversation, and the thread's. The cursor only moves past an
   answer once it is posted, or has nowhere on this machine to go.

`request` is the one way in for a person or an agent: it writes the ask and
takes it at once, through the same claim, so the caller can link the
session it started. A second Learn racing for the same ask simply loses the
claim. With no board at all, it starts the session directly and says that
no answer will come back.

Assumed, and to revisit when it stops being true: the ask, its session and
its answer happen on one machine (the board is a local Unix socket; the
folders and pages in an ask are paths on the asking machine). An ask from
another machine is left for that machine's Learn.

What Learn remembers (`<data_dir>/learn-study.json`): each session's asks,
whether it is open and whether the tutor said ready, and the asks taken.
Nothing here is about code: the material can be a recipe, a medical
question or a codebase. The runner's own heartbeats and task events stay on
its own board (runnerlib/blackboard.py) until they move on their own card.
"""

import fcntl
import json
import os
import re
import tempfile
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import cadres, rboard

NAMESPACE = cadres.LEARN
TOPIC = "study"
CONSUMER = "learn"
PURPOSES = ("decision", "skill", "course")
FILE = "learn-study.json"
LEASE_MS = 120_000
# A session open this long without a verdict is answered as ended, so its
# askers are not left waiting and new asks stop folding into it.
OPEN_FOR = 14 * 86400


class NoBoard(Exception):
    """The Rust board is not reachable; the caller decides what to do instead."""


def _runner(cfg) -> dict:
    return (getattr(cfg, "runner", None) or {}) if cfg else {}


def client(cfg=None) -> rboard.Client:
    sock = _runner(cfg).get("blackboard_socket")
    return rboard.Client(os.path.expanduser(sock) if sock else None)


def learn_namespace(cfg) -> str:
    """Where Learn reads asks: `learn`, unless `[runner] learn_namespace`
    says otherwise (the live test uses a namespace of its own, so the
    machine's own Learn never takes its asks)."""
    return str(_runner(cfg).get("learn_namespace") or NAMESPACE)


def namespace_of(cadre: str) -> str:
    """The board namespace a cadre answers in: its id (Other and Holo
    without the leading ~)."""
    return str(cadre or cadres.OTHER).lstrip("~") or "other"


_MACHINE: dict = {}


def machine(cfg) -> str:
    """This machine's name, as the fleet shows it (backends.machine_name),
    worked out once per process."""
    if "name" not in _MACHINE:
        from .backends import machine_name
        _MACHINE["name"] = machine_name(_runner(cfg))
    return _MACHINE["name"]


def _who(cfg, role: str) -> dict:
    return rboard.who(f"cadre/{role}", machine(cfg), os.environ.get("CADRE_SESSION_ID", ""))


def same_material(a: str, b: str) -> bool:
    """Two asks are on the same topic when their material reads the same,
    ignoring case, spacing and punctuation. Deliberately plain: a near match
    starts its own session rather than folding into the wrong one."""
    def norm(t):
        return " ".join(re.sub(r"[^\w\s]", " ", str(t or "").casefold()).split())
    return bool(norm(a)) and norm(a) == norm(b)


# ------------------------------------------------------------------ the state


def _path(cfg) -> Path:
    return Path(cfg.data_dir) / FILE


def _load(cfg) -> dict:
    try:
        raw = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("sessions", {})
    raw.setdefault("asks", {})
    return raw


@contextmanager
def _state(cfg):
    """The state, read and written back under one lock."""
    d = Path(cfg.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / (FILE + ".lock"), "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            st = _load(cfg)
            yield st
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".learn-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(st, f, indent=1, sort_keys=True)
            os.replace(tmp, _path(cfg))
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def session_page(cfg, session: str) -> str:
    return str(Path(cfg.data_dir) / "surfaces" / f"task-{session}.html")


def sessions(cfg) -> list[dict]:
    """Learn's study sessions, newest first, as the Review tab and Learn's
    home show them: {session, material, open, ready, since, purposes,
    askers, page (a path on this machine)}. A failed ask is not a session."""
    out = []
    for sid, s in _load(cfg)["sessions"].items():
        if s.get("failed"):
            continue
        asks = s.get("asks") or ()
        out.append({"session": sid, "material": str(s.get("material") or ""),
                    "open": bool(s.get("open")), "ready": bool(s.get("ready")),
                    "since": float(s.get("since") or 0),
                    "purposes": sorted({str(a.get("purpose") or "") for a in asks}),
                    "askers": sorted({str(a.get("asker") or "") for a in asks}),
                    "page": session_page(cfg, sid)})
    return sorted(out, key=lambda r: -r["since"])


# ------------------------------------------------------------------ the ask


def ask(cfg, *, asker: str, material: str, purpose: str = "decision", page: str = "",
        thread: str = "", from_task: str = "", cwd: str = "", passage: str = "",
        board: rboard.Client | None = None) -> dict:
    """Write a study ask for Learn; returns the stored event. Raises
    ValueError for a bad ask and NoBoard when the board is not there."""
    material = (material or "").strip()
    if not material:
        raise ValueError("name what to learn")
    if purpose not in PURPOSES:
        raise ValueError(f"purpose is one of {', '.join(PURPOSES)}, not {purpose!r}")
    key = f"ask:{uuid.uuid4().hex[:12]}"
    data = {"asker": asker or cadres.OTHER, "page": str(page or ""), "thread": thread or "",
            "material": material, "purpose": purpose, "machine": machine(cfg)}
    data.update({k: v for k, v in (("from_task", from_task), ("cwd", cwd), ("passage", passage)) if v})
    board = board or client(cfg)
    try:
        return board.emit(learn_namespace(cfg), TOPIC, key, "ask",
                          {"question": material[:2000], "expects": {"type": "text"}},
                          who=_who(cfg, "ask"), data=data, correlation_id=key)
    except rboard.BoardRefused:
        raise
    except rboard.BoardError as e:
        raise NoBoard(str(e)) from e


# ------------------------------------------------------------------ Learn takes it


def take(cfg, log=print, board: rboard.Client | None = None, key: str = "") -> list[dict]:
    """Claim every open study ask on this machine (only `key`'s when given)
    and start or fold a session for each. Returns [{seq, session, folded}]
    for what it took."""
    board = board or client(cfg)
    took, passed = [], []
    while True:
        ev = board.claim(CONSUMER, LEASE_MS, namespace=learn_namespace(cfg), topic=TOPIC,
                         kind="ask", key=key)
        if ev is None or ev["seq"] in passed:
            return took
        got = _take_one(cfg, log, board, ev)
        if got is None:
            passed.append(ev["seq"])            # another machine's: its lease runs out for it
        elif got:
            took.append(got)
        if key:
            return took


def _entry(ev: dict) -> dict:
    data = ev.get("data") or {}
    return {"seq": int(ev["seq"]), "key": ev["key"],
            "correlation_id": ev.get("correlation_id") or ev["key"],
            "asker": str(data.get("asker") or cadres.OTHER), "page": str(data.get("page") or ""),
            "thread": str(data.get("thread") or ""), "purpose": str(data.get("purpose") or "decision"),
            "material": str(data.get("material") or ev.get("question") or ""), "answered": False}


def _complete(board, log, seq: int) -> None:
    """Mark the claim done. A lease that ran out first is no loss: the next
    taker finds the ask recorded and only completes it."""
    try:
        board.complete(CONSUMER, seq)
    except rboard.BoardRefused as e:
        log(f"learn: ask {seq} was not completed ({e}); the next pass completes it")


def _take_one(cfg, log, board, ev: dict) -> dict | None:
    """One claimed ask. Returns what was taken, {} when it was taken before
    (completed again), or None when it is another machine's (left alone)."""
    from . import tasks as tasks_mod
    data = ev.get("data") or {}
    if data.get("machine") and data["machine"] != machine(cfg):
        return None
    entry = _entry(ev)
    seq = str(entry["seq"])
    now = time.time()
    # Decide and reserve under one lock. A new session is recorded, marked
    # `starting`, before anything starts: a second ask on the same material
    # joins it, and a taker after a crash finds it and starts it again (the
    # same id starts one session).
    with _state(cfg) as st:
        known = st["asks"].get(seq)
        sess = st["sessions"].get(known) if known else None
        if sess and not sess.get("starting"):
            done, folded, session = True, False, known
        elif sess:                                  # reserved, maybe never started
            done, folded, session = False, False, known
        else:
            done = False
            session = next((t for t, x in st["sessions"].items()
                            if x.get("open") and not x.get("failed")
                            and now - float(x.get("since") or now) < OPEN_FOR
                            and same_material(x.get("material"), entry["material"])), None)
            folded = session is not None
            if folded:
                st["sessions"][session]["asks"].append(entry)
            else:
                session = tasks_mod.new_id(f"Study: {entry['material']}")
                st["sessions"][session] = {"material": entry["material"], "open": True,
                                           "starting": True, "since": now, "asks": [entry]}
            st["asks"][seq] = session
    if done:
        _complete(board, log, entry["seq"])
        return {}
    if folded:
        try:
            tasks_mod.carry_note(cfg, session, (
                f"Another ask joined this session, from the {entry['asker']} cadre"
                + (f" (page {entry['page']})" if entry["page"] else "")
                + f", for a {entry['purpose']}: {entry['material']}"), source="learn")
        except (OSError, ValueError) as e:
            log(f"learn: could not tell {session} about ask {seq}: {e}")
        cadres.show_also(cfg.data_dir, session, entry["asker"])
    else:
        try:
            tasks_mod.submit_study(cfg, entry["material"], from_task=data.get("from_task") or None,
                                   cwd=data.get("cwd") or None, page=entry["page"] or None,
                                   passage=str(data.get("passage") or ""), asker=entry["asker"],
                                   task_id=session)
        except Exception as e:                  # a bad ask must not wedge the queue
            log(f"learn: ask {seq} could not start a session: {e}")
            with _state(cfg) as st:
                # every ask that joined it meanwhile is answered with why
                x = st["sessions"].pop(session)
                st["sessions"][f"failed:{seq}"] = {
                    "material": entry["material"], "open": False, "failed": True, "since": now,
                    "asks": [{**a, "error": str(e)[:300]} for a in x["asks"]]}
                for a in x["asks"]:
                    st["asks"][str(a["seq"])] = f"failed:{seq}"
            send_answers(cfg, log, board)       # answered before the claim completes
            _complete(board, log, entry["seq"])
            return {}
        with _state(cfg) as st:
            st["sessions"][session].pop("starting", None)
    _complete(board, log, entry["seq"])
    log(f"learn: ask {seq} from {entry['asker']} → "
        f"{'folded into' if folded else 'started'} {session}")
    return {"seq": entry["seq"], "session": session, "folded": folded}


# ------------------------------------------------------------------ the session ends


def mark_ready(cfg, session: str, ready: bool) -> None:
    """The tutor's word on whether the driver now has what they asked for."""
    with _state(cfg) as st:
        if session not in st["sessions"]:
            raise ValueError(f"{session} is not a session Learn took from the board")
        st["sessions"][session]["ready"] = bool(ready)


def _kept_by_session(cfg) -> dict:
    """{session id: cards kept that are anchored to it}, from one read of the deck."""
    from . import cards as cards_mod
    try:
        accepted = cards_mod.listing(cfg.cards_dir, status="accepted")
    except (OSError, cards_mod.CardError) as e:
        raise rboard.BoardError(f"the deck could not be read: {e}") from e
    out: dict = {}
    for c in accepted:
        anchor = str(c.get("anchor") or "")
        if anchor:
            out[anchor] = out.get(anchor, 0) + 1
    return out


def _approved(cfg, task_ids) -> set:
    """Which of these tasks the driver approved: their verdict on the
    runner's own board."""
    from . import engine_seam, tasks as tasks_mod
    from .blackboard import Board
    path = engine_seam.board_path(cfg)
    if not task_ids or not Path(path).exists():
        return set()
    board = Board(path)
    try:
        return {t for t in task_ids
                if any(e["payload"].get("target") == tasks_mod.TARGET_VERDICT
                       and e["payload"].get("verdict") == "approve"
                       for e in board.peek(topic="tasks", key=tasks_mod.key_for(t), kind="command"))}
    finally:
        board.close()


def close_ended(cfg, log=print, now: float | None = None) -> list[str]:
    """Close every open session the driver approved, or that sat open past
    OPEN_FOR. Returns the sessions it closed."""
    now = time.time() if now is None else now
    open_ = {t: s for t, s in _load(cfg)["sessions"].items() if s.get("open")}
    approved = _approved(cfg, list(open_))
    closed = []
    with _state(cfg) as st:
        for t in open_:
            s = st["sessions"].get(t) or {}
            stale = now - float(s.get("since") or now) >= OPEN_FOR
            if s.get("open") and (t in approved or stale):
                s["open"] = False
                s["ended"] = now
                s["ending"] = "approved" if t in approved else "left open"
                closed.append(t)
    for t in closed:
        log(f"learn: {t} ended ({'approved' if t in approved else 'left open'}) — answering its asks")
    return closed


def send_answers(cfg, log=print, board: rboard.Client | None = None) -> int:
    """Answer every ask of every closed session not answered yet. Stops at
    the first the board will not take; the next pass carries on."""
    board = board or client(cfg)
    pending = [(sid, s) for sid, s in _load(cfg)["sessions"].items()
               if not s.get("open") and any(not a.get("answered") for a in s.get("asks") or ())]
    if not pending:
        return 0
    kept = _kept_by_session(cfg)
    sent = 0
    for sid, s in pending:
        done = []
        for entry in s["asks"]:
            if entry.get("answered"):
                continue
            try:
                if s.get("failed"):
                    _emit_answer(cfg, board, entry, ready=False, cards_kept=0, session_page="",
                                 text=f"Learn could not start a session for this: {entry.get('error')}")
                else:
                    _emit_answer(cfg, board, entry, ready=bool(s.get("ready")),
                                 cards_kept=kept.get(sid, 0), session_page=session_page(cfg, sid),
                                 ending=s.get("ending") or "")
            except rboard.BoardError as e:
                log(f"learn: answer to ask {entry['seq']} waits for the board: {e}")
                _mark_answered(cfg, sid, done)
                return sent + len(done)
            done.append(entry["seq"])
        _mark_answered(cfg, sid, done)
        sent += len(done)
    return sent


def _mark_answered(cfg, session: str, seqs) -> None:
    if not seqs:
        return
    with _state(cfg) as st:
        for a in st["sessions"][session]["asks"]:
            if a["seq"] in seqs:
                a["answered"] = True


def _emit_answer(cfg, board, entry: dict, *, ready: bool, cards_kept: int,
                 session_page: str, text: str = "", ending: str = "") -> dict:
    how = " It was left open without being ended, and closed." if ending == "left open" else ""
    text = text or (f"Study on “{entry['material'][:120]}” finished: "
                    f"{'ready' if ready else 'not ready yet'}, {cards_kept} "
                    f"card{'' if cards_kept == 1 else 's'} kept.{how}")
    return board.emit(namespace_of(entry["asker"]), TOPIC, entry["key"], "answer",
                      {"answer": text, "ask_seq": int(entry["seq"])},
                      who=_who(cfg, "learn"), correlation_id=entry.get("correlation_id"),
                      idempotency_key=f"learn-answer:{entry['seq']}",
                      data={"ready": bool(ready), "cards_kept": int(cards_kept),
                            "session_page": session_page, "page": entry.get("page") or "",
                            "thread": entry.get("thread") or "", "material": entry["material"]})


# ------------------------------------------------------------------ delivery


def _local_namespaces(cfg) -> list[str]:
    """The namespaces this machine's cadres answer in (where it reads answers)."""
    from . import projects
    ids = [c["id"] for c in cadres.cadre_list(projects.load(cfg.data_dir))]
    return list(dict.fromkeys(namespace_of(c) for c in [*ids, cadres.OTHER, cadres.HOLO]))


def deliver(cfg, log=print, board: rboard.Client | None = None, post=None) -> int:
    """Post every study answer in this machine's cadres' namespaces after its
    cursor into the page that asked and the thread that asked, when they are
    on this machine. `post(page path, text)` returns True (posted), None
    (the page has no live conversation: nowhere to post) or False (try
    again later). The cursor moves past an answer only once it is posted or
    has nowhere to go here. Returns how many answers it got through."""
    board = board or client(cfg)
    post = post or (lambda page, text: _post_to_page(cfg, page, text))
    name = f"cadre-runner:study-answers:{machine(cfg)}"
    after = board.cursor(name)
    events = sorted((e for ns in _local_namespaces(cfg)
                     for e in board.read(after=after, namespace=ns, topic=TOPIC, kind="answer")),
                    key=lambda e: e["seq"])
    through = 0
    last = after
    for ev in events:
        data = ev.get("data") or {}
        text = str(ev.get("answer") or "")
        if data.get("session_page"):
            text += " Session page: /open?file=" + urllib.parse.quote(data["session_page"], safe="/")
        stuck = False
        for target in (data.get("page"), _thread_page(cfg, data.get("thread"))):
            if not target or not Path(target).exists():
                continue
            try:
                ok = post(str(target), text)
            except Exception as e:
                ok = False
                log(f"learn: answer {ev['seq']} not posted to {target}: {e}")
            if ok:
                log(f"learn: answer {ev['seq']} posted to {Path(target).name}")
            elif ok is None:
                log(f"learn: answer {ev['seq']} has no open conversation at {Path(target).name}")
            else:
                stuck = True
        if stuck:
            break                               # this one and the rest go next pass
        last, through = int(ev["seq"]), through + 1
    if last != after:
        board.set_cursor(name, last)
    return through


def _thread_page(cfg, thread: str) -> str:
    if not thread:
        return ""
    from . import study_thread
    try:
        return str(study_thread.load(cfg, thread).get("page") or "")
    except (OSError, ValueError):
        return ""


def _post_to_page(cfg, page: str, text: str) -> bool | None:
    """Into the page's conversation, as an agent reply (chat.post_reply). No
    poll: a poll would consume the driver's queued notes. None when the page
    has no open session to post into."""
    from . import chat, surface
    key = (surface.sessions(cfg).get(page) or {}).get("key") or ""
    return chat.post_reply(key, text) if key else None


# ------------------------------------------------------------------ the daemon's pass


def tick(cfg, log=print, board: rboard.Client | None = None) -> dict:
    """One pass of Learn on this machine: take asks, close sessions that
    ended, send answers, deliver answers. Quiet when there is no board: the
    health check keeps an install without one from logging every pass."""
    board = board or client(cfg)
    if not board.up():
        return {"board": False}
    out = {"board": True, "took": [], "closed": [], "sent": 0, "delivered": 0}
    for step, fn in (("took", lambda: take(cfg, log, board)),
                     ("closed", lambda: close_ended(cfg, log)),
                     ("sent", lambda: send_answers(cfg, log, board)),
                     ("delivered", lambda: deliver(cfg, log, board))):
        try:
            out[step] = fn()
        except rboard.BoardError as e:
            log(f"learn: {step} stopped: {e}")
    return out


# ------------------------------------------------------------------ one way in


@dataclass
class Started:
    """What `request` did. `session` is None only when another Learn took
    the ask first (its session will answer on the board); `via` is "board",
    or "direct" when there was no board (`note` says what that means)."""
    session: str | None
    seq: int | None
    folded: bool
    via: str
    note: str = ""


def request(cfg, material: str, *, from_task: str = "", cwd: str = "", page: str = "",
            passage: str = "", thread: str = "", purpose: str = "", asker: str = "",
            log=print, board: rboard.Client | None = None) -> Started:
    """How every study request starts (`pipeline.py study`, a thread's offer,
    the Review tab's study box): write the ask, then take it at once, as
    Learn, through the same claim the daemon uses, so the caller can link
    the session it started.

    `asker` defaults to the cadre owning `from_task`, `purpose` to decision
    when there is a page to rule on and skill otherwise."""
    from .tasks import submit_study
    cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
    if not asker and from_task:
        asker = cadres.load(cfg.data_dir).get(from_task, "")
    purpose = purpose or ("decision" if (from_task or page) else "skill")
    board = board or client(cfg)
    try:
        ev = ask(cfg, asker=asker or cadres.OTHER, material=material, purpose=purpose,
                 page=page, thread=thread, from_task=from_task, cwd=cwd, passage=passage,
                 board=board)
    except NoBoard as e:
        session = submit_study(cfg, material, from_task=from_task or None, cwd=cwd,
                               page=page or None, passage=passage, asker=asker)
        return Started(session, None, False, "direct",
                       f"no study board ({e}); started without an ask, so no answer comes back")
    took = take(cfg, log, board, key=ev["key"])
    if not took:
        return Started(None, ev["seq"], False, "board")
    return Started(took[0]["session"], took[0]["seq"], took[0]["folded"], "board")
