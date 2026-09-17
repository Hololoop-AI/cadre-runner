#!/usr/bin/env python3
"""Shared machinery for the surface judges (`judge_round`, `judge_risk`,
`judge_final`).

`judge_spec.py` is judge 1 and stays as it is — the first judge written, and the
one whose live behaviour is already calibrated. This module is that script's
architecture lifted out so judges 2-4 cannot drift from it:

  * **Per-criterion binary verdicts** — PASS / FAIL / UNKNOWN, never a score.
  * **One isolated call per criterion GROUP**, groups cut by which reference the
    criterion is extracted against, so a single call never holds two opposite
    extraction jobs.
  * **A verbatim quote is required for every PASS**, and the quote is CHECKED in
    Python (`verify_quote`) against the graded text. A judge that invents
    supporting text is downgraded to UNKNOWN rather than believed.
  * **UNKNOWN counts as FAIL at the gate and is tracked separately** — an honest
    abstention must not read as a pass.
  * **One retry, only for an unparseable reply.** A reply that parsed is never
    re-asked; that would be shopping for a verdict.
  * **The judge model is pinned on the command line, default sonnet** — eval
    sessions never inherit the invoking terminal's model.
  * Rubric first, inputs last: a byte-identical cache prefix, and no clock, run
    id or artifact name anywhere in the prompt.

What is new here, and why. Judges 2-4 grade PAGE STRUCTURE (a diff shown as a
diff, a `changed` chip, an embedded screenshot), and structure lives in markup
that `artifact_text()` deliberately throws away. So the graded document is the
page text PLUS a deterministic, Python-computed appendix describing that markup
(`structure_digest`). The judge never inspects HTML: it reads prose and a small
factual inventory, both of which its quotes are verified against.

Docs the criteria are drawn from: `docs/audits/2026-09-surface-quality.md`
(the per-gate bar) and the `auto-surface` skill (the page-structure contract).
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
RESULTS_DIR = HERE / "results"

DEFAULT_MODEL = "sonnet"          # pinned; never inherited from the session
CALL_TIMEOUT = 300
MIN_QUOTE_CHARS = 12              # a two-word "quote" verifies against anything

PREAMBLE = """\
You are grading ONE artifact against supplied references, criterion by criterion.

This is an extraction-and-comparison task. You are never being asked whether the
artifact is "good", well written, or thorough.

The artifact is a driver review surface: a decision page written for a reader
with zero shared context. Part of what it must do is structural, so the artifact
text below ends with a RENDERED STRUCTURE inventory, computed mechanically from
the page markup. It is part of the artifact for grading and for quoting.

Rules, all of them binding:

1. Return exactly one verdict per criterion you are given: PASS, FAIL or
   UNKNOWN. Nothing in between, no scores.
