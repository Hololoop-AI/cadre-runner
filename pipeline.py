#!/usr/bin/env python3
"""PR-gated pipeline — local runner (the "work version").

Commands:
  install --repo owner/name              clone + link pipeline skills into the checkout
  start   --repo owner/name --story ...  run intake (S0): feature branch, tracking issue,
                                         planning PR + self-interrogation
  run                                    poll daemon: dispatch merges/summons to stages
  once                                   single poll pass (testing)
  status                                 print local registry state
"""

import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runnerlib import automerge, board as board_mod, claude_run, config as config_mod, dispatcher, poller
from runnerlib import runs as runs_mod
from runnerlib import status as status_mod
from runnerlib.dispatcher import AGENT_MARKER
from runnerlib.gh import NOT_MODIFIED, GitHub
from runnerlib.registry import Registry, classify_branch, feature_branch, slugify, stage_branch

VARIANT_DESC = {
    "change-spec": ("change-level spec: planning artifacts live in changes/<story>/ "
                    "(change-spec.md + contracts/<slice>.md); nothing durable is required of the repo"),
    "spec-as-source": ("spec-as-source: planning artifacts are edits to the durable spec/*.md "
                       "and context/*.md layers"),
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- setup


def cmd_install(cfg, args):
    repo_cfg = cfg.repo(args.repo)
    checkout = cfg.checkout_dir(repo_cfg)
    claude_run.ensure_checkout(args.repo, checkout, repo_cfg["default_branch"], cfg.commit_identity)
    linked, missing = claude_run.install_skills(checkout, cfg.skills_source)
    log(f"checkout ready: {checkout}")
    log(f"skills linked: {len(linked)} new; missing from source: {missing or 'none'}")


# --------------------------------------------------------------------------- intake


def cmd_start(cfg, args):
    story_text = Path(args.story_file).read_text() if args.story_file else args.story
    if not story_text:
        sys.exit("start: provide --story or --story-file")
    story_id = args.story_id or f"story-{time.strftime('%Y%m%d-%H%M')}"
    reg = Registry(cfg.data_dir / "registry.json")
    if slugify(story_id) in reg.data["stories"]:
        sys.exit(f"start: story {slugify(story_id)!r} already registered (see `status`)")
    variant = args.variant or cfg.repo(args.repo).get("variant", "change-spec")
    slug, story, planning = _intake_story(cfg, reg, args.repo, story_id,
                                          args.title, story_text, variant)
    if planning:
        log(f"story {slug} registered — planning PR #{planning}")
        log("review the planning PR on GitHub; summon with @claude; merge it to start contracts")
    else:
        log("WARNING: intake ran but no planning PR found — inspect the run log and the repo, "
            "then re-run start (the intake prompt is written to resume partial work)")


def _intake_story(cfg, reg, repo_name, story_id, title, story_text, variant,
                  story_url=""):
    """S0 for one story, synchronous (CLI path): register, spawn, wait, finish.
    The board path uses _begin_intake alone and finishes at reap — the daemon
    never blocks on an intake. Raises RuntimeError on failure."""
    slug, story = _begin_intake(cfg, reg, repo_name, story_id, title, story_text,
                                variant, story_url=story_url)
    ghc = GitHub(cfg.data_dir / "etags.json")
    while any(not runs_mod.finished(r) for r in story["active_runs"].values()):
        time.sleep(10)
    _reap_runs(cfg, reg, ghc, slug, story)
    reg.save()
    if story["status"] == "intake-failed":
        raise RuntimeError(f"intake run failed — see log in {cfg.data_dir / 'logs' / slug}")
    return slug, story, story.get("planning_pr")


def _begin_intake(cfg, reg, repo_name, story_id, title, story_text, variant, story_url=""):
    """Register the story stub and spawn the intake run detached. Intake
    bypasses the concurrency cap — it's rare, and a queued intake with its
    board card already moved would never re-trigger."""
    repo_cfg = cfg.repo(repo_name)
    slug = slugify(story_id)
    title = title or story_text.strip().splitlines()[0][:80]
    reg.add_story(slug, repo_name, story_id, title, variant)
    story = reg.get(slug)
    story["status"] = "intaking"
    _run_stage(cfg, reg, GitHub(cfg.data_dir / "etags.json"), slug, story,
               {"stage": "intake", "slice": None, "pr": None,
                "extra_vars": {"story_text": story_text, "story_url": story_url}},
               skip_cap=True)
    reg.save()
    return slug, story


def _finish_intake(cfg, reg, ghc, slug, story, ok):
    """Reap-side of intake: discover what the run created (PR list is the
    source of truth), seed the cache, activate the story."""
    if not ok:
        story["status"] = "intake-failed"
        log(f"{slug}: intake FAILED")
        board = board_mod.make_board(cfg.intake)
        if story.get("board") and board and board.enabled:
            try:
                body = ("⚠️ Cadre intake failed — fix the cause, then move the card "
                        "back to the trigger column to retry.")
                cid = story["board"].get("status_comment_id")
                if cid:
                    board.edit_comment(cid, body)
                else:
                    board.comment(story["board"]["issue_id"], body)
            except Exception as e:
                log(f"{slug}: board failure-notice failed: {e}")
        return
    pulls = ghc.pulls(story["repo"], base=feature_branch(slug))
    pulls = [] if pulls is NOT_MODIFIED else pulls
    planning = next((p for p in pulls if p["head"]["ref"] == stage_branch(slug, "planning")), None)
    story["planning_pr"] = planning["number"] if planning else None
    # seed the PR cache so a first-poll 304 on the pulls ETag can't hide the planning PR
    story["prs_cache"] = {
        str(p["number"]): {
            "role": role, "slice": slice_name, "state": p["state"],
            "merged": bool(p.get("merged_at")), "head": p["head"]["ref"],
        }
        for p in pulls
        for role, slice_name in [classify_branch(p["head"]["ref"], slug)]
        if role is not None
    }
    story["status"] = "active"
    board = board_mod.make_board(cfg.intake)
    if story.get("board") and board and board.enabled:
        try:
            if planning:
                board.attach(story["board"]["issue_id"],
                             f"https://github.com/{story['repo']}/pull/{planning['number']}",
                             f"Planning PR #{planning['number']}")
        except Exception as e:
            log(f"{slug}: board attach failed: {e}")
    log(f"{slug}: intake done — planning PR #{story['planning_pr']}")


# --------------------------------------------------------------------------- daemon


def cmd_run(cfg, args, single_pass=False):
    ghc = GitHub(cfg.data_dir / "etags.json")
    board = board_mod.make_board(cfg.intake)
    log(f"runner up — poll every {cfg.runner['poll_interval']}s"
        f" · max {cfg.runner['max_concurrent_runs']} concurrent runs"
        + (f" · board intake: {cfg.intake['provider']}" if board and board.enabled else ""))
    status_mod.install_page(cfg)
    # Detached run wrappers are our children until they exit; auto-reap them so
    # the daemon never accumulates zombies (outcomes come from exit files).
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    while True:
        # reload each pass so stories registered by `start` mid-run are picked
        # up (and never clobbered by this process's saves)
        reg = Registry(cfg.data_dir / "registry.json")
        if board and board.enabled:
            try:
                _board_intake(cfg, reg, board)
            except Exception as e:  # tracker trouble must not stall the pipeline
                log(f"ERROR board intake: {e}")
        for slug, story in list({**reg.stories("intaking"), **reg.stories()}.items()):
            try:
                _poll_story(cfg, reg, ghc, slug, story)
            except Exception as e:  # keep the daemon alive; surface in logs
                log(f"ERROR polling {slug}: {e}")
            board_mod.mirror_status(board, story, cfg.limits["max_rounds_per_stage"], log)
            reg.save()
        act = [{"stage": r["stage"], "story": s, "slice": r.get("slice"),
                "pr": r.get("pr"), "started": r["started"]}
               for s, _rid, r in runs_mod.all_active(reg)]
        status_mod.write_status(cfg, reg,
                                run=(min(act, key=lambda r: r["started"]) if act else None),
                                runs=act)
        if single_pass:
            return
        try:
            time.sleep(cfg.runner["poll_interval"])
        except KeyboardInterrupt:
            log("stopped")
            return


def _board_intake(cfg, reg, board):
    """Start a story for each new card in the trigger column."""
    known = board_mod.known_issue_ids(reg)
    for issue in board.candidates():
        if issue["id"] in known or slugify(issue["identifier"]) in reg.data["stories"]:
            continue
        story_text = issue["title"] + ("\n\n" + issue["description"] if issue.get("description") else "")
        log(f"board: new card {issue['identifier']} — {issue['title']!r}; running intake")
        status_mod.write_status(cfg, reg, run={"stage": "intake", "story": issue["identifier"],
                                               "slice": None, "pr": None, "started": time.time()})
        # Acknowledge on the card BEFORE the (many-minute) intake run — silence
        # reads as failure. Moving the card out of the trigger column here also
        # prevents a failed intake from re-triggering every poll pass.
        ack_comment = None
        try:
            board.move(issue["id"], "In Progress")
            ack_comment = board.comment(
                issue["id"], "⏳ Cadre picked this up — intake is running "
                             "(10–30 min). The planning PR lands here when it's done.")
        except Exception as e:
            log(f"board: pickup ack failed for {issue['identifier']}: {e}")
        try:
            slug, story = _begin_intake(
                cfg, reg, cfg.intake["repo"], issue["identifier"], issue["title"],
                story_text, cfg.intake.get("variant") or cfg.repo(cfg.intake["repo"]).get("variant", "change-spec"),
                story_url=issue["url"])
        except Exception as e:
            log(f"board: intake spawn FAILED for {issue['identifier']}: {e}")
            body = f"⚠️ Cadre intake failed to start: {e} — fix the cause, then move the card back to the trigger column to retry."
            if ack_comment:
                board.edit_comment(ack_comment, body)
            else:
                board.comment(issue["id"], body)
            continue
        story["board"] = {"provider": "linear", "issue_id": issue["id"],
                          "identifier": issue["identifier"], "url": issue["url"],
                          "status_comment_id": ack_comment, "last_state": "In Progress"}
        reg.save()
        log(f"board: {issue['identifier']} intake spawned — finishes at reap")


def _poll_story(cfg, reg, ghc, slug, story):
    _reap_runs(cfg, reg, ghc, slug, story)
    if story["status"] == "intaking":
        return  # nothing to poll until the intake run reaps
    events, open_prs = poller.collect_events(ghc, reg, slug, story)
    _try_automerge(cfg, ghc, slug, story)
    # Events are seen-marked at collection, so a crash between collection and
    # execution would lose them forever (the since-window never re-collects).
    # Failed events persist in a retry queue instead: re-dispatched next pass,
    # dead-lettered loudly after 3 attempts. The system must work to build the
    # system.
    retries = story.get("retry_events", [])
    story["retry_events"] = []
    pairs = [(dispatcher.dispatch(story, ev, cfg.limits), ev) for ev in retries + events]
    for action, event in poller.coalesce(pairs):
        try:
            _execute(cfg, reg, ghc, slug, story, action, event, open_prs)
        except runs_mod.RunsBusy as e:
            # backpressure, not failure — requeue WITHOUT an attempt bump so a
            # crowded machine can never dead-letter a legitimate event
            story["retry_events"].append(event)
            log(f"{slug}: busy ({e}) — event requeued")
        except Exception as e:
            attempts = event.get("_attempts", 0) + 1
            if attempts >= 3:
                clean = {k: v for k, v in event.items() if k != "_attempts"}
                story.setdefault("dead_letter", []).append({"event": clean, "error": str(e)[:300]})
                log(f"{slug}: EVENT DEAD-LETTERED after {attempts} attempts: "
                    f"{event.get('kind')} on #{event.get('pr')} — {e}")
                _comment(ghc, story,
                         f"⚠️ A pipeline event failed {attempts}× and was parked: "
                         f"`{event.get('kind')}` on #{event.get('pr')} — `{e}`. "
                         f"After fixing the cause, re-fire with `pipeline.py trigger`. {AGENT_MARKER}")
            else:
                event["_attempts"] = attempts
                story["retry_events"].append(event)
                log(f"{slug}: event failed (attempt {attempts}/3), queued for retry: {e}")
    if (dispatcher.all_built(story, open_prs) and story["phase"] == "slices"
            and not runs_mod.key_active(story, "assembly", None, story.get("planning_pr"))):
        try:
            _run_stage(cfg, reg, ghc, slug, story,
                       {"stage": "assembly", "slice": None, "pr": story.get("planning_pr")})
        except runs_mod.RunsBusy as e:
            log(f"{slug}: assembly deferred ({e}) — retried next pass")


def _try_automerge(cfg, ghc, slug, story):
    """NEX-150: merge green stage PRs so the pipeline advances without a human,
    except contract PRs below high confidence. Merge events dispatch the next
    stage on the following pass — auto-merge IS the handoff."""
    if story["status"] != "active":
        return
    for num_s, p in list(story.get("prs_cache", {}).items()):
        if p["state"] != "open" or p["role"] not in ("contract", "tests", "build"):
            continue
        try:
            detail = ghc.pr(story["repo"], int(num_s))
            if detail.get("state") != "open" or detail.get("merged"):
                continue
            checks = ghc.check_runs(story["repo"], detail["head"]["sha"])
            bodies = _pr_conversation_latest_is_human(ghc, story["repo"], int(num_s))
            ok, reason = automerge.decide(p["role"], detail, checks, cfg.automerge, bodies)
            if ok:
                ghc.merge_pr(story["repo"], int(num_s))
                log(f"{slug}: auto-merged {p['role']} PR #{num_s} ({reason})")
            elif "driver gate" in reason or "human" in reason:
                pass  # expected waits — don't spam the log
        except Exception as e:
            log(f"{slug}: auto-merge check failed for PR #{num_s}: {e}")


def _pr_conversation_latest_is_human(ghc, repo, number):
    """True when the newest conversation item on the PR is marker-free (a human
    had the last word) — auto-merge must not close a PR mid-review."""
    items = []
    for c in ghc.get(f"/repos/{repo}/issues/{number}/comments", {"per_page": 100}) or []:
        items.append((c["created_at"], c.get("body") or ""))
    for c in ghc.get(f"/repos/{repo}/pulls/{number}/comments", {"per_page": 100}) or []:
        items.append((c["created_at"], c.get("body") or ""))
    for r in ghc.get(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 100}) or []:
        if r.get("body"):
            items.append((r["submitted_at"], r["body"]))
    if not items:
        return False
    items.sort()
    return dispatcher.AGENT_MARKER not in items[-1][1]


