# Stage: Intake (S0) — story → planning PR + self-interrogation

Story input:

---
$story_text
---

Invoke the **auto-spec** skill against this story. It owns the whole stage — mechanics, spec authoring, self-adversarial critique, planning PR, surviving questions.

Runtime bindings for the skill:
- Feature branch: `$feature_branch` (off `origin/$default_branch`)
- Planning branch: `$planning_branch` — exactly this name; the daemon routes events by it
- Artifact home (change-spec variant): `changes/$story_slug/change-spec.md`
- Story link: $story_url
- Spec-writer workflow: `$workflows_dir` — when non-empty, auto-spec's workflow path applies (see its "Workflow-backed drafting" section)

Title the planning PR exactly `[planning] $story_id: $story_title` — the runner lints PR titles and malformed ones block downstream automation.

The planning PR body MUST end with the slice manifest — the machine-readable plan the daemon dispatches from (state lives on PR surfaces, never in committed files):

````
```cadre-manifest
{"slices": [{"name": "<slice>", "nodes": ["tests", "build"], "depends_on": [], "wave": 1, "files": ["<loose footprint>"]}]}
```
````

One entry per slice, matching the artifact's slice list exactly. `nodes` is the slice's resolved flow as stage names — the default is `["tests", "build"]` because **the spec you are writing IS the contract layer** (unified spec, 2026-08-26): every slice's test contract — setup, action, input, expected output, side effects, error cases, in plain text with real function names — lives INSIDE the spec artifact, in a `## Contract` section per slice (or `contracts/<slice>.md` beside it). There is no separate contracts stage; the approval merge locks contracts and spec together. A slice whose flow skips tests omits that node (an author-only slice is `"nodes": ["build"]`). The manifest is frozen by the approval merge; interrogate/revise rounds that change the slice list MUST update it in the same round.

## The review surface — how the driver actually reads your plan

The driver reviews your spec on a Review Surface artifact, not the raw PR diff.
After the planning PR exists, write a self-contained HTML page to the exact path
in `$CADRE_SURFACE_OUT` (an absolute path in your environment; skip this section
entirely if that variable is empty).

This page is YOUR briefing, written from your full context — never a copy of the
files. The driver can read the raw spec on GitHub any time; the surface exists to
give them what the diff cannot:

- what the story asks and how you understood it (2-3 sentences)
- the shape of your solution: the slices, why you cut them there, what depends on what
- **the footprint, as a file tree** (required): an annotated tree of the
  repo areas this change touches — `<pre>` block, real paths, one short
  annotation per entry, new files marked distinctly from modified ones
  (e.g. `+ runner/provenance.py   new — loader + predicate` vs
  `~ runner/dispatch.py          gate after actor_allowed`). Untouched areas
  collapse to a bare directory line. Loose by design: caption it as the
  expected footprint, NOT a lock — builds may land differently and that is
  fine (only the spec prose and contracts lock at approval). No pseudocode.
  This is the map the driver glances at to see the blast radius; a Mermaid
  diagram is the wrong tool here (that belongs to true system-architecture
  views, which live upstream in the context repo).
- each slice: one paragraph of intent + **its full test contract, shown** —
  render the contract (or embed it in a collapsed `<details>`) so the driver
  can review it on the surface without opening the spec files; name the
  slice's flow (its `nodes`) beside it
- what you were unsure about: decisions you made that the driver might make
  differently, ranked — these are the things worth annotating
- what your self-interrogation killed or changed

Style: the surface palette (common rules above), exactly — readable
sections, no external assets except the Mermaid CDN if you use a diagram. Every
section annotatable (plain semantic HTML — the surface handles annotation).

End the page with EXACTLY this verdict form, substituting `<PR>` with the real
planning PR number (twice) and `<SLUG>` with `$story_slug` (once):

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

Do not alter the form's JavaScript. No double quotes inside attribute values
anywhere on the page — they truncate HTML attributes.

If a previous intake attempt left partial work (branch/issue/PR already exists), resume and repair it — do not duplicate.
