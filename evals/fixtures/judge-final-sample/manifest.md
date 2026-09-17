# Change manifest — tag-notes, run 41

What the run set out to do, what counts as done, and the review order the
assembly stage is meant to reuse. A reference for the judge, never the graded
artifact.

**Story** — tag-notes: notes gain optional tags at add time, and
`notes list --tag` filters by one.

**PR** — `notes#88`, branch `run-41/tag-notes` → `main`.

**Done-criteria**

1. `notes add "buy milk" --tag errand --tag home` stores exactly those two tags,
   in the order given.
2. `notes list --tag errand` prints only errand-tagged notes, in id order.
3. A store written before this change keeps loading, and `notes list` output for
   it is byte-identical to today's.
4. Storage stays one JSON file — no second file, no index, no database.

**review_order** (blast-radius ranked, highest first)

1. `notes/store.py:96-131` — the serialiser and the writer: the risk-hold guard
   landed here.
2. `notes/cli.py:40-72` — `--tag` parsing on `add` and `list`.
3. `tests/test_store.py:210-248` — the save-after-load test added for the hold.

**Plan deviations** — slices 1 and 2 merged into one slice after round 2 of
spec review; `notes search --tag` declined and not built.
