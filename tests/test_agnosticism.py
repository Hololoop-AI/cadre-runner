"""The headline claim: the engine is workflow-agnostic.

Two workflows exist — `config/actions-pipeline.json` (the 7-stage PR-gated
pipeline) and `config/actions-eval.json` (an eval run: build a fixture, judge
it, report to the driver). They share no vocabulary: different topics,
different kinds, different node registry, one spawning `claude -p` sessions and
one spawning a python script. Neither is mentioned anywhere in `runnerlib`.

This suite asserts that three ways, because "workflow-agnostic" is a claim that
rots quietly:

  1. Both files load and validate through the SAME `load_actions` path.
  2. ONE `tick` over ONE board carrying one pipeline event and one eval event
     fires actions from both files, with the two configs concatenated in a
     single list and no flag, branch or ordering telling them apart.
  3. `runnerlib/engine.py`'s executable source mentions no name from either
     workflow. Docstrings are stripped before the grep — prose explaining the
     design is not coupling, and this file would otherwise be gamed by
     rewording a comment.

Run directly: python3 tests/test_agnosticism.py
"""

import ast
import io
import json
import os
import re
import sys
import tempfile
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runnerlib import engine, seed_eval_nodes, seed_nodes            # noqa: E402
from runnerlib.blackboard import Board                               # noqa: E402
from runnerlib.nodes import Nodes                                    # noqa: E402

PIPELINE = ROOT / "config" / "actions-pipeline.json"
EVAL = ROOT / "config" / "actions-eval.json"
FIXTURE = ROOT / "evals" / "fixtures" / "judge-sample"


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


# --------------------------------------------------------------------------- 1. load


def test_both_workflows_load_through_the_same_path():
    pipeline, evals = engine.load_actions(PIPELINE), engine.load_actions(EVAL)
    assert pipeline and evals

    # the same closed vocabulary covers both, and it is a SMALL one
    for a in pipeline + evals:
        assert engine.validate_action(a) is a
        for cond in engine._conditions(a["trigger"].get("where")):
            assert cond["op"] in engine.WHERE_OPS
        if a.get("body"):
            assert a["body"]["type"] in engine.BODY_TYPES
        for em in (a["emitter"] if isinstance(a["emitter"], list) else [a["emitter"]]):
            assert em["type"] in engine.EMITTER_TYPES

    # and they really are different workflows, not a copy with new names
    def topics(actions):
        return {a["trigger"]["topic"] for a in actions}

    assert topics(pipeline).isdisjoint(topics(evals))
    assert {a["name"] for a in pipeline}.isdisjoint({a["name"] for a in evals})


# --------------------------------------------------------------------------- 2. tick


def test_one_tick_serves_both_workflows_with_no_special_casing():
    d = scratch()
    b = Board(d / "board.db")
    seed_nodes.seed(d)
    seed_eval_nodes.seed(d)
    nodes = Nodes(d)

    # ONE list, concatenated. The engine is handed no hint that these came from
    # two files, and receives no per-workflow argument of any kind.
    actions = engine.load_actions(PIPELINE) + engine.load_actions(EVAL)

    was = os.getcwd()
    os.chdir(ROOT)
    try:
        # a pipeline event: the tests PR merged, so the build stage should spawn
        b.write("cadre", "github", "story:nex-1", "signal",
                {"status": "finished", "story": "nex-1", "repo": "o/r",
                 "role": "tests", "state": "merged", "slice": "core", "pr": 9})
        # an eval event: somebody asked for a fixture story to be judged
        b.write("cadre", "evals", "eval:judge-sample", "command",
                {"target": "eval", "story": "judge-sample",
                 "story_path": str(FIXTURE / "story.md"),
                 "out": str(d / "run"), "board": str(d / "board.db")})

        spawns, firings = engine.tick(b, actions, nodes, d)
    finally:
        os.chdir(was)

    fired = {f["action"] for f in firings if f["outcome"] == "fired"}
    assert "tests-merged-to-build" in fired          # from actions-pipeline.json
    assert "eval-requested" in fired                 # from actions-eval.json
    assert all(f["outcome"] == "fired" for f in firings), firings

    # both bodies did their real work in the same pass, from the same
    # three-part action shape: a `claude -p` stage spawn and a python-script
    # node spawn, plus a command body that really ran
    assert sorted(s["node"] for s in spawns) == ["build", "eval-judge-spec"]
    assert (d / "run" / "briefing-good.md").exists()
    argv = {s["node"]: s["argv"][0] for s in spawns}
    assert argv["build"] == "claude" and argv["eval-judge-spec"] == "python3"

    # the emitted events landed on their own workflow's topic and nowhere else
    assert [e["payload"]["stage"] for e in b.peek(topic="stages", kind="signal")] == ["build"]
    assert [e["payload"]["stage"] for e in b.peek(topic="evals", kind="signal")] \
        == ["eval-fixture", "eval-judge"]

    # one firing log, two workflows — the ONLY thing that knows about both
    assert {f["action"] for f in b.firings()} == fired


