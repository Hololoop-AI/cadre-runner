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

If a previous intake attempt left partial work (branch/issue/PR already exists), resume and repair it — do not duplicate.
