#!/usr/bin/env python3
"""Judge 1 — spec-briefing groundedness & sufficiency, as a plain script.

The prototype scope says judges are "plain ad-hoc scripts (results in jsonl)"
(`evals/PLAN-2026-09.md`, scope re-cut 2026-09-08). No backend, no Langfuse, no
new component: one script, one jsonl file, and a board event when asked for one.

Rubric: `evals/PLAN-2026-09.md` §2 / `~/research/agent-pipeline-evals-2026-09.md`
§5.3, Judge 1 — seven binary criteria, four mandatory, three important.

The design rules this file exists to enforce, all from §5.3:

  * **Per-criterion binary verdicts.** PASS / FAIL / UNKNOWN, never a score.
  * **Isolated call per criterion GROUP.** "Grade each dimension with an
    isolated LLM-as-judge rather than using one to grade all dimensions."
    Groups here are cut by which reference the criterion is extracted
    against, so one call never mixes story→artifact coverage with
    artifact→story grounding.
  * **A verbatim quote is required for every PASS** — and the quote is
    CHECKED against the artifact here, in Python (`verify_quote`). That check
    is the anti-hallucination teeth: a judge that invents supporting text is
    downgraded to UNKNOWN rather than believed. Without it "quote required"
    is a request, not a constraint.
  * **UNKNOWN counts as FAIL for the gate, and is tracked separately** — an
    honest abstention must not read as a pass, and a judge that abstains a lot
    is a judge that needs rewriting (κ / UNKNOWN-rate, §3 of the plan).
  * **The judge model is pinned on the command line, default sonnet.** Eval
    sessions never inherit the invoking terminal's model (`evals/README.md`),
    and the judge tier is deliberately not the generator tier.

Prompt shape: the rubric comes FIRST and is byte-identical across artifacts, so
the long prefix is cache-friendly and nothing varies run to run except the two
inputs. There is no clock, no run id and no artifact name inside the prompt —
temperature stability starts with not moving the prompt.

Usage:

    judge_spec.py --story S.md --artifact A.md [--model sonnet]
                  [--rubric PATH] [--board board.db] [--key eval:name]
                  [--results evals/results/judged.jsonl]

One row is appended to the results jsonl per run:

    {ts, story, artifact_path, artifact_sha, judge_model, criteria: [...]}

`--board` additionally writes `signal finished stage=eval-judge` so the eval
workflow (`config/actions-eval.json`) can carry on without this script knowing
anything about what comes next.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_RESULTS = HERE / "results" / "judged.jsonl"

DEFAULT_MODEL = "sonnet"          # pinned; never inherited from the session
CALL_TIMEOUT = 300
MIN_QUOTE_CHARS = 12              # a two-word "quote" verifies against anything

# --------------------------------------------------------------------------- rubric

# Judge 1, research report §5.3. `type` is the RaR typing: a mandatory FAIL
# fails the artifact; important criteria are aggregated and reported.
CRITERIA = {
    "constraint_coverage": {
        "type": "mandatory",
        "text": ("Every constraint stated in the story appears in the artifact, "
                 "or is explicitly listed there as out of scope."),
    },
    "no_invention": {
        "type": "mandatory",
        "text": ("The artifact contains no capability, file path, module or "
                 "constraint claim that is absent from the story. A file or "
                 "component named in the artifact but nowhere in the story is a "
                 "FAIL, and the quote to cite is the invented reference."),
    },
    "concrete_validation_paths": {
        "type": "mandatory",
        "text": ("Each slice's validation path names a concrete observable "
                 "outcome — a command run, an output compared, a state checked. "
                 "A validation path that restates the slice's own title in other "
                 "words is a FAIL."),
    },
    "question_legitimacy": {
        "type": "mandatory",
        "text": ("Every open question the artifact poses to the driver is one "
                 "the story genuinely does not answer. A question whose answer "
                 "is written in the story is a FAIL. If the artifact poses no "
                 "questions, that is a PASS — quote the section that shows it."),
    },
    "stated_non_goals": {
        "type": "important",
        "text": ("The artifact states what will NOT be built, in terms a reader "
                 "can check against the story's non-goals."),
    },
    "standalone_restatable": {
        "type": "important",
        "text": ("A reader who has not seen the story could restate the intended "
                 "end state from the artifact alone: the artifact names the "
                 "behaviour being added and the outcome it produces, without "
                 "requiring the story text."),
    },
    "risks_tied_to_slices": {
        "type": "important",
        "text": ("Named risks or unknowns are tied to a specific slice, file or "
                 "change — not generic ('there may be edge cases', 'testing "
                 "could be tricky')."),
    },
}

# One isolated call per group. The cut is by REFERENCE DIRECTION, so a single
# call never has to hold two opposite extraction jobs at once.
GROUPS = {
    # story -> artifact: is everything the story said still present?
    "coverage": ("constraint_coverage", "stated_non_goals"),
    # artifact -> story: is everything the artifact says actually sourced?
    "grounding": ("no_invention", "question_legitimacy"),
    # artifact alone: is it usable as written?
    "sufficiency": ("concrete_validation_paths", "risks_tied_to_slices",
                    "standalone_restatable"),
}

PREAMBLE = """\
You are grading ONE artifact against ONE story, criterion by criterion.

