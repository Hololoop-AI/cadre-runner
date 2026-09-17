#!/usr/bin/env python3
"""Judge 4 — final / PR-review surfaces (gate d).

`docs/audits/2026-09-surface-quality.md` calls gate (d) "ambitious, wrong center
of gravity": assembly.md already demands demonstration, curated hotspots and
ranked residual risks, but the page opens with context instead of the verdict
case, offers an unordered pile of snippets, and shows final state where the
reader needs change:

    BLUF inverted: open with the verdict case in five lines … move residual
    risks to right after the demo.
    No review order: numbered hotspots, each with why it's at that position and
    what to check there.
    Snippets are final-state, not diffs.

"Demonstration in the change's own modality" is the criterion the audit's word
*real* stands for: a CLI change demonstrated by a transcript, a UI change by a
screenshot, an API change by a request/response pair — present on the page, not
asserted in prose. The RENDERED STRUCTURE inventory is what makes that decidable
without the judge inspecting markup.

The reference is the CHANGE MANIFEST — what the run set out to do, its
done-criteria and its `review_order`, so coverage and ordering are graded
against something rather than against taste.

Usage:

    judge_final.py --manifest manifest.md --artifact final-review.html
                   [--model sonnet] [--rubric PATH] [--results ...]
                   [--board board.db] [--key eval:name]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge_core                                                  # noqa: E402
from judge_core import JudgeSpec, DEFAULT_MODEL                    # noqa: E402

run_claude = judge_core.run_claude          # patched by tests; see `_call`

# --------------------------------------------------------------------------- rubric

CRITERIA = {
    "verdict_case_first": {
        "type": "mandatory",
        "text": ("The page's opening lines carry the verdict case: what shipped, "
                 "at least one pointer to the evidence, and a recommendation the "
                 "driver could act on without reading further. A page that opens "
                 "with background, a plan recap, or a description of the work "
                 "done is a FAIL."),
    },
    "demonstration_in_modality": {
        "type": "mandatory",
        "text": ("The change is demonstrated in its own modality and the "
                 "demonstration is ON the page: a command transcript with its "
                 "real output, a screenshot (see the RENDERED STRUCTURE media "
                 "inventory), a request/response pair, a named test with its "
                 "result. Prose asserting that it was verified ('I ran it and it "
                 "works', 'tests pass') with no shown artifact is a FAIL."),
    },
    "numbered_review_order": {
        "type": "mandatory",
        "text": ("Hotspots are presented in an explicit numbered review order, "
                 "and each one says what to check THERE specifically. An "
                 "unordered pile of snippets, or numbered hotspots with no "
                 "per-hotspot instruction, is a FAIL."),
    },
    "code_shown_as_change": {
        "type": "mandatory",
        "text": ("Changed code is shown AS change: the RENDERED STRUCTURE "
                 "inventory reports diff blocks with added and removed lines. "
                 "Final-state listings only — code blocks with no removed lines "
                 "anywhere — is a FAIL, because the reader cannot see what moved."),
    },
    "residual_risks_adjacent": {
        "type": "important",
        "text": ("Residual risks are named, ranked or ordered, and sit adjacent "
                 "to the demonstration rather than in a trailing appendix, so the "
                 "reader meets the caveat while the evidence is still in view."),
    },
    "orientation_standalone": {
        "type": "important",
        "text": ("An orientation block names the story, what it is about and "
                 "where in the pipeline this page sits, so a reader with no "
                 "shared context and no chat history can place it. A page that "
                 "assumes the reader followed the run is a FAIL."),
    },
    "done_criteria_addressed": {
        "type": "important",
        "text": ("Every done-criterion in the CHANGE MANIFEST is either shown "
                 "met or reported unmet on the page. A criterion the page is "
                 "silent about is a FAIL — name it in `reason`."),
    },
}

# One isolated call per group, cut by reference direction.
GROUPS = {
    # page alone, top-down: verdict first, and can a stranger place the page?
    "verdict": ("verdict_case_first", "orientation_standalone"),
    # the proof: is the claim shown, and does the caveat sit beside it?
    "demonstration": ("demonstration_in_modality", "residual_risks_adjacent"),
    # manifest -> page: is the review path curated and complete?
    "review_path": ("numbered_review_order", "code_shown_as_change",
                    "done_criteria_addressed"),
}

SPEC = JudgeSpec("judge_final", "eval-judge-final", CRITERIA, GROUPS)


def _call(prompt, model, timeout=judge_core.CALL_TIMEOUT):
    """Indirection so `judge_final.run_claude` is what a test replaces."""
    return run_claude(prompt, model, timeout)


def references(manifest: str) -> list[tuple[str, str]]:
    return [("CHANGE MANIFEST (what the run set out to do, its done-criteria "
             "and its review order)", manifest)]


def judge(manifest: str, artifact: str, model: str = DEFAULT_MODEL,
          rubric_path=None, groups=None) -> list[dict]:
    return judge_core.judge(SPEC, references(manifest), artifact, model,
                            rubric_path, groups, _call)


# --------------------------------------------------------------------------- cli


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", required=True,
                    help="the change manifest: done-criteria and review_order")
    judge_core.add_common_args(ap, SPEC)
    args = ap.parse_args(argv)

    manifest = judge_core.read_artifact(args.manifest)
    return judge_core.run_cli(SPEC, args, references(manifest), _call)


if __name__ == "__main__":
    sys.exit(main())
