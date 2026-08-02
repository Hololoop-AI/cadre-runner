# Stage: Revision — summoned on PR #$pr ($role$slice_suffix)

The driver left comments on this open stage PR. Revision round **$iteration of $max_rounds** for this PR.

1. You are on a clean copy of the PR's head branch. Read the PR's diff and its stage artifact(s) fresh.
2. Collect ALL open conversation on PR #$pr: unresolved review threads (`gh api graphql` reviewThreads), PR comments, review bodies.
3. For each open item: think it through against the codebase; reply in-thread; revise where the point holds (commit per logical resolution); push back with reasoning where it doesn't.
4. Stage-specific constraints still bind:
   - **contract** PR: contract stays plain-English and human-verifiable.
   - **tests** PR: every test must still fail for the right reason after revision — re-run and update the failure evidence in the PR body if it changed.
   - **build** PR: locked test files remain untouched; full suite green; re-run the lock check (`git diff origin/$feature_branch...HEAD -- <locked test paths>` empty).
5. Push. Post one summary comment: what changed, what awaits the driver, and what merging this PR unlocks next.

Never merge anything.
