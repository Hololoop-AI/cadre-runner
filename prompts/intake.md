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
{"slices": [{"name": "<slice>", "nodes": ["contract", "tests", "build"], "depends_on": [], "wave": 1, "files": ["<loose footprint>"]}]}
```
````

One entry per slice, matching the artifact's slice list exactly. `nodes` is the slice's resolved flow as stage names — a slice whose flow skips contracts or tests simply omits those nodes, and the daemon exempts them (an author-only slice is `"nodes": ["build"]`). The manifest is frozen by the approval merge; interrogate/revise rounds that change the slice list MUST update it in the same round.

If a previous intake attempt left partial work (branch/issue/PR already exists), resume and repair it — do not duplicate.
