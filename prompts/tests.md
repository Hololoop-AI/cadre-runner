# Stage: Tests (S3) — slice `$slice` — its Contract PR merged

The driver merged slice `$slice`'s Contract PR: the contract is locked. Translate it into executable red tests.

1. You are on a clean `$feature_branch`. Read the merged contract for `$slice` (variant layout: `changes/$story_slug/contracts/$slice.md` or `spec/$slice.md`). Branch `$tests_branch` off `$feature_branch`.
2. Invoke the **auto-test-writer** skill: integration tests per the contract, AAA structure, one test per contract case. Run each test and confirm it **fails for the right reason** (missing behavior — not import errors or typos). Capture the failure output.
2a. If a `<language>-quality` skill exists for the implementation language (python-quality, rust-quality), invoke it — test code is held to the same bar as implementation code.

3. **Feature CI must stay green after this merges.** Gate the slice's tests on pipeline state: skip them while `.pipeline/state.json` shows slice `$slice` not yet `"built"` (stack-appropriate mechanism — e.g. a pytest `skipif`/custom marker or vitest/jest guard that reads the state file). The Build PR flips the status on its branch, which un-skips them there and after its merge.
4. Push; open the Tests PR (base `$feature_branch`) titled "[tests] $slice". Body:
   - per test: the contract case it proves + its captured failure reason (evidence it's red for the right reason)
   - the line: "**Merging locks these tests.** Build implements against them and may not modify them."
5. Update `.pipeline/state.json` on `$tests_branch` (slice `$slice` → `"stage": "tests-in-review"`, tests PR number) as part of this PR.

Tests define done — write them from the contract, not from any implementation ideas. Never merge anything.
