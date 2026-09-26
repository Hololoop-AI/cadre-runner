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

Task pages (workflow #3, `config/actions-dialogue.json`) are the exception,
and `_task_bridge` is the whole of it: they have no PR, so their feedback goes
to the BLACKBOARD as that workflow's own commands and nowhere near GitHub.
The scope is one session kind — a session opened as `kind="task"` — so every
pipeline surface keeps the flow above untouched.

A batch the daemon consumes that NOTHING routes — no task id, no PR to
mirror to — is STRANDED: `_dead_letter` writes it verbatim under
surfaces/stranded/ and the fleet badges the row. A tripwire, not a router.

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

from . import board_events, dispatcher, gh

CLI = "review-surface"
_DECISION_RE = re.compile(
    r"CADRE_DECISION gate=(\w+) story=([\w.-]+) pr=(\d+) verdict=(approve|reject)")
# Workflow #3's verdict (prompts/task.md). Its own pattern because the pipeline
# one is PR-shaped to the bone — `pr=<n>` is required and the verdicts are
# approve|reject — and a dialogue has no PR and a `continue` instead of a
# reject. Matched FIRST, so a task token can never be read as a pipeline one.
#
# `route:<node>` is the third kind of verdict: the driver picked the node
# that takes the work next (runnerlib/tasks.py "the node event"). Same form,
# same token — only the radio value differs.
_TASK_DECISION_RE = re.compile(
    r"CADRE_DECISION gate=task story=([\w.-]+) task=([\w.-]+) "
    # `route:` is the name the hand-off verdict had before it was renamed for
    # what it does. Every page written before the rename is still open with
    # `route:<node>` in its form, so dropping it here would make the hand-off
    # line on all of them parse as nothing and fall through to a plain
    # feedback turn — the driver's chosen specialist silently ignored.
    r"verdict=(approve|continue|(?:handoff|route):[\w.-]+)")
_ANSWER_RE = re.compile(r"CADRE_ANSWER ticket=([\w-]+) :: (.*)", re.DOTALL)

def outbox_path() -> Path:
    return Path(os.environ.get("REVIEW_SURFACE_OUTBOX",
                               str(Path.home() / ".review-surface/outbox.jsonl")))


DEFAULT_UPSTREAM = "http://127.0.0.1:4387"


def upstream() -> str:
    """Where the review-surface server lives. One reader of one env var, shared
    with statusd — the health check and the proxy pointing at different hosts is
    a failure nobody can see from either side."""
    return os.environ.get("CADRE_SURFACE_UPSTREAM") or DEFAULT_UPSTREAM


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
    url = gh.web_url(detail.get('base', {}).get('repo', {}).get('full_name') or '', pr)
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
  window.reviewSurface.queuePrompt('{token}'+v,
    {{tag:'choice', text:'Risk-hold verdict: '+v, element:event.currentTarget,
     data:{{gate:'risk_hold', story:'{slug}', pr:{pr}, verdict:v}}}});">
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
     data:{{ticket:'{ticket}'}}}});">
<textarea name="text" placeholder="Answer in your own words — it reaches the run verbatim."></textarea>
<button type="submit">Queue answer</button></form></div>"""
    return _write_artifact(cfg, f"ask-{ticket}.html", f"Question — {ticket}", body)


def author_spec_review(cfg, slug: str, pr: int, detail: dict,
                       spec_files: list[dict]) -> Path:
    """The unified spec gate (cadre-context 2026-08-26-unified-spec): one
    artifact carrying the plan narrative and every spec document on the
    planning branch, each annotatable in place, with the approval verdict.
    Approve merges the planning PR; revise feeds annotations to the ordinary
    revise machinery. spec_files: [{path, text}]."""
    html_url = detail.get("html_url") or ""
    token = f"CADRE_DECISION gate=spec_review story={slug} pr={pr} verdict="
    docs = "".join(
        f"""<div class="card"><span class="chip">{escape(f['path'])}</span>
<pre>{escape(f['text'][:30000])}</pre></div>"""
        for f in spec_files)
    body = f"""
<h1>Spec review — the one human gate</h1>
<div class="meta">{escape(slug)} · <a href="{escape(html_url)}">planning PR #{pr}</a> — {escape(detail.get('title') or '')}</div>
<div class="card"><span class="chip">Plan narrative (PR description)</span>
<pre>{escape((detail.get('body') or '(empty)')[:20000])}</pre></div>
{docs}
<div class="card"><span class="chip">Verdict</span>
<p class="dim">Annotate any slice, decision, or contract above first — annotations reach the
agents as your PR comments either way. On approval the spec locks and everything
after is automated; a GitHub merge of the planning PR does the same thing (first
channel wins).</p>
<form data-review-surface-question="verdict" onsubmit="event.preventDefault();
  const v=new FormData(event.currentTarget).get('verdict'); if(!v) return;
  window.reviewSurface.queuePrompt('{token}'+v,
    {{tag:'choice', text:'Spec verdict: '+v, element:event.currentTarget,
     data:{{gate:'spec_review', story:'{slug}', pr:{pr}, verdict:v}}}});">
