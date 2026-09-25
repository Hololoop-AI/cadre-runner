"""One conversation, one row.

A discussion used to show on the fleet twice: once as its original page under
its project, once as its owning dialogue's round page under a generic "tasks"
section. Both are the same work. This module folds every page of one
conversation into a single row — the newest page, under the project the
conversation belongs to — and keeps the earlier rounds on the row, reachable
but not listed.

Two sources say which pages are one conversation, used together:

- typed links from review-surface's link store (`links.jsonl` beside its
  state file, one `{type, from, to, at}` record per line, keys are session
  keys). `supersedes` joins a new page to the one it replaces; `child-of` is
  where a page lives. The fleet panel's own repair log is read after it, so
  a repair made here wins.
- what this runner already knows: every page a dialogue task opens carries
  that task's id in its session record (an adopted page is recorded under its
  task, `registry.json`'s `page` field), so pages sharing a task id are one
  conversation even with no link written.

A page with no project gets one from its working directory — the project
other pages in that checkout already use, else the checkout's name — so the
generic "tasks" bucket stops being where real work hides.

Pure functions over plain dicts; the only disk reads are the explicit loaders
at the bottom, so the page server stays read-only and the tests stay hermetic.
"""

import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

LINK_TYPES = ("child-of", "supersedes", "derived-from")
# A holder that only anchors the tree ("Home") is not a project.
ROOT_HOLDERS = {"root", "home"}
_SESSION_KEY = re.compile(r"^[0-9a-f]{16}$")
PROJECT_NAME = re.compile(r"^[\w][\w .-]{0,63}$")


def key_of(sf: dict) -> str:
    return str(sf.get("path") or "").rsplit("/", 1)[-1]


# -------------------------------------------------------------------- links

def parse_links(text: str) -> tuple[list[dict], dict]:
    """(link records oldest first, holder id -> title). Torn or foreign lines
    are skipped: this reads files other processes append to."""
    records, holders = [], {}
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("op") == "holder" and r.get("id"):
            holders[str(r["id"])] = str(r.get("title") or r["id"])
        elif r.get("type") in LINK_TYPES and r.get("from") and r.get("to"):
            records.append({"type": r["type"], "from": str(r["from"]),
                            "to": str(r["to"])})
    return records, holders


def build_graph(records: list[dict]) -> tuple[dict, dict]:
    """(parent, replaced_by). Later records win: a second child-of is a move."""
    parent, replaced_by = {}, {}
    for r in records:
        if r["type"] == "child-of":
            parent[r["from"]] = r["to"]
        elif r["type"] == "supersedes":
            replaced_by[r["to"]] = r["from"]
    return parent, replaced_by


class LinkRefused(ValueError):
    pass


def check_link(records: list[dict], type_: str, frm: str, to: str) -> str:
    """The link rules, the same ones review-surface's link write path
    enforces (src/page-graph.js checkLink): "unchanged" when the exact link is
    already recorded, "new" when it may be appended, LinkRefused otherwise."""
    if type_ not in LINK_TYPES:
        raise LinkRefused(f"link type must be one of {', '.join(LINK_TYPES)}")
    if frm == to:
        raise LinkRefused("a page cannot link to itself")
    parent, replaced_by = build_graph(records)
    if type_ == "child-of":
        if parent.get(frm) == to:
            return "unchanged"
        node, seen = to, set()
        while node in parent and node not in seen:
            seen.add(node)
            node = parent[node]
            if node == frm:
                raise LinkRefused("nesting cycle: the parent is already inside this page")
        return "new"
    if type_ == "supersedes":
        if replaced_by.get(to) == frm:
            return "unchanged"
        node, seen = frm, set()
        while node in replaced_by and node not in seen:
            seen.add(node)
            node = replaced_by[node]
            if node == to:
                raise LinkRefused("supersede cycle: the older page already replaces this one")
        if to in replaced_by:
            raise LinkRefused("that page is already replaced by another page")
        return "new"
    return "unchanged" if any(r["type"] == type_ and r["from"] == frm and r["to"] == to
                              for r in records) else "new"


# ------------------------------------------------------------------ collapse

def cwd_project(cwd: str, surfaces: list[dict]) -> str:
    """The project a checkout belongs to: the one pages already registered in
    that directory use, else the checkout's own name (dot-dirs skipped, so a
    repo's `.review-surface` folder files under the repo)."""
    if not cwd:
        return ""
    named = Counter(str(sf["project"]) for sf in surfaces
                    if sf.get("project") and sf.get("cwd") == cwd)
    if named:
        return named.most_common(1)[0][0]
    parts = [p for p in Path(cwd).parts if p not in ("/", "") and not p.startswith(".")]
    return parts[-1] if parts else ""


def _when(key: str, by_key: dict, pages: dict) -> float:
    sf = by_key.get(key)
    if sf is not None and sf.get("opened"):
        return float(sf["opened"])
    return float((pages.get(key) or {}).get("updated") or 0)


