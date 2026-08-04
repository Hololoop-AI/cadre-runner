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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runnerlib import claude_run, config as config_mod, dispatcher, poller
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
    claude_run.ensure_checkout(args.repo, checkout, repo_cfg["default_branch"])
    linked, missing = claude_run.install_skills(checkout, cfg.skills_source)
    log(f"checkout ready: {checkout}")
    log(f"skills linked: {len(linked)} new; missing from source: {missing or 'none'}")


# --------------------------------------------------------------------------- intake


def cmd_start(cfg, args):
    repo_cfg = cfg.repo(args.repo)
    checkout = cfg.checkout_dir(repo_cfg)
    story_text = Path(args.story_file).read_text() if args.story_file else args.story
    if not story_text:
        sys.exit("start: provide --story or --story-file")
    story_id = args.story_id or f"story-{time.strftime('%Y%m%d-%H%M')}"
    slug = slugify(story_id)
    title = args.title or story_text.strip().splitlines()[0][:80]
    variant = args.variant or repo_cfg.get("variant", "change-spec")

    reg = Registry(cfg.data_dir / "registry.json")
    if slug in reg.data["stories"]:
        sys.exit(f"start: story {slug!r} already registered (see `status`)")

    claude_run.ensure_checkout(args.repo, checkout, repo_cfg["default_branch"])
    claude_run.install_skills(checkout, cfg.skills_source)
    claude_run.prepare(checkout, repo_cfg["default_branch"])

    prompt = claude_run.render("intake", _vars(args.repo, repo_cfg, story_id, slug, title,
                                              variant, tracking_issue="TBD")
                               | {"story_text": story_text})
    log(f"intake: running claude ({cfg.model_for('intake')}) in {checkout} — this can take a while")
    ok, result, usage = _run(cfg, "intake", prompt, checkout, slug)
    if not ok:
        sys.exit(f"intake run failed — see log in {cfg.data_dir / 'logs' / slug}")
    log(f"intake session done: {result[:300]}")

    # discover what intake created (PR list is the source of truth)
    ghc = GitHub(cfg.data_dir / "etags.json")
    pulls = ghc.pulls(args.repo, base=feature_branch(slug))
    pulls = [] if pulls is NOT_MODIFIED else pulls
    planning = next((p for p in pulls if p["head"]["ref"] == stage_branch(slug, "planning")), None)
    issues = ghc.get(f"/repos/{args.repo}/issues",
                     {"state": "open", "creator": cfg.limits["allowed_actors"][0], "per_page": 50})
    tracking = next((i["number"] for i in issues
                     if f"[pipeline] {story_id}" in i["title"] and "pull_request" not in i), None)

    reg.add_story(slug, args.repo, story_id, title, variant)
    story = reg.get(slug)
    story["tracking_issue"] = tracking
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
    reg.save()

    if planning:
        log(f"story {slug} registered — planning PR #{planning['number']}, tracking issue #{tracking}")
        log("review the planning PR on GitHub; summon with @claude; merge it to start contracts")
    else:
        log("WARNING: intake ran but no planning PR found — inspect the run log and the repo, "
            "then re-run start (the intake prompt is written to resume partial work)")


# --------------------------------------------------------------------------- daemon


def cmd_run(cfg, args, single_pass=False):
    ghc = GitHub(cfg.data_dir / "etags.json")
    log(f"runner up — poll every {cfg.runner['poll_interval']}s")
    while True:
        # reload each pass so stories registered by `start` mid-run are picked
        # up (and never clobbered by this process's saves)
        reg = Registry(cfg.data_dir / "registry.json")
        for slug, story in list(reg.stories().items()):
            try:
                _poll_story(cfg, reg, ghc, slug, story)
            except Exception as e:  # keep the daemon alive; surface in logs
                log(f"ERROR polling {slug}: {e}")
            reg.save()
        if single_pass:
            return
        try:
            time.sleep(cfg.runner["poll_interval"])
        except KeyboardInterrupt:
            log("stopped")
            return