# --------------------------------------------------------------------------- 3. source


def executable_source(path: Path) -> str:
    """The module's source with every docstring and comment removed. What is
    left is the code, which is what the coupling claim is about."""
    src = path.read_text()
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.string.strip("rbfu\"'\n ") in \
                {d.strip() for d in docstrings}:
            continue
        out.append(tok.string)
    return "\n".join(out)


# Words both workflows share with the engine's OWN vocabulary — board header
# fields, kinds, statuses. Finding these in engine.py proves nothing, so they
# are excluded rather than quietly weakening the assertion.
SHARED = {"cadre", "status", "started", "finished", "progress", "failed",
          "signal", "command", "notify", "report", "heartbeat", "key", "topic",
          "namespace", "payload", "type", "action", "error", "note", "message",
          "target", "kind",
          # `open` is a python builtin the engine calls; finding it says nothing
          # about coupling, even though a pipeline action routes on state=open.
          "open",
          # a board claim state, and also the pipeline's terminal phase name
          "done"}


def workflow_vocabulary() -> set[str]:
    """Every name either workflow uses that the engine must not know: action
    names, topics, node names, and the `stage`/`phase`/`role` values the
    actions route on."""
    words = set()
    for path in (PIPELINE, EVAL):
        for a in engine.load_actions(path):
            words.add(a["name"])
            words.add(a["trigger"].get("topic"))
            for cond in engine._conditions(a["trigger"].get("where")):
                if cond["op"] == "payload_eq":
                    words.add(cond.get("value"))
                words.add(cond.get("topic"))
            for em in (a["emitter"] if isinstance(a["emitter"], list)
                       else [a["emitter"]]):
                words.add(em.get("topic"))
                payload = em.get("payload") or {}
                words.update(payload.get(f) for f in ("stage", "phase", "role"))
            if (a.get("body") or {}).get("type") == "spawn_node":
                words.add(a["body"]["node"])
    words |= set(seed_nodes.STAGES) | set(seed_eval_nodes.NODES)
    # the workflow nouns themselves, which no config field happens to spell out
    words |= {"story", "github", "briefing", "judge", "fixture", "rubric"}
    words = {w for w in words
             if isinstance(w, str) and w and "{" not in w and " " not in w}
    return words - SHARED


def mentions(word: str, code: str) -> bool:
    """Whole-word, hyphens included: `pr` must not match `proc`, and
    `eval-judge` must not match `eval-judge-spec` by accident."""
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", code) is not None


def test_engine_source_names_no_workflow_concept():
    vocab = workflow_vocabulary()
    for module in ("engine.py", "nodes.py", "blackboard.py"):
        code = executable_source(ROOT / "runnerlib" / module)
        leaked = sorted(w for w in vocab if mentions(w, code))
        assert leaked == [], f"{module}'s code mentions workflow concepts: {leaked}"

    # controls on the grep itself, so a vacuous pass is not possible:
    # (a) the vocabulary is real and non-trivial
    assert len(vocab) > 25, sorted(vocab)
    assert {"intake", "assembly", "risk-triage", "eval-judge-spec", "eval-fixture",
            "eval-judge", "stages", "evals", "github"} <= vocab
    # (b) every word in it really does appear in the sources it was drawn from
    corpus = (PIPELINE.read_text() + EVAL.read_text()
              + " ".join(seed_nodes.STAGES) + " ".join(seed_eval_nodes.NODES)
              + " story github briefing judge fixture rubric")
    assert all(w in corpus for w in vocab)
    # (c) the grep CAN fire: the engine's own vocabulary is found by it
    code = executable_source(ROOT / "runnerlib" / "engine.py")
    for word in ("spawn_node", "write_event", "payload_eq", "hitl"):
        assert mentions(word, code)
    # (d) ...and it fires on a plant, which is what a regression would look like
    assert mentions("assembly", code + "\nif stage == 'assembly': pass")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("agnosticism tests: all passed")
