"""Workflow #2: an eval run expressed as data.

What this suite guards is the same seam `test_pipeline_actions.py` guards, from
the other side: `config/actions-eval.json` validates against the engine's closed
vocabulary, and a whole eval run — fixture build, judge spawn, driver report —
can be walked by writing events at the board. No claude, no network: the only
agent call in the judge goes through `judge_spec.run_claude`, which every test
here replaces.

The judge's own contract is checked here too, because the quote-verification
downgrade is the thing that makes a judged claim worth anything (see
`evals/judge_spec.py` and PLAN-2026-09.md §2).

Run directly: python3 tests/test_eval_workflow.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

import judge_spec                                                  # noqa: E402
from runnerlib import engine, seed_eval_nodes                      # noqa: E402
from runnerlib.blackboard import Board                             # noqa: E402
from runnerlib.nodes import Nodes                                  # noqa: E402

ACTIONS = ROOT / "config" / "actions-eval.json"
FIXTURE = ROOT / "evals" / "fixtures" / "judge-sample"


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


def actions() -> list[dict]:
    return engine.load_actions(ACTIONS)


def seeded(d: Path) -> Nodes:
    seed_eval_nodes.seed(d)
    return Nodes(d)


class fake_claude:
    """Replace `judge_spec.run_claude` for the duration of a block. Every reply
    is a canned string; nothing here can reach the network."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, prompt, model, timeout=None):
        self.calls.append({"prompt": prompt, "model": model})
        r = self.replies
        return r(prompt) if callable(r) else r

    def __enter__(self):
        self.real = judge_spec.run_claude
        judge_spec.run_claude = self
        return self

    def __exit__(self, *exc):
        judge_spec.run_claude = self.real


def reply_for(prompt: str, verdict="PASS", quote=None) -> str:
    """A well-formed judge reply for whichever criteria this prompt asked about,
    quoting real artifact text so the verification passes."""
    ids = [cid for cid in judge_spec.CRITERIA if f"id `{cid}`" in prompt]
    rows = [{"criterion_id": cid, "verdict": verdict,
             "evidence_quote": quote if quote is not None
             else "Notes gain optional tags at add time",
             "reason": "canned"} for cid in ids]
    return json.dumps({"criteria": rows})


# --------------------------------------------------------------------------- config


def test_actions_eval_loads_and_validates():
    loaded = actions()
    names = [a["name"] for a in loaded]
    assert names == ["eval-requested", "fixture-done-to-judge", "judge-done-to-report"]
    assert len(names) == len(set(names))
    for a in loaded:
        assert engine.validate_action(a) is a

    # the three shapes the workflow is built from: a command body, a node spawn,
    # a human report
    by = {a["name"]: a for a in loaded}
    assert by["eval-requested"]["body"]["type"] == "run_command"
    assert by["eval-requested"]["body"]["argv"][:2] == ["python3", "evals/run_fixture.py"]
    assert by["fixture-done-to-judge"]["body"] == {"type": "spawn_node",
                                                  "node": "eval-judge-spec"}
    assert [e["type"] for e in by["judge-done-to-report"]["emitter"]] == ["hitl",
                                                                         "write_event"]
    # every spawn_node names a node the eval seed actually installs
    spawned = {a["body"]["node"] for a in loaded
               if (a.get("body") or {}).get("type") == "spawn_node"}
    assert spawned <= set(seed_eval_nodes.NODES)


