#!/usr/bin/env python3
"""Cadre page server: agent-manager home page + static assets + reverse proxy
to Review Surface.

Replaces the bare `python -m http.server` in cadre-status.service, same port,
per cadre-context decision 2026-08-26-surface-as-driver-channel: ONE exposed
port on the tailnet; review-surface keeps its loopback default and every
surface session is reached through this proxy.

Routing rule: `/` and the fleet's other screens are the React app in
web/dist (REACT_ROUTE), which draws them from this server's JSON API — read
from the daemon's status.json snapshot; `POST /tasks` and the other form
posts are its writes. Any other path that resolves to a file in the
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

import gzip
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
from concurrent.futures import ThreadPoolExecutor
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runnerlib import activity as activity_mod
from runnerlib import blackboard as blackboard_mod
from runnerlib import cadres as cadres_mod
from runnerlib import records as records_mod
from runnerlib import cards as cards_mod
from runnerlib import codestamp
from runnerlib import config as config_mod
from runnerlib import conversations as conv_mod
from runnerlib import costs as costs_mod
from runnerlib import items as items_mod
from runnerlib import kanban as kanban_mod
from runnerlib import notifications as notifications_mod
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
    """Every place Cadre holds skills (runnerlib/skillstore.py), for the library."""
    if _CFG:
        from runnerlib import skillstore
        return skillstore.paths(_CFG)
    return Path(os.path.expanduser(_DEFAULTS["skills_source"]))


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
REPAIR_PATH = "/repair"
PROJECTS_PATH = "/projects"
COSTS_PATH = "/costs"
ITEMS_PATH = "/items"
# The kanban board (runnerlib/kanban.py): the Board screen, its form posts,
# and its read.
KANBAN_PATH = "/kanban"
KANBAN_API_PATH = "/api/kanban"
# rows on the fleet's "Recently returned" card, and all the API ships of them
RECENT_LIMIT = 6
STREAM_PATH = "/fleet/events"
# The React fleet UI (web/): its build output, served under /app/. Any path
# under it that is not a built file gets index.html, so the app's own routes
# survive a reload.
APP_PATH = "/app"
APP_DIR = Path(__file__).resolve().parent / "web" / "dist"
# The fleet's screens, all drawn by the React app (web/src/ui/router.ts routes
# them): these paths answer with its index.html. Without a build in dist/
# they are a 404 that says how to build it.
REACT_ROUTE = re.compile(r"^/?$|^/(library|costs|runs|tree|kanban)$|^/(tree|project|page|view)/[^/]+$"
                         r"|^/c/[^/]+(/(kanban|blackboard|records|agents|settings|review))?$")
# A cadre's navigation (runnerlib/cadres.py nav): GET /api/nav?cadre=<id>.
NAV_API_PATH = "/api/nav"
# The newest events on the blackboard (runnerlib/blackboard.py recent), read
# only: GET /api/blackboard?limit=<n>. The Blackboard tab draws them.
BLACKBOARD_API_PATH = "/api/blackboard"
# Learn's Cards tab and home: the deck, its topics and Learn's study sessions.
DECK_API_PATH = "/api/deck"
# A cadre's records (runnerlib/records.py): GET /api/records?cadre=<id>. Each
# opens as a review page through the page server's /open?file=.
RECORDS_API_PATH = "/api/records"
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
# driver with nothing to click but the browser's back button. The frame is a
# screen of the React app (REACT_ROUTE); what it reads is VIEW_API_PATH<key>.
VIEW_PATH = "/view/"
VIEW_API_PATH = "/api/view/"
# Cadre's notification center (runnerlib/notifications.py); the page server
# has no notifications of its own.
NOTIFICATIONS_API_PATH = "/api/notifications"
# Your deck (personal plus the team cards synced into it), and where a study
# session is started from a page or from the deck.
CARDS_PATH = "/cards"
STUDY_PATH = "/study"
# Keys archived from this server. The row leaves the snapshot at the daemon's
# next pass; until then it is hidden here, so a click visibly does something.
_ARCHIVED: set[str] = set()
SESSION_PREFIX = "/session/"
SESSION_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_TASK_BYTES = 64 * 1024
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
    lets this machine's fleet frame it. The React app on another configured
    machine (CORS_ORIGINS: the same origins that may read this API) frames it
    too when that machine's app has this one chosen; every other site is
    still refused.
    Returns the value to send, or None to drop the header."""
    if not path.startswith(SESSION_PREFIX):
        return value
    lname = name.lower()
    if lname == "x-frame-options":
        return None
    if lname == "content-security-policy":
        return value.replace("frame-ancestors 'none'", " ".join(["frame-ancestors 'self'",
                                                                 *sorted(CORS_ORIGINS)]))
    return value


