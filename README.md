# PR-Gated Pipeline — Local Runner

The "work version" of the pipeline in `../design.md`: a polling daemon that drives the full story lifecycle through the GitHub UI, executing every stage as a headless `claude -p` session on this machine. No GitHub Actions, no claude-code-action — just `gh`, `git`, `python3` (3.11+), and the Claude Code CLI on a laptop.

**The driver never opens Claude Code.** You review stage PRs on GitHub, summon the agent with `@claude` in comments, and **merge a stage PR to approve it — merging is what advances the pipeline.**

## Lifecycle

```
pipeline start ─▶ [Planning PR + self-grill] ──you merge──▶ [Contract PR × slice] ──you merge──▶
  [Tests PR (red, locked on merge)] ──you merge──▶ [Build PR (TDD)] ──you merge──▶ … all slices …
  ──▶ assembly stub comment (S5 not yet implemented) ──▶ you open feature→main
```

Slices advance independently: merge slice A's contract and its tests/build proceed while slice B's contract review stays open.

## Quickstart

```bash
cp config.example.toml config.toml       # edit: allowed_actors, [[repos]]
python3 pipeline.py install --repo owner/name        # clone + link skills into the checkout
python3 pipeline.py start --repo owner/name \
    --story-id ENG-123 --story "As a user, I want ..."   # S0: planning PR + self-grill
python3 pipeline.py run                  # the daemon; leave it running
python3 pipeline.py status               # local view of every story
```

## Driving from GitHub

| You do | What happens |
|---|---|
| Leave line comments / a review on a stage PR, then a comment containing `@claude` | Next poll: one session processes **all** open threads — replies in-thread, revises, pushes |
| **Merge** a stage PR | The next stage starts (planning→contracts, contract→tests, tests→build) |
| `/status` on the tracking issue or any stage PR | Runner posts a progress summary (no model call) |
| `/escalate` anywhere | Runner stops dispatching the story; take over in Claude Code |

Notes: a review whose only content is line comments summons on its own comments' text — include `@claude` in the review body or a comment. Body-only "Approve" reviews don't summon; merging is the approval that matters.

## Dispatcher contract (design.md §2.6)

Deterministic, no model calls — `runnerlib/dispatcher.py`:

| Event | Guard | Action |
|---|---|---|
| Planning PR merged | phase == grill | run `contracts` (opens one Contract PR per slice) |
| Contract PR (slice s) merged | phase == slices | run `tests` for s |
| Tests PR (slice s) merged | phase == slices | run `build` for s |
| Build PR (slice s) merged | phase == slices | mark built; when **all** slices built + no open pipeline PRs → assembly stub |
| `@claude` on Planning PR | phase == grill, round < cap | run `grill` (processes every open thread) |
| `@claude` on contract/tests/build PR | round < cap for that PR | run `revise` on that PR |
| `/status`, `/escalate` | actor allowed | status post / stop story |
| anything by a non-allowed actor, or any body carrying the agent marker | — | dropped (loop guard) |

Iteration caps (default 5 rounds/stage) escalate instead of looping. PR roles come from branch naming: `pipe/<slug>/planning`, `…/contract-<slice>`, `…/tests-<slice>`, `…/build-<slice>` (stage branches use their own `pipe/` prefix because git forbids `feat/<slug>/x` while branch `feat/<slug>` exists).

## Architecture

```
pipeline.py            CLI + daemon loop + action execution
runnerlib/config.py    config.toml loading
runnerlib/gh.py        GitHub REST (token via `gh auth token`, ETag conditional requests — 304s are rate-limit-free)
runnerlib/poller.py    polling results → normalized events (~4-8 API calls/story/pass)
runnerlib/dispatcher.py  the contract above (pure functions; tests/test_dispatcher.py)
runnerlib/registry.py  local operational state (~/.local/state/pipeline-runner/registry.json)
runnerlib/claude_run.py  checkout prep + headless `claude -p` invocation + skill linking
prompts/*.md           stage prompts — ALL stage logic lives here + in the repo skills
```

Stage sessions run in a **runner-owned checkout** (`<data_dir>/checkouts/<repo>`), on an explicit model per stage (`[claude.stage_models]` — sessions never inherit the laptop's default model), with `--dangerously-skip-permissions` by default. That's confinement by convention, not enforcement: the checkout is disposable, the session can only push branches (`git push origin main` is forbidden by prompt, protect `main` in repo settings for real enforcement — recommended).

## v0 deviations from design.md (deliberate)

- **Registry-authoritative dispatch.** The daemon dispatches off branch/PR conventions + its local registry, not `.pipeline/state.json`. Sessions still maintain the state file as the durable record (and the future Actions runner's input), but the local runner doesn't depend on reading it back.
- **Loop guard is a comment marker** (`<!-- pipeline-run -->`), because sessions post as *your* account — actor filtering can't tell agent from driver. Every session-authored body carries the marker; marked bodies are never dispatched.
- **Assembly (S5) is a stub**: when the last Build PR merges, the runner posts a tracking-issue comment and stops. Adversarial pass, walkthrough + quiz, and the fresh final PR come next.
- **Failed runs don't retry.** Events are marked seen immediately (no retry storms); the runner posts a failure comment — check `<data_dir>/logs/<story>/` and re-summon.

## Troubleshooting

- Run logs (full prompt, result, cost): `<data_dir>/logs/<story>/<timestamp>-<stage>.json`
- Un-escalate a story: edit `registry.json`, set its `status` back to `"active"`.
- Intake ran but nothing registered: the discovery step needs the planning PR on branch `pipe/<slug>/planning` — inspect the log, fix the repo state, re-run `start` (intake resumes partial work).
- Work-laptop setup: install `gh` (authed), Claude Code CLI (logged in), Python 3.11+, copy this `runner/` dir + the `skills/` source dir, set `skills_source` in config.