def collapse(surfaces: list[dict], records: list[dict] | None = None,
             holders: dict | None = None, pages: dict | None = None) -> list[dict]:
    """Snapshot surface rows in, one row per conversation out.

    `pages` is review-surface's page list (key -> file/title/updated/ended),
    used to show a newest round whose session is already closed and to title
    rows. Each head row gains `earlier` (older rounds, newest first) and
    `project` resolved in this order: a child-of link (a move made from the
    panel lands here, so it must win), a project recorded on any page of the
    conversation, the working directory's project. Rows with no page (a
    terminal session, a dispatch target) pass through untouched."""
    records = records or []
    holders = holders or {}
    pages = pages or {}
    parent, replaced_by = build_graph(records)
    by_key = {key_of(sf): sf for sf in surfaces if key_of(sf)}

    root: dict[str, str] = {}

    def find(x):
        root.setdefault(x, x)
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    def union(a, b):
        root[find(a)] = find(b)

    for old, new in replaced_by.items():
        union(old, new)
    # A page derived from one that is no longer live (the handoff: the driver
    # approved a page, its session closed, and an agent carried the work into
    # a new page) continues that conversation rather than starting an orphan
    # row. Two LIVE pages stay two rows — folding one under the other would
    # hide a page that still wants the driver.
    derived = [(r["from"], r["to"]) for r in records
               if r["type"] == "derived-from" and r["to"] not in by_key]
    for new, old in derived:
        union(new, old)
    by_task: dict[str, list[str]] = {}
    for k, sf in by_key.items():
        if sf.get("task"):
            by_task.setdefault(str(sf["task"]), []).append(k)
    for ks in by_task.values():
        for k in ks[1:]:
            union(ks[0], k)

    groups: dict[str, set] = {}
    for k in by_key:
        groups.setdefault(find(k), set()).add(k)
    # chain members whose session is closed are not in the snapshot; pull the
    # ones review-surface still knows into their conversation
    for k in {*replaced_by, *replaced_by.values(), *(k for pair in derived for k in pair)}:
        r = find(k)
        if r in groups and (k in by_key or k in pages):
            groups[r].add(k)

    def project_of(target: str) -> str:
        if target in ROOT_HOLDERS:
            return ""
        if target in holders:
            return target
        if target in by_key:
            return str(by_key[target].get("project") or by_key[target].get("title") or "")
        if target in pages:
            return str(pages[target].get("title") or "")
        return "" if _SESSION_KEY.match(target) else target

    out = [sf for sf in surfaces if not key_of(sf)]
    for members in groups.values():
        order = sorted(members, key=lambda k: -_when(k, by_key, pages))
        live = [k for k in order if k not in replaced_by] or order
        head_key = live[0]
        if head_key in by_key:
            head = dict(by_key[head_key])
        else:
            pg = pages[head_key]
            head = {"kind": "page", "path": f"/session/{head_key}", "stranded": [],
                    "artifact": pg.get("file") or "", "ended": bool(pg.get("ended"))}
        member_rows = [by_key[k] for k in order if k in by_key]
        # a strand anywhere in the conversation stays on its row: hiding the
        # alarm behind a newer round defeats it
        head["stranded"] = [n for sf in member_rows for n in (sf.get("stranded") or [])]
        head["opened"] = max(_when(k, by_key, pages) for k in members)
        linked = next((project_of(parent[k]) for k in [head_key, *order]
                       if k in parent and project_of(parent[k])), "")
        head["project"] = (linked
                           or next((sf["project"] for sf in member_rows if sf.get("project")), "")
                           or cwd_project(str(next((sf.get("cwd") for sf in member_rows
                                                    if sf.get("cwd")), "")), surfaces)
                           or None)
        if not head.get("title"):
            head["title"] = (next((sf["title"] for sf in member_rows if sf.get("title")), "")
                             or (pages.get(head_key) or {}).get("title")
                             or head.get("task") or "")
        if not head.get("task"):
            head["task"] = next((sf["task"] for sf in member_rows if sf.get("task")), None)
        head["page_title"] = str((pages.get(head_key) or {}).get("title") or head["title"])
        head["earlier"] = [
            {"key": k, "path": f"/session/{k}",
             # a round is named by its own page, so rounds read apart
             "title": str((pages.get(k) or {}).get("title")
                          or (by_key.get(k) or {}).get("title") or k),
             "opened": _when(k, by_key, pages)}
            for k in order if k != head_key]
        out.append(head)
    return sorted(out, key=lambda s: s.get("opened") or 0, reverse=True)


