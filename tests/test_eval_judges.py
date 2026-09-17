"""The three surface judges (gates b, c, d), deterministic parts only.

`judge_round`, `judge_risk` and `judge_final` each spend real inference, so what
runs in the suite is everything that is NOT the model: the artifact parser and
its structure digest, the criteria/group wiring, quote verification and the
UNKNOWN downgrade, the retry-once-only policy, the results jsonl shape, and the
seeded-corruption fixtures' ground truth. The single agent call in each judge
goes through that module's `run_claude`, which every test here replaces — no
test path can reach the network.

This mirrors `test_eval_workflow.py`, which covers judge 1 the same way.

Run directly: python3 tests/test_eval_judges.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

import judge_core                                                  # noqa: E402
import judge_final                                                 # noqa: E402
import judge_risk                                                  # noqa: E402
import judge_round                                                 # noqa: E402
from runnerlib.blackboard import Board                             # noqa: E402

FIX = ROOT / "evals" / "fixtures"
ROUND, RISK, FINAL = (FIX / "judge-round-sample", FIX / "judge-risk-sample",
                      FIX / "judge-final-sample")

# (module, good artifact, corrupt artifact, argv for the references it needs)
JUDGES = [
    (judge_round, ROUND / "round-2-good.html", ROUND / "round-2-corrupt.html",
     ["--feedback", str(ROUND / "feedback.json"),
      "--prev", str(ROUND / "round-1.html")]),
    (judge_risk, RISK / "risk-hold-good.html", RISK / "risk-hold-corrupt.html",
     ["--risk", str(RISK / "risk.md")]),
    (judge_final, FINAL / "final-good.html", FINAL / "final-corrupt.html",
     ["--manifest", str(FINAL / "manifest.md")]),
]


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


def has(text: str, phrase: str) -> bool:
    """Substring, whitespace-insensitively — the fixtures are wrapped prose and
    a line break is not a difference in what the page says."""
    return judge_core.normalize(phrase) in judge_core.normalize(text)


class fake_claude:
    """Replace one judge module's `run_claude` for the duration of a block.
    Every reply is a canned string; nothing here can reach the network."""

    def __init__(self, mod, replies):
        self.mod, self.replies, self.calls = mod, replies, []

    def __call__(self, prompt, model, timeout=None):
        self.calls.append({"prompt": prompt, "model": model})
        r = self.replies
        return r(prompt) if callable(r) else r

    def __enter__(self):
        self.real = self.mod.run_claude
        self.mod.run_claude = self
        return self

    def __exit__(self, *exc):
        self.mod.run_claude = self.real


def reply_for(mod, prompt, verdict="PASS", quote=None) -> str:
    """A well-formed reply for whichever criteria this prompt asked about,
    quoting real artifact text so verification passes."""
    ids = [cid for cid in mod.CRITERIA if f"id `{cid}`" in prompt]
    rows = [{"criterion_id": cid, "verdict": verdict,
             "evidence_quote": quote if quote is not None
             else "Story tag-notes", "reason": "canned"} for cid in ids]
    return json.dumps({"criteria": rows})


def refs_for(mod, argv) -> list:
    """The references a judge builds from its CLI inputs, without running it."""
    if mod is judge_round:
        return mod.references(mod.read_feedback(argv[1]),
                              judge_core.read_artifact(argv[3]))
    return mod.references(judge_core.read_artifact(argv[1]))


# --------------------------------------------------------------------------- wiring


def test_every_judge_is_a_well_formed_rubric():
    """Binary criteria in two tiers, and a group cut that is a partition — a
    criterion in no group is never asked; one in two groups is asked twice and
    the second answer silently wins."""
    seen_stages = set()
    for mod, *_ in JUDGES:
        spec = mod.SPEC
        assert 7 <= len(spec.criteria) <= 8
        grouped = [cid for ids in spec.groups.values() for cid in ids]
        assert sorted(grouped) == sorted(spec.criteria)
        assert len(grouped) == len(set(grouped))
        tiers = {c["type"] for c in spec.criteria.values()}
        assert tiers <= {"mandatory", "important"}
        assert "mandatory" in tiers and "important" in tiers
        for c in spec.criteria.values():
            assert c["text"].strip() and len(c["text"]) > 60   # a rule, not a label
        assert spec.stage not in seen_stages                   # routable on the board
        seen_stages.add(spec.stage)
        assert spec.name == mod.__name__
    # and they are distinct judges, not one rubric copied three times
    all_ids = [cid for mod, *_ in JUDGES for cid in mod.CRITERIA]
    assert len(set(all_ids)) >= len(all_ids) - 1     # only `orientation_standalone` repeats


def test_a_criterion_outside_its_groups_is_an_authoring_error():
    try:
        judge_core.JudgeSpec("x", "s", {"a": {"type": "mandatory", "text": "t"}},
                             {"g": ("a", "b")})
        raise AssertionError("an ungrouped/unknown criterion must not build")
    except AssertionError as e:
        assert "exactly one group" in str(e)


def test_one_isolated_call_per_criterion_group():
    for mod, good, _corrupt, argv in JUDGES:
        artifact = judge_core.read_artifact(good)
        with fake_claude(mod, lambda p: reply_for(mod, p)) as fake:
            judge_core.judge(mod.SPEC, refs_for(mod, argv), artifact, "haiku",
                             call=mod._call)
        assert len(fake.calls) == len(mod.GROUPS) == 3, mod.__name__
        assert {c["model"] for c in fake.calls} == {"haiku"}     # the pin travels
        # rubric first, inputs last: a byte-identical prefix on every call
        assert {c["prompt"][:len(judge_core.PREAMBLE)] for c in fake.calls} == \
            {judge_core.PREAMBLE}
        # each criterion asked exactly once, all seven covered
        asked = [cid for c in fake.calls for cid in mod.CRITERIA
                 if f"id `{cid}`" in c["prompt"]]
        assert sorted(asked) == sorted(mod.CRITERIA), mod.__name__
        # the artifact is the LAST document in the prompt, after the references
        for c in fake.calls:
            assert c["prompt"].index("ARTIFACT (the thing being graded)") > \
                max(c["prompt"].index(label) for label, _ in refs_for(mod, argv))


def test_each_judge_carries_its_own_references_into_the_prompt():
    for mod, good, _corrupt, argv in JUDGES:
        refs = refs_for(mod, argv)
        prompt = judge_core.build_prompt(mod.SPEC, sorted(mod.GROUPS)[0], refs,
                                         judge_core.read_artifact(good))
        for label, text in refs:
            assert label in prompt and text[:80] in prompt
    # the round judge's references are the pair that makes "every point has a
    # row" and "changed sections marked" checkable at all
    labels = [l for l, _ in refs_for(judge_round, JUDGES[0][3])]
    assert any("DRIVER FEEDBACK" in l for l in labels)
    assert any("PREVIOUS ROUND" in l for l in labels)
    assert "RISK RECORD" in refs_for(judge_risk, JUDGES[1][3])[0][0]
    assert "CHANGE MANIFEST" in refs_for(judge_final, JUDGES[2][3])[0][0]


def test_driver_feedback_renders_as_an_enumerable_numbered_list():
    """A judge counting "every point has a row" has to see the points as a
    list. The annotation payload's anchor travels with the point."""
    rendered = judge_round.read_feedback(ROUND / "feedback.json")
    lines = [l for l in rendered.splitlines() if l.strip()]
    assert len(lines) == 4
    assert lines[0].startswith("1. ") and lines[3].startswith("4. ")
    assert "Keep it in notes/store.py" in lines[3]
    assert "[anchored on: #slice-1]" in lines[3]     # selector preserved, not dropped
    # anything that is not the payload shape passes through as written
    d = scratch() / "f.md"
    d.write_text("- make it faster\n- and cheaper\n")
    assert judge_round.read_feedback(d) == "- make it faster\n- and cheaper"


# --------------------------------------------------------------------------- the teeth


def test_a_fabricated_quote_is_downgraded_to_unknown():
    """The anti-hallucination check, per judge: a PASS whose evidence is not in
    the artifact is an abstention, not a pass."""
    for mod, good, _corrupt, argv in JUDGES:
        artifact = judge_core.read_artifact(good)
        refs = refs_for(mod, argv)
        with fake_claude(mod, lambda p: reply_for(
                mod, p, quote="the tag index is rebuilt nightly")):
            criteria = judge_core.judge(mod.SPEC, refs, artifact, "sonnet",
                                        call=mod._call)
        n = len(mod.CRITERIA)
        assert {c["verdict"] for c in criteria} == {"UNKNOWN"}, mod.__name__
        assert all("does not appear verbatim" in c["reason"] for c in criteria)
        t = judge_core.tally(criteria)
        assert t["UNKNOWN"] == n and t["PASS"] == 0
        assert t["passed"] is False           # UNKNOWN counts as FAIL at the gate

        with fake_claude(mod, lambda p: reply_for(mod, p)):
            assert judge_core.tally(judge_core.judge(
                mod.SPEC, refs, artifact, "sonnet", call=mod._call))["PASS"] == n


def test_quote_verification_rules():
    artifact = judge_core.read_artifact(RISK / "risk-hold-good.html")
    # a two-word "quote" is not evidence, even though it does appear
    assert judge_core.verify_quote("the risk", artifact) is False
    assert judge_core.verify_quote("Likelihood: likely", artifact) is True
    # line wrapping is not a quote edit; changing a word is
    assert judge_core.verify_quote("It merges run 41 into main, which is the "
                                   "branch the notes release tag is cut from",
                                   artifact) is True
    assert judge_core.verify_quote("It merges run 41 into trunk, which is the "
                                   "branch the notes release tag is cut from",
                                   artifact) is False
    # the structure digest is part of the graded document, so it is quotable —
    # that is what makes "shown as a diff" evidenceable at all
    assert judge_core.verify_quote("diff blocks (pre.diff): 2", artifact) is True