This is an extraction-and-comparison task against supplied references. You are
never being asked whether the artifact is "good", well written, or thorough.

Rules, all of them binding:

1. Return exactly one verdict per criterion you are given: PASS, FAIL or
   UNKNOWN. Nothing in between, no scores.
2. Every PASS must carry `evidence_quote`: a span copied VERBATIM, character
   for character, from THE ARTIFACT below (not from the story, not
   paraphrased). Quotes are checked programmatically against the artifact
   text; a quote that does not appear there is discarded and your verdict is
   replaced with UNKNOWN. Quote at least a full clause.
3. FAIL when the artifact demonstrably violates the criterion. For a FAIL,
   quote the offending span if there is one; quote "" if the problem is an
   absence.
4. UNKNOWN only when the supplied material genuinely cannot settle the
   question. UNKNOWN is not a polite FAIL and not a polite PASS; it is counted
   as a failure at the gate and tracked separately as an abstention.
5. `reason` is one sentence. No preamble, no summary, no recommendations.

Reply with a single JSON object and nothing else, in exactly this shape:

{"criteria": [{"criterion_id": "<id>", "verdict": "PASS|FAIL|UNKNOWN",
               "evidence_quote": "<verbatim span from the artifact, or \\"\\">",
               "reason": "<one sentence>"}]}
"""


def rubric_text(rubric_path=None) -> str:
    """The judging preamble. `--rubric` swaps it for a registry-held version so
    the node's versioned prompt is what actually runs (`runnerlib/nodes.py`
    keeps the lineage); the criteria themselves stay here as data."""
    if rubric_path:
        return Path(rubric_path).read_text()
    return PREAMBLE


# The last thing the model reads. Observed failure on the first live run: a
# rubric-first prompt read as a description of a judging task got a description
# of a judging RESULT back — prose, no JSON. Compliance is won at the end of
# the prompt, so the imperative lives here rather than only in the preamble.
TAIL = """\
Output the JSON object now. Grade the ARTIFACT above; do not describe this task,
do not summarise, do not add a sentence before or after the JSON. Escape any
double quote inside a string as \\" so the object parses."""


def build_prompt(group: str, story: str, artifact: str, rubric_path=None,
                 repair: bool = False) -> str:
    """Rubric first, inputs last — a stable cache prefix and nothing that moves
    between runs except the two documents."""
    lines = [rubric_text(rubric_path), "", "CRITERIA FOR THIS CALL", ""]
    for cid in GROUPS[group]:
        c = CRITERIA[cid]
        lines.append(f"- id `{cid}` ({c['type']}): {c['text']}")
    lines += ["", "=" * 60, "STORY (the reference)", "=" * 60, "", story,
              "", "=" * 60, "ARTIFACT (the thing being graded)", "=" * 60, "",
              artifact, "", TAIL]
    if repair:
        lines.append("Your previous reply was not parseable JSON. Reply with the "
                     "JSON object alone.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- the call


def run_claude(prompt: str, model: str, timeout: int = CALL_TIMEOUT) -> str:
    """The ONE place this script talks to an agent. Every test monkeypatches
    this function, so no test path can reach the network by accident.

    Text output, not `--output-format json`: the judge's whole reply is the
    JSON object we want, and one less wrapper is one less thing to unwrap.
    """
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", model,
         "--output-format", "text", "--dangerously-skip-permissions"],
        text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[-500:]}")
    return proc.stdout