def _execute(cfg, reg, ghc, slug, story, action, event, open_prs):
    kind = action["type"]
    if kind == "noop":
        if "built" in action:
            log(f"{slug}: slice {action['built']!r} built ✓")
        else:
            log(f"{slug}: noop — {action['reason']}")
        return
    if kind == "post_status":
        _post_status(cfg, ghc, slug, story, open_prs)
        return
    if kind == "escalate":
        story["status"] = "escalated"
        _comment(ghc, story, f"⚠️ Pipeline escalated: {action['reason']}. "
                             f"The runner will ignore this story until re-activated. {AGENT_MARKER}")
        log(f"{slug}: ESCALATED — {action['reason']}")
        return
    if kind == "run_stage":
        _run_stage(cfg, reg, ghc, slug, story, action)


def _run_stage(cfg, reg, ghc, slug, story, action, skip_cap=False, wait=False):
    """Spawn a stage session detached in its own worktree. Raises RunsBusy on
    cap/branch contention (callers requeue); duplicate targets drop silently.
    wait=True (CLI paths: start, trigger) blocks until the run reaps."""
    stage, slice_name, pr = action["stage"], action.get("slice"), action.get("pr")
    if runs_mod.key_active(story, stage, slice_name, pr):
        log(f"{slug}: {stage}"
            + (f" (slice {slice_name})" if slice_name else "")
            + (f" on PR #{pr}" if pr else "") + " already running — skipped")
        return
    if not skip_cap and len(runs_mod.all_active(reg)) >= cfg.runner["max_concurrent_runs"]:
        raise runs_mod.RunsBusy(f"{cfg.runner['max_concurrent_runs']} concurrent runs")
    repo_cfg = cfg.repo(story["repo"])
    checkout = cfg.checkout_dir(repo_cfg)
    claude_run.ensure_checkout(story["repo"], checkout, repo_cfg["default_branch"], cfg.commit_identity)

    branch, start_ref = _run_branch(checkout, story, slug, stage, slice_name, pr, repo_cfg)
    if runs_mod.branch_held(reg, story["repo"], branch):
        raise runs_mod.RunsBusy(f"branch {branch} held by an active run")

    rid = (f"{time.strftime('%Y%m%d-%H%M%S')}-{stage}"
           + (f"-{slice_name}" if slice_name else "") + (f"-pr{pr}" if pr else ""))
    wt = cfg.data_dir / "worktrees" / slug / rid
    runs_mod.add_worktree(checkout, wt, branch, start_ref)
    try:
        claude_run.install_skills(wt, cfg.skills_source)
    except Exception:
        # never leak a worktree on a failed spawn — the event retries with a
        # fresh one, and nobody has shell access to sweep by hand
        runs_mod.remove_worktree(checkout, wt)
        raise

    v = _vars(story["repo"], repo_cfg, story["story_id"], slug, story["title"],
              story["variant"], story_url=(story.get("board") or {}).get("url", ""),
              workflows_dir=cfg.workflows_dir)
    v |= {
        "planning_pr": story.get("planning_pr") or pr or "",
        "pr": pr or "", "slice": slice_name or "", "role": action.get("role", ""),
        "slice_suffix": f", slice {slice_name}" if slice_name else "",
        "iteration": story["iterations"]["interrogate"] + 1 if stage == "interrogate"
        else story["iterations"]["revise"].get(str(pr), 0) + 1,
        "max_rounds": cfg.limits["max_rounds_per_stage"],
        "tests_branch": stage_branch(slug, "tests", slice_name) if slice_name else "",
        "build_branch": stage_branch(slug, "build", slice_name) if slice_name else "",
    }
    v |= action.get("extra_vars", {})
    prompt = claude_run.render(stage, v)
    run_dir = cfg.data_dir / "runs" / slug / rid
    pid = runs_mod.spawn(cfg.claude["bin"], prompt, wt, cfg.model_for(stage),
                         cfg.claude["effort"], cfg.claude["permission_mode"],
                         cfg.claude["timeout_seconds"], run_dir)
    story.setdefault("active_runs", {})[rid] = {
        "stage": stage, "slice": slice_name, "pr": pr, "pid": pid,
        "repo": story["repo"], "branch": branch, "model": cfg.model_for(stage),
        "worktree": str(wt), "run_dir": str(run_dir), "started": time.time(),
    }
    log(f"{slug}: spawned stage {stage}"
        + (f" (slice {slice_name})" if slice_name else "")
        + (f" on PR #{pr}" if pr else "") + f" — pid {pid}")
    if wait:
        while not runs_mod.finished(story["active_runs"][rid]):
            time.sleep(10)
        _reap_runs(cfg, reg, ghc, slug, story)