def test_the_judge_node_is_not_an_agent_cli():
    """The node abstraction is agent-command-agnostic: this node's command is a
    python script, and the model pin still travels into it."""
    d = scratch()
    n = seeded(d)
    node = n.active(seed_eval_nodes.JUDGE_NODE)
    assert node["command"].startswith("python3 ")
    assert "judge_spec.py" in node["command"]
    # The point is that no AGENT CLI is invoked, so check what is being run
    # rather than the whole string. The command embeds an absolute path, and
    # a substring test failed for anyone whose checkout lived under a
    # directory with "claude" in its name — `~/.claude/...` or a scratch dir —
    # which is a test that breaks on where you cloned it, not on what it does.
    invoked = [tok for tok in node["command"].split()
               if not tok.startswith("-") and not tok.startswith("{")]
    assert not any(Path(tok).name.startswith("claude") for tok in invoked), invoked
    assert node["model"] == "sonnet"

    event = {"id": "e1", "key": "eval:judge-sample",
             "payload": {"story": "judge-sample", "artifact": "/o/b.md",
                         "story_path": "/s/story.md", "board": "/o/board.db"}}
    argv = n.command_argv(seed_eval_nodes.JUDGE_NODE, event=event, node=node)
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--rubric") + 1] == node["prompt_path"]
    assert argv[argv.index("--key") + 1] == "eval:judge-sample"
    assert argv[argv.index("--name") + 1] == "judge-sample"
    # the versioned prompt IS the rubric the script runs
    assert node["prompt"] == judge_spec.PREAMBLE


def test_seed_is_idempotent():
    d = scratch()
    assert seed_eval_nodes.seed(d) == {"eval-judge-spec": "registered"}
    assert seed_eval_nodes.seed(d) == {"eval-judge-spec": "unchanged"}
    assert len(Nodes(d).history("eval-judge-spec")) == 1


# --------------------------------------------------------------------------- the walk


def test_eval_run_walks_from_request_to_driver_report():
    """One eval run, request to HITL, driven only by events at the board."""
    d = scratch()
    out = d / "run"
    b, n, acts = Board(d / "board.db"), seeded(d), actions()
    key = "eval:judge-sample"

    was = os.getcwd()
    os.chdir(ROOT)          # the argv is repo-relative, like `runnerlib.gh_watch`
    try:
        # -- somebody asks for an eval -------------------------------------
        b.write("cadre", "evals", key, "command",
                {"target": "eval", "story": "judge-sample",
                 "story_path": str(FIXTURE / "story.md"), "out": str(out),
                 "board": str(d / "board.db")})
        spawns, firings = engine.tick(b, acts, n, d)

        # the body really ran: the stub copied the artifact into place. Both
        # steps land in ONE pass — actions are independent consumers, so the
        # signal action 1 emitted is already on the board when action 2 reads.
        assert [f["outcome"] for f in firings] == ["fired", "fired"]
        assert (out / "briefing-good.md").exists()
        done = [e for e in b.peek(topic="evals", kind="signal")
                if e["payload"]["stage"] == "eval-fixture"]
        assert len(done) == 1
        assert done[0]["payload"]["artifact"] == str(out / "briefing-good.md")

        # -- ...so the judge is spawned ------------------------------------
        assert [s["node"] for s in spawns] == ["eval-judge-spec"]
        argv = spawns[0]["argv"]
        assert argv[1].endswith("evals/judge_spec.py")
        assert argv[argv.index("--artifact") + 1] == str(out / "briefing-good.md")
        assert argv[argv.index("--board") + 1] == str(d / "board.db")
        assert spawns[0]["model"] == "sonnet"

        # -- the judge runs (the daemon would exec that argv) ---------------
        with fake_claude(lambda p: reply_for(p)):
            rc = judge_spec.main(argv[2:] + ["--results", str(d / "judged.jsonl")])
        assert rc == 0

        # once-only: a second request on the same key is guarded, because the
        # run's own signals are now on the board
        b.write("cadre", "evals", key, "command",
                {"target": "eval", "story": "judge-sample",
                 "story_path": str(FIXTURE / "story.md"), "out": str(out),
                 "board": str(d / "board.db")})

        # -- its signal reaches the driver ----------------------------------
        spawns, firings = engine.tick(b, acts, n, d)
        assert [f["action"] for f in firings] == ["judge-done-to-report"]
        assert spawns == []
        hitl = [json.loads(l) for l in (d / "hitl-outbox.jsonl").read_text().splitlines()]
        assert len(hitl) == 1
        assert hitl[0]["key"] == key
        assert "Eval judged: judge-sample" in hitl[0]["summary"]
        assert "7/7 PASS" in hitl[0]["summary"]
        assert hitl[0]["detail"]["channel"] == "eval_report"
        assert any(e["payload"]["message"].startswith("eval judged:")
                   for e in b.peek(topic="evals", kind="notify"))
    finally:
        os.chdir(was)


