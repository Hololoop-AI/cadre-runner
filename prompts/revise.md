# Stage: Revision — summoned on PR #$pr ($role$slice_suffix)

The driver left comments on this open stage PR. Revision round **$iteration of $max_rounds** for this PR.

1. You are on a clean copy of the PR's head branch. Read the PR's diff and its stage artifact(s) fresh.
2. Collect ALL open conversation on PR #$pr: unresolved review threads (`gh api graphql` reviewThreads), PR comments, review bodies.
3. For each open item: think it through against the codebase; reply in-thread; revise where the point holds (commit per logical resolution); push back with reasoning where it doesn't.
4. Stage-specific constraints still bind:
   - **contract** PR: contract stays plain-English and human-verifiable.
   - **tests** PR: every test must still fail for the right reason after revision — re-run and update the failure evidence in the PR body if it changed.
   - **build** PR: locked test files remain untouched; full suite green; re-run the lock check (`git diff origin/$feature_branch...HEAD -- <locked test paths>` empty).
5. Re-earn the confidence line — never drop it, never carry it forward unexamined. Confidence comes from fresh adversarial agents, not from you: when `$workflows_dir` is non-empty, re-run the stage's workflow verification on the revised artifact (contract PR: contract-writer's breaker over the revised contract; tests PR: test-writer's verify pass) and write the resulting `Confidence: high|medium|low — <why>` into the PR body, replacing the old line. When no workflows path is bound, keep the prior line's level unless the revision resolved its named uncertainty — then say so in the why. A revised PR body without a Confidence: line silently loses its auto-merge path.
6. Push. Post one summary comment: what changed, the new confidence line and what earned it, what awaits the driver, and what merging this PR unlocks next.

Never merge anything.

## The round surface — where the driver actually reads your answer

Rewrite the driver's briefing at `$CADRE_SURFACE_OUT` every round (skip this
section if that variable is empty). Read `$surface_prev` first when it is
non-empty: that archived page is exactly what the driver last saw, and this one
replaces it.

Invoke the **auto-surface** skill for the page: it owns the skeleton (starting
with the standalone orientation block for a reader with zero shared context — this
driver may be arriving from another story), the iteration-round rules, the reading
budget, and the layout rules. The gate specifics:

- header carries **Round $iteration of $max_rounds** for PR #$pr, and says what
  happens when the budget runs out
- the page opens with **What changed in round $iteration** — one row per driver
  comment: quote it, what you changed, where it lives (file, or the section of
  this page). Where you pushed back, the row carries the reasoning; a comment
  with no row is a defect.
- below that, the current state of the PR restated in full — what it does now,
  the confidence line and what earned it, what merging unlocks — so the driver
  rules off this page alone. `<span class="chip">changed</span>` on the sections
  this round touched, nothing on the rest.
- when #$pr IS the planning PR (#$planning_pr), end with the `spec_review` verdict
  form below, `<PR>` replaced by $planning_pr (twice) and `<SLUG>` by
  `$story_slug` (once), its JavaScript unaltered. On any other PR the page carries
  no form — the driver acts on GitHub. No double quotes inside attribute values
  anywhere on the page — they truncate HTML attributes.

```html
<form data-review-surface-question="verdict" onsubmit="event.preventDefault();
  const v=new FormData(event.currentTarget).get('verdict'); if(!v) return;
  window.reviewSurface.queuePrompt('CADRE_DECISION gate=spec_review story=<SLUG> pr=<PR> verdict='+v,
    {tag:'choice', text:'Spec verdict: '+v, element:event.currentTarget,
     data:{gate:'spec_review', story:'<SLUG>', pr:<PR>, verdict:v}});">
<label><input type="radio" name="verdict" value="approve"> Approve — lock the spec and run</label>
<label><input type="radio" name="verdict" value="revise"> Revise — send my annotations back</label>
<button type="submit">Queue verdict</button></form>
```
