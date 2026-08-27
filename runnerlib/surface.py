"""Review Surface as the driver channel (cadre-context decision
2026-08-26-surface-as-driver-channel).

The daemon authors deterministic HTML artifacts (no LLM, no tokens) at each
driver touchpoint and opens them as Review Surface sessions. Nothing here
long-polls: our review-surface fork appends a wake signal to an outbox JSONL
on every driver "send", and the daemon's tick consumes new signals with a
short one-shot poll — delivery semantics stay review-surface's own. Surface
feedback comes back as machine tokens embedded in queued prompts:

    CADRE_DECISION gate=risk_hold story=<slug> pr=<n> verdict=approve|reject
    CADRE_ANSWER ticket=<id> :: <free text>

Everything else the driver annotates is recorded to the board as
`surface_feedback` and mirrored to the PR when there is one — the PR stays
the record (dual channel: GitHub remains fully live for teammates; the gate
advances on the first qualifying event from either side).

sessions.json is the durable session list; the outbox cursor is a byte
offset persisted next to it, so a daemon restart re-reads nothing and loses
nothing. All entry points swallow exceptions — the surface must never take
the daemon down."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from html import escape
from pathlib import Path

from . import board_events

CLI = "review-surface"
_DECISION_RE = re.compile(
    r"CADRE_DECISION gate=(\w+) story=([\w.-]+) pr=(\d+) verdict=(approve|reject)")
_ANSWER_RE = re.compile(r"CADRE_ANSWER ticket=([\w-]+) :: (.*)", re.DOTALL)

def outbox_path() -> Path:
    return Path(os.environ.get("REVIEW_SURFACE_OUTBOX",
                               str(Path.home() / ".review-surface/outbox.jsonl")))


def available() -> bool:
    return shutil.which(CLI) is not None


def _dir(cfg) -> Path:
    d = Path(cfg.data_dir) / "surfaces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sessions_path(cfg) -> Path:
    return _dir(cfg) / "sessions.json"


def sessions(cfg) -> dict:
    try:
        return json.loads(_sessions_path(cfg).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sessions(cfg, data: dict) -> None:
    p = _sessions_path(cfg)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".sess-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ------------------------------------------------------------------ templates

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
  body{{margin:0;background:#0f1115;color:#f7f3ea;font:14px/1.5 ui-sans-serif,system-ui,sans-serif;padding:36px 24px}}
  .wrap{{max-width:880px;margin:0 auto}} h1{{font-size:24px;color:#fffbf3;margin:0 0 6px}}
  .meta{{font-family:ui-monospace,monospace;font-size:12px;color:#8c96aa;margin-bottom:20px}}
  .meta a{{color:#f4c95d}} .card{{background:#11141a;border:1px solid #303745;border-radius:12px;padding:16px 18px;margin:12px 0}}
  .chip{{display:inline-block;font-family:ui-monospace,monospace;font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;
        border-radius:5px;padding:2px 8px;margin-bottom:8px;background:rgba(244,201,93,.1);color:#f4c95d;border:1px solid rgba(244,201,93,.4)}}
  pre{{background:#0f1115;border:1px solid #2a2f3a;border-radius:8px;padding:12px;overflow-x:auto;white-space:pre-wrap;color:#d8deea;font-size:12.5px}}
  label{{display:block;padding:8px 10px;border:1px solid #2a2f3a;border-radius:8px;margin:6px 0;cursor:pointer;color:#d8deea;background:#0f1115}}
  label:has(input:checked){{border-color:#f4c95d;color:#fffbf3}}
  input[type=radio]{{accent-color:#f4c95d;margin-right:8px}}
  textarea{{width:100%;min-height:70px;background:#0f1115;color:#f7f3ea;border:1px solid #303745;border-radius:8px;padding:8px;font:inherit}}
  button{{margin-top:10px;font-weight:600;font-size:13px;background:#f4c95d;color:#17130a;border:0;border-radius:8px;padding:9px 16px;cursor:pointer}}
  button:hover{{background:#ffd877}} .dim{{color:#aeb6c6}} .why{{font-size:12px;color:#8c96aa;margin-left:24px;display:block}}
</style></head><body><div class="wrap">{body}</div></body></html>"""


