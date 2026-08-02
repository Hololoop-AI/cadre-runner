# Stage: Intake (S0) — story → planning PR + self-grill

Story input:

---
$story_text
---

If a previous intake attempt left partial work (branch/issue/PR already exists), resume and repair it — do not duplicate.

Do, in order:

1. **Feature branch.** `git fetch origin`, create `$feature_branch` off `origin/$default_branch`. Create `.pipeline/state.json` (schema in ground rules; `"stage": "grill"`, slices empty for now). Commit, `git push -u origin $feature_branch`.

2. **Tracking issue.** `gh issue create --title "[pipeline] $story_id: $story_title"` — body: the story text, plus a stage checklist (planning/grill → contracts → per-slice tests+build → assembly → final review) and one line explaining that this issue is the story's control surface (`/status`, `/escalate`).

3. **Planning artifacts.** Create `$planning_branch` off `$feature_branch` — use exactly this branch name; the daemon routes events by it. Load context first — read the codebase: stack, conventions, structure, existing tests (invoke the investigating skill for anything non-obvious). Then derive the scope using the slicing skill's methodology in autonomous mode: no user checkpoints — record reasoning, alternatives, and non-goals in the artifact instead.
   - Variant **change-spec**: write `changes/$story_slug/change-spec.md` — problem, approach, **repo map** (tree of the areas this change touches), **file-level change details in plain English** (per file: what changes and why), proposed slice list (small, independently buildable, integration-testable verticals), open questions.
   - Variant **spec-as-source**: land scope as `spec/*.md` stubs + `context/*.md` decision entries per the engineering skill's model.
   Commit and push.

4. **Planning PR.** Open with base `$feature_branch`, head `$planning_branch`, title "[planning] $story_id: $story_title". Body: story summary, slice list, and a **"How to drive this PR"** section: leave line comments or a review on the artifact; summon the agent with `@claude` in a comment or review body (write the token in backticks here — PR bodies are not scanned for summons, but comments are, and ground rule 5 still applies to everything else you post); merging this PR approves the scope and starts contract authoring.

5. **Self-grill.** Invoke the grill skill against your own planning artifact — all lenses: structure/tensions, terminology vs the codebase's existing language, prior-decision conflicts, necessity/scope (observed demand, one-branch version, concept budget, wiring completeness), plus one refutation attempt. Post the findings as a **PR review on the Planning PR**: each finding is a line comment on the relevant artifact line (use `gh api repos/$repo/pulls/<n>/reviews -f event=COMMENT` with a comments array); resolve what you can in the artifact first — post only findings that survived or genuinely need the driver. Review body: "Self-grill: N findings — see threads."

6. **State + handoff.** Update `.pipeline/state.json` on `$feature_branch` (planning PR number, tracking issue, slice names with `"stage": "pending"`), commit, push. Comment on the tracking issue: intake complete, link the Planning PR, next action = review it.
