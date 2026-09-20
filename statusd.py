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

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runnerlib import config as config_mod
from runnerlib import surface as surface_mod


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


# Env wins over config wins over the defaults: a unit file overrides one dial
# without a config edit, and the config is what keeps the served directory the
# SAME directory the daemon writes its status into (they drifted while this was
# a literal path).
STATUS_DIR = Path(os.environ.get("CADRE_STATUS_DIR") or
                  (_CFG.data_dir / "status" if _CFG
                   else Path(os.path.expanduser(_DEFAULTS["data_dir"])) / "status"))
SURFACE = surface_mod.upstream()
BIND = os.environ.get("CADRE_STATUS_BIND") or _runner("status_bind")
PORT = int(os.environ.get("CADRE_STATUS_PORT") or _runner("status_port"))
BLOCKED = {"/shutdown"}
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "host",
               "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "upgrade"}
TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json",
         ".css": "text/css", ".js": "text/javascript", ".png": "image/png",
         ".svg": "image/svg+xml"}

HOME_PATH = "/"
TASKS_PATH = "/tasks"
MAX_TASK_BYTES = 64 * 1024
REFRESH_MS = 5000

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
                      "pr": sf.get("pr"), "opened": sf.get("opened")}
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


def agent_state_badge(st: dict | None, now: float | None = None) -> tuple[str, str]:
    """(label, css class) for a session's live agent state — the driver's
    question is 'did my answer land, is the agent on it, or did it stall',
    so the states are named from THEIR side of the loop."""
    if not st:
        return ("", "")
    if st.get("status") == "ended":
        return ("ended", "")
    pending = int(st.get("pending_prompts") or 0)
    presence = st.get("presence")
    if pending and presence == "waiting":
        # feedback is sitting in the queue and no agent poll is attached —
        # the one state that means "stalled", and the one worth alarming on
        return ("queued — no agent listening", "needs")
    if pending:
        return ("delivering to agent", "running")
    if presence == "working":
        return ("agent working", "running")
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
        back += f' · <a href="{escape(str(sf["path"]))}">open surface</a>'
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
a.row .go{color:var(--accent);font-size:.78rem;font-family:var(--mono)}
@media (prefers-reduced-motion:reduce){a.row{transition:none}}
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
footer{color:var(--label);font-size:.74rem;font-family:var(--mono);margin-top:1.4rem}
footer a{color:var(--accent)}
"""

_POLL_JS = """
(function(){
 var ms=%d;
 setInterval(function(){
  var a=document.activeElement;
  if(a&&a.closest&&a.closest('form.newtask'))return;   // never eat a half-typed task
  fetch('/?partial=1',{cache:'no-store'}).then(function(r){return r.text()})
   .then(function(h){var el=document.getElementById('fleet');if(el)el.innerHTML=h})
   .catch(function(){});
 },ms);
})();
""" % REFRESH_MS


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
        bits.append(f'<a href="{escape(sf["path"])}">surface: {escape(label)}</a>')
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


def render_fleet(snap: dict, now: float | None = None,
                 statuses: dict | None = None) -> str:
    """The hierarchy fragment — also what the poll swaps in, so the page and
    the refresh can never render two different shapes. `statuses` is the
    live agent-state map from fetch_agent_statuses; None renders without
    live badges (tests, surface server down)."""
    now = time.time() if now is None else now
    statuses = statuses or {}
    projects = fleet(snap)
    out = []

    def _live(sf) -> str:
        key = str(sf.get("path") or "").rsplit("/", 1)[-1]
        st = statuses.get(key)
        label, cls = agent_state_badge(st, now)
        bits = ""
        if label:
            bits += f'<span class="badge {cls}">{escape(label)}</span>'
        upd = _iso_epoch((st or {}).get("updated_at"))
        if upd:
            bits += f'<span class="badge">upd {escape(_rel_time(upd, now))}</span>'
        return bits

    def _history_link(sf) -> str:
        n = len(journal_batches(str(sf.get("artifact") or "")))
        if not n:
            return ""
        key = str(sf.get("path") or "").rsplit("/", 1)[-1]
        return f'<a class="hist" href="/history/{escape(key)}">history ({n})</a>'
    for ext in external_projects(snap):
        # An orchestrator is the project's parent, not a sibling of the pages
        # under it: it renders as the header's byline. Only page-less
        # orchestrators lift — one WITH a page is still a row you can open.
        orch = [sf for sf in ext["rows"]
                if sf.get("role") == "orchestrator" and not sf.get("path")]
        rows_src = [sf for sf in ext["rows"] if sf not in orch]
        byline = "".join(
            f'<p class="orch">orchestrated by <b>{escape(str(sf.get("title") or ""))}</b>'
            f' · {escape(_rel_time(sf.get("opened") or 0, now))}</p>' for sf in orch)
        rows = []
        for sf in rows_src:
            role = (f'<span class="badge">{escape(str(sf["role"]))}</span>'
                    if sf.get("role") else "")
            when = f'<span class="badge">{escape(_rel_time(sf.get("opened") or 0, now))}</span>'
            title = escape(str(sf.get("title") or sf.get("kind") or ""))
            if sf.get("path"):
                # the whole row is the click target — a 12px "open surface"
                # link under each row made every open a precision task
                rows.append(f'<div class="rowline">'
                            f'<a class="row" href="{escape(str(sf["path"]))}">'
                            f'<span class="title">{title}</span>{_live(sf)}{role}{when}'
                            f'<span class="go">open →</span></a>'
                            f'{_history_link(sf)}</div>')
            else:
                rows.append(f'<div class="story"><div class="line">'
                            f'<span class="title">{title}</span>{role}{when}'
                            f'</div></div>')
        count = f'<span class="badge">{len(rows_src)} session{"s" if len(rows_src) != 1 else ""}</span>'
        out.append(f'<section class="card project"><h2>'
                   f'<span class="repo">{escape(ext["project"])}</span>{count}</h2>'
                   f'{byline}{"".join(rows)}</section>')
    orphans = orphan_surfaces(snap)
    tasks = [sf for sf in orphans if sf.get("kind") == "task"]
    orphans = [sf for sf in orphans if sf.get("kind") != "task"]

    def _orow(sf, label):
        when = f'<span class="badge">{escape(_rel_time(sf.get("opened") or 0, now))}</span>'
        return (f'<div class="rowline">'
                f'<a class="row" href="{escape(str(sf.get("path")))}">'
                f'<span class="title">{escape(label)}</span>{_live(sf)}{when}'
                f'<span class="go">open →</span></a>'
                f'{_history_link(sf)}</div>')

    if tasks:
        # a dialogue task's page is its whole deliverable — a row reading just
        # "task" with no identity was noise, not a link worth clicking
        rows = "".join(_orow(sf, str(sf.get("task") or sf.get("story")
                                     or "task")) for sf in tasks)
        out.append(f'<section class="card project"><h2>'
                   f'<span class="repo">tasks</span></h2>{rows}</section>')
    if orphans:
        rows = "".join(_orow(sf, str(sf.get("story") or sf.get("kind")
                                     or "surface")) for sf in orphans)
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
    return "".join(out)


def render_home(snap: dict, notice: str = "", now: float | None = None,
                statuses: dict | None = None) -> str:
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
        f'<span class="updated">snapshot {escape(str(stamp))}</span></header>'
        f'{banner}'
        '<section class="card"><h2>New task</h2>'
        f'<form class="newtask" method="post" action="{TASKS_PATH}">'
        '<textarea name="text" placeholder="What should the fleet do?" '
        'required autofocus></textarea>'
        '<div class="row">'
        '<input type="text" name="cwd" placeholder="working directory (optional)">'
        '<button type="submit">Dispatch</button></div></form></section>'
        f'<div id="fleet">{render_fleet(snap, now, statuses=statuses)}</div>'
        '<footer>Auto-refreshes every 5 s · '
        '<a href="/index.html">legacy dashboard</a></footer>'
        f'</div><script>{_POLL_JS}</script></body></html>')


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
        for k, v in resp.headers.items():
            if k.lower() not in HOP_HEADERS:
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
                chunk = resp.read(8192)
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
        snap = read_snapshot()
        statuses = fetch_agent_statuses(snap.get("surfaces") or [])
        if query.get("partial"):
            self._send_html(render_fleet(snap, statuses=statuses))
            return
        notice = (query.get("notice") or [""])[0]
        self._send_html(render_home(snap, notice=notice, statuses=statuses))

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
        path, _, raw_query = self.path.partition("?")
        query = urllib.parse.parse_qs(raw_query)
        if path == TASKS_PATH and self.command == "POST":
            self._new_task()
            return
        if path in ("", HOME_PATH) and self.command in ("GET", "HEAD"):
            self._serve_home(query)
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
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"statusd: serving {STATUS_DIR} on {BIND}:{PORT}, "
          f"proxying the rest to {SURFACE}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
