# Process evals

Fixture stories with known-correct end states, run against pipeline nodes,
scored deterministically. A process change (prompt, node, gate) gets a
before/after number instead of a feeling — and borrowing live stories to test
process changes stops being necessary.

## Method

- `fixtures/` — scripts that BUILD a small target repo from nothing (never a
  checked-in repo: generated fresh per run, so no eval can rot into passing on
  stale state).
- `stories/<name>.md` — the story text a fixture story runs with; beside it
  `<name>.expected.json` — the falsifiable claims about a correct outcome.
- `score_spec.py` — deterministic scorer for spec-writer output against an
  expected file. Scorers judge STRUCTURE (slice shape, validation paths, flow
  choices, question discipline) — never prose style.
- Judges — one plain script per driver gate, binary criteria, isolated call per
  criterion group, verbatim-quote evidence verified in Python, results appended
  to `results/*.jsonl`. `judge_spec.py` is gate (a); `judge_round.py` (b),
  `judge_risk.py` (c) and `judge_final.py` (d) share `judge_core.py`, which also
  appends a mechanically-derived RENDERED STRUCTURE inventory (diff hunks,
  chips, embedded media) to the graded text so page structure is judgeable
  without the judge reading markup. Criteria come from
  `docs/audits/2026-09-surface-quality.md` and the `auto-surface` skill; each
  judge has a good/corrupt fixture pair under `fixtures/`, and
  `tests/test_eval_judges.py` covers everything that is not the model call.
- `run_spec_eval.sh` — builds the fixture, runs the spec-writer node headless,
  scores. Each run costs real inference (one intake-scale run); run evals
  deliberately, not in CI-on-every-commit.

## Rules

- Models are ALWAYS pinned explicitly in eval runs (sessions must not inherit
  whatever the invoking terminal has set).
- An eval that can't fail is decoration: every expected file must contain at
  least one claim a plausible regression would violate.
- Score history is append-only (`results/`, gitignored locally, summarized in
  commit messages when a process change cites an eval).
