#!/usr/bin/env python3
"""Cadre page server: agent-manager home page + static assets + reverse proxy
to Review Surface.

Replaces the bare `python -m http.server` in cadre-status.service, same port,
per cadre-context decision 2026-08-26-surface-as-driver-channel: ONE exposed
port on the tailnet; review-surface keeps its loopback default and every
surface session is reached through this proxy.

Routing rule: `/` is the fleet home page — server-rendered here from the
daemon's status.json snapshot (one item per agent thread, the ones that want
a human on top) with the new-task form at the top;
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
from runnerlib import cards as cards_mod
from runnerlib import codestamp
from runnerlib import config as config_mod
from runnerlib import conversations as conv_mod
from runnerlib import costs as costs_mod
from runnerlib import items as items_mod
from runnerlib import library as library_mod
from runnerlib import backends as backends_mod
from runnerlib import projects as projects_mod
from runnerlib import recent as recent_mod
from runnerlib import session_tree as tree_mod
from runnerlib import surface as surface_mod
from runnerlib import theme as theme_mod
from runnerlib import wake as wake_mod
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
# Pages that may read this server's API and post its forms from another
# origin: the React app served by any configured backend (it switches
# between machines and talks to each one directly), plus any origin listed
# under [runner] cors_origins, e.g. a teammate's fleet that does not list
# this machine back. Everything else stays same-origin only.
CORS_ORIGINS = backends_mod.origins(BACKENDS) | set(
    backends_mod.origins([{"url": u} for u in (_CFG.runner if _CFG else {}).get("cors_origins") or []]))
BLOCKED = {"/shutdown"}
# The page server's "send" endpoint: a verdict or annotations queued for the
# session — the one proxied request the daemon has to act on.
SEND_RE = re.compile(r"^/api/[^/]+/prompts$")
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "host",
               "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "upgrade"}
TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json",
         ".css": "text/css", ".js": "text/javascript", ".png": "image/png",
         ".svg": "image/svg+xml", ".ico": "image/x-icon", ".woff2": "font/woff2"}

HOME_PATH = "/"
TASKS_PATH = "/tasks"
PARTIAL_SPLIT = "<!--cadre:work-->"
REPAIR_PATH = "/repair"
PROJECTS_PATH = "/projects"
LIBRARY_PATH = "/library"
COSTS_PATH = "/costs"
RUNS_PATH = "/runs"
TREE_PATH = "/tree"
ITEMS_PATH = "/items"
# rows on the fleet's "Recently returned" card, and all the API ships of them
RECENT_LIMIT = 6
STREAM_PATH = "/fleet/events"
# The React fleet UI (web/): its build output, served under /app/. Any path
# under it that is not a built file gets index.html, so the app's own routes
# survive a reload.
APP_PATH = "/app"
APP_DIR = Path(__file__).resolve().parent / "web" / "dist"
# What the React app's New work dialog reads beside /api/fleet (newwork_payload).
NEWWORK_API_PATH = "/api/newwork"
# The machines the React app's server switcher offers (backends_payload).
BACKENDS_API_PATH = "/api/backends"
# The React app's other screens, one read each: what the Python page at the
# same name (/library, /costs, /runs, /tree) draws from, as JSON.
SCREEN_API_PATHS = {"/api/library": "library", "/api/costs": "costs", "/api/runs": "runs",
                    "/api/tree": "tree"}
# rows the runs screen draws at most; the payload says how many there are
RUNS_LIMIT = 200
# A surface opens inside the fleet's own frame, so the fleet stays one click
# away. Opening review-surface's /session/<key> as the whole tab left the
# driver with nothing to click but the browser's back button.
VIEW_PATH = "/view/"
# Your deck (personal plus the team cards synced into it), and where a study
# session is started from a page or from the deck.
CARDS_PATH = "/cards"
STUDY_PATH = "/study"
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


_VIEW_CSS = theme_mod.fleet_root() + """
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
  font:14px/1.4 system-ui,sans-serif}
body{display:flex;flex-direction:column}
.bar{display:flex;gap:1rem;align-items:center;padding:.45rem .9rem;
  border-bottom:1px solid var(--border);flex:none}
.bar a{color:var(--accent);text-decoration:none}
.bar a:hover{text-decoration:underline}
.bar .title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:var(--muted)}
iframe{flex:1;border:0;width:100%;background:var(--bg)}
.bar .study{display:flex;gap:.35rem}
.bar .study input{background:var(--card-2);color:var(--fg);border:1px solid var(--border);
  border-radius:4px;padding:.2rem .45rem;width:16rem;font:inherit}
.bar .study button{background:var(--card-2);color:var(--accent);border:1px solid var(--border);
  border-radius:4px;padding:.2rem .6rem;font:inherit;cursor:pointer}
.bar .notice{color:var(--ok)}
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


def task_for_view(key: str, snap: dict | None = None) -> dict | None:
    """The snapshot's row for the page this key opens, when it is a dialogue's
    page: its task id, directory and file. The bar's study box needs them, and
    the snapshot is what this server reads instead of the registry."""
    snap = read_snapshot() if snap is None else snap
    for r in snap.get("surfaces") or []:
        if r.get("path") == SESSION_PREFIX + key and r.get("task"):
            return r
    return None


def render_view(key: str, row: dict | None = None, notice: str = "") -> str:
    """The fleet's frame around one surface: a bar with the way back, and the
    surface itself filling the rest of the window. On a dialogue's page the
    bar also starts a study session on a topic, now, beside that dialogue."""
    src = SESSION_PREFIX + escape(key)
    study = (f'<form class="study" method="post" action="{STUDY_PATH}">'
             '<input name="topic" required maxlength="300" '
             'placeholder="study a topic before you rule…">'
             f'<input type="hidden" name="from" value="{escape(str(row["task"]))}">'
             f'<input type="hidden" name="back" value="{VIEW_PATH}{escape(key)}">'
             '<button type="submit">study</button></form>') if row else ""
    note = f'<span class="notice">{escape(notice)}</span>' if notice else ""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            # The page server's ring mark. A fresh URL, because Chrome remembers an
            # icon URL that once failed and stops asking for it: /favicon.ico 404ed
            # here before the page server learned to serve it.
            '<link rel="icon" type="image/svg+xml" href="/favicon.ico?v=2">'
            f'<title>Surface — Cadre</title><style>{_VIEW_CSS}</style></head><body>'
            '<nav class="bar"><a href="/">← fleet</a>'
            '<span class="title" id="title"></span>'
            f'{note}{study}'
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


# Journal path -> (size, mtime_ns, {artifact: batches}). The fleet asks once per
# surface on every payload, so the file is parsed once per change, not per ask.
_JOURNALS: dict[Path, tuple[int, int, dict[str, list[dict]]]] = {}
_JOURNALS_LOCK = threading.Lock()


def _journal_index(journal: Path) -> dict[str, list[dict]]:
    """Every batch in the journal, by artifact. The lock is held across the
    parse so concurrent requests wait for one read instead of each doing it."""
    with _JOURNALS_LOCK:
        try:
            st = journal.stat()
            hit = _JOURNALS.get(journal)
            if hit and hit[:2] == (st.st_size, st.st_mtime_ns):
                return hit[2]
            index: dict[str, list[dict]] = {}
            for line in journal.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                index.setdefault(rec.get("file"), []).append(rec)
        except OSError:
            _JOURNALS.pop(journal, None)
            return {}
        _JOURNALS[journal] = (st.st_size, st.st_mtime_ns, index)
        return index


def journal_batches(artifact: str, state_dir: Path | None = None) -> list[dict]:
    """The per-surface answer history: every feedback batch the driver ever
    sent, from the feedback journal review-surface writes at accept time
    (append-only, survives delivery — this is the 'versions of my answers'
    record, already on disk). The journal lives beside review-surface's
    STATE file (one journal for all sessions), and each record carries the
    artifact path it belongs to — filter, don't glob."""
    if not artifact:
        return []
    root = Path(state_dir) if state_dir is not None else Path(
        os.environ.get("REVIEW_SURFACE_STATE_DIR")
        or Path.home() / ".review-surface")
    return list(_journal_index(root / "feedback-journal.jsonl").get(artifact, []))


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


def _tree_href(task: str) -> str:
    """/tree/<task>, quoted: a task id from another machine's payload is not
    guaranteed to be a slug."""
    return escape(f'{TREE_PATH}/{urllib.parse.quote(task, safe="")}')


def _tree_node(task: str, node: dict, now: float) -> str:
    """One session in a tree: what it is and how it stands, never its page."""
    # Only "working" is known for every machine's sessions; whether a quiet
    # one waits on the driver is the page server's to say, on the fleet row.
    state = '<span class="badge running">working</span>' if node.get("working") else ""
    step = escape(str(node.get("node") or "task"))
    if node.get("round"):
        step += f' · round {escape(str(node["round"]))}'
    page = (f'<a class="hist" href="{escape(view_href(node["page"]))}">page →</a>'
            if node.get("page") else "")
    return (f'<div class="tnode"><a class="ttitle" href="{_tree_href(task)}">'
            f'{escape(str(node.get("title") or task))}</a>{state}'
            f'<span class="badge">{step}</span>'
            f'<span class="badge">{escape(str(node.get("project") or ""))}</span>'
            f'<span class="badge">launched {escape(_rel_time(node.get("since") or 0, now))}</span>'
            f'{page}</div>')


def tree_list(task: str, nodes: dict, kids: dict, now: float, seen=None) -> str:
    """`task`'s children as nested lists, all the way down. Each child carries
    the first line of its last result, so the tree says what each did."""
    seen = set(seen or ()) | {task}
    items = []
    for child in kids.get(task) or ():
        if child in seen:
            continue
        node = nodes.get(child) or {}
        said = str(node.get("result") or "").strip().split("\n", 1)[0]
        line = f'<p class="tsaid">{escape(said[:200])}</p>' if said else ""
        items.append(f'<li>{_tree_node(child, node, now)}{line}'
                     f'{tree_list(child, nodes, kids, now, seen)}</li>')
    return f'<ul class="tree">{"".join(items)}</ul>' if items else ""


def tree_dropdown(task: str, nodes: dict, kids: dict, now: float) -> str:
    n = tree_mod.descendants(task, kids)
    return (f'<details class="kids"><summary>{n} child session{"s" if n != 1 else ""}'
            f'</summary>{tree_list(task, nodes, kids, now)}'
            f'<a class="hist" href="{_tree_href(task)}">the whole tree →</a></details>')


def load_tree(pages: dict | None = None) -> dict:
    """session_tree.load() with each session titled the way its newest page
    is (`pages` is load_runs()'s) — the title the author gave the page reads
    far better than the ask's first 80 characters."""
    pages = load_runs()[2] if pages is None else pages
    nodes = tree_mod.load(DATA_DIR)
    for t, n in nodes.items():
        title = (pages.get(t) or {}).get("title")
        if title:
            n["title"] = title
    return nodes


def render_tree(task: str, nodes: dict, now: float | None = None) -> str:
    """/tree/<task>: one session and everything it launched, without its
    review page. /tree alone: every session that launched others."""
    now = time.time() if now is None else now
    nodes = tree_mod.clean(nodes)
    kids = tree_mod.kids_of(nodes)
    if task:
        node = nodes[task]
        title = str(node.get("title") or task)
        trail = " › ".join(f'<a href="{_tree_href(a)}">'
                           f'{escape(str((nodes.get(a) or {}).get("title") or a))}</a>'
                           for a in tree_mod.ancestors(task, tree_mod.parents_of(nodes)))
        if trail:
            origin = f'launched by {trail}'
        elif node.get("launcher"):
            origin = (f'launched by session {escape(node["launcher"])}, which this '
                      'machine does not know as a task')
        else:
            origin = 'launched from outside any session'
        result = str(node.get("result") or "").strip()
        result_html = (f'<p class="tresult">{escape(result[:tree_mod.RESULT_CHARS])}</p>' if result else
                       '<p class="empty">No turn has finished yet.</p>')
        kids_html = (tree_list(task, nodes, kids, now) or
                     '<p class="empty">This session has not launched any others.</p>')
        n = tree_mod.descendants(task, kids)
        body = (f'<section class="card"><h2>{escape(title)}</h2>'
                f'{_tree_node(task, node, now)}<p class="meta">{origin}</p>{result_html}</section>'
                f'<section class="card"><h2>{n} child session{"s" if n != 1 else ""}</h2>'
                f'{kids_html}</section>')
    else:
        roots = [t for t in nodes if not nodes[t].get("parent") and kids.get(t)]
        roots.sort(key=lambda t: -(nodes[t].get("since") or 0))
        body = "".join(f'<section class="card">{_tree_node(t, nodes[t], now)}'
                       f'{tree_list(t, nodes, kids, now)}</section>' for t in roots) or (
            '<section class="card"><p class="empty">No session has launched another yet.'
            '</p></section>')
        title = "Session trees"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)} — tree</title><style>{_CSS}</style></head>'
            f'<body><div class="wrap"><header><h1>{escape(title)}<span class="dot">.</span> '
            f'tree</h1></header><p class="meta"><a href="/">← fleet</a> · '
            f'<a href="{TREE_PATH}">every tree</a></p>{body}</div></body></html>')


def orphan_surfaces(snap: dict) -> list[dict]:
    """Open sessions with no project to live under (ad-hoc notices, loose
    pages). They still want a human, so they render rather than being
    dropped."""
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
            "title": str(p.get("title") or ""),
            "project": p.get("project") or "", "since": ts,
            "active_runs": {}, "failed": failed.get(eid, "")})
    return out


