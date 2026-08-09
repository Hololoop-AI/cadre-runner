"""Event collection: turn GitHub polling results into normalized dispatcher
events. Network in, plain dicts out — no model calls here."""

import json
import re
import time

from .dispatcher import AGENT_MARKER
from .gh import NOT_MODIFIED
from .registry import classify_branch

SUMMON_RE = re.compile(r"@claude\b", re.IGNORECASE)
COMMAND_RE = re.compile(r"^/(status|escalate)\b")
MANIFEST_RE = re.compile(r"```cadre-manifest\s*\n(.*?)```", re.DOTALL)


def parse_manifest(body: str | None):
    """Slice manifest from a planning PR body — the approved plan as machine-
    readable state on the PR surface (nothing committed to the repo). Returns
    [{"name", "nodes", "depends_on", ...}] or None."""
    m = MANIFEST_RE.search(body or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    slices = data.get("slices")
    if not isinstance(slices, list):
        return None
    return [s for s in slices if isinstance(s, dict) and s.get("name")] or None


def collect_events(ghc, reg, slug: str, story: dict) -> tuple[list[dict], int]:
    """Returns (events, open_pipeline_pr_count) for one story."""
    repo = story["repo"]
    events: list[dict] = []

    # --- 1. PR structure: one ETag'd list call covers every stage PR ----------
    pulls = ghc.pulls(repo, base=story["feature_branch"])
    # The FINAL story PR rides the feature branch itself (feat/... -> main), so
    # the base-filtered call never sees it — without this, teammate @claude
    # summons on the one PR humans actually review are silently unroutable.
    finals = ghc.pulls(repo, head=f"{repo.split('/')[0]}:{story['feature_branch']}")
    if pulls is NOT_MODIFIED and finals is NOT_MODIFIED:
        prs = story.get("prs_cache", {})
    else:
        cached = story.get("prs_cache", {})
        prs = {}
        for pr in (pulls if pulls is not NOT_MODIFIED else []):
            role, slice_name = classify_branch(pr["head"]["ref"], slug)
            if role is None:
                continue
            prs[str(pr["number"])] = {
                "role": role, "slice": slice_name, "state": pr["state"],
                "merged": bool(pr.get("merged_at")), "head": pr["head"]["ref"],
            }
        for pr in (finals if finals is not NOT_MODIFIED else []):
            prs[str(pr["number"])] = {
                "role": "final", "slice": None, "state": pr["state"],
                "merged": bool(pr.get("merged_at")), "head": pr["head"]["ref"],
            }
        # a 304 on one call must not drop the other call's cached entries
        if pulls is NOT_MODIFIED:
            prs.update({k: v for k, v in cached.items() if v["role"] != "final"})
        if finals is NOT_MODIFIED:
            prs.update({k: v for k, v in cached.items() if v["role"] == "final"})
        story["prs_cache"] = prs

    open_count = sum(1 for p in prs.values() if p["state"] == "open" and p["role"] != "planning")

    # merge events (oldest PR numbers first for deterministic ordering)
    for num_s, p in sorted(prs.items(), key=lambda kv: int(kv[0])):
        num = int(num_s)
        if p["merged"] and not reg.seen(story, "merged", num):
            reg.mark_seen(story, "merged", num)
            _record_merge(reg, story, p)
            events.append({"kind": "pr_merged", "pr": num, "role": p["role"], "slice": p["slice"]})
        elif p["role"] in ("contract", "tests", "build"):
            reg.slice_rec(story, p["slice"])[f"{p['role']}_pr"] = num

    # --- 2. conversation: repo-wide since-feeds + per-open-PR reviews ---------
    watch_issues = {int(n) for n in prs} | ({story["tracking_issue"]} if story["tracking_issue"] else set())
    since = story["since"]

    for c in _fresh(ghc.issue_comments_since(repo, since)):
        issue_num = int(c["issue_url"].rsplit("/", 1)[1])
        if issue_num not in watch_issues or reg.seen(story, "comments", c["id"]):
            continue
        reg.mark_seen(story, "comments", c["id"])
        events.extend(_classify_body(c["body"], c["user"]["login"], c["id"],
                                     issue_num, prs, source="issue_comment"))

    for c in _fresh(ghc.review_comments_since(repo, since)):
        pr_num = int(c["pull_request_url"].rsplit("/", 1)[1])
        if str(pr_num) not in prs or reg.seen(story, "review_comments", c["id"]):
            continue
        reg.mark_seen(story, "review_comments", c["id"])
        events.extend(_classify_body(c["body"], c["user"]["login"], c["id"],
                                     pr_num, prs, source="review_comment"))

    for num_s, p in prs.items():
        if p["state"] != "open":
            continue
        reviews = ghc.reviews(repo, int(num_s))
        if reviews is NOT_MODIFIED:
            continue
        for r in reviews:
            if reg.seen(story, "reviews", r["id"]):
                continue
            reg.mark_seen(story, "reviews", r["id"])
            evts = _classify_body(r.get("body") or "", r["user"]["login"], r["id"],
                                  int(num_s), prs, source="review")
            if not evts:
                evts = _review_as_summon(ghc, repo, r, int(num_s), prs)
            events.extend(evts)

    # overlap the watermark 2 min; seen-IDs dedupe the replays
    story["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
    return events, open_count


def coalesce(actions_with_events: list[tuple[dict, dict]]) -> list[tuple[dict, dict]]:
    """One run per (stage, pr, slice) per pass — a single interrogate/revise run
    processes ALL open threads, so N summons collapse into one invocation."""
    out, seen_keys = [], set()
    for action, event in actions_with_events:
        if action["type"] == "run_stage":
            key = (action["stage"], action.get("pr"), action.get("slice"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
        out.append((action, event))
    return out


def _record_merge(reg, story, p):
    if p["role"] in ("contract", "tests", "build"):
        rec = reg.slice_rec(story, p["slice"])
        rec[f"{p['role']}_merged"] = True


def _review_as_summon(ghc, repo, review, pr_num, prs):
    """A submitted review IS the driver's 'your turn' act — treat marker-free
    reviews as implicit summons, no @claude required.

    Loop-safety subtlety: standalone line comments AND the agent's own
    in-thread replies both arrive wrapped in implicit empty-body reviews, and
    the loop marker lives in the comment bodies, not the review body. So an
    empty-body review only summons if at least one of its comments is
    marker-free (i.e., human-authored)."""
    body = review.get("body") or ""
    p = prs.get(str(pr_num))
    if p is None or AGENT_MARKER in body:
        return []
    if not body:
        if review.get("state") == "APPROVED":
            return []  # bare approval — merge is the act that matters
        comments = ghc.review_comments_for(repo, pr_num, review["id"])
        if not isinstance(comments, list) or not comments \
                or all(AGENT_MARKER in (c.get("body") or "") for c in comments):
            return []
    return [{"kind": "summon", "pr": pr_num, "role": p["role"], "slice": p["slice"],
             "id": review["id"], "body": body or "(review with line comments)",
             "actor": review["user"]["login"], "source": "review"}]


def _classify_body(body, actor, item_id, issue_num, prs, source):
    m = COMMAND_RE.match(body.strip())
    if m:
        return [{"kind": "command", "name": m.group(1), "actor": actor,
                 "id": item_id, "body": body}]
    if SUMMON_RE.search(body):
        p = prs.get(str(issue_num))
        if p is None:
            return []  # summon on tracking issue / unowned PR — not routable
        return [{"kind": "summon", "pr": issue_num, "role": p["role"], "slice": p["slice"],
                 "id": item_id, "body": body, "actor": actor, "source": source}]
    return []


def _fresh(items):
    return items if isinstance(items, list) else []