def conversation_of(key: str, surfaces: list[dict], records: list[dict],
                    holders: dict, pages: dict) -> dict | None:
    """The collapsed row a page belongs to, whether it is the head or one of
    the earlier rounds."""
    for row in collapse(surfaces, records, holders, pages):
        if key_of(row) == key or any(e["key"] == key for e in row.get("earlier") or []):
            return row
    if key in pages:
        pg = pages[key]
        return {"kind": "page", "path": f"/session/{key}", "title": pg.get("title") or key,
                "artifact": pg.get("file") or "", "ended": bool(pg.get("ended")),
                "opened": pg.get("updated") or 0, "project": None, "earlier": [],
                "stranded": []}
    return None


# --------------------------------------------------------------------- find

def find(query: str, surfaces: list[dict], records: list[dict], holders: dict,
         pages: dict, limit: int = 30) -> list[dict]:
    """Every page ever opened, not just the rows on screen: rounds folded into
    a conversation, closed sessions, pages opened outside Cadre. Title hits
    rank above path hits; newest first within each."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    _, replaced_by = build_graph(records)
    rows = collapse(surfaces, records, holders, pages)
    head_of = {}
    for row in rows:
        head_of[key_of(row)] = row
        for e in row.get("earlier") or []:
            head_of[e["key"]] = row
    by_key = {key_of(sf): sf for sf in surfaces if key_of(sf)}
    hits = []
    for key in {*pages, *by_key}:
        sf, pg = by_key.get(key) or {}, pages.get(key) or {}
        head = head_of.get(key) or {}
        title = str(sf.get("title") or pg.get("title") or key)
        hay_title = " ".join([title, str(pg.get("title") or "")]).lower()
        hay = " ".join([hay_title, str(pg.get("file") or sf.get("artifact") or ""),
                        str(head.get("project") or sf.get("project") or ""),
                        str(sf.get("task") or head.get("task") or ""), key]).lower()
        if not all(w in hay for w in words):
            continue
        state = ("replaced" if key in replaced_by
                 else "open" if key in by_key and not sf.get("ended")
                 else "ended" if pg.get("ended") else "page")
        hits.append({"key": key, "path": f"/session/{key}", "title": title,
                     "file": str(pg.get("file") or sf.get("artifact") or ""),
                     "project": head.get("project") or sf.get("project") or "",
                     "state": state, "replaced_by": replaced_by.get(key),
                     "updated": _when(key, by_key, pages),
                     "title_hit": all(w in hay_title for w in words)})
    hits.sort(key=lambda h: (not h["title_hit"], -h["updated"]))
    return hits[:limit]


# ------------------------------------------------------------------ loaders

def review_surface_dir() -> Path:
    return Path(os.environ.get("REVIEW_SURFACE_STATE_DIR")
                or Path.home() / ".review-surface")


def link_files(panel_log: Path | None) -> list[Path]:
    """Read order = precedence order (later wins): review-surface's link
    store, then the panel's own repair log. REVIEW_SURFACE_LINKS is
    review-surface's own override for where its store lives, honoured so both
    programs always read the same file."""
    rs = Path(os.environ.get("REVIEW_SURFACE_LINKS") or review_surface_dir() / "links.jsonl")
    return [rs, *([panel_log] if panel_log else [])]


def load_links(files: list[Path]) -> tuple[list[dict], dict]:
    records, holders = [], {}
    for f in files:
        try:
            r, h = parse_links(Path(f).read_text(encoding="utf-8"))
        except OSError:
            continue
        records += r
        holders.update(h)
    return records, holders


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_title_cache: dict[str, tuple[float, str]] = {}


def page_title(file: str) -> str:
    """A page's <title>, cached on mtime. Display and search text only."""
    try:
        mtime = os.stat(file).st_mtime
    except OSError:
        return Path(file).name if file else ""
    hit = _title_cache.get(file)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(file, encoding="utf-8", errors="replace") as fh:
            m = _TITLE.search(fh.read(16384))
    except OSError:
        m = None
    from html import unescape
    title = re.sub(r"\s+", " ", unescape(m.group(1))).strip() if m else ""
    title = title or Path(file).name
    _title_cache[file] = (mtime, title)
    return title


def _epoch(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def load_pages(state_dir: Path | None = None) -> dict:
    """Every page review-surface ever opened, from its state file (read-only).
    Missing or unreadable -> no pages: the fleet still renders from the
    runner's own snapshot."""
    d = Path(state_dir) if state_dir is not None else review_surface_dir()
    try:
        sessions = json.loads((d / "state.json").read_text(encoding="utf-8")).get("sessions") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for key, s in sessions.items():
        if not isinstance(s, dict):
            continue
        f = str(s.get("file") or "")
        out[str(key)] = {"file": f, "title": page_title(f),
                         "updated": _epoch(s.get("updated_at")),
                         "ended": s.get("status") == "ended"}
    return out


def append_link(log: Path, type_: str, frm: str, to: str, by: str = "cadre-panel") -> dict:
    rec = {"type": type_, "from": frm, "to": to,
           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "by": by}
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec
