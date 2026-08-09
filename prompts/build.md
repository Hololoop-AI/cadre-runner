# Stage: Build (S4) — slice `$slice` — its Tests PR merged

The driver merged slice `$slice`'s Tests PR: those tests are **locked**. Implement until they pass.

0. Workflow binding: `$workflows_dir` — when non-empty, auto-build's workflow-backed building applies (its "Workflow-backed building" section); its check panel supplies the `Confidence:` line.
1. You are on a clean `$feature_branch` (which now contains the locked red tests). Branch `$build_branch` off it. If the repo gates unbuilt slices' tests via a PR-reality mechanism, note it in the PR body; never touch a committed state file (ground rule 6) — the slice's tests run red on this branch and that is the point.
2. Invoke the **auto-build** skill: TDD against the locked tests until the slice's tests are green AND the full suite is green. The locked test files are read-only for you — if a test seems wrong, STOP, post the problem as a comment on the slice's merged Tests PR explaining why, and end the run; a locked-test change must go back through contracts, it is never made here.
3. If a `<language>-quality` skill exists for the slice's implementation language (python-quality, rust-quality), invoke it and hold the new code to its rules.
4. Invoke the **refactor** skill on the new code (reuse, simplification, altitude), then the **code-review** skill with its scope-discipline lenses (necessity, concept budget, wiring completeness) against the slice's contract and done criteria.
5. **Mechanical lock check** before opening the PR: `git diff origin/$feature_branch...HEAD -- <paths of the locked test files>` must be empty. If it isn't, revert the test edits and fix the implementation instead.
6. Push; open the Build PR (base `$feature_branch`) titled "[$story_slug][build][<o>/<N>] $slice" — `<o>/<N>` from the slice's `review_order` in the planning artifact's slice list (omit the token if the plan carries no ordering). Body: what was built and how the locked tests prove it, refactor notes, the code-review verdict, full-suite result, the lock-check result, and a line `Confidence: high|medium|low — <one-line why>` derived STRICTLY from the code-review skill's verdict (fresh reviewer agents), never from your own assessment of your build — low/medium waits for the driver.
7. Post the code-review findings (if any) as a self-review comment on the Build PR so the driver sees them inline.

Never merge anything. If iteration stops converging (same test failing after 5 distinct approaches), stop and report as a comment on the slice's merged Tests PR rather than thrashing.
