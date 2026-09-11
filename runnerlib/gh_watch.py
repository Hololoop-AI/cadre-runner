"""The gh-watch edge: GitHub state onto the blackboard, as a CLI an action runs.

Per the driver decision, GitHub is reached by an action whose body runs a CLI —
there is no adapter component and no GitHub-aware code inside the engine. The
`gh-watch` action in `config/actions-pipeline.json` triggers on the daemon's
per-pass `heartbeat` and runs `python3 -m runnerlib.gh_watch`; this module is
that command. It reads the repo and writes events. It never spawns anything,
never merges anything, and never decides a transition — the actions do that.

One command, many events, which is why the writes happen here rather than in an
emitter: a poll pass can discover four merges, a conflict and two summons, and
a single `write_event` emitter can only say one thing.

Everything it emits lands on `cadre / github / story:<slug>`:

    signal  state=merged   role=planning|contract|tests|build|final, pr, slice
                           planning merges also carry `flow` (contracts|slices)
    signal  state=open     risk=high            — a PR whose reviewer graded it high
    signal  state=open     mergeable=conflict   — a PR git cannot merge
    signal  state=merged   role=all-built       — every planned slice is built
    command target=stage:revise|stage:interrogate  — a driver summon

Deduplication is its own state file (`gh-watch-seen.json`), deliberately NOT the
registry's seen-set: while CADRE_ENGINE is a feature flag, the legacy poller and
this watcher observe the same repo at the same time, and sharing a seen-set
would mean whichever ran first silently blinded the other.

Read-only classification reuses the existing modules rather than restating
them: `registry.classify_branch`, `poller.parse_manifest`, `poller.SUMMON_RE`,
`automerge.RISK_RE`, `dispatcher.all_built`, `dispatcher.AGENT_MARKER`. The
merge DECISION stays in `automerge.decide`, called by `_try_automerge` — this
module only reports the grade it can see.
"""

import json
import os
import sys
import time
from pathlib import Path

from . import automerge, config as config_mod, dispatcher, poller
from .blackboard import Board
from .gh import NOT_MODIFIED, GitHub
from .registry import Registry, classify_branch, feature_branch

NAMESPACE = "cadre"
TOPIC = "github"
SEEN_FILE = "gh-watch-seen.json"

# Which PR roles a summon may route to which stage (dispatcher.dispatch's
# summon branch, minus the round caps — those stay in the daemon).
SUMMON_STAGE = {"planning": "stage:interrogate", "contract": "stage:revise",
                "tests": "stage:revise", "build": "stage:revise",
                "final": "stage:revise"}