def test_live_flag_refuses_rather_than_stubbing_silently():
    import run_fixture

    d = scratch()
    try:
        run_fixture.main(["--story", "judge-sample", "--out", str(d), "--live"])
        raise AssertionError("--live must not quietly replay the stub")
    except SystemExit as e:
        assert "not wired up" in str(e)


# --------------------------------------------------------------------------- the judge


def test_a_fabricated_quote_is_downgraded_to_unknown():
    """The anti-hallucination teeth: a PASS whose evidence is not in the
    artifact is an abstention, not a pass."""
    artifact = judge_spec.artifact_text(FIXTURE / "briefing-good.md")
    story = (FIXTURE / "story.md").read_text()

    with fake_claude(lambda p: reply_for(p, quote="the tag index is rebuilt nightly")):
        criteria = judge_spec.judge(story, artifact, "sonnet")

    assert {c["verdict"] for c in criteria} == {"UNKNOWN"}
    assert all("does not appear verbatim" in c["reason"] for c in criteria)
    t = judge_spec.tally(criteria)
    assert t["UNKNOWN"] == 7 and t["PASS"] == 0
    assert t["passed"] is False                  # UNKNOWN counts as FAIL at the gate

    # a real quote from the artifact survives
    with fake_claude(lambda p: reply_for(p)):
        assert judge_spec.tally(judge_spec.judge(story, artifact, "sonnet"))["PASS"] == 7

    # a too-short "quote" is not evidence, even though it does appear
    assert judge_spec.verify_quote("tags", artifact) is False
    assert judge_spec.verify_quote("Notes gain optional tags", artifact) is True
    # line wrapping is not a quote edit; changing a word is
    assert judge_spec.verify_quote("a user can see only the notes\ncarrying a "
                                   "given tag", artifact) is True
    assert judge_spec.verify_quote("a user can see all the notes carrying a "
                                   "given tag", artifact) is False


def test_a_broken_judge_abstains_instead_of_passing():
    story, artifact = "story", "artifact text long enough to quote from"

    def boom(prompt, model, timeout=None):
        raise RuntimeError("claude -p exited 1")

    judge_spec.run_claude, real = boom, judge_spec.run_claude
    try:
        criteria = judge_spec.judge(story, artifact, "sonnet")
    finally:
        judge_spec.run_claude = real
    assert len(criteria) == 7
    assert {c["verdict"] for c in criteria} == {"UNKNOWN"}

    # so does a reply that is not JSON at all — after one repair attempt
    with fake_claude("I'd be happy to help you evaluate this briefing!") as fake:
        assert {c["verdict"] for c in judge_spec.judge(story, artifact, "s")} == {"UNKNOWN"}
    assert len(fake.calls) == 2 * len(judge_spec.GROUPS)
    assert "was not parseable JSON" in fake.calls[1]["prompt"]


def test_an_unparseable_reply_is_retried_once_and_a_parsed_one_never_is():
    story = (FIXTURE / "story.md").read_text()
    artifact = judge_spec.artifact_text(FIXTURE / "briefing-good.md")

    # the live failure mode this exists for: the judge narrates instead of
    # answering, then complies on the repair prompt
    seen = []

    def flaky(prompt):
        seen.append(prompt)
        return ("Here's my assessment of the briefing!" if len(seen) % 2 == 1
                else reply_for(prompt))

    with fake_claude(flaky) as fake:
        criteria = judge_spec.judge(story, artifact, "sonnet")
    assert judge_spec.tally(criteria)["PASS"] == 7
    assert len(fake.calls) == 2 * len(judge_spec.GROUPS)

    # a reply that PARSED is never re-asked — that would be shopping for a verdict
    with fake_claude(lambda p: reply_for(p, verdict="FAIL")) as fake:
        criteria = judge_spec.judge(story, artifact, "sonnet")
    assert len(fake.calls) == len(judge_spec.GROUPS)
    assert judge_spec.tally(criteria)["FAIL"] == 7