def view_payload(key: str) -> dict:
    """What the React frame around one surface (/view/<key>) reads besides the
    surface itself: the session it frames, the page's title, and the task
    when it is a dialogue's page (the frame's study box starts a session
    beside that dialogue). A key the snapshot does not know still frames:
    the page server may know it."""
    session = SESSION_PREFIX + key
    row = next((r for r in read_snapshot().get("surfaces") or [] if r.get("path") == session), {})
    _, _, pages = load_graph()
    title = (pages.get(key) or {}).get("title") or row.get("title") or ""
    return {"key": key, "session": session, "title": str(title), "task": row.get("task") or None}


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


STATUS_FETCHERS = 16


def fetch_agent_statuses(surfaces: list[dict], base: str | None = None) -> dict:
    """Live agent state per session key, from review-surface's read-only
    /api/:key/agent-status (never consumes — safe on every render). Asked
    STATUS_FETCHERS at a time: one after another, a hundred pages cost half a
    second of round trips on every fleet build. A key that fails is left out;
    with the surface server down every ask fails within the same 0.5 s
    timeout, together rather than one after another."""
    root = (base or SURFACE).rstrip("/")
    keys = [k for k in (str(sf.get("path") or "").rsplit("/", 1)[-1] for sf in surfaces) if k]

    def one(key):
        try:
            with urllib.request.urlopen(f"{root}/api/{key}/agent-status", timeout=0.5) as resp:
                return key, json.loads(resp.read().decode())
        except Exception:
            return key, None

    if not keys:
        return {}
    with ThreadPoolExecutor(min(STATUS_FETCHERS, len(keys))) as pool:
        return {k: st for k, st in pool.map(one, keys) if st is not None}


def _iso_epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


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


def surface_state_dir() -> Path:
    """review-surface's state directory: the page server this fleet proxies
    to shares its environment, so the same variable names it."""
    return Path(os.environ.get("REVIEW_SURFACE_STATE_DIR") or Path.home() / ".review-surface")