2. Every PASS must carry `evidence_quote`: a span copied VERBATIM, character
   for character, from THE ARTIFACT below (not from the references, not
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

# The last thing the model reads. Observed failure on judge 1's first live run:
# a rubric-first prompt read as a description of a judging task got a
# description of a judging RESULT back — prose, no JSON. Compliance is won at
# the end of the prompt.
TAIL = """\
Output the JSON object now. Grade the ARTIFACT above; do not describe this task,
do not summarise, do not add a sentence before or after the JSON. Escape any
double quote inside a string as \\" so the object parses."""

REPAIR = ("Your previous reply was not parseable JSON. Reply with the JSON "
          "object alone.")


class JudgeSpec:
    """One judge as data: its criteria, its group cut, and the labels it writes.

    `stage` is what lands in the board signal and the jsonl, so an eval workflow
    can route on it without knowing which script ran.
    """

    def __init__(self, name: str, stage: str, criteria: dict, groups: dict,
                 preamble: str = PREAMBLE):
        self.name, self.stage = name, stage
        self.criteria, self.groups, self.preamble = criteria, groups, preamble
        grouped = [cid for ids in groups.values() for cid in ids]
        # a criterion in no group is never asked; a criterion in two is asked
        # twice and the second answer silently wins. Both are authoring bugs.
        assert sorted(grouped) == sorted(criteria), \
            f"{name}: every criterion must appear in exactly one group"

    def rubric_text(self, rubric_path=None) -> str:
        """`--rubric` swaps the preamble for a registry-held version so a node's
        versioned prompt is what actually runs; criteria stay here as data."""
        return Path(rubric_path).read_text() if rubric_path else self.preamble


# --------------------------------------------------------------------------- prompt


def build_prompt(spec: JudgeSpec, group: str, references: list[tuple[str, str]],
                 artifact: str, rubric_path=None, repair: bool = False) -> str:
    """Rubric first, inputs last — a stable cache prefix and nothing that moves
    between runs except the documents."""
    lines = [spec.rubric_text(rubric_path), "", "CRITERIA FOR THIS CALL", ""]
    for cid in spec.groups[group]:
        c = spec.criteria[cid]
        lines.append(f"- id `{cid}` ({c['type']}): {c['text']}")
    for label, text in references:
        lines += ["", "=" * 60, f"{label} (a reference)", "=" * 60, "", text]
    lines += ["", "=" * 60, "ARTIFACT (the thing being graded)", "=" * 60, "",
              artifact, "", TAIL]
    if repair:
        lines.append(REPAIR)
    return "\n".join(lines)


# --------------------------------------------------------------------------- the call


def run_claude(prompt: str, model: str, timeout: int = CALL_TIMEOUT) -> str:
    """The ONE place these scripts talk to an agent. Each judge module re-exports
    this name and every test monkeypatches it there, so no test path can reach
    the network by accident."""
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
    """Pull the criteria list out of the reply, fenced or bare. A reply we cannot
    parse is not an error to swallow — the caller turns it into UNKNOWNs so a
    broken judge reads as abstention, never as a pass."""
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


def settle(spec: JudgeSpec, row: dict, criterion_id: str, artifact: str) -> dict:
    """One judged criterion, after the anti-hallucination check.

    A PASS whose evidence is not in the artifact becomes UNKNOWN. Not FAIL: the
    artifact may well satisfy the criterion — what failed is the judge's ability
    to show it, and that is an abstention, not a defect in the work.
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
            "type": spec.criteria[criterion_id]["type"],
            "verdict": verdict,
            "evidence_quote": quote,
            "reason": " ".join(x for x in (reason, note) if x)}


def abstained(spec: JudgeSpec, criterion_id: str, why: str) -> dict:
    return {"criterion_id": criterion_id,
            "type": spec.criteria[criterion_id]["type"],
            "verdict": "UNKNOWN", "evidence_quote": "", "reason": why}


def judge(spec: JudgeSpec, references: list[tuple[str, str]], artifact: str,
          model: str = DEFAULT_MODEL, rubric_path=None, groups=None,
          call=run_claude) -> list[dict]:
    """Run every criterion group and return the settled criteria in rubric order.

    One call per group; a group that fails to answer abstains rather than taking
    the rest of the run down with it. `call` is the agent call — each judge
    module passes a thunk onto its own module-level `run_claude` so patching it
    there is enough to keep a test offline.
    """
    results = {}
    for group in (groups or spec.groups):
        ids = spec.groups[group]
        rows, why = None, ""
        # One retry, and only for a reply we could not parse: an unparseable
        # reply is a formatting miss, not a verdict, and abstaining on it would
        # inflate the UNKNOWN rate with noise that says nothing about the
        # artifact. A parsed reply is never re-asked.
        for attempt in (False, True):
            try:
                rows = parse_reply(call(
                    build_prompt(spec, group, references, artifact, rubric_path,
                                 attempt), model))
                break
            except Exception as e:              # noqa: BLE001 — reported, not raised
                why = (f"judge call for group {group!r} failed: "
                       f"{type(e).__name__}: {e}")
        if rows is None:
            results.update({cid: abstained(spec, cid, why) for cid in ids})
            continue
        by_id = {str(r.get("criterion_id")): r for r in rows if isinstance(r, dict)}
        for cid in ids:
            row = by_id.get(cid)
            results[cid] = (settle(spec, row, cid, artifact) if row else
                            abstained(spec, cid, f"group {group!r} returned no verdict"))
    return [results[cid] for cid in spec.criteria if cid in results]


# --------------------------------------------------------------------------- reporting


def tally(criteria: list[dict]) -> dict:
    counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
    for c in criteria:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    mandatory_fail = [c["criterion_id"] for c in criteria
                      if c["type"] == "mandatory" and c["verdict"] != "PASS"]
    return {"total": len(criteria), **counts,
            "mandatory_failures": mandatory_fail,
            # UNKNOWN counts as FAIL for the gate; the count keeps it visible.
            "passed": not mandatory_fail}


def summary_line(t: dict) -> str:
    return (f"{t['PASS']}/{t['total']} PASS · {t['FAIL']} FAIL · "
            f"{t['UNKNOWN']} UNKNOWN (counted as FAIL) · "
            f"{'ACCEPT' if t['passed'] else 'REJECT'}"
            + (f" · mandatory failures: {', '.join(t['mandatory_failures'])}"
               if t["mandatory_failures"] else ""))


# --------------------------------------------------------------------------- inputs


class _Text(HTMLParser):
    """What a reader of the page would actually read, plus a note of the markup
    that carries meaning the text cannot (diff hunks, chips, collapsed details,
    embedded media). Comments never reach either — they are not part of the
    rendered page, which is also what keeps the seeded-corruption fixtures
    honest: their ground-truth markers live in comments."""

    MEDIA = {"img": "image", "video": "video", "iframe": "embed"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0
        self.diffs, self.chips, self.media = [], [], []
        self.details = 0
        self._stack = []                      # open elements we are recording

    @staticmethod
    def _classes(attrs) -> set:
        return set((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag, attrs):
        cls = self._classes(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if tag in self.MEDIA:
            d = dict(attrs)
            self.media.append({"kind": self.MEDIA[tag],
                               "src": d.get("src", ""), "alt": d.get("alt", "")})
        if tag == "details":
            self.details += 1
        if tag == "pre" and "diff" in cls:
            self.diffs.append({"caption": self._last_caption(), "add": 0, "del": 0})
            self._stack.append("diff")
        elif tag == "span" and self.diffs and self._stack[-1:] == ["diff"]:
            for k in ("add", "del"):
                if k in cls:
                    self.diffs[-1][k] += 1
        elif "chip" in cls:
            self.chips.append(len(self.parts))          # resolved on close
            self._stack.append("chip")
        elif "diff-caption" in cls:
            self._stack.append("caption")
            self._caption_at = len(self.parts)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if self._stack:
            if self._stack[-1] == "diff" and tag == "pre":
                self._stack.pop()
            elif self._stack[-1] == "chip" and tag in ("span", "div", "b", "em"):
                i = self.chips.pop()
                self.chips.append(normalize("".join(self.parts[i:])))
                self._stack.pop()
            elif self._stack[-1] == "caption":
                self._stack.pop()

    def _last_caption(self) -> str:
        """`.diff-caption` renders immediately before its `<pre class="diff">`.
        Consumed on use, so an uncaptioned hunk reports no caption rather than
        inheriting the previous hunk's."""
        at, self._caption_at = getattr(self, "_caption_at", None), None
        return normalize("".join(self.parts[at:])) if at is not None else ""

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()

    def digest(self) -> str:
        """A factual inventory of the page's structure, for criteria about how
        the page SHOWS things. Mechanically derived, so the judge is reading a
        measurement rather than being asked to imagine the rendering."""
        lines = ["=== RENDERED STRUCTURE (extracted mechanically from the page "
                 "markup; not authored prose) ==="]
        lines.append(f"diff blocks (pre.diff): {len(self.diffs)}")
        for i, d in enumerate(self.diffs, 1):
            lines.append(f"  diff {i}: caption {d['caption']!r} — "
                         f"{d['add']} added lines, {d['del']} removed lines")
        chips = [c for c in self.chips if isinstance(c, str)]
        lines.append(f"chips (span.chip): {len(chips)}"
                     + (f" — {', '.join(repr(c) for c in chips)}" if chips else ""))
        lines.append(f"collapsed sections (details): {self.details}")
        lines.append(f"embedded media: {len(self.media)}"
                     + ("".join(f"\n  {m['kind']}: src={m['src']!r} "
                                f"alt={m['alt']!r}" for m in self.media)))
        return "\n".join(lines)


def read_artifact(path, with_structure: bool = True) -> str:
    """The graded document: page text, plus the structure appendix when the page
    is HTML. Markdown artifacts get their HTML comments stripped for the same
    reason `_Text` drops them — they are not part of the rendered briefing."""
    raw = Path(path).read_text()
    if Path(path).suffix.lower() in (".html", ".htm"):
        p = _Text()
        p.feed(raw)
        return p.text() + ("\n\n" + p.digest() if with_structure else "")
    return re.sub(r"\n{3,}", "\n\n",
                  re.sub(r"<!--.*?-->", "", raw, flags=re.S)).strip()


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
    a judge is a plain script and must run with no board at all."""
    sys.path.insert(0, str(ROOT))
    from runnerlib.blackboard import Board

    t = record["tally"]
    with Board(board_path) as b:
        return b.write("cadre", "evals", key, "signal",
                       {"status": "finished", "stage": record["stage"],
                        "story": record["story"],
                        "artifact": record["artifact_path"],
                        "summary": summary,
                        "results": str(record.get("results_path", "")),
                        "passed": t["passed"], "pass_count": t["PASS"],
                        "fail_count": t["FAIL"], "unknown_count": t["UNKNOWN"]})


# --------------------------------------------------------------------------- cli


def add_common_args(ap: argparse.ArgumentParser, spec: JudgeSpec,
                    groups_choices=True):
    """Every judge takes the same shape as `judge_spec.py`: the artifact in, a
    pinned model, a jsonl out, an optional board signal."""
    ap.add_argument("--artifact", required=True,
                    help="the surface HTML (or markdown) being graded")
    ap.add_argument("--name", default=None,
                    help="the story/fixture NAME to record (default: the "
                         "artifact file's stem)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="judge model — ALWAYS pinned, never inherited "
                         f"(default {DEFAULT_MODEL})")
    ap.add_argument("--rubric", default=None,
                    help="judging preamble to use instead of the built-in one "
                         "(the node registry passes its versioned prompt here)")
    ap.add_argument("--results", default=str(RESULTS_DIR / "judged.jsonl"))
    ap.add_argument("--board", default=None,
                    help=f"board.db to write `signal finished stage={spec.stage}` to")
    ap.add_argument("--key", default=None,
                    help="board key for that signal (default eval:<name>)")
    if groups_choices:
        ap.add_argument("--group", action="append", choices=sorted(spec.groups),
                        help="run only these criterion groups (debugging)")
    return ap


def run_cli(spec: JudgeSpec, args, references: list[tuple[str, str]],
            call=run_claude) -> int:
    """Grade, record, print, signal. Identical across judges on purpose: the
    jsonl is one table, so every row has to have one shape."""
    art_path = Path(args.artifact)
    artifact = read_artifact(art_path)
    name = args.name or art_path.stem

    criteria = judge(spec, references, artifact, args.model, args.rubric,
                     getattr(args, "group", None), call)
    t = tally(criteria)
    record = {"ts": time.time(), "judge": spec.name, "stage": spec.stage,
              "story": name, "artifact_path": str(art_path),
              "artifact_sha": sha256_of(art_path), "judge_model": args.model,
              "criteria": criteria, "tally": t}
    record["results_path"] = str(append_result(record, args.results))

    line = summary_line(t)
    for c in criteria:
        print(f"{c['verdict']:8} {c['criterion_id']:28} {c['reason'][:90]}")
    print("\n" + line)

    if args.board:
        write_board_signal(args.board, args.key or f"eval:{name}", record, line)
    return 0 if t["passed"] else 1