def test_a_broken_judge_abstains_and_only_unparseable_replies_are_retried():
    for mod, good, _corrupt, argv in JUDGES:
        artifact, refs = judge_core.read_artifact(good), refs_for(mod, argv)

        def boom(prompt, model, timeout=None):
            raise RuntimeError("claude -p exited 1")

        mod.run_claude, real = boom, mod.run_claude
        try:
            criteria = judge_core.judge(mod.SPEC, refs, artifact, "sonnet",
                                        call=mod._call)
        finally:
            mod.run_claude = real
        assert len(criteria) == len(mod.CRITERIA)
        assert {c["verdict"] for c in criteria} == {"UNKNOWN"}

        # a reply that is not JSON at all: one repair attempt, then abstain
        with fake_claude(mod, "Here's my assessment of the page!") as fake:
            assert {c["verdict"] for c in judge_core.judge(
                mod.SPEC, refs, artifact, "s", call=mod._call)} == {"UNKNOWN"}
        assert len(fake.calls) == 2 * len(mod.GROUPS)
        assert "was not parseable JSON" in fake.calls[1]["prompt"]

        # ...and it complies on the repair prompt
        seen = []

        def flaky(prompt):
            seen.append(prompt)
            return ("Sure! Here's the evaluation." if len(seen) % 2 == 1
                    else reply_for(mod, prompt))

        with fake_claude(mod, flaky) as fake:
            criteria = judge_core.judge(mod.SPEC, refs, artifact, "sonnet",
                                        call=mod._call)
        assert judge_core.tally(criteria)["PASS"] == len(mod.CRITERIA)
        assert len(fake.calls) == 2 * len(mod.GROUPS)

        # a reply that PARSED is never re-asked — that would be shopping for a verdict
        with fake_claude(mod, lambda p: reply_for(mod, p, verdict="FAIL")) as fake:
            criteria = judge_core.judge(mod.SPEC, refs, artifact, "sonnet",
                                        call=mod._call)
        assert len(fake.calls) == len(mod.GROUPS)
        t = judge_core.tally(criteria)
        assert t["FAIL"] == len(mod.CRITERIA) and t["passed"] is False


def test_a_mandatory_failure_rejects_and_an_important_one_does_not():
    spec = judge_final.SPEC
    mandatory = [c for c, v in spec.criteria.items() if v["type"] == "mandatory"]
    important = [c for c, v in spec.criteria.items() if v["type"] == "important"]
    rows = [{"criterion_id": c, "type": spec.criteria[c]["type"], "verdict": "PASS",
             "evidence_quote": "", "reason": ""} for c in spec.criteria]
    assert judge_core.tally(rows)["passed"] is True

    bad_important = [dict(r, verdict="FAIL") if r["criterion_id"] in important[:1]
                     else r for r in rows]
    assert judge_core.tally(bad_important)["passed"] is True

    bad_mandatory = [dict(r, verdict="FAIL") if r["criterion_id"] in mandatory[:1]
                     else r for r in rows]
    t = judge_core.tally(bad_mandatory)
    assert t["passed"] is False and t["mandatory_failures"] == mandatory[:1]
    assert "REJECT" in judge_core.summary_line(t)


# --------------------------------------------------------------------------- artifacts