def _write_artifact(cfg, name: str, title: str, body: str) -> Path:
    path = _dir(cfg) / name
    path.write_text(_PAGE.format(title=escape(title), body=body), encoding="utf-8")
    return path


def _risk_rationale(body: str) -> str:
    """The Risk line plus its adjacent rationale — the part of the report that
    actually argues for the hold."""
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*\**risk:\s*\**\s*high\b", line, re.IGNORECASE):
            block = [line.strip()]
            for follow in lines[i + 1:i + 8]:
                if not follow.strip():
                    break
                block.append(follow.strip())
            return "\n".join(block)
    return ""


def author_risk_hold(cfg, slug: str, pr: int, detail: dict, stage: str,
                     files: list | None = None) -> Path:
    """A decision brief, not a body dump: what the change is, what it touches,
    why it held — with the full report collapsed for when it's needed."""
    url = f"https://github.com/{detail.get('base', {}).get('repo', {}).get('full_name') or ''}/pull/{pr}"
    html_url = detail.get("html_url") or url
    token = f"CADRE_DECISION gate=risk_hold story={slug} pr={pr} verdict="
    body_text = detail.get("body") or ""
    summary = next((p.strip() for p in body_text.split("\n\n")
                    if p.strip() and not p.lstrip().startswith("#")
                    and not re.match(r"^\s*\**(risk|confidence):", p.strip(),
                                     re.IGNORECASE)), "")
    rationale = _risk_rationale(body_text)
    file_rows = "".join(
        f"<tr><td style='font-family:ui-monospace,monospace;font-size:12.5px'>{escape(f.get('filename') or '')}</td>"
        f"<td style='color:#8fe39e;text-align:right'>+{f.get('additions', 0)}</td>"
        f"<td style='color:#f06464;text-align:right'>−{f.get('deletions', 0)}</td></tr>"
        for f in (files or [])[:40])
    files_html = (f"<div class='card'><span class='chip'>Touches "
                  f"{len(files)} file(s)</span><table style='width:100%;border-collapse:collapse'>"
                  f"{file_rows}</table></div>") if files else ""
    body = f"""
<h1>Risk HIGH — driver decision</h1>
<div class="meta">{escape(slug)} · {escape(stage)} · <a href="{escape(html_url)}">PR #{pr}</a> — {escape(detail.get('title') or '')}</div>
<div class="card"><span class="chip">The change</span>
<p>{escape(summary[:800]) or '<span class="dim">(no summary in the PR body)</span>'}</p></div>
{files_html}
<div class="card"><span class="chip">Why it held</span>
{f'<pre>{escape(rationale[:3000])}</pre>' if rationale else ''}
<p class="dim">A fresh reviewer assigned <b>Risk: high</b> to this finished change — low/medium
auto-merge; high is a decision, not a PR to read. Annotate anything here, then give the
verdict. Approving merges the PR (a GitHub merge works too; first channel wins).</p></div>
<div class="card"><details><summary class="dim" style="cursor:pointer">Full reviewer report (PR body)</summary>
<pre>{escape(body_text[:20000] or '(empty body)')}</pre></details></div>
<div class="card"><span class="chip">Verdict</span>
<form data-review-surface-question="verdict" onsubmit="event.preventDefault();
  const v=new FormData(event.currentTarget).get('verdict'); if(!v) return;
  window.reviewSurface.queuePrompt({json.dumps(token)}+v,
    {{tag:'choice', text:'Risk-hold verdict: '+v, element:event.currentTarget,
     data:{{gate:'risk_hold', story:{json.dumps(slug)}, pr:{pr}, verdict:v}}}});">
<label><input type="radio" name="verdict" value="approve"><b>Approve — merge it</b>
<span class="why">The risk is acceptable; the change merges as-is.</span></label>
<label><input type="radio" name="verdict" value="reject"><b>Reject — send back</b>
<span class="why">Holds the PR with a `hold` label; annotate above to say what must change.</span></label>
<button type="submit">Queue verdict</button></form></div>"""
    return _write_artifact(cfg, f"hold-{slug}-pr{pr}.html",
                           f"Risk hold — {slug} PR #{pr}", body)