def _run_branch(checkout, story, slug, stage, slice_name, pr, repo_cfg):
    """(branch, start_ref) for a stage run's worktree. Every run owns exactly
    one branch — uniqueness is what makes concurrent runs safe."""
    fb = story["feature_branch"]
    if stage == "revise":
        b = story["prs_cache"][str(pr)]["head"]
        return b, f"origin/{b}"
    if stage == "interrogate":
        b = stage_branch(slug, "planning")
        return b, f"origin/{b}"
    if stage == "intake":
        return stage_branch(slug, "planning"), f"origin/{repo_cfg['default_branch']}"
    if stage in ("tests", "build"):
        b = stage_branch(slug, stage, slice_name)
        # resume from the remote branch when a prior run pushed it, else base off feature
        probe = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"origin/{b}"],
                               cwd=checkout, capture_output=True)
        return b, (f"origin/{b}" if probe.returncode == 0 else f"origin/{fb}")
    return fb, f"origin/{fb}"  # contracts, assembly


def _reap_runs(cfg, reg, ghc, slug, story):
    """Collect finished runs: write the log record, tear down the worktree,
    apply the stage's registry effects. Runs in every pass before events, so
    completions register before anything new dispatches."""
    for rid, run in list((story.get("active_runs") or {}).items()):
        if not runs_mod.finished(run):
            continue
        ok, result, usage, record = runs_mod.outcome(run)
        log_path = cfg.data_dir / "logs" / slug / f"{rid}.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(record, indent=2))
        del story["active_runs"][rid]
        runs_mod.remove_worktree(cfg.checkout_dir(cfg.repo(story["repo"])), Path(run["worktree"]))
        stage, pr = run["stage"], run.get("pr")
        log(f"{slug}: stage {stage}"
            + (f" (slice {run.get('slice')})" if run.get("slice") else "")
            + f" {'done' if ok else 'FAILED'} — {result[:200]}")
        if stage == "intake":
            _finish_intake(cfg, reg, ghc, slug, story, ok)
            continue
        if stage == "interrogate":
            story["iterations"]["interrogate"] += 1
        elif stage == "revise":
            story["iterations"]["revise"][str(pr)] = story["iterations"]["revise"].get(str(pr), 0) + 1
        elif stage == "contracts" and ok:
            story["phase"] = "slices"
        elif stage == "assembly":
            # ok -> awaiting the human merge of the final PR; fail -> parked for
            # a manual re-trigger (pipeline.py trigger --stage assembly)
            story["phase"] = "final-review" if ok else "assembly-pending"
        if not ok:
            _comment(ghc, story, f"⚠️ Stage `{stage}` run failed (see runner logs). "
                                 f"Re-summon with @claude after checking. {AGENT_MARKER}")
    _sweep_worktrees(cfg, story, slug)


