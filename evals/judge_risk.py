#!/usr/bin/env python3
"""Judge 3 — risk-hold decision briefs (gate c).

The risk hold is the only gate with merge authority, and per
`docs/audits/2026-09-surface-quality.md` its prompt is the best specified one we
have while the page is missing the decision half:

    "What approving commits to" absent … merge target, what ships, reversal cost.
    Zero code shown — embed the 2-3 hunks the risk concerns, file:line captions.
    No severity anchor: grade likelihood × impact on a stated scale
    (likely/possible/unlikely × contained/story-wide/repo-wide).

So this judge grades the brief as a DECISION, not as a description of a risk.
Note what it deliberately does not do: it never re-grades the risk itself. Per
`evals/PLAN-2026-09.md` §2 risk triage gets no judge (it is scored as
precision/recall against driver adjudication); what is judged here is whether
the grade the page states is anchored, justified per axis, and shown in code.

The reference is the RISK RECORD the triage stage worked from — the flagged
change, its blast radius, the diff available to the author. Criteria that ask
"is this grounded in the change" are extracted against it; criteria that ask
"can the driver decide from this page" are extracted against the page alone.

Usage:

    judge_risk.py --risk risk.md --artifact risk-hold.html [--model sonnet]
                  [--rubric PATH] [--results ...] [--board board.db] [--key ...]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge_core                                                  # noqa: E402
from judge_core import JudgeSpec, DEFAULT_MODEL                    # noqa: E402

run_claude = judge_core.run_claude          # patched by tests; see `_call`

# The scale the audit fixed. Stated here because the criterion is "graded on the
# stated scale", and a scale the judge has to guess is not a scale.
SCALE = ("likelihood ∈ {likely, possible, unlikely} × "
         "impact ∈ {contained, story-wide, repo-wide}")

# --------------------------------------------------------------------------- rubric

CRITERIA = {
    "approval_commits_to": {
        "type": "mandatory",
        "text": ("The page states what APPROVING commits to: where the change "
                 "lands (the merge target or what ships) and what undoing it "
                 "would cost. A page that only offers buttons, or that says what "
                 "the change is without saying what saying yes locks in, is a "
                 "FAIL."),
    },
    "graded_on_the_stated_scale": {
        "type": "mandatory",
        "text": ("The risk carries a likelihood grade AND an impact grade drawn "
                 f"from the stated scale ({SCALE}), both named on the page. A "
                 "bare severity word ('medium risk', 'high'), a number with no "
                 "scale, or only one of the two axes is a FAIL."),
    },
    "per_axis_justification": {
        "type": "mandatory",
        "text": ("Each of the two axes carries its own justification: why THIS "
                 "likelihood, and separately why THIS impact. One sentence "
                 "covering both, or a justification that only restates the grade "
                 "word, is a FAIL."),
    },
    "code_embedded_as_hunks": {
        "type": "mandatory",
        "text": ("The code the risk concerns is ON the page as hunks — the "
                 "RENDERED STRUCTURE inventory reports at least one diff block, "
                 "with a caption naming file and lines. A page that summarises "
                 "what the code does, or links out to the PR instead of showing "
                 "the hunks, is a FAIL."),
    },
    "alternatives_considered": {
        "type": "mandatory",
        "text": ("At least one alternative to the proposed handling is named and "
                 "weighed, with why it was not taken. 'No alternatives exist' "
                 "counts only if the page says why none do."),
    },
    "plain_recommendation": {
        "type": "important",
        "text": ("The page makes a plain recommendation in the author's own "
                 "voice — what they would do and why — rather than presenting "
                 "balanced options and leaving the driver to infer a preference."),
    },
    "decision_first_ordering": {
        "type": "important",
        "text": ("The decision comes before its support: the proposal, the "
                 "judgement call and what approval commits to appear in the "
                 "page's opening, ahead of the evidence, the hunks and the "
                 "checks that were run. A page that opens with background, "
                 "method or what was inspected is a FAIL."),
    },
    "risk_grounded_in_the_change": {
        "type": "important",
        "text": ("The risk is described concretely against THIS change — the "
                 "files, behaviour or data named in the risk record — in the "
                 "author's own words. A restatement of the mechanical flag text, "
                 "or a generic hazard ('migrations can be risky'), is a FAIL."),
    },
}

# One isolated call per group, cut by reference direction.
GROUPS = {
    # page alone, top-down: can the driver decide, and in what order are they
    # asked to read?
    "decision": ("approval_commits_to", "decision_first_ordering",
                 "plain_recommendation"),
    # the grade as an artifact of its own: anchored scale, per-axis reasons
    "grading": ("graded_on_the_stated_scale", "per_axis_justification"),
    # risk record -> page: is what is shown the actual change?
    "evidence": ("code_embedded_as_hunks", "alternatives_considered",
                 "risk_grounded_in_the_change"),
}

SPEC = JudgeSpec("judge_risk", "eval-judge-risk", CRITERIA, GROUPS)


def _call(prompt, model, timeout=judge_core.CALL_TIMEOUT):
    """Indirection so `judge_risk.run_claude` is what a test replaces."""
    return run_claude(prompt, model, timeout)


def references(risk: str) -> list[tuple[str, str]]:
    return [("RISK RECORD (the flagged change the brief is about)", risk)]


def judge(risk: str, artifact: str, model: str = DEFAULT_MODEL,
          rubric_path=None, groups=None) -> list[dict]:
    return judge_core.judge(SPEC, references(risk), artifact, model, rubric_path,
                            groups, _call)


# --------------------------------------------------------------------------- cli


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--risk", required=True,
                    help="the risk record the triage stage worked from: the "
                         "flagged change, blast radius, available diff")
    judge_core.add_common_args(ap, SPEC)
    args = ap.parse_args(argv)

    risk = judge_core.read_artifact(args.risk)
    return judge_core.run_cli(SPEC, args, references(risk), _call)


if __name__ == "__main__":
    sys.exit(main())
