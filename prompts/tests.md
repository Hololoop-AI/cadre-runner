# Stage: Tests (S3) — slice `$slice` — its Contract PR merged

The driver approved the spec: slice `$slice`'s contract is locked with it (unified spec — contracts live inside the approved spec documents, there is no separate contract PR). Translate the contract into executable red tests.

0. Workflow binding: `$workflows_dir` — when non-empty, auto-test-writer's workflow-backed authoring applies (its section -1); include the resulting `Confidence:` line in the Tests PR body.
1. You are on a clean `$feature_branch`. Read the locked contract for `$slice` — it lives in the approved spec: the `## Contract` section for this slice in `changes/$story_slug/change-spec.md`, or `changes/$story_slug/contracts/$slice.md`, or legacy `spec/$slice.md`. Branch `$tests_branch` off `$feature_branch`.
2. Invoke the **auto-test-writer** skill: integration tests per the contract, AAA structure, one test per contract case. Run each test and confirm it **fails for the right reason** (missing behavior — not import errors or typos). Capture the failure output.
2a. If a `<language>-quality` skill exists for the implementation language (python-quality, rust-quality), invoke it — test code is held to the same bar as implementation code.

3. **Feature CI must stay green after this merges.** If the repo provides a skip mechanism that reads PR reality (e.g. a CI step that queries which build PRs have merged and skips unbuilt slices' tests), wire the new tests into it. NEVER wire gating through a committed state file (ground rule 6). If the repo has no such mechanism yet, say so in the PR body: feature CI shows these tests red until the slice's build merges — expected and temporary, priced into the merge decision.
4. Push; open the Tests PR (base `$feature_branch`) titled "[$story_slug][tests][<o>/<N>] $slice" — `<o>/<N>` from the slice's `review_order` in the planning artifact's slice list (omit the token if the plan carries no ordering). Body:
   - per test: the contract case it proves + its captured failure reason (evidence it's red for the right reason)
   - the line: "**Merging locks these tests.** Build implements against them and may not modify them."

Tests define done — write them from the contract, not from any implementation ideas. Never merge anything.

If the workflow returned `notes_for_later` entries, add a **"Noted for later (out of scope)"** section at the end of the PR body listing them — real concerns about future stories, deliberately excluded from the Confidence line. Never let them lower it.

The PR body carries `Risk: <level> — <why>` from the workflow's derivation (the merge gate: low/medium auto-merge, high holds for the driver) and `Confidence: ...` as a logged, non-blocking signal. Add an **Assumptions** section only when some decision sits below high confidence.