def _sweep_worktrees(cfg, story, slug):
    """Remove worktree dirs no active run owns — crash litter from failed
    spawns. Runs every reap pass; the daemon is its own janitor because the
    box won't always have a human with shell access."""
    base = cfg.data_dir / "worktrees" / slug
    if not base.is_dir():
        return
    live = {Path(r["worktree"]).name for r in (story.get("active_runs") or {}).values()}
    for d in base.iterdir():
        if d.is_dir() and d.name not in live:
            runs_mod.remove_worktree(cfg.checkout_dir(cfg.repo(story["repo"])), d)
            log(f"{slug}: swept stale worktree {d.name}")


def _post_status(cfg, ghc, slug, story, open_prs):
    lines = [f"**Pipeline status — {story['story_id']}** (phase: `{story['phase']}`,"
             f" status: `{story['status']}`)", ""]
    lines.append(f"- Planning PR: #{story.get('planning_pr')} — interrogate rounds: "
                 f"{story['iterations']['interrogate']}/{cfg.limits['max_rounds_per_stage']}")
    for name, s in story["slices"].items():
        prog = [f"{r} {'✅' if s.get(f'{r}_merged') else ('#' + str(s[f'{r}_pr']) if s.get(f'{r}_pr') else '—')}"
                for r in ("contract", "tests", "build")]
        lines.append(f"- `{name}`: " + " · ".join(prog))
    lines.append(f"\nOpen pipeline PRs: {open_prs} {AGENT_MARKER}")
    _comment(ghc, story, "\n".join(lines))
    log(f"{slug}: status posted")


