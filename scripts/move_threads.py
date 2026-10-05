#!/usr/bin/env python3
"""Move dialogue threads, with their agent-side history, to another machine.

A thread is spread over six stores, and every one of them is keyed on
absolute paths, which is why the target must lay its projects out at the same
paths as the source:

    runner      registry.json record, board events (topic tasks, key task:<id>),
                history.jsonl turns, runs/<id>/, surfaces/sessions.json,
                surfaces/panel-links.jsonl, the page files and their -media dirs
    page server state.json sessions, links.jsonl, events.jsonl,
                feedback-journal.jsonl
    claude      ~/.claude/projects/<cwd as a dir name>/<session>.jsonl and
                <session>/ (subagents), for every session the thread ran

The runner's data dir is the one path that differs (cadre-local here,
cadre-shared on the team backend), so it is rewritten in every record and
page. The page the driver reads moves as it is; the agent side moves with its
transcripts, so the next turn on the target resumes the same session.

Every transcript carries the source machine's auto-memory file as an
`instructions` attachment. That file holds personal details that do not belong
on a shared machine, so it is replaced with a one-line note in transit.

Threads outside ~/Projects (a research thread whose findings the joint work
needs) are named in MOVE_INCLUDE, and their project record travels with them.
The files they read are not in any repo, so copy those over alongside.

    move_threads.py plan                # what would move; writes nothing
    move_threads.py export BUNDLE_DIR   # stage files + records
    move_threads.py import BUNDLE_JSON  # on the target, services stopped
"""

import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

HOME = Path.home()
SRC_DATA = HOME / ".local/state/cadre-local"
DST_DATA = HOME / ".local/state/cadre-shared"
SRC_RS = HOME / ".review-surface"
DST_RS = HOME / ".review-surface-shared"
DST_RS_PORT = 4389
CLAUDE_PROJECTS = HOME / ".claude/projects"

PROJECTS = str(HOME / "Projects")
JOINT_ROOT = PROJECTS + "/"
NOT_JOINT = (str(HOME / "Projects/arboreus"), str(HOME / "Projects/cadre-context"))
# Pages outside ~/Projects that belong to the joint work: the August design
# pages that fed HITL, and the two research pages on serving agent UIs.
EXTRA_PAGES = [str(p) for p in sorted((HOME / ".review-surface-views").glob("*.html"))] + [
    str(HOME / "research/agent-ui-serving-2026-09.html"),
    str(HOME / "research/agentic-ui-productionization-2026-09.html"),
]
# Whole directories that hold joint pages and the assets they link to.
EXTRA_DIRS = [HOME / "Projects/review-surface/.review-surface", HOME / ".review-surface-views"]
EXTRA_DIR_EXCLUDES = ["spikes/", "spike-no-watcher/", "stale-dist-before-rebuild/",
                      "node_modules/", "target/"]
PROJECTS_TO_CARRY = ["cadre-runner", "review-surface", "hitl", "blackboard", "picatrix"]
MEMORY_NOTE = "[auto-memory file from the source machine, removed when this thread moved]"


def rewrite(s: str) -> str:
    return s.replace(str(SRC_DATA) + "/", str(DST_DATA) + "/")


