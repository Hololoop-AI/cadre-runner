# Stage: Interrogate (S1) — process review threads on the Planning PR

Planning PR: **#$planning_pr** (head `$planning_branch`). Interrogate round **$iteration of $max_rounds** — after round $max_rounds the runner escalates.

Invoke the **auto-interrogate** skill against this PR. It owns the stage — collecting open threads, researching, revising the artifact, replying, and the round summary.

## The round surface — where the driver actually reads your answer

Rewrite the driver's briefing at `$CADRE_SURFACE_OUT` every round (skip this
section if that variable is empty). Read `$surface_prev` first when it is
non-empty: that archived page is exactly what the driver last saw, and this one
replaces it.

Invoke the **auto-surface** skill for the page: it owns the skeleton (starting
with the standalone orientation block for a reader with zero shared context), the
iteration-round rules, the reading budget, and the layout rules. The gate
specifics:

- header carries **Round $iteration of $max_rounds**, and says the runner
  escalates to the driver after the last one
- the page opens with **What changed in round $iteration** — one row per driver
  point: quote what they said, what you did, where on the page it now lives. A
  point you pushed back on gets a row carrying the reasoning; a driver point with
  no row at all is a defect.
- below that, the full current briefing restated — the same shape intake wrote
  (decision first, driver calls second, then support), so the driver rules off
  this page alone rather than diffing it against memory. Mark the sections this
  round touched with `<span class="chip">changed</span>`; leave the rest unmarked
  so they can be skipped.
- end with EXACTLY this verdict form, `<PR>` replaced by $planning_pr (twice) and
  `<SLUG>` by `$story_slug` (once), its JavaScript unaltered. No double quotes
  inside attribute values anywhere on the page — they truncate HTML attributes.

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
