# Working in cadre-runner

- **One change, one worktree.** Never edit the main checkout
  (`~/Projects/cadre/cadre-runner`) in place: the live Cadre services run from
  it, and two unfinished changes in one tree block each other. Start every
  change on its own branch in `.worktrees/<name>`
  (`git worktree add -b <branch> .worktrees/<name> origin/main`), and merge it
  through a PR. The main checkout only ever moves by pulling `main`.
  When the PR merges, remove the worktree and delete the local branch
  (`git worktree remove .worktrees/<name> && git branch -D <branch>`); a deploy
  checkout must `git fetch` before it checks out `main`.
- **Tests:** `python3 -m pytest -q` (stdlib only; no claude, network or
  review-surface CLI needed).
- **Setup for people:** the README's "Quick start" and "More than one
  machine"; details in `docs/DIALOGUE-SETUP.md`.
- **No environment-specific details in the repo.** This repo is public and a
  general-purpose tool: no real hostnames, tailnet names, private IPs, user
  names or home paths in docs, examples, tests or scripts. Use placeholders
  (`<host>`, `box-1`, `100.64.0.x`, `/home/dev`); operator notes for a
  particular deployment live outside the repo.