def pageless_tasks(projs: dict, data_dir: Path | None = None) -> list[dict]:
    """Every task launched that has no page, newest launch first, whichever
    project it is in (`project` is "" when none claims it).

    A task becomes visible on the fleet only once its first round finishes and
    writes a page, which can be minutes. Until then pressing Launch changed
    nothing on screen, so the driver pressed it again — the report was four
    identical dialogues in one project. A launch has to leave a mark
    immediately, and so does a run that died before writing anything.

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
    out = []
    for task_id, rec in tasks.items() if isinstance(tasks, dict) else ():
        if not isinstance(rec, dict) or task_id in paged:
            continue
        proj = rec.get("project") or ""
        if not proj:
            owner = projects_mod.for_dir(projs, rec.get("cwd") or "")
            proj = owner["id"] if owner else ""
        out.append(
            {"task": task_id, "since": rec.get("since") or 0, "project": proj,
             "title": str(rec.get("title") or ""), "cwd": str(rec.get("cwd") or ""),
             "running": bool(rec.get("active_runs")),
             "ran": bool(rec.get("last_result")),
             # A session id is assigned before the process starts, so its
             # presence is the proof that something actually ran — which is
             # what separates "the runner never took this" from "it started
             # and died". Those two need different repairs.
             "started": bool(rec.get("session_id")),
             "failed": rec.get("failed") or ""})
    out.sort(key=lambda r: -(r.get("since") or 0))
    return out


def starting_tasks(projs: dict, data_dir: Path | None = None) -> dict:
    """{project id: [tasks launched but with no page yet]} — pageless_tasks()
    by project, for the project pages. One with no project is left out."""
    out: dict[str, list] = {}
    for row in pageless_tasks(projs, data_dir):
        if row["project"]:
            out.setdefault(row["project"], []).append(row)
    return out


def bare_endings(turns: list[dict], store: dict, busy) -> dict:
    """{task id: its newest finished turn} for each task whose newest turn
    ended without writing or rewriting any of its pages — a handoff or a
    continuation round that died, which otherwise leaves its item showing the
    last round as if nothing had happened. A task with a turn in flight
    (`busy`, task ids) is not judged yet.

    `store` is the page server's session store (sessions.json: {page file:
    meta with its task}) — every page each task ever wrote, not the fleet's
    collapsed rows, which fold a handoff's page into the dialogue it answers.
    A page counts as written in a turn when its file changed after the turn
    started (`ended - seconds`). A turn that wrote one and then failed is
    still reported when it failed (`ok` False). Each value carries ended, ok
    and node (the stage that ran)."""
    last: dict[str, dict] = {}
    for e in turns:
        task = str(e.get("story") or "")
        if task and float(e.get("ended") or 0) >= float(
                (last.get(task) or {}).get("ended") or 0):
            last[task] = e
    written: dict[str, float] = {}
    for art, meta in (store.items() if isinstance(store, dict) else ()):
        task = str((meta or {}).get("task") or "") if isinstance(meta, dict) else ""
        if not task or not art:
            continue
        try:
            written[task] = max(written.get(task, 0.0), Path(art).stat().st_mtime)
        except OSError:
            continue
    out = {}
    for task, e in last.items():
        if task in busy or task not in written:
            continue
        ended = float(e.get("ended") or 0)
        began = ended - float(e.get("seconds") or 0)
        if written[task] < began or e.get("ok") is False:
            out[task] = {"ended": ended, "ok": e.get("ok") is not False,
                         "node": str(e.get("stage") or "")}
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


def running_agents(data_dir: Path | None = None, now: float | None = None,
                   titles: dict | None = None, tree: dict | None = None) -> list[dict]:
    """One card per agent the runner has in flight, newest launch first, for
    the Running now strip — the old fleet status page said "Idle" while four
    agents ran, so the home page answers "what is running" itself.

    A card is JSON-shaped (a fleet on another machine renders it from the
    API): task, title, doing (live_turns' clause), agents (subagents touched
    lately), page and page_title (its newest page, "" before the first one is
    written), item and item_title (the root of its session tree — the line of
    work it belongs to), folder, started and deadline (epoch seconds), and
    waiting (launched, no slot yet). No node names: which node runs a turn is
    the runner's business, not the driver's.

    `titles` is load_runs()' pages ({task: newest page meta with its title});
    `tree` is load_tree(). Both default to empty, which only costs titles.
    Read-only and best-effort, like every registry read here.
    """
    d = DATA_DIR if data_dir is None else data_dir
    now = time.time() if now is None else now
    try:
        reg = json.loads((Path(d) / "registry.json").read_text(encoding="utf-8"))
        tasks = dict(reg.get("tasks") or {})
    except (OSError, ValueError):
        tasks = {}
    titles = titles or {}
    nodes = tree_mod.clean(tree)
    parents = tree_mod.parents_of(nodes)
    asked = requested_tasks(d)

    def _title(task: str) -> str:
        return str((titles.get(task) or {}).get("title") or (nodes.get(task) or {}).get("title")
                   or (asked.get(task) or {}).get("title") or _task_label(task))

    def _item(task: str) -> tuple[str, str]:
        root = (tree_mod.ancestors(task, parents) or [task])[0]
        return root, _title(root)

    cards = []
    for task_id, rec in tasks.items():
        if not isinstance(rec, dict) or not rec.get("active_runs"):
            continue
        run = next(iter(rec["active_runs"].values()), {}) or {}
        cwd = run.get("worktree") or rec.get("cwd") or ""
        sid = run.get("session_id") or rec.get("session_id") or ""
        snap = activity_mod.snapshot(cwd, sid, now) if cwd and sid else None
        page = titles.get(task_id) or {}
        item, item_title = _item(task_id)
        cards.append({"task": task_id, "title": _title(task_id),
                      "doing": activity_mod.phrase(snap, now),
                      "agents": int((snap or {}).get("agents") or 0),
                      "page": str(page.get("path") or ""),
                      "page_title": str(page.get("title") or ""),
                      "item": item, "item_title": item_title, "folder": cwd,
                      "started": float(run.get("started") or 0),
                      "deadline": float(run.get("deadline") or 0), "waiting": False})
    # A launch the runner has not given a slot yet is still something the
    # driver set running: it gets a card, or pressing Launch looks like nothing.
    for task_id, rec in asked.items():
        held = tasks.get(task_id)
        if rec.get("failed") or (isinstance(held, dict) and (
                held.get("active_runs") or held.get("session_id") or held.get("last_result"))):
            continue
        # Unheld past the grace is lost, not waiting (starting_rows says so on
        # its project); held by the registry with no run yet is waiting.
        if not held and now - float(rec.get("since") or 0) > STARTUP_GRACE:
            continue
        item, item_title = _item(task_id)
        cards.append({"task": task_id, "title": _title(task_id), "doing": "", "agents": 0,
                      "page": "", "page_title": "", "item": item, "item_title": item_title,
                      "folder": rec.get("cwd") or "", "started": float(rec.get("since") or 0),
                      "deadline": 0.0, "waiting": True})
    cards.sort(key=lambda c: -c["started"])
    return cards


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
/* One design system: the colours are runnerlib/theme.py's, the same ones
   surface-theme.css carries, so the fleet and every surface it opens read as
   one product. Dark, committed: the surfaces have no light mode. */
""" + theme_mod.fleet_root() + """*{box-sizing:border-box}
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
 padding:.5rem 1.1rem;background:var(--accent);color:var(--on-accent);cursor:pointer}
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
.kids{margin:.1rem 0 .4rem 1.2rem;font-size:.8rem;color:var(--muted)}
.kids > summary{cursor:pointer;font-family:var(--mono);font-size:.74rem;color:var(--label)}
.kids > summary:hover{color:var(--accent)}
ul.tree{list-style:none;margin:.3rem 0 .3rem .4rem;padding-left:.9rem;border-left:1px solid var(--border)}
ul.tree li{margin:.35rem 0}
.tnode{display:flex;flex-wrap:wrap;align-items:baseline;gap:.3rem .5rem}
.tnode .ttitle{color:var(--fg);text-decoration:none;font-size:.86rem}
.tnode .ttitle:hover{color:var(--accent)}
.tsaid{margin:.1rem 0 0;color:var(--muted);font-size:.78rem}
.tresult{white-space:pre-wrap;font-size:.86rem;color:var(--fg)}
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
.running-now .agentcard{border-top:1px solid var(--border);padding:.55rem 0}
.running-now .agentcard:first-of-type{border-top:0}
.running-now .rtitle{margin:0;display:flex;flex-wrap:wrap;gap:.35rem .6rem;align-items:baseline}
.running-now .rmeta{margin:.15rem 0 0;color:var(--muted);font-size:.8rem}
.items h2 .badge{margin-left:.6rem;text-transform:none;letter-spacing:0}
.items select.projfilter{float:right;text-transform:none;letter-spacing:0;font-size:.78rem;
 background:var(--card-2);color:var(--fg);border:1px solid var(--border);border-radius:6px}
details.item{display:block;border-top:1px solid var(--border)}
details.item > summary{display:flex;flex-wrap:wrap;align-items:baseline;gap:.4rem .7rem;
 padding:.6rem .5rem;cursor:pointer;list-style:none}
details.item > summary::-webkit-details-marker{display:none}
details.item > summary:hover{background:var(--soft)}
details.item > summary{font-family:var(--sans);color:var(--fg)}
details.item details summary::before{content:none}
details.item details summary{font-family:var(--mono)}
details.item > summary .title{flex:1 1 12rem;font-size:.9rem;color:var(--fg)}
details.item > summary .go{color:var(--accent);font-size:.78rem;font-family:var(--mono);text-decoration:none}
details.item .igroups{padding:0 .5rem .6rem 1.2rem}
details.item h4{margin:.5rem 0 .2rem;font-size:.68rem;text-transform:uppercase;
 letter-spacing:.08em;color:var(--label)}
.prow{display:flex;align-items:center;gap:.2rem}
.prow > a.row{flex:1;min-width:0}
details.item.offlist{display:none}
.unopened{color:var(--accent);font-size:.7rem}
.badge.tag{border-style:dashed}
.badge.state-done{border-color:var(--ok);color:var(--ok)}
.badge.state-idle,.badge.state-hidden{color:var(--label)}
form.itemctl{display:flex;flex-wrap:wrap;gap:.4rem;margin:.2rem 0 .6rem}
form.itemctl select{font-size:.78rem;background:var(--card-2);color:var(--fg);
 border:1px solid var(--border);border-radius:6px;padding:.15rem .3rem}
.itemactions{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.7rem}
.itemactions form{margin:0}
.itemactions button,.itemactions input{font-size:.75rem;background:var(--card-2);color:var(--fg);
 border:1px solid var(--border);border-radius:6px;padding:.2rem .5rem;cursor:pointer}
details.fold.idle > summary{padding:.5rem}
.running-now a{color:var(--accent);text-decoration:none}
.running-now .rmeta.folder{font-family:var(--mono);font-size:.72rem}
.running-now h2 .badge{margin-left:.6rem;text-transform:none;letter-spacing:0}
.dirwrap{position:relative;display:block}
.dirwrap .lbl{display:block;font-size:.78rem;color:var(--muted);margin:0 0 .2rem}
#dir1,#workdir{width:100%;font-family:ui-monospace,monospace;font-size:.88rem;
 padding:.45rem .6rem .45rem 1.7rem;border:1px solid var(--line);border-radius:6px;
 background:var(--card) url("data:image/svg+xml;charset=utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='%238a94a6'%3E%3Cpath d='M1.5 3.5a1 1 0 011-1h3.2l1.3 1.4h6.5a1 1 0 011 1v6.6a1 1 0 01-1 1h-11a1 1 0 01-1-1z'/%3E%3C/svg%3E") no-repeat .5rem 50%/ .9rem .9rem}
#dir1:focus,#workdir:focus{outline:2px solid var(--accent);outline-offset:-1px}
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
#runq:focus{outline:2px solid var(--accent);outline-offset:-1px}
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
details.fold.item > summary{font-family:var(--sans);color:var(--fg)}
details.item details > summary::before{content:none}
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
form.newtask select,form.newtask input[name=name],form.newtask input[name=context],
form.newtask input[name=tags]{
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
/* Phone width. A row is one unwrapping flex line, and its title (overflow
   hidden) is the only item allowed to shrink — so on a narrow screen the
   title went to zero width and the nowrap badges spilled past the edge,
   painting over the "earlier rounds" link and widening the page. Here the
   title takes its own line (two at most), the badges wrap under it, and the
   row's side links drop below instead of being painted over. */
@media (max-width:640px){
 .wrap{padding:1rem .9rem 2.5rem}
 header{flex-wrap:wrap;gap:.2rem .8rem}
 header h1{flex-basis:100%}
 header .updated{margin-left:0}
 .card{padding:.8rem .85rem}
 .backend{padding-left:.6rem}
 .project > h2, .card.recent h2{flex-wrap:wrap;row-gap:.3rem}
 .rowline{flex-wrap:wrap;column-gap:.4rem}
 .rowline > a.row{flex-basis:100%}
 .rowline > a.row + .hist{margin-left:auto}
 .rowline > a.row ~ .hist, .rowline > .rowmenu > summary{padding:.05rem .4rem .35rem}
 a.row, span.row.starting{flex-wrap:wrap;align-items:center;row-gap:.35rem}
 /* the whole row is the tap target; the "open →" label only cost a line */
 a.row .go{display:none}
 a.row .title, span.row.starting .title{flex:1 1 100%;white-space:normal;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2}
 .story .title{white-space:normal}
}
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

_HOME_CSS = """
/* The home page, after the round-2 mockup the driver approved: a header with
   + New work, Running now as a grid of cards, then Work as one list of items
   with a toolbar of chips. Tokens are _CSS's. */
body.home{font-size:14px;padding-bottom:4rem}
body.home .wrap{max-width:1000px;padding:22px 22px 0}
body.home header{align-items:center;gap:12px;margin-bottom:14px}
body.home header h1{font:600 20px/1 Georgia,serif}
body.home header .sp{flex:1}
body.home header a{color:var(--muted);font-size:12px;text-decoration:none}
body.home header a:hover{color:var(--accent)}
body.home button,body.home select,body.home input,body.home textarea,body.home .btn{
 font:inherit;color:var(--fg);background:var(--card-2);border:1px solid var(--border);
 border-radius:8px;padding:6px 10px}
body.home button,body.home .btn{cursor:pointer;text-decoration:none;display:inline-block;
 line-height:1.4}
body.home button.primary{background:color-mix(in srgb,var(--accent) 13%,transparent);border-color:var(--accent);color:var(--accent);
 font-weight:600}
body.home .ghost{background:transparent}
.notice{border:1px solid var(--accent);border-radius:8px;padding:6px 10px;font-size:12px;
 margin:0 0 12px;color:var(--fg)}
.home h2,.home h2.sect{font:600 11px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
 color:var(--muted);margin:18px 0 8px;display:flex;gap:8px;align-items:center}
.home h2 .n{color:var(--fg)}
.running-now .agents{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
 gap:8px;margin:0;font-size:inherit}
.card.agent{padding:10px 12px;display:flex;flex-direction:column;gap:4px;margin:0;border-radius:10px}
.agent .top{display:flex;gap:6px;align-items:center}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--run);animation:pulse 1.6s infinite;
 flex:none}
