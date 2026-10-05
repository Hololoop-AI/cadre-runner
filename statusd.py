#!/usr/bin/env python3
"""Cadre page server: agent-manager home page + static assets + reverse proxy
to Review Surface.

Replaces the bare `python -m http.server` in cadre-status.service, same port,
per cadre-context decision 2026-08-26-surface-as-driver-channel: ONE exposed
port on the tailnet; review-surface keeps its loopback default and every
surface session is reached through this proxy.

Routing rule: `/` is the fleet home page — server-rendered here from the
daemon's status.json snapshot (project -> story -> stage sessions, sorted so
the rows that want a human sit on top) with the new-task form at the top;
`POST /tasks` submits that form. Any other path that resolves to a file in the
status directory is served statically (the legacy dashboard is still at
/index.html); everything else is forwarded verbatim to the Review Surface
server (`surface.upstream()`, loopback by default) — its pages use root-relative
asset URLs, so prefix-free forwarding is the only shape that works. /shutdown is
blocked: nothing reachable from the network may stop the surface server.

The directory served, the bind address and the port come from the runner's
config (`[runner] data_dir / status_bind / status_port`), with env overrides,
so the page this serves is the page the daemon writes and a host with a network
policy can narrow the bind without a patch.

Streams responses chunk-by-chunk so the surface's SSE channel (/events/:key)
works through the proxy.
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runnerlib import activity as activity_mod
from runnerlib import codestamp
from runnerlib import config as config_mod
from runnerlib import conversations as conv_mod
from runnerlib import costs as costs_mod
from runnerlib import library as library_mod
from runnerlib import backends as backends_mod
from runnerlib import projects as projects_mod
from runnerlib import recent as recent_mod
from runnerlib import surface as surface_mod
from runnerlib.tasks import TARGET_REQUEST as TASK_REQUEST, TOPIC as TASK_TOPIC


def _runner_config():
    """The daemon's config when there is one. This service is started by hand
    as often as by systemd and must come up either way, so a missing or broken
    config is a fallback to the shipped defaults, never a failure to serve."""
    try:
        return config_mod.load(os.environ.get("CADRE_CONFIG"))
    except SystemExit:
        return None


_CFG = _runner_config()
_DEFAULTS = config_mod.DEFAULTS["runner"]


def _runner(key):
    return _CFG.runner[key] if _CFG else _DEFAULTS[key]


def _skills_source():
    return _CFG.skills_source if _CFG else Path(os.path.expanduser(_DEFAULTS["skills_source"]))


# Env wins over config wins over the defaults: a unit file overrides one dial
# without a config edit, and the config is what keeps the served directory the
# SAME directory the daemon writes its status into (they drifted while this was
# a literal path).
STATUS_DIR = Path(os.environ.get("CADRE_STATUS_DIR") or
                  (_CFG.data_dir / "status" if _CFG
                   else Path(os.path.expanduser(_DEFAULTS["data_dir"])) / "status"))
# Where the daemon dead-letters feedback batches nothing routed (surface.py
# `_dead_letter`). Same data dir as the status snapshot, by the same rule.
STRANDED_DIR = (_CFG.data_dir if _CFG else
                Path(os.path.expanduser(_DEFAULTS["data_dir"]))) / "surfaces" / "stranded"
# The panel's repair log: link records in review-surface's link-store shape,
# read last so a repair made here wins over what any other store says.
PANEL_LINKS = STRANDED_DIR.parent / "panel-links.jsonl"
# The durable project records (runnerlib/projects.py), same data dir.
DATA_DIR = STRANDED_DIR.parent.parent
SURFACE = surface_mod.upstream()
# What this process loaded at startup, to say so on the page once an edit to
# it is waiting for a restart (codestamp.py).
_CODE = codestamp.Stamp()
BIND = backends_mod.binds(os.environ.get("CADRE_STATUS_BIND") or _runner("status_bind"))
PORT = int(os.environ.get("CADRE_STATUS_PORT") or _runner("status_port"))
# This process as a backend (runnerlib/backends.py): which machine it is, and
# an id that lets the fleet tell its own backend from a remote one.
INSTANCE = uuid.uuid4().hex
MACHINE = backends_mod.machine_name(_CFG.runner if _CFG else {})
# Every backend the fleet shows, this machine's first — all reached the same
# way, over HTTP, through the API below. This machine's URL is resolved per
# request from the socket the request came in on (Handler._backends).
BACKENDS = backends_mod.configured(_CFG.raw if _CFG else {})
BLOCKED = {"/shutdown"}
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "host",
               "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "upgrade"}
TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json",
         ".css": "text/css", ".js": "text/javascript", ".png": "image/png",
         ".svg": "image/svg+xml"}

HOME_PATH = "/"
TASKS_PATH = "/tasks"
REPAIR_PATH = "/repair"
PROJECTS_PATH = "/projects"
LIBRARY_PATH = "/library"
COSTS_PATH = "/costs"
RUNS_PATH = "/runs"
# rows on the fleet's "Recently returned" card, and all the API ships of them
RECENT_LIMIT = 6
STREAM_PATH = "/fleet/events"
# A surface opens inside the fleet's own frame, so the fleet stays one click
# away. Opening review-surface's /session/<key> as the whole tab left the
# driver with nothing to click but the browser's back button.
VIEW_PATH = "/view/"
# Keys archived from this server. The row leaves the snapshot at the daemon's
# next pass; until then it is hidden here, so a click visibly does something.
_ARCHIVED: set[str] = set()
# Lucide "archive" glyph, stroked in the text colour so it follows the theme.
ARCHIVE_ICON = ('<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round"><rect width="20" height="5" x="2" y="3" rx="1"/>'
                '<path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/></svg>')
SESSION_PREFIX = "/session/"
SESSION_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_TASK_BYTES = 64 * 1024
# The page is pushed, not polled: it refreshes when the fleet stream says
# something changed. This slow poll runs only while the stream is down.
FALLBACK_POLL_MS = 30000
# Fleet stream cadence: how often the server looks at the runner's snapshot
# (a local stat + hash, no network), and how often it pings an idle client.
STREAM_TICK = 1.0
STREAM_PING = 15.0

# Surface session kinds that are a QUESTION to the driver (a verdict, an answer)
# rather than a report. An open one of these is the strongest "this is waiting
# on you" signal the runner has, stronger than any phase.
VERDICT_KINDS = {"risk_hold", "spec_review", "final_review", "ask"}
# Phases that mean the pipeline has handed the story back to a human.
ATTENTION_PHASES = {"final-review"}
DONE_PHASES = {"done"}

# Sort buckets, ascending = higher on the page. The driver's rule: things that
# want a human first, then things that finished and nobody has acknowledged,
# then quiet work in progress.
RANK_NEEDS_HUMAN, RANK_FINISHED, RANK_RUNNING = 0, 1, 2
RANK_LABELS = {RANK_NEEDS_HUMAN: "needs you",
               RANK_FINISHED: "finished",
               RANK_RUNNING: "in progress"}


# ---------------------------------------------------------------- surface view

def view_href(path) -> str:
    """Where a link to a surface should go: its framed view when it is a
    review-surface session, the path unchanged otherwise."""
    p = str(path or "")
    return VIEW_PATH + p[len(SESSION_PREFIX):] if p.startswith(SESSION_PREFIX) else p


def framable_header(path: str, name: str, value: str):
    """A proxied response header, adjusted so the fleet may frame a session.

    review-surface refuses all framing of /session/<key> by default. Through
    this proxy the fleet and the session share one origin, so allowing 'self'
    lets exactly the fleet frame it and still refuses every other site.
    Returns the value to send, or None to drop the header."""
    if not path.startswith(SESSION_PREFIX):
        return value
    lname = name.lower()
    if lname == "x-frame-options":
        return None
    if lname == "content-security-policy":
        return value.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
    return value


_VIEW_CSS = """
html,body{margin:0;height:100%;background:#0f1115;color:#e8e6e1;
  font:14px/1.4 system-ui,sans-serif}
body{display:flex;flex-direction:column}
.bar{display:flex;gap:1rem;align-items:center;padding:.45rem .9rem;
  border-bottom:1px solid #2a2e36;flex:none}
.bar a{color:#8fc7ff;text-decoration:none}
.bar a:hover{text-decoration:underline}
.bar .title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:#9aa4b2}
iframe{flex:1;border:0;width:100%;background:#fff}
"""

# The frame is same-origin with this page (both come through this server), so
# the bar can read the surface's own title, and follow it when a link inside
# the surface moves the frame to another surface.
_VIEW_JS = """
const f=document.getElementById('surface'),t=document.getElementById('title'),
  alone=document.getElementById('alone');
f.addEventListener('load',()=>{try{const d=f.contentDocument;
  if(d&&d.title){t.textContent=d.title;document.title=d.title+' — Cadre';}
  const p=f.contentWindow.location.pathname;
  if(p.startsWith('/session/')){alone.href=p;
    history.replaceState(null,'','/view/'+p.slice(9));}
}catch(e){}});
"""


def render_view(key: str) -> str:
    """The fleet's frame around one surface: a bar with the way back, and the
    surface itself filling the rest of the window."""
    src = SESSION_PREFIX + escape(key)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            # The page server's ring mark. A fresh URL, because Chrome remembers an
            # icon URL that once failed and stops asking for it: /favicon.ico 404ed
            # here before the page server learned to serve it.
            '<link rel="icon" type="image/svg+xml" href="/favicon.ico?v=2">'
            f'<title>Surface — Cadre</title><style>{_VIEW_CSS}</style></head><body>'
            '<nav class="bar"><a href="/">← fleet</a>'
            '<span class="title" id="title"></span>'
            f'<a id="alone" href="{src}" target="_blank" rel="noopener">open alone ↗</a></nav>'
            f'<iframe id="surface" src="{src}" title="surface"></iframe>'
            f'<script>{_VIEW_JS}</script></body></html>')


# --------------------------------------------------------------------- model

def read_snapshot(status_dir: Path | None = None) -> dict:
    """The daemon's status.json, or an empty snapshot. The home page is a
    read-only view of what the daemon already wrote: statusd never loads the
    registry itself, so a wedged or absent daemon degrades to an empty fleet
    instead of taking the page (and the surface proxy) down with it."""
    d = Path(status_dir) if status_dir is not None else STATUS_DIR
    try:
        snap = json.loads((d / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return snap if isinstance(snap, dict) else {}


def _runs_for(snap: dict, story: dict) -> list[dict]:
    """In-flight stage sessions belonging to a story. The daemon keys runs by
    slug; intake runs (no story registered yet) key by the board card id, so
    story_id is matched too."""
    keys = {story.get("slug"), story.get("story_id")} - {None}
    return [r for r in (snap.get("runs") or []) if r.get("story") in keys]


def _questions_for(snap: dict, story: dict) -> list[dict]:
    keys = {story.get("slug"), story.get("story_id")} - {None}
    return [q for q in (snap.get("questions") or []) if q.get("story") in keys]


def _last_activity(snap: dict, story: dict) -> float:
    """Newest timestamp that touched this story, from any channel the snapshot
    carries: a running stage, an open surface, a finished run in history."""
    stamps = [0.0]
    for r in _runs_for(snap, story):
        stamps.append(float(r.get("started") or 0))
    for sf in story.get("surfaces") or []:
        stamps.append(float(sf.get("opened") or 0))
    for q in _questions_for(snap, story):
        stamps.append(float(q.get("asked_at") or 0) if
                      isinstance(q.get("asked_at"), (int, float)) else 0.0)
    keys = {story.get("slug"), story.get("story_id")} - {None}
    for h in snap.get("recent") or []:
        if h.get("story") in keys:
            stamps.append(float(h.get("ended") or 0))
    return max(stamps)


def classify(snap: dict, story: dict) -> tuple[int, str]:
    """(rank, why) for one story. Order of the checks IS the priority: an
    escalation or an open verdict outranks 'done', which outranks running."""
    if story.get("status") == "escalated":
        return RANK_NEEDS_HUMAN, "escalated"
    if any(sf.get("stranded") for sf in (story.get("surfaces") or [])):
        return RANK_NEEDS_HUMAN, "feedback stranded"
    open_kinds = [sf.get("kind") for sf in (story.get("surfaces") or [])]
    waiting = [k for k in open_kinds if k in VERDICT_KINDS]
    if waiting:
        return RANK_NEEDS_HUMAN, f"awaiting {waiting[0].replace('_', ' ')}"
    if _questions_for(snap, story):
        return RANK_NEEDS_HUMAN, "question pending"
    if story.get("phase") in ATTENTION_PHASES:
        return RANK_NEEDS_HUMAN, "final review"
    if story.get("phase") in DONE_PHASES or story.get("status") == "done":
        return RANK_FINISHED, "done — unacknowledged"
    if _runs_for(snap, story):
        return RANK_RUNNING, "running"
    return RANK_RUNNING, story.get("phase") or "idle"


def _pr_links(story: dict) -> list[dict]:
    repo = story.get("repo") or ""
    out = []
    for n in story.get("prs") or ([story["planning_pr"]] if story.get("planning_pr") else []):
        try:
            num = int(n)
        except (TypeError, ValueError):
            continue
        out.append({"pr": num,
                    "url": f"https://github.com/{repo}/pull/{num}" if repo else ""})
    return out


def story_view(snap: dict, story: dict) -> dict:
    rank, why = classify(snap, story)
    runs = _runs_for(snap, story)
    return {
        "slug": story.get("slug") or "",
        "story_id": story.get("story_id") or story.get("slug") or "?",
        "title": story.get("title") or "",
        "repo": story.get("repo") or "(unassigned)",
        "phase": story.get("phase") or "—",
        "status": story.get("status") or "—",
        "rank": rank,
        "why": why,
        "activity": _last_activity(snap, story),
        # stage sessions: the agents themselves, deep-linked to their surface
        # when one is open.
        "runs": [{"stage": r.get("stage") or "stage", "slice": r.get("slice"),
                  "pr": r.get("pr"), "started": r.get("started")} for r in runs],
        "surfaces": [{"kind": sf.get("kind") or "surface", "path": sf.get("path") or "",
                      "pr": sf.get("pr"), "opened": sf.get("opened"),
                      "stranded": sf.get("stranded") or []}
                     for sf in (story.get("surfaces") or [])],
        "questions": _questions_for(snap, story),
        "prs": _pr_links(story),
    }


def fleet(snap: dict) -> list[dict]:
    """The whole hierarchy, sorted. Projects inherit the urgency of their most
    urgent story, so a repo with an escalation cannot hide below a quiet one."""
    groups: dict[str, list[dict]] = {}
    for st in snap.get("stories") or []:
        view = story_view(snap, st)
        groups.setdefault(view["repo"], []).append(view)
    projects = []
    for repo, stories in groups.items():
        stories.sort(key=lambda s: (s["rank"], -s["activity"], s["story_id"]))
        projects.append({
            "repo": repo,
            "stories": stories,
            "rank": min(s["rank"] for s in stories),
            "activity": max(s["activity"] for s in stories),
            "attention": sum(1 for s in stories if s["rank"] == RANK_NEEDS_HUMAN),
        })
    projects.sort(key=lambda p: (p["rank"], -p["activity"], p["repo"]))
    return projects


def fetch_agent_statuses(surfaces: list[dict], base: str | None = None) -> dict:
    """Live agent state per session key, from review-surface's read-only
    /api/:key/agent-status (never consumes — safe on every render). One
    connection failure short-circuits the rest of the batch: if the surface
    server is down, eight sequential timeouts would stall the page render."""
    out = {}
    root = (base or SURFACE).rstrip("/")
    for sf in surfaces:
        key = str(sf.get("path") or "").rsplit("/", 1)[-1]
        if not key:
            continue
        try:
            with urllib.request.urlopen(f"{root}/api/{key}/agent-status",
                                        timeout=0.5) as resp:
                out[key] = json.loads(resp.read().decode())
        except Exception:
            break
    return out


def agent_state_badge(st: dict | None, now: float | None = None,
                      owned: bool | None = None) -> tuple[str, str]:
    """(label, css class) for a session's live agent state — the driver's
    question is 'did my answer land, is the agent on it, or did it stall',
    so the states are named from THEIR side of the loop.

    `owned` is the runner's side: False = nothing answers this page (see
    surface.unowned), True = a dialogue or the pipeline does, None = not
    known here. It separates "nobody will ever pick this up" from "the agent
    is between turns" — both used to read as "no agent listening"."""
    if not st:
        return ("no owner", "") if owned is False else ("", "")
    if st.get("status") == "ended":
        return ("ended", "")
    pending = int(st.get("pending_prompts") or 0)
    presence = st.get("presence")
    if pending and presence == "waiting":
        if owned is False:
            # the daemon adopts this state on its next tick
            return ("queued — no owner yet, adopting", "needs")
        if owned:
            # an owner exists and the runner delivers on the next outbox signal
            return ("queued — agent between turns", "running")
        # feedback is sitting in the queue and no agent poll is attached —
        # the one state that means "stalled", and the one worth alarming on
        return ("queued — no agent listening", "needs")
    if pending:
        return ("delivering to agent", "running")
    if presence == "working":
        # an unowned page can still have a loop outside the runner on it
        return ("agent working", "running")
    if owned is False:
        return ("no owner — attach from manage", "")
    if st.get("last_agent_reply_at"):
        return ("agent replied — your turn", "finished")
    return ("awaiting you", "")


def _iso_epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def stranded_badge(sf: dict) -> str:
    """The black-hole alarm: the daemon consumed this page's feedback and
    nothing routed it. Loud on purpose — it outranks every other state on the
    row, DECIDED included, because the driver's words are sitting in a file
    nobody reads. One link per dead-lettered batch, newest first."""
    names = list(sf.get("stranded") or [])
    if not names:
        return ""
    links = "".join(f'<a class="hist" href="/stranded/{escape(n)}">'
                    f'dead letter {i}</a>'
                    for i, n in zip(range(len(names), 0, -1), reversed(names)))
    return (f'<span class="badge needs">stranded — feedback reached no one'
            f'{f" ({len(names)})" if len(names) > 1 else ""}</span>{links}')


_DEAD_LETTER_NAME = re.compile(r"^[\w.-]+\.json$")


def read_dead_letter(name: str, stranded_dir: Path | None = None) -> dict | None:
    """One dead-lettered batch by file name, or None. The name is checked
    against a strict pattern — this is served on the tailnet and must never
    read outside the stranded directory."""
    if not _DEAD_LETTER_NAME.match(name or "") or name.startswith("."):
        return None
    d = Path(stranded_dir) if stranded_dir is not None else STRANDED_DIR
    try:
        return json.loads((d / name).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def render_dead_letter(name: str, rec: dict) -> str:
    """The stranded batch, verbatim: each prompt in full with what it was
    attached to, then the raw poll response — the thing the driver hand-routes
    from, so nothing is truncated."""
    items = []
    for p in rec.get("prompts") or []:
        if not isinstance(p, dict):
            p = {"prompt": str(p)}
        anchor = str(p.get("text") or "").strip()
        items.append(
            f'<li><span class="stage">{escape(str(p.get("tag") or "note"))}</span>'
            f'<span>{escape(str(p.get("prompt") or ""))}'
            f'{f"<br><span class=meta>on: {escape(anchor[:400])}</span>" if anchor else ""}'
            f'</span></li>')
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.get("at") or 0))
    sess = rec.get("session") or {}
    title = escape(str(sess.get("title") or Path(str(rec.get("artifact") or name)).name))
    back = '<p class="meta"><a href="/">← fleet</a>'
    if sess.get("path"):
        back += f' · <a href="{escape(view_href(sess["path"]))}">open surface</a>'
    back += "</p>"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title} — stranded feedback</title><style>{_CSS}</style></head>'
            f'<body><div class="wrap"><header><h1>{title}<span class="dot">.</span>'
            f' stranded feedback</h1></header>{back}'
            f'<section class="card"><h2>why it reached no one · {escape(when)}</h2>'
            f'<p>{escape(str(rec.get("reason") or ""))}</p>'
            f'<p class="meta">artifact {escape(str(rec.get("artifact") or ""))}<br>'
            f'dead letter {escape(str(STRANDED_DIR / name))} — delete or move it '
            f'once routed and the fleet badge clears</p></section>'
            f'<section class="card"><h2>the driver\'s words · {len(items)} item(s)</h2>'
            f'<ul class="agents">{"".join(items) or "<li>(no prompts parsed)</li>"}</ul></section>'
            f'<section class="card"><details><summary class="meta">raw poll response</summary>'
            f'<pre>{escape(str(rec.get("raw") or ""))}</pre></details></section>'
            f'</div></body></html>')


