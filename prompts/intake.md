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

   **Write for a fast read. The driver reviews this and their reading time is the bottleneck — a scope that could be settled in a few prompts must not become a 300-line spec.** Lead with the decision, cut restatement and ceremony, and only write down what needs durable capture (decisions, contracts, non-obvious trade-offs) — not a narration of everything.
   - Variant **change-spec**: write `changes/$story_slug/change-spec.md` with **EXACTLY these four sections and no others** — target **under 80 lines total**:
     - **Problem** — 2–3 sentences.
     - **Approach** — one paragraph: the solution's shape and the load-bearing decisions. Name a rejected alternative only if it genuinely matters, as a single clause — never its own section.
     - **Slices** — one line each: name + what it delivers.
     - **Open questions** — only what genuinely needs the driver, one line each.

     Do **NOT** add sections such as "repo map", "file-level change details", "verified against code", "wiring status", or "rejected alternatives". Fold any essential such point into Approach or an Open Question as one terse line. You will often have done deep code analysis and self-interrogation to get here — the **conclusions** belong in the spec; the analysis does not. Self-interrogation findings go as PR review comments (step 5), never as write-backs inside the spec.
   - Variant **spec-as-source**: land scope as `spec/*.md` stubs + `context/*.md` decision entries per the engineering skill's model — same four-part, under-80-line bar per spec file.
   Commit and push.

4. **Planning PR.** Open with base `$feature_branch`, head `$planning_branch`, title "[planning] $story_id: $story_title". Body: story summary, slice list, and a **"How to drive this PR"** section: leave line comments or a review on the artifact; summon the agent with `@claude` in a comment or review body (write the token in backticks here — PR bodies are not scanned for summons, but comments are, and ground rule 5 still applies to everything else you post); merging this PR approves the scope and starts contract authoring.

5. **Self-grill.** Invoke the grill skill against your own planning artifact — all lenses: structure/tensions, terminology vs the codebase's existing language, prior-decision conflicts, necessity/scope (observed demand, one-branch version, concept budget, wiring completeness), plus one refutation attempt. Post the findings as a **PR review on the Planning PR**: each finding is a line comment on the relevant artifact line (use `gh api repos/$repo/pulls/<n>/reviews -f event=COMMENT` with a comments array); resolve what you can in the artifact first — post only findings that survived or genuinely need the driver. Review body: "Self-grill: N findings — see threads."

6. **State + handoff.** Update `.pipeline/state.json` on `$feature_branch` (planning PR number, tracking issue, slice names with `"stage": "pending"`), commit, push. Comment on the tracking issue: intake complete, link the Planning PR, next action = review it.