class Seen:
    """A flat set of `<kind>:<id>` markers. Crude and honest: the watcher only
    needs to know whether it has already said a thing."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.marks = set(json.loads(path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            self.marks = set()

    def take(self, mark: str) -> bool:
        """True the first time only."""
        if mark in self.marks:
            return False
        self.marks.add(mark)
        return True

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self.marks)))
        tmp.replace(self.path)


def key_for(slug: str) -> str:
    return f"story:{slug}"


def watch(board: Board, ghc: GitHub, reg: Registry, seen: Seen,
          cfg=None, now: float | None = None) -> list[dict]:
    """One pass over every active story. Returns the events written."""
    written = []
    for slug, story in reg.stories("active").items():
        try:
            written += _watch_story(board, ghc, reg, seen, slug, story, cfg)
        except Exception as e:
            # A repo the token cannot see must not stop the other stories, and
            # it must not look like success either.
            written.append(board.write(
                NAMESPACE, TOPIC, key_for(slug), "signal",
                {"status": "failed", "story": slug,
                 "error": f"{type(e).__name__}: {e}"[:300]}))
    seen.save()
    return written


def _watch_story(board, ghc, reg, seen, slug, story, cfg) -> list[dict]:
    repo, key = story["repo"], key_for(slug)
    out = []

    # -- PR structure. etag=False on purpose: the legacy poller holds the ETag
    # for these URLs, and a 304 handed to a watcher with no cache of its own
    # reconstructs nothing, forever (the nex-162 lesson, same shape).
    prs = {}
    for pulls, forced_role in ((ghc.pulls(repo, base=feature_branch(slug), etag=False), None),
                               (ghc.pulls(repo,
                                          head=f"{repo.split('/')[0]}:{story['feature_branch']}",
                                          etag=False), "final")):
        for pr in (pulls if pulls is not NOT_MODIFIED else []):
            role, slice_name = ((forced_role, None) if forced_role
                                else classify_branch(pr["head"]["ref"], slug))
            if role is None:
                continue
            prs[str(pr["number"])] = {"role": role, "slice": slice_name,
                                      "state": pr["state"], "number": pr["number"],
                                      "merged": bool(pr.get("merged_at")),
                                      "head": pr["head"]["ref"]}

    # -- merges: the events that advance the machine ---------------------------
    for num_s, p in sorted(prs.items(), key=lambda kv: int(kv[0])):
        if not p["merged"] or not seen.take(f"merged:{repo}:{num_s}"):
            continue
        payload = {"status": "finished", "state": "merged", "story": slug,
                   "repo": repo, "role": p["role"], "slice": p["slice"] or "",
                   "pr": p["number"],
                   "url": f"https://github.com/{repo}/pull/{num_s}"}
        if p["role"] == "planning":
            payload["flow"] = _plan_flow(ghc, repo, p["number"])
        out.append(board.write(NAMESPACE, TOPIC, key, "signal", payload))

    # -- open stage PRs: conflicts and risk grades -----------------------------
    open_count = 0
    for num_s, p in sorted(prs.items(), key=lambda kv: int(kv[0])):
        if p["state"] != "open":
            continue
        if p["role"] not in ("planning", "final"):
            open_count += 1
        if p["role"] not in ("contract", "tests", "build", "final"):
            continue
        try:
            detail = ghc.pr(repo, p["number"])
        except Exception:
            continue
        base = {"status": "blocked", "state": "open", "story": slug, "repo": repo,
                "role": p["role"], "slice": p["slice"] or "", "pr": p["number"],
                "url": f"https://github.com/{repo}/pull/{num_s}"}
        if detail.get("mergeable") is False:
            if seen.take(f"conflict:{repo}:{num_s}:{detail.get('head', {}).get('sha')}"):
                # The branch reconcile merges from — same rule as _maybe_reconcile:
                # final PRs rebase onto the default branch, slice PRs onto the
                # story's integration branch. Without this the reconcile prompt's
                # $reconcile_base renders empty on the engine path.
                rec_base = (cfg.repo(repo)["default_branch"] if p["role"] == "final"
                            else story["feature_branch"])
                out.append(board.write(NAMESPACE, TOPIC, key, "signal",
                                       {**base, "mergeable": "conflict",
                                        "reconcile_base": rec_base}))
            continue
        grade = automerge.RISK_RE.search(detail.get("body") or "")
        if grade and grade.group(1).lower() == "high" and p["role"] != "final":
            if seen.take(f"risk-high:{repo}:{num_s}"):
                out.append(board.write(NAMESPACE, TOPIC, key, "signal",
                                       {**base, "risk": "high"}))

    # -- the aggregate the closed `where` vocabulary cannot express ------------
    # dispatcher.all_built reads the approved manifest, not the slice records the
    # runner happens to have discovered. That is a join over story state, so it
    # is evaluated here and reported as one fact.
    if dispatcher.all_built(story, open_count) and seen.take(f"all-built:{repo}:{slug}"):
        out.append(board.write(NAMESPACE, TOPIC, key, "signal",
                               {"status": "finished", "state": "merged",
                                "role": "all-built", "story": slug, "repo": repo,
                                "slice": "", "pr": story.get("planning_pr") or ""}))

    out += _summons(board, ghc, seen, slug, story, prs, key)
    return out


def _plan_flow(ghc, repo, pr: int) -> str:
    """"contracts" or "slices" — which branch of dispatcher.dispatch's planning
    merge applies. The unified-spec rule (2026-08-26): when no slice's flow
    carries a contract node, the approved spec IS the contract.

    This classification lives here rather than in a trigger condition because
    the closed `where` vocabulary reads payloads, not PR bodies — so the fact is
    computed at the edge and STAMPED on the event, where an action can match it.
    """
    try:
        plan = poller.parse_manifest((ghc.pr(repo, pr) or {}).get("body"))
    except Exception:
        return "contracts"
    if not plan:
        return "contracts"    # legacy manifest-less story keeps the old path
    contract_nodes = any("contract" in (s.get("nodes") or ["contract", "tests", "build"])
                         for s in plan)
    return "contracts" if contract_nodes else "slices"


def _summons(board, ghc, seen, slug, story, prs, key) -> list[dict]:
    """Driver summons as in-board commands. A summon on a CLOSED PR is dropped
    here, not routed: its branch is often already gone, and "closing, superseded
    by X" is a normal thing for a human to write — it must not buy a stage run.
    """
    repo = story["repo"]
    since = story.get("since") or time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(time.time() - 3600))
    out = []
    feeds = ((ghc.issue_comments_since(repo, since), "issue_url", "issue_comment"),
             (ghc.review_comments_since(repo, since), "pull_request_url", "review_comment"))
    for items, url_field, source in feeds:
        for c in (items if isinstance(items, list) else []):
            try:
                num_s = c[url_field].rsplit("/", 1)[1]
            except (KeyError, AttributeError, IndexError):
                continue
            p = prs.get(num_s)
            body = c.get("body") or ""
            if p is None or p["state"] != "open":
                continue
            if dispatcher.AGENT_MARKER in body or not poller.SUMMON_RE.search(body):
                continue
            target = SUMMON_STAGE.get(p["role"])
            if not target or not seen.take(f"summon:{repo}:{c['id']}"):
                continue
            out.append(board.write(
                NAMESPACE, TOPIC, key, "command",
                {"target": target, "story": slug, "repo": repo, "pr": p["number"],
                 "role": p["role"], "slice": p["slice"] or "", "source": source,
                 "actor": (c.get("user") or {}).get("login", ""),
                 "comment_id": c["id"]}))
    return out


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="write GitHub state onto the blackboard")
    ap.add_argument("--config", default=os.environ.get("CADRE_CONFIG"))
    ap.add_argument("--data-dir", default=os.environ.get("CADRE_DATA_DIR"))
    args = ap.parse_args(argv)

    cfg = None
    if args.data_dir:
        data_dir = Path(os.path.expanduser(args.data_dir))
    else:
        cfg = config_mod.load(args.config)
        data_dir = cfg.data_dir
    board = Board(data_dir / "board.db")
    try:
        events = watch(board, GitHub(data_dir / "etags.json"),
                       Registry(data_dir / "registry.json"),
                       Seen(data_dir / SEEN_FILE), cfg)
    finally:
        board.close()
    # stdout is what the gh-watch action's report emitter records as its pointer.
    print(f"gh-watch: {len(events)} event(s) "
          + ", ".join(sorted({e['payload'].get('role') or e['payload'].get('target', e['kind'])
                              for e in events})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