def test_the_structure_digest_reports_markup_the_page_text_loses():
    """Judges b/c/d grade how the page SHOWS things — a diff as a diff, a
    `changed` chip, an embedded screenshot — and all of that lives in markup
    `read_artifact` throws away. The digest is computed in Python, so the judge
    reads a measurement instead of imagining a rendering."""
    good = judge_core.read_artifact(FINAL / "final-good.html")
    corrupt = judge_core.read_artifact(FINAL / "final-corrupt.html")
    assert "diff blocks (pre.diff): 3" in good
    assert "notes/store.py:96-131" in good and "6 added lines, 1 removed lines" in good
    assert "embedded media: 1" in good and "list-filtered.png" in good
    # the corrupt page's final-state listings produce no diff blocks at all
    assert "diff blocks (pre.diff): 0" in corrupt and "embedded media: 0" in corrupt

    # round surfaces: the chips are the "changed sections marked" evidence
    rnd = judge_core.read_artifact(ROUND / "round-2-good.html")
    assert "chips (span.chip): 3 — 'changed', 'changed', 'changed'" in rnd

    # an uncaptioned hunk reports no caption rather than inheriting the previous
    # one's, and script/style never reach the text
    d = scratch() / "p.html"
    d.write_text('<style>.x{color:red}</style><script>var a=1</script>'
                 '<p class="diff-caption">a.py:1-2 — one</p>'
                 '<pre class="diff"><span class="del">-x</span></pre>'
                 '<pre class="diff"><span class="add">+y</span></pre>')
    out = judge_core.read_artifact(d)
    assert "color:red" not in out and "var a=1" not in out
    assert "diff 1: caption 'a.py:1-2 — one' — 0 added lines, 1 removed lines" in out
    assert "diff 2: caption '' — 1 added lines, 0 removed lines" in out


def test_seeded_corruptions_carry_their_ground_truth_and_hide_it_from_the_judge():
    """Each corrupt fixture names its defects and the criterion each targets —
    and none of that reaches the judge, because markers are comments and
    comments are not what a driver reads."""
    expected = {
        ROUND / "round-2-corrupt.html": {
            "missing-orientation": "orientation_standalone",
            "unreasoned-decline": "declined_points_reasoned",
            "silent-driver-point": "every_point_has_a_row"},
        RISK / "risk-hold-corrupt.html": {
            "missing-approval-commitment": "approval_commits_to",
            "blended-grade-justification": "per_axis_justification",
            "no-code-hunks": "code_embedded_as_hunks"},
        FINAL / "final-corrupt.html": {
            "context-first-opening": "verdict_case_first",
            "described-only-demonstration": "demonstration_in_modality",
            "final-state-code": "code_shown_as_change"},
    }
    for (mod, good, corrupt, _argv), (path, defects) in zip(JUDGES, expected.items()):
        assert path == corrupt
        raw = corrupt.read_text()
        for defect, criterion in defects.items():
            assert f"corruption: {defect}" in raw, defect
            assert criterion in raw and criterion in mod.CRITERIA
            assert mod.CRITERIA[criterion]["type"] == "mandatory"
        graded = judge_core.read_artifact(corrupt)
        assert "corruption:" not in graded and "<!--" not in graded
        # the good sample carries no seeded defects
        assert "corruption:" not in good.read_text()

    # ...and each defect is really present in what the judge reads --------------
    r_good = judge_core.read_artifact(ROUND / "round-2-good.html")
    r_bad = judge_core.read_artifact(ROUND / "round-2-corrupt.html")
    # 1. orientation block gone
    assert "Where you are" in r_good and "Where you are" not in r_bad
    # 2. the decline lost its reasoning
    assert has(r_good, "that is a second decision, not a second line of code")
    assert not has(r_bad, "that is a second decision, not a second line of code")
    assert "Not doing this. Out of scope." in " ".join(r_bad.split())
    # 3. driver point 4 has no row, and the plan it asked about is unchanged
    assert not has(r_bad, "normalize_tag() lives in notes/store.py")
    assert not has(r_bad, "Don't add a new module") and has(r_bad, "notes/taxonomy.py")
    assert "normalize_tag" in r_good

    k_good = judge_core.read_artifact(RISK / "risk-hold-good.html")
    k_bad = judge_core.read_artifact(RISK / "risk-hold-corrupt.html")
    assert has(k_good, "What approving commits to") and has(k_good, "Reversal cost:")
    assert not has(k_bad, "What approving commits to") and not has(k_bad, "Reversal cost:")
    assert "diff blocks (pre.diff): 2" in k_good
    assert "diff blocks (pre.diff): 0" in k_bad          # code is prose + a PR link
    assert has(k_bad, "The full diff is on the PR.")
    # both still name the two axes on the scale; only the justification blends
    for text in (k_good, k_bad):
        assert "Likelihood: likely" in text and "story-wide" in text
    assert has(k_good, "The only users who escape are those who never write again.")

    f_good = judge_core.read_artifact(FINAL / "final-good.html")
    f_bad = judge_core.read_artifact(FINAL / "final-corrupt.html")
    assert has(f_good, "The case for merging") and not has(f_bad, "The case for merging")
    assert has(f_bad, "How this run went")                   # opens with context instead
    assert "IDENTICAL" in f_good and "IDENTICAL" not in f_bad
    assert has(f_bad, "and everything works")                    # asserted, not shown
    # what survives intact, so the defects stay independently visible
    for text in (f_good, f_bad):
        assert "Review order" in text and "Check:" in text
        assert "Where you are" in text


