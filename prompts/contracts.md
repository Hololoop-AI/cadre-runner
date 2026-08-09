# Stage: Contracts (S2) — Planning PR merged; author per-slice test contracts

The driver merged the Planning PR: the scope is locked. Author plain-English integration test contracts for **all** slices, then open **one Contract PR per slice** so each slice can be approved (merged) independently — a slice whose contract merges proceeds to test-writing while other reviews stay open.

0. Workflow binding: `$workflows_dir` — when non-empty, auto-test-planning's workflow-backed authoring applies (its section -1).
1. You are on a clean `$feature_branch`. Read the merged planning artifact(s) and the slice list.
2. Invoke the **auto-test-planning** skill to author the contracts — per slice: setup, action, input, expected output, side effects, error cases; mock boundaries per the codebase's real infrastructure (controlled deps real, uncontrolled mocked at the adapter).
   - Variant **change-spec**: one file per slice — `changes/$story_slug/contracts/<slice>.md`.
   - Variant **spec-as-source**: contracts land inside each slice's `spec/<slice>.md`.
3. For **each** slice, in dependency order:
   - branch `pipe/$story_slug/contract-<slice>` off `$feature_branch`, containing **only that slice's contract file(s)** — use exactly this branch name; the daemon routes by it
   - push; open a Contract PR (base `$feature_branch`) titled "[contracts] <slice>"
   - body: what the slice does, the contract's intent in two sentences, a line `Confidence: high|medium|low — <one-line why>` (high = the contract follows directly from the spec and codebase with no judgment calls a reviewer could reasonably dispute; medium/low = name the specific uncertainty — these WAIT for driver review while high-confidence contracts auto-merge), and: "Merging locks this contract and starts test-writing for this slice."
4. Update `.pipeline/state.json` on `$feature_branch`: `"stage": "slices"`, each slice's contract PR number. Commit, push.
5. Comment on the merged Planning PR (#$planning_pr): contract PRs opened (links), suggested review order.

Contracts must be reviewable by a human in plain English — a reader should be able to say "yes, that proves the slice works" without reading code. Never merge anything.