def journal_batches(artifact: str, state_dir: Path | None = None) -> list[dict]:
    """The per-surface answer history: every feedback batch the driver ever
    sent, from the feedback journal review-surface writes at accept time
    (append-only, survives delivery — this is the 'versions of my answers'
    record, already on disk). The journal lives beside review-surface's
    STATE file (one journal for all sessions), and each record carries the
    artifact path it belongs to — filter, don't glob."""
    if not artifact:
        return []
    out = []
    try:
        root = Path(state_dir) if state_dir is not None else Path(
            os.environ.get("REVIEW_SURFACE_STATE_DIR")
            or Path.home() / ".review-surface")
        journal = root / "feedback-journal.jsonl"
        for line in journal.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("file") == artifact:
                out.append(rec)
    except OSError:
        pass
    return out


def render_history(sf: dict, batches: list[dict]) -> str:
    rows = []
    for i, rec in enumerate(reversed(batches)):
        n = len(batches) - i
        when = escape(str(rec.get("at") or ""))
        items = []
        for p in rec.get("prompts") or []:
            text = str(p.get("prompt") or "")
            items.append(f'<li><span class="stage">{escape(str(p.get("tag") or "note"))}</span>'
                         f'<span>{escape(text[:400])}</span></li>')
        rows.append(f'<section class="card"><h2>round {n} · {when}'
                    f'{" · session ended" if rec.get("end_session") else ""}</h2>'
                    f'<ul class="agents">{"".join(items)}</ul></section>')
    title = escape(str(sf.get("title") or sf.get("task") or "surface"))
    back = '<p class="meta"><a href="/">← fleet</a>'
    if sf.get("path"):
        back += f' · <a href="{escape(view_href(sf["path"]))}">open surface</a>'
    back += "</p>"
    body = "".join(rows) or '<section class="card"><p class="empty">No feedback sent on this surface yet.</p></section>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title} — history</title><style>{_CSS}</style></head>'
            f'<body><div class="wrap"><header><h1>{title}<span class="dot">.</span> history</h1></header>'
            f'{back}{body}</div></body></html>')


def orphan_surfaces(snap: dict) -> list[dict]:
    """Open sessions with no story to live under (ad-hoc notices, asks from a
    story that has since been closed). They still want a human, so they render
    above the fleet rather than being dropped."""
    return [sf for sf in (snap.get("surfaces") or [])
            if sf.get("path") and not sf.get("project")]


def external_projects(snap: dict) -> list[dict]:
    """Registered sessions grouped by project — the driver's hierarchy of work
    that is NOT runner-spawned: orchestrator terminal sessions and the design
    discussions living under them. Orchestrators sort first inside a project;
    projects sort by most recent activity."""
    groups: dict[str, list[dict]] = {}
    for sf in (snap.get("surfaces") or []):
        if sf.get("project"):
            groups.setdefault(str(sf["project"]), []).append(sf)
    out = []
    for name, rows in groups.items():
        rows.sort(key=lambda r: (0 if r.get("role") == "orchestrator" else 1,
                                 -(r.get("opened") or 0)))
        out.append({"project": name, "rows": rows,
                    "activity": max((r.get("opened") or 0) for r in rows)})
    out.sort(key=lambda p: -p["activity"])
    return out


def load_projects(data_dir: Path | None = None) -> dict:
    return projects_mod.load(DATA_DIR if data_dir is None else data_dir)


# How long a board request may sit unclaimed before the fleet stops calling it
# queued. Generous against the runner's poll interval; a request still unheld
# after this is not waiting, it is lost.
STARTUP_GRACE = 300.0