def rewrite_obj(o):
    return json.loads(rewrite(json.dumps(o)))


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def project_dirname(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# ------------------------------------------------------------------ select


def select(exclude: set[str], include: set[str] = frozenset()) -> dict:
    reg = json.loads((SRC_DATA / "registry.json").read_text())["tasks"]
    db = sqlite3.connect(SRC_DATA / "board.db")
    requests = {}
    for (payload,) in db.execute(
            "SELECT payload FROM events WHERE topic='tasks' AND kind='command' ORDER BY seq"):
        p = json.loads(payload)
        if p.get("target") == "task":
            requests[p["task"]] = p
    runner_sessions = json.loads((SRC_DATA / "surfaces/sessions.json").read_text())
    with_page = {m.get("task") for m in runner_sessions.values()}
    tasks = {}
    for tid in set(reg) | set(requests):
        # A task that never wrote a page has nothing to read and no session to
        # resume; on the target it would only sit in the fleet as "starting".
        if tid not in with_page:
            continue
        rec = reg.get(tid) or {}
        cwd = rec.get("cwd") or (requests.get(tid) or {}).get("cwd") or ""
        if tid in exclude or rec.get("active_runs"):
            continue
        if tid in include or (
                (cwd == PROJECTS or cwd.startswith(JOINT_ROOT)) and not cwd.startswith(NOT_JOINT)):
            tasks[tid] = cwd

    rs_state = json.loads((SRC_RS / "state.json").read_text())["sessions"]

    def page_of_task(f: str) -> bool:
        name = Path(f).name
        return any(name.startswith(f"task-{t}") for t in tasks)

    def joint_file(f: str) -> bool:
        if f in EXTRA_PAGES:
            return True
        if f.startswith(str(SRC_DATA / "surfaces") + "/"):
            return page_of_task(f)
        return f.startswith(JOINT_ROOT) and not f.startswith(NOT_JOINT)

    sess = {p: m for p, m in runner_sessions.items()
            if m.get("task") in tasks
            or (m.get("kind") == "external" and joint_file(p) and Path(p).exists())}
    keys = {m["key"] for m in sess.values() if m.get("key")}
    pages = {k: v for k, v in rs_state.items()
             if k in keys or (joint_file(v.get("file") or "") and Path(v["file"]).exists())}
    return {"tasks": tasks, "registry": {t: reg[t] for t in tasks if t in reg},
            "sessions": sess, "pages": pages}


# ------------------------------------------------------------------ export


def sessions_of(tid: str, rec: dict) -> dict[str, str]:
    """{claude session id: cwd} for every turn the thread ran."""
    out = {}
    if rec.get("session_id"):
        out[rec["session_id"]] = rec.get("cwd") or ""
    for run in sorted((SRC_DATA / "runs" / tid).glob("*/out.json")):
        try:
            sid = json.loads(run.read_text()).get("session_id")
        except (OSError, ValueError):
            continue
        if sid:
            out.setdefault(sid, rec.get("cwd") or "")
    return out


def find_transcript(sid: str, cwd: str) -> Path | None:
    p = CLAUDE_PROJECTS / project_dirname(cwd) / f"{sid}.jsonl"
    if p.exists():
        return p
    hits = list(CLAUDE_PROJECTS.glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


PERSONAL_SKILLS = ("medical-planning", "therapist", "apartment-hunting", "morning-plan",
                   "evening-review", "weekly-retro", "restaurant-assessor", "email",
                   "google-workspace", "resume-builder", "capture", "anthropic-skills:morning")


def _drop_personal_skills(text: str) -> str:
    return "\n".join(l for l in text.split("\n")
                     if not any(l.startswith(f"- {n}:") for n in PERSONAL_SKILLS))


def redact_line(line: str) -> tuple[str, int]:
    """Strip the source machine's auto-memory file, and the listing lines of
    its personal skills, from one transcript record. Both appear twice: in the
    attachment itself and in `rendered`, the text the model actually read."""
    try:
        o = json.loads(line)
    except ValueError:
        return line, 0
    a = o.get("attachment") if isinstance(o, dict) else None
    if not isinstance(a, dict):
        return line, 0
    n = 0
    rendered = [r for r in o.get("rendered") or [] if isinstance(r, dict)]
    if a.get("type") == "instructions":
        for f in a.get("files") or []:
            if isinstance(f, dict) and (f.get("type") == "AutoMem"
                                        or "/memory/" in str(f.get("path") or "")):
                body = f.get("content") or ""
                for r in rendered:
                    if body and body in str(r.get("content") or ""):
                        r["content"] = r["content"].replace(body, MEMORY_NOTE)
                f["content"] = MEMORY_NOTE
                n += 1
    elif a.get("type") == "skill_listing":
        a["content"] = _drop_personal_skills(str(a.get("content") or ""))
        if isinstance(a.get("names"), list):
            a["names"] = [x for x in a["names"] if x not in PERSONAL_SKILLS]
        for r in rendered:
            r["content"] = _drop_personal_skills(str(r.get("content") or ""))
        n += 1
    return (json.dumps(o, ensure_ascii=False) if n else line), n


def copy_transcript(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with src.open(encoding="utf-8") as fi, dst.open("w", encoding="utf-8") as fo:
        for line in fi:
            out, k = redact_line(line.rstrip("\n"))
            n += k
            fo.write(out + "\n")
    return n


def export(bundle: Path, exclude: set[str], include: set[str] = frozenset()) -> dict:
    sel = select(exclude, include)
    root = bundle / "root"          # mirrors / on the target
    if bundle.exists():
        shutil.rmtree(bundle)
    root.mkdir(parents=True)

    def stage(src: Path, dst: str | None = None, text: bool = False) -> None:
        target = root / (dst or rewrite(str(src))).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True)
        elif text:
            target.write_text(rewrite(src.read_text(encoding="utf-8")), encoding="utf-8")
        else:
            shutil.copy2(src, target)

    # pages that live in the runner's data dir, plus their media
    staged_pages = 0
    for meta in sel["pages"].values():
        f = Path(meta["file"])
        if str(f).startswith(str(SRC_DATA)) and f.exists():
            stage(f, text=True)
            staged_pages += 1
            media = f.with_name(f.stem + "-media")
            if media.is_dir():
                stage(media)
    for path in sel["sessions"]:
        f = Path(path)
        if str(f).startswith(str(SRC_DATA)) and f.exists() and not (root / rewrite(path).lstrip("/")).exists():
            stage(f, text=True)
            staged_pages += 1

    # joint pages kept outside ~/Projects that no directory copy carries
    for f in EXTRA_PAGES:
        if Path(f).exists() and not any(f.startswith(str(d)) for d in EXTRA_DIRS):
            stage(Path(f), dst=f)

    # run logs and claude transcripts
    transcripts, redacted, missing = 0, 0, []
    for tid, cwd in sel["tasks"].items():
        if (SRC_DATA / "runs" / tid).is_dir():
            stage(SRC_DATA / "runs" / tid)
        rec = sel["registry"].get(tid) or {"cwd": cwd}
        for sid, scwd in sessions_of(tid, rec).items():
            src = find_transcript(sid, scwd or cwd)
            if not src:
                missing.append(f"{tid} {sid}")
                continue
            redacted += copy_transcript(src, root / str(src).lstrip("/"))
            transcripts += 1
            sub = src.with_suffix("")
            if sub.is_dir():
                for child in sub.rglob("*"):
                    if child.is_file():
                        dst = root / str(child).lstrip("/")
                        if child.suffix == ".jsonl":
                            redacted += copy_transcript(child, dst)
                        else:
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(child, dst)

    keys = set(sel["pages"])
    tids = set(sel["tasks"])
    db = sqlite3.connect(SRC_DATA / "board.db")
    events = [dict(zip(("id", "ts", "namespace", "topic", "key", "kind", "provenance",
                        "payload", "correlation_id", "visible_after"), row))
              for row in db.execute(
                  "SELECT id, ts, namespace, topic, key, kind, provenance, payload, "
                  "correlation_id, visible_after FROM events WHERE topic='tasks' ORDER BY seq")
              if row[4].removeprefix("task:") in tids]
    projects = json.loads((SRC_DATA / "projects.json").read_text())["projects"]

    def touches(line: dict) -> bool:
        return bool({line.get("from"), line.get("to"), line.get("key"), line.get("subject")} & keys)

    records = {
        "tasks": sorted(tids),
        "registry": {t: {**rewrite_obj(r), "active_runs": {}} for t, r in sel["registry"].items()},
        "events": [{**e, "payload": rewrite(e["payload"])} for e in events],
        "history": [rewrite_obj(h) for h in jsonl(SRC_DATA / "history.jsonl") if h.get("story") in tids],
        "sessions": {rewrite(p): rewrite_obj(m) for p, m in sel["sessions"].items()},
        "panel_links": [l for l in jsonl(SRC_DATA / "surfaces/panel-links.jsonl") if touches(l)],
        "pages": {k: {**rewrite_obj(v),
                      "url": f"http://127.0.0.1:{DST_RS_PORT}/session/{k}"}
                  for k, v in sel["pages"].items()},
        "rs_links": [l for l in jsonl(SRC_RS / "links.jsonl") if touches(l)],
        "rs_events": [rewrite_obj(l) for l in jsonl(SRC_RS / "events.jsonl") if touches(l)],
        "rs_journal": [rewrite_obj(l) for l in jsonl(SRC_RS / "feedback-journal.jsonl") if touches(l)],
        "projects": {p: projects[p] for p in PROJECTS_TO_CARRY + sorted(
            {(sel["registry"].get(t) or {}).get("project") for t in include} - {None})
            if p in projects},
    }
    (bundle / "records.json").write_text(json.dumps(records, indent=1))
    summary = {"threads": len(tids), "open_threads": sum(
        1 for m in sel["sessions"].values() if m.get("open") and m.get("task")),
        "pages": len(keys), "staged_pages": staged_pages, "transcripts": transcripts,
        "memory_attachments_redacted": redacted, "missing_transcripts": missing,
        "events": len(events), "history": len(records["history"])}
    (bundle / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


# ------------------------------------------------------------------ import


def _merge_json(path: Path, key: str | None, new: dict) -> int:
    data = json.loads(path.read_text()) if path.exists() else ({key: {}} if key else {})
    target = data.setdefault(key, {}) if key else data
    added = 0
    for k, v in new.items():
        if k not in target:
            target[k] = v
            added += 1
    tmp = path.with_suffix(path.suffix + ".move-tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)
    return added


def _append(path: Path, lines: list[dict]) -> int:
    """Append the lines the file does not already hold. Compared as parsed
    JSON, so a re-run after a partial import adds nothing twice."""
    def canon(o) -> str:
        return json.dumps(o, sort_keys=True)
    have = {canon(o) for o in jsonl(path)}
    fresh = [l for l in lines if canon(l) not in have]
    with path.open("a", encoding="utf-8") as f:
        for l in fresh:
            # ASCII escapes: a few source lines carry a lone surrogate (half an
            # emoji), which UTF-8 cannot encode but a JSON escape can.
            f.write(json.dumps(l) + "\n")
    return len(fresh)


def do_import(records_path: Path) -> dict:
    r = json.loads(records_path.read_text())
    out = {}
    out["registry"] = _merge_json(DST_DATA / "registry.json", "tasks", r["registry"])
    out["sessions"] = _merge_json(DST_DATA / "surfaces/sessions.json", None, r["sessions"])
    out["projects"] = _merge_json(DST_DATA / "projects.json", "projects", r["projects"])
    out["pages"] = _merge_json(DST_RS / "state.json", "sessions", r["pages"])
    out["history"] = _append(DST_DATA / "history.jsonl", r["history"])
    out["panel_links"] = _append(DST_DATA / "surfaces/panel-links.jsonl", r["panel_links"])
    out["rs_links"] = _append(DST_RS / "links.jsonl", r["rs_links"])
    out["rs_events"] = _append(DST_RS / "events.jsonl", r["rs_events"])
    out["rs_journal"] = _append(DST_RS / "feedback-journal.jsonl", r["rs_journal"])

    # Board history goes in AFTER every consumer's cursor, so the engine must
    # not read it as new work: a moved `task` command would spawn a turn.
    # Every cursor is moved past the import. A consumer with unread work other
    # than the runner's own heartbeats and reports (which the gh-watch action
    # always trails by a tick or two) means the daemon was mid-flight, and
    # nothing is written.
    db = sqlite3.connect(DST_DATA / "board.db")
    top = db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
    behind = [(c, seq) for c, seq in db.execute("SELECT consumer, seq FROM cursors")
              if db.execute("SELECT COUNT(*) FROM events WHERE seq > ? AND topic != 'runner'",
                            (seq,)).fetchone()[0]]
    if behind:
        raise SystemExit(f"board consumers have unread work ({behind}); start the daemon, "
                         f"let it drain, stop it, and import again")
    have = {row[0] for row in db.execute("SELECT id FROM events")}
    n = 0
    with db:
        for e in r["events"]:
            if e["id"] in have:
                continue
            db.execute("INSERT INTO events (id, ts, namespace, topic, key, kind, provenance, "
                       "payload, correlation_id, visible_after) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       tuple(e[c] for c in ("id", "ts", "namespace", "topic", "key", "kind",
                                            "provenance", "payload", "correlation_id",
                                            "visible_after")))
            n += 1
        if n:
            new_top = db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
            db.execute("UPDATE cursors SET seq = ?", (new_top,))
    out["events"] = n
    return out


def main(argv: list[str]) -> None:
    cmd = argv[1] if len(argv) > 1 else "plan"
    exclude = set(os.environ.get("MOVE_EXCLUDE", "").split())
    include = set(os.environ.get("MOVE_INCLUDE", "").split())
    if cmd == "plan":
        sel = select(exclude, include)
        for tid, cwd in sorted(sel["tasks"].items()):
            open_ = any(m.get("open") for m in sel["sessions"].values() if m.get("task") == tid)
            print(f"{'open  ' if open_ else 'closed'} {cwd:55} {tid}")
        print(f"{len(sel['tasks'])} threads, {len(sel['pages'])} pages")
    elif cmd == "export":
        print(json.dumps(export(Path(argv[2]), exclude, include), indent=1))
    elif cmd == "import":
        print(json.dumps(do_import(Path(argv[2])), indent=1))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
