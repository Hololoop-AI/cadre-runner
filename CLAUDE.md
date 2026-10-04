# Working in cadre-runner

- **One change, one worktree.** Never edit the main checkout
  (`~/Projects/cadre/cadre-runner`) in place: the live Cadre services run from
  it, and two unfinished changes in one tree block each other. Start every
  change on its own branch in `.worktrees/<name>`
  (`git worktree add -b <branch> .worktrees/<name> origin/main`), and merge it
  through a PR. The main checkout only ever moves by pulling `main`.
- **Tests:** `python3 -m pytest -q` (stdlib only; no claude, network or
  review-surface CLI needed).
- **Setup for people:** the README's "Installing it" and "Joining the shared
  team backend"; details in `docs/DIALOGUE-SETUP.md`, including "More than one
  machine" (several backends on one fleet page, over Tailscale).
