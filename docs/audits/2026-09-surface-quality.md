# Driver-surface quality audit (2026-09-15)

Bar (driver's words): the reader must fully understand the problem and the proposed changes
while their reading time is optimized; iteration (back-and-forths) must work; risk stops must
explain the risk; PR-level review must explain the actual code changes and what to look for.

Added 2026-09-16 (driver note on the audit surface): surfaces assume the reader shares the
agent's context/chat history. **Every surface must brief a reader with zero shared context** —
open with a standalone orientation block (what story, where in it we are, what happened since
the driver last looked). This matters most when the driver bounces between several stories'
surfaces.

## Authoring model

Two paths: session-authored (`$CADRE_SURFACE_OUT`, per prompts/*.md — the real path) and
daemon-templated fallback (surface.py author_* — runs only when the session wrote nothing).
intake/interrogate/revise all share ONE out-path: `surfaces/spec-<slug>.html`.

## Gate (a) spec review — closest to the bar

intake.md already demands a briefing-not-a-copy ("what the diff cannot" give), annotated file
tree, per-slice contracts, ranked doubts. Gaps:

- Driver calls buried at position 5 of 6; should be second, retitled **Driver calls**, each:
  question in one line, options weighed, the one taken, what it costs to change later.
- "What approval commits to" never stated (only the radio label carries it).
- Alternatives-rejected only implicit ("what self-interrogation killed").
- No length budget: contracts should collapse into `<details>`; uncollapsed page readable in
  ~3 min (small change) / ~6 min (large).
- Open with the decision, not the context: three lines — what's proposed, the biggest
  judgement call, what merging locks.

## Gate (b) interrogation — the broken gate

- interrogate.md and revise.md never mention the surface; both stages get
  `CADRE_SURFACE_OUT=spec-<slug>.html` anyway.
- Round N overwrites round N−1 (single shared path) — no round history exists, so "what
  changed since your feedback" is unimplementable and unverifiable.
- The authored-spec reopen path has NO staleness check (the risk-hold path does:
  `st_mtime >= run["started"]`) — the driver can be re-shown round 1's briefing as current.
- Annotation anchors discarded: `_parse_feedback` keeps only `prompt`; the payload carries
  `uid/selector/tag/text`. The agent receives a floating sentence with no target. Mirror to
  PR truncates at 1500 chars; board event at 2000.
- Feedback is one-way: driver annotations mirror OUT to GitHub; replies never render back on
  the surface. The "primary channel" cannot show the answer to the driver's own question.

Prompt fixes (both stages): rewrite the surface each round; read the previous file first; open
with **What changed in round N** — one row per driver point (quote it), what was done, where
the change lives; declined points get a row with reasoning, never silent absence. Below that,
the full current briefing with `changed` chips on touched sections. Carry "Round N of M".

## Gate (c) risk hold — best-specified prompt, missing the decision half

risk-triage.md mandates a decision brief (change in two sentences, risk in own words,
what was checked, alternatives, plain recommendation) and autonomously re-grades mechanical
over-flags. Gaps:

- "What approving commits to" absent from the session-authored path (only the unused template
  has it): merge target, what ships, reversal cost.
- Zero code shown — embed the 2-3 hunks the risk concerns, file:line captions.
- No severity anchor: grade likelihood × impact on a stated scale
  (likely/possible/unlikely × contained/story-wide/repo-wide).
- Button copy lies: "Reject — send back" actually parks (hold label + comment, no revise
  round). Label must match the mechanism.

## Gate (d) final/PR review — ambitious, wrong center of gravity

assembly.md demands demonstration (real transcripts/screenshots), curated hotspot hunks, plan
deviations, ranked residual risks. Gaps:

- No review order: reuse the manifest's `review_order`; numbered hotspots, each with why it's
  at that position and what to check there.
- BLUF inverted: open with the verdict case in five lines (what shipped, one evidence pointer,
  the 1-2 residual risks, recommendation); move residual risks to right after the demo.
- Snippets are final-state, not diffs — and the theme has no add/remove styling while
  _common.md bans session CSS, so a diff view is impossible to author. Needs a theme
  primitive: before/after `<pre>` pairs or unified hunks with -/+ styling.
- Surface should be written before the PR body is drafted, not assembled from it.

## Measurement gap

evals/judge_spec.py covers gate (a) only, and scores groundedness, not decision-brief shape
(no BLUF/driver-call-first/what-approval-commits-to/length criteria). Gates b/c/d have no
judge at all. `artifact_text()` already parses surfaces — extending is rubric authoring, not
plumbing.

## Structural fixes (Pack 1)

1. Versioned round artifacts (`spec-<slug>-r<N>.html`) + canonical copy; prior round exposed
   to the next session.
2. Staleness guard on the authored-spec reopen path (mirror the risk-hold mtime check).
3. Preserve `selector`/`tag`/`text` through `_parse_feedback`; prefix PR mirrors with the
   anchored text as a blockquote.
4. Render agent replies back onto/into the surface session, not GitHub-only.
5. Diff primitive in surface-theme.css (+ documented vocabulary in _common.md).