def test_one_isolated_call_per_criterion_group():
    story = (FIXTURE / "story.md").read_text()
    artifact = judge_spec.artifact_text(FIXTURE / "briefing-good.md")
    with fake_claude(lambda p: reply_for(p)) as fake:
        judge_spec.judge(story, artifact, "haiku")

    assert len(fake.calls) == len(judge_spec.GROUPS) == 3
    assert {c["model"] for c in fake.calls} == {"haiku"}      # the pin travels
    # rubric first, inputs last: every call shares a byte-identical prefix, and
    # nothing that moves run to run is in it
    prefixes = {c["prompt"][:len(judge_spec.PREAMBLE)] for c in fake.calls}
    assert prefixes == {judge_spec.PREAMBLE}
    # each criterion is asked about exactly once, and the seven are covered
    asked = [cid for c in fake.calls for cid in judge_spec.CRITERIA
             if f"id `{cid}`" in c["prompt"]]
    assert sorted(asked) == sorted(judge_spec.CRITERIA)


def test_judged_jsonl_schema():
    d = scratch()
    path = d / "judged.jsonl"
    with fake_claude(lambda p: reply_for(p)):
        judge_spec.main(["--story", str(FIXTURE / "story.md"),
                         "--artifact", str(FIXTURE / "briefing-good.md"),
                         "--model", "sonnet", "--results", str(path)])
        judge_spec.main(["--story", str(FIXTURE / "story.md"),
                         "--artifact", str(FIXTURE / "briefing-corrupt.md"),
                         "--model", "sonnet", "--results", str(path)])

    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows) == 2                        # append-only history
    for row in rows:
        assert set(row) >= {"ts", "story", "artifact_path", "artifact_sha",
                            "judge_model", "criteria"}
        assert row["story"] == "story" and row["judge_model"] == "sonnet"
        assert len(row["artifact_sha"]) == 64
        assert len(row["criteria"]) == 7
        for c in row["criteria"]:
            assert set(c) == {"criterion_id", "type", "verdict", "evidence_quote",
                              "reason"}
            assert c["verdict"] in ("PASS", "FAIL", "UNKNOWN")
            assert c["type"] in ("mandatory", "important")
        assert [c["criterion_id"] for c in row["criteria"]] == list(judge_spec.CRITERIA)
    # the two artifacts are different bytes, and the record says so
    assert rows[0]["artifact_sha"] != rows[1]["artifact_sha"]


def test_seeded_corruptions_carry_their_ground_truth_and_hide_it_from_the_judge():
    raw = (FIXTURE / "briefing-corrupt.md").read_text()
    seeded_defects = {"invented-file-reference", "dropped-constraint",
                      "restated-validation-path"}
    assert all(f"corruption: {name}" in raw for name in seeded_defects)

    # ...and none of it reaches the judge: markers are comments, and comments
    # are not what a driver reads
    graded = judge_spec.artifact_text(FIXTURE / "briefing-corrupt.md")
    assert "corruption" not in graded and "<!--" not in graded
    # the defects themselves survive the strip
    assert "notes/taxonomy.py" in graded and "notes/export.py" in graded
    assert "the tag-filtered\nlisting behaves as a tag-filtered listing should" in graded
    # the story's one constraint is present in the good briefing and gone here
    good = judge_spec.artifact_text(FIXTURE / "briefing-good.md")
    assert "one JSON file" in good and "one JSON file" not in graded


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("eval-workflow tests: all passed")