def _comment(ghc, story, body):
    if story.get("tracking_issue"):
        ghc.comment(story["repo"], story["tracking_issue"], body)


# --------------------------------------------------------------------------- misc


def cmd_trigger(cfg, args):
    """Manually fire a stage for a slice — the re-fire path for a build that
    exited without producing a PR (e.g. dependency-blocked, then unblocked when
    the dependency's own build merged). The daemon has no auto-retrigger for
    that case yet; this is the manual stand-in."""
    reg = Registry(cfg.data_dir / "registry.json")
    ghc = GitHub(cfg.data_dir / "etags.json")
    slug = args.story
    if slug not in reg.data["stories"]:
        sys.exit(f"trigger: story {slug!r} not registered (see `status`)")
    story = reg.get(slug)
    # refresh prs_cache so revise/base lookups resolve
    poller.collect_events(ghc, reg, slug, story)
    action = {"stage": args.stage, "slice": args.slice, "pr": args.pr,
              "role": args.role or ""}
    log(f"{slug}: manual trigger — stage {args.stage}"
        + (f" (slice {args.slice})" if args.slice else ""))
    _run_stage(cfg, reg, ghc, slug, story, action, skip_cap=True, wait=True)
    reg.save()


def cmd_board_check(cfg, args):
    """Validate the board connection live: config, key, team, trigger column."""
    board = board_mod.make_board(cfg.intake)
    if board is None:
        sys.exit("board-check: no [intake] provider configured")
    if not board.enabled:
        sys.exit(f"board-check: API key env "
                 f"{cfg.intake.get('linear', {}).get('api_key_env', 'LINEAR_API_KEY')} is not set")
    states = board.states()
    print(f"team {board.team!r}: connected — states: {', '.join(sorted(states))}")
    trig = board.trigger_state.lower()
    if trig not in states:
        sys.exit(f"trigger column {board.trigger_state!r} MISSING — create it on the team board")
    cands = board.candidates()
    known = board_mod.known_issue_ids(Registry(cfg.data_dir / "registry.json"))
    print(f"trigger column {board.trigger_state!r}: {len(cands)} card(s)"
          + (f" — {', '.join(c['identifier'] for c in cands)}" if cands else ""))
    for c in cands:
        print(f"  {c['identifier']}: {'already registered' if c['id'] in known else 'would intake'}")


