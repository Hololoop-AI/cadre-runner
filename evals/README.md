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