.agent.waiting .pulse{background:var(--label);animation:none}
@keyframes pulse{50%{opacity:.3}}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}}
.agent .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agent .doing{color:var(--run);font-size:12px}
.agent .pg{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.agent .pg a{color:var(--accent)}
.agent .pg a.under{color:var(--muted);text-decoration:none}
.agent .pg a.under b{color:var(--fg)}
.agent .nopage{color:var(--label)}
.bar{height:3px;background:var(--border);border-radius:2px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--run)}
.home .badge{font-size:11px;padding:1px 7px}
.badge.you{border-color:var(--accent);color:var(--accent)}
.badge.work{border-color:var(--run);color:var(--run)}
.badge.idle{border-style:dashed}
.badge.done{border-color:var(--ok);color:var(--ok)}
.badge.q{border-color:var(--hot);color:var(--hot)}
.tag{font-size:11px;color:var(--tag)}
.toolbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:10px 0}
.toolbar input[type=search]{flex:1 1 220px}
body.home #filter{margin:0;width:auto;padding:6px 10px;font-size:inherit;border-radius:8px;
 background:var(--card-2)}
.toolbar .hint,.hint{font-size:12px;color:var(--muted)}
.toolbar.chips:empty{display:none}
.chipbtn{font-size:12px!important;padding:3px 10px!important;border-radius:99px!important}
.chipbtn[aria-pressed=true]{border-color:var(--accent)!important;color:var(--accent)!important;
 background:color-mix(in srgb,var(--accent) 8%,transparent)!important}
.unseen{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent);flex:none}
.card.items{padding:0;overflow:visible;border-radius:10px}
.items details.item{border-top:1px solid var(--border)}
.items details.item:first-child{border-top:0}
.items details.item.offlist,.items .offbox details.item{display:none}
.items summary.row{display:flex;gap:8px;align-items:center;padding:9px 12px;cursor:pointer;
 list-style:none}
