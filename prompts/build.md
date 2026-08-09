# Stage: Build (S4) — slice `$slice` — its Tests PR merged

The driver merged slice `$slice`'s Tests PR: those tests are **locked**. Implement until they pass.

1. You are on a clean `$feature_branch` (which now contains the locked red tests). Branch `$build_branch` off it. First commit: set slice `$slice` to `"stage": "built"` in `.pipeline/state.json` so the slice's tests un-skip **on this branch** and run red.
2. Invoke the **auto-build** skill: TDD against the locked tests until the slice's tests are green AND the full suite is green. The locked test files are read-only for you — if a test seems wrong, STOP, post the problem as a comment on the slice's merged Tests PR explaining why, and end the run; a locked-test change must go back through contracts, it is never made here.
3. If a `<language>-quality` skill exists for the slice's implementation language (python-quality, rust-quality), invoke it and hold the new code to its rules.
4. Invoke the **refactor** skill on the new code (reuse, simplification, altitude), then the **code-review** skill with its scope-discipline lenses (necessity, concept budget, wiring completeness) against the slice's contract and done criteria.
5. **Mechanical lock check** before opening the PR: `git diff origin/$feature_branch...HEAD -- <paths of the locked test files>` must be empty (the state-file line from step 1 lives in `.pipeline/`, not in test files). If it isn't, revert the test edits and fix the implementation instead.
6. Push; open the Build PR (base `$feature_branch`) titled "[build] $slice". Body: what was built and how the locked tests prove it, refactor notes, the code-review verdict, full-suite result, and the lock-check result.
7. Post the code-review findings (if any) as a self-review comment on the Build PR so the driver sees them inline.

Never merge anything. If iteration stops converging (same test failing after 5 distinct approaches), stop and report as a comment on the slice's merged Tests PR rather than thrashing.