<label><input type="radio" name="verdict" value="approve"><b>Approve — lock the spec and run</b>
<span class="why">Merges the planning PR; slices dispatch from the next daemon pass.</span></label>
<label><input type="radio" name="verdict" value="revise"><b>Revise — send my annotations back</b>
<span class="why">Your annotations become driver comments on the PR; a revise round picks them up.</span></label>
<button type="submit">Queue verdict</button></form></div>"""
    return _write_artifact(cfg, f"spec-{slug}-pr{pr}.html",
                           f"Spec review — {slug}", body)


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


def _create_session_quietly(path: Path) -> str:
    """POST the artifact straight to the running server and return the session
    URL path. No browser tab: the daemon works in the background, and the
    driver arrives from the fleet page when they choose — a tab stealing focus
    for every finished round was the complaint that made this the default.
    Raises when the server isn't up; the caller falls back to the CLI, which
    spawns the server (and does open a tab — the cold-start case only)."""
    import urllib.request
    req = urllib.request.Request(
        upstream() + "/api/sessions", method="POST",
        data=json.dumps({"file": str(path)}).encode(),
        headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        url = json.loads(r.read()).get("url") or ""
    return re.sub(r"^https?://[^/]+", "", url)


def open_session(cfg, path: Path, kind: str, log, **meta) -> None:
    """Open (or resume) the artifact and record the session. Never raises."""
    try:
        try:
            url_path = _create_session_quietly(path)
        except Exception:
            out = _run_cli([str(path)])
            m = re.search(r'url: "([^"]+)"', out)
            url_path = re.sub(r"^https?://[^/]+", "", m.group(1)) if m else ""
        key = url_path.rsplit("/", 1)[-1] if url_path else ""
        s = sessions(cfg)
        # Where a page BELONGS outlives any one turn that opens it. Re-opening
        # used to replace the record wholesale, so an adopted page dropped out
        # of its project the moment its new dialogue wrote a round and showed
        # up as a bare task id under "loose" — the driver lost the thread.
        prior = s.get(str(path)) or {}
        keep = {k: prior[k] for k in ("project", "title", "role")
                if prior.get(k) and k not in meta}
        s[str(path)] = {"kind": kind, "path": url_path, "key": key, "open": True,
                        "opened": time.time(), **keep, **meta}
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


def agent_reply(cfg, reg, ghc, log, path: Path, message: str) -> bool:
    """Answer the driver where they asked (audit 2026-09-15: feedback was
    one-way — annotations went out to GitHub and nothing ever came back on the
    surface). Posts into the session's conversation panel; no-ops when the CLI
    is absent or the session is closed, and never raises.

    --agent-reply rides a poll, and poll delivery CONSUMES: anything the driver
    had queued comes back in this call's response and is handled here rather
    than dropped. --timeout-ms 0 keeps the daemon tick from blocking on the
    long poll this command would otherwise become."""
    if not available():
        return False
    key = str(path)
    meta = sessions(cfg).get(key) or {}
    if not meta.get("open"):
        return False
    text = (message or "").strip()
    if not text:
        return False
    try:
        raw = _run_cli(["poll", key, "--agent-reply", text[:1000],
                        "--timeout-ms", "0", "--json"], timeout=30)
    except Exception as e:
        log(f"surface: agent reply failed for {path.name}: {e}")
        return False
    try:
        _handle_poll(cfg, reg, ghc, log, key, meta, raw)
    except Exception as e:
        log(f"surface: agent reply poll response unhandled for {path.name}: {e}")
    log(f"surface: replied into {path.name}: {text[:120]}")
    return True


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


ANCHOR_LIMIT = 200


def _quote(anchor: str) -> str:
    """The anchored text as a markdown blockquote above the driver's words. It
    gets its own budget — an anchor must never eat into the driver's text."""
    if not anchor:
        return ""
    text = anchor[:ANCHOR_LIMIT] + ("…" if len(anchor) > ANCHOR_LIMIT else "")
    return "> " + text.replace("\n", "\n> ") + "\n\n"


def _anchor(p: dict) -> dict:
    """What the driver attached their words TO. The payload's own `text` is the
    selected/annotated element's text — without it the agent receives a floating
    sentence and has to guess the target (audit 2026-09-15)."""
    return {"uid": str(p.get("uid") or ""),
            "selector": str(p.get("selector") or ""),
            "tag": str(p.get("tag") or ""),
            "anchor": str(p.get("text") or "").strip()}


def _classify_prompts(prompts: list[dict]) -> tuple[list[dict], list[dict]]:
    """(decisions/answers, free-text feedback). Tokens are authoritative; every
    non-token prompt is driver feedback worth recording. Both kinds carry the
    annotation anchor they arrived with."""
    structured, free = [], []
    for p in prompts:
        if not isinstance(p, dict):
            p = {"prompt": str(p)}
        text = str(p.get("prompt") or "")
        anchor = _anchor(p)
        t = _TASK_DECISION_RE.search(text)
        if t:
            # Whatever the driver typed AROUND the token is theirs and is kept:
            # on a `continue` verdict it is the instruction for the next turn,
            # and dropping it would lose the one thing they said.
            structured.append({"type": "task_decision", "gate": "task",
                               "story": t.group(1), "task": t.group(2),
                               "verdict": t.group(3),
                               "text": (text[:t.start()] + text[t.end():]).strip(),
                               **anchor})
            continue
        d = _DECISION_RE.search(text)
        if d:
            structured.append({"type": "decision", "gate": d.group(1),
                               "story": d.group(2), "pr": int(d.group(3)),
                               "verdict": d.group(4), **anchor})
            continue
        a = _ANSWER_RE.search(text)
        if a:
            structured.append({"type": "answer", "ticket": a.group(1),
                               "text": a.group(2).strip(), **anchor})
            continue
        if text.strip():
            free.append({"text": text.strip(), **anchor})
    return structured, free


