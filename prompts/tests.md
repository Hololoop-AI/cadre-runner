# Stage: Tests (S3) — slice `$slice` — its Contract PR merged

The driver merged slice `$slice`'s Contract PR: the contract is locked. Translate it into executable red tests.

0. Workflow binding: `$workflows_dir` — when non-empty, auto-test-writer's workflow-backed authoring applies (its section -1); include the resulting `Confidence:` line in the Tests PR body.
1. You are on a clean `$feature_branch`. Read the merged contract for `$slice` (variant layout: `changes/$story_slug/contracts/$slice.md` or `spec/$slice.md`). Branch `$tests_branch` off `$feature_branch`.
2. Invoke the **auto-test-writer** skill: integration tests per the contract, AAA structure, one test per contract case. Run each test and confirm it **fails for the right reason** (missing behavior — not import errors or typos). Capture the failure output.
2a. If a `<language>-quality` skill exists for the implementation language (python-quality, rust-quality), invoke it — test code is held to the same bar as implementation code.

3. **Feature CI must stay green after this merges.** If the repo provides a skip mechanism that reads PR reality (e.g. a CI step that queries which build PRs have merged and skips unbuilt slices' tests), wire the new tests into it. NEVER wire gating through a committed state file (ground rule 6). If the repo has no such mechanism yet, say so in the PR body: feature CI shows these tests red until the slice's build merges — expected and temporary, priced into the merge decision.
4. Push; open the Tests PR (base `$feature_branch`) titled "[$story_slug][tests][<o>/<N>] $slice" — `<o>/<N>` from the slice's `review_order` in the planning artifact's slice list (omit the token if the plan carries no ordering). Body:
   - per test: the contract case it proves + its captured failure reason (evidence it's red for the right reason)
   - the line: "**Merging locks these tests.** Build implements against them and may not modify them."

Tests define done — write them from the contract, not from any implementation ideas. Never merge anything.
