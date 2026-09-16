# Portability audit — GHES + Jira target (2026-09-15)

Scope: everything except the GitHub transport (already moved to the `gh` CLI + GH_HOST, 2026-09-14).
Severity rated for a corporate GitHub Enterprise + Jira deployment.

## 1. Board seam is a facade — HIGH

No provider interface; `make_board` (board.py) returns `LinearBoard` and callers consume raw
Linear GraphQL dicts. A Jira provider must mimic Linear's payload shape.

- board.py `candidates()` returns raw Linear nodes (`id, identifier, title, description, url,
  labels{nodes}, assignee{name,email}`) — this shape is the unwritten contract.
- board.py phase→state map hardcodes literal state names "In Progress"/"In Review"/"Done";
  Jira moves via transitions, not a state-name set.
- pipeline.py `_board_intake` reads Linear field names and calls `board.move(issue["id"],
  "In Progress")` — a hardcoded state name outside the provider; story record stamps
  `"provider": "linear"`; `cmd_board_check` reaches into `cfg.intake['linear']` by name.
- board.py `status_text` composes GitHub PR links and slice grammar — the tracker module
  inherits GitHub vocabulary.

Port path: normalize `candidates()` to a neutral issue dict, move state maps into `[intake]`
config, fix the three out-of-module call sites, then add `JiraBoard`.

## 2. Agent command is node data in theory, hardcoded `claude` in practice — HIGH if the CLI changes

The registry genuinely stores the command as data and the engine computes argv via
`nodes.command_argv` — and the spawn path throws it away:

- engine_seam `run_spawn_spec` uses only `prompt_path`/`model`/`version`; `spec["argv"]` is
  never executed.
- runs.py `spawn()` re-derives argv with Claude Code flags baked in (`-p`, `--model`,
  `--effort`, `--output-format json`, `--session-id/--resume`,
  `--dangerously-skip-permissions`); only argv[0] is configurable.
- seed_nodes COMMAND template hardcodes literal `claude` and ignores `cfg.claude["bin"]` —
  exported node cards can disagree with what actually runs.
- claude_run.py `run_claude` is a second copy of the same flag set; evals invoke `claude`
  literally.

Port path: make `runs.spawn` execute the node's argv (template contract for session-resume /
effort), or minimally drive the seed template from `cfg.claude["bin"]`.

## 3. OS/environment assumptions — MEDIUM

- GNU `timeout` + `/bin/sh -c` wrapper; `os.waitpid`/`os.kill`; SIGCHLD reliance; `fcntl.flock`
  (POSIX-only). Fine under Linux/WSL, blocking Windows-native.
- claude_run `install_skills` hard-fails (SystemExit) if any of 23 skills is missing from
  `skills_source` (default `~/dev-config/ai-workflow-config/skills`) — fresh-machine install
  blocker; skills installed as symlinks into worktrees.
- actions-pipeline gh-watch body runs `python3 -m runnerlib.gh_watch` — relies on daemon cwd
  and `python3` on PATH.
- No systemd unit files ship; `cadre-status.service` is referenced but absent.

## 4. review-surface CLI + status host — MEDIUM

- surface.py: bare PATH lookup of `review-surface`; every driver gate silently degrades to
  nothing if absent. Health check hardcodes `http://127.0.0.1:4387/health` while statusd
  honors `CADRE_SURFACE_UPSTREAM` — the two can disagree.
- statusd binds `0.0.0.0:8181` (corporate-policy issue) and defaults STATUS_DIR to a literal
  path instead of the config's data_dir; board_events.py has the same drift.

## 5. Vocabulary leaks into layers that claim neutrality — MEDIUM

(Workflow vocabulary in actions-pipeline.json and gh_watch.py is by design; these are not.)

- surface.py is GitHub-specific: `ghc.comment`/`ghc.merge_pr`/raw labels POST; session metadata
  keys `pr`/`repo`. The "driver channel" is really the GitHub-PR driver channel.
- engine_seam carries stage names (`ROUND_CAPPED`, `stage:intake` adoption writes) and its
  docstrings say story text "lives in the Linear card"; test_agnosticism greps only
  engine/nodes/blackboard, so nothing guards the seam.
- poller.py `SUMMON_RE = @claude` — mention handle hardcoded; corporate bot account will differ.
- registry.py branch grammar and automerge.py title grammar are fixed text — a repo with
  enforced branch policy needs edits.
- prompts/_common.md references an unresolved `<runner>` placeholder that `_vars` never
  substitutes — agents must guess the path.

## Shortest path to a GHES+Jira port

1. Board normalization + config state maps + JiraBoard.
2. Spawn executes node argv (agent-command-as-data made true).
3. Config-ify: summon token, skills_source hard-fail, statusd bind/dir, board_events path,
   surface health URL.
4. Decide review-surface's presence on the target machine; ship service/launcher files.
