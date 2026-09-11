<!--
Seeded corruption fixture. Ground truth for the judge calibration set
(evals/PLAN-2026-09.md §3: "~20 seeded corruptions of accepted artifacts").
This file is briefing-good.md with exactly three defects introduced, each
marked below. The markers are HTML comments and are stripped before the
artifact reaches the judge (judge_spec.artifact_text) — a judge that could
read them would be grading the marker, not the prose.

  1. corruption: invented-file-reference  -> criterion `no_invention`
  2. corruption: dropped-constraint       -> criterion `constraint_coverage`
  3. corruption: restated-validation-path -> criterion `concrete_validation_paths`
-->
# Spec briefing — tag notes and filter by tag

## What this builds

Notes gain optional tags at add time, and `notes list` gains a `--tag` filter
so a user can see only the notes carrying a given tag. Nothing else about the
CLI changes.

## Scope, as slices

### Slice 1 — tags travel with a note through storage

`notes add "buy milk" --tag errand --tag home` accepts a repeatable `--tag`
option and persists the tags alongside the note. Tag normalization lives in a
new `notes/taxonomy.py` module, and the existing `notes/export.py` command is
updated to emit the tag column.
<!-- corruption: invented-file-reference — neither notes/taxonomy.py nor
     notes/export.py appears anywhere in the story; the export command does not
     exist. -->

**Validation path** — run `notes add "buy milk" --tag errand --tag home`, then
read the JSON storage file back and assert the stored record carries exactly
the two tags `errand` and `home`, in the order given.

**Flow** — vertical-tdd.

### Slice 2 — listing filtered by tag

`notes list --tag errand` prints only the notes carrying `errand`, in id order.
`notes list` with no `--tag` is unchanged.

**Validation path** — confirm that listing is filtered by tag: the tag-filtered
listing behaves as a tag-filtered listing should.
<!-- corruption: restated-validation-path — this restates the slice title
     instead of naming a concrete observable outcome. The good briefing's
     version seeds a store, runs the command and asserts the printed lines. -->

**Flow** — vertical-tdd.

## Constraints carried from the story

- Tags are optional, so every command must behave sensibly when a note has none.
<!-- corruption: dropped-constraint — the story's only stated constraint,
     "Storage stays one JSON file; existing files without tag fields keep
     loading", is gone: neither the single-file rule nor the
     backwards-compatible load survives anywhere in this briefing, and it is
     not listed as out of scope either. -->

## What this does NOT build

Taken directly from the story's non-goals: no renaming of tags, no deleting of
tags, no tag autocomplete, and no multi-tag boolean queries (`--tag a --tag b`
on `list` is out of scope for this change).

## Risks

- **Slice 2** — id ordering is asserted by the story but is a property of the
  existing listing code; if today's listing is insertion-ordered rather than
  id-ordered, slice 2 must sort explicitly rather than inherit.

## Open questions for the driver

None. The story states the outcome and the non-goals; nothing here needs an
answer before work starts.