def install_surface_theme(state_dir: Path | None = None) -> Path | None:
    """Write the theme for the review server's own controls (its bar, panel,
    menus and annotation card) where it reads it on every page load, so the
    pages and what frames them wear one theme; return where it went. Best
    effort: a page server that cannot be themed still serves pages. The temp
    name is per process, so two fleets sharing a state directory cannot
    publish each other's half-written file."""
    d = Path(state_dir) if state_dir is not None else surface_state_dir()
    tmp = d / f"theme.css.{os.getpid()}.tmp"
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp.write_text(theme_mod.review_surface_css(), encoding="utf-8")
        tmp.replace(d / "theme.css")
        return d / "theme.css"
    except OSError as e:
        print(f"statusd: could not write the review server's theme in {d}: {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def journal_batches(artifact: str, state_dir: Path | None = None) -> list[dict]:
    """The per-surface answer history: every feedback batch the driver ever
    sent, from the feedback journal review-surface writes at accept time
    (append-only, survives delivery — this is the 'versions of my answers'
    record, already on disk). The journal lives beside review-surface's
    STATE file (one journal for all sessions), and each record carries the
    artifact path it belongs to — filter, don't glob."""
    if not artifact:
        return []
    root = Path(state_dir) if state_dir is not None else surface_state_dir()
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


def load_projects(data_dir: Path | None = None) -> dict:
    return projects_mod.load(DATA_DIR if data_dir is None else data_dir)


# How long a board request may sit unclaimed before the fleet stops calling it
# queued. Generous against the runner's poll interval; a request still unheld
# after this is not waiting, it is lost.
STARTUP_GRACE = 300.0


_REQUESTED: dict = {}


def _board_stamp(db: Path) -> tuple:
    """The board's size and mtime, its write-ahead log's included: a write
    lands in board.db-wal first, so board.db alone can look unchanged."""
    out = []
    for f in (db, db.with_name(db.name + "-wal")):
        try:
            st = os.stat(f)
            out.append((st.st_size, st.st_mtime_ns))
        except OSError:
            out.append(None)
    return tuple(out)


def requested_tasks(data_dir) -> dict:
    """{task id: registry-shaped record} for task requests on the board.

    Kept until the board changes (_board_stamp): one fleet build asks three
    times, and the board is the daemon's whole event log (hundreds of
    thousands of rows), which these queries scan; each read cost ~25 ms.

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
    stamp = _board_stamp(db)
    hit = _REQUESTED.get(db)
    if hit and hit[0] == stamp:
        return {k: {**v, "active_runs": {}} for k, v in hit[1].items()}
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
    _REQUESTED[db] = (stamp, out)
    return {k: {**v, "active_runs": {}} for k, v in out.items()}


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


# The home page's own rules. No Python page draws the home any more: they live
# here only so web/scripts/sync_fleet_css.py builds the React app's fleet.css
# from _CSS + this, one source for the rules the Python pages still share.
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


def load_graph() -> tuple[list, dict, dict]:
    """(link records, holders, pages) from disk, for the live page. Tests
    pass their own; every read here degrades to empty, never to an error."""
    records, holders = conv_mod.load_links(conv_mod.link_files(PANEL_LINKS))
    return records, holders, conv_mod.load_pages()


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


LANE, ROW = 14, 24     # the item graph: px between depth lanes, px per row


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


def render_cards(deck: list[dict], due: int, dirs: list[str], notice: str = "",
                 now: float | None = None) -> str:
    """Learn's Cards tab, also served alone at /cards: your deck on this
    machine (your own cards and the team cards synced into it), what is due,
    and a box to start a study session. The deck is
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
             '<p class="meta">A study session in Learn: due cards first, one short idea, a '
             'few questions, graded next round. It reads the material in the folder you '
             'pick; it never changes it.</p>'
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
            'Study sessions and threads propose cards about what you learned; they wait '
            'under Proposed until you keep or drop them. Team cards arrive with each '
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


def project_payload(pid: str) -> dict | None:
    """/project/<id>'s data, or None for no such project: the project and its
    group and members, its open conversations with what the fleet list reads
    about them, what finished and what is starting, its context store, what a
    task launched here is told, and the skills the launcher offers."""
    projs = load_projects()
    p = projects_mod.resolve(projs, pid)
    if p is None:
        return None
    mine = {m["id"]: m for m in [p, *projects_mod.members(projs, p["id"])]}
    names = set().union(*(projects_mod.names_of(projs, k) for k in mine))
    snap = read_snapshot()
    graph = load_graph()
    rows = [r for r in conv_mod.collapse(snap.get("surfaces") or [], *graph)
            if str(r.get("project") or "") in names and conv_mod.key_of(r) not in _ARCHIVED]
    turns, _, pages = load_runs(titles=graph[2])
    closed_all = closed_pages(projs)
    start_all = starting_tasks(projs)
    dirs = [d for m in mine.values() for d in m.get("dirs") or ()]
    try:
        where = projects_mod.workdir(projs, p["id"])
    except projects_mod.ProjectError:
        where = ""
    ctx = projects_mod.context_path(projs, p["id"])
    group = projs.get(p.get("group") or "")
    arts = {str(r.get("artifact") or "") for r in rows} - {""}
    return {
        "project": p, "mine": mine,
        "group": {"id": group["id"], "name": group["name"]} if group else None,
        "members": projects_mod.members(projs, p["id"]),
        "rows": rows, "statuses": fetch_agent_statuses(rows), "doing": live_turns(),
        "spent": costs_mod.per_task(turns), "returned": recent_mod.last_returned(turns),
        "tree": load_tree(pages), "histories": {a: len(journal_batches(a)) for a in arts},
        "closed": sorted((m for k in mine for m in closed_all.get(k, [])),
                         key=lambda m: -(m.get("opened") or 0)),
        "starting": sorted((s for k in mine for s in start_all.get(k, [])),
                           key=lambda s: -(s.get("since") or 0)),
        "where": where,
        "prompt": projects_mod.prompt_block(projs, p["id"], where),
        "context": ({"path": ctx, "brief": projects_mod.brief(ctx)[1][:4000],
                     "decisions": [Path(d).name for d in projects_mod.decisions(ctx)]} if ctx else None),
        "library": library_mod.scan(dirs=dirs[:1], skills_source=_skills_source()),
        "fleet_page_code": {"stale": _CODE.stale(), "since": _CODE.since, "head": _CODE.head},
    }


def page_payload(key: str) -> dict | None:
    """/page/<key>'s data, or None for an unknown page: the conversation the
    key belongs to (its newest round as the head, the rest as `earlier`),
    the projects it can move to, the conversations that can replace it (same
    project first) and who owns the page."""
    snap = read_snapshot()
    records, holders, pages = load_graph()
    surfaces = snap.get("surfaces") or []
    row = conv_mod.conversation_of(key, surfaces, records, holders, pages)
    if row is None:
        return None
    rows = conv_mod.collapse(surfaces, records, holders, pages)
    projects = sorted({str(r["project"]) for r in rows if r.get("project")}
                      | {h for h in holders if h not in conv_mod.ROOT_HOLDERS})
    cands = sorted((r for r in rows if conv_mod.key_of(r)),
                   key=lambda r: (r.get("project") != row.get("project"), -(r.get("opened") or 0)))
    return {"key": key, "row": row, "projects": projects,
            "candidates": [{"key": conv_mod.key_of(r), "title": r.get("title") or conv_mod.key_of(r)}
                           for r in cands],
            "owner": (surface_mod.ownership(_CFG, str(row["artifact"]))
                      if _CFG and row.get("artifact") else None)}


def tree_payload() -> dict:
    """/tree's data: every session that launched or was launched, by task."""
    return {"nodes": load_tree()}


def backends_payload() -> dict:
    """The machines the React app can switch between: this one first (url ""
    means "where the app was loaded from"), then every configured
    [[backends]] entry, which the app reads and posts to directly."""
    return {"backends": [{"name": b["name"] or MACHINE, "url": ""} if b["url"] is None
                         else {"name": b["name"] or b["url"], "url": b["url"]} for b in BACKENDS]}


def _is_cadre_tab(path: str) -> bool:
    """A cadre's tab in the app (/c/<id>/<tab>), where a form posted from it returns."""
    return bool(re.fullmatch(r"/c/[A-Za-z0-9_.~-]+(/[a-z]+)?", path or ""))


def deck_payload(now=None) -> dict:
    """GET /api/deck: what Learn's Cards tab and home draw. The deck (due,
    in review, proposed), its topics (a card's `source`), the folders a study
    can run in, and the study sessions Learn took from the board
    (runnerlib/learn.py), newest first."""
    out = {"due": [], "review": [], "proposed": [], "discarded": 0, "topics": [],
           "dirs": [], "sessions": [], "notice": ""}
    if not _CFG:
        out["notice"] = "No runner config: no deck here."
        return out
    def row(c):
        return {"id": c["id"], "front": c["front"], "source": c["source"],
                "kind": c.get("kind") or "fact", "anchor": c.get("anchor") or "",
                "scope": c.get("scope") or "personal",
                "due_at": str(c.get("fsrs_due") or "") if c["status"] == "accepted" else ""}
    try:
        deck = cards_mod.listing(_CFG.cards_dir)
        due = cards_mod.due(_CFG.cards_dir, now=now)
    except Exception as e:                      # a broken deck must not 500 the tab
        out["notice"] = f"The deck could not be read: {e}"
        deck, due = [], []
    due_ids = {c["id"] for c in due}
    out["due"] = [row(c) for c in due]
    out["review"] = sorted((row(c) for c in deck if c["status"] == "accepted"), key=lambda r: r["due_at"])
    out["proposed"] = [row(c) for c in deck if c["status"] == "proposed"]
    out["discarded"] = sum(1 for c in deck if c["status"] == "discarded")
    topics: dict = {}
    for c in deck:
        if c["status"] == "accepted":
            t = topics.setdefault(c["source"], {"source": c["source"], "cards": 0, "due": 0})
            t["cards"] += 1
            t["due"] += c["id"] in due_ids
    out["topics"] = sorted(topics.values(), key=lambda t: (-t["due"], -t["cards"], t["source"]))
    projs = load_projects()
    out["dirs"] = list(dict.fromkeys(d for p in projs.values() if not p.get("archived")
                                     for d in p.get("dirs") or ()))
    from runnerlib import learn
    out["sessions"] = [{**s, "page": "/open?file=" + urllib.parse.quote(s["page"], safe="/")}
                       for s in learn.sessions(_CFG)]
    return out


def newwork_payload() -> dict:
    """What the React app's New work dialog needs that /api/fleet does not
    carry: this machine's home directory (a typed ~ resolves against it) and
    the skill picker's library. The Python page inlines both."""
    return {"home": str(Path.home()),
            "library": run_library(library_mod.scan(dirs=[], skills_source=_skills_source()))}


def _task_label(task_id: str) -> str:
    """`task-fix-the-thing-20260925-124025-badfc9` reads as `fix the thing`."""
    body = re.sub(r"^task-", "", str(task_id))
    body = re.sub(r"-\d{8}-\d{6}(-[0-9a-f]+)?$", "", body)
    return body.replace("-", " ") or str(task_id)


# --------------------------------------------------------------- live + fix

_VOLATILE = ("ts", "iso", "log_tail")


def _local_signature() -> str:
    """What the fleet looks like from this machine's files, as one hash: the
    runner's snapshot minus its heartbeat fields (it is rewritten every pass
    whether or not anything moved), plus the size and mtime of every link
    store, the project store, the item store (tags, done, hidden), the kanban
    board (so a card an agent moves reaches every open Board screen through
    the fleet stream) and the run log, which gains a line each time a turn
    finishes (its cost, Recent, the item's last-returned time)."""
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
              DATA_DIR / items_mod.FILE, DATA_DIR / kanban_mod.FILE, DATA_DIR / "history.jsonl"]:
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
    """Everything the fleet list needs for this machine, as JSON. Anything the
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
            # which cadre owns each run, and what each spent this week
            "cadres": (cmap := cadres_mod.load(DATA_DIR)),
            # and the other cadres a run is shown in (a study session, in
            # the cadre that asked for it)
            "cadres_also": cadres_mod.load_also(DATA_DIR),
            "cadre_list": cadres_mod.cadre_list(projs),
            "cadre_week_usd": cadres_mod.week_usd(turns, cmap, projs),
            "fleet_page_code": {"stale": _CODE.stale(), "since": _CODE.since,
                                "head": _CODE.head}}


# /api/fleet, built once per version and kept warm. Building it reads the
# registry, the run log and every page's agent state (~0.1-0.5 s); every
# request used to pay that. The copy is keyed by fleet_version, which moves
# whenever anything the fleet shows moved — the same test the fleet stream
# uses to tell browsers to re-fetch — and is rebuilt anyway once PAYLOAD_TTL
# old, for what moves without a version (a running agent's "doing" clause),
# on the same clock the Python fleet keeps its copy (backends.FLEET_TTL).
# A warmer thread rebuilds it as soon as the version moves, so the request
# that follows a change, or the first look after a quiet hour, finds it ready.
# A build is ~0.2 s of CPU: idle, that is 1% of a core.
PAYLOAD_TTL = backends_mod.FLEET_TTL
PAYLOAD_TICK = 1.0
_PAYLOAD = {"version": None, "at": 0.0, "body": b"", "gz": b""}
_PAYLOAD_LOCK = threading.Lock()


def fleet_payload_bytes(now: float | None = None) -> tuple[bytes, bytes]:
    """(JSON, the same gzipped) of fleet_payload(), from the copy when it is
    still this version's and young enough, else built now. One build at a
    time: a request that arrives during a build waits for it, not a second."""
    with _PAYLOAD_LOCK:
        version = fleet_version()["version"]
        now = time.time() if now is None else now
        if _PAYLOAD["version"] == version and now - _PAYLOAD["at"] < PAYLOAD_TTL:
            return _PAYLOAD["body"], _PAYLOAD["gz"]
        body = json.dumps(fleet_payload(), default=str).encode()
        _PAYLOAD.update(version=version, at=now, body=body, gz=gzip.compress(body, 6))
        return body, _PAYLOAD["gz"]


def forget_payload():
    """Drop the copy: after a write through this server, the next read shows it."""
    with _PAYLOAD_LOCK:
        _PAYLOAD["version"] = None


def _warm_payload(stop: threading.Event):
    """Keep fleet_payload_bytes' copy current, once a PAYLOAD_TICK; a build
    that fails is retried on the next tick and costs no request anything."""
    while not stop.wait(PAYLOAD_TICK):
        try:
            fleet_payload_bytes()
        except Exception as e:                  # a bad read must not end the warmer
            print(f"statusd: fleet payload build failed: {e}", file=sys.stderr)


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


# Text answers are gzipped for a browser that takes it (Handler._send_bytes):
# the fleet is read over a tailnet, often a phone's, and the React bundle,
# /api/fleet and the Python home are each hundreds of KB as plain text.
GZIP_MIN = 1024
COMPRESSIBLE = ("text/", "application/json", "application/javascript", "image/svg+xml")
_STATIC: dict[Path, tuple[tuple, bytes, bytes]] = {}
_INDEX: dict = {}
INLINE_FLEET_ID = "cadre-fleet"


def static_bytes(p: Path) -> tuple[bytes, bytes | None]:
    """(a file's bytes, the same gzipped) — the gzip kept until the file
    changes, so a static file is compressed once, not per request."""
    st = p.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _STATIC.get(p)
    if hit and hit[0] == stamp:
        return hit[1], hit[2]
    data = p.read_bytes()
    gz = gzip.compress(data, 9) if TYPES.get(p.suffix, "").startswith(COMPRESSIBLE) else None
    _STATIC[p] = (stamp, data, gz)
    return data, gz


def index_with_fleet(index: Path) -> tuple[bytes, bytes]:
    """The React app's index.html with this machine's /api/fleet inside it,
    as <script type="application/json" id="cadre-fleet"> (the app reads it in
    place of its first GET /api/fleet: web/src/ui/useFleet.ts). Every "<"
    in the JSON is written \u003c, so nothing in a title can close the tag.
    Kept per payload build; (html, gzipped html)."""
    body, _ = fleet_payload_bytes()
    html, _ = static_bytes(index)
    if _INDEX.get("body") is not body or _INDEX.get("index") != html:
        tag = (f'<script type="application/json" id="{INLINE_FLEET_ID}">'.encode()
               + body.replace(b"<", b"\\u003c") + b"</script>\n")
        out = html.replace(b"</head>", tag + b"</head>", 1)
        _INDEX.update(body=body, index=html, html=out, gz=gzip.compress(out, 6))
    return _INDEX["html"], _INDEX["gz"]


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

    def _serve_app(self, rel: str, with_fleet: bool = False):
        """A built file of the React app, or index.html for one of the app's own
        routes. A missing file (a path with an extension, such as an old
        build's hashed chunk) is a 404: index.html served as JavaScript only
        breaks the page with a MIME error. `with_fleet` (the home page)
        carries this machine's /api/fleet inside index.html, so the list draws
        as soon as the app runs instead of a round trip after it."""
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
            self.send_error(404, "the React UI is not built: cd web && npm run build")
            return
        if with_fleet and target == root / "index.html":
            html, gz = index_with_fleet(target)
            self._send_bytes(html, TYPES[".html"], gz=gz)
            return
        # Vite names every built asset by its content hash, so it never changes
        hashed = target.parent == root / "assets"
        self._serve_static(target, "public, max-age=31536000, immutable" if hashed else "no-store")

    def _serve_static(self, p: Path, cache: str = "no-store"):
        data, gz = static_bytes(p)
        self._send_bytes(data, TYPES.get(p.suffix, "application/octet-stream"), cache, gz=gz)

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
        self._send_bytes(body.encode("utf-8"), TYPES[".html"], code=code, headers=headers)

    def _backends(self) -> list[dict]:
        return backends_mod.with_self(BACKENDS, self.server.server_address)

    def _serve_api_fleet(self, version_only: bool, cadre: str = ""):
        if version_only:
            self._send_json(fleet_version())
            return
        body, gz = fleet_payload_bytes()
        if cadre:
            # one cadre's runs only (cadres.payload_for), for readers other
            # than the app, which filters the whole payload itself
            self._send_json(cadres_mod.payload_for(json.loads(body), cadre))
            return
        self._send_bytes(body, "application/json", gz=gz, cors=True)

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
        self._send_bytes(json.dumps(data, default=str).encode(), "application/json", cors=True)

    def _accepts_gzip(self) -> bool:
        for part in (self.headers.get("Accept-Encoding") or "").lower().split(","):
            name, _, params = part.strip().partition(";")
            if name.strip() in ("gzip", "*"):
                return params.replace(" ", "") not in ("q=0", "q=0.0", "q=0.00", "q=0.000")
        return False

    def _send_bytes(self, body: bytes, ctype: str, cache: str = "no-store", gz: bytes | None = None,
                    cors: bool = False, code: int = 200, headers: dict | None = None):
        """One answer, gzipped when it is text worth compressing and the
        browser takes gzip (`gz` is the body already compressed, when the
        caller keeps a copy). Over a tailnet link the bytes are the cost:
        /api/fleet is 240 KB as JSON and about a tenth of that gzipped."""
        text = ctype.startswith(COMPRESSIBLE)
        squeeze = text and len(body) >= GZIP_MIN and self._accepts_gzip()
        if squeeze:
            body = gz if gz is not None else gzip.compress(body, 6)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if text:
            self.send_header("Vary", "Accept-Encoding")
        if squeeze:
            self.send_header("Content-Encoding", "gzip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if cors:
            self._send_cors()
        self.end_headers()
        if self.command != "HEAD":   # a HEAD body puts stray bytes on the wire
            self.wfile.write(body)

    def _serve_api_cards(self):
        """A team backend's deck, for teammates' machines to copy in
        (`pipeline.py cards sync`). Only a backend configured as the team's
        answers: a personal deck is never served, whatever this binds to."""
        if not (_CFG and _CFG.learning.get("cards") == "team"):
            self.send_error(404, "no team cards here")
            return
        self._send_json({"cards": cards_mod.export_team(_CFG.cards_dir)})

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
        back = form.get("back") or ""
        self._redirect(back if _is_cadre_tab(back) else CARDS_PATH, notice)

    def _study(self):
        """POST /study: topic, and either `from` (the dialogue whose page needs
        it — from the bar above a page) or `cwd` (from the deck). `back` is
        where to land with the outcome."""
        form = self._form()
        if form is None:
            return
        back = form.get("back") or CARDS_PATH
        if not (back.startswith(VIEW_PATH) or back == CARDS_PATH or _is_cadre_tab(back)):
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
            from runnerlib import learn
            got = learn.request(_CFG, form.get("topic", ""), from_task=form.get("from") or "",
                                cwd=cwd, page=str((row or {}).get("artifact") or ""),
                                log=lambda *_: None)
        except Exception as e:                      # a bad ask must not 500 the page
            self._redirect(back, f"Study not started: {e}")
            return
        self._redirect(back, ("Joined the study session already open on this." if got.folded
                              else "Study asked for: another Learn took it, and answers on the board."
                              if not got.session
                              else "Study started in Learn — it joins the fleet when its first "
                                   "round is ready.")
                       + (" (No study board here, so no answer comes back to this page.)"
                          if got.via == "direct" and form.get("from") else ""))

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

    def _session_into_frame(self, path: str, raw_query: str) -> bool:
        """A review page asked for as the tab's own page goes to the fleet's
        frame around it (/view/<key>, query kept; the browser keeps the
        #fragment), which runs the notification center. Only a top-level
        navigation the browser marks as one (Sec-Fetch-Dest: document) moves:
        the frame's own load (iframe), a thread window (?embed=), clients that
        send no such header (curl, the CLI), and every request while the app
        is not built are forwarded to the page server unchanged."""
        key = path[len(SESSION_PREFIX):] if path.startswith(SESSION_PREFIX) else ""
        if (not SESSION_KEY.fullmatch(key) or "embed" in urllib.parse.parse_qs(raw_query, keep_blank_values=True)
                or self.headers.get("Sec-Fetch-Dest") != "document" or not (APP_DIR / "index.html").is_file()):
            return False
        self.send_response(302)
        self.send_header("Location", view_href(path) + ("?" + raw_query if raw_query else ""))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

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

    def _kanban_post(self):
        """POST /kanban: action=add (title, column, note, item) | move (card,
        column, before) | edit (card, title, note) | remove (card). Writes
        <data_dir>/kanban.json as the driver and lands back on the Board with
        what happened. An edit changes only the fields the form sent."""
        form = self._form()
        if form is None:
            return
        fields = {k: form.get(k, "") for k in ("card", "column", "item", "before")}
        try:
            msg, _ = kanban_mod.apply(DATA_DIR, form.get("action", ""), **fields,
                                      title=form.get("title"), note=form.get("note"),
                                      by=kanban_mod.DRIVER)
        except ValueError as e:
            msg = f"Refused: {e}"
        self._redirect(KANBAN_PATH, msg)

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
        extra = {k: v for k in ("parent", "run", "cadre", "card")
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
            forget_payload()
        path, _, raw_query = self.path.partition("?")
        query = urllib.parse.parse_qs(raw_query)
        if self.command in ("GET", "HEAD") and REACT_ROUTE.match(path):
            self._serve_app("", with_fleet=path in ("", HOME_PATH) or path.startswith("/c/"))
            return
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
        if path == KANBAN_PATH and self.command == "POST":
            self._kanban_post()
            return
        if path == NOTIFICATIONS_API_PATH and self.command in ("GET", "HEAD"):
            self._send_json(notifications_mod.payload(surface_state_dir(), DATA_DIR))
            return
        if self.command == "GET" and self._session_into_frame(path, raw_query):
            return
        if path == KANBAN_API_PATH and self.command in ("GET", "HEAD"):
            self._send_json(kanban_mod.load(DATA_DIR))
            return
        if (path == PROJECTS_PATH or path.startswith(PROJECTS_PATH + "/")) \
                and self.command == "POST":
            self._projects_post(path)
            return
        if path == COSTS_PATH + ".json" and self.command in ("GET", "HEAD"):
            self._send_json(costs_payload())
            return
        if path in (backends_mod.FLEET_PATH, backends_mod.VERSION_PATH) \
                and self.command in ("GET", "HEAD"):
            self._serve_api_fleet(path == backends_mod.VERSION_PATH,
                                  (query.get("cadre") or [""])[0].strip())
            return
        if path == NAV_API_PATH and self.command in ("GET", "HEAD"):
            projs = load_projects()
            cadre = cadres_mod.resolve(projs, (query.get("cadre") or [""])[0])
            if cadre is None:
                self.send_error(404, "no such cadre")
            else:
                self._send_json(cadres_mod.nav(projs, cadre,
                                               board=kanban_mod.load(DATA_DIR)["board"]))
            return
        if path == RECORDS_API_PATH and self.command in ("GET", "HEAD"):
            cadre = cadres_mod.resolve(load_projects(), (query.get("cadre") or [""])[0])
            if cadre is None:
                self.send_error(404, "no such cadre")
            else:
                self._send_json({"records": [
                    {**r, "href": "/open?file=" + urllib.parse.quote(r["path"], safe="/")}
                    for r in records_mod.query(DATA_DIR, cadre=cadre)]})
            return
        if path == DECK_API_PATH and self.command in ("GET", "HEAD"):
            self._send_json(deck_payload())
            return
        if path == BLACKBOARD_API_PATH and self.command in ("GET", "HEAD"):
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError:
                limit = 100
            self._send_json({"events": blackboard_mod.recent(DATA_DIR / "board.db", limit)})
            return
        if path.startswith(VIEW_API_PATH) and self.command in ("GET", "HEAD"):
            key = urllib.parse.unquote(path[len(VIEW_API_PATH):])
            if SESSION_KEY.fullmatch(key):
                self._send_json(view_payload(key))
            else:
                self.send_error(404, "no such surface")
            return
        if path.startswith(("/api/project/", "/api/page/")) and self.command in ("GET", "HEAD"):
            which, _, ref = path[len("/api/"):].partition("/")
            d = (project_payload if which == "project" else page_payload)(urllib.parse.unquote(ref))
            if d is None:
                self.send_error(404, f"no such {which}")
            else:
                self._send_json(d)
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
    try:                    # runs launched before cadres owned them get one, once
        filled = cadres_mod.migrate_dir(DATA_DIR)
        if filled:
            print(f"statusd: gave {filled} earlier runs a cadre", file=sys.stderr)
    except Exception as e:  # bookkeeping must not keep the API down
        print(f"statusd: cadre migration failed: {e}", file=sys.stderr)
    threading.Thread(target=_warm_payload, args=(threading.Event(),), daemon=True,
                     name="fleet payload").start()
    theme = install_surface_theme()
    print(f"statusd: serving {STATUS_DIR} on {', '.join(bind or BIND)} port {port or PORT}, "
          f"proxying the rest to {SURFACE}"
          + (f"; its theme in {theme}" if theme else "")
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