def _poll_story(cfg, reg, ghc, slug, story):
    events, open_prs = poller.collect_events(ghc, reg, slug, story)
    pairs = [(dispatcher.dispatch(story, ev, cfg.limits), ev) for ev in events]
    for action, event in poller.coalesce(pairs):
        _execute(cfg, reg, ghc, slug, story, action, event, open_prs)
    if dispatcher.all_built(story, open_prs):
        _assembly_stub(cfg, ghc, story)
        story["phase"] = "assembly-pending"


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


def _run_stage(cfg, reg, ghc, slug, story, action):
    stage, slice_name, pr = action["stage"], action.get("slice"), action.get("pr")
    repo_cfg = cfg.repo(story["repo"])
    checkout = cfg.checkout_dir(repo_cfg)
    claude_run.ensure_checkout(story["repo"], checkout, repo_cfg["default_branch"])
    claude_run.install_skills(checkout, cfg.skills_source)

    base = {"interrogate": stage_branch(slug, "planning")}.get(stage, story["feature_branch"])
    if stage == "revise":
        base = story["prs_cache"][str(pr)]["head"]
    claude_run.prepare(checkout, base)

    v = _vars(story["repo"], repo_cfg, story["story_id"], slug, story["title"],
              story["variant"], story["tracking_issue"])
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
    prompt = claude_run.render(stage, v)
    log(f"{slug}: running stage {stage}"
        + (f" (slice {slice_name})" if slice_name else "")
        + (f" on PR #{pr}" if pr else ""))
    ok, result, usage = _run(cfg, stage, prompt, checkout, slug)
    log(f"{slug}: stage {stage} {'done' if ok else 'FAILED'} — {result[:200]}")

    if stage == "interrogate":
        story["iterations"]["interrogate"] += 1
    elif stage == "revise":
        story["iterations"]["revise"][str(pr)] = story["iterations"]["revise"].get(str(pr), 0) + 1
    elif stage == "contracts" and ok:
        story["phase"] = "slices"
    if not ok:
        _comment(ghc, story, f"⚠️ Stage `{stage}` run failed (see runner logs). "
                             f"Re-summon with @claude after checking. {AGENT_MARKER}")


def _run(cfg, stage, prompt, checkout, slug):
    log_path = cfg.data_dir / "logs" / slug / f"{time.strftime('%Y%m%d-%H%M%S')}-{stage}.json"
    return claude_run.run_claude(
        prompt, checkout, model=cfg.model_for(stage), effort=cfg.claude["effort"],
        permission_mode=cfg.claude["permission_mode"], timeout=cfg.claude["timeout_seconds"],
        log_path=log_path, claude_bin=cfg.claude["bin"])


def _assembly_stub(cfg, ghc, story):
    _comment(ghc, story,
             "✅ All slices built and merged into the feature branch.\n\n"
             "Assembly (S5) isn't implemented in the local runner yet — run final CI, "
             "review aids, and open the feature→main PR manually for now. "
             f"{AGENT_MARKER}")
    log(f"{story['story_id']}: all slices built — assembly stub posted")


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
    _run_stage(cfg, reg, ghc, slug, story, action)
    reg.save()


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


def _vars(repo, repo_cfg, story_id, slug, title, variant, tracking_issue):
    return {
        "repo": repo, "story_id": story_id, "story_slug": slug, "story_title": title,
        "variant": variant, "variant_desc": VARIANT_DESC[variant],
        "feature_branch": feature_branch(slug),
        "planning_branch": stage_branch(slug, "planning"),
        "default_branch": repo_cfg["default_branch"],
        "tracking_issue": tracking_issue, "agent_marker": AGENT_MARKER,
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
    p = sub.add_parser("trigger", help="manually fire a stage for a slice (re-fire stranded builds)")
    p.add_argument("--story", required=True)
    p.add_argument("--stage", required=True, choices=["contracts", "tests", "build", "interrogate", "revise"])
    p.add_argument("--slice")
    p.add_argument("--pr", type=int)
    p.add_argument("--role")

    args = ap.parse_args()
    cfg = config_mod.load(args.config)
    {"install": cmd_install, "start": cmd_start, "status": cmd_status, "trigger": cmd_trigger,
     "run": lambda c, a: cmd_run(c, a, single_pass=False),
     "once": lambda c, a: cmd_run(c, a, single_pass=True)}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
