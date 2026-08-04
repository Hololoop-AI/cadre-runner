# Stage: Interrogate (S1) — process review threads on the Planning PR

Planning PR: **#$planning_pr** (head `$planning_branch`). Interrogate round **$iteration of $max_rounds** — after round $max_rounds the runner escalates, so drive threads toward resolution, not ping-pong.

You were summoned because the driver left new comments/reviews. Process **all** open conversation in one pass:

1. You are already on a clean `$planning_branch`. Read the planning artifact(s) fresh.
2. Collect the full open conversation:
   - unresolved review threads: `gh api graphql` on the PR's `reviewThreads` (include `isResolved`, comment bodies, ids, paths)
   - PR-level comments and review bodies: `gh pr view $planning_pr --comments` / `gh api`
3. For **each** open item, in artifact order:
   - Think it through against the codebase — read the relevant code before answering. If a point needs outside evidence, research it (investigating skill; durable findings go into the artifact — change-spec "Research" section or `context/research/` per variant — and are cited in your reply).
   - Reply **in that thread** (`gh api repos/$repo/pulls/$planning_pr/comments -f in_reply_to=<id>` for line threads). Where the point holds, say what you changed; where you disagree, push back with reasoning — interrogate is adversarial both ways, and silent compliance is a failure mode.
   - Revise the planning artifact as points land — commit per logical resolution with messages naming the thread.
4. Apply the interrogate skill's lenses to any **new** scope your revisions introduce (don't let fixes smuggle in unexamined additions).
5. Resolve only threads **you** opened (self-interrogation threads you have now addressed). The driver resolves their own.
6. Push. Post one PR comment: round summary — what changed, which threads await the driver, and a closing line: "When the scope is right, merge this PR — that locks it and starts contract authoring."

Never merge. Never resolve the driver's threads.