# --------------------------------------------------------------------------- output


def test_judged_jsonl_schema_is_one_table_across_the_three_judges():
    d = scratch()
    path = d / "judged.jsonl"
    for mod, good, corrupt, argv in JUDGES:
        with fake_claude(mod, lambda p: reply_for(mod, p)):
            for art in (good, corrupt):
                mod.main(argv + ["--artifact", str(art), "--model", "sonnet",
                                 "--results", str(path)])

    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows) == 6                          # append-only history
    assert [r["judge"] for r in rows] == ["judge_round"] * 2 + ["judge_risk"] * 2 \
        + ["judge_final"] * 2
    assert [r["stage"] for r in rows] == ["eval-judge-round"] * 2 \
        + ["eval-judge-risk"] * 2 + ["eval-judge-final"] * 2
    for row, (mod, *_rest) in zip(rows, [j for j in JUDGES for _ in (0, 1)]):
        assert set(row) >= {"ts", "judge", "stage", "story", "artifact_path",
                            "artifact_sha", "judge_model", "criteria", "tally"}
        assert row["judge_model"] == "sonnet"
        assert len(row["artifact_sha"]) == 64
        assert len(row["criteria"]) == len(mod.CRITERIA)
        assert [c["criterion_id"] for c in row["criteria"]] == list(mod.CRITERIA)
        for c in row["criteria"]:
            assert set(c) == {"criterion_id", "type", "verdict", "evidence_quote",
                              "reason"}
            assert c["verdict"] in ("PASS", "FAIL", "UNKNOWN")
            assert c["type"] in ("mandatory", "important")
    # good and corrupt are different bytes, and each record says so
    assert rows[0]["artifact_sha"] != rows[1]["artifact_sha"]
    assert rows[0]["story"] == "round-2-good"


def test_the_cli_shape_matches_judge_spec_and_the_model_is_pinned():
    d = scratch()
    for mod, good, _corrupt, argv in JUDGES:
        with fake_claude(mod, lambda p: reply_for(mod, p)) as fake:
            rc = mod.main(argv + ["--artifact", str(good),
                                  "--results", str(d / "j.jsonl")])
        assert rc == 0                                   # all PASS -> ACCEPT
        # default model is the pin, never the session's
        assert {c["model"] for c in fake.calls} == {judge_core.DEFAULT_MODEL} == {"sonnet"}

        # --group narrows the run to one isolated call, for debugging
        group = sorted(mod.GROUPS)[0]
        with fake_claude(mod, lambda p: reply_for(mod, p)) as fake:
            mod.main(argv + ["--artifact", str(good), "--group", group,
                             "--results", str(d / "j.jsonl")])
        assert len(fake.calls) == 1

        # a mandatory FAIL is a non-zero exit: the gate reads the return code
        with fake_claude(mod, lambda p: reply_for(mod, p, verdict="FAIL")):
            assert mod.main(argv + ["--artifact", str(good),
                                    "--results", str(d / "j.jsonl")]) == 1


def test_the_board_signal_names_the_judge_stage():
    """A judge is a plain script that runs with no board at all; with --board it
    writes the one event the next action reads."""
    d = scratch()
    mod, good, _corrupt, argv = JUDGES[1]
    with fake_claude(mod, lambda p: reply_for(mod, p)):
        mod.main(argv + ["--artifact", str(good), "--results", str(d / "j.jsonl"),
                         "--board", str(d / "board.db"), "--key", "eval:risky"])
    with Board(d / "board.db") as b:
        events = [e for e in b.peek(topic="evals", kind="signal")]
    assert len(events) == 1
    p = events[0]["payload"]
    assert events[0]["key"] == "eval:risky"
    assert p["status"] == "finished" and p["stage"] == "eval-judge-risk"
    n = len(mod.CRITERIA)
    assert p["passed"] is True and p["pass_count"] == n
    assert p["fail_count"] == 0 and p["unknown_count"] == 0
    assert f"{n}/{n} PASS" in p["summary"] and p["artifact"].endswith("risk-hold-good.html")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("surface-judge tests: all passed")
