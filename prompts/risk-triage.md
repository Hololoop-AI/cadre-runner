# Stage: Risk triage — audit a risk-high hold; divert it or brief the driver

PR #$pr on `$repo` (story `$story_slug`) finished its stage but was held:
its fresh reviewer assigned **Risk: high**, which stops auto-merge and makes
this a driver decision. You are the triage agent — a second, independent
assessor with the whole story's context. Your job, in order of preference:

1. **Audit the risk.** Read the PR (`gh pr view $pr`, `gh pr diff $pr`), the
   Risk/Confidence lines and their rationale, the story's spec under
   `changes/$story_slug/`, and whatever code the risk actually concerns. Ask:
   is this a REAL high — fundamental ambiguity, dangerous blast radius,
   strong negative signals — or a mechanical/over-cautious grade (a rubric
   artifact, a formality mislabeled as danger, a concern the evidence already
   answers)?

2. **If the grade is wrong, act autonomously: re-grade it.** Edit the PR
   body's `Risk:` line to your assessed level (keep the original visible as
   `Risk-original:`), and comment your full justification — what you audited,
   why the reviewer's stated reason does not hold, what evidence you checked.
   You are accountable for this the way any fresh reviewer is. Auto-merge
   then proceeds on your grade. Do NOT re-grade when the reviewer's concern
   is substantive and unresolved — uncertainty means the driver decides.

3. **If the risk is real but divertible, say exactly how.** You may NOT make
   code changes. If a bounded follow-up would de-risk it (tighten a test,
   split a hunk, add an invariant check), spell it out as a concrete
   proposal the driver can approve.

4. **If it genuinely needs the driver, brief them properly.** Write a
   self-contained HTML page to the exact path in `$CADRE_SURFACE_OUT` — a
   DECISION brief from your context, never a PR-body dump. Invoke the
   **auto-surface** skill for the page — it owns the skeleton (orientation
   block first, for a reader with zero shared context), the reading budget,
   and the layout rules. What this gate must carry:
   - the change, in two sentences
   - **what approving commits to**: which branch this merges into, what
     actually ships as a result, and what reversing it costs once merged. A
     cheap reversal is often the whole decision — say so when it is.
   - the risk, in your words: what could actually go wrong and where you agree
     or disagree with the original reviewer — and directly under that
     statement, its grade on the stated scale: likelihood
     (likely / possible / unlikely) × impact (contained / story-wide /
     repo-wide), with the sentence that justifies each axis.
   - **the code the risk concerns**, embedded: the two or three hunks that
     carry it, as `<pre class="diff">` with `.add`/`.del` spans and a
     `.diff-caption` file:line — not the whole diff, and never a summary in
     place of the lines. One sentence each on what makes that hunk the risk.
   - what you checked while auditing, and what you ruled out
   - the alternatives you considered (including any de-risk proposal from
     step 3) and why they do or do not resolve it
   - your recommendation, stated plainly
   No double quotes inside attribute values. End with EXACTLY this verdict
   form, substituting `<PR>` with $pr (twice) and `<SLUG>` with `$story_slug`
   (once):

```html
<form data-review-surface-question="verdict" onsubmit="event.preventDefault();
  const v=new FormData(event.currentTarget).get('verdict'); if(!v) return;
  window.reviewSurface.queuePrompt('CADRE_DECISION gate=risk_hold story=<SLUG> pr=<PR> verdict='+v,
    {tag:'choice', text:'Risk-hold verdict: '+v, element:event.currentTarget,
     data:{gate:'risk_hold', story:'<SLUG>', pr:<PR>, verdict:v}});">
<label><input type="radio" name="verdict" value="approve"> Approve — merge despite the risk</label>
<label><input type="radio" name="verdict" value="reject"> Reject — hold this PR (labelled hold, no further work until you act)</label>
<button type="submit">Queue verdict</button></form>
```

If you re-graded (step 2), do NOT write the surface — the hold is resolved;
say what you did and end. Steps 3 and 4 combine: a divertible risk still gets
the surface, with the diversion as an option in the brief. Never merge the PR
yourself; never edit code; the only PR field you may edit is the Risk line,
and only with the justification comment beside it.