def _parse_feedback(raw: str) -> tuple[dict | None, list[dict], list[dict]]:
    """Parse `poll --json` output: (payload, structured, free). payload is
    None when the line isn't JSON (old CLI on PATH, or an error banner)."""
    line = next((l for l in raw.splitlines() if l.startswith("{")), "")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None, [], []
    structured, free = _classify_prompts(payload.get("prompts") or [])
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
    _handle_poll(cfg, reg, ghc, log, path, meta, raw)


def _handle_poll(cfg, reg, ghc, log, path: str, meta: dict, raw: str) -> None:
    """Everything a poll response means: session-ended bookkeeping, structured
    decisions, and the driver's free-text annotations. Shared by the feedback
    consume pass and by agent replies, which poll the same session and would
    otherwise CONSUME queued feedback into the void."""
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
    reason = _strand_reason(meta, free)
    if reason:
        # The consume already happened — poll delivery is destructive — so the
        # driver's words exist only in this response now. Keep them verbatim
        # and make the fleet say so; the board events below still fire.
        _dead_letter(cfg, log, path, meta, raw, payload, reason)
    if meta.get("kind") == "task":
        # Workflow #3 owns this page. Its feedback goes to the blackboard as
        # the dialogue's own commands and NOWHERE else — no PR mirror, no
        # merge, no `gh` at all, which is the claim that makes this workflow
        # the engine's agnosticism proof.
        _task_bridge(cfg, reg, log, path, meta, structured, free,
                     raw=raw, payload=payload)
        return
    for item in structured:
        _apply(cfg, reg, ghc, log, path, meta, item)
    for item in free:
        text, anchor = item["text"], item.get("anchor") or ""
        board_events.emit("surface_feedback", artifact=path,
                          story=meta.get("story"), pr=meta.get("pr"),
                          text=text[:2000], anchor=anchor[:ANCHOR_LIMIT],
                          selector=item.get("selector") or "",
                          tag=item.get("tag") or "", uid=item.get("uid") or "")
        if meta.get("pr") and meta.get("repo"):
            try:
                # Deliberately NO agent marker: these are the driver's words.
                # They must count as human activity — blocking automerge and
                # triggering revise rounds — exactly like a typed PR comment.
                ghc.comment(meta["repo"], int(meta["pr"]),
                            f"{dispatcher.DRIVER_PREFIX}\n\n"
                            f"{_quote(anchor)}{text[:1500]}")
            except Exception as e:
                log(f"surface: PR mirror failed: {e}")
        log(f"surface: feedback on {Path(path).name}: {text[:120]}")


def _strand_reason(meta: dict, free: list[dict]) -> str:
    """Why this batch reached no reader, or "" when something owns it.

    A task page routes by its task id; every other page routes its free text
    only by mirroring to a PR. Without either, the driver's annotations become
    board events nothing reads — the black-hole page. Decision and answer
    tokens carry their own routing (story+pr, ticket), so a batch of only
    those is never stranded."""
    if meta.get("kind") == "task":
        return "" if meta.get("task") else "task page with no task id"
    if not any((f.get("text") or "").strip() for f in free):
        return ""
    if meta.get("pr") and meta.get("repo"):
        return ""
    return f"{meta.get('kind') or 'unknown'} session with no task id and no PR to mirror to"


def stranded_dir(cfg) -> Path:
    return _dir(cfg) / "stranded"


def agent_status(key: str, timeout: float = 8) -> dict | None:
    """Read-only session state from the surface server. Never consumes — which
    is the whole reason this endpoint exists, and why the daemon may ask it
    about sessions it does not own."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"{upstream()}/api/{key}/agent-status", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# What every dialogue that takes over a page is told to do with it — shared by
# adoption and on-demand attach so the two owners work the page the same way.
_OWN_PAGE_STEPS = """1. Read the page at {path} in full. It is the whole context you have — treat
   it as a document written for a reader with no session history, because
   that is what you are."""

_REWRITE_IN_PLACE = """3. Rewrite that same page in place at that exact path — it is the driver's
   bookmark and it must stay one page, not a new one. Follow the authoring
   contract (invoke the auto-surface skill): what changed this round at the
   top, the settled answers pinned in a green decided block, and the verdict
   form last.

From here this is an ordinary dialogue: their next annotation on that page
reaches you as the next turn of this session."""

ADOPT_ASK = """You now own the review surface page at {path}

The driver answered on that page and nobody was listening: it was registered
as a presence-only session with no agent behind it, so their words were
queued with no loop to deliver them to. You are that loop from now on.

Their queued feedback, verbatim:

{feedback}

Do this:

""" + _OWN_PAGE_STEPS + """
2. Act on what they said. If they answered questions, those answers are
   DECIDED: record them as settled, with the date, and do not reargue them.
   If they asked for something, do it. If a point is wrong, push back with
   your reasoning rather than complying silently.
