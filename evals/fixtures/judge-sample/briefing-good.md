# Spec briefing — tag notes and filter by tag

## What this builds

Notes gain optional tags at add time, and `notes list` gains a `--tag` filter
so a user can see only the notes carrying a given tag. Nothing else about the
CLI changes.

## Scope, as slices

### Slice 1 — tags travel with a note through storage

`notes add "buy milk" --tag errand --tag home` accepts a repeatable `--tag`
option and persists the tags alongside the note in the existing single JSON
storage file.

**Validation path** — run `notes add "buy milk" --tag errand --tag home`, then
read the JSON storage file back and assert the stored record carries exactly
the two tags `errand` and `home`, in the order given. Then run `notes add
"plain"` with no `--tag` and assert the stored record for it is byte-identical
to what the current code writes for the same input.

**Flow** — vertical-tdd.

### Slice 2 — listing filtered by tag

`notes list --tag errand` prints only the notes carrying `errand`, in id order.
`notes list` with no `--tag` is unchanged.

**Validation path** — seed a store containing tagged and untagged notes, run
`notes list --tag errand`, and assert the printed lines are exactly the
`errand`-tagged notes ordered by id. Separately, capture the output of `notes
list` against a store written before this change and assert it is byte-identical
to the output the current code produces for that same store.

**Flow** — vertical-tdd.

## Constraints carried from the story

- Storage stays one JSON file. No second file, no index, no database.
- Storage files written before this change — records with no tag field at all —
  must keep loading, and must keep listing in exactly today's format.

## What this does NOT build

Taken directly from the story's non-goals: no renaming of tags, no deleting of
tags, no tag autocomplete, and no multi-tag boolean queries (`--tag a --tag b`
on `list` is out of scope for this change).

## Risks

- **Slice 1** — the backwards-compatible load is the risky half: a record with
  no tag field must not become a record with an empty tag list on rewrite, or
  the byte-identical guarantee in slice 2's validation path breaks on the first
  save. Load-tolerant, write-minimal.
- **Slice 2** — id ordering is asserted by the story but is a property of the
  existing listing code; if today's listing is insertion-ordered rather than
  id-ordered, slice 2 must sort explicitly rather than inherit.

## Open questions for the driver

None. The story states the outcome, the compatibility constraint and the
non-goals; nothing here needs an answer before work starts.
