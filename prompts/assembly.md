# Stage: Assembly (S5) — all slices built; polish, equip, open the final PR

Every slice of `$story_id` is built and merged into `$feature_branch`. Assemble the story for human review — the final PR must arrive already equipped.

1. You are on a clean `$feature_branch`. Run the full test suite first; it must be green before anything else. If red, STOP: comment the failure on tracking issue #$tracking_issue and end the run.
2. **Cross-slice refactor** — invoke the **refactor** skill over the combined diff vs `origin/$default_branch`. Per-slice refactors already ran inside each build; only cross-slice findings belong here: duplicated helpers across slices, drifted naming, dead scaffolding at the seams. Full suite green after. Commit to `$feature_branch`, push.
3. **Cross-slice code review** — invoke the **spec-review** skill (scope-discipline lenses) against the combined diff and each slice's contract. Its verdict and any findings go into the final PR body's review section — do not edit code here.
4. **Review aids** — invoke the **pr-walkthrough** skill against the combined change (head `$feature_branch`, base `$default_branch`). Commit `study/$story_slug/walkthrough.md` on `$feature_branch` — GitHub-flavored markdown, which renders where the reviewer already is. Produce only that: interactive HTML and flashcard decks need a host to be usable, and until one exists they cost tokens for a file nobody can open. Push.
5. **Final PR** — open `$feature_branch` → `$default_branch`, title "[story] $story_id: $story_title". Body per artifact-voice — orient first (what this story is and why, from the planning artifact), then: slice list with one-liners, a link to `study/$story_slug/walkthrough.md`, the spec-review verdict, the full-suite result. Merging this PR ships the story; it is the one PR written for human readers.
6. Comment on tracking issue #$tracking_issue: assembly done, link the final PR.
7. **Final-review surface** — the driver reviews on a Review Surface artifact,
   not the PR diff (GitHub stays available; the surface is primary). Write a
   self-contained HTML page to the exact path in `$CADRE_SURFACE_OUT` (skip if
   that env var is empty). This is YOUR briefing from full context, never a
   diff copy — and its stance is a DEMONSTRATION: you are the engineer
   showing the boss a finished assignment. Prove it works, explain why it
   works, name what might still be wrong. The driver approves a working
   solution to the story, not a diff:
   - the story's problems restated, and per problem the demonstration that
     it is now solved: the actual behavior, in the modality the change is
     experienced in. CLI/daemon → real command transcripts of the story's own
     scenario; UI → screenshots (drive the app headless, capture, save the
     PNGs beside the artifact and reference them by relative filename — the
     surface serves sibling files); API/service → request/response
     transcripts; data/pipeline → small before/after samples; bug fix → the
     repro failing before and passing after; performance → measured
     numbers, both sides. The passing locked tests are standing evidence
     under all of it. Never describe behavior you could show.
   - what shipped, in the story's own terms (2-3 sentences)
   - per slice: what it does now and how the locked tests prove it
   - **the code itself, curated**: the surface must be sufficient to review
     WITHOUT opening GitHub. Embed the hotspots — the handful of hunks a
     reviewer would actually scrutinize (new public surfaces, the trickiest
     logic, anything security- or data-touching) — as `<pre>` snippets with
     file:line captions and one sentence each on WHY it is written that way.
     Skim-level code (boilerplate, mechanical edits) gets a one-line mention,
     not a snippet. Every snippet is annotatable; a GitHub link per file is
     the escape hatch, not the venue.
   - what changed between plan and build: deviations, refactors, anything the
     spec reader would not expect
   - the spec-review verdict and any findings, plain
   - what you would look at first if something breaks — ranked residual risks
   Style: dark self-painted page (`background:#0f1115; color:#f7f3ea`), plain
   semantic HTML, no double quotes inside attribute values. End with EXACTLY
   this verdict form, substituting `<PR>` with the final PR number (twice) and
   `<SLUG>` with `$story_slug` (once):

```html
<form data-review-surface-question="verdict" onsubmit="event.preventDefault();
  const v=new FormData(event.currentTarget).get('verdict'); if(!v) return;
  window.reviewSurface.queuePrompt('CADRE_DECISION gate=final_review story=<SLUG> pr=<PR> verdict='+v,
    {tag:'choice', text:'Final verdict: '+v, element:event.currentTarget,
     data:{gate:'final_review', story:'<SLUG>', pr:<PR>, verdict:v}});">
<label><input type="radio" name="verdict" value="approve"> Approve — ship the story</label>
<label><input type="radio" name="verdict" value="revise"> Revise — send my annotations back</label>
<button type="submit">Queue verdict</button></form>
```

Never merge the final PR yourself — approval on the surface, or the driver's
GitHub merge, is the human act that ships the story.