def parse_reply(reply: str) -> list[dict]:
    """Pull the criteria list out of the reply, fenced or bare. A reply we
    cannot parse is not an error to swallow — the caller turns it into UNKNOWNs
    so a broken judge reads as abstention, never as a pass."""
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in the reply")
    data = json.loads(text[start:end + 1])
    rows = data.get("criteria")
    if not isinstance(rows, list):
        raise ValueError("reply has no `criteria` list")
    return rows


# --------------------------------------------------------------------------- verdicts


def normalize(text: str) -> str:
    """Whitespace-insensitive comparison. Line wrapping is not a quote edit;
    changing a word is. Nothing else is normalized — no case folding, no
    punctuation stripping — because those ARE quote edits."""
    return re.sub(r"\s+", " ", text or "").strip()


def verify_quote(quote: str, artifact: str) -> bool:
    q = normalize(quote)
    return len(q) >= MIN_QUOTE_CHARS and q in normalize(artifact)


def settle(row: dict, criterion_id: str, artifact: str) -> dict:
    """One judged criterion, after the anti-hallucination check.

    A PASS whose evidence is not in the artifact becomes UNKNOWN. Not FAIL:
    the artifact may well satisfy the criterion — what failed is the judge's
    ability to show it, and that is an abstention, not a defect in the work.
    """
    verdict = str(row.get("verdict", "")).upper()
    if verdict not in ("PASS", "FAIL", "UNKNOWN"):
        verdict, note = "UNKNOWN", f"unparseable verdict {row.get('verdict')!r}"
    else:
        note = ""
    quote = row.get("evidence_quote") or ""
    if verdict == "PASS" and not verify_quote(quote, artifact):
        verdict = "UNKNOWN"
        note = ("PASS downgraded: evidence quote does not appear verbatim in "
                "the artifact")
    reason = str(row.get("reason") or "").strip()
    return {"criterion_id": criterion_id,
            "type": CRITERIA[criterion_id]["type"],
            "verdict": verdict,
            "evidence_quote": quote,
            "reason": " ".join(x for x in (reason, note) if x)}


def abstained(criterion_id: str, why: str) -> dict:
    return {"criterion_id": criterion_id, "type": CRITERIA[criterion_id]["type"],
            "verdict": "UNKNOWN", "evidence_quote": "", "reason": why}


def judge(story: str, artifact: str, model: str = DEFAULT_MODEL,
          rubric_path=None, groups=None) -> list[dict]:
    """Run every criterion group and return the seven settled criteria, in
    rubric order. One call per group; a group that fails to answer abstains
    rather than taking the rest of the run down with it."""
    results = {}
    for group in (groups or GROUPS):
        ids = GROUPS[group]
        rows, why = None, ""
        # One retry, and only for a reply we could not parse: an unparseable
        # reply is a formatting miss, not a verdict, and abstaining on it would
        # inflate the UNKNOWN rate with noise that says nothing about the
        # artifact. A parsed reply is never re-asked — that would be shopping
        # for a verdict.
        for attempt in (False, True):
            try:
                rows = parse_reply(run_claude(
                    build_prompt(group, story, artifact, rubric_path, attempt),
                    model))
                break
            except Exception as e:                  # noqa: BLE001 — reported, not raised
                why = (f"judge call for group {group!r} failed: "
                       f"{type(e).__name__}: {e}")
        if rows is None:
            results.update({cid: abstained(cid, why) for cid in ids})
            continue
        by_id = {str(r.get("criterion_id")): r for r in rows if isinstance(r, dict)}
        for cid in ids:
            row = by_id.get(cid)
            results[cid] = (settle(row, cid, artifact) if row else
                            abstained(cid, f"group {group!r} returned no verdict"))
    return [results[cid] for cid in CRITERIA if cid in results]


# --------------------------------------------------------------------------- reporting


def tally(criteria: list[dict]) -> dict:
    counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
    for c in criteria:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    mandatory_fail = [c["criterion_id"] for c in criteria
                      if c["type"] == "mandatory" and c["verdict"] != "PASS"]
    return {"total": len(criteria), **counts,
            "mandatory_failures": mandatory_fail,
            # UNKNOWN counts as FAIL for the gate; the count above keeps it visible.
            "passed": not mandatory_fail}