""" + _REWRITE_IN_PLACE

ATTACH_ASK = """You now own the review surface page at {path}

The driver asked for an agent on this page from the fleet's manage view. It
had no agent behind it — it was registered as a presence-only session — so
nothing they wrote there had a loop to reach. You are that loop from now on.

Their instruction for you:

{instruction}

Do this:

""" + _OWN_PAGE_STEPS + """
2. Carry out the instruction. Anything the page already records as DECIDED
   stays decided: do not reargue it. If a point is wrong, push back with your
   reasoning rather than complying silently.
""" + _REWRITE_IN_PLACE

ATTACH_DEFAULT = ("None given. Read this page and continue it: pick up where it "
                  "left off, do the next thing it is waiting on, and say on the "
                  "page what you did.")


def unowned(meta: dict) -> bool:
    """No loop answers this page's feedback. Every other open kind is polled by
    the daemon (the pipeline's kinds) or belongs to a dialogue (`task`); a
    presence-only `external` row with no task behind it is the one hole.
    Adoption, attach and the fleet badge all ask this — one definition."""
    return meta.get("kind") == "external" and not meta.get("task")


def _own_page(cfg, reg, path: str, ask: str, title: str, **stamp) -> str:
    """Dispatch a dialogue that owns `path` from now on: submit the task, point
    its record at the page (so it rewrites that page rather than opening a
    second one — the driver's bookmark IS the conversation), and flip the
    session to kind=task so the daemon delivers its feedback. Raises if the
    dispatch fails; nothing is flipped in that case."""
    from . import tasks as tasks_mod
    s = sessions(cfg)
    cwd = str((s.get(path) or {}).get("cwd") or "")
    cwd = cwd if cwd.startswith("/") else str(Path(path).parent)
    task_id = tasks_mod.submit_task(cfg, ask, cwd=cwd, title=title)
    rec = tasks_mod.record(reg, task_id)
    rec["page"] = str(path)
    rec["cwd"] = cwd
    if hasattr(reg, "save"):
        reg.save()
    s = sessions(cfg)
    if path in s:
        s[path].update(kind="task", task=task_id, **stamp)
        s[path].pop("attach_error", None)
        _save_sessions(cfg, s)
    return task_id


def _adopt_unowned(cfg, reg, log, sess: dict) -> None:
    """Feedback queued on a page nobody owns: give it an owner, automatically.

    `kind="external"` sessions are presence-only — the daemon never polls them,
    because polling CONSUMES and would steal feedback from whichever loop is
    waiting on it. The hole that leaves: a page registered without a dialogue
    behind it has no such loop, so the driver's answers sit in the queue
    forever while the fleet says "no agent listening". Three discussion pages
    lost answers that way, each rescued by hand from the journal.

    Hand-rescue is not a lifecycle layer. The read-only status endpoint can
    tell us the exact stranded state without consuming anything, so the daemon
    recognises it, consumes the batch once, and dispatches a dialogue that owns
    the page from then on — the driver's next annotation is an ordinary
    feedback turn. Nothing here needs a human to notice first.
    """
    from . import tasks as tasks_mod
    for path, meta in list(sess.items()):
        if not meta.get("open") or not unowned(meta):
            continue
        if not str(path).startswith("/") or not Path(path).exists():
            continue                    # a page-less presence row owns nothing
        key = str(meta.get("key") or "")
        st = agent_status(key) if key else None
        if not st or st.get("status") == "ended":
            continue
        if (not int(st.get("pending_prompts") or 0)
                or st.get("presence") != "waiting"):
            continue                    # "queued — no agent listening", exactly
        try:
            raw = _run_cli(["poll", path, "--timeout-ms", "4000", "--json"],
                           timeout=30)
        except Exception as e:
            log(f"surface: adoption poll failed for {Path(path).name}: {e}")
            continue
        payload, structured, free = _parse_feedback(raw)
        notes = [{"anchor": i.get("anchor") or "",
                  "text": (i.get("text") or "").strip()}
                 for i in [*structured, *free]]
        notes = [n for n in notes if n["text"]]
        if not notes:
            # The batch is consumed and gone; keep it where a human can read it
            # rather than let a parse miss become the silent loss all over.
            _dead_letter(cfg, log, path, meta, raw, payload,
                         "adoption found no readable feedback")
            continue
        try:
            task_id = _own_page(
                cfg, reg, path,
                ADOPT_ASK.format(path=path,
                                 feedback=tasks_mod.format_feedback(notes)),
                title=f"adopt {Path(path).stem}", adopted=time.time())
        except Exception as e:
            _dead_letter(cfg, log, path, meta, raw, payload,
                         f"adoption dispatch failed: {e}")
            continue
        board_events.emit("surface_adopted", artifact=path, task=task_id,
                          prompts=len(notes),
                          project=meta.get("project") or "",
                          title=meta.get("title") or "")
        log(f"surface: ADOPTED {Path(path).name} — {len(notes)} queued note(s) "
            f"had no listener; dialogue {task_id} owns it now")


# ------------------------------------------------------- attach on demand
#
# The driver's half of adoption: "start an agent on this page now", for a page
# with no owner and nothing queued (which adoption, rightly, never touches).
# The page server is a separate process and the task registry is the daemon's
# in-memory state — a write from outside is erased by the daemon's next save —
# so the page server only files a request here and the daemon's tick does the
# ownership wiring, through the same `_own_page` adoption uses.

def _attach_dir(cfg) -> Path:
    return _dir(cfg) / "attach"


def _attach_file(cfg, path: str) -> Path:
    import hashlib
    return _attach_dir(cfg) / (hashlib.sha1(str(path).encode()).hexdigest()[:16] + ".json")


def ownership(cfg, path: str) -> dict:
    """Who answers this page, from the runner's session store (authoritative,
    unlike the status snapshot, which lags a tick): state is `task` (a
    dialogue owns it), `pipeline` (a runner-polled kind), `pending` (an attach
    is queued), `none`, or `unknown` (not a runner session with a page)."""
    meta = sessions(cfg).get(str(path)) if str(path).startswith("/") else None
    if not meta:
        return {"state": "unknown"}
    if not meta.get("open"):
        return {"state": "unknown", "why": "its session has ended"}
    if meta.get("task"):
        return {"state": "task", "task": meta["task"]}
    if not unowned(meta):
        return {"state": "pipeline", "kind": meta.get("kind") or ""}
    if _attach_file(cfg, path).exists():
        return {"state": "pending"}
    st = agent_status(str(meta["key"]), timeout=1) if meta.get("key") else None
    if (st or {}).get("presence") == "working":
        # registered presence-only BY a loop that is on it right now (an
        # orchestrator polling its own page): attaching would be a second owner
        return {"state": "outside"}
    return {"state": "none", "error": meta.get("attach_error") or ""}


def request_attach(cfg, path: str, instruction: str = "") -> str:
    """File an attach request for the daemon; returns what happened in words
    for the driver. Raises ValueError when the page already has an owner (or
    cannot have one) — the control says so rather than spawn a second."""
    own = ownership(cfg, path)
    if own["state"] == "task":
        raise ValueError(f"already owned by dialogue {own['task']}")
    if own["state"] == "pipeline":
        raise ValueError(f"already owned by the pipeline ({own['kind']} session)")
    if own["state"] == "pending":
        raise ValueError("an attach is already queued for this page")
    if own["state"] == "outside":
        raise ValueError("an agent outside the runner is working on this page")
    if own["state"] == "unknown":
        raise ValueError(own.get("why") or "not a runner session with a page")
    if not Path(path).exists():
        raise ValueError(f"the page file is gone: {path}")
    f = _attach_file(cfg, path)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"artifact": str(path),
                               "instruction": (instruction or "").strip(),
                               "requested": time.time()}))
    os.replace(tmp, f)
    return "Attach queued: the runner dispatches a dialogue for this page on its next tick."


def _attach_requested(cfg, reg, log, sess: dict) -> None:
    """Serve the manage view's attach requests. Ownership is re-checked here,
    at the moment of wiring: adoption runs first in the same tick, so a page
    that gained an owner since the request was filed is left alone."""
    d = _attach_dir(cfg)
    if not d.is_dir():
        return
    for f in sorted(d.glob("*.json")):
        try:
            req = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            f.unlink(missing_ok=True)
            continue
        path = str(req.get("artifact") or "")
        meta = sess.get(path) or {}
        f.unlink(missing_ok=True)       # one attempt per request, never a loop
        if ownership(cfg, path)["state"] != "none" or not Path(path).exists():
            board_events.emit("surface_attach_skipped", artifact=path,
                              task=meta.get("task") or "")
            log(f"surface: attach skipped for {Path(path).name} — it has an "
                f"owner or no open page")
            continue
        try:
            task_id = _own_page(
                cfg, reg, path,
                ATTACH_ASK.format(path=path, instruction=(
                    req.get("instruction") or ATTACH_DEFAULT)),
                title=f"attach {Path(path).stem}", attached=time.time())
        except Exception as e:
            s = sessions(cfg)
            if path in s:
                s[path]["attach_error"] = str(e)[:300]
                _save_sessions(cfg, s)
            log(f"surface: attach dispatch failed for {Path(path).name}: {e}")
            continue
        board_events.emit("surface_attached", artifact=path, task=task_id,
                          project=meta.get("project") or "",
                          title=meta.get("title") or "")
        log(f"surface: ATTACHED dialogue {task_id} to {Path(path).name} "
            f"on the driver's request")
        sess = sessions(cfg)


def _dead_letter(cfg, log, path: str, meta: dict, raw: str,
                 payload: dict | None, reason: str) -> Path | None:
    """Write the whole consumed batch where a human can find it, record the
    file on the session so the fleet row can badge it, and say so on the
    board. Deleting (or moving) the file is the acknowledgement: status_list
    only reports dead letters that still exist."""
    try:
        d = stranded_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w.-]", "_", Path(path).stem)[:80] or "session"
        out = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}.json"
        n = 1
        while out.exists():
            n += 1
            out = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}-{n}.json"
        out.write_text(json.dumps({
            "at": time.time(), "artifact": path, "reason": reason,
            "session": meta, "prompts": (payload or {}).get("prompts") or [],
            "raw": raw}, ensure_ascii=False, indent=1), encoding="utf-8")
        s = sessions(cfg)
        if path in s:
            s[path]["stranded"] = [*(s[path].get("stranded") or []), out.name]
            _save_sessions(cfg, s)
        board_events.emit("surface_stranded", artifact=path, dead_letter=str(out),
                          reason=reason,
                          prompts=len((payload or {}).get("prompts") or []),
                          project=meta.get("project") or "",
                          title=meta.get("title") or "")
        log(f"surface: STRANDED feedback on {Path(path).name} ({reason}) — "
            f"batch kept verbatim at {out}")
        return out
    except Exception as e:
        log(f"surface: dead-letter write failed for {Path(path).name}: {e}")
        return None


def _task_bridge(cfg, reg, log, path: str, meta: dict, structured: list[dict],
                 free: list[dict], raw: str = "", payload: dict | None = None) -> None:
    """Surface -> board, for workflow #3's pages only.

    The rest of this module mirrors driver feedback to a PR, because for the
    pipeline the PR is the record. A dialogue has no PR: the BOARD is the
    record, so the driver's words become the two commands
    `config/actions-dialogue.json` triggers on and the engine takes the next
    turn from there.

    The mapping is the config's rule, not a choice made here:

        approve            -> `task:verdict`, and the dialogue is over
        annotations        -> `task:feedback`, the next turn of this session
        `continue` verdict -> ALSO `task:feedback` — continuing IS a feedback
                              turn, which is why the config has no continue
                              branch to fire.

        `handoff:<node>`   -> `task:handoff`: that node takes the work next
                              (see tasks.py "the handoff event"). A node not
                              registered to take it is refused and the batch
                              stranded, where the fleet shows it. `route:` is
                              accepted as the same thing: pages written before
                              the rename are still open and their forms say
                              route, and a driver ruling on one of those must
                              not silently do nothing.

    Approve and handoff are not rival verdicts. On the page, approving IS
    handing on: the Approve line carries a hand-to target, preselected, and the
    form sends `handoff:<node>` when one is set. A bare `approve` reaches here
    only when the driver deliberately cleared that target — "settled, and it
    stops with me" — or from an older page written before the target existed.

    So a handoff in the same batch wins over approve: a batch holding both is a
    driver who clicked Approve and then picked someone, and must not lose the
    handoff to the plain close.

    A bare approve carrying written annotations does not discard them. Those
    words are usually the decision itself, so they buy one closing turn (see
    `tasks.mark_closing`) and the verdict is written when that turn is reaped.
    """
    from . import tasks as tasks_mod
    task_id = meta.get("task") or ""
    if not task_id:
        log(f"surface: task feedback on {Path(path).name} with no task id — not routed (dead-lettered)")
        return
    notes = list(free)
    approve = asked_to_continue = False
    handoff = None
    for item in structured:
        if item.get("type") != "task_decision":
            continue
        # Both prefixes: `handoff:` is what forms say now, `route:` is what the
        # pages already open say. Dropping the old one would make the hand-off
        # line on every page written before today do nothing at all.
        if item["verdict"].startswith(("handoff:", "route:")):
            handoff = item["verdict"].split(":", 1)[1]
            notes.append(item)
        elif item["verdict"] == "approve":
            approve = True
        else:                       # continue: whatever it rode in on counts
            asked_to_continue = True
            notes.append(item)
    if approve and not handoff:
        written = [n for n in notes if (n.get("text") or "").strip()]
        if written:
            # Approving WITH annotations does not mean the annotations were
            # decoration. Most often they ARE the decision — the driver picks
            # among the options the page offered and approves in the same
            # gesture — so an approve that drops them throws away the content
            # and keeps the envelope. They used to be filed to
            # closing-annotations.json, which nothing has ever read.
            #
            # So the words get one closing turn: the session is resumed with
            # them, acts on them, and writes the decision back into its page
            # so the page becomes the record of what was settled. The verdict
            # is written when that turn is reaped, not here — approving is
            # still final, it just happens after the last thing said has been
            # heard.
            tasks_mod.mark_closing(reg, task_id)
            tasks_mod.write_feedback(cfg, reg, task_id, meta.get("cwd") or "",
                                     notes, closing=True)
            log(f"surface: {task_id} approved with {len(written)} annotation(s) — "
                f"closing turn resumes the session to act on them and record "
                f"the decision on the page; the dialogue closes when it ends")
            return
        tasks_mod.write_verdict(cfg, task_id, "approve")
        log(f"surface: {task_id} approved via surface — dialogue closed")
        end_session(cfg, path, log)
        return
    if handoff:
        _handoff(cfg, reg, log, path, meta, task_id, handoff, notes, raw, payload)
        return
    if not any((n.get("text") or "").strip() for n in notes):
        if not asked_to_continue:
            return
        # The driver pressed Continue and annotated nothing. That is still a
        # turn — refusing it would leave a button that does nothing — and the
        # line saying so is the bridge's, not words put in the driver's mouth.
        notes = [{"text": "Continue — the driver asked for another round "
                          "without annotating this page."}]
    ev = tasks_mod.write_feedback(cfg, reg, task_id, meta.get("cwd") or "", notes)
    if ev is None:
        return
    log(f"surface: {task_id} round {ev['payload']['iteration']} requested — "
        f"{len(notes)} annotation(s) back to the session")


def _handoff(cfg, reg, log, path: str, meta: dict, task_id: str, node: str,
             notes: list[dict], raw: str = "", payload: dict | None = None) -> None:
    """The driver picked the node that takes the work next: write the handoff
    event. A node that is not registered to take it is refused before
    anything is written — and not quietly: the batch is stranded, which
    badges the page on the fleet ("feedback reached no one") with the
    driver's words kept verbatim, and the page stays open so they can pick
    again. A written handoff ends this page's session; the node writes the
    next page."""
    from . import tasks as tasks_mod
    try:
        tasks_mod.write_handoff(cfg, reg, task_id, node, path, notes)
    except tasks_mod.UnknownNode as e:
        _dead_letter(cfg, log, path, meta, raw, payload, f"handoff refused: {e}")
        return
    log(f"surface: {task_id} handed to {node} from {Path(path).name} "
        f"with {sum(1 for n in notes if (n.get('text') or '').strip())} annotation(s)")
    end_session(cfg, path, log)


def _keep(cfg, task_id: str, name: str, notes: list[dict]) -> Path:
    """Driver words no turn will read, kept verbatim beside the task's logs."""
    kept = Path(cfg.data_dir) / "logs" / task_id / name
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text(json.dumps(
        [{"text": n.get("text") or "", "anchor": n.get("anchor") or "",
          "prompt": n.get("prompt") or ""}
         for n in notes], indent=1))
    return kept


def _ensure_server(cfg, sess: dict, log) -> None:
    """The surface server self-stops when idle and dies on reboots; any open
    session must be reachable, so re-open the first one to relaunch it."""
    open_paths = [p for p, m in sess.items() if m.get("open") and Path(p).exists()]
    if not open_paths:
        return
    import urllib.request
    try:
        urllib.request.urlopen(upstream() + "/health", timeout=3)
        return
    except OSError:
        pass
    try:
        _run_cli([open_paths[0]])
        log("surface: server was down — relaunched")
    except Exception as e:
        log(f"surface: server relaunch failed: {e}")


def _sweep_stale(cfg, reg, log) -> None:
    """Close sessions that can no longer be acted on: artifact file deleted,
    an ask whose ticket is already answered/gone (a CLI answer must retire
    the surface twin, or the driver is shown a dead question — observed), or
    any session on a story that is no longer active (shipped/escalated)."""
    from . import messages
    sess = sessions(cfg)
    # reg.stories() filters to active — the whole point here is seeing the
    # NON-active ones, so read the raw table.
    stories = (reg.data.get("stories") or {}) if reg is not None else {}
    for path, meta in list(sess.items()):
        if not meta.get("open"):
            continue
        if meta.get("kind") == "external" and not str(path).startswith("/"):
            # a page-less registration (a terminal session) is keyed
            # synthetically, not by an artifact path — there is no file whose
            # absence could mean stale, and the first sweep was closing it
            continue
        stale = not Path(path).exists()
        if not stale and meta.get("story"):
            st = stories.get(meta["story"])
            if st is None:
                stale = True  # story record deleted (reset/re-plan)
            elif st.get("status") not in ("active", "intaking"):
                # "intaking" is live: S0 asks questions too, and treating it
                # as dead closed an ask surface 56s after it opened — the
                # driver was left a form that posts into nothing (observed on
                # the nex-151 clean run, 2026-09-12).
                stale = True
            elif (meta.get("kind") == "spec_review" and meta.get("pr")
                  and st.get("planning_pr")
                  and int(meta["pr"]) != int(st["planning_pr"])):
                # A re-planned story has a NEW planning PR; a surface pinned
                # to the superseded one is a zombie (observed: the driver
                # reviewed a closed PR's plan).
                stale = True
        if not stale and meta.get("kind") == "ask" and meta.get("ticket"):
            m = messages.get(cfg.data_dir, meta["ticket"])
            stale = m is None or m.get("answer") is not None
        if stale:
            end_session(cfg, path, log)
            board_events.emit("surface_closed", artifact=path, by="sweep")


def _tick(cfg, reg, ghc, log) -> None:
    from . import messages
    _sweep_stale(cfg, reg, log)
    sess = sessions(cfg)
    _ensure_server(cfg, sess, log)

    # 1) outbox signals -> consume feedback from exactly those sessions
    signalled = {sig.get("key") for sig in _read_outbox(cfg) if sig.get("key")}
    if signalled:
        # kind="external" is presence-only: those sessions belong to whoever
        # registered them (an orchestrator terminal session, a design round),
        # and poll delivery CONSUMES — polling one here would steal the
        # driver's feedback from the loop that is actually waiting on it.
        by_key = {(m.get("key") or (m.get("path") or "").rsplit("/", 1)[-1]): (p, m)
                  for p, m in sess.items()
                  if m.get("open") and m.get("kind") != "external"}
        for key in signalled:
            hit = by_key.get(key)
            if hit:
                _consume(cfg, reg, ghc, log, hit[0], hit[1])
        sess = sessions(cfg)  # consuming may close sessions

    # 1b) feedback queued on a page with no owner gets one
    _adopt_unowned(cfg, reg, log, sess)
    sess = sessions(cfg)
    # 1c) the driver asked for an agent on a page nobody owns
    _attach_requested(cfg, reg, log, sess)
    sess = sessions(cfg)

    # 2) new pending questions get artifacts. Only OPEN sessions suppress
    # re-authoring — counting closed ones meant a question whose surface got
    # swept could never come back, leaving a parked session waiting on an
    # answer the driver had no way to give.
    open_tickets = {m.get("ticket") for m in sess.values()
                    if m.get("kind") == "ask" and m.get("open")}
    stories = (reg.data.get("stories") or {}) if reg is not None else {}
    for msg in messages.pending(cfg.data_dir):
        if msg["ticket"] in open_tickets:
            continue
        st = stories.get(msg.get("story") or "")
        if st is None or st.get("status") not in ("active", "intaking"):
            continue  # same liveness rule as the sweep, or the two would churn
        path = author_question(cfg, msg)
        open_session(cfg, path, "ask", log, ticket=msg["ticket"],
                     story=msg.get("story"), pr=msg.get("pr"))


def _apply(cfg, reg, ghc, log, path: str, meta: dict, item: dict) -> None:
    from . import messages
    from .dispatcher import AGENT_MARKER
    if item["type"] == "answer":
        m = messages.answer(cfg.data_dir, item["ticket"], item["text"])
        board_events.emit("surface_answer", ticket=item["ticket"],
                          story=(m or {}).get("story"), text=item["text"][:2000],
                          anchor=(item.get("anchor") or "")[:ANCHOR_LIMIT],
                          selector=item.get("selector") or "",
                          tag=item.get("tag") or "", uid=item.get("uid") or "")
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
    if item["type"] == "decision" and item["gate"] in ("risk_hold", "spec_review",
                                                       "final_review"):
        slug, pr = item["story"], item["pr"]
        story = reg.stories().get(slug) or {}
        if item["gate"] == "spec_review" and story.get("planning_pr"):
            # The spec gate targets THE planning PR; a session-substituted
            # number in the form can be stale after a re-plan.
            pr = int(story["planning_pr"])
        if item["gate"] == "final_review":
            # Same trust rule: the open final PR from the store beats the
            # session-substituted number.
            finals = [int(n) for n, p in (story.get("prs_cache") or {}).items()
                      if p.get("role") == "final" and p.get("state") == "open"]
            if finals:
                pr = finals[0]
        repo = story.get("repo") or meta.get("repo")
        if not repo:
            log(f"surface: no repo known for {slug} — decision dropped")
            return
        board_events.emit("surface_approval", gate=item["gate"], story=slug,
                          pr=pr, verdict=item["verdict"])
        try:
            if item["verdict"] == "approve":
                ghc.merge_pr(repo, pr)
                ghc.comment(repo, pr, f"Approved via surface — merged. {AGENT_MARKER}")
                log(f"surface: {slug} PR #{pr} approved via surface — merged")
            elif item["verdict"] == "revise":  # spec gate: back to the agents
                ghc.comment(repo, pr,
                            f"{dispatcher.DRIVER_PREFIX} revise requested — my "
                            "annotations above say what to change.")
                log(f"surface: {slug} planning PR #{pr} sent to revise via surface")
            else:  # reject (risk hold)
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
        if not meta.get("open") or meta.get("kind") not in ("risk_hold", "spec_review",
                                                            "final_review"):
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
    sdir = stranded_dir(cfg)
    for path, meta in sessions(cfg).items():
        stranded = [n for n in (meta.get("stranded") or []) if (sdir / n).exists()]
        # a strand keeps its row even after the session closed — "Send & End"
        # is exactly how a final batch gets stranded, and the badge must not
        # vanish with the session
        if meta.get("open") or stranded:
            out.append({"kind": meta.get("kind"), "story": meta.get("story"),
                        "pr": meta.get("pr"), "ticket": meta.get("ticket"),
                        "path": meta.get("path"), "opened": meta.get("opened"),
                        "project": meta.get("project"), "title": meta.get("title"),
                        "role": meta.get("role"), "task": meta.get("task"),
                        "cwd": meta.get("cwd"), "stranded": stranded,
                        "artifact": path if str(path).startswith("/") else ""})
    return sorted(out, key=lambda s: s.get("opened") or 0, reverse=True)


def register_external(cfg, path: Path | None, project: str, title: str,
                      role: str = "", cwd: str = "", log=print) -> None:
    """Record a session the runner did NOT spawn — an orchestrator terminal
    session, a design discussion already open on Review Surface — so the fleet
    page shows it under its project instead of the driver holding the URLs in
    their head. With a path the artifact is opened/resumed and linked; without
    one the row is informational (a terminal session has no page). The runner
    never polls these: whoever registered the session owns its feedback loop,
    this is presence in the hierarchy only."""
    if path is not None:
        # reuse the normal open path so the URL and key are recorded the same
        # way as runner-spawned sessions; kind "external" keeps every consumer
        # (bridge, reaper) from mistaking it for a session it owns
        open_session(cfg, path, "external", log,
                     project=project, title=title, role=role, cwd=cwd)
        return
    s = sessions(cfg)
    s[f"external:{project}:{title}"] = {
        "kind": "external", "path": "", "key": "", "open": True,
        "opened": time.time(), "project": project, "title": title, "role": role,
        # A pathless row with a cwd is a DISPATCH TARGET: the fleet renders it
        # clickable and clicking prefills the task box's working directory —
        # a bare presence row the driver cannot click reads as broken.
        "cwd": cwd}
    _save_sessions(cfg, s)
    log(f"surface: registered external session {project}/{title}")
