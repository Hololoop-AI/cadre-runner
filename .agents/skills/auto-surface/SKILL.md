---
name: auto-surface
description: Design and build driver review surfaces (decision pages) for the Cadre pipeline. Invoke whenever a stage authors or rewrites the artifact at $CADRE_SURFACE_OUT — spec briefings, interrogation rounds, risk holds, final reviews — or any ad-hoc decision page for the driver.
---

# Auto-Surface — the driver's decision page

A surface is a decision brief for a reader with **zero shared context**, not a report of
what you did. The driver may be arriving from a different story, hours later, with none of
your conversation history. Every design choice below serves two goals that pull against
each other: the driver fully understands the problem and the proposal, and their reading
time is minimized. When the goals conflict, cut detail — never clarity.

## Page skeleton (in this order, always)

1. **Orientation block** (3-5 lines, always first): what story this is, one sentence of
   what it's about, where in the pipeline it stands, and — on any round after the first —
   what has happened since the driver last looked. Written for someone with no context.
   Assume they just closed a different story's surface.
2. **The decision** (BLUF): what you propose, the single biggest judgement call inside it,
   and what saying yes commits to (what locks, what stays changeable, what they'll be asked
   next). If the page asks for a verdict, the reader should be able to give it from this
   box alone and treat everything below as verification.
3. **Driver calls**: the open questions, ranked. Each is one card: the question in one
   line, the options you weighed, the one you recommend, and what it blocks or costs to
   change later. A doubt without options is not a driver call — do the option work first.
4. **Support**: the evidence for the decision — briefing sections, contracts, code, proof.
   Everything here is skippable by design; say so implicitly through structure, never by
   padding.
5. **Verdict form** last, with button/label copy that names the real mechanism ("Approve —
   lock the spec and run", "Reject — hold this PR; no further work until you act"). Never
   promise an action the system doesn't perform.

## Iteration rounds (interrogate / revise)

- Read the previous round's page before writing (it's what the driver last saw; the
  archived copy is at `$surface_prev` when provided).
- Open section 1 with **What changed in round N** — one row per driver point: quote what
  they said, what you did about it, and where on the page the change lives. A point you
  pushed back on gets a row with your reasoning; silence on a driver point is a defect.
- Everything below is the full current briefing, restated — the driver decides off this
  page alone, never by diffing against memory. Mark changed sections with
  `<span class="chip">changed</span>`; leave unchanged sections unmarked so they can skip.
- Carry `Round N of M` in the header and say what happens when the budget runs out.

## Showing code

- Changed code renders as change, not final state: a unified hunk inside
  `<pre class="diff">` with `.add`/`.del` line spans, captioned via `.diff-caption`
  ("file.py:120-138 — the gate now runs before the write"). New files get one plain block.
- Curate and order: a numbered review path, each hotspot with one line on why it's at that
  position and what specifically to check there. An unordered pile makes the reader invent
  the path.
- Two or three hunks that carry the risk beat a complete listing every time. Link the PR
  for completeness; the page is the tour, not the archive.

## Showing structure — diagrams are whiteboards

An architecture, a flow, a state machine or a sequence goes in a Mermaid diagram,
never in box-drawing text inside `<pre>`. The page server turns every rendered
diagram in a `<div class="mermaid">` into an editable Excalidraw whiteboard: the
driver clicks it, redraws the part they disagree with, and the edit comes back to
you as a `whiteboard` feedback item with a summary. Apply it by changing the
Mermaid source, never by writing the scene back. Text art cannot be drawn on, so
it silently takes that answer channel away.

The page must render the diagram itself; the server does not load Mermaid. Put
this once, before `</body>`:

```html
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.15.0/dist/mermaid.esm.min.mjs";
mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
await mermaid.run({ querySelector: ".mermaid" });
</script>
```

Flowchart, sequence, class, ER and state diagrams become editable shapes; other
types become an image the driver can draw on. Keep one diagram to one idea.

## Showing a UI change — use it, record it, link it