def summary_line(t: dict) -> str:
    return (f"{t['PASS']}/{t['total']} PASS · {t['FAIL']} FAIL · "
            f"{t['UNKNOWN']} UNKNOWN (counted as FAIL) · "
            f"{'ACCEPT' if t['passed'] else 'REJECT'}"
            + (f" · mandatory failures: {', '.join(t['mandatory_failures'])}"
               if t["mandatory_failures"] else ""))


# --------------------------------------------------------------------------- inputs


class _Text(HTMLParser):
    """Spec surfaces are HTML; the judge grades their text content. Kept to a
    dozen lines on purpose — this is not a rendering engine."""

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def artifact_text(path) -> str:
    """What a reader of the artifact would actually read.

    HTML comments are stripped from markdown for the same reason `_Text` drops
    them from HTML: they are not part of the rendered briefing, so grading them
    would be grading something no driver ever sees. (It is also what keeps the
    seeded-corruption fixtures honest — their ground-truth markers live in
    comments, and a judge that could read them would be grading the marker.)
    """
    raw = Path(path).read_text()
    if Path(path).suffix.lower() in (".html", ".htm"):
        p = _Text()
        p.feed(raw)
        return re.sub(r"\n{3,}", "\n\n", "".join(p.parts)).strip()
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"<!--.*?-->", "", raw, flags=re.S)).strip()


def sha256_of(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------- output


def append_result(record: dict, results_path) -> Path:
    path = Path(results_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return path


def write_board_signal(board_path, key: str, record: dict, summary: str):
    """The workflow's next action reads this and nothing else. Imported lazily:
    the judge is a plain script and must run with no board at all."""
    sys.path.insert(0, str(ROOT))
    from runnerlib.blackboard import Board

    t = record["tally"]
    with Board(board_path) as b:
        return b.write("cadre", "evals", key, "signal",
                       {"status": "finished", "stage": "eval-judge",
                        "story": record["story"],
                        "artifact": record["artifact_path"],
                        "summary": summary,
                        "results": str(record.get("results_path", "")),
                        "passed": t["passed"], "pass_count": t["PASS"],
                        "fail_count": t["FAIL"], "unknown_count": t["UNKNOWN"]})


# --------------------------------------------------------------------------- cli


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--story", required=True, help="story text file")
    ap.add_argument("--name", default=None,
                    help="the story/fixture NAME to record (default: the story "
                         "file's stem, which is often just 'story')")
    ap.add_argument("--artifact", required=True,
                    help="briefing markdown, or a spec-surface HTML file")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="judge model — ALWAYS pinned, never inherited "
                         f"(default {DEFAULT_MODEL})")
    ap.add_argument("--rubric", default=None,
                    help="judging preamble to use instead of the built-in one "
                         "(the node registry passes its versioned prompt here)")
    ap.add_argument("--results", default=str(DEFAULT_RESULTS))
    ap.add_argument("--board", default=None,
                    help="board.db to write `signal finished stage=eval-judge` to")
    ap.add_argument("--key", default=None,
                    help="board key for that signal (default eval:<story stem>)")
    ap.add_argument("--group", action="append", choices=sorted(GROUPS),
                    help="run only these criterion groups (debugging)")
    args = ap.parse_args(argv)

    story_path, art_path = Path(args.story), Path(args.artifact)
    story = story_path.read_text()
    artifact = artifact_text(art_path)
    story_name = args.name or story_path.stem

    criteria = judge(story, artifact, args.model, args.rubric, args.group)
    t = tally(criteria)
    record = {"ts": time.time(), "story": story_name,
              "artifact_path": str(art_path), "artifact_sha": sha256_of(art_path),
              "judge_model": args.model, "criteria": criteria, "tally": t}
    record["results_path"] = str(append_result(record, args.results))

    line = summary_line(t)
    for c in criteria:
        print(f"{c['verdict']:8} {c['criterion_id']:26} {c['reason'][:90]}")
    print("\n" + line)

    if args.board:
        write_board_signal(args.board, args.key or f"eval:{story_name}", record, line)
    return 0 if t["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
