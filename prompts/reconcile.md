# Stage: Reconcile — PR #$pr ($role$slice_suffix) has merge conflicts

PR #$pr cannot merge into `$reconcile_base` — another branch landed first and the changes collide. Resolve the conflict so the PR can proceed.

1. You are on the PR's head branch. `git merge origin/$reconcile_base`; examine every conflict.
2. **Locked-test rule:** if any conflict touches locked test files, STOP — abort the merge, post a driver-call comment on PR #$pr (claim → conflict → decision with a recommendation, plain words, identifiers parenthesized last) and end the run. Conflicting locked artifacts are a human decision by definition.
3. Resolve each conflict by **intent, not by side**: read what each branch was trying to do (their PR bodies, their contracts) and produce code honoring both. Never blindly take ours/theirs; never drop either side's behavior to make the merge compile.
4. The full test suite must be green after resolution. If it goes red and the fix isn't obvious from the conflict itself, stop and post a driver call instead of improvising code neither branch asked for.
5. Push. Replace the PR body's `Confidence:` line to describe the RESOLUTION: `high` = purely textual conflicts (adjacent-line edits, imports), suite green; `medium` = any semantic reconciliation (two behaviors interleaved) — name exactly what a reviewer should double-check. Post one comment in driver voice: what conflicted, how each side's intent was honored, the suite result.

Never merge the PR itself.