When the work changes anything a person sees or clicks, the page is a demo, not a
description. The driver should not have to imagine the feature or trust that it works:
your team did the work, and this page is where you show it.

The evidence stacks; a richer kind never replaces a plainer one. Every UI change gets
screenshots. A flow that spans screens or moves on its own adds a video, and keeps
the screenshots. A change whose feel is the question adds a live link the driver can
click through, and keeps the screenshots and the video, because the live copy stops
when the page is ruled on and the stills are what remain.

1. **Use it yourself, in a real browser.** Drive the feature with Playwright, doing
   exactly what the driver would do: open the page, click, type, send, go back. If
   the change needs a running service you must not restart, run a scratch copy (its
   own data directory, port and daemon) rather than skipping this step. "Tests pass"
   is not evidence that a screen works.
2. **Show every step with a screenshot**, in the order the driver would meet them,
   each captioned with what it proves ("the tab updated without a reload"). Show
   the before and after for anything that moved.
3. **Record the full flow as a video** when it involves more than one screen, or
   something that happens on its own (a live update, an agent reply): open a
   Playwright context with `recordVideo`, run the flow, then close the context so
   the file is written. Embed it with `<video controls src="…">`.
4. **Put the media next to the page, and link to it by relative path.** Review
   Surface serves files beside the artifact: `media/01-home.png` resolves, but
   `/media/01-home.png` does not. Keep one folder per page, named after it.
5. **Link the exact working page**, as an absolute URL the driver can click
   (`http://127.0.0.1:8181/costs`), and say whether it is live now or only after a
   restart or deploy. A link to something not yet running must say so.
6. **Report what broke while you used it**, even when you fixed it. A bug the
   recording surfaced belongs on the page in words, with the fix, because the
   recording of the fixed flow does not show that it ever failed.
7. **Clean up.** Stop the scratch services and end the scratch review sessions you
   opened. List anything you left running, and why.

## Linking to another surface

A page is read inside the platform, served at `/session/<key>` — never opened as a file
from disk. So a plain relative href to another artifact (`href="other-page.html"`, or any
`../` path) resolves against the session URL and leads nowhere. The driver gets a dead
link to a page they can reach from the home page but not from the surface they're on.

Link by absolute artifact path through the resolver, which finds or opens that artifact's
session and redirects to it:

```html
<a href="/open?file=/absolute/path/to/the-other-surface.html">the framework discussion</a>
```

Rules that make these links trustworthy:

- **Absolute path, URL-encoded.** The resolver canonicalises it; a relative path is the
  bug this exists to fix.
- **Name the destination by what it is about**, never by an internal code or filename —
  "the discussion on how surfaces are organised", not "d7" or `hitl-d7-workspace.html`.
- **Say what the reader will find there and whether they need it now.** A bare link in
  the middle of a decision is an interruption; a sentence of orientation makes it
  skippable.
- **Never link a page you have not read this round.** Linking to a stale page is worse
  than not linking at all, because it reads as a citation.

## Layout rules (non-negotiable)

- Use the pipeline theme (`surface-theme.css` vocabulary) and nothing else — no custom CSS,
  no inline styles beyond what `_common.md` permits.
- Cap the measure: body text in a centered column (the theme's `.wrap`); never let prose
  run full-bleed.
- One question per card. Two decisions in one form guarantees one goes unanswered.
- Length budget: the uncollapsed page reads in ~3 minutes for a small change, ~6 for a
  large one. Contracts, transcripts, and long evidence go in `<details>` — open only the
  one thing the driver must see.
- Annotations are the driver's answer channel: structure the page so any sentence they'd
  want to challenge is its own element they can anchor a comment to (short paragraphs,
  one claim per bullet), not a wall of prose.

## Self-check before finishing

Read the page as a stranger: Can you tell what story this is and what's being asked within
ten seconds? Is every decision that needs the driver findable on the first screen? Does any
section restate the repo instead of briefing it? Would the page still make sense if this is
the fourth surface the driver opened today? Fix what fails; then stop polishing.