.items summary.row::-webkit-details-marker{display:none}
.items summary.row:hover{background:#ffffff06}
.items .caret{color:var(--muted);width:12px;flex:none}
.items .caret::before{content:'▸'}
.items details[open] > summary .caret::before{content:'▾'}
.items .row .title{font-weight:600;flex:1;min-width:0;white-space:nowrap;overflow:hidden;
 text-overflow:ellipsis;font-size:inherit;color:var(--fg)}
.items .row .proj{font:11px var(--mono);color:var(--muted)}
.items .row .when{font-size:12px;color:var(--muted);width:84px;text-align:right;flex:none}
.items .inside{padding:0 12px 12px 32px}
.items .grp{font:11px var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em;
 margin:8px 0 2px}
.items .pages{border-left:2px solid var(--border);padding-left:10px;margin:4px 0 8px}
.items .pages .pages{margin:0 0 2px 6px}
.items .page{display:flex;gap:8px;align-items:center;padding:3px 0;font-size:13px;
 flex-wrap:nowrap;white-space:nowrap}
.items .page > *{flex:none}
.items .page > a.t{flex:1 1 auto;min-width:3em;overflow:hidden;text-overflow:ellipsis}
.items .page a{color:var(--accent);text-decoration:none}
.items .pages:has(> .graph){border-left:none;padding-left:0}
.items .page.grow{height:24px;padding:0;gap:6px}
.items .grow svg{flex:none;display:block}
.items .grow .e{stroke:var(--muted);stroke-width:1.5;fill:none;opacity:.7}
.items .grow .n{fill:var(--card);stroke:var(--muted);stroke-width:1.5}
.items .grow .n.you{fill:var(--accent);stroke:var(--accent)}
.items .grow .n.new{fill:var(--ok);stroke:var(--ok)}
.items .grow .n.work{fill:var(--run);stroke:var(--run)}
.items .grow .n.bad{fill:var(--hot);stroke:var(--hot)}
.items .grow.done a.t{color:var(--muted)}
.items .grow .rn{font:11px var(--mono);color:var(--muted)}
@media (max-width:640px){.items .page .badge.cost,.items .page a.hist{display:none}}
.items .page a.hist{color:var(--label)}
.items .page .k{font:11px var(--mono);color:var(--muted);width:70px;flex:none}
.items .threads:empty{display:none}
.items details.earlier{margin:2px 0 4px}
.items details.earlier > summary{cursor:pointer;font:11px var(--mono);color:var(--label);
 list-style:none;padding:2px 0}
.items details.earlier > summary::-webkit-details-marker{display:none}
.items details.earlier > summary::before{content:'▸ '}
.items details.earlier[open] > summary::before{content:'▾ '}
.items .acts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.items .acts form{margin:0}
.items .acts input[name=tag]{padding:4px 8px;font-size:12px}
.items details.item:not([data-state=idle]) .cmu{display:none}
details.addtag{display:inline-flex;gap:6px;align-items:center}
details.addtag > summary{list-style:none}
details.addtag > summary::-webkit-details-marker{display:none}
details.addtag[open]{display:inline-flex}
.items .idlefold > summary.foldrow{padding:8px 12px;color:var(--muted);cursor:pointer;
 border-top:1px solid var(--border);background:#ffffff04;list-style:none;font-size:inherit}
.items .idlefold > summary::-webkit-details-marker{display:none}
.items .idlefold > summary::before{content:'▸ '}
.items .idlefold[open] > summary::before{content:'▾ '}
.items .idlefold[hidden]{display:none}
.items .idlebox details.item:first-child{border-top:1px solid var(--border)}
.items .noitems{padding:14px;margin:0}
.card.items[hidden]{display:none}
details.item[data-machine] .proj::after{content:' · ' attr(data-machine)}
dialog#newwork{background:var(--card);color:var(--fg);border:1px solid var(--border);
 border-radius:12px;width:min(560px,92vw)}
dialog#newwork::backdrop{background:#0008}
dialog#newwork h3{margin:0}
dialog#newwork label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}
dialog#newwork textarea,dialog#newwork input,dialog#newwork select{width:100%}
dialog#newwork .dirwrap .lbl{font-size:12px;margin:10px 0 4px}
.sugg{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.sugg button{font:12px var(--mono)!important;padding:2px 8px!important}
.dlgacts{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}
details.newproject{margin-top:22px}
.home .backend{border:0;padding:0;margin:0}
@media (max-width:640px){
 body.home .wrap{padding:14px 12px 0}
 .items summary.row{flex-wrap:wrap}
 .items .row .title{flex:1 1 calc(100% - 40px);white-space:normal}
 .items .row .when{width:auto}
 .items .inside{padding-left:16px}
}
"""


_PAGE_JS = """
(function(){
 var FALLBACK=%d, SPLIT='%s', KEY='cadre.home.v1';
 var LABEL={working:'Agent working','your-turn':'Your turn',idle:'Idle',done:'Done',hidden:'Hidden'};
 var CLS={'your-turn':'you',idle:'idle',done:'done',hidden:'idle'};
 var CHIPS=[['your-turn','Your turn'],['working','Agent working'],['idle','Idle'],
  ['unopened','Unopened'],['done','Done'],['hidden','Hidden']];
 var HOME=['your-turn','working','idle'];
 function $(id){return document.getElementById(id)}
 function esc(t){return String(t==null?'':t).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
 // The view: which states show, the tag, the project, the sort and the home
 // limit. Kept in this browser; a link with ?show= ?sort= ?tag= ?limit= wins.
 var sel={show:HOME.slice(),unopened:false,tag:'',proj:'',sort:'',limit:0,idleOpen:false};
 try{var kept=JSON.parse(localStorage.getItem(KEY)||'{}');for(var k in kept)sel[k]=kept[k];}catch(e){}
 (function(){
  var q=new URLSearchParams(location.search);
  if(q.get('sort'))sel.sort=q.get('sort');
  if(q.get('limit'))sel.limit=+q.get('limit')||0;
  if(q.has('tag'))sel.tag=q.get('tag');
  if(q.has('show')){var s=q.get('show');sel.unopened=s==='unopened';
   sel.show=s==='done'?['done']:s==='hidden'?['hidden']:(s==='all'||s==='unopened')?
    HOME.concat(['done','hidden']):HOME.slice();}
 })();
 function save(){try{localStorage.setItem(KEY,JSON.stringify(sel))}catch(e){}}
 function n(el,k){return parseFloat(el.dataset[k])||0}
 function tagsOf(el){return el.dataset.tags?el.dataset.tags.split(','):[]}
 function lists(){return [].slice.call(document.querySelectorAll('#fleet .card.items'))}
 // Every machine draws its own list; the page shows them as one.
 function merge(){
  var all=lists();if(all.length<2)return;
  all.slice(1).forEach(function(l){
   l.querySelectorAll('details.item').forEach(function(it){all[0].querySelector('.offbox').appendChild(it)});
   l.hidden=true;
  });
 }
 function badge(el,st){
  var b=el.querySelector('.stbadge');if(!b)return;
  var a=n(el,'agents');
  b.innerHTML=st==='working'?'<span class="badge work">'+(a?a+' agent'+(a>1?'s':'')+' running':'Agent working')+'</span>'
   :'<span class="badge '+CLS[st]+'">'+LABEL[st]+'</span>';
 }
 function layout(){
  merge();
  var list=lists()[0];if(!list)return;
  var items=[].slice.call(document.querySelectorAll('#fleet details.item'));
  var limitSel=$('limit'),limit=sel.limit||(limitSel?+limitSel.value:15)||15;
  if(limitSel&&sel.limit){
   if(![].some.call(limitSel.options,function(o){return +o.value===sel.limit}))limitSel.add(new Option(sel.limit,sel.limit));
   limitSel.value=String(sel.limit);
  }
  // Idle is by count, never by age: of the items not done or hidden, the
  // limit most recently touched stay; an item an agent works on never idles.
  var live=items.filter(function(e){return !e.dataset.store});
  var busy=live.filter(function(e){return e.dataset.working==='1'}).length;
  var quiet=live.filter(function(e){return e.dataset.working!=='1'})
   .sort(function(a,b){return n(b,'touched')-n(a,'touched')});
  var room=Math.max(0,limit-busy),count={unopened:0};
  items.forEach(function(e){
   var st=e.dataset.store||(e.dataset.working==='1'?'working':quiet.indexOf(e)<room?'your-turn':'idle');
   e.dataset.state=st;badge(e,st);count[st]=(count[st]||0)+1;
   if(e.dataset.unopened==='1')count.unopened++;
  });
  chips(count,items);
  var f=$('filter'),q=(f&&f.value||'').trim().toLowerCase();
  var pf=$('proj'),proj=pf?pf.value:'';
  var shown=items.filter(function(e){
   return (q||sel.show.indexOf(e.dataset.state)>-1)&&(!sel.unopened||e.dataset.unopened==='1')
    &&(!sel.tag||tagsOf(e).indexOf(sel.tag)>-1)&&(!proj||e.dataset.project===proj)
    &&(!q||e.textContent.toLowerCase().indexOf(q)>-1);
  });
  var sort=$('sort')?$('sort').value:'needs';
  var shelf=function(e){return e.dataset.state==='done'||e.dataset.state==='hidden'?1:0};
  shown.sort(sort==='result'?function(a,b){return n(b,'returned')-n(a,'returned')}
   :sort==='untouched'?function(a,b){return n(a,'touched')-n(b,'touched')}
   :sort==='spent'?function(a,b){return n(b,'spent')-n(a,'spent')}
   :function(a,b){return shelf(a)-shelf(b)||n(a,'needs')-n(b,'needs')
    ||(n(b,'returned')||n(b,'touched'))-(n(a,'returned')||n(a,'touched'))});
  var fold=list.querySelector('.idlefold'),box=fold.querySelector('.idlebox'),
      off=list.querySelector('.offbox'),none=list.querySelector('.noitems');
  items.forEach(function(e){if(shown.indexOf(e)<0){e.classList.add('offlist');off.appendChild(e);}});
  var idle=0;
  shown.forEach(function(e){
   e.classList.remove('offlist');
   if(e.dataset.state==='idle'){box.appendChild(e);idle++;}else list.insertBefore(e,none);
  });
  fold.hidden=!idle;fold.querySelector('.n').textContent=idle;
  fold.open=!!idle&&(sel.idleOpen||!!q);
  none.hidden=!!shown.length;
  if(pf){ // projects only the items know of (another machine's, or by name)
   items.forEach(function(e){var v=e.dataset.project;
    if(v&&![].some.call(pf.options,function(o){return o.value===v}))pf.add(new Option(e.dataset.projname||v,v));});
   if(sel.proj&&pf.value!==sel.proj&&[].some.call(pf.options,function(o){return o.value===sel.proj}))pf.value=sel.proj;
  }
 }
 function chips(count,items){
  var s=$('states');if(!s)return;s.innerHTML='';
  CHIPS.forEach(function(c){
   var on=c[0]==='unopened'?sel.unopened:sel.show.indexOf(c[0])>-1;
   var b=document.createElement('button');b.type='button';b.className='chipbtn';
   b.setAttribute('aria-pressed',on);
   b.innerHTML=(c[0]==='unopened'?'<span class="unseen"></span> ':'')+esc(c[1])+' '+(count[c[0]]||0);
   b.onclick=function(){
    if(c[0]==='unopened')sel.unopened=!sel.unopened;
    else{var i=sel.show.indexOf(c[0]);if(i>-1)sel.show.splice(i,1);else sel.show.push(c[0]);}
    save();layout();
   };
   s.appendChild(b);
  });
  var t=$('tags');if(!t)return;t.innerHTML='';
  var all={};items.forEach(function(e){tagsOf(e).forEach(function(x){all[x]=1})});
  var names=Object.keys(all).sort();
  if(sel.tag&&!all[sel.tag])names.push(sel.tag);
  if(!names.length)return;
  t.innerHTML='<span class="hint">tags:</span>';
  names.forEach(function(x){
   var b=document.createElement('button');b.type='button';b.className='chipbtn';
   b.setAttribute('aria-pressed',sel.tag===x);b.textContent='#'+x;
   b.onclick=function(){sel.tag=sel.tag===x?'':x;save();layout();};
   t.appendChild(b);
  });
 }
 ['sort','proj','limit'].forEach(function(id){
  var el=$(id);if(!el)return;
  if(id==='sort'&&sel.sort&&[].some.call(el.options,function(o){return o.value===sel.sort}))el.value=sel.sort;
  el.addEventListener('change',function(){
   if(id==='sort')sel.sort=el.value;else if(id==='proj')sel.proj=el.value;else sel.limit=+el.value;
   save();layout();
  });
 });
 var bar=$('workbar');if(bar)bar.addEventListener('submit',function(e){e.preventDefault();});
 document.addEventListener('toggle',function(e){
  if(e.target.classList&&e.target.classList.contains('idlefold')){
   var f=$('filter');if(!(f&&f.value.trim()))sel.idleOpen=e.target.open;save();}
 },true);
 // Find across everything: the list filter above only sees what is listed;
 // this asks the server about every page ever opened, folded rounds included.
 var findT=null;
 function findEverywhere(){
  var f=$('filter'),box=$('found');
  if(!f||!box)return;
  var q=f.value.trim();
  if(q.length<2){box.hidden=true;box.innerHTML='';return;}
  fetch('/find?q='+encodeURIComponent(q),{cache:'no-store'}).then(function(r){return r.text()})
   .then(function(h){if(f.value.trim()!==q)return;box.innerHTML=h;box.hidden=false;})
   .catch(function(){});
 }
 var f=$('filter');
 if(f)f.addEventListener('input',function(){layout();clearTimeout(findT);findT=setTimeout(findEverywhere,200);});
 // New work: the header button, an item's "New work under this", or a link
 // with ?parent= opens the dialog; the folder says which project it files under.
 var dlg=$('newwork');
 function openWork(parent){
  if(!dlg)return;
  var u=$('under');if(u&&parent!=null)u.value=parent;
  if(dlg.showModal&&!dlg.open)dlg.showModal();else dlg.setAttribute('open','');
  hint();var w=$('what');if(w)w.focus();
 }
 var owners={};try{owners=JSON.parse(($('owners')||{}).textContent||'{}')}catch(e){}
 function hint(){
  var i=$('workdir'),h=$('projhint');if(!i||!h)return;
  var v=i.value.trim().replace(/\\/+$/,'');
  if(!v){h.textContent='The project is the one that owns this folder; a folder no project owns becomes a new project.';return;}
  if(v[0]==='~')v=(owners.home||'')+v.slice(1);
  var best=null;(owners.dirs||[]).forEach(function(d){
   if((v===d[0]||v.indexOf(d[0]+'/')===0)&&(!best||d[0].length>best[0].length))best=d;});
  h.textContent=best?'Files under project “'+best[1]+'” (from the folder).'
   :'No project owns this folder yet: it starts one named after the folder.';
 }
 var wd=$('workdir');if(wd){wd.addEventListener('input',hint);wd.addEventListener('change',hint);}
 document.addEventListener('click',function(e){
  var t=e.target.closest?e.target:null;if(!t)return;
  if(t.closest('#newworkbtn')){openWork('');return;}
  if(t.closest('#cancelwork')){dlg.close?dlg.close():dlg.removeAttribute('open');return;}
  var d=t.closest('.sugg button[data-dir]');
  if(d&&wd){wd.value=d.dataset.dir;hint();return;}
  var u=t.closest('a.nwu[data-parent]');
  if(u&&!u.target){e.preventDefault();openWork(u.dataset.parent);}
 });
 if(dlg&&dlg.hasAttribute('data-open'))openWork(null);
 // An item's done, hide and tag change in place: the store is written, then
 // the list is re-fetched, so the driver never loses their place.
 document.addEventListener('submit',function(e){
  var fm=e.target;if(!fm.matches||!fm.matches('#fleet form[action="/items"]'))return;
  e.preventDefault();
  // getAttribute: the form's own field named "action" shadows fm.action
  fetch(fm.getAttribute('action'),{method:'POST',body:new URLSearchParams(new FormData(fm)),redirect:'manual'})
   .then(function(){refresh();}).catch(function(){fm.submit();});
 });
 // Live: the fleet stream says when the runner's snapshot, a review page or
 // a link changed; only then are Running now and the list re-fetched.
 function refresh(){
  fetch('/?partial=1'+(location.search?'&'+location.search.slice(1):''),{cache:'no-store'})
   .then(function(r){return r.text()})
   .then(function(h){
    var el=$('fleet'),run=$('fleet-running');if(!el)return;
    var i=h.indexOf(SPLIT);if(i<0)return;
    var open={};  // which items are open is DOM state — carry it across the swap
    el.querySelectorAll('details.item[open]').forEach(function(d){open[d.dataset.fold]=1});
    if(run)run.innerHTML=h.slice(0,i);
    el.innerHTML=h.slice(i+SPLIT.length);
    el.querySelectorAll('details.item').forEach(function(d){if(open[d.dataset.fold])d.open=true});
    layout();
   })
   .catch(function(){});
 }
 var soonT=null;function soon(){clearTimeout(soonT);soonT=setTimeout(refresh,250);}
 var live=$('live'),poll=null;
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
 layout();
})();
""" % (FALLBACK_POLL_MS, PARTIAL_SPLIT, STREAM_PATH)


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
                 own=None, returned: dict | None = None,
                 tree: dict | None = None, by_item: bool = False,
                 view: dict | None = None, item_store: dict | None = None,
                 running: list | None = None, endings: dict | None = None,
                 pageless: list | None = None) -> str:
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
    page. `tree` is session_tree.load(): which session launched which, drawn
    as a dropdown under every row that launched others. `by_item` draws the
    home page's shape instead of project sections: one item per agent thread
    (see fleet_items), filtered and sorted by `view` (show, sort, tag, limit,
    from the home page's query) with `item_store` (items.load) supplying tags,
    done and hidden; `running` (running_agents()), `endings` (bare_endings())
    and `pageless` (pageless_tasks()) say which items have agents on them and
    which had a run end without a page."""
    now = time.time() if now is None else now
    statuses = statuses or {}
    tree = tree_mod.clean(tree)
    kids = tree_mod.kids_of(tree)
    doing = doing or {}
    spent = spent or {}
    returned = returned or {}
    records, holders, pages = graph or ([], {}, {})
    snap = {**snap, "surfaces": conv_mod.collapse(snap.get("surfaces") or [],
                                                  records, holders, pages)}
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
        return (f'<span class="badge cost" title="what this task\'s finished turns cost">'
                f'{escape(costs_mod.usd(usd))}</span>' if usd else "")

    def _history_link(sf) -> str:
        art = str(sf.get("artifact") or "")
        n = (int(histories.get(art) or 0) if histories is not None
             else len(journal_batches(art)))
        if not n:
            return ""
        key = str(sf.get("path") or "").rsplit("/", 1)[-1]
        return f'<a class="hist" href="/history/{escape(key)}">history ({n})</a>'

    def _page_link(sf, rounds_link: bool = True) -> str:
        # Earlier rounds are reachable from the row, never listed in the scan.
        # The ⋮ menu is the row's manage control: Archive (one click, back to
        # the fleet). The rarer repairs (move, mark replaced, attach) stay on
        # /page/<key>, reached from the earlier-rounds link and from find.
        key = escape(str(sf.get("path") or "").rsplit("/", 1)[-1])
        n = len(sf.get("earlier") or [])
        rounds = (f'<a class="hist" href="/page/{key}">'
                  f'{n} earlier round{"s" if n != 1 else ""}</a>') if n and rounds_link else ""
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

    def _parent_link(sf) -> str:
        parent = (tree.get(str(sf.get("task") or "")) or {}).get("parent")
        if not parent:
            return ""
        title = escape(str((tree.get(parent) or {}).get("title") or parent))
        return (f'<a class="hist" href="{_tree_href(parent)}" '
                f'title="launched by {title}">↳ parent</a>')

    def _kids_dropdown(sf) -> str:
        # An orchestrator's children are a dropdown under its row, recursive,
        # so the tree it built is readable without opening any page.
        task = str(sf.get("task") or "")
        return tree_dropdown(task, tree, kids, now) if kids.get(task) else ""

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
                       f'{_parent_link(sf)}{_history_link(sf)}{_page_link(sf)}</div>'
                       f'{_kids_dropdown(sf)}')
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

    if by_item:
        return code_line(snap, own=own, now=now) + _items_html(
            snap, now, tree, kids, projs or {}, doing, statuses, returned,
            {"live": _live, "spent": _spent, "history": _history_link,
             "menu": _page_link, "opened": _opened},
            spent=spent, view=view or {}, store=item_store or {}, running=running,
            endings=endings, pageless=pageless)

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
                f'{_parent_link(sf)}{_history_link(sf)}{_page_link(sf)}</div>{_kids_dropdown(sf)}')

    if orphans:
        rows = "".join(_orow(sf, str(sf.get("title") or sf.get("task") or sf.get("story")
                                     or sf.get("kind") or "surface")) for sf in orphans)
        out.append(f'<section class="card"><h2>Loose sessions</h2>{rows}</section>')
    if not out:
        out.append('<section class="card"><p class="empty">Nothing in flight. '
                   'Start something with the box above.</p></section>')
    return code_line(snap, own=own, now=now) + "".join(out)


def render_inbox(data: dict | None = None, now: float | None = None) -> str:
    """Track A's slot: questions agents ask mid-run, drawn above Running now.
    Empty until track A lands; the call site is here so it lands as a fill-in,
    not a layout change."""
    return ""


def render_footer_hooks() -> str:
    """Track B's slot at the foot of the home page (the message-thread
    messenger). Empty until track B lands."""
    return ""


def _home_path(p: str) -> str:
    """A folder as the driver would type it: ~ for the home directory."""
    home = str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


def running_cards_html(cards: list[dict], now: float | None = None) -> tuple[str, int, int]:
    """(cards html, running, waiting) for the Running now strip: one card per
    running_agents() card — what it is doing, its page, the item it is under,
    its folder and its clock against the deadline. No node names."""
    now = time.time() if now is None else now
    out, n_run, n_wait = [], 0, 0
    for c in cards:
        if not isinstance(c, dict) or not c.get("task"):
            continue
        title = escape(str(c.get("title") or c["task"]))
        if c.get("waiting"):
            n_wait += 1
            doing = "waiting for a slot"
        else:
            n_run += 1
            doing = str(c.get("doing") or "working")
            n = int(c.get("agents") or 0)
            if n and "agent" not in doing:
                doing = f"{n} subagent{'s' if n != 1 else ''} · {doing}"
        page = (f'<a href="{escape(view_href(c["page"]))}">'
                f'{escape(str(c.get("page_title") or "its page"))}</a>'
                if c.get("page") else '<span class="nopage">not written yet</span>')
        under = ("" if c.get("item") == c.get("task") else
                 f' · under <a class="under" href="{_tree_href(str(c["item"]))}">'
                 f'<b>{escape(str(c.get("item_title") or c["item"]))}</b></a>')
        started, deadline = float(c.get("started") or 0), float(c.get("deadline") or 0)
        used = int(max(0.0, now - started) // 60) if started else 0
        budget = int((deadline - started) // 60) if deadline > started else 0
        clock = f"{used} min of {budget}" if budget else f"{used} min"
        pct = min(100, int(used * 100 / budget)) if budget else 0
        out.append(f'<div class="card agent{" waiting" if c.get("waiting") else ""}">'
                   f'<div class="top"><span class="pulse"></span>'
                   f'<span class="t" title="{title}">{title}</span></div>'
                   f'<div class="doing">{escape(doing)}</div>'
                   f'<div class="pg">page: {page}{under}</div>'
                   f'<div class="pg" title="minutes this turn has used, against its deadline">'
                   f'{escape(_home_path(str(c.get("folder") or "")))} · {escape(clock)}</div>'
                   f'<div class="bar"><i style="width:{pct}%"></i></div></div>')
    return "".join(out), n_run, n_wait


def running_section(body: str, n_run: int, n_wait: int, slots: int = 0) -> str:
    """The strip around running_cards_html(): a count against the runner's
    slots, and a plain sentence when nothing runs — an absent strip reads the
    same as a broken one."""
    count = f"{n_run} of {slots} slots" if slots else str(n_run)
    if n_wait:
        count += f" · {n_wait} waiting for a slot"
    inner = body or '<p class="empty">No agent is running.</p>'
    return (f'<section class="running-now"><h2>Running now <span class="n">{count}</span></h2>'
            f'<div class="agents">{inner}</div></section>')


def render_running(cards: list[dict], now: float | None = None, slots: int = 0) -> str:
    """The Running now strip for one machine's cards, above the work list."""
    return running_section(*running_cards_html(cards, now), slots)


def _item_root(task: str, parents: dict) -> str:
    return (tree_mod.ancestors(task, parents) or [task])[0]


def fleet_items(surfaces: list[dict], tree: dict, projs: dict,
                statuses: dict | None = None, returned: dict | None = None,
                pageless: list | None = None) -> list[dict]:
    """The home page's items: one per agent thread, newest touch first.

    An item is a root task of the session tree plus everything started from
    it; a task nothing started and that started nothing is its own item. Each
    collapsed page (conversations.collapse has already folded a dialogue's
    rounds into one) files under the item its task belongs to; a page with no
    task is an item of its own. A launched task with no page (`pageless`,
    pageless_tasks()) is an item too when nothing else holds its tree: a run
    that died before writing anything must still be on the list. Returns dicts
    carrying key (the root task id, or "page:<key>"), title, head (the page
    the item opens, None when it has none), pages, cwd, project (the durable
    project id, "" when none claims it) and touched (epoch).
    """
    statuses, returned = statuses or {}, returned or {}
    parents = tree_mod.parents_of(tree)
    by_name = {n: pid for pid in projs for n in projects_mod.names_of(projs, pid)}
    items: dict[str, dict] = {}
    for sf in surfaces:
        if not sf.get("path") or conv_mod.key_of(sf) in _ARCHIVED:
            continue
        task = str(sf.get("task") or "")
        key = _item_root(task, parents) if task else f'page:{conv_mod.key_of(sf)}'
        it = items.setdefault(key, {"key": key, "pages": []})
        it["pages"].append(sf)
    out = []
    for key, it in items.items():
        pages = sorted(it["pages"], key=lambda sf: -(sf.get("opened") or 0))
        head = next((sf for sf in pages if str(sf.get("task") or "") == key), pages[0])
        # The durable project's id when one claims the page, else the name
        # the page was registered under: either way the filter can reach it.
        pid = by_name.get(str(head.get("project") or ""))
        if pid is None:
            owner = projects_mod.for_dir(projs, head.get("cwd") or "")
            pid = owner["id"] if owner else str(head.get("project") or "")

        def _touch(sf):
            st = statuses.get(str(sf.get("path") or "").rsplit("/", 1)[-1]) or {}
            return max(float(sf.get("opened") or 0), _iso_epoch(st.get("updated_at")),
                       float(returned.get(str(sf.get("task") or "")) or 0))
        out.append({"key": key, "head": head, "pages": pages, "project": pid,
                    "cwd": str(head.get("cwd") or ""),
                    "title": str((tree.get(key) or {}).get("title") or head.get("title")
                                 or head.get("task") or head.get("kind") or "page"),
                    "touched": max(_touch(sf) for sf in pages)})
    held = {it["key"] for it in out}
    for row in pageless or ():
        key = _item_root(str(row.get("task") or ""), parents)
        if not row.get("task") or key in held:
            continue
        held.add(key)
        top = row if key == row["task"] else {}
        out.append({"key": key, "head": None, "pages": [], "project": str(row.get("project") or ""),
                    "cwd": str(row.get("cwd") or ""),
                    "title": str((tree.get(key) or {}).get("title") or top.get("title")
                                 or _task_label(key)),
                    "touched": max(float(row.get("since") or 0),
                                   float(returned.get(row["task"]) or 0))})
    out.sort(key=lambda it: -it["touched"])
    return out


def parent_choices(data: dict) -> list[tuple[str, str]]:
    """[(item key, title)] a new task can be filed under, newest first, from
    this machine's fleet payload. Only task items: a lone page has no task to
    be a parent."""
    snap, graph = data.get("snap") or {}, data.get("graph") or ([], {}, {})
    surfaces = conv_mod.collapse(snap.get("surfaces") or [], *graph)
    items = fleet_items(surfaces, tree_mod.clean(data.get("tree")), data.get("projs") or {},
                        pageless=data.get("pageless") or [])
    return [(it["key"], it["title"]) for it in items if not it["key"].startswith("page:")]


def _page_state(sf: dict, statuses: dict, now: float) -> tuple[str, str]:
    """agent_state_badge for one fleet row, with render_fleet's ownership rule."""
    owned = (None if sf.get("kind") in (None, "page") or not sf.get("artifact")
             else not surface_mod.unowned(sf))
    return agent_state_badge(statuses.get(conv_mod.key_of(sf) or ""), now, owned=owned)


def pageless_phrase(row: dict, now: float) -> str:
    """What became of a launch with no page, in starting_rows' words; "" while
    it runs (the Running now strip has it)."""
    if row.get("running"):
        return ""
    if row.get("ran"):
        return "ran, but wrote no page"
    if row.get("failed"):
        return f"could not start — {_short_error(row['failed'])}"
    if row.get("started"):
        return "started, then stopped before writing a page"
    if now - float(row.get("since") or 0) > STARTUP_GRACE:
        return "never started"
    return "queued"


def ending_phrase(e: dict, now: float) -> str:
    """A bare_endings() value as the item says it."""
    who = "" if e.get("node") in ("", "task") else f'{e["node"]} '
    what = "failed" if not e.get("ok", True) else "ended without a page"
    return f"{who}run {what} {_rel_time(e.get('ended') or 0, now)}".strip()


def item_facts(items: list[dict], tree: dict, kids: dict, statuses: dict, doing: dict,
               returned: dict, spent: dict, now: float, running: list | None = None,
               endings: dict | None = None, pageless: list | None = None) -> list[dict]:
    """Add what the states, filters and sorts read to each fleet_items item,
    in place: working (a page's agent or any agent in its tree is on it),
    agents (running cards under it), needs (0 when a page wants the driver
    now, or a run in it died without a page), returned, spent (all its
    tasks' turns), unseen (each page's answer from the page server), and
    bare: the words for a run in it that ended without a page, "" if none."""
    endings = endings or {}
    bare_rows = {str(r.get("task")): r for r in pageless or () if r.get("task")}
    under: dict[str, int] = {}
    for c in running or ():
        if isinstance(c, dict) and not c.get("waiting"):
            under[str(c.get("item") or "")] = under.get(str(c.get("item") or ""), 0) + 1
    for it in items:
        tasks = {str(sf.get("task") or "") for sf in it["pages"]} - {""}
        if not it["key"].startswith("page:"):
            tasks.add(it["key"])
            stack = list(kids.get(it["key"]) or ())
            while stack:
                t = stack.pop()
                if t not in tasks:
                    tasks.add(t)
                    stack.extend(kids.get(t) or ())
        states = [_page_state(sf, statuses, now) for sf in it["pages"]]
        it["agents"] = under.get(it["key"], 0)
        it["working"] = (bool(it["agents"]) or any(cls == "running" for _, cls in states)
                         or any(t in doing or (tree.get(t) or {}).get("working") for t in tasks)
                         or any((bare_rows.get(t) or {}).get("running") for t in tasks))
        said = [ending_phrase(endings[t], now) for t in sorted(
            tasks & set(endings), key=lambda t: -float(endings[t].get("ended") or 0))]
        said += [p for p in (pageless_phrase(bare_rows[t], now) for t in tasks & set(bare_rows))
                 if p]
        it["bare"] = "" if it["working"] or not said else said[0]
        it["needs"] = 0 if it["bare"] or any(cls == "needs" or "your turn" in label
                                             for label, cls in states) else 1
        it["returned"] = max([float(returned.get(t) or 0) for t in tasks] or [0.0])
        it["spent"] = sum(float(spent.get(t) or 0) for t in tasks)
        it["unseen"] = [(statuses.get(conv_mod.key_of(sf) or "") or {}).get("unseen")
                        for sf in it["pages"]]
        it["viewed"] = [_iso_epoch((statuses.get(conv_mod.key_of(sf) or "") or {})
                                   .get("viewed_at")) for sf in it["pages"]]
    return items


def _options(choices: dict, current: str) -> str:
    return "".join(f'<option value="{escape(k)}"{" selected" if k == current else ""}>'
                   f'{escape(v)}</option>' for k, v in choices.items())


def work_toolbar(projs: dict, limit: int = items_mod.HOME_LIMIT, sort: str = "needs") -> str:
    """The Work list's controls, drawn once in the page chrome so a live
    refresh never takes the search box from under the driver's typing: find,
    sort, project, how many items the home shows before the rest fold into
    Idle, then the state and tag chips (filled by the page script from the
    items it finds). A GET form, so a view is a link and works without the
    script."""
    names = {p["id"]: p["name"] for p in projects_mod.live(projs)}
    projects = "".join(f'<option value="{escape(k)}">{escape(v)}</option>'
                       for k, v in sorted(names.items(), key=lambda kv: kv[1].lower()))
    limits = sorted({6, 15, 30, 50, limit})
    return (f'<form class="toolbar" id="workbar" method="get" action="{HOME_PATH}">'
            '<input id="filter" type="search" name="q" aria-label="find" autocomplete="off" '
            'placeholder="find: filters these items and searches every page ever opened">'
            f'<select id="sort" name="sort" aria-label="sort">'
            f'{_options({k: "Sort: " + v for k, v in items_mod.SORTS.items()}, sort)}</select>'
            f'<select id="proj" class="projfilter" aria-label="project">'
            f'<option value="">All projects</option>{projects}</select>'
            '<label class="hint">Home shows <select id="limit" name="limit" '
            f'aria-label="home limit">{_options({str(n): str(n) for n in limits}, str(limit))}'
            '</select> items, then the least recently touched fold into Idle</label>'
            '<noscript><button type="submit">apply</button></noscript></form>'
            '<div class="toolbar chips" id="states"></div>'
            '<div class="toolbar chips" id="tags"></div>'
            '<section id="found" class="card" hidden></section>')


def catch_up_text(it: dict) -> str:
    """The ask a Catch me up press sends: a short brief on this item since
    the driver last opened it, from its own pages and the agents under it."""
    seen = max((float(v) for v in it.get("viewed") or () if v), default=0.0)
    since = (time.strftime("%Y-%m-%d %H:%M", time.localtime(seen)) if seen
             else "the last time I opened any of its pages (the page server may not know)")
    pages = "\n".join(f"- {sf.get('title') or sf.get('kind') or 'page'}: {sf.get('artifact') or ''}"
                      for sf in it["pages"])
    return (f"Catch me up on \"{it['title']}\": a short brief, one screen, of what changed "
            f"in this line of work since {since}. Read its pages and what the agents started "
            f"from it reported; say what is decided, what moved, and what waits on me. "
            f"Change nothing.\n\nIts pages:\n{pages}")


def _catch_up(key: str, it: dict) -> str:
    """A Catch me up button. Drawn on every item with a folder; the page shows
    it only while the item is idle, because the page script moves items in
    and out of Idle as the driver changes the home limit. It only ever runs
    when pressed: it posts a New work request filed under this item."""
    cwd = it.get("cwd") or ""
    if key.startswith("page:") or not cwd or not it["pages"]:
        return ""
    return (f'<form class="cmu" method="post" action="{TASKS_PATH}">'
            f'<input type="hidden" name="text" value="{escape(catch_up_text(it))}">'
            f'<input type="hidden" name="cwd" value="{escape(cwd)}">'
            f'<input type="hidden" name="parent" value="{escape(key)}">'
            '<button type="submit" title="starts an agent that writes a short brief of '
            'what changed since you last opened this">Catch me up</button></form>')


def _item_actions(key: str, it: dict) -> str:
    """Catch me up, done, hide, tags and new work for one item. The store
    changes POST /items and come back to the home page."""
    def _btn(action, label, extra=""):
        return (f'<form method="post" action="{ITEMS_PATH}">'
                f'<input type="hidden" name="item" value="{escape(key)}">'
                f'<input type="hidden" name="action" value="{action}">{extra}'
                f'<button type="submit">{label}</button></form>')
    done = _btn("undone", "Reopen") if it["state"] == items_mod.DONE else _btn("done", "Mark done")
    hide = (_btn("unhide", "Show on home") if it["state"] == items_mod.HIDDEN
            else _btn("hide", "Hide from home"))
    untag = "".join(_btn("untag", f"#{escape(t)} ×",
                         f'<input type="hidden" name="tag" value="{escape(t)}">')
                    for t in it["tags"])
    under = ("" if key.startswith("page:") else
             f'<a class="btn ghost nwu" data-parent="{escape(key)}" '
             f'href="{HOME_PATH}?parent={escape(urllib.parse.quote(key))}#newwork">'
             'New work under this</a>')
    return (f'<div class="acts">{_catch_up(key, it)}{done}{hide}'
            f'<details class="addtag"><summary class="btn ghost">+ Tag</summary>'
            f'<form method="post" action="{ITEMS_PATH}">'
            f'<input type="hidden" name="item" value="{escape(key)}">'
            '<input type="hidden" name="action" value="tag">'
            '<input name="tag" placeholder="tag, then Enter" aria-label="tag" size="12" '
            'maxlength="32"></form></details>'
            f'{under}{untag}</div>')


def _kid_rows(task: str, tree: dict, kids: dict, now: float, endings: dict,
              bare: dict, seen=None) -> list[tuple[str, bool]]:
    """The agents started from `task`, all the way down, each with how it
    stands: running, ended, or ended with no page — the one that must never
    pass unseen. One (html, live) pair per child: live when it, or anything
    under it, is running or ended without a page."""
    seen = set(seen or ()) | {task}
    rows = []
    for child in kids.get(task) or ():
        if child in seen:
            continue
        node = tree.get(child) or {}
        title = escape(str(node.get("title") or _task_label(child)))
        href = escape(view_href(node["page"])) if node.get("page") else _tree_href(child)
        live = True
        if node.get("working"):
            state = '<span class="badge work">running</span>'
        elif child in endings:
            state = f'<span class="badge q">{escape(ending_phrase(endings[child], now))}</span>'
        elif child in bare and pageless_phrase(bare[child], now):
            state = f'<span class="badge q">{escape(pageless_phrase(bare[child], now))}</span>'
        else:
            state, live = '<span class="badge">ended</span>', False
        deeper = _kid_rows(child, tree, kids, now, endings, bare, seen)
        inner = "".join(h for h, _ in deeper)
        rows.append((f'<div class="page"><a href="{href}">{title}</a>{state}</div>'
                     + (f'<div class="pages">{inner}</div>' if inner else ""),
                     live or any(a for _, a in deeper)))
    return rows


def _rounds(sf: dict) -> list[dict]:
    """A page row's rounds, oldest first: its earlier rounds, then itself."""
    head = {"title": sf.get("title"), "path": sf.get("path"), "opened": sf.get("opened")}
    return list(reversed(sf.get("earlier") or [])) + [head]


LANE, ROW = 14, 24     # the item graph: px between depth lanes, px per row


def _graph(root: str, pages: list[dict], tree: dict, kids: dict, statuses: dict,
           now: float, endings: dict, bare: dict, row) -> str:
    """An item drawn as its tree: one row per task, source at the top, each
    task under the one that started it, in launch order, joined by lines the
    way `git log --graph` joins commits. Each row is ordinary HTML (one line,
    its page linked) after a small SVG cell; the cells meet edge to edge, so
    the lines read as one drawing.

    The dot says where it stands: filled green, a page you have not opened
    yet; filled blue, a page you have opened but not responded to (a head);
    amber, an agent working; red, a run that failed or ended without a page;
    hollow, answered or done. A branch where everything is
    answered and nothing runs draws as one hollow row, "+N" for what it
    holds; the root always draws."""
    sf_of: dict[str, dict] = {}
    for sf in pages:
        t = str(sf.get("task") or "")
        if float(sf.get("opened") or 0) >= float((sf_of.get(t) or {}).get("opened") or 0):
            sf_of[t] = sf
    working = lambda t: bool((tree.get(t) or {}).get("working"))

    def state(t: str) -> str:
        if working(t):
            return "work"
        if t in endings or (t in bare and pageless_phrase(bare[t], now)):
            return "bad"
        if t in sf_of and (statuses.get(conv_mod.key_of(sf_of[t]) or "") or {}).get(
                "unseen") is True:
            return "new"
        if t in sf_of and not _answered(sf_of[t], statuses):
            return "you"
        return "done"

    seen: set = set()

    def walk(t: str, depth: int) -> list[list]:
        """Preorder rows [task, depth, children drawn, hidden count]."""
        seen.add(t)
        kin = [c for c in kids.get(t) or () if c not in seen]
        rows, me = [], [t, depth, [], 0]
        rows.append(me)
        for c in kin:
            sub = walk(c, depth + 1)
            if len(sub) > 1 and all(state(r[0]) == "done" for r in sub):
                sub[0][3] = sum(1 + r[3] for r in sub[1:])     # a settled branch: one row
                sub[0][2] = []                                  # nothing under it is drawn
                sub = sub[:1]
            me[2].append(c)
            rows += sub
        return rows

    rows = walk(root, 0)
    at = {r[0]: i for i, r in enumerate(rows)}
    width = (max(r[1] for r in rows) + 1) * LANE + 4
    x = lambda d: 7 + d * LANE
    cy, h = ROW // 2, ROW
    lines: list[list[str]] = [[] for _ in rows]
    for i, (t, d, drawn, _) in enumerate(rows):
        if not drawn:
            continue
        xp, last = x(d), max(at[c] for c in drawn)
        lines[i].append(f'<path class="e" d="M{xp},{cy} V{h}"/>')
        for j in range(i + 1, last + 1):
            if rows[j][0] in drawn:
                xc = x(rows[j][1])
                lines[j].append(f'<path class="e" d="M{xp},0 V{cy - 5} Q{xp},{cy} {xp + 5},{cy} '
                                f'H{xc - 4}"/>')
                if j < last:
                    lines[j].append(f'<path class="e" d="M{xp},{cy} V{h}"/>')
            else:
                lines[j].append(f'<path class="e" d="M{xp},0 V{h}"/>')

    out = []
    for i, (t, d, _, hidden) in enumerate(rows):
        st = state(t)
        cell = (f'<svg width="{width}" height="{h}" aria-hidden="true">{"".join(lines[i])}'
                f'<circle class="n {st}" cx="{x(d)}" cy="{cy}" r="4.5"/></svg>')
        more = (f'<span class="badge" title="{hidden} more, all answered">+{hidden}</span>'
                if hidden else "")
        if t in sf_of:
            html = row(sf_of[t])
            k = html.split('<span class="k">', 1)[1].split("</span>", 1)[0]
            n = len(_rounds(sf_of[t]))
            html = html.replace(f'<span class="k">{k}</span>', cell, 1)
            extra = ((f'<span class="rn" title="{n} rounds">r{n}</span>' if n > 1 else "")
                     + ('<span class="badge work">running</span>'
                        if st == "work" and "badge work" not in html else "") + more)
            html = html.replace("</a>", "</a>" + extra, 1)
        else:
            info = tree.get(t) or {}
            title = escape(str(info.get("title") or _task_label(t)))
            href = escape(view_href(info["page"])) if info.get("page") else _tree_href(t)
            if st == "work":
                said = '<span class="badge work">running</span>'
            elif t in endings:
                said = f'<span class="badge q">{escape(ending_phrase(endings[t], now))}</span>'
            elif st == "bad":
                said = f'<span class="badge q">{escape(pageless_phrase(bare[t], now))}</span>'
            else:
                said = '<span class="badge">ended, no page</span>'
            html = (f'<div class="page">{cell}<a class="t" href="{href}" title="{title}">'
                    f'{title}</a>{said}{more}</div>')
        out.append(html.replace('<div class="page">',
                                f'<div class="page grow{" done" if st == "done" else ""}">', 1))
    loose = [sf for t, sf in sf_of.items() if t not in seen]   # in no tree (folded ones are)
    return "".join(row(sf) for sf in loose) + f'<div class="graph">{"".join(out)}</div>'


def _earlier(rows: list[str], noun: str) -> str:
    """Rows the driver rarely needs, one click away instead of in the way."""
    if not rows:
        return ""
    return (f'<details class="earlier"><summary>{len(rows)} {noun}'
            f'{"s" if len(rows) != 1 else ""}</summary>{"".join(rows)}</details>')


def _answered(sf: dict, statuses: dict) -> bool:
    """The driver closed this page — approved it, or sent and ended — so the
    page server reports its session ended, or it is titled DECIDED. Read or
    not, a page still open is not answered; a page the page server says
    nothing about is not known to be answered either. A strand is never
    answered: its feedback reached no one."""
    if sf.get("stranded"):
        return False
    st = statuses.get(conv_mod.key_of(sf) or "") or {}
    return st.get("status") == "ended" or "DECIDED" in str(sf.get("title") or "")


def _when_title(it: dict, now: float) -> str:
    """The row's time is when an agent last brought something back — what is
    new — not when anything (you opening it included) last touched it."""
    touched = escape(_rel_time(it["touched"], now))
    if it.get("returned"):
        return f"newest result {escape(_rel_time(it['returned'], now))}; touched {touched}"
    return f"no result yet; touched {touched}"


_STATE_BADGE = {items_mod.YOUR_TURN: "you", items_mod.IDLE: "idle",
                items_mod.DONE: "done", items_mod.HIDDEN: "idle"}


def _state_badge(it: dict) -> str:
    if it["state"] == items_mod.WORKING:
        n = it.get("agents") or 0
        text = f'{n} agent{"s" if n != 1 else ""} running' if n else "Agent working"
        return f'<span class="badge work">{text}</span>'
    return (f'<span class="badge {_STATE_BADGE[it["state"]]}">'
            f'{items_mod.LABELS[it["state"]]}</span>')


def _items_html(snap, now, tree, kids, projs, doing, statuses, returned,
                bits, spent=None, view=None, store=None, running=None,
                endings=None, pageless=None) -> str:
    """render_fleet's by-item body: the Work list. Every item is drawn with
    the facts the page script sorts, filters and folds on (data-*), so the
    toolbar answers at once, and in the order and state the server would pick
    so the list reads right before the script runs."""
    view, store, endings = view or {}, store or {}, endings or {}
    items = fleet_items(snap.get("surfaces") or [], tree, projs, statuses, returned, pageless)
    item_facts(items, tree, kids, statuses, doing or {}, returned or {}, spent or {}, now,
               running=running, endings=endings, pageless=pageless)
    try:
        limit = max(1, int(view.get("limit") or items_mod.HOME_LIMIT))
    except ValueError:
        limit = items_mod.HOME_LIMIT
    items_mod.assign_states(items, store, limit)
    sort = view.get("sort") if view.get("sort") in items_mod.SORTS else "needs"
    show = view.get("show") if view.get("show") in items_mod.SHOWS else ""
    tag = str(view.get("tag") or "")
    items = items_mod.order(items, sort)
    names = {p["id"]: p["name"] for p in projects_mod.live(projs)}
    bare = {str(r.get("task")): r for r in pageless or () if r.get("task")}

    def _prow(sf, when=False, head=False):
        label, cls = _page_state(sf, statuses, now)
        st = (f'<span class="badge {"work" if cls == "running" else "q"}">{escape(label)}</span>'
              if cls in ("running", "needs") else "")
        n = len(sf.get("earlier") or [])
        k = f"round {n + 1}" if n else str(sf.get("kind") or "page")
        new = ('<span class="badge you">unopened</span>'
               if (statuses.get(conv_mod.key_of(sf) or "") or {}).get("unseen") is True else "")
        name = escape(str(sf.get("title") or sf.get("kind") or "page"))
        return (f'<div class="page"><span class="k">{escape(k)}</span>'
                f'<a class="t" href="{escape(view_href(sf["path"]))}" title="{name}">{name}</a>'
                f'{new}{st}'
                + (f'<span class="badge" title="when this page was written">'
                   f'{escape(_rel_time(float(sf.get("opened") or 0), now))}</span>' if when else "")
                + f'{bits["spent"](sf)}'
                + ("" if head else bits["history"](sf))
                + (bits["menu"](sf, False) if head else bits["menu"](sf)) + '</div>')

    rows, idle, off = [], [], []
    for it in items:
        key, state = it["key"], it["state"]
        # Only a page the driver answered folds away. Opening a page is not
        # answering it: a page still open is the last word in its line of
        # work, so it stays in view however long ago it was read. The newest
        # page stays too, answered or not, so the item always shows its head.
        cur = [_prow(sf) for n, sf in enumerate(it["pages"])
               if n == 0 or not _answered(sf, statuses)]
        old = [_prow(sf) for n, sf in enumerate(it["pages"])
               if n and _answered(sf, statuses)]
        pages = ("".join(cur) + _earlier(old, "answered page")
                 or '<p class="empty">No page yet.</p>')
        kid_rows = _kid_rows(key, tree, kids, now, endings, bare)
        started = ("".join(h for h, live in kid_rows if live)
                   + _earlier([h for h, live in kid_rows if not live], "finished agent"))
        if kids.get(key):
            # An item with a tree reads as one: each page under the page whose
            # agent started it, oldest launch first, so who made what and in
            # what order is the shape of the list rather than a lookup.
            pages, started = _graph(key, it["pages"], tree, kids, statuses, now, endings,
                                    bare, lambda sf: _prow(sf, when=True, head=True)), ""
        dot = ('<span class="unseen" title="has a page you have not opened since the agent '
               'wrote it"></span>' if it["unopened"] else "")
        flag = (f'<span class="badge q" title="a run in this item stopped without leaving a '
                f'page to read">{escape(it["bare"])}</span>' if it["bare"] else "")
        tags = "".join(f'<span class="tag">#{escape(t)}</span>' for t in it["tags"])
        pname = names.get(it["project"], it["project"])
        cost = (f'<span class="badge cost" title="every turn of every task in this item">'
                f'{escape(costs_mod.usd(it["spent"]))}</span>' if it.get("spent") else "")
        stored = state if state in (items_mod.DONE, items_mod.HIDDEN) else ""
        shown = items_mod.visible(it, show, tag)
        html = (
            f'<details class="item{"" if shown else " offlist"}" data-fold="{escape(key)}" '
            f'data-item="{escape(key)}" data-project="{escape(it["project"])}" '
            f'data-projname="{escape(pname)}" data-state="{state}" data-store="{stored}" '
            f'data-working="{int(bool(it["working"]))}" data-agents="{it.get("agents") or 0}" '
            f'data-needs="{it["needs"]}" data-touched="{int(it["touched"])}" '
            f'data-returned="{int(it["returned"])}" data-spent="{it["spent"]:.2f}" '
            f'data-unopened="{int(it["unopened"])}" '
            f'data-tags="{escape(",".join(it["tags"]))}">'
            f'<summary class="row"><span class="caret"></span>{dot}'
            f'<span class="title">{escape(it["title"])}</span>'
            f'<span class="stbadge">{_state_badge(it)}</span>{flag}{tags}'
            f'<span class="proj">{escape(pname)}</span>{cost}'
            f'<span class="when" title="{_when_title(it, now)}">'
            f'{escape(_rel_time(it["returned"] or it["touched"], now))}'
            f'</span></summary><div class="inside">'
            f'<div class="grp">{"Pages, as the tree that made them" if kids.get(key) else "Pages"}'
            f'</div><div class="pages">{pages}</div>'
            # Track B's message threads land here, under the item they are about.
            f'<div class="threads" data-item="{escape(key)}"></div>'
            + (f'<div class="grp">Agents started from this</div>'
               f'<div class="pages">{started}</div>' if started else "")
            + f'{_item_actions(key, it)}</div></details>')
        (rows if shown and not (show == "" and state == items_mod.IDLE)
         else idle if shown else off).append(html)
    fold = (f'<details class="idlefold" data-fold="idle"{"" if idle else " hidden"}>'
            f'<summary class="foldrow">Idle: <span class="n">{len(idle)}</span> more, least '
            'recently touched. They fold here only because the home limit is reached.'
            f'</summary><div class="idlebox">{"".join(idle)}</div></details>')
    out = (f'<div class="card items" data-limit="{limit}">{"".join(rows)}'
           f'<p class="empty noitems"{"" if not rows and not idle else " hidden"}>'
           'Nothing matches. Clear a filter above.</p>'
           f'{fold}<div class="offbox">{"".join(off)}</div></div>')
    return out


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
                fleet_html=None, parents: list | None = None,
                entries: list | None = None, parent: str = "",
                view: dict | None = None) -> str:
    """The home page: a header (spend, cards, every run, + New work), the
    Running now strip, track A's inbox slot, then Work — its toolbar drawn
    here, once, and its list re-fetched when the fleet stream reports a
    change. `fleet_html` is (running html, work html) already rendered from
    every backend's API (the live page); None renders `snap` directly
    (tests)."""
    now = time.time() if now is None else now
    view = view or {}
    projs = projs or {}
    if fleet_html is None:
        fleet_html = ("", render_fleet(snap, now, statuses=statuses, graph=graph, projs=projs,
                                       finished=finished, doing=doing, spent=spent,
                                       by_item=True, view=view))
    elif isinstance(fleet_html, str):
        fleet_html = ("", fleet_html)
    running_html, work_html = fleet_html
    try:
        limit = max(1, int(view.get("limit") or items_mod.HOME_LIMIT))
    except ValueError:
        limit = items_mod.HOME_LIMIT
    sort = view.get("sort") if view.get("sort") in items_mod.SORTS else "needs"
    banner = f'<p class="notice">{escape(notice)}</p>' if notice else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Cadre</title>'
        '<link rel="icon" href="data:image/svg+xml,'
        '%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22%3E'
        '%3Ctext y=%2213%22 font-size=%2213%22%3E%F0%9F%9B%B0%3C/text%3E%3C/svg%3E">'
        f'<style>{_CSS}{_HOME_CSS}</style></head><body class="home"><div class="wrap">'
        '<header><h1>Cadre<span class="dot">.</span></h1><span class="sp"></span>'
        + (f'<a href="{COSTS_PATH}">{escape(costs_mod.usd(week_usd))} in 7 days</a>'
           if week_usd is not None else "") +
        f'<a href="{CARDS_PATH}">cards</a><a href="{RUNS_PATH}">every run</a>'
        f'<span id="live" class="live" title="snapshot {escape(str(snap.get("iso") or "—"))}">'
        'connecting…</span>'
        '<button type="button" class="primary" id="newworkbtn">+ New work</button></header>'
        f'{banner}'
        f'<div id="fleet-running">{running_html}</div>'
        f'<h2 class="sect">Work</h2>{work_toolbar(projs, limit, sort)}'
        f'<div id="fleet">{work_html}</div>'
        f'{_new_project_form(projs)}'
        f'<footer><a href="{LIBRARY_PATH}">installed skills, agents and workflows</a> · '
        f'<a href="{TREE_PATH}">which session launched which</a> · '
        f'<a href="{COSTS_PATH}">what each run cost</a></footer>'
        f'{render_footer_hooks()}</div>'
        f'{_new_work_form(parents or [], entries or [], parent, projs)}'
        f'<script>{_PAGE_JS}</script></body></html>')


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


def _dir_field(fid: str, lid: str, label: str, name: str = "") -> str:
    """A directory input that completes from this machine through /fs
    (complete_dirs), with its own suggestion list. Two forms use it: New
    project and New work."""
    return (
        f'<div class="dirwrap"><label class="lbl" for="{fid}">{label} — '
        'type <code>/</code> or <code>~</code> and it completes from this machine'
        f'</label><input name="{name or fid}" id="{fid}" '
        'autocomplete="off" spellcheck="false" role="combobox" aria-expanded="false" '
        f'aria-autocomplete="list" aria-controls="{lid}" '
        'placeholder="~/Projects/…">'
        f'<ul id="{lid}" class="sug" role="listbox" hidden></ul></div>'
        # A real completion list rather than a datalist: a datalist only opens
        # when the browser feels like it and filters full paths badly, so it
        # read as "no autocomplete at all". This draws every keystroke's
        # matches, supports arrow keys and Enter, and shows "no match" rather
        # than going silent — the feedback the driver asked for.
        '<script>(function(){'
        f'var i=document.getElementById("{fid}"),box=document.getElementById("{lid}");'
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
    )


def _new_work_form(parents: list, entries: list, parent: str = "",
                   projs: dict | None = None) -> str:
    """New work, a dialog the header's + New work opens: what to do, the
    folder (completed from this machine, with the projects' own folders one
    click away, and which project the folder files under said as you type),
    an optional item to put it under, tags, and an optional skill. Posts to
    /tasks.

    `parents` is [(item key, title)] — the items a new task can join;
    `parent` preselects one and opens the dialog (an item's "new work under
    this" link)."""
    opts = "".join(f'<option value="{escape(k)}"{" selected" if k == parent else ""}>'
                   f'{escape(t[:90])}</option>' for k, t in parents)
    live = projects_mod.live(projs or {})
    owners = json.dumps({"home": str(Path.home()),
                         "dirs": [[str(d).rstrip("/"), p["name"]] for p in live
                                  for d in p.get("dirs") or ()]})
    picks = "".join(f'<button type="button" data-dir="{escape(_home_path(str(d)))}">'
                    f'{escape(_home_path(str(d)))}</button>'
                    for d in dict.fromkeys(str((p.get("dirs") or [""])[0]) for p in live) if d)
    return (
        f'<dialog id="newwork"{" data-open" if parent else ""}>'
        f'<form class="newtask" method="post" action="{TASKS_PATH}">'
        '<h3>Start work</h3>'
        '<label for="what">What should the agent do?</label>'
        '<textarea id="what" name="text" rows="3" required '
        'placeholder="e.g. Find out why Study cards are not useful yet"></textarea>'
        f'{_dir_field("workdir", "worksug", "Folder it works in", name="cwd")}'
        f'<div class="sugg">{picks}</div>'
        f'<script type="application/json" id="owners">{owners}</script>'
        '<p class="hint" id="projhint">The project is the one that owns this folder; a '
        'folder no project owns becomes a new project.</p>'
        '<label for="under">Put it under an item (optional)</label>'
        '<select id="under" name="parent"><option value="">New item of its own</option>'
        f'{opts}</select>'
        '<label for="newtags">Tags</label>'
        '<input id="newtags" name="tags" placeholder="comma separated, e.g. cadre, ux">'
        '<label>Use a skill (optional)</label>'
        f'{_run_picker(entries)}'
        '<div class="dlgacts"><button type="button" class="ghost" id="cancelwork">Cancel'
        '</button><button type="submit" class="primary">Start</button></div>'
        f'</form><script>{_PICKER_JS}</script></dialog>')


def _new_project_form(projs: dict) -> str:
    return (
        # Folded under the work list: New work makes a project from a folder
        # no project owns, so this is for naming one up front or grouping it.
        '<details class="card newproject"><summary class="meta">New project — a durable home '
        'for work, kept on the fleet until you archive it</summary>'
        f'<form class="newtask" method="post" action="{PROJECTS_PATH}">'
        '<div class="row"><input name="name" required maxlength="64" '
        'placeholder="name, e.g. Cadre"></div>'
        f'{_dir_field("dir1", "dirsug", "Working directory")}'
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


def render_cards(deck: list[dict], due: int, dirs: list[str], notice: str = "",
                 now: float | None = None) -> str:
    """Your deck, on this machine: your own cards and the team cards synced
    into it, what is due, and a box to start a study session. The deck is
    never another backend's to read — /api/cards serves only a team's."""
    banner = f'<section class="card"><p>{escape(notice)}</p></section>' if notice else ""
    opts = "".join(f'<option value="{escape(d)}">{escape(d)}</option>'
                   for d in dict.fromkeys(dirs))
    study = (f'<section class="card"><h2>Study a topic</h2>'
             f'<form class="newtask" method="post" action="{STUDY_PATH}">'
             '<textarea name="topic" required placeholder="What do you need to understand?">'
             '</textarea><div class="row">'
             + (f'<select name="cwd">{opts}</select>' if opts else "")
             + '<button type="submit">Study it</button></div>'
             '<p class="meta">A tutor session: due cards first, one short idea, a few '
             'questions, graded next round. It reads the code; it never changes it.</p>'
             '</form></section>')

    def row(c):
        team = " · team card" if c.get("scope") == "team" else ""
        when = (escape(str(c.get("fsrs_due") or ""))[:16].replace("T", " ")
                if c["status"] == "accepted" else "")
        return (f'<tr><td class="desc">{escape(c["front"])}</td>'
                f'<td class="scope">{escape(c["source"])}'
                f'{" · " + escape(c["anchor"]) if c.get("anchor") else ""}{team}</td>'
                f'<td class="inv">{when or keep(c)}</td></tr>')

    def keep(c):
        # A proposed card waits on the driver: keep it into review or drop it.
        if c["status"] != "proposed":
            return ""
        return (f'<form class="repair" method="post" action="{CARDS_PATH}">'
                f'<input type="hidden" name="id" value="{escape(c["id"])}">'
                '<button name="do" value="keep">keep</button>'
                '<button name="do" value="drop" class="danger">drop</button></form>')

    def table(title, rows):
        return (f'<section class="card"><h2>{title}</h2><table class="lib">'
                + "".join(row(c) for c in rows) + '</table></section>') if rows else ""

    live = [c for c in deck if c["status"] == "accepted"]
    proposed = [c for c in deck if c["status"] == "proposed"]
    gone = sum(1 for c in deck if c["status"] == "discarded")
    head = (f'<section class="card"><h2>Deck</h2><p><b>{due}</b> due now · '
            f'<b>{len(live)}</b> in review · <b>{len(proposed)}</b> waiting for you to '
            f'keep or discard · {gone} discarded.</p>'
            '<p class="empty">Cards come due inside study sessions; start one below. '
            'Pages that change code propose cards about the change; they wait under '
            'Proposed until you keep or drop them. Team cards arrive with each '
            'session\'s sync and are scheduled for you alone.</p></section>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Cadre — cards</title><style>{_CSS}</style></head>'
        '<body><div class="wrap"><header><h1>Cards<span class="dot">.</span></h1></header>'
        f'<p class="meta"><a href="/">← fleet</a></p>'
        f'{banner}{head}{study}'
        f'{table("In review · soonest due first", sorted(live, key=lambda c: c.get("fsrs_due") or ""))}'
        f'{table("Proposed", proposed)}'
        '</div></body></html>')


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


def run_library(entries: list[dict]) -> list[dict]:
    """What the skill picker offers: every installed entry that is not broken,
    by kind then name, as {v: kind:name, i: how it is invoked, k: kind, d:
    the start of its description}."""
    live = sorted((e for e in entries if not e.get("broken")),
                  key=lambda e: (e["kind"], e["name"].lower()))
    return [{"v": f'{e["kind"]}:{e["name"]}', "i": e["invoke"],
             "k": e["kind"], "d": (e.get("description") or "")[:120]} for e in live]


def library_payload() -> dict:
    """/library's data: everything a launch can pick, and where it was found."""
    dirs = list(dict.fromkeys(d for p in projects_mod.live(load_projects()) for d in p.get("dirs") or ()))
    return {"entries": library_mod.scan(dirs=dirs, skills_source=_skills_source()), "scanned": dirs}


def costs_payload() -> dict:
    """/costs's data: costs.summarize over every finished turn (also /costs.json)."""
    return costs_mod.summarize(*load_costs())


def runs_payload(query: dict) -> dict:
    """/runs's data for one view: the view's rows (at most RUNS_LIMIT, with
    how many there are), and every project and node a filter can pick."""
    turns, owners, pages = load_runs()
    sel = {k: (query.get(k) or [""])[0].strip()[:200] for k in ("view", "sort", "project", "node", "q")}
    rows = recent_mod.rows(turns, owners, pages, view=sel["view"], sort=sel["sort"],
                           project=sel["project"], node=sel["node"], q=sel["q"])
    everything = recent_mod.rows(turns, owners, pages, view="turns")
    return {"rows": rows[:RUNS_LIMIT], "total": len(rows), "limit": RUNS_LIMIT,
            "projects": sorted({r["project"] for r in everything if r["project"]}),
            "nodes": sorted({r["node"] for r in everything if r["node"]})}


def tree_payload() -> dict:
    """/tree's data: every session that launched or was launched, by task."""
    return {"nodes": load_tree()}


def backends_payload() -> dict:
    """The machines the React app can switch between: this one first (url ""
    means "where the app was loaded from"), then every configured
    [[backends]] entry, which the app reads and posts to directly."""
    return {"backends": [{"name": b["name"] or MACHINE, "url": ""} if b["url"] is None
                         else {"name": b["name"] or b["url"], "url": b["url"]} for b in BACKENDS]}


def newwork_payload() -> dict:
    """What the React app's New work dialog needs that /api/fleet does not
    carry: this machine's home directory (a typed ~ resolves against it) and
    the skill picker's library. The Python page inlines both."""
    return {"home": str(Path.home()),
            "library": run_library(library_mod.scan(dirs=[], skills_source=_skills_source()))}


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
    lib = run_library(entries)
    data = json.dumps(lib, ensure_ascii=False)
    chosen = next((e for e in lib if e["v"] == current), None)
    summary = (f'run {escape(chosen["i"])}' if chosen else
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
                '<button type="submit">Launch</button>'
                # The same box, read as a topic: a tutor session on it, in the
                # chosen directory, instead of a task.
                '<button type="submit" name="node" value="tutor">Study it</button></div>'
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
    store, the project store and the item store (tags, done, hidden)."""
    h = hashlib.sha1()
    try:
        snap = json.loads((STATUS_DIR / "status.json").read_text(encoding="utf-8"))
        if isinstance(snap, dict):
            for k in _VOLATILE:
                snap.pop(k, None)
        h.update(json.dumps(snap, sort_keys=True, default=str).encode())
    except (OSError, ValueError):
        h.update(b"no-snapshot")
    for f in [*conv_mod.link_files(PANEL_LINKS), DATA_DIR / projects_mod.FILE,
              DATA_DIR / items_mod.FILE]:
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


def _session_store() -> dict:
    try:
        store = json.loads((DATA_DIR / "surfaces" / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return store if isinstance(store, dict) else {}


def fleet_payload() -> dict:
    """Everything render_fleet needs for this machine, as JSON. Anything the
    renderer would otherwise read from this machine's disk (answer-history
    counts, the fleet page's code stamp) is resolved here, so a fleet on
    another machine renders it without touching this one's files."""
    snap = read_snapshot()
    # A row archived here leaves the snapshot at the daemon's next pass; until
    # then it leaves the payload, so no reader has to filter it again.
    snap = {**snap, "surfaces": [sf for sf in snap.get("surfaces") or []
                                 if conv_mod.key_of(sf) not in _ARCHIVED]}
    graph = load_graph()
    surfaces = conv_mod.collapse(snap.get("surfaces") or [], *graph)
    projs = load_projects()
    turns, owners, pages = load_runs(titles=graph[2])
    arts = {str(sf.get("artifact") or "") for sf in surfaces} - {""}
    tree = load_tree(pages)
    running = running_agents(titles=pages, tree=tree)
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
            "tree": tree,
            "running": running,
            "slots": int((_CFG.runner if _CFG else {}).get("max_concurrent_runs") or 0),
            "pageless": pageless_tasks(projs),
            "endings": bare_endings(turns, _session_store(),
                                    {c["task"] for c in running if not c["waiting"]}),
            "items": items_mod.load(DATA_DIR),
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


def fleet_parts(results: list, now: float | None = None,
                view: dict | None = None) -> tuple[str, str]:
    """(Running now html, Work html) for the home page from every backend's
    API payload, this machine's included. Running now is one strip across
    every machine; each machine's items are drawn into its own list, which
    the page script merges into one, so Work reads as one list too. A machine
    gets a heading only when there is more than one to tell apart, and one
    that does not answer says so where its items would be. Links on another
    machine's cards and items point at that machine (backends.point_at)."""
    now = time.time() if now is None else now
    many = len(results) > 1
    cards, n_run, n_wait, slots, inbox, work = [], 0, 0, 0, [], []
    for i, (b, data, err) in enumerate(results):
        same = bool(data) and data.get("instance") == INSTANCE
        url = escape(b["url"], quote=True)
        name = escape(_backend_name(b, data))
        where = ("this machine" if same else
                 f'<a target="_blank" rel="noopener" href="{url}/">{url} →</a>')
        head = (f'<p class="machine" id="machine-{i}">machine · <b>{name}</b> · {where}</p>'
                if many or data is None else "")
        if data is None:
            work.append(head + f'<section class="card"><p><span class="badge needs">unreachable'
                        f'</span> Could not reach the Cadre backend at <code>{url}</code>: '
                        f'{escape(err or "no answer")}. Its pages are still on that machine; '
                        'its items come back when it answers.</p></section>')
            continue
        html, r, w = running_cards_html(data.get("running") or [], now)
        cards.append(backends_mod.point_at(html, b, same))
        n_run, n_wait, slots = n_run + r, n_wait + w, slots + int(data.get("slots") or 0)
        inbox.append(backends_mod.point_at(render_inbox(data, now), b, same))
        code = data.get("fleet_page_code") or {}
        own = SimpleNamespace(stale=lambda c=code: bool(c.get("stale")),
                              since=code.get("since"), head=code.get("head"))
        body = render_fleet(
            data.get("snap") or {}, now, statuses=data.get("statuses") or {},
            graph=tuple(data.get("graph") or ([], {}, {})),
            projs=data.get("projs") or {}, finished=data.get("finished") or {},
            doing=data.get("doing") or {}, spent=data.get("spent") or {},
            histories=data.get("histories") or {}, own=own,
            returned=data.get("returned") or {}, tree=data.get("tree") or {},
            by_item=True, view=view, item_store=data.get("items") or {},
            running=data.get("running") or [], endings=data.get("endings") or {},
            pageless=data.get("pageless") or [])
        if many and not same:
            body = body.replace('<details class="item', f'<details data-machine="{name}" '
                                'class="item')
        work.append(f'<div class="backend{"" if same else " remote"}">{head}'
                    f'{backends_mod.point_at(body, b, same)}</div>')
    return (running_section("".join(cards), n_run, n_wait, slots) + "".join(inbox),
            "".join(work))


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

    def _serve_app(self, rel: str):
        """A built file of the React app, or index.html for one of the app's own
        routes. A missing file (a path with an extension, such as an old
        build's hashed chunk) is a 404: index.html served as JavaScript only
        breaks the page with a MIME error."""
        root = APP_DIR.resolve()
        target = (root / rel).resolve() if rel else root / "index.html"
        if not target.is_relative_to(root):
            self.send_error(404)
            return
        if not target.is_file():
            if Path(rel).suffix:
                self.send_error(404, "no such file in the React build")
                return
            target = root / "index.html"
        if not target.is_file():
            self.send_error(404, "the React UI is not built — cd web && npm run build")
            return
        # Vite names every built asset by its content hash, so it never changes
        hashed = target.parent == root / "assets"
        self._serve_static(target, "public, max-age=31536000, immutable" if hashed else "no-store")

    def _serve_static(self, p: Path, cache: str = "no-store"):
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(p.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
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
        upstream_path = self.path.split("?", 1)[0]
        if self.command == "POST" and resp.status < 300 and SEND_RE.match(upstream_path):
            # the driver's send is now queued in the page server; ring the
            # daemon so it consumes it now, not on its next timed pass
            wake_mod.notify(_CFG, "send")
        self.send_response(resp.status)
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
        view = {k: (query.get(k) or [""])[0] for k in ("show", "sort", "tag", "limit")}
        running_html, work_html = fleet_parts(results, now, view)
        running_html = render_machines(results) + running_html
        if query.get("partial"):
            # The page script splits the two halves at the marker.
            self._send_html(running_html + PARTIAL_SPLIT + work_html)
            return
        # The page chrome around the sections (snapshot time, spend, the new
        # project form) is this machine's, from the same payload.
        mine = next((d for _, d, _ in results if d and d.get("instance") == INSTANCE), {})
        notice = (query.get("notice") or [""])[0]
        self._send_html(render_home(mine.get("snap") or {}, notice=notice,
                                    projs=mine.get("projs") or {},
                                    week_usd=mine.get("week_usd"),
                                    fleet_html=(running_html, work_html), view=view,
                                    parents=parent_choices(mine),
                                    entries=library_mod.scan(dirs=[],
                                                             skills_source=_skills_source()),
                                    parent=(query.get("parent") or [""])[0]))

    def _backends(self) -> list[dict]:
        return backends_mod.with_self(BACKENDS, self.server.server_address)

    def _serve_api_fleet(self, version_only: bool):
        self._send_json(fleet_version() if version_only else fleet_payload())

    def _cors_origin(self) -> str:
        """The request's Origin when it is another origin allowed to use this
        server's API (CORS_ORIGINS), else ''."""
        origin = (self.headers.get("Origin") or "").rstrip("/")
        return origin if origin in CORS_ORIGINS else ""

    def _send_cors(self):
        """Let an allowed cross-origin page read this answer. No credentials:
        the API has no session to send."""
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")

    def _send_json(self, data):
        """200 with `data` as JSON, never cached; HEAD gets the headers only."""
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_api_cards(self):
        """A team backend's deck, for teammates' machines to copy in
        (`pipeline.py cards sync`). Only a backend configured as the team's
        answers: a personal deck is never served, whatever this binds to."""
        if not (_CFG and _CFG.learning.get("cards") == "team"):
            self.send_error(404, "no team cards here")
            return
        self._send_json({"cards": cards_mod.export_team(_CFG.cards_dir)})

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
        turns, _, pages = load_runs(titles=graph[2])
        rows_html = render_fleet({"surfaces": rows}, statuses=fetch_agent_statuses(rows),
                                 projs={**mine, p["id"]: {**p, "group": None, "archived": False}},
                                 doing=doing, spent=costs_mod.per_task(turns),
                                 returned=recent_mod.last_returned(turns),
                                 tree=load_tree(pages)) if rows else ""
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

    def _serve_view(self, key: str, query: dict | None = None):
        key = urllib.parse.unquote(key).strip("/")
        if not SESSION_KEY.fullmatch(key):
            self.send_error(404, "no such surface")
            return
        notice = ((query or {}).get("notice") or [""])[0][:300]
        self._send_html(render_view(key, task_for_view(key), notice))

    def _serve_cards(self, query: dict):
        notice = (query.get("notice") or [""])[0][:300]
        if not _CFG:
            self._send_html(render_cards([], 0, [], "No runner config: no deck here."))
            return
        try:
            deck = cards_mod.listing(_CFG.cards_dir)
            due = len(cards_mod.due(_CFG.cards_dir))
        except Exception as e:                      # a broken deck must not 500 the page
            deck, due, notice = [], 0, f"The deck could not be read: {e}"
        projs = load_projects()
        dirs = [d for p in projs.values() if not p.get("archived") for d in p.get("dirs") or ()]
        self._send_html(render_cards(deck, due, dirs, notice))

    def _cards_post(self):
        """POST /cards: keep (accept) or drop (discard) one proposed card."""
        form = self._form()
        if form is None:
            return
        if not _CFG:
            self._redirect(CARDS_PATH, "No runner config: no deck here.")
            return
        cid, do = form.get("id", ""), form.get("do", "")
        try:
            if do == "keep":
                cards_mod.accept(_CFG.cards_dir, cid)
                notice = "Kept — it comes due in your next study session."
            elif do == "drop":
                cards_mod.discard(_CFG.cards_dir, cid)
                notice = "Dropped."
            else:
                notice = "Nothing done: keep or drop?"
        except Exception as e:                      # a bad id must not 500 the page
            notice = f"Nothing done: {e}"
        self._redirect(CARDS_PATH, notice)

    def _study(self):
        """POST /study: topic, and either `from` (the dialogue whose page needs
        it — from the bar above a page) or `cwd` (from the deck). `back` is
        where to land with the outcome."""
        form = self._form()
        if form is None:
            return
        back = form.get("back") or CARDS_PATH
        if not (back.startswith(VIEW_PATH) or back == CARDS_PATH):
            back = CARDS_PATH
        if not _CFG:
            self._redirect(back, "No runner config: a study session cannot start here.")
            return
        row = None
        if form.get("from"):
            row = next((r for r in read_snapshot().get("surfaces") or []
                        if r.get("task") == form["from"]), None)
        cwd = (row or {}).get("cwd") or form.get("cwd") or ""
        if not cwd:
            # Never this server's own directory: a study runs where the code is.
            self._redirect(back, "Study not started: no directory to study in — "
                                 "the page is not on the fleet yet, or none was picked.")
            return
        try:
            from runnerlib import tasks as tasks_mod
            result = tasks_mod.submit_study(
                _CFG, form.get("topic", ""), from_task=form.get("from") or None,
                cwd=cwd, page=(row or {}).get("artifact"))
        except Exception as e:                      # a bad ask must not 500 the page
            self._redirect(back, f"Study not started: {e}")
            return
        self._redirect(back, "Study started — it joins the fleet when its first "
                             "round is ready.")

    def _serve_costs(self, as_json: bool):
        summary = costs_payload()
        if not as_json:
            self._send_html(render_costs(summary))
            return
        self._send_json(summary)

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
            if form.get("node") == tasks_mod.TUTOR:
                result = tasks_mod.submit_study(_CFG, form["text"], cwd=form.get("cwd") or None,
                                                project=pid)
            else:
                result = tasks_mod.submit_task(_CFG, form["text"], form.get("cwd") or None,
                                               project=pid, run=form.get("run") or None)
        except Exception as e:                      # a bad launch must not 500 the page
            self._redirect(back, f"Not launched: {e}")
            return
        self._redirect(back, f"Launched {result}. Its page appears under running when "
                             "the first round finishes.")

    def _redirect(self, where: str, notice: str):
        """Where a form post lands, with what happened. A browser form gets a
        303 (so a refresh does not re-post); a client that asks for JSON (the
        React app) gets {notice, location} instead, so it need not follow the
        redirect and have the whole page rendered only to read the notice."""
        target = where + "?" + urllib.parse.urlencode({"notice": notice})
        if "application/json" in (self.headers.get("Accept") or ""):
            self._send_json({"notice": notice, "location": target})
            return
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _serve_fs(self, query: dict):
        """Directory completions for the project form, read live off this
        machine. Local-only like the rest of this server, and it returns
        directory names only — never file contents."""
        q = (query.get("q") or [""])[0][:400]
        self._send_json({"dirs": complete_dirs(q)})

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
        self._send_cors()
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

    def _items_post(self):
        """POST /items: item, action=done|undone|hide|unhide|tag|untag, tag.
        Writes <data_dir>/items.json and lands back on the home page with
        what happened, the way an archive from the row menu does."""
        form = self._form()
        if form is None:
            return
        try:
            msg = items_mod.apply(DATA_DIR, form.get("item", ""), form.get("action", ""),
                                  form.get("tag", ""))
        except ValueError as e:
            msg = f"Refused: {e}"
        self._redirect_home(msg)

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
        self._redirect(f"/page/{urllib.parse.quote(key)}", msg)

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

    def _serve_tree(self, task: str):
        nodes = load_tree()
        if task and task not in nodes:
            self.send_error(404, "that session launched no others and was launched by none")
            return
        self._send_html(render_tree(task, nodes))

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
        # the React app on another configured machine posts here directly
        return urllib.parse.urlsplit(origin).netloc == host or bool(self._cors_origin())

    def _new_task(self):
        """Form contract: POST /tasks, application/x-www-form-urlencoded,
        `text` (required), `cwd`, `parent`, `run` and `tags` (comma
        separated; all optional). Hands off to runnerlib.tasks.submit_task,
        tags the item in items.json, and redirects back home."""
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
        # The New work dialog's extras; passed only when given, so a bare
        # text+cwd post calls submit_task exactly as it always did.
        extra = {k: v for k in ("parent", "run")
                 if (v := (form.get(k) or [""])[0].strip())}
        tags = [t.strip() for t in (form.get("tags") or [""])[0].split(",") if t.strip()]
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
            result = tasks_mod.submit_task(_CFG, text, cwd, **extra)
        except Exception as e:                      # a bad task must not 500 the page
            self._redirect_home(f"Task rejected: {e}")
            return
        # Tags belong to the item: the parent's when the task joins one,
        # else the new task's own.
        untagged = []
        for t in tags:
            try:
                items_mod.apply(DATA_DIR, extra.get("parent") or str(result), "tag", t)
            except ValueError:
                untagged.append(t)
        note = f" Not tagged (letters, digits, space, . _ - only): {', '.join(untagged)}." \
            if untagged else ""
        self._redirect_home(f"Task submitted{f': {result}' if result else '.'}{note}")

    def _redirect_home(self, notice: str):
        self._redirect(HOME_PATH, notice)

    # --------------------------------------------------------------- routing

    def _route(self):
        if self.command == "POST":
            backends_mod.forget()       # the next read shows what this write did
        path, _, raw_query = self.path.partition("?")
        query = urllib.parse.parse_qs(raw_query)
        if path == TASKS_PATH and self.command == "POST":
            self._new_task()
            return
        if (path == APP_PATH or path.startswith(APP_PATH + "/")) \
                and self.command in ("GET", "HEAD"):
            self._serve_app(path[len(APP_PATH):].lstrip("/"))
            return
        if path == REPAIR_PATH and self.command == "POST":
            self._repair()
            return
        if path == ITEMS_PATH and self.command == "POST":
            self._items_post()
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
        if path in SCREEN_API_PATHS and self.command in ("GET", "HEAD"):
            screen = SCREEN_API_PATHS[path]
            self._send_json(runs_payload(query) if screen == "runs" else
                            {"library": library_payload, "costs": costs_payload,
                             "tree": tree_payload}[screen]())
            return
        if path == BACKENDS_API_PATH and self.command in ("GET", "HEAD"):
            self._send_json(backends_payload())
            return
        if path == NEWWORK_API_PATH and self.command in ("GET", "HEAD"):
            self._send_json(newwork_payload())
            return
        if path == CARDS_PATH and self.command in ("GET", "HEAD"):
            self._serve_cards(query)
            return
        if path == CARDS_PATH and self.command == "POST":
            self._cards_post()
            return
        if path == STUDY_PATH and self.command == "POST":
            self._study()
            return
        if path == cards_mod.API_PATH and self.command in ("GET", "HEAD"):
            self._serve_api_cards()
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
            self._serve_view(path[len(VIEW_PATH):], query)
            return
        if path in ("", HOME_PATH) and self.command in ("GET", "HEAD"):
            self._serve_home(query)
            return
        if path.startswith("/stranded/") and self.command in ("GET", "HEAD"):
            self._serve_dead_letter(path.rsplit("/", 1)[-1])
            return
        if (path == TREE_PATH or path.startswith(TREE_PATH + "/")) \
                and self.command in ("GET", "HEAD"):
            self._serve_tree(urllib.parse.unquote(path[len(TREE_PATH):].strip("/")))
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


def start_servers(bind=None, port=None) -> list:
    """Bind every listen address and serve each on its own thread; return the
    servers. One server per bind address: loopback for this machine, the
    tailnet address for fleets on other machines. Any that fails to bind
    stops the whole process — half-listening would look like a problem on the
    other machine.

    `pipeline.py serve` calls this and then runs the engine loop on the main
    thread: one process is both the API and the thing that starts sessions,
    so a request is acted on in the same process that took it."""
    servers = [ThreadingHTTPServer((addr, port or PORT), Handler) for addr in (bind or BIND)]
    print(f"statusd: serving {STATUS_DIR} on {', '.join(bind or BIND)} port {port or PORT}, "
          f"proxying the rest to {SURFACE}"
          + f"; machine {MACHINE}; other backends: "
          + (", ".join(b["url"] for b in BACKENDS[1:]) or "none"),
          file=sys.stderr)
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True,
                         name=f"api {srv.server_address[0]}").start()
    return servers


def main():
    """The API alone, as `cadre-statusd.service` ran it before `serve`."""
    start_servers()
    threading.Event().wait()


if __name__ == "__main__":
    main()