def author_question(cfg, msg: dict) -> Path:
    ticket = msg["ticket"]
    rec = msg.get("recommendation") or ""
    body = f"""
<h1>The pipeline has a question</h1>
<div class="meta">{escape(msg.get('story') or '')} · {escape(msg.get('stage') or '')}
{('· PR #' + str(msg['pr'])) if msg.get('pr') else ''} · ticket {escape(ticket)}</div>
<div class="card"><span class="chip">Question</span>
<p>{escape(msg.get('question') or '')}</p>
{f'<p class="dim"><b>Agent recommendation:</b> {escape(rec)}</p>' if rec else ''}</div>
<div class="card"><span class="chip">Your answer</span>
<form data-review-surface-question="answer" onsubmit="event.preventDefault();
  const t=new FormData(event.currentTarget).get('text'); if(!t) return;
  window.reviewSurface.queuePrompt('CADRE_ANSWER ticket={ticket} :: '+t,
    {{tag:'choice', text:'Answer: '+t.slice(0,120), element:event.currentTarget,
     data:{{ticket:{json.dumps(ticket)}}}}});">
<textarea name="text" placeholder="Answer in your own words — it reaches the run verbatim."></textarea>
<button type="submit">Queue answer</button></form></div>"""
    return _write_artifact(cfg, f"ask-{ticket}.html", f"Question — {ticket}", body)


def author_notice(cfg, story: str, title: str, message_html: str) -> Path:
    """Agent-outbound message as a surface artifact (replaces the message UI).
    No controls — annotations come back as surface_feedback."""
    body = f"""<h1>{escape(title)}</h1>
<div class="meta">{escape(story)} · pipeline report</div>
<div class="card">{message_html}</div>
<p class="dim">Annotate anything here — feedback flows back to the pipeline's board.</p>"""
    return _write_artifact(cfg, f"notice-{story}-{int(time.time())}.html", title, body)


# ------------------------------------------------------------------ lifecycle

