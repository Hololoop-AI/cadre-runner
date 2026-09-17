#!/usr/bin/env python3
"""Judge 2 — interrogation / revise ROUND surfaces (gate b).

Gate (b) is the broken gate: `docs/audits/2026-09-surface-quality.md` found that
round N overwrites round N−1, so "what changed since your feedback" was
unimplementable, and that driver annotations reach the agent as floating
sentences. Pack 1 fixes the plumbing (versioned round artifacts, preserved
anchors); this judge measures whether the PAGE then does its job.

The bar, from the audit + the `auto-surface` skill's iteration-round section:

    open with **What changed in round N** — one row per driver point: quote what
    they said, what you did about it, and where on the page the change lives. A
    point you pushed back on gets a row with your reasoning; silence on a driver
    point is a defect. Everything below is the full current briefing, restated —
    the driver decides off this page alone, never by diffing against memory.

That makes the DRIVER FEEDBACK LIST a first-class input, not context: "every
point has a row" is only checkable against the list of points the driver
actually made. The fixture inputs are therefore a triple — previous-round page,
driver feedback list, current page — and the group cut follows the same
reference-direction rule as judge 1.

Usage:

    judge_round.py --feedback F.json|F.md --artifact round-2.html
                   [--prev round-1.html] [--model sonnet] [--rubric PATH]
                   [--results ...] [--board board.db] [--key eval:name]

One row per run is appended to the results jsonl:

    {ts, judge, stage, story, artifact_path, artifact_sha, judge_model,
     criteria: [...], tally: {...}}
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge_core                                                  # noqa: E402
from judge_core import JudgeSpec, DEFAULT_MODEL                    # noqa: E402

run_claude = judge_core.run_claude          # patched by tests; see `_call`

# --------------------------------------------------------------------------- rubric

CRITERIA = {
    "orientation_standalone": {
        "type": "mandatory",
        "text": ("The page opens with an orientation block that names the story, "
                 "says in one sentence what it is about, and says where in the "
                 "pipeline it stands. A reader who has seen none of this work and "
                 "no chat history can answer 'which story is this and what is "
                 "being asked of me' from that block alone. A page that opens "
                 "straight into content, or whose opening only makes sense to "
                 "someone who read the previous round, is a FAIL."),
    },
    "opens_with_what_changed": {
        "type": "mandatory",
        "text": ("Before any restated briefing content, the page carries a "
                 "'what changed in round N' section, and it quotes the driver's "
                 "own words for the points it answers. A page that describes its "
                 "changes only in the agent's summary voice, with no quoted "
                 "driver point, is a FAIL."),
    },
    "every_point_has_a_row": {
        "type": "mandatory",
        "text": ("EVERY point in the DRIVER FEEDBACK list has its own row or card "
                 "on the page saying what was done about it. Silence on a driver "
                 "point is a FAIL — quote the nearest row and name the missing "
                 "point in `reason`."),
    },
    "declined_points_reasoned": {
        "type": "mandatory",
        "text": ("Every driver point that was NOT acted on carries reasoning for "
                 "the pushback on the page. A row that records a decline without "
                 "saying why ('not doing this', 'out of scope') is a FAIL. If "
                 "every point was acted on, that is a PASS — quote a row that "
                 "shows the work was done."),
    },
    "change_locations_named": {
        "type": "important",
        "text": ("Each answered point says WHERE the change lives — the section, "
                 "slice, file or contract it landed in — not merely that a change "
                 "was made."),
    },
    "changed_sections_marked": {
        "type": "important",
        "text": ("Sections touched this round are marked as changed (the page's "
                 "`changed` chips, per the RENDERED STRUCTURE inventory) while "
                 "untouched sections are left unmarked, so the driver can skip "
                 "what did not move. No marking at all on a page that reports "
                 "changes is a FAIL."),
    },
    "full_briefing_restated": {
        "type": "important",
        "text": ("The full current briefing is restated on this page — scope, "
                 "slices or proposal as they now stand — so the driver can decide "
                 "from this page alone. A page that only lists deltas and refers "
                 "the reader back to the previous round is a FAIL."),
    },
}

# One isolated call per group, cut by which reference the criterion is extracted
# against — so one call never mixes feedback→page coverage with page-alone
# readability.
GROUPS = {
    # driver feedback -> page: is every point the driver made answered here?
    "responsiveness": ("every_point_has_a_row", "declined_points_reasoned",
                       "change_locations_named"),
    # page alone, top-down: can a stranger orient and see the round's delta?
    "orientation": ("orientation_standalone", "opens_with_what_changed"),
    # previous round + page: does this page stand on its own, and does it say
    # what moved?
    "restatement": ("changed_sections_marked", "full_briefing_restated"),
}

SPEC = JudgeSpec("judge_round", "eval-judge-round", CRITERIA, GROUPS)


def _call(prompt, model, timeout=judge_core.CALL_TIMEOUT):
    """Indirection so `judge_round.run_claude` is what a test replaces."""
    return run_claude(prompt, model, timeout)


def judge(feedback: str, artifact: str, prev: str = "",
          model: str = DEFAULT_MODEL, rubric_path=None, groups=None) -> list[dict]:
    return judge_core.judge(SPEC, references(feedback, prev), artifact, model,
                            rubric_path, groups, _call)


def references(feedback: str, prev: str = "") -> list[tuple[str, str]]:
    refs = [("DRIVER FEEDBACK (the points raised on the previous round)", feedback)]
    if prev:
        refs.append(("PREVIOUS ROUND'S PAGE (what the driver last saw)", prev))
    return refs


def read_feedback(path) -> str:
    """Driver points as the surface plumbing carries them.

    JSON in (the board's annotation payload shape: `uid/selector/tag/text/prompt`
    per the audit) is rendered to a numbered list, because a judge counting
    "every point has a row" must see the points as an enumerable list and
    nothing else. Anything that is not JSON is passed through as written.
    """
    raw = Path(path).read_text()
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.strip()
    points = data.get("points", data) if isinstance(data, dict) else data
    if not isinstance(points, list):
        return raw.strip()
    lines = []
    for i, p in enumerate(points, 1):
        if not isinstance(p, dict):
            lines.append(f"{i}. {p}")
            continue
        said = p.get("prompt") or p.get("text") or ""
        anchor = p.get("selector") or p.get("uid") or ""
        lines.append(f"{i}. {said}" + (f"   [anchored on: {anchor}]" if anchor else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- cli


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feedback", required=True,
                    help="driver feedback points from the previous round: the "
                         "annotation JSON payload, or a plain list")
    ap.add_argument("--prev", default=None,
                    help="the previous round's archived page (spec-<slug>-r<N-1>.html)")
    judge_core.add_common_args(ap, SPEC)
    args = ap.parse_args(argv)

    feedback = read_feedback(args.feedback)
    prev = judge_core.read_artifact(args.prev) if args.prev else ""
    return judge_core.run_cli(SPEC, args, references(feedback, prev), _call)


if __name__ == "__main__":
    sys.exit(main())