def cmd_status(cfg, args):
    reg = Registry(cfg.data_dir / "registry.json")
    if not reg.data["stories"]:
        print("no stories registered")
        return
    for slug, s in reg.data["stories"].items():
        print(f"{slug}  [{s['status']}/{s['phase']}]  {s['repo']}  "
              f"planning=#{s.get('planning_pr')}  issue=#{s.get('tracking_issue')}")
        for name, sl in s["slices"].items():
            done = [r for r in ("contract", "tests", "build") if sl.get(f"{r}_merged")]
            print(f"   - {name}: merged={done or '[]'}")


def _vars(repo, repo_cfg, story_id, slug, title, variant, story_url="", workflows_dir=""):
    return {
        "repo": repo, "story_id": story_id, "story_slug": slug, "story_title": title,
        "variant": variant, "variant_desc": VARIANT_DESC[variant],
        "feature_branch": feature_branch(slug),
        "planning_branch": stage_branch(slug, "planning"),
        "default_branch": repo_cfg["default_branch"],
        "story_url": story_url or "(no board link — CLI-started story)",
        "workflows_dir": workflows_dir,
        "agent_marker": AGENT_MARKER,
    }


def main():
    ap = argparse.ArgumentParser(prog="pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install");  p.add_argument("--repo", required=True)
    p = sub.add_parser("start")
    p.add_argument("--repo", required=True)
    p.add_argument("--story", help="story text inline")
    p.add_argument("--story-file", help="path to story markdown")
    p.add_argument("--story-id", help="board id, e.g. ENG-123 (default: timestamp)")
    p.add_argument("--title")
    p.add_argument("--variant", choices=["change-spec", "spec-as-source"])
    sub.add_parser("run")
    sub.add_parser("once")
    sub.add_parser("status")
    sub.add_parser("board-check", help="validate board-intake config against the live tracker")
    p = sub.add_parser("trigger", help="manually fire a stage for a slice (re-fire stranded builds)")
    p.add_argument("--story", required=True)
    p.add_argument("--stage", required=True, choices=["contracts", "tests", "build", "interrogate", "revise", "assembly"])
    p.add_argument("--slice")
    p.add_argument("--pr", type=int)
    p.add_argument("--role")

    args = ap.parse_args()
    cfg = config_mod.load(args.config)
    {"install": cmd_install, "start": cmd_start, "status": cmd_status, "trigger": cmd_trigger,
     "board-check": cmd_board_check,
     "run": lambda c, a: cmd_run(c, a, single_pass=False),
     "once": lambda c, a: cmd_run(c, a, single_pass=True)}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