def _run_cli(args: list[str], timeout: int = 25) -> str:
    r = subprocess.run([CLI, *args], capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def open_session(cfg, path: Path, kind: str, log, **meta) -> None:
    """Open (or resume) the artifact and record the session. Never raises."""
    try:
        out = _run_cli([str(path)])
        m = re.search(r'url: "([^"]+)"', out)
        url_path = ""
        if m:
            url_path = re.sub(r"^https?://[^/]+", "", m.group(1))
        key = url_path.rsplit("/", 1)[-1] if url_path else ""
        s = sessions(cfg)
        s[str(path)] = {"kind": kind, "path": url_path, "key": key, "open": True,
                        "opened": time.time(), **meta}
        _save_sessions(cfg, s)
        board_events.emit("surface_opened", session_kind=kind, artifact=str(path),
                          session=url_path, **{k: v for k, v in meta.items()
                                               if isinstance(v, (str, int, float))})
        log(f"surface: opened {kind} session {url_path or path.name}")
    except Exception as e:
        log(f"surface: open failed for {path.name}: {e}")


def end_session(cfg, path: str, log) -> None:
    try:
        _run_cli(["end", path])
    except Exception:
        pass
    s = sessions(cfg)
    if path in s:
        s[path]["open"] = False
        _save_sessions(cfg, s)
    log(f"surface: ended session for {Path(path).name}")


def _cursor_path(cfg) -> Path:
    return _dir(cfg) / "outbox.cursor"


def _read_outbox(cfg) -> list[dict]:
    """New outbox signals since the persisted byte offset."""
    ob = outbox_path()
    try:
        size = ob.stat().st_size
    except OSError:
        return []
    try:
        pos = int(_cursor_path(cfg).read_text().strip() or "0")
    except (OSError, ValueError):
        pos = 0
    if pos > size:  # outbox truncated/rotated
        pos = 0
    if pos == size:
        return []
    with open(ob, "rb") as f:
        f.seek(pos)
        chunk = f.read().decode(errors="replace")
    _cursor_path(cfg).write_text(str(size))
    out = []
    for line in chunk.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _classify_prompts(texts: list[str]) -> tuple[list[dict], list[str]]:
    """(decisions/answers, free-text prompts). Tokens are authoritative; every
    non-token prompt string is driver feedback worth recording."""
    structured, free = [], []
    for text in texts:
        d = _DECISION_RE.search(text)
        if d:
            structured.append({"type": "decision", "gate": d.group(1),
                               "story": d.group(2), "pr": int(d.group(3)),
                               "verdict": d.group(4)})
            continue
        a = _ANSWER_RE.search(text)
        if a:
            structured.append({"type": "answer", "ticket": a.group(1),
                               "text": a.group(2).strip()})
            continue
        if text.strip():
            free.append(text.strip())
    return structured, free


def _parse_feedback(raw: str) -> tuple[dict | None, list[dict], list[str]]:
    """Parse `poll --json` output: (payload, structured, free). payload is
    None when the line isn't JSON (old CLI on PATH, or an error banner)."""
    line = next((l for l in raw.splitlines() if l.startswith("{")), "")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None, [], []
    prompts = payload.get("prompts") or []
    texts = [str(p.get("prompt") or "") for p in prompts
             if isinstance(p, dict)]
    structured, free = _classify_prompts(texts)
    return payload, structured, free


def tick(cfg, reg, ghc, log) -> None:
    """One daemon pass: consume new outbox signals (short one-shot poll per
    signalled session) -> open sessions for new pending questions."""
    if not available():
        return
    try:
        _tick(cfg, reg, ghc, log)
    except Exception as e:
        log(f"surface: tick error: {e}")


def _consume(cfg, reg, ghc, log, path: str, meta: dict) -> None:
    """One short poll on a session the outbox says has feedback waiting."""
    try:
        raw = _run_cli(["poll", path, "--timeout-ms", "4000", "--json"], timeout=30)
    except Exception as e:
        log(f"surface: consume poll failed for {Path(path).name}: {e}")
        return
    payload, structured, free = _parse_feedback(raw)
    status = (payload or {}).get("status") or ""
    had_prompts = bool((payload or {}).get("prompts"))
    # "Send & End" delivers the final feedback once with an ended status —
    # process prompts whenever present, close whenever the session is over.
    if status == "ended" or re.search(
            r"ended the session|No active Review Surface session", raw, re.IGNORECASE):
        s2 = sessions(cfg)
        if path in s2 and s2[path].get("open"):
            s2[path]["open"] = False
            _save_sessions(cfg, s2)
            board_events.emit("surface_closed", artifact=path, by="driver")
            log(f"surface: session ended from browser for {Path(path).name}")
    if not had_prompts:
        return
    if not structured and not free:
        # Poll delivery consumes — a parse miss here would silently LOSE the
        # driver's feedback. Keep the raw capture and shout about it.
        keep = _dir(cfg) / f"unparsed-{int(time.time())}.txt"
        keep.write_text(raw)
        board_events.emit("surface_unparsed", artifact=path, raw_file=str(keep))
        log(f"surface: feedback arrived but parsed to NOTHING — raw kept at {keep}")
        return
    for item in structured:
        _apply(cfg, reg, ghc, log, path, meta, item)
    for text in free:
        board_events.emit("surface_feedback", artifact=path,
                          story=meta.get("story"), pr=meta.get("pr"),
                          text=text[:2000])
        if meta.get("pr") and meta.get("repo"):
            try:
                from .dispatcher import AGENT_MARKER
                ghc.comment(meta["repo"], int(meta["pr"]),
                            f"**Driver (via surface):** {text[:1500]} {AGENT_MARKER}")
            except Exception as e:
                log(f"surface: PR mirror failed: {e}")
        log(f"surface: feedback on {Path(path).name}: {text[:120]}")


def _ensure_server(cfg, sess: dict, log) -> None:
    """The surface server self-stops when idle and dies on reboots; any open
    session must be reachable, so re-open the first one to relaunch it."""
    open_paths = [p for p, m in sess.items() if m.get("open") and Path(p).exists()]
    if not open_paths:
        return
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:4387/health", timeout=3)
        return
    except OSError:
        pass
    try:
        _run_cli([open_paths[0]])
        log("surface: server was down — relaunched")
    except Exception as e:
        log(f"surface: server relaunch failed: {e}")


def _tick(cfg, reg, ghc, log) -> None:
    from . import messages
    sess = sessions(cfg)
    _ensure_server(cfg, sess, log)

    # 1) outbox signals -> consume feedback from exactly those sessions
    signalled = {sig.get("key") for sig in _read_outbox(cfg) if sig.get("key")}
    if signalled:
        by_key = {(m.get("key") or (m.get("path") or "").rsplit("/", 1)[-1]): (p, m)
                  for p, m in sess.items() if m.get("open")}
        for key in signalled:
            hit = by_key.get(key)
            if hit:
                _consume(cfg, reg, ghc, log, hit[0], hit[1])
        sess = sessions(cfg)  # consuming may close sessions

    # 2) new pending questions get artifacts
    open_tickets = {m.get("ticket") for m in sess.values() if m.get("kind") == "ask"}
    for msg in messages.pending(cfg.data_dir):
        if msg["ticket"] in open_tickets:
            continue
        path = author_question(cfg, msg)
        open_session(cfg, path, "ask", log, ticket=msg["ticket"],
                     story=msg.get("story"), pr=msg.get("pr"))


def _apply(cfg, reg, ghc, log, path: str, meta: dict, item: dict) -> None:
    from . import messages
    from .dispatcher import AGENT_MARKER
    if item["type"] == "answer":
        m = messages.answer(cfg.data_dir, item["ticket"], item["text"])
        board_events.emit("surface_answer", ticket=item["ticket"],
                          story=(m or {}).get("story"), text=item["text"][:2000])
        if m and m.get("pr") and meta.get("repo"):
            try:  # the PR is still the record
                ghc.comment(meta["repo"], int(m["pr"]),
                            f"**Driver answer (via surface, {item['ticket']}):** "
                            f"{item['text'][:1500]} {AGENT_MARKER}")
            except Exception:
                pass
        log(f"surface: answer recorded for {item['ticket']}")
        end_session(cfg, path, log)
        return
    if item["type"] == "decision" and item["gate"] == "risk_hold":
        slug, pr = item["story"], item["pr"]
        story = reg.stories().get(slug) or {}
        repo = story.get("repo") or meta.get("repo")
        if not repo:
            log(f"surface: no repo known for {slug} — decision dropped")
            return
        board_events.emit("surface_approval", gate="risk_hold", story=slug,
                          pr=pr, verdict=item["verdict"])
        try:
            if item["verdict"] == "approve":
                ghc.merge_pr(repo, pr)
                ghc.comment(repo, pr, f"Approved via surface — merged. {AGENT_MARKER}")
                log(f"surface: {slug} PR #{pr} approved via surface — merged")
            else:
                ghc.post(f"/repos/{repo}/issues/{pr}/labels", {"labels": ["hold"]})
                ghc.comment(repo, pr,
                            f"Rejected via surface — held. See surface annotations "
                            f"for what must change. {AGENT_MARKER}")
                log(f"surface: {slug} PR #{pr} rejected via surface — held")
        except Exception as e:
            log(f"surface: applying {item['verdict']} on {repo}#{pr} failed: {e}")
        end_session(cfg, path, log)


def reconcile_merged_holds(cfg, reg, log) -> None:
    """Dual channel, other direction: a hold PR merged on GitHub while its
    surface session sat open -> close the session (first event won there)."""
    sess = sessions(cfg)
    for path, meta in list(sess.items()):
        if not meta.get("open") or meta.get("kind") != "risk_hold":
            continue
        slug = meta.get("story")
        story = reg.stories().get(slug) if slug else None
        if not story:
            continue
        p = (story.get("prs_cache") or {}).get(str(meta.get("pr")))
        if p and p.get("state") != "open":
            board_events.emit("surface_superseded", story=slug, pr=meta.get("pr"),
                              by="github")
            end_session(cfg, path, log)


def status_list(cfg) -> list[dict]:
    """Open sessions for the status page — served through the statusd proxy,
    so the stored path fragment is directly linkable."""
    out = []
    for path, meta in sessions(cfg).items():
        if meta.get("open"):
            out.append({"kind": meta.get("kind"), "story": meta.get("story"),
                        "pr": meta.get("pr"), "ticket": meta.get("ticket"),
                        "path": meta.get("path"), "opened": meta.get("opened")})
    return sorted(out, key=lambda s: s.get("opened") or 0, reverse=True)