def requested_tasks(data_dir) -> dict:
    """{task id: registry-shaped record} for task requests on the board.

    Carries `failed` when the action the request triggered failed: the board
    records the reason, and a request that could not start is the one case
    where the fleet can say what went wrong instead of only that nothing
    happened. One such failure sat unreported for eleven hours.

    Read-only and best-effort: the board is the daemon's file, the page server
    only looks. Anything unreadable (no board yet, a locked write, a schema
    that moved) is no requests, never an error page — a broken read here must
    not take down the fleet.
    """
    db = Path(data_dir) / "board.db"
    if not db.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT id, ts, payload FROM events WHERE kind='command' "
                "AND topic=? ORDER BY seq DESC LIMIT 200", (TASK_TOPIC,)).fetchall()
            failed = dict(conn.execute(
                "SELECT event_id, detail FROM firings WHERE outcome='failed'").fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    for eid, ts, payload in rows:
        try:
            p = json.loads(payload)
        except ValueError:
            continue
        if p.get("target") != TASK_REQUEST or not p.get("task"):
            continue
        out.setdefault(str(p["task"]), {
            "task": p["task"], "cwd": p.get("cwd") or "",
            "project": p.get("project") or "", "since": ts,
            "active_runs": {}, "failed": failed.get(eid, "")})
    return out


def starting_tasks(projs: dict, data_dir: Path | None = None) -> dict:
    """{project id: [tasks launched but with no page yet]}.

    A task becomes visible on the fleet only once its first round finishes and
    writes a page, which can be minutes. Until then pressing Launch changed
    nothing on screen, so the driver pressed it again — the report was four
    identical dialogues in one project. A launch has to leave a mark
    immediately.

    Two sources, because the registry alone is a step behind: submitting
    writes a request event to the BOARD, and the registry record only appears
    when the daemon picks it up on its next pass — the exact window the driver
    was staring at. So the board's unserved requests are read too, and the
    registry's record wins for any task that has one.
    """
    d = DATA_DIR if data_dir is None else data_dir
    try:
        reg = json.loads((Path(d) / "registry.json").read_text(encoding="utf-8"))
        tasks = dict(reg.get("tasks") or {})
    except (OSError, ValueError):
        tasks = {}
    # The registry's record wins for anything it knows, but the board's
    # failure reason is never in the registry — a request that failed to
    # start has no record there at all — so it is merged in either way.
    for task_id, rec in requested_tasks(d).items():
        held = tasks.get(task_id)
        tasks[task_id] = {**rec, **held} if held else rec
        if rec.get("failed"):
            tasks[task_id]["failed"] = rec["failed"]
    try:
        store = json.loads(
            (Path(d) / "surfaces" / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        store = {}
    # A task that has ever opened a session is drawn from the session store
    # like any other row; only the ones with nothing to show belong here.
    paged = {str((m or {}).get("task") or "") for m in store.values()
             if isinstance(m, dict)}
    out: dict[str, list] = {}
    for task_id, rec in tasks.items() if isinstance(tasks, dict) else ():
        if not isinstance(rec, dict) or task_id in paged:
            continue
        proj = rec.get("project") or ""
        if not proj:
            owner = projects_mod.for_dir(projs, rec.get("cwd") or "")
            proj = owner["id"] if owner else ""
        if not proj:
            continue
        out.setdefault(proj, []).append(
            {"task": task_id, "since": rec.get("since") or 0,
             "running": bool(rec.get("active_runs")),
             "ran": bool(rec.get("last_result")),
             # A session id is assigned before the process starts, so its
             # presence is the proof that something actually ran — which is
             # what separates "the runner never took this" from "it started
             # and died". Those two need different repairs.
             "started": bool(rec.get("session_id")),
             "failed": rec.get("failed") or ""})
    for rows in out.values():
        rows.sort(key=lambda r: -(r.get("since") or 0))
    return out


def live_turns(data_dir: Path | None = None, now: float | None = None) -> dict:
    """{task id: one clause saying what its running turn is doing}.

    Only tasks the registry says have a run in flight are looked at, so this
    never speaks about a turn that has finished. The clause comes from the
    session's own transcripts (runnerlib.activity) — nothing is asked of the
    agent, because a turn deep in a fan-out is exactly the turn that would
    forget to report.

    Empty for anything it cannot answer for. A task with no clause renders the
    way it always did, which is the point: this only ever adds detail.
    """
    d = DATA_DIR if data_dir is None else data_dir
    try:
        reg = json.loads((Path(d) / "registry.json").read_text(encoding="utf-8"))
        tasks = dict(reg.get("tasks") or {})
    except (OSError, ValueError):
        return {}
    out = {}
    for task_id, rec in tasks.items():
        if not isinstance(rec, dict) or not rec.get("active_runs"):
            continue
        # The run record carries the directory the turn is in, which is not
        # always the task's own cwd once a handoff has moved the work.
        run = next(iter(rec["active_runs"].values()), {})
        cwd = (run or {}).get("worktree") or rec.get("cwd") or ""
        sid = (run or {}).get("session_id") or rec.get("session_id") or ""
        if not cwd or not sid:
            continue
        said = activity_mod.phrase(activity_mod.snapshot(cwd, sid, now), now)
        if said:
            out[task_id] = said
    return out


def closed_pages(projs: dict, data_dir: Path | None = None) -> dict:
    """{project id: [closed page sessions, newest first]} from the runner's
    session store — pages whose session ended stay on record there with
    open False. A page files under a project by its recorded project, else
    by the project owning its working directory."""
    d = DATA_DIR if data_dir is None else data_dir
    try:
        store = json.loads((Path(d) / "surfaces" / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    by_name = {n: pid for pid in projs for n in projects_mod.names_of(projs, pid)}
    out: dict[str, list] = {}
    for artifact, meta in (store.items() if isinstance(store, dict) else ()):
        if not isinstance(meta, dict) or meta.get("open") or not meta.get("key"):
            continue
        pid = by_name.get(str(meta.get("project") or ""))
        if pid is None and not meta.get("project"):
            owner = projects_mod.for_dir(projs, meta.get("cwd"))
            pid = owner["id"] if owner else None
        if pid:
            out.setdefault(pid, []).append({**meta, "artifact": artifact})
    for rows in out.values():
        rows.sort(key=lambda m: -(m.get("opened") or 0))
    return out


# -------------------------------------------------------------------- render

_CSS = """
/* One design system: these tokens are surface-theme.css's, verbatim, so the
   fleet and every surface it opens read as one product. Dark, committed —
   the surfaces have no light mode, so a light fleet is a theme break. */
:root{--bg:#0f1115;--fg:#e8e6e1;--muted:#9aa4b2;--label:#8c96aa;
 --card:#12161e;--card-2:#171c26;--border:#2a2e36;
 --ok:#8fe0a8;--run:#e6c07b;--hot:#ff8f8f;--accent:#8fc7ff;--soft:#ffffff08;
 --serif:"Iowan Old Style",Georgia,serif;
 --sans:ui-sans-serif,system-ui,"Segoe UI",sans-serif;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:52rem;margin:0 auto;padding:1.6rem 1.2rem 3rem}
header{display:flex;align-items:baseline;gap:.8rem;margin-bottom:1.1rem}
h1{font-family:var(--serif);font-weight:600;font-size:1.55rem;margin:0;
 letter-spacing:.2px}h1 .dot{color:var(--accent)}
header .updated{margin-left:auto;color:var(--label);font-size:.74rem;
 font-family:var(--mono)}
header a.costs{color:var(--accent);font-size:.74rem;font-family:var(--mono);
 text-decoration:none}header a.costs:hover{text-decoration:underline}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;
 padding:.9rem 1.1rem;margin-bottom:.9rem}
.card h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;
 color:var(--label);margin:0 0 .6rem;font-family:var(--mono);font-weight:650}
form.newtask textarea{width:100%;min-height:4.5rem;resize:vertical;padding:.6rem .7rem;
 border:1px solid var(--border);border-radius:9px;background:var(--bg);color:var(--fg);font:inherit}
form.newtask .row{display:flex;gap:.6rem;align-items:center;margin-top:.6rem;flex-wrap:wrap}
form.newtask input[type=text]{flex:1 1 18rem;padding:.45rem .6rem;border:1px solid var(--border);
 border-radius:9px;background:var(--bg);color:var(--fg);font:inherit;
 font-family:ui-monospace,Menlo,monospace;font-size:.82rem}
form.newtask button{font:inherit;font-weight:650;border:0;border-radius:9px;
 padding:.5rem 1.1rem;background:var(--accent);color:#0f1115;cursor:pointer}
.project{margin-bottom:1rem}
.project > h2{display:flex;align-items:baseline;gap:.6rem}
.project .repo{font-family:var(--mono);text-transform:none;
 letter-spacing:0;font-size:.92rem;color:var(--fg);font-weight:650}
.project .orch{font-size:.78rem;color:var(--muted);margin:-.35rem 0 .55rem;
 font-style:italic}
.project .orch b{color:var(--fg);font-weight:600;font-style:normal}
.story{padding:.55rem 0}
.story + .story, .rowline + .rowline, .story + .rowline, .rowline + .story{
 border-top:1px solid var(--border)}
.rowline{display:flex;align-items:center;gap:.2rem}
.rowline > a.row{flex:1;min-width:0}
.hist{font-size:.74rem;color:var(--label);font-family:var(--mono);
 text-decoration:none;white-space:nowrap;padding:.2rem .4rem;border-radius:6px}
.hist:hover{color:var(--accent);background:var(--soft)}
.rowmenu{position:relative}
.rowmenu > summary{list-style:none;cursor:pointer;font-size:1rem;line-height:1}
.rowmenu > summary::-webkit-details-marker{display:none}
.rowmenu .menu{position:absolute;right:0;top:100%;z-index:5;min-width:9rem;
 background:var(--card);border:1px solid var(--border);border-radius:9px;
 padding:.25rem;box-shadow:0 6px 20px rgba(0,0,0,.35)}
.rowmenu .menu form{margin:0}
.rowmenu .menu button, .rowmenu .menu a{display:flex;align-items:center;gap:.45rem;
 width:100%;padding:.4rem .6rem;border:0;border-radius:6px;background:none;
 color:var(--fg);font:inherit;font-size:.82rem;text-decoration:none;cursor:pointer;
 text-align:left}
.rowmenu .menu button:hover, .rowmenu .menu a:hover{background:var(--soft);color:var(--accent)}
.meta{color:var(--label);font-family:var(--mono);font-size:.78rem}
.meta a{color:var(--accent);text-decoration:none}
.story .line{display:flex;flex-wrap:wrap;align-items:baseline;gap:.4rem .7rem}
.story .id{font-weight:650}
.story .title{color:var(--muted);font-size:.86rem;flex:1 1 12rem;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
a.row{display:flex;align-items:baseline;gap:.4rem .7rem;padding:.6rem .5rem;
 margin:0 -.5rem;border-radius:8px;text-decoration:none;color:inherit;
 transition:background .15s ease}
a.row:hover{background:var(--soft)}
a.row .title{flex:1 1 12rem;color:var(--fg);font-size:.9rem;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
a.row .go{color:var(--accent);font-size:.78rem;font-family:var(--mono);white-space:nowrap}
@media (prefers-reduced-motion:reduce){a.row{transition:none}}
/* A launched task with no page yet: same shape as a row so the section reads
   as one list, but not a link — there is nothing to open. */
span.row.starting{display:flex;align-items:baseline;gap:.4rem .7rem;
 padding:.6rem .5rem;margin:0 -.5rem;border-radius:8px;flex:1;min-width:0;
 border:1px dashed var(--border);color:var(--muted)}
span.row.starting .title{flex:1 1 12rem;color:var(--fg);font-size:.9rem;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{font-size:.72rem;padding:.1rem .55rem;border-radius:99px;border:1px solid var(--border);
 color:var(--muted);white-space:nowrap}
.badge.needs{border-color:var(--hot);color:var(--hot)}
.badge.finished{border-color:var(--ok);color:var(--ok)}
.badge.running{border-color:var(--run);color:var(--run)}
.agents{margin:.35rem 0 0;padding:0;list-style:none;font-size:.82rem}
.agents li{display:flex;flex-wrap:wrap;gap:.5rem;padding:.15rem 0;color:var(--muted)}
.agents .stage{font-family:var(--mono);color:var(--fg)}
.links a{font-size:.78rem;color:var(--accent);text-decoration:none;margin-right:.6rem}
.links a:hover{text-decoration:underline}
.empty{color:var(--muted);font-size:.88rem}
.dirwrap{position:relative;display:block}
.dirwrap .lbl{display:block;font-size:.78rem;color:var(--muted);margin:0 0 .2rem}
#dir1{width:100%;font-family:ui-monospace,monospace;font-size:.88rem;
 padding:.45rem .6rem .45rem 1.7rem;border:1px solid var(--line);border-radius:6px;
 background:var(--card) url("data:image/svg+xml;charset=utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='%238a94a6'%3E%3Cpath d='M1.5 3.5a1 1 0 011-1h3.2l1.3 1.4h6.5a1 1 0 011 1v6.6a1 1 0 01-1 1h-11a1 1 0 01-1-1z'/%3E%3C/svg%3E") no-repeat .5rem 50%/ .9rem .9rem}
#dir1:focus{outline:2px solid var(--accent,#4c8dff);outline-offset:-1px}
/* Floats over the page: the list is a transient overlay, not a block that
   pushes the rest of the form down every time it opens. */
.sug{position:absolute;z-index:40;top:100%;left:0;right:0;margin:.15rem 0 0;padding:0;
 list-style:none;max-height:14rem;overflow-y:auto;border:1px solid var(--line);
 border-radius:6px;background:var(--card);box-shadow:0 8px 24px rgba(0,0,0,.35)}
.sug[hidden]{display:none}
.sug li{padding:.35rem .6rem;font-family:ui-monospace,monospace;font-size:.85rem;
 cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sug li:hover,.sug li.on{background:var(--line)}
.sug li.none{color:var(--muted);cursor:default;font-style:italic}
/* The library picker. Two lines per row — what to type, and what it is — so
   the description is readable without being the thing that wraps. */
.sug li b{display:block;color:var(--fg);font-weight:600}
.sug li .hint{display:block;font-family:var(--sans,inherit);font-size:.76rem;
 color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sug li:has(b){white-space:normal}
details.pick{margin-top:.6rem}
details.pick > summary{font-size:.8rem;color:var(--muted);cursor:pointer;
 padding:.25rem 0;list-style:revert}
details.pick > summary:hover{color:var(--fg)}
details.pick .dirwrap{margin-top:.4rem}
#runq{width:100%;font-family:ui-monospace,monospace;font-size:.88rem;
 padding:.45rem .6rem;border:1px solid var(--line);border-radius:6px;
 background:var(--card);color:var(--fg)}
#runq:focus{outline:2px solid var(--accent,#4c8dff);outline-offset:-1px}
.linky{background:none;border:0;padding:0;color:var(--accent);cursor:pointer;
 font:inherit;text-decoration:underline}
.story.dispatch{cursor:pointer;border-radius:8px;padding:.55rem .5rem;margin:0 -.5rem;
 transition:background .15s ease}
.story.dispatch:hover,.story.dispatch:focus-visible{background:var(--soft)}
.story.dispatch .go{color:var(--accent);font-size:.78rem;font-family:var(--mono);
 margin-left:auto}
details.fold{margin-top:.35rem}
details.fold summary{cursor:pointer;color:var(--label);font-family:var(--mono);
 font-size:.74rem;padding:.35rem 0;list-style:none}
details.fold summary::before{content:'▸ ';color:var(--ok)}
details.fold[open] summary::before{content:'▾ '}
details.fold summary:hover{color:var(--fg)}
#filter{width:100%;padding:.5rem .7rem;margin-bottom:.9rem;
 border:1px solid var(--border);border-radius:9px;background:var(--card);
 color:var(--fg);font:inherit;font-size:.88rem}
#filter::placeholder{color:var(--label)}
footer{color:var(--label);font-size:.74rem;font-family:var(--mono);margin-top:1.4rem}
footer a{color:var(--accent)}
.live{font-family:var(--mono);font-size:.74rem;color:var(--label)}
.live.on{color:var(--ok)}.live.half{color:var(--run)}.live.off{color:var(--hot)}
.story.dispatch.picked{background:var(--soft);box-shadow:inset 3px 0 0 var(--accent)}
.story.dispatch.picked .go{color:var(--ok)}
form.newtask .target{margin:.55rem 0 0;padding:.45rem .7rem;border-radius:9px;
 border:1px solid var(--accent);color:var(--fg);font-size:.84rem;background:var(--soft)}
form.newtask .target code{font-family:var(--mono);font-size:.78rem;color:var(--muted)}
form.newtask .target .clear{background:none;border:0;color:var(--accent);padding:0 .2rem;
 font:inherit;font-size:.78rem;cursor:pointer;font-weight:400}
form.newtask input[name=cwd]:not(:placeholder-shown){border-color:var(--accent)}
@keyframes flash{from{box-shadow:0 0 0 2px var(--accent)}to{box-shadow:0 0 0 0 transparent}}
.card.flash{animation:flash 1.4s ease-out}
@media (prefers-reduced-motion:reduce){.card.flash{animation:none;border-color:var(--accent)}}
#found{margin-top:-.4rem}
.chip{font-family:var(--mono);font-size:.68rem;padding:.05rem .45rem;border-radius:5px;
 border:1px solid var(--border);color:var(--label);white-space:nowrap}
.chip.open{border-color:var(--ok);color:var(--ok)}
.chip.replaced{border-color:var(--run);color:var(--run)}
.repair{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin:.5rem 0}
.repair input,.repair select{flex:1 1 14rem;padding:.4rem .6rem;border:1px solid var(--border);
 border-radius:9px;background:var(--bg);color:var(--fg);font:inherit;font-size:.84rem}
.repair button{font:inherit;font-size:.84rem;font-weight:650;border:1px solid var(--accent);
 border-radius:9px;padding:.4rem .9rem;background:none;color:var(--accent);cursor:pointer}
.repair button.danger{border-color:var(--hot);color:var(--hot)}
.repair button:disabled{opacity:.4;cursor:default}
.repair p{margin:0;flex-basis:100%;color:var(--muted);font-size:.8rem}
.project h2 a.repo{color:var(--fg);text-decoration:none}
.project h2 a.repo:hover{color:var(--accent)}
.project .member{margin:.4rem 0 0 .2rem;padding-left:.8rem;border-left:2px solid var(--border)}
.backend{margin:0 0 1.4rem;padding-left:.9rem;border-left:3px solid var(--border)}
.backend.remote{border-left-color:var(--accent)}
nav.machines{display:flex;flex-wrap:wrap;gap:.4rem 1.2rem;padding:.6rem 1.1rem}
nav.machines a{color:var(--fg);text-decoration:none;font-size:.85rem}
nav.machines a:hover b{text-decoration:underline}
.machine{margin:0 0 .6rem;font:650 .72rem var(--mono);text-transform:uppercase;
 letter-spacing:.09em;color:var(--label)}.machine b{color:var(--fg)}
.backend.remote .machine b{color:var(--accent)}
.machine a{color:var(--accent);text-decoration:none}.machine a:hover{text-decoration:underline}
.project .member h3{font-size:.8rem;margin:.2rem 0;font-family:var(--mono);font-weight:600}
.project .member h3 a{color:var(--muted);text-decoration:none}
form.newtask select,form.newtask input[name=name],form.newtask input[name=context]{
 padding:.45rem .6rem;border:1px solid var(--border);border-radius:9px;
 background:var(--bg);color:var(--fg);font:inherit;font-size:.84rem}
pre.brief{white-space:pre-wrap;font-family:var(--mono);font-size:.78rem;color:var(--muted);
 max-height:18rem;overflow:auto;margin:.3rem 0}
table.lib{width:100%;border-collapse:collapse;font-size:.82rem}
table.lib td{padding:.3rem .4rem;border-top:1px solid var(--border);vertical-align:top}
table.lib td.inv{font-family:var(--mono);white-space:nowrap;color:var(--fg)}
table.lib td.scope{font-family:var(--mono);color:var(--label);font-size:.74rem}
.card.recent h2{display:flex;align-items:baseline;gap:.8rem}
.card.recent h2 .hist{margin-left:auto;text-transform:none;letter-spacing:0}
table.lib td.desc{color:var(--muted)}
"""

# Fuzzy find over the installed library. Subsequence matching, the way a
# terminal fuzzy finder works: "revsur" finds "review-surface". Scored so that
# a match starting at a word beginning beats one buried mid-word, and a short
# name beats a long one that happens to contain the same letters — without
# that, every query matched half of 111 descriptions and the list was noise.
_PICKER_JS = """
(function(){
 var q=document.getElementById("runq"),box=document.getElementById("runsug"),
     val=document.getElementById("runval"),src=document.getElementById("runlib"),
     clear=document.getElementById("runclear");
 if(!q||!box||!val||!src)return;
 var lib=[];try{lib=JSON.parse(src.textContent)}catch(e){return}
 var items=[],cur=-1;
 function hide(){box.hidden=true;q.setAttribute("aria-expanded","false");cur=-1}
 function show(){box.hidden=false;q.setAttribute("aria-expanded","true")}
 // Score one candidate, or -1 for no match. Walks the needle through the
 // haystack once; a hit right after a separator counts double, and adjacent
 // hits count double, which is what makes "revsur" rank review-surface over
 // a description that merely contains r,e,v,s,u,r somewhere.
 function score(hay,needle){
  var h=hay.toLowerCase(),n=needle.toLowerCase(),i=0,s=0,last=-2;
  for(var j=0;j<n.length;j++){
   var c=n[j],at=h.indexOf(c,i);
   if(at<0)return -1;
   var bonus=1;
   if(at===last+1)bonus+=2;
   if(at===0||"-_/: .".indexOf(h[at-1])>=0)bonus+=2;
   s+=bonus;last=at;i=at+1;}
  return s-h.length*0.01;
 }
 function draw(){
  box.innerHTML="";cur=-1;
  if(!items.length){box.innerHTML="<li class=\\"none\\">nothing installed matches that</li>";show();return}
  items.forEach(function(e,n){
   var li=document.createElement("li");li.setAttribute("role","option");
   var b=document.createElement("b");b.textContent=e.i;
   var s=document.createElement("span");s.className="hint";s.textContent=e.d;
   li.appendChild(b);li.appendChild(s);
   li.addEventListener("mousedown",function(ev){ev.preventDefault();pick(n)});
   box.appendChild(li)});
  show();
 }
 function pick(n){
  if(n<0||n>=items.length)return;
  var e=items[n];val.value=e.v;q.value=e.i;hide();
  var sum=q.closest("details");
  if(sum&&sum.querySelector("summary"))sum.querySelector("summary").textContent="run "+e.i;
 }
 function mark(){Array.prototype.forEach.call(box.children,function(li,n){
  li.className=(n===cur?"on":"")})}
 // Names first, descriptions only as a fallback, and then only as a plain
 // substring. Fuzzy-matching 111 descriptions put "/arboreus-teach" at the
 // top for "revsur": with that much prose to walk, every query matches
 // something, and a list that always answers is a list you stop trusting.
 function find(){
  var v=q.value.trim();
  if(!v){hide();return}
  items=lib.map(function(e){return {e:e,s:score(e.i,v)}})
   .filter(function(r){return r.s>0})
   .sort(function(a,b){return b.s-a.s})
   .slice(0,12).map(function(r){return r.e});
  if(!items.length){
   var n=v.toLowerCase();
   items=lib.filter(function(e){return e.d.toLowerCase().indexOf(n)>=0}).slice(0,12);
  }
  draw();
 }
 q.addEventListener("input",find);
 q.addEventListener("focus",find);
 q.addEventListener("blur",function(){setTimeout(hide,120)});
 q.addEventListener("keydown",function(e){
  if(e.key==="Escape"){hide();return}
  if(box.hidden||!items.length)return;
  if(e.key==="ArrowDown"){e.preventDefault();cur=(cur+1)%items.length;mark()}
  else if(e.key==="ArrowUp"){e.preventDefault();cur=(cur<=0?items.length:cur)-1;mark()}
  else if(e.key==="Enter"){e.preventDefault();pick(cur<0?0:cur)}
  else if(e.key==="Tab"&&items.length===1){e.preventDefault();pick(0)}});
 if(clear)clear.addEventListener("click",function(){
  val.value="";q.value="";hide();
  var d=q.closest("details");
  if(d&&d.querySelector("summary"))
   d.querySelector("summary").textContent=
    "the session picks its own skill — click to choose one yourself"});
})();
"""

_PAGE_JS = """
(function(){
 var FALLBACK=%d;
 function esc(t){return String(t==null?'':t).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
 function applyFilter(){
  var f=document.getElementById('filter');
  var q=(f&&f.value||'').toLowerCase();
  document.querySelectorAll('#fleet section.card').forEach(function(sec){
   var any=false, rows=sec.querySelectorAll('.rowline,.story');
   rows.forEach(function(r){
    var hit=!q||r.textContent.toLowerCase().indexOf(q)>-1;
    r.style.display=hit?'':'none'; if(hit)any=true;
   });
   sec.style.display=(!q||any||!rows.length)?'':'none';
  });
  // a match hidden inside a closed fold is a match the driver can't see
  if(q)document.querySelectorAll('#fleet details.fold').forEach(function(d){d.open=true});
 }
 // Find across everything: the row filter above only sees what is listed;
 // this asks the server about every page ever opened, folded rounds included.
 var findT=null;
 function findEverywhere(){
  var f=document.getElementById('filter'),box=document.getElementById('found');
  if(!f||!box)return;
  var q=f.value.trim();
  if(q.length<2){box.hidden=true;box.innerHTML='';return;}
  fetch('/find?q='+encodeURIComponent(q),{cache:'no-store'}).then(function(r){return r.text()})
   .then(function(h){if(f.value.trim()!==q)return;box.innerHTML=h;box.hidden=false;})
   .catch(function(){});
 }
 var f=document.getElementById('filter');
 if(f)f.addEventListener('input',function(){applyFilter();clearTimeout(findT);findT=setTimeout(findEverywhere,200);});
 // Dispatch-target rows: click (or Enter) points the task box at that
 // checkout. It must LOOK like it did something: the row stays marked, the
 // form says where the task will run, and the form flashes into view.
 function markPicked(){
  var cwd=document.querySelector('form.newtask input[name=cwd]');
  var v=cwd?cwd.value:'';
  document.querySelectorAll('.dispatch[data-cwd]').forEach(function(r){
   var on=!!v&&r.dataset.cwd===v;
   r.classList.toggle('picked',on);r.setAttribute('aria-pressed',on?'true':'false');
   var go=r.querySelector('.go');if(go)go.textContent=on?'selected ✓':'new task here →';
  });
 }
 function showTarget(){
  var form=document.querySelector('form.newtask');if(!form)return;
  var cwd=form.querySelector('input[name=cwd]'),note=form.querySelector('.target');
  var txt=form.querySelector('textarea'),v=cwd?cwd.value.trim():'';
  var picked=document.querySelector('.dispatch.picked');
  var name=picked?(picked.dataset.name||v):v;
  if(note){
   note.hidden=!v;
   note.innerHTML=v?'New task will run in <b>'+esc(name)+'</b> <code>'+esc(v)+
    '</code> <button type=button class=clear>clear</button>':'';
  }
  if(txt)txt.placeholder=v?'What should the fleet do in '+name+'?':'What should the fleet do?';
 }
 function dispatchTo(el){
  var form=document.querySelector('form.newtask');if(!form)return;
  var cwd=form.querySelector('input[name=cwd]'),txt=form.querySelector('textarea');
  if(cwd)cwd.value=el.dataset.cwd||'';
  markPicked();showTarget();
  var card=form.closest('.card');
  if(card){card.classList.remove('flash');void card.offsetWidth;card.classList.add('flash');}
  form.scrollIntoView({behavior:'smooth',block:'center'});
  if(txt)txt.focus({preventScroll:true});
 }
 document.addEventListener('click',function(e){
  if(e.target.closest&&e.target.closest('form.newtask .clear')){
   var c=document.querySelector('form.newtask input[name=cwd]');if(c)c.value='';
   markPicked();showTarget();return;
  }
  var r=e.target.closest&&e.target.closest('.dispatch[data-cwd]');
  if(r)dispatchTo(r);
 });
 document.addEventListener('keydown',function(e){
  if(e.key!=='Enter'&&e.key!==' ')return;
  var r=e.target.closest&&e.target.closest('.dispatch[data-cwd]');
  if(r){e.preventDefault();dispatchTo(r);}
 });
 var cwdIn=document.querySelector('form.newtask input[name=cwd]');
 if(cwdIn)cwdIn.addEventListener('input',function(){markPicked();showTarget();});
 // Live: the fleet stream says when the runner's snapshot, a review page or
 // a link changed; only then is the fleet fragment re-fetched.
 function refresh(){
  fetch('/?partial=1',{cache:'no-store'}).then(function(r){return r.text()})
   .then(function(h){
    var el=document.getElementById('fleet');if(!el)return;
    var open={};  // fold state is DOM state — carry it across the swap
    el.querySelectorAll('details.fold[open]').forEach(function(d){open[d.dataset.fold]=1});
    el.innerHTML=h;
    el.querySelectorAll('details.fold').forEach(function(d){if(open[d.dataset.fold])d.open=true});
    applyFilter();markPicked();
   })
   .catch(function(){});
 }
 var soonT=null;function soon(){clearTimeout(soonT);soonT=setTimeout(refresh,250);}
 var live=document.getElementById('live'),poll=null;
 function setLive(ok,upstream){
  if(!live)return;
  live.className='live '+(ok?(upstream?'on':'half'):'off');
  live.textContent=ok?'● live':'○ reconnecting';
  live.title=ok?(upstream?'Every machine is answering; updates within a couple of seconds'
   :'A machine is not answering, or cannot see its own review pages change; refreshes may lag')
   :'Stream lost; refreshing every '+(FALLBACK/1000)+' s until it reconnects';
 }
 function startPoll(){if(!poll)poll=setInterval(refresh,FALLBACK);}
 function stopPoll(){if(poll){clearInterval(poll);poll=null;}}
 if(!window.EventSource){setLive(false);startPoll();}
 else{
  var es=new EventSource('%s');
  es.addEventListener('hello',function(e){
   var d={};try{d=JSON.parse(e.data)}catch(_){}
   setLive(true,d.upstream);stopPoll();soon();   // catch up on anything missed while away
  });
  es.addEventListener('source',function(e){var d={};try{d=JSON.parse(e.data)}catch(_){}setLive(true,d.upstream);});
  es.addEventListener('change',soon);
  es.onerror=function(){setLive(false);startPoll();};
 }
 markPicked();showTarget();
})();
""" % (FALLBACK_POLL_MS, STREAM_PATH)


def _rel_time(ts: float, now: float | None = None) -> str:
    if not ts:
        return ""
    delta = max(0, int((now if now is not None else time.time()) - ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _links_html(view: dict) -> str:
    bits = []
    for sf in view["surfaces"]:
        label = sf["kind"].replace("_", " ")
        bits.append(f'<a href="{escape(view_href(sf["path"]))}">surface: {escape(label)}</a>')
        for name in sf.get("stranded") or []:
            bits.append(f'<a href="/stranded/{escape(name)}">stranded feedback</a>')
    for pr in view["prs"]:
        if pr["url"]:
            bits.append(f'<a href="{escape(pr["url"])}" rel="noreferrer">PR #{pr["pr"]}</a>')
        else:
            bits.append(f'<span>PR #{pr["pr"]}</span>')
    return f'<div class="links">{"".join(bits)}</div>' if bits else ""


def _story_html(view: dict, now: float) -> str:
    cls = {RANK_NEEDS_HUMAN: "needs", RANK_FINISHED: "finished",
           RANK_RUNNING: "running"}[view["rank"]]
    agents = []
    for r in view["runs"]:
        where = " ".join(x for x in [r.get("slice"),
                                     f'PR #{r["pr"]}' if r.get("pr") else ""] if x)
        agents.append(
            f'<li><span class="stage">{escape(str(r["stage"]))}</span>'
            f'<span>{escape(where)}</span>'
            f'<span>{escape(_rel_time(r.get("started") or 0, now))}</span></li>')
    for q in view["questions"]:
        agents.append(f'<li><span class="stage">question</span>'
                      f'<span>{escape(str(q.get("question") or ""))[:160]}</span></li>')
    agent_html = f'<ul class="agents">{"".join(agents)}</ul>' if agents else ""
    return (
        f'<div class="story">'
        f'<div class="line">'
        f'<span class="id">{escape(view["story_id"])}</span>'
        f'<span class="title">{escape(view["title"])}</span>'
        f'<span class="badge {cls}">{escape(view["why"])}</span>'
        f'<span class="badge">{escape(view["phase"])}</span>'
        f'<span class="badge">{escape(_rel_time(view["activity"], now))}</span>'
        f'</div>{agent_html}{_links_html(view)}</div>')


def load_graph() -> tuple[list, dict, dict]:
    """(link records, holders, pages) from disk, for the live page. Tests
    pass their own; every read here degrades to empty, never to an error."""
    records, holders = conv_mod.load_links(conv_mod.link_files(PANEL_LINKS))
    return records, holders, conv_mod.load_pages()


def render_fleet(snap: dict, now: float | None = None,
                 statuses: dict | None = None, graph: tuple | None = None,
                 projs: dict | None = None, finished: dict | None = None,
                 starting: dict | None = None, doing: dict | None = None,
                 spent: dict | None = None, histories: dict | None = None,
                 own=None, returned: dict | None = None) -> str:
    """The hierarchy fragment — also what the stream-triggered refresh swaps
    in, so the page and the refresh can never render two different shapes.
    `statuses` is the live agent-state map from fetch_agent_statuses; None
    renders without live badges (tests, surface server down). `graph` is
    load_graph()'s (links, holders, pages): with it, a conversation's rounds
    fold into one row; without it, pages still fold by their dialogue task.
    `projs` is the durable project store (projects.load) and `finished` the
    count of closed pages per project id; every live project gets a section
    even with nothing open in it. `starting` is starting_tasks(): work that
    has been launched but has no page yet, which must still be visible or a
    launch looks like it did nothing and gets repeated. `doing` is
    live_turns(): what each in-flight turn is actually doing, which replaces a
    bare "working" badge wherever it has something to say. `spent` is
    costs_mod.per_task(): dollars each task's finished turns have cost, shown
    on its row. `histories` is how many answer batches each page's artifact
    has (artifact -> count) when the data came from a backend's API; None
    reads this machine's journal. `own` is the fleet page's code stamp for the
    stale-code line, per code_line. `returned` is recent_mod.last_returned():
    when each task's newest turn ended, which is how new the work is — the
    page server's own timestamp also moves when the driver merely opens the
    page."""
    now = time.time() if now is None else now
    statuses = statuses or {}
    doing = doing or {}
    spent = spent or {}
    returned = returned or {}
    records, holders, pages = graph or ([], {}, {})
    snap = {**snap, "surfaces": conv_mod.collapse(snap.get("surfaces") or [],
                                                  records, holders, pages)}
    projects = fleet(snap)
    out = []

    def _live(sf) -> str:
        strand = stranded_badge(sf)
        # Interim lifecycle marker until the HITL store carries real state
        # (d7): a surface registered with DECIDED in its title is settled —
        # presence churn on it must not read as "your turn".
        if "DECIDED" in str(sf.get("title") or ""):
            return strand or '<span class="badge finished">decided — nothing needs you</span>'
        key = str(sf.get("path") or "").rsplit("/", 1)[-1]
        st = statuses.get(key)
        # upstream-only pages ("page" rows) have no runner session to own them
        owned = (None if sf.get("kind") in (None, "page") or not sf.get("artifact")
                 else not surface_mod.unowned(sf))
        label, cls = agent_state_badge(st, now, owned=owned)
        # "agent working" is true and says nothing. When the transcripts can
        # say what the turn is doing, that replaces it — a row that has been
        # quiet for half an hour must be able to prove it is alive.
        said = doing.get(str(sf.get("task") or ""))
        if said and cls == "running":
            label = said
        bits = strand
        if label:
            bits += f'<span class="badge {cls}">{escape(label)}</span>'
        # Two clocks, never one: "returned" is when the agent last delivered,
        # "touched" when anything last handled the page — opening it counts —
        # so a page the driver just looked at no longer reads as new work.
        back = returned.get(str(sf.get("task") or ""))
        if back:
            bits += (f'<span class="badge" title="when the agent\'s newest turn on this '
                     f'task ended">returned {escape(_rel_time(back, now))}</span>')
        upd = _iso_epoch((st or {}).get("updated_at"))
        if upd:
            bits += (f'<span class="badge" title="when anything last touched this page — '
                     f'you opening it, your feedback, or the agent">touched '
                     f'{escape(_rel_time(upd, now))}</span>')
        return bits

    def _spent(sf) -> str:
        usd = spent.get(str(sf.get("task") or ""))
        return (f'<span class="badge" title="what this task\'s finished turns cost">'
                f'{escape(costs_mod.usd(usd))}</span>' if usd else "")

    def _history_link(sf) -> str:
        art = str(sf.get("artifact") or "")
        n = (int(histories.get(art) or 0) if histories is not None
             else len(journal_batches(art)))
        if not n:
            return ""
        key = str(sf.get("path") or "").rsplit("/", 1)[-1]
        return f'<a class="hist" href="/history/{escape(key)}">history ({n})</a>'

    def _page_link(sf) -> str:
        # Earlier rounds are reachable from the row, never listed in the scan.
        # The ⋮ menu is the row's manage control: Archive (one click, back to
        # the fleet). The rarer repairs (move, mark replaced, attach) stay on
        # /page/<key>, reached from the earlier-rounds link and from find.
        key = escape(str(sf.get("path") or "").rsplit("/", 1)[-1])
        n = len(sf.get("earlier") or [])
        rounds = (f'<a class="hist" href="/page/{key}">'
                  f'{n} earlier round{"s" if n != 1 else ""}</a>') if n else ""
        return (f'{rounds}<details class="rowmenu"><summary class="hist" title="More" '
                f'aria-label="More actions">⋮</summary><div class="menu">'
                f'<form method="post" action="{REPAIR_PATH}">'
                f'<input type="hidden" name="key" value="{key}">'
                '<input type="hidden" name="action" value="archive">'
                '<input type="hidden" name="back" value="home">'
                f'<button type="submit">{ARCHIVE_ICON}Archive</button></form>'
                '</div></details>')

    def _opened(sf) -> tuple[str, str]:
        """(badge, hover) for when the page was opened. Once the row says
        when the agent returned, a third bare time crowded the title out, so
        it moves to the hover text."""
        ago = escape(_rel_time(sf.get("opened") or 0, now))
        if returned.get(str(sf.get("task") or "")):
            return "", f' title="page opened {ago}"' if ago else ""
        return f'<span class="badge">{ago}</span>', ""

    def _group_rows(ext_rows):
        """(byline, live rows, settled rows, row count) for one project's
        registered sessions."""
        # An orchestrator is the project's parent, not a sibling of the pages
        # under it: it renders as the header's byline. Only page-less
        # orchestrators lift — one WITH a page is still a row you can open.
        orch = [sf for sf in ext_rows
                if sf.get("role") == "orchestrator" and not sf.get("path")]
        rows_src = [sf for sf in ext_rows if sf not in orch]
        byline = "".join(
            f'<p class="orch">orchestrated by <b>{escape(str(sf.get("title") or ""))}</b>'
            f' · {escape(_rel_time(sf.get("opened") or 0, now))}</p>' for sf in orch)
        rows, decided = [], []
        for sf in rows_src:
            if conv_mod.key_of(sf) in _ARCHIVED:
                continue
            role = (f'<span class="badge">{escape(str(sf["role"]))}</span>'
                    if sf.get("role") else "")
            when = f'<span class="badge">{escape(_rel_time(sf.get("opened") or 0, now))}</span>'
            title = escape(str(sf.get("title") or sf.get("kind") or ""))
            if sf.get("path"):
                # the whole row is the click target — a 12px "open surface"
                # link under each row made every open a precision task
                badge, hover = _opened(sf)
                row = (f'<div class="rowline">'
                       f'<a class="row"{hover} href="{escape(view_href(sf["path"]))}">'
                       f'<span class="title">{title}</span>{_live(sf)}{role}{badge}'
                       f'{_spent(sf)}<span class="go">open →</span></a>'
                       f'{_history_link(sf)}{_page_link(sf)}</div>')
            elif sf.get("cwd"):
                # A dispatch target: clicking prefills the task box's cwd.
                # A row that shows up as a "session" but responds to nothing
                # reads as broken — every pathless row needs a reason to exist.
                row = (f'<div class="story dispatch" role="button" tabindex="0" '
                       f'aria-pressed="false" data-cwd="{escape(str(sf["cwd"]))}" '
                       f'data-name="{title}"><div class="line">'
                       f'<span class="title">{title}</span>{role}{when}'
                       f'<span class="go">new task here →</span>'
                       f'</div></div>')
            else:
                row = (f'<div class="story"><div class="line">'
                       f'<span class="title">{title}</span>{role}{when}'
                       f'</div></div>')
            # Settled surfaces stay reachable but stop occupying the driver's
            # scan: the list was becoming every decision ever made. A strand
            # is never folded away — hiding the alarm defeats it.
            settled = "DECIDED" in str(sf.get("title") or "") and not sf.get("stranded")
            (decided if settled else rows).append(row)
        return byline, rows, decided, len(rows_src)

    exts = external_projects(snap)
    projs = projs or {}
    finished = finished or {}
    claimed = set()
    # Durable projects first: a section per top-level project, drawn whether
    # or not anything is open in it. Members fold into their group's section.
    # Finished work is not listed here — it is a count linking to the page.
    tops = [p for p in projects_mod.live(projs)
            if not (p.get("group") and (projs.get(p["group"]) or {}).get("archived") is False)]
    for p in tops:
        parts, n_live, n_done = [], 0, 0
        for m in [p, *[m for m in projects_mod.members(projs, p["id"]) if not m.get("archived")]]:
            names = projects_mod.names_of(projs, m["id"])
            src = [sf for e in exts if e["project"] in names for sf in e["rows"]]
            claimed |= names
            byline, rows, decided, _ = _group_rows(src)
            n_live += len(rows)
            n_done += len(decided) + int(finished.get(m["id"]) or 0)
            if m is p:
                parts.insert(0, byline + "".join(rows))
            else:
                parts.append(f'<div class="member"><h3><a href="/project/{escape(m["id"])}">'
                             f'{escape(m["name"])}</a></h3>{byline}{"".join(rows)}'
                             + ("" if rows else '<p class="empty">nothing running</p>')
                             + '</div>')
        # Launched-but-pageless work belongs to the project that launched it,
        # and is shown before its first page exists so the launch is visible.
        start = [s for m in [p, *projects_mod.members(projs, p["id"])]
                 for s in (starting or {}).get(m["id"], [])]
        if start:
            parts.insert(0, starting_rows(start, now, doing))
        n_live += len(start)
        count = f'<span class="badge">{n_live} running</span>' if n_live else ""
        done = (f'<a class="hist" href="/project/{escape(p["id"])}#finished">'
                f'{n_done} finished</a>') if n_done else ""
        empty = ("" if n_live or len(parts) > 1 else
                 '<p class="empty">Nothing running in this project.</p>')
        out.append(f'<section class="card project durable"><h2>'
                   f'<a class="repo" href="/project/{escape(p["id"])}">{escape(p["name"])}</a>'
                   f'{count}{done}<a class="hist" href="/project/{escape(p["id"])}#launch">'
                   f'new work →</a></h2>{"".join(parts)}{empty}</section>')
    for ext in exts:
        if ext["project"] in claimed:
            continue
        byline, rows, decided, n_src = _group_rows(ext["rows"])
        fold = (f'<details class="fold" data-fold="{escape(ext["project"])}">'
                f'<summary>{len(decided)} decided</summary>'
                f'{"".join(decided)}</details>') if decided else ""
        count = f'<span class="badge">{n_src} session{"s" if n_src != 1 else ""}</span>'
        out.append(f'<section class="card project"><h2>'
                   f'<span class="repo">{escape(ext["project"])}</span>{count}</h2>'
                   f'{byline}{"".join(rows)}{fold}</section>')
    # With conversations collapsed, a dialogue's pages file under their
    # project (or their checkout's); what is left here has no project and no
    # working directory to take one from.
    orphans = orphan_surfaces(snap)

    def _orow(sf, label):
        when, hover = _opened(sf)
        return (f'<div class="rowline">'
                f'<a class="row"{hover} href="{escape(view_href(sf.get("path")))}">'
                f'<span class="title">{escape(label)}</span>{_live(sf)}{when}{_spent(sf)}'
                f'<span class="go">open →</span></a>'
                f'{_history_link(sf)}{_page_link(sf)}</div>')

    if orphans:
        rows = "".join(_orow(sf, str(sf.get("title") or sf.get("task") or sf.get("story")
                                     or sf.get("kind") or "surface")) for sf in orphans)
        out.append(f'<section class="card"><h2>Loose sessions</h2>{rows}</section>')
    if not projects and not out:
        out.append('<section class="card"><p class="empty">No stories in flight. '
                   'Start one with the box above.</p></section>')
    for p in projects:
        flag = (f'<span class="badge needs">{p["attention"]} need you</span>'
                if p["attention"] else "")
        rows = "".join(_story_html(s, now) for s in p["stories"])
        out.append(f'<section class="card project"><h2>'
                   f'<span class="repo">{escape(p["repo"])}</span>{flag}</h2>{rows}</section>')
    return code_line(snap, own=own, now=now) + "".join(out)


def code_line(snap: dict, own=None, now: float | None = None) -> str:
    """One line naming whichever process runs older code than the folder
    holds: the daemon (its snapshot's `code`) or this page server (`own`, its
    startup stamp). Empty while both are current."""
    own = _CODE if own is None else own
    behind = []
    code = snap.get("code") or {}
    if code.get("running") and code.get("tree") and code["running"] != code["tree"]:
        behind.append(("the daemon", code.get("since"), code.get("head")))
    if own.stale():
        behind.append(("the fleet page", own.since, own.head))
    if not behind:
        return ""
    who = " and ".join(
        f'{name} (started {escape(_rel_time(since or 0, now))}'
        + (f' on {escape(head)}' if head else "") + ")"
        for name, since, head in behind)
    verb, them = ("is", "it") if len(behind) == 1 else ("are", "them")
    return ('<section class="card stale"><p><span class="badge needs">stale code</span> '
            f'{who[0].upper()}{who[1:]} {verb} running code older than this folder; '
            f'edits wait until you restart {them}.</p></section>')


def render_home(snap: dict, notice: str = "", now: float | None = None,
                statuses: dict | None = None, graph: tuple | None = None,
                projs: dict | None = None, finished: dict | None = None,
                starting: dict | None = None, doing: dict | None = None,
                spent: dict | None = None, week_usd: float | None = None,
                fleet_html: str | None = None) -> str:
    """The panel: a static layout — task box, find, the fleet — whose fleet
    fragment is re-fetched when the fleet stream reports a change.
    `fleet_html` is the fleet already rendered from every backend's API (the
    live page); None renders `snap` directly (tests)."""
    now = time.time() if now is None else now
    stamp = snap.get("iso") or "—"
    banner = f'<section class="card"><p>{escape(notice)}</p></section>' if notice else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Cadre — Agent Fleet</title>'
        '<link rel="icon" href="data:image/svg+xml,'
        '%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22%3E'
        '%3Ctext y=%2213%22 font-size=%2213%22%3E%F0%9F%9B%B0%3C/text%3E%3C/svg%3E">'
        f'<style>{_CSS}</style></head><body><div class="wrap">'
        '<header><h1>Cadre<span class="dot">.</span> agent fleet</h1>'
        f'<span class="updated">snapshot {escape(str(stamp))}</span>'
        + (f'<a class="costs" href="{COSTS_PATH}">{escape(costs_mod.usd(week_usd))} '
           'in 7 days</a>' if week_usd is not None else "") +
        '<span id="live" class="live">connecting…</span></header>'
        f'{banner}'
        # No fleet-wide task box: work belongs to a project, and the box asked
        # the driver to type a raw absolute path, which is what tab-complete
        # exists to avoid. Launch from a project instead — it knows its own
        # directory, so there is no path to type.
        f'{_new_project_form(projs or {})}'
        '<input id="filter" type="search" aria-label="find" '
        'placeholder="find — filters the rows below and searches every page ever opened">'
        '<section id="found" class="card" hidden></section>'
        f'<div id="fleet">{render_fleet(snap, now, statuses=statuses, graph=graph, projs=projs, finished=finished, starting=starting, doing=doing, spent=spent) if fleet_html is None else fleet_html}</div>'
        '<footer>Live: refreshes when the runner, a review page or a link changes · '
        f'<a href="{LIBRARY_PATH}">installed skills, agents and workflows</a> · '
        f'<a href="{RUNS_PATH}">every run, newest first</a> · '
        f'<a href="{COSTS_PATH}">what each run cost</a> · '
        '<a href="/index.html">legacy dashboard</a></footer>'
        f'</div><script>{_PAGE_JS}</script></body></html>')


def _group_options(projs: dict, selected: str = "", exclude: str = "") -> str:
    return "".join(
        f'<option value="{escape(p["id"])}"{" selected" if p["id"] == selected else ""}>'
        f'{escape(p["name"])}</option>'
        for p in projects_mod.live(projs) if p["id"] != exclude)


def _known_dirs() -> list[str]:
    """Directories the runner has already worked in. These seed the field
    before the driver types; once they type, completions come from the real
    filesystem via /fs."""
    seen = []
    try:
        sess = surface_mod.sessions(_CFG)
    except Exception:
        return seen
    for meta in sess.values():
        d = str((meta or {}).get("cwd") or "").strip()
        if d and d not in seen:
            seen.append(d)
    return sorted(seen)


def complete_dirs(prefix: str, limit: int = 40) -> list[str]:
    """Real directories on this machine matching `prefix`, the way a shell
    would complete them. A browser datalist can only offer a list we gave it
    up front; reading the filesystem per keystroke is what makes this behave
    like tab-complete instead of a guess at what the driver might want.

    Directories only — a task runs in one — and never follows into a
    directory it cannot read.
    """
    raw = (prefix or "").strip()
    if not raw:
        return []
    p = Path(raw).expanduser()
    # "/home/me/Pro" means: list /home/me, keep the names starting with "Pro".
    # A trailing slash means the directory itself is complete; list inside it.
    parent, stem = (p, "") if raw.endswith("/") else (p.parent, p.name)
    try:
        if not parent.is_dir():
            return []
        names = sorted(
            str(c) + "/" for c in parent.iterdir()
            if c.name.startswith(stem) and not c.name.startswith(".") and c.is_dir())
    except (OSError, PermissionError):
        return []
    return names[:limit]


def _new_project_form(projs: dict) -> str:
    return (
        # Open by default: this is the one control that creates the thing the
        # whole page is organised around, and a collapsed summary made it read
        # as an advanced option.
        '<details class="card" open><summary class="meta">New project — a durable home '
        'for work, kept on the fleet until you archive it</summary>'
        f'<form class="newtask" method="post" action="{PROJECTS_PATH}">'
        '<div class="row"><input name="name" required maxlength="64" '
        'placeholder="name, e.g. Cadre"></div>'
        '<div class="dirwrap"><label class="lbl" for="dir1">Working directory — '
        'type <code>/</code> or <code>~</code> and it completes from this machine'
        '</label><input name="dir1" id="dir1" '
        'autocomplete="off" spellcheck="false" role="combobox" aria-expanded="false" '
        'aria-autocomplete="list" aria-controls="dirsug" '
        'placeholder="~/Projects/…">'
        '<ul id="dirsug" class="sug" role="listbox" hidden></ul></div>'
        # A real completion list rather than a datalist: a datalist only opens
        # when the browser feels like it and filters full paths badly, so it
        # read as "no autocomplete at all". This draws every keystroke's
        # matches, supports arrow keys and Enter, and shows "no match" rather
        # than going silent — the feedback the driver asked for.
        '<script>(function(){'
        'var i=document.getElementById("dir1"),box=document.getElementById("dirsug");'
        'if(!i||!box)return;var t,items=[],cur=-1;'
        'function hide(){box.hidden=true;i.setAttribute("aria-expanded","false");cur=-1;}'
        'function show(){box.hidden=false;i.setAttribute("aria-expanded","true");}'
        'function draw(dirs,note){items=dirs;box.innerHTML="";cur=-1;'
        'if(!dirs.length){box.innerHTML="<li class=\\"none\\">"+note+"</li>";show();return;}'
        'dirs.forEach(function(p,n){var li=document.createElement("li");'
        'li.textContent=p;li.setAttribute("role","option");'
        'li.addEventListener("mousedown",function(e){e.preventDefault();pick(n);});'
        'box.appendChild(li);});show();}'
        'function pick(n){if(n<0||n>=items.length)return;i.value=items[n];i.focus();load();}'
        'function mark(){Array.prototype.forEach.call(box.children,function(li,n){'
        'li.className=(n===cur?"on":"");});}'
        # Nothing is drawn until the driver types: an unprompted list of every
        # directory in $HOME is noise, and it pushed the form around.
        'function load(){var v=i.value;if(!v){hide();return;}'
        'fetch("/fs?q="+encodeURIComponent(v)).then(function(r){return r.json();})'
        '.then(function(d){draw(d.dirs||[],"no directory matches");})'
        '.catch(function(){});}'
        'i.addEventListener("input",function(){clearTimeout(t);t=setTimeout(load,80);});'
        'i.addEventListener("blur",function(){setTimeout(hide,120);});'
        'i.addEventListener("keydown",function(e){'
        'if(e.key==="Escape"){hide();return;}'
        'if(!items.length||box.hidden)return;'
        'if(e.key==="ArrowDown"){e.preventDefault();cur=(cur+1)%items.length;mark();}'
        'else if(e.key==="ArrowUp"){e.preventDefault();'
        'cur=(cur<=0?items.length:cur)-1;mark();}'
        'else if(e.key==="Enter"&&cur>=0){e.preventDefault();pick(cur);}'
        'else if(e.key==="Tab"&&items.length===1){e.preventDefault();pick(0);}});'
        '})();</script>'
        # No free-text box here. A large empty textarea on a creation form
        # reads as "describe this thing", and the one that used to sit here
        # took absolute paths — so a sentence typed into it came back as
        # "not an absolute path", blaming the driver for the form's wording.
        # One directory is all a project needs to start; more are added from
        # the project page, where the field says so plainly.
        '<div class="row"><select name="group"><option value="">no group</option>'
        f'{_group_options(projs)}</select>'
        '<input name="context" type="text" placeholder="context store path (optional): '
        'a folder with brief.md and decisions/">'
        '<button type="submit">Create</button></div></form></details>')


def render_library(entries: list[dict], scanned: list[str]) -> str:
    """Everything installed that a launch can pick, found by scanning — so
    nobody has to know where a skill lives to use it."""
    kinds = [("skill", "Skills"), ("workflow", "Workflows"), ("agent", "Agents"),
             ("flow", "Cadre flows")]
    cards = []
    for kind, label in kinds:
        rows = [e for e in entries if e["kind"] == kind]
        if not rows:
            continue
        body = "".join(
            f'<tr><td class="inv">{escape(e["invoke"] or e["name"])}</td>'
            f'<td class="scope">{escape(e["scope"])}</td>'
            f'<td class="desc">{"<b>broken link</b> " if e["broken"] else ""}'
            f'{escape(e["description"][:180])}</td></tr>' for e in rows)
        cards.append(f'<section class="card"><h2>{label} · {len(rows)}</h2>'
                     f'<table class="lib">{body}</table></section>')
    where = ", ".join(scanned) or "none"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Cadre — library</title><style>{_CSS}</style></head>'
        '<body><div class="wrap"><header><h1>Library<span class="dot">.</span></h1></header>'
        '<p class="meta"><a href="/">← fleet</a> · found by scanning ~/.claude, the '
        f'runner\'s skills tree and these project directories: {escape(where)}</p>'
        '<section class="card"><p class="empty">Launch any of these from a project page, or '
        '<code>pipeline.py task "…" --run kind:name</code>. Skills and workflows start as '
        '<code>/name</code> at the front of the prompt, agents as <code>@agent-name</code>; '
        'flows are listed for reference — every launch runs as a dialogue today.</p></section>'
        f'{"".join(cards)}</div></body></html>')


def load_costs(data_dir: Path | None = None) -> tuple[list[dict], dict]:
    """(every finished turn, {task id: project}) — read off the daemon's own
    history log and registry, the same read-only way live_turns() does."""
    d = Path(DATA_DIR if data_dir is None else data_dir)
    try:
        reg = json.loads((d / "registry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reg = {}
    if not isinstance(reg, dict):
        reg = {}
    projs = projects_mod.load(d)
    return costs_mod.read(d / "history.jsonl"), costs_mod.task_projects(reg, projs)


def _cost_rows(buckets: list[dict], label, limit: int | None = None) -> str:
    usd = costs_mod.usd
    rows = []
    for b in buckets[:limit]:
        avg = b["usd"] / max(1, b["runs"] - b["unpriced"])
        unpriced = f' · {b["unpriced"]} unpriced' if b["unpriced"] else ""
        rows.append(f'<tr><td class="desc">{label(b)}</td>'
                    f'<td class="inv">{usd(b["usd"])}</td>'
                    f'<td class="scope">{b["runs"]} turn{"s" if b["runs"] != 1 else ""}'
                    f'{unpriced} · {usd(avg)} avg</td></tr>')
    return "".join(rows)


def render_costs(summary: dict, now: float | None = None) -> str:
    """Where the money went: totals, then the same spend by node (the kind of
    work — a dialogue turn, implement, a pipeline stage), by project and by
    task, then the newest turns one by one."""
    now = time.time() if now is None else now
    usd = costs_mod.usd
    s = summary
    unpriced = (f' {s["unpriced"]} turn{"s" if s["unpriced"] != 1 else ""} reported no cost '
                '(lost, timed out, or paused on a usage limit) and are left out of the sums.'
                if s["unpriced"] else "")
    head = (f'<section class="card"><h2>Spent</h2><p><b>{usd(s["total_usd"])}</b> across '
            f'{s["priced"]} priced turns · <b>{usd(s["last_7d_usd"])}</b> in the last 7 days · '
            f'<b>{usd(s["last_24h_usd"])}</b> in the last 24 hours.</p>'
            f'<p class="empty">A turn\'s cost is what Claude Code reported when it ended, so a '
            f'turn still running shows up here once it finishes.{unpriced}</p></section>')
    node = _cost_rows(s["by_node"], lambda b: escape(b["key"]))
    proj = _cost_rows(s["by_project"], lambda b: escape(b["key"]))
    task = _cost_rows(s["by_task"], lambda b: (
        f'{escape(_task_label(b["key"]))} '
        f'<span class="scope">{escape(b.get("project") or "")}</span>'), limit=40)
    recent = "".join(
        f'<tr><td class="desc">{escape(_task_label(str(e.get("story") or "")))}</td>'
        f'<td class="inv">{usd(c) if (c := costs_mod.cost_of(e)) is not None else "—"}</td>'
        f'<td class="scope">{escape(str(e.get("stage") or ""))} · '
        f'{escape(str(e.get("model") or ""))} · {round(float(e.get("seconds") or 0) / 60)} min · '
        f'{escape(_rel_time(float(e.get("ended") or 0), now))}'
        f'{"" if e.get("ok") else " · failed"}</td></tr>' for e in s["recent"])

    def card(title, body):
        return (f'<section class="card"><h2>{title}</h2><table class="lib">{body}</table></section>'
                if body else "")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Cadre — costs</title><style>{_CSS}</style></head>'
        '<body><div class="wrap"><header><h1>Costs<span class="dot">.</span></h1></header>'
        f'<p class="meta"><a href="/">← fleet</a> · <a href="{COSTS_PATH}.json">as JSON</a></p>'
        f'{head}{card("By node", node)}{card("By project", proj)}'
        f'{card("By task · most expensive first", task)}{card("Newest turns", recent)}'
        '</div></body></html>')


def load_runs(data_dir: Path | None = None,
              titles: dict | None = None) -> tuple[list[dict], dict, dict]:
    """(every finished turn, {task: project}, {task: newest page meta}) —
    what the runs view and the fleet's "returned" badges read. Each page
    carries the title its author gave it (review-surface's page list,
    `titles`), which reads far better than the task id it came from."""
    d = Path(DATA_DIR if data_dir is None else data_dir)
    turns, owners = load_costs(d)
    try:
        store = json.loads((d / "surfaces" / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        store = {}
    titles = conv_mod.load_pages() if titles is None else titles
    pages = {t: {**m, "title": str((titles.get(m.get("key")) or {}).get("title") or "")}
             for t, m in recent_mod.pages_by_task(store).items()}
    return turns, owners, pages


def _run_row(r: dict, now: float, per_task: bool = True) -> str:
    """One run: the task, where it lives, when it returned and how it went.
    The row opens the task's newest page; a task with no page is listed
    but not clickable, since there is nothing to open. Turn count and time
    ride on the hover text — on the row they crowded the title out."""
    usd = costs_mod.usd
    badges = (f'<span class="badge">{escape(r["project"])}</span>' if r["project"] else "")
    # a dialogue turn is the common case; only other nodes earn a badge
    badges += (f'<span class="badge">{escape(r["node"])}</span>'
               if r["node"] and r["node"] != "task" else "")
    if not r["ok"]:
        badges += '<span class="badge needs">failed</span>'
    if r["open"]:
        badges += '<span class="chip open">open</span>'
    badges += f'<span class="badge">returned {escape(_rel_time(r["ended"], now))}</span>'
    if r["cost"] is not None:
        badges += f'<span class="badge">{escape(usd(r["cost"]))}</span>'
    turns = f'{r["turns"]} turns, ' if per_task and r["turns"] > 1 else ""
    hover = escape(f'{turns}{max(1, round(r["seconds"] / 60))} min of agent time')
    page = r.get("page") or {}
    title = f'<span class="title">{escape(page.get("title") or _task_label(r["task"]))}</span>'
    if not page.get("path"):
        return (f'<div class="rowline"><span class="row" title="{hover}">'
                f'{title}{badges}</span></div>')
    return (f'<div class="rowline"><a class="row" title="{hover}" '
            f'href="{escape(view_href(page["path"]))}">{title}{badges}'
            f'<span class="go">open →</span></a>'
            f'<a class="hist" href="/page/{escape(str(page.get("key") or ""))}">manage</a></div>')


def render_recent(rows: list[dict], now: float | None = None,
                  limit: int = RECENT_LIMIT) -> str:
    """The fleet's cross-project card: the tasks whose agents came back most
    recently, whichever project they are in, with the way into the full view."""
    now = time.time() if now is None else now
    if not rows:
        return ""
    return (f'<section class="card recent"><h2>Recently returned'
            f'<a class="hist" href="{RUNS_PATH}">all runs, sort and filter →</a></h2>'
            f'{"".join(_run_row(r, now) for r in rows[:limit])}</section>')


_RUNS_JS = """
(function(){
 // j/k or the arrow keys walk the list, Enter opens the row under focus.
 var rows=[].slice.call(document.querySelectorAll('#runs a.row'));
 document.addEventListener('keydown',function(e){
  var t=e.target;if(t&&(t.tagName==='INPUT'||t.tagName==='SELECT'))return;
  var down=e.key==='j'||e.key==='ArrowDown',up=e.key==='k'||e.key==='ArrowUp';
  if(!down&&!up||!rows.length)return;
  e.preventDefault();
  var i=rows.indexOf(document.activeElement);
  i=i<0?0:Math.max(0,Math.min(rows.length-1,i+(down?1:-1)));
  rows[i].focus();rows[i].scrollIntoView({block:'nearest'});
 });
 document.querySelectorAll('#runfilter select').forEach(function(s){
  s.addEventListener('change',function(){s.form.submit()});
 });
})();
"""


def render_runs(rows: list[dict], sel: dict, projects: list[str], nodes: list[str],
                now: float | None = None, limit: int = 200) -> str:
    """Every run across every project, in one of a handful of views, sorted
    and filtered by query string so a view is a link the driver can keep."""
    now = time.time() if now is None else now
    view = sel.get("view") if sel.get("view") in recent_mod.VIEWS else "latest"

    def href(**over) -> str:
        q = {k: v for k, v in {**sel, **over}.items() if v}
        return RUNS_PATH + ("?" + urllib.parse.urlencode(q) if q else "")
    tabs = " · ".join(
        f'<b>{escape(label)}</b>' if key == view
        else f'<a href="{escape(href(view=key))}">{escape(label)}</a>'
        for key, label in recent_mod.VIEWS.items())

    def options(name, items, blank):
        cur = sel.get(name) or ""
        return (f'<select name="{name}" aria-label="{name}"><option value="">{blank}</option>'
                + "".join(f'<option value="{escape(v)}"{" selected" if v == cur else ""}>'
                          f'{escape(label)}</option>' for v, label in items)
                + '</select>')
    sorts = options("sort", [(k, v) for k, v in recent_mod.SORTS.items() if k != "newest"],
                    recent_mod.SORTS["newest"])
    form = (f'<form id="runfilter" class="repair" method="get" action="{RUNS_PATH}">'
            f'<input type="hidden" name="view" value="{escape(view)}">{sorts}'
            f'{options("project", [(p, p) for p in projects], "every project")}'
            f'{options("node", [(n, n) for n in nodes], "every node")}'
            f'<input type="search" name="q" value="{escape(sel.get("q") or "")}" '
            f'placeholder="task or project contains…" aria-label="search">'
            f'<button type="submit">apply</button></form>')
    shown = rows[:limit]
    count = (f'{len(rows)} run{"s" if len(rows) != 1 else ""}'
             + (f' · showing the first {limit}' if len(rows) > limit else ""))
    body = ("".join(_run_row(r, now, per_task=view in ("latest", "open")) for r in shown)
            or '<p class="empty">Nothing matches this view.</p>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Cadre — runs</title><style>{_CSS}</style></head>'
        '<body><div class="wrap"><header><h1>Runs<span class="dot">.</span></h1></header>'
        f'<p class="meta"><a href="/">← fleet</a> · {tabs}</p>{form}'
        f'<p class="meta">{escape(count)} · j / k or the arrow keys move, Enter opens</p>'
        f'<section id="runs" class="card">{body}</section>'
        f'</div><script>{_RUNS_JS}</script></body></html>')


def starting_rows(starting: list[dict], now: float,
                  doing: dict | None = None) -> str:
    """Launched, no page yet. Says which of the three it is — waiting for a
    slot, mid-round, or finished having written nothing — because "nothing
    visible" is what made the driver launch the same work four times. The
    third state is named rather than hidden: a turn that ends without a page
    is a failure worth seeing, and hiding it is how work went missing."""
    out = []
    for s in starting:
        age = now - (s.get("since") or 0)
        if s.get("running"):
            # What it is DOING, when the transcripts can say. A turn that fans
            # out goes quiet for half an hour, and "working" alone is what made
            # the driver assume a healthy run had died.
            said = (doing or {}).get(s["task"])
            what = (f"working — {said}" if said
                    else "working — its page appears when this round ends")
        elif s.get("ran"):
            what = "ran, but wrote no page"
        elif s.get("failed"):
            what = f"could not start — {_short_error(s['failed'])}"
        elif s.get("started"):
            # A session id was assigned and the process began. Nothing is
            # running now and no page exists, so it was interrupted — a
            # restart, a reboot, a killed daemon. Its transcript survives.
            what = "started, then stopped before writing a page"
        elif age > STARTUP_GRACE:
            # Launched long ago, nothing ever ran. Saying "queued" about a
            # request this old would be the same lie told more politely.
            what = "never started — the runner did not pick this up"
        else:
            what = "queued — the runner starts it on its next pass"
        out.append(
            f'<div class="rowline"><span class="row starting">'
            f'<span class="title">{escape(_task_label(s["task"]))}</span>'
            f'<span class="badge running">{escape(what)}</span>'
            f'<span class="badge">{escape(_rel_time(s.get("since") or 0, now))}</span>'
            f'</span></div>')
    return "".join(out)


def _run_picker(entries: list[dict], current: str = "") -> str:
    """Pick a skill, workflow, agent or flow to launch — an OVERRIDE, folded
    away, not a question the driver has to answer every time.

    It was a `<select>` of 111 options in four groups, which is unusable at
    that size and, worse, implied the driver had to know which one fits. He
    does not: a session told "this is research" can load a research skill
    itself. So the default is that nobody chooses, and this opens only when
    someone wants to force a particular one.

    Everything installed rides along as JSON and the filtering happens in the
    browser. The list is small enough to send once and a round trip per
    keystroke is exactly the lag that made the directory field feel broken.
    """
    live = [e for e in entries if not e.get("broken")]
    live.sort(key=lambda e: (e["kind"], e["name"].lower()))
    data = json.dumps([{"v": f'{e["kind"]}:{e["name"]}', "i": e["invoke"],
                        "k": e["kind"], "d": (e.get("description") or "")[:120]}
                       for e in live], ensure_ascii=False)
    chosen = next((e for e in live if f'{e["kind"]}:{e["name"]}' == current), None)
    summary = (f'run {escape(chosen["invoke"])}' if chosen else
               "the session picks its own skill — click to choose one yourself")
    return (
        f'<details class="pick"{" open" if chosen else ""}>'
        f'<summary>{summary}</summary>'
        f'<script type="application/json" id="runlib">{data}</script>'
        f'<input type="hidden" name="run" id="runval" value="{escape(current)}">'
        '<div class="dirwrap">'
        '<input id="runq" type="text" autocomplete="off" spellcheck="false" '
        'role="combobox" aria-expanded="false" aria-controls="runsug" '
        'aria-autocomplete="list" placeholder="type to find — build, review, spanish…">'
        '<ul id="runsug" class="sug" role="listbox" hidden></ul></div>'
        '<p class="meta">Leave this alone and the session chooses. '
        f'<button type="button" id="runclear" class="linky">clear</button></p>'
        '</details>')


def _short_error(detail: str, limit: int = 110) -> str:
    """The reason a firing failed, short enough to sit in a badge. The
    exception class name is dropped — `ActionFailed:` in front of every one of
    them is noise, and the sentence after it is the part that says what to
    fix."""
    text = " ".join(str(detail).split())
    text = re.sub(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Failed|Exception):\s*", "", text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _task_label(task_id: str) -> str:
    """`task-fix-the-thing-20260925-124025-badfc9` reads as `fix the thing`."""
    body = re.sub(r"^task-", "", str(task_id))
    body = re.sub(r"-\d{8}-\d{6}(-[0-9a-f]+)?$", "", body)
    return body.replace("-", " ") or str(task_id)


def render_project(p: dict, projs: dict, rows_html: str, closed: list[dict],
                   entries: list[dict], notice: str = "", now: float | None = None,
                   starting: list[dict] | None = None,
                   doing: dict | None = None) -> str:
    """One project's own page: the launcher, what runs in it, what finished,
    and what a task launched here is told."""
    now = time.time() if now is None else now
    pid = p["id"]
    group = projs.get(p.get("group") or "")
    mem = projects_mod.members(projs, pid)
    meta = ['<a href="/">← fleet</a>']
    if group:
        meta.append(f'in <a href="/project/{escape(group["id"])}">{escape(group["name"])}</a>')
    if mem:
        meta.append("group of " + ", ".join(
            f'<a href="/project/{escape(m["id"])}">{escape(m["name"])}</a>' for m in mem))
    if p.get("archived"):
        meta.append("<b>archived</b>")
    banner = f'<section class="card"><p>{escape(notice)}</p></section>' if notice else ""
    try:
        where = projects_mod.workdir(projs, pid)
    except projects_mod.ProjectError:
        where = ""
    dirs = [d for m in [p, *mem] for d in m.get("dirs") or ()]
    dir_opts = "".join(f'<option value="{escape(d)}"{" selected" if d == where else ""}>'
                       f'{escape(d)}</option>' for d in dict.fromkeys(dirs))
    if p.get("archived"):
        form = '<p class="empty">Archived — restore it to launch work here.</p>'
    elif not where:
        form = '<p class="empty">This project has no directory to run work in.</p>'
    else:
        form = (f'<form class="newtask" method="post" action="{PROJECTS_PATH}/{escape(pid)}/tasks">'
                '<textarea name="text" required placeholder="What should run in this project?">'
                '</textarea><div class="row">'
                f'<select name="cwd">{dir_opts}</select>'
                '<button type="submit">Launch</button></div>'
                f'{_run_picker(entries, p.get("launch") or "")}'
                f'<p class="meta">everything installed: <a href="{LIBRARY_PATH}">library</a></p>'
                f'</form><script>{_PICKER_JS}</script>')
    launch = f'<section class="card" id="launch"><h2>new work</h2>{form}</section>'
    done = "".join(
        f'<div class="rowline"><a class="row" href="{VIEW_PATH}{escape(str(m["key"]))}">'
        f'<span class="title">{escape(str(m.get("title") or m.get("task") or m["key"]))}</span>'
        f'<span class="badge">{escape(_rel_time(m.get("opened") or 0, now))}</span>'
        f'<span class="go">open →</span></a></div>' for m in closed[:100])
    ctx = projects_mod.context_path(projs, pid)
    if ctx:
        bpath, btext = projects_mod.brief(ctx)
        dec = projects_mod.decisions(ctx)
        context = (f'<p class="meta">{escape(ctx)}</p>'
                   + (f'<pre class="brief">{escape(btext[:4000])}</pre>' if btext
                      else '<p class="empty">No brief.md or README.md in it yet.</p>')
                   + (f'<details class="fold"><summary>{len(dec)} recorded decisions</summary>'
                      + "".join(f'<p class="meta">{escape(Path(d).name)}</p>' for d in dec)
                      + '</details>' if dec else '<p class="empty">No decisions recorded yet.</p>'))
    else:
        context = ('<p class="empty">No context store. Point one at a folder holding brief.md '
                   f'and decisions/: <code>pipeline.py project set {escape(pid)} --context PATH</code>'
                   '</p>')
    defaults = []
    if p.get("skills"):
        defaults.append("default skills: " + ", ".join(escape(s) for s in p["skills"]))
    if p.get("launch"):
        defaults.append(f"default launch: {escape(p['launch'])}")
    arch = "0" if p.get("archived") else "1"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(p["name"])} — project</title><style>{_CSS}</style></head>'
        f'<body><div class="wrap"><header><h1>{escape(p["name"])}<span class="dot">.</span>'
        '</h1></header>'
        f'<p class="meta">{" · ".join(meta)}</p>{banner}{launch}'
        f'<section class="card project"><h2>running</h2>'
        f'{starting_rows(starting or [], now, doing)}'
        f'{rows_html or ("" if starting else "<p class=empty>Nothing running.</p>")}</section>'
        f'<section class="card" id="finished"><h2>finished · {len(closed)}</h2>'
        f'{done or "<p class=empty>Nothing finished yet.</p>"}</section>'
        f'<section class="card"><h2>context</h2>{context}'
        f'<details class="fold"><summary>what a task launched here is told</summary>'
        f'<pre class="brief">{escape(projects_mod.prompt_block(projs, pid, where))}</pre>'
        '</details></section>'
        '<section class="card"><h2>project</h2>'
        + "".join(f'<p class="meta">{d}</p>' for d in defaults)
        + "".join(f'<p class="meta">{escape(d)}</p>' for d in dirs)
        + f'<form class="repair" method="post" action="{PROJECTS_PATH}/{escape(pid)}/archive">'
        f'<input type="hidden" name="archived" value="{arch}">'
        f'<button type="submit"{" class=danger" if arch == "1" else ""}>'
        f'{"Archive" if arch == "1" else "Restore"}</button>'
        '<p>Archiving hides the project from the fleet and keeps its record.</p></form>'
        '</section></div></body></html>')


def render_found(query: str, hits: list[dict], now: float | None = None) -> str:
    """Find results: every page ever opened that matches, with where it lives
    and whether it is the live round of its conversation."""
    now = time.time() if now is None else now
    if not hits:
        return (f'<h2>everywhere · no page matches “{escape(query)}”</h2>')
    rows = []
    for h in hits:
        state = h["state"]
        chip = (f'<span class="chip replaced">replaced</span>' if state == "replaced"
                else f'<span class="chip {escape(state)}">{escape(state)}</span>')
        proj = f'<span class="badge">{escape(h["project"])}</span>' if h["project"] else ""
        when = (f'<span class="badge">{escape(_rel_time(h["updated"], now))}</span>'
                if h["updated"] else "")
        rows.append(f'<div class="rowline"><a class="row" href="{escape(view_href(h["path"]))}">'
                    f'<span class="title">{escape(h["title"])}</span>{chip}{proj}{when}'
                    f'<span class="go">open →</span></a>'
                    f'<a class="hist" href="/page/{escape(h["key"])}">manage</a></div>')
    return (f'<h2>everywhere · {len(hits)} page{"s" if len(hits) != 1 else ""} '
            f'match “{escape(query)}”</h2>{"".join(rows)}')


def _attach_form(head_key: str, owner: dict | None) -> str:
    """The fourth repair: put an agent on a page nobody owns. Every other
    ownership state renders the control disabled with the reason, so the
    driver can never spawn a second owner from here."""
    owner = owner or {"state": "unknown"}
    state = owner.get("state")
    why = {
        "task": f"Owned by dialogue {owner.get('task')}: annotate the page and it "
                "reaches that agent as its next turn.",
        "pipeline": f"Owned by the pipeline ({owner.get('kind')} session): the runner "
                    "already delivers its feedback.",
        "pending": "An attach is queued; the runner dispatches it on its next tick.",
        "outside": "An agent outside the runner is working on this page right now; "
                   "attaching would give it a second owner.",
        "unknown": "Not an open runner session with a page file, so nothing can be "
                   "attached from here" + (f" ({owner['why']})." if owner.get("why") else "."),
    }.get(state)
    err = (f' Last attach failed: {escape(owner["error"])}.' if owner.get("error") else "")
    if why:
        return (f'<form class="repair"><button type="submit" disabled>Attach agent</button>'
                f'<p>{escape(why)}</p></form>')
    return (f'<form class="repair" method="post" action="{REPAIR_PATH}">'
            f'<input type="hidden" name="key" value="{escape(head_key)}">'
            '<input type="hidden" name="action" value="attach">'
            '<input name="instruction" maxlength="4000" '
            'placeholder="instruction for the agent (optional)">'
            '<button type="submit">Attach agent</button>'
            '<p>No agent owns this page. Dispatches a dialogue that owns it from now on '
            'and rewrites this same page; with no instruction it reads the page and '
            f'continues it.{err}</p></form>')


def render_page(key: str, row: dict, projects: list[str], candidates: list[dict],
                notice: str = "", now: float | None = None,
                owner: dict | None = None) -> str:
    """One conversation's page in the panel: its rounds, newest first, and the
    four repairs — move it, mark it replaced, end its session, attach an
    agent. Repairs post back here; each one is a recorded link, a recorded
    session end, or an attach request the daemon serves."""
    now = time.time() if now is None else now
    head_key = conv_mod.key_of(row)
    title = escape(str(row.get("title") or head_key))
    rounds = [{"key": head_key, "title": row.get("page_title") or row.get("title") or head_key,
               "opened": row.get("opened") or 0}] + list(row.get("earlier") or [])
    newest = '<span class="chip open">newest</span>'
    items = "".join(
        f'<div class="rowline"><a class="row" href="{VIEW_PATH}{escape(r["key"])}">'
        f'<span class="title">{escape(str(r["title"]))}</span>'
        f'{newest if i == 0 else ""}'
        f'<span class="badge">{escape(_rel_time(r.get("opened") or 0, now))}</span>'
        f'<span class="go">open →</span></a></div>' for i, r in enumerate(rounds))
    focus = "" if key == head_key else (
        f'<p class="meta">you opened an earlier round ({escape(key)}); '
        f'the conversation continues in the newest one</p>')
    opts = "".join(f'<option value="{escape(p)}">' for p in projects)
    cands = "".join(f'<option value="{escape(c["key"])}">{escape(str(c["title"]))[:90]}</option>'
                    for c in candidates if c["key"] != head_key)
    ended = bool(row.get("ended"))
    banner = f'<section class="card"><p>{escape(notice)}</p></section>' if notice else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title} — manage</title><style>{_CSS}</style></head>'
        f'<body><div class="wrap"><header><h1>{title}</h1></header>'
        '<p class="meta"><a href="/">← fleet</a> · project '
        f'<b>{escape(str(row.get("project") or "none"))}</b> · page {escape(head_key)}</p>'
        f'{banner}{focus}'
        f'<section class="card"><h2>this conversation · {len(rounds)} round'
        f'{"s" if len(rounds) != 1 else ""}, newest first</h2>{items}</section>'
        '<section class="card"><h2>repairs</h2>'
        f'<form class="repair" method="post" action="{REPAIR_PATH}">'
        f'<input type="hidden" name="key" value="{escape(head_key)}">'
        '<input type="hidden" name="action" value="move">'
        f'<input name="project" list="projects" required placeholder="move to project…" '
        f'value="{escape(str(row.get("project") or ""))}"><datalist id="projects">{opts}</datalist>'
        '<button type="submit">Move</button>'
        '<p>Files this conversation under another project. Recorded as a child-of link.</p></form>'
        f'<form class="repair" method="post" action="{REPAIR_PATH}">'
        f'<input type="hidden" name="key" value="{escape(head_key)}">'
        '<input type="hidden" name="action" value="replace">'
        f'<select name="target" required><option value="">replaced by…</option>{cands}</select>'
        '<button type="submit">Mark replaced</button>'
        '<p>Folds this conversation into the page that replaces it; it stays reachable '
        'as an earlier round. Recorded as a supersedes link, refused if it would loop.</p></form>'
        f'<form class="repair" method="post" action="{REPAIR_PATH}" '
        'onsubmit="return confirm(\'End this review session? The agent stops receiving feedback from it.\')">'
        f'<input type="hidden" name="key" value="{escape(head_key)}">'
        '<input type="hidden" name="action" value="end">'
        f'<button type="submit" class="danger"{" disabled" if ended else ""}>End session</button>'
        f'<p>{"This session has already ended." if ended else "For a stuck session: ends it in review-surface and marks it closed for the runner."}</p>'
        f'</form>{_attach_form(head_key, owner)}</section></div></body></html>')


# --------------------------------------------------------------- live + fix

_VOLATILE = ("ts", "iso", "log_tail")


def _local_signature() -> str:
    """What the fleet looks like from this machine's files, as one hash: the
    runner's snapshot minus its heartbeat fields (it is rewritten every pass
    whether or not anything moved), plus the size and mtime of every link
    store."""
    h = hashlib.sha1()
    try:
        snap = json.loads((STATUS_DIR / "status.json").read_text(encoding="utf-8"))
        if isinstance(snap, dict):
            for k in _VOLATILE:
                snap.pop(k, None)
        h.update(json.dumps(snap, sort_keys=True, default=str).encode())
    except (OSError, ValueError):
        h.update(b"no-snapshot")
    for f in [*conv_mod.link_files(PANEL_LINKS), DATA_DIR / projects_mod.FILE]:
        try:
            st = os.stat(f)
            h.update(f"{f}:{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            pass
    return h.hexdigest()


def _relay_upstream(stop: threading.Event, poke: threading.Event, state: dict):
    """Hold one connection to review-surface's event stream and poke the
    panel's stream on every event. Reconnects with Last-Event-ID so nothing
    between connections is lost; a server without the endpoint (404) or not
    running is retried every 30 s and reported as upstream unavailable."""
    last_id = None
    while not stop.is_set():
        headers = {"Accept": "text/event-stream"}
        if last_id:
            headers["Last-Event-ID"] = last_id
        try:
            req = urllib.request.Request(SURFACE + "/api/events", headers=headers)
            with urllib.request.urlopen(req, timeout=STREAM_PING * 3) as resp:
                state["upstream"] = True
                poke.set()
                for line in resp:
                    if stop.is_set():
                        return
                    if line.startswith(b"id:"):
                        last_id = line[3:].strip().decode() or last_id
                    elif line.startswith(b"data:"):
                        poke.set()
        except Exception:
            pass
        was = state["upstream"]
        state["upstream"] = False
        if was:
            poke.set()
        stop.wait(2 if was else 30)


# ------------------------------------------------------ this machine as a backend
# What every fleet — this machine's own included — reads about this machine,
# through GET /api/fleet and GET /api/fleet/version (runnerlib/backends.py).

_EVENTS = {"n": 0, "upstream": False, "started": False}
_EVENTS_LOCK = threading.Lock()


def _review_events():
    """Count this machine's review-page events, once per process. The one held
    connection is from this backend to its own page server, on the same
    machine; fleets only ever poll the version this count feeds."""
    with _EVENTS_LOCK:
        if _EVENTS["started"]:
            return
        _EVENTS["started"] = True
    poke, state = threading.Event(), {"upstream": False}

    def count():
        while True:
            poke.wait()
            poke.clear()
            _EVENTS["n"] += 1
            _EVENTS["upstream"] = state["upstream"]

    threading.Thread(target=_relay_upstream, args=(threading.Event(), poke, state),
                     daemon=True).start()
    threading.Thread(target=count, daemon=True).start()


def fleet_version() -> dict:
    """Changes whenever anything the fleet shows for this machine changed: the
    runner's snapshot, a link store, a review page. Cheap; polled."""
    _review_events()
    sig = hashlib.sha1(f'{_local_signature()}:{_EVENTS["n"]}'.encode()).hexdigest()
    return {"version": sig, "instance": INSTANCE, "machine": MACHINE,
            "review_events": _EVENTS["upstream"]}


def fleet_payload() -> dict:
    """Everything render_fleet needs for this machine, as JSON. Anything the
    renderer would otherwise read from this machine's disk (answer-history
    counts, the fleet page's code stamp) is resolved here, so a fleet on
    another machine renders it without touching this one's files."""
    snap = read_snapshot()
    graph = load_graph()
    surfaces = conv_mod.collapse(snap.get("surfaces") or [], *graph)
    projs = load_projects()
    turns, owners, pages = load_runs(titles=graph[2])
    arts = {str(sf.get("artifact") or "") for sf in surfaces} - {""}
    return {**fleet_version(),
            "snap": snap, "graph": list(graph),
            "statuses": fetch_agent_statuses(surfaces),
            "projs": projs,
            "finished": {pid: len(rows) for pid, rows in closed_pages(projs).items()},
            "starting": starting_tasks(projs), "doing": live_turns(),
            "spent": costs_mod.per_task(turns),
            "returned": recent_mod.last_returned(turns),
            "recent": recent_mod.rows(turns, owners, pages)[:RECENT_LIMIT],
            "week_usd": costs_mod.summarize(turns, owners)["last_7d_usd"],
            "histories": {a: len(journal_batches(a)) for a in arts},
            "fleet_page_code": {"stale": _CODE.stale(), "since": _CODE.since,
                                "head": _CODE.head}}


def _backend_name(b: dict, data: dict | None) -> str:
    return b.get("name") or (data or {}).get("machine") or b["url"]


def render_machines(results: list) -> str:
    """The bar above the sections: every machine, whether it answered, and a
    jump to its section — the far machine is otherwise a long scroll away.
    Drawn only once there is more than one machine to tell apart."""
    if len(results) < 2:
        return ""
    bits = []
    for i, (b, data, _) in enumerate(results):
        state = ("this machine" if data and data.get("instance") == INSTANCE
                 else "answering" if data else "unreachable")
        cls = "badge needs" if data is None else "badge"
        bits.append(f'<a href="#machine-{i}"><b>{escape(_backend_name(b, data))}</b> '
                    f'<span class="{cls}">{state}</span></a>')
    return f'<nav class="machines card">{"".join(bits)}</nav>'


def render_backend(b: dict, data: dict | None, err: str | None,
                   now: float | None = None, index: int = 0) -> str:
    """One machine's section of the fleet, from its API payload: a heading
    naming the machine, then its fleet exactly as render_fleet draws any
    fleet. Nothing here knows whether the machine is this one, except where
    the links point and the 'this machine' label."""
    same = bool(data) and data.get("instance") == INSTANCE
    url = escape(b["url"], quote=True)
    name = escape(_backend_name(b, data))
    where = ("this machine" if same else
             f'<a target="_blank" rel="noopener" href="{url}/">{url} →</a>')
    head = (f'<div class="backend{"" if same else " remote"}" id="machine-{index}">'
            f'<p class="machine">machine · <b>{name}</b> · {where}</p>')
    if data is None:
        return head + (f'<section class="card"><p><span class="badge needs">unreachable</span> '
                       f'Could not reach the Cadre backend at <code>{url}</code>: '
                       f'{escape(err or "no answer")}. Its pages are still on that machine; '
                       'this section comes back when it answers.</p></section></div>')
    code = data.get("fleet_page_code") or {}
    own = SimpleNamespace(stale=lambda: bool(code.get("stale")),
                          since=code.get("since"), head=code.get("head"))
    body = render_recent(data.get("recent") or [], now) + render_fleet(
                        data.get("snap") or {}, now, statuses=data.get("statuses") or {},
                        graph=tuple(data.get("graph") or ([], {}, {})),
                        projs=data.get("projs") or {}, finished=data.get("finished") or {},
                        starting=data.get("starting") or {}, doing=data.get("doing") or {},
                        spent=data.get("spent") or {}, histories=data.get("histories") or {},
                        own=own, returned=data.get("returned") or {})
    return head + backends_mod.point_at(body, b, same) + '</div>'


def _known_keys() -> tuple[set, list[dict], dict]:
    snap = read_snapshot()
    surfaces = snap.get("surfaces") or []
    pages = conv_mod.load_pages()
    keys = {conv_mod.key_of(sf) for sf in surfaces if conv_mod.key_of(sf)} | set(pages)
    return keys, surfaces, pages


def _post_upstream_link(type_: str, frm: str, to: str):
    """review-surface's rule-checked link write path (build step 2), when the
    running server has it. None = not available here, fall back to the
    panel's log; a refusal from it is final."""
    req = urllib.request.Request(
        SURFACE + "/api/links", method="POST",
        data=json.dumps({"type": type_, "from": frm, "to": to}).encode(),
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        try:
            err = json.loads(e.read() or b"{}").get("error")
        except ValueError:
            err = ""
        raise conv_mod.LinkRefused(err or f"review-surface answered {e.code}")
    except (OSError, ValueError):
        return None


def apply_repair(action: str, key: str, form: dict) -> str:
    """The repairs the manage view and the fleet row menu offer. Returns what happened, in
    words for the driver; raises LinkRefused when a rule says no."""
    keys, surfaces, _ = _known_keys()
    if key not in keys:
        raise conv_mod.LinkRefused(f"no page {key!r} is known to the runner or review-surface")
    records, _ = conv_mod.load_links(conv_mod.link_files(PANEL_LINKS))
    if action == "move":
        project = form.get("project", "")
        if not conv_mod.PROJECT_NAME.match(project):
            raise conv_mod.LinkRefused("a project name is letters, digits, space, . _ - (max 64)")
        if conv_mod.check_link(records, "child-of", key, project) == "unchanged":
            return f"Already filed under {project}."
        conv_mod.append_link(PANEL_LINKS, "child-of", key, project)
        return f"Moved to {project}."
    if action == "replace":
        new = form.get("target", "")
        if new not in keys:
            raise conv_mod.LinkRefused("pick the page that replaces this one")
        if conv_mod.check_link(records, "supersedes", new, key) == "unchanged":
            return "Already marked replaced by that page."
        if _post_upstream_link("supersedes", new, key) is not None:
            return "Marked replaced (recorded in review-surface's link store)."
        conv_mod.append_link(PANEL_LINKS, "supersedes", new, key)
        return "Marked replaced (recorded in the panel's link log)."
    if action == "end":
        sf = next((s for s in surfaces if conv_mod.key_of(s) == key), None)
        if sf is None or not sf.get("artifact"):
            raise conv_mod.LinkRefused("that session is not open under the runner")
        if _CFG is None:
            raise conv_mod.LinkRefused("no runner config: cannot reach the runner's session store")
        surface_mod.end_session(_CFG, str(sf["artifact"]), lambda *_: None)
        return "Session ended."
    if action == "archive":
        # Archive is End session under a name that says what it is for: the
        # page leaves the scan for the project's finished list, its file stays,
        # and `review-surface <file>` reopens it. A runner-owned page ends
        # through the runner so its own record closes too; any other page
        # ends at the page server.
        sf = next((s for s in surfaces if conv_mod.key_of(s) == key), None)
        if sf is not None and sf.get("artifact") and _CFG is not None:
            surface_mod.end_session(_CFG, str(sf["artifact"]), lambda *_: None)
        else:
            req = urllib.request.Request(
                f"{SURFACE}/api/{urllib.parse.quote(key)}/end", method="POST", data=b"")
            try:
                with urllib.request.urlopen(req, timeout=3):
                    pass
            except (OSError, ValueError) as e:
                raise conv_mod.LinkRefused(f"the page server did not end it ({e})")
        _ARCHIVED.add(key)
        return "Archived. It is under the project's finished list."
    if action == "attach":
        sf = next((s for s in surfaces if conv_mod.key_of(s) == key), None)
        if sf is None or not sf.get("artifact"):
            raise conv_mod.LinkRefused("that page is not an open runner session")
        if _CFG is None:
            raise conv_mod.LinkRefused("no runner config: cannot reach the runner's session store")
        try:
            return surface_mod.request_attach(_CFG, str(sf["artifact"]),
                                              form.get("instruction", ""))
        except ValueError as e:
            raise conv_mod.LinkRefused(str(e))
    raise conv_mod.LinkRefused(f"unknown repair {action!r}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # journald noise control
        pass

    def _static_path(self):
        rel = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        p = (STATUS_DIR / rel).resolve()
        if p.is_relative_to(STATUS_DIR.resolve()) and p.is_file():
            return p
        return None

    def _serve_static(self, p: Path):
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(p.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":   # a HEAD body puts stray bytes on the wire
            self.wfile.write(data)

    def _proxy(self):
        if self.path.split("?", 1)[0] in BLOCKED:
            self.send_error(403, "blocked at the proxy")
            return
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)
        req = urllib.request.Request(SURFACE + self.path, data=body,
                                     method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in HOP_HEADERS:
                req.add_header(k, v)
        # Standard proxy identity: review-surface's origin guard validates the
        # browser's Origin against X-Forwarded-Host when that hostname is in
        # its REVIEW_SURFACE_ALLOWED_HOSTS list (set in the daemon's env).
        if self.headers.get("Host"):
            req.add_header("X-Forwarded-Host", self.headers["Host"])
            req.add_header("X-Forwarded-Proto", "http")
        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            resp = e
        except OSError:
            self.send_error(502, "review-surface is not running")
            return
        self.send_response(resp.status)
        upstream_path = self.path.split("?", 1)[0]
        for k, v in resp.headers.items():
            if k.lower() not in HOP_HEADERS:
                v = framable_header(upstream_path, k, v)
                if v is not None:
                    self.send_header(k, v)
        # 1xx/204/304 (and HEAD) responses MUST NOT carry a body — chunked
        # framing on them puts stray bytes on the wire and the browser reports
        # ERR_INVALID_HTTP_RESPONSE on the next request (observed).
        bodyless = (resp.status in (204, 304) or resp.status < 200
                    or self.command == "HEAD")
        if bodyless:
            self.end_headers()
            resp.close()
            return
        chunked = "content-length" not in {k.lower() for k in resp.headers}
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                # read1, not read: read(8192) waits for 8 KB, so a server-sent
                # event stream (review-surface's live reload, agent replies,
                # presence) sat in this buffer and never reached the browser.
                chunk = resp.read1(8192)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()  # SSE: deliver events as they arrive
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()

    # ---------------------------------------------------------------- home

    def _send_html(self, body: str, code: int = 200, headers: dict | None = None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_home(self, query: dict):
        """The fleet: every backend — this machine's included — fetched through
        the same API and rendered in its own section."""
        now = time.time()
        results = [(b, *backends_mod.fleet(b, now)) for b in self._backends()]
        fleet_html = render_machines(results) + "".join(
            render_backend(b, data, err, now, i) for i, (b, data, err) in enumerate(results))
        if query.get("partial"):
            self._send_html(fleet_html)
            return
        # The page chrome around the sections (snapshot time, spend, the new
        # project form) is this machine's, from the same payload.
        mine = next((d for _, d, _ in results if d and d.get("instance") == INSTANCE), {})
        notice = (query.get("notice") or [""])[0]
        self._send_html(render_home(mine.get("snap") or {}, notice=notice,
                                    projs=mine.get("projs") or {},
                                    week_usd=mine.get("week_usd"), fleet_html=fleet_html))

    def _backends(self) -> list[dict]:
        return backends_mod.with_self(BACKENDS, self.server.server_address)

    def _serve_api_fleet(self, version_only: bool):
        body = json.dumps(fleet_version() if version_only else fleet_payload(),
                          default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_project(self, pid: str, query: dict):
        projs = load_projects()
        p = projects_mod.resolve(projs, pid)
        if p is None:
            self.send_error(404, "no such project")
            return
        mine = {m["id"]: m for m in [p, *projects_mod.members(projs, p["id"])]}
        names = set().union(*(projects_mod.names_of(projs, k) for k in mine))
        snap = read_snapshot()
        graph = load_graph()
        rows = [r for r in conv_mod.collapse(snap.get("surfaces") or [], *graph)
                if str(r.get("project") or "") in names]
        # This project's section exactly as the fleet draws it, without the
        # finished count (the page lists finished work itself).
        doing = live_turns()
        turns = load_costs()[0]
        rows_html = render_fleet({"surfaces": rows}, statuses=fetch_agent_statuses(rows),
                                 projs={**mine, p["id"]: {**p, "group": None, "archived": False}},
                                 doing=doing, spent=costs_mod.per_task(turns),
                                 returned=recent_mod.last_returned(turns)
                                 ) if rows else ""
        closed_all = closed_pages(projs)
        closed = sorted((m for k in mine for m in closed_all.get(k, [])),
                        key=lambda m: -(m.get("opened") or 0))
        dirs = [d for m in mine.values() for d in m.get("dirs") or ()]
        entries = library_mod.scan(dirs=dirs[:1], skills_source=_skills_source())
        start_all = starting_tasks(projs)
        starting = [s for k in mine for s in start_all.get(k, [])]
        starting.sort(key=lambda s: -(s.get("since") or 0))
        self._send_html(render_project(p, projs, rows_html, closed, entries,
                                       notice=(query.get("notice") or [""])[0],
                                       starting=starting, doing=doing))

    def _serve_view(self, key: str):
        key = urllib.parse.unquote(key).strip("/")
        if not SESSION_KEY.fullmatch(key):
            self.send_error(404, "no such surface")
            return
        self._send_html(render_view(key))

    def _serve_costs(self, as_json: bool):
        summary = costs_mod.summarize(*load_costs())
        if not as_json:
            self._send_html(render_costs(summary))
            return
        body = json.dumps(summary).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_runs(self, query: dict):
        turns, owners, pages = load_runs()
        sel = {k: (query.get(k) or [""])[0].strip()[:200]
               for k in ("view", "sort", "project", "node", "q")}
        rows = recent_mod.rows(turns, owners, pages, view=sel["view"], sort=sel["sort"],
                               project=sel["project"], node=sel["node"], q=sel["q"])
        everything = recent_mod.rows(turns, owners, pages, view="turns")
        self._send_html(render_runs(
            rows, sel, sorted({r["project"] for r in everything if r["project"]}),
            sorted({r["node"] for r in everything if r["node"]})))

    def _serve_library(self):
        dirs = [d for p in projects_mod.live(load_projects()) for d in p.get("dirs") or ()]
        dirs = list(dict.fromkeys(dirs))
        self._send_html(render_library(
            library_mod.scan(dirs=dirs, skills_source=_skills_source()), dirs))

    def _form(self) -> dict | None:
        """A same-origin url-encoded POST body as {field: first value}, or
        None after answering the request with an error."""
        if not self._same_origin():
            self.send_error(403, "cross-origin post")
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_TASK_BYTES:
            self.send_error(413, "too large")
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return {k: v[0].strip() for k, v in
                urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _projects_post(self, path: str):
        """POST /projects (create: name, dirs one per line, group, context) ·
        /projects/<id>/tasks (launch: text, run, cwd) · /projects/<id>/archive
        (archived=1|0). Every outcome lands on a page with what happened."""
        form = self._form()
        if form is None:
            return
        parts = [p for p in path.split("/") if p][1:]
        if not _CFG:
            self._redirect("/", "No runner config: projects cannot be changed from here.")
            return
        if not parts:
            try:
                rec = projects_mod.create(_CFG.data_dir, form.get("name", ""),
                                          # dir1 is the completing field and
                                          # comes first: it is the directory
                                          # launched work runs in.
                                          dirs=([form.get("dir1", "").strip()]
                                                + form.get("dirs", "").splitlines()),
                                          group=form.get("group") or None,
                                          context=form.get("context", ""))
            except projects_mod.ProjectError as e:
                self._redirect("/", f"Project not created: {e}")
                return
            self._redirect(f"/project/{rec['id']}", f"Created {rec['name']}.")
            return
        pid, verb = urllib.parse.unquote(parts[0]), (parts[1] if len(parts) > 1 else "")
        back = f"/project/{urllib.parse.quote(pid)}"
        if verb == "archive":
            try:
                rec = projects_mod.archive(_CFG.data_dir, pid, form.get("archived") == "1")
            except projects_mod.ProjectError as e:
                self._redirect(back, f"Refused: {e}")
                return
            self._redirect(back, "Archived: hidden from the fleet, record kept."
                           if rec["archived"] else "Restored to the fleet.")
            return
        if verb != "tasks":
            self.send_error(404, "unknown project action")
            return
        if not form.get("text"):
            self._redirect(back, "A task needs some text.")
            return
        try:
            from runnerlib import tasks as tasks_mod
            result = tasks_mod.submit_task(_CFG, form["text"], form.get("cwd") or None,
                                           project=pid, run=form.get("run") or None)
        except Exception as e:                      # a bad launch must not 500 the page
            self._redirect(back, f"Not launched: {e}")
            return
        self._redirect(back, f"Launched {result}. Its page appears under running when "
                             "the first round finishes.")

    def _redirect(self, where: str, notice: str):
        self.send_response(303)
        self.send_header("Location", where + "?" + urllib.parse.urlencode({"notice": notice}))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _serve_fs(self, query: dict):
        """Directory completions for the project form, read live off this
        machine. Local-only like the rest of this server, and it returns
        directory names only — never file contents."""
        q = (query.get("q") or [""])[0][:400]
        body = json.dumps({"dirs": complete_dirs(q)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_find(self, query: dict):
        q = (query.get("q") or [""])[0].strip()[:200]
        snap = read_snapshot()
        hits = conv_mod.find(q, snap.get("surfaces") or [], *load_graph())
        self._send_html(render_found(q, hits))

    def _serve_page(self, key: str, query: dict):
        snap = read_snapshot()
        records, holders, pages = load_graph()
        surfaces = snap.get("surfaces") or []
        row = conv_mod.conversation_of(key, surfaces, records, holders, pages)
        if row is None:
            self.send_error(404, "unknown page")
            return
        rows = conv_mod.collapse(surfaces, records, holders, pages)
        projects = sorted({str(r["project"]) for r in rows if r.get("project")}
                          | {h for h in holders if h not in conv_mod.ROOT_HOLDERS})
        # "replaced by" offers the other live conversations, same project first
        cands = sorted((r for r in rows if conv_mod.key_of(r)),
                       key=lambda r: (r.get("project") != row.get("project"),
                                      -(r.get("opened") or 0)))
        cands = [{"key": conv_mod.key_of(r), "title": r.get("title") or conv_mod.key_of(r)}
                 for r in cands]
        notice = (query.get("notice") or [""])[0]
        owner = (surface_mod.ownership(_CFG, str(row["artifact"]))
                 if _CFG and row.get("artifact") else None)
        self._send_html(render_page(key, row, projects, cands, notice=notice,
                                    owner=owner))

    def _serve_stream(self):
        """The fleet stream: one SSE connection per open panel. It says
        `change` when any backend's version moved — this machine's included,
        polled through the same API as every other — and the page then
        re-fetches its fleet fragment. `source` reports whether every backend
        is answering; the page shows a half-live dot while one is not."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        stop = threading.Event()
        try:
            backends = self._backends()
            sig, seen_up = backends_mod.signature(backends)
            self._sse("hello", {"upstream": seen_up})
            last_write = time.time()
            while True:
                stop.wait(STREAM_TICK)
                now_sig, all_up = backends_mod.signature(backends)
                changed = now_sig != sig
                sig = now_sig
                if all_up != seen_up:
                    seen_up = all_up
                    self._sse("source", {"upstream": seen_up})
                    last_write = time.time()
                if changed:
                    self._sse("change", {})
                    last_write = time.time()
                elif time.time() - last_write > STREAM_PING:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_write = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            stop.set()

    def _sse(self, event: str, data: dict):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def _repair(self):
        """POST /repair: action=move (key, project) | replace (key, target —
        the page that replaces key) | end (key) | archive (key, back=home
        returns to the fleet) | attach (key, instruction). Every repair is checked
        against the link rules before anything is written, and lands back on
        the conversation's page with what happened."""
        if not self._same_origin():
            self.send_error(403, "cross-origin post")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_TASK_BYTES:
            self.send_error(413, "too large")
            return
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = {k: v[0].strip() for k, v in
                urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
        key = form.get("key", "")
        try:
            msg = apply_repair(form.get("action", ""), key, form)
        except conv_mod.LinkRefused as e:
            msg = f"Refused: {e}"
        if form.get("back") == "home" and not msg.startswith("Refused"):
            self._redirect_home(msg)    # archived from the fleet: stay on it
            return
        self.send_response(303)
        self.send_header("Location", f"/page/{urllib.parse.quote(key)}?"
                         + urllib.parse.urlencode({"notice": msg}))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _serve_dead_letter(self, name: str):
        rec = read_dead_letter(name)
        if rec is None:
            self.send_error(404, "no such dead letter")
            return
        self._send_html(render_dead_letter(name, rec))

    def _serve_history(self, key: str):
        snap = read_snapshot()
        sf = next((s for s in (snap.get("surfaces") or [])
                   if str(s.get("path") or "").rsplit("/", 1)[-1] == key), None)
        if sf is None:
            self.send_error(404, "unknown session")
            return
        self._send_html(render_history(sf, journal_batches(str(sf.get("artifact") or ""))))

    def _same_origin(self) -> bool:
        """A POST that changes the world only comes from this page. There is no
        session to forge against, but the proxy is reachable on the tailnet, so
        a cross-origin form post from a page in the same browser is the one real
        risk — an Origin that is not this host is refused. A missing Origin
        (curl, the CLI) is allowed: this is the same posture the surface proxy
        already takes."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("Host") or "").strip()
        return urllib.parse.urlsplit(origin).netloc == host

    def _new_task(self):
        """Form contract: POST /tasks, application/x-www-form-urlencoded,
        `text` (required) and `cwd` (optional). Hands off to
        runnerlib.tasks.submit_task(cfg, text, cwd) and redirects back home."""
        if not self._same_origin():
            self.send_error(403, "cross-origin post")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_TASK_BYTES:
            self.send_error(413, "task too large")
            return
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = urllib.parse.parse_qs(raw, keep_blank_values=True)
        text = (form.get("text") or [""])[0].strip()
        cwd = (form.get("cwd") or [""])[0].strip() or None
        if not text:
            self._redirect_home("A task needs some text.")
            return
        # Imported per request, not at module load: the task submitter is a
        # separate moving part and the page server must come up (and keep
        # proxying the surface) whether or not it is installed yet.
        try:
            from runnerlib import tasks as tasks_mod
        except ImportError:
            self._redirect_home("Task submission is not installed on this runner.")
            return
        try:
            result = tasks_mod.submit_task(_CFG, text, cwd)
        except Exception as e:                      # a bad task must not 500 the page
            self._redirect_home(f"Task rejected: {e}")
            return
        self._redirect_home(f"Task submitted{f': {result}' if result else '.'}")

    def _redirect_home(self, notice: str):
        # 303 so a refresh after submitting does not re-post the task.
        target = HOME_PATH + "?" + urllib.parse.urlencode({"notice": notice})
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # --------------------------------------------------------------- routing

    def _route(self):
        if self.command == "POST":
            backends_mod.forget()       # the next read shows what this write did
        path, _, raw_query = self.path.partition("?")
        query = urllib.parse.parse_qs(raw_query)
        if path == TASKS_PATH and self.command == "POST":
            self._new_task()
            return
        if path == REPAIR_PATH and self.command == "POST":
            self._repair()
            return
        if (path == PROJECTS_PATH or path.startswith(PROJECTS_PATH + "/")) \
                and self.command == "POST":
            self._projects_post(path)
            return
        if path.startswith("/project/") and self.command in ("GET", "HEAD"):
            self._serve_project(urllib.parse.unquote(path.split("/", 2)[2].strip("/")), query)
            return
        if path == RUNS_PATH and self.command in ("GET", "HEAD"):
            self._serve_runs(query)
            return
        if path == LIBRARY_PATH and self.command in ("GET", "HEAD"):
            self._serve_library()
            return
        if path in (COSTS_PATH, COSTS_PATH + ".json") and self.command in ("GET", "HEAD"):
            self._serve_costs(path.endswith(".json"))
            return
        if path in (backends_mod.FLEET_PATH, backends_mod.VERSION_PATH) \
                and self.command in ("GET", "HEAD"):
            self._serve_api_fleet(path == backends_mod.VERSION_PATH)
            return
        if path == STREAM_PATH and self.command == "GET":
            self._serve_stream()
            return
        if path == "/fs" and self.command in ("GET", "HEAD"):
            self._serve_fs(query)
            return
        if path == "/find" and self.command in ("GET", "HEAD"):
            self._serve_find(query)
            return
        if path.startswith("/page/") and self.command in ("GET", "HEAD"):
            self._serve_page(urllib.parse.unquote(path.rsplit("/", 1)[-1]), query)
            return
        if path.startswith(VIEW_PATH) and self.command in ("GET", "HEAD"):
            self._serve_view(path[len(VIEW_PATH):])
            return
        if path in ("", HOME_PATH) and self.command in ("GET", "HEAD"):
            self._serve_home(query)
            return
        if path.startswith("/stranded/") and self.command in ("GET", "HEAD"):
            self._serve_dead_letter(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/history/") and self.command in ("GET", "HEAD"):
            self._serve_history(path.rsplit("/", 1)[-1])
            return
        p = self._static_path()
        if p is not None:
            self._serve_static(p)
        else:
            self._proxy()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _route


def main():
    # One server per bind address: loopback for this machine, the tailnet
    # address for fleets on other machines. Any that fails to bind stops the whole process —
    # half-listening would look like a problem on the other machine.
    servers = [ThreadingHTTPServer((addr, PORT), Handler) for addr in BIND]
    print(f"statusd: serving {STATUS_DIR} on {', '.join(BIND)} port {PORT}, "
          f"proxying the rest to {SURFACE}"
          + f"; machine {MACHINE}; other backends: "
          + (", ".join(b["url"] for b in BACKENDS[1:]) or "none"),
          file=sys.stderr)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
