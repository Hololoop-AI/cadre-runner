"""Workflow #1: the 7-stage pipeline expressed as data.

The seam this suite guards is "stage sequencing lives in config, not Python".
So it checks the three things that can silently rot: the seed stays idempotent,
`config/actions-pipeline.json` still validates against the engine's closed
vocabulary, and a story can be walked end to end by writing events at the board
and reading spawn specs back out. No claude, no gh, no network — the only
command any of these actions runs is echo.

Run directly: python3 tests/test_pipeline_actions.py
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import config as config_mod
from runnerlib import engine, engine_seam, gh_watch, preflight, seed_nodes
from runnerlib.blackboard import Board
from runnerlib.nodes import Nodes, version_id

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ROOT / "config" / "actions-pipeline.json"
PROMPTS = ROOT / "prompts"


def scratch() -> Path:
    return Path(tempfile.mkdtemp())


def board(d: Path) -> Board:
    return Board(d / "board.db")


def seeded(d: Path) -> Nodes:
    seed_nodes.seed(d)
    return Nodes(d)


def actions() -> list[dict]:
    return engine.load_actions(ACTIONS)


def by_name(name: str) -> dict:
    return next(a for a in actions() if a["name"] == name)


def gh_signal(b, slug="nex-1", **payload):
    """What the gh-watch shim writes: repo state on the story's github key."""
    return b.write("cadre", "github", f"story:{slug}", "signal",
                   {"status": "finished", "story": slug, "repo": "o/r",
                    "slice": "", "pr": 12, **payload})


def stage_signal(b, slug="nex-1", **payload):
    return b.write("cadre", "stages", f"story:{slug}", "signal",
                   {"status": "finished", "story": slug, "slice": "", "pr": 12,
                    **payload})


@contextmanager
def engine_mode(value):
    """CADRE_ENGINE for the duration, restored afterwards. The seam caches a
    board handle per data dir, so the cache is dropped on the way out too."""
    was = os.environ.get("CADRE_ENGINE")
    if value is None:
        os.environ.pop("CADRE_ENGINE", None)
    else:
        os.environ["CADRE_ENGINE"] = value
    try:
        yield
    finally:
        engine_seam.reset()
        os.environ.pop("CADRE_ENGINE", None)
        if was is not None:
            os.environ["CADRE_ENGINE"] = was


class FakeCfg:
    """Enough config for the seam: a data dir, the stage pins, the round cap."""

    def __init__(self, d, max_rounds=5):
        self.data_dir = Path(d)
        self.limits = {"max_rounds_per_stage": max_rounds,
                       "allowed_actors": ["driver"]}
        # the real defaults, so a new config dial does not silently read as
        # absent in every seam test
        self.runner = {**config_mod.DEFAULTS["runner"], "max_concurrent_runs": 4}
        self.claude = {**config_mod.DEFAULTS["claude"]}

    def model_for(self, stage):
        return "opus"

    def effort_for(self, stage):
        return "high"

    def repo(self, name):
        return {"name": name, "default_branch": "main"}

    def checkout_dir(self, repo_cfg):
        return self.data_dir / "checkouts"


class FakeReg:
    def __init__(self, stories):
        self.data = {"stories": stories}

    def stories(self, status="active"):
        return {k: v for k, v in self.data["stories"].items()
                if v.get("status") == status}

    def get(self, slug):
        return self.data["stories"][slug]

    def save(self):
        pass

    def seen(self, story, kind, pr):
        return False

    def mark_seen(self, story, kind, pr):
        pass


# --------------------------------------------------------------------------- seed


def test_seed_registers_every_stage_prompt():
    d = scratch()
    result = seed_nodes.seed(d)
    n = Nodes(d)

    # every prompt file that names a stage became a node
    assert set(result) == set(seed_nodes.STAGES)
    assert set(n.names()) == set(seed_nodes.STAGES)
    assert all(v == "registered" for v in result.values())

    node = n.active("build")
    assert node["model"] and node["command"].startswith("claude -p {prompt}")
    # the seed is _common.md + the stage file, exactly as claude_run.render joins
    assert node["prompt"] == (PROMPTS / "_common.md").read_text() + "\n\n" + \
        (PROMPTS / "build.md").read_text()
    assert node["version"] == version_id(node["prompt"])
    # the export copy — the human-handoff artifact — exists for each
    assert n.export_path("build").exists()


def test_seed_is_idempotent_and_never_demotes():
    d = scratch()
    seed_nodes.seed(d)
    first = Nodes(d).active("tests")

    again = seed_nodes.seed(d)
    assert all(v == "unchanged" for v in again.values())
    n = Nodes(d)
    assert n.active("tests")["version"] == first["version"]
    assert len(n.history("tests")) == 1        # no near-duplicate version minted

    # a hand-promoted version survives a re-seed: the new file text is RECORDED
    # but promotion stays an explicit act (nodes.py's rule, not a new one)
    hand = n.new_version("tests", "hand-tuned prompt", produced_by="hand")
    n.promote("tests", hand)
    tmp = scratch()
    (tmp / "_common.md").write_text("COMMON")
    for stage in seed_nodes.STAGES:
        (tmp / f"{stage}.md").write_text(f"seed text for {stage}")
    result = seed_nodes.seed(d, prompts_dir=tmp)

    assert result["tests"] == "version-recorded"
    n2 = Nodes(d)
    assert n2.active("tests")["version"] == hand      # NOT rolled back
    assert any(v["id"] == version_id("COMMON\n\nseed text for tests")
               for v in n2.history("tests"))


def test_seed_takes_model_and_effort_pins_from_config():
    class FakeCfg:
        claude = {"bin": "agent-cli"}

        def model_for(self, stage):
            return "sonnet" if stage == "build" else "opus"

        def effort_for(self, stage):
            return "low" if stage == "build" else "high"

    d = scratch()
    seed_nodes.seed(d, FakeCfg())
    n = Nodes(d)
    assert n.active("build")["model"] == "sonnet"
    assert "--effort low" in n.active("build")["command"]
    assert n.active("intake")["model"] == "opus"
    assert "--effort high" in n.active("intake")["command"]
    # the binary is a config dial too: an exported node card must name the
    # program that will actually run, not a literal `claude`
    assert n.active("build")["command"].startswith("agent-cli -p ")
    assert "agent-cli" in n.export_path("build").read_text()

    # a re-seed re-pins the operator dials without touching the prompt version
    v = n.active("build")["version"]
    seed_nodes.seed(d, FakeCfg())
    assert Nodes(d).active("build")["version"] == v

    # ...and the re-pin reaches the export card: a handoff copy still naming the
    # binary the node was FIRST seeded with is the drift the seed exists to stop
    class Switched(FakeCfg):
        claude = {"bin": "other-agent"}

    seed_nodes.seed(d, Switched())
    card = Nodes(d).export_path("build").read_text()
    assert "command: other-agent -p " in card and "agent-cli" not in card
    assert Nodes(d).active("build")["version"] == v      # still not a promotion


# --------------------------------------------------------------------------- config


def test_actions_pipeline_loads_and_validates():
    loaded = actions()
    names = [a["name"] for a in loaded]
    assert len(names) == len(set(names)), "action names must be unique"
    for a in loaded:
        assert engine.validate_action(a) is a

    # the graph covers every stage transition the daemon used to hard-code
    assert {"gh-watch", "story-to-intake", "planning-merged-to-contracts",
            "planning-merged-to-slices", "contract-merged-to-tests",
            "tests-merged-to-build", "all-built-to-assembly",
            "assembly-finished-to-final-review", "risk-high-to-triage",
            "summon-to-revise", "summon-to-interrogate"} <= set(names)

    # every spawn_node names a node the seed actually installs
    spawned = {a["body"]["node"] for a in loaded
               if (a.get("body") or {}).get("type") == "spawn_node"}
    assert spawned <= set(seed_nodes.STAGES), spawned - set(seed_nodes.STAGES)


def test_gh_watch_action_runs_a_command_on_the_heartbeat():
    a = by_name("gh-watch")
    assert a["trigger"]["kind"] == "heartbeat"
    # GitHub is reached by an action running a CLI — no adapter component
    assert a["body"]["type"] == "run_command"
    assert a["body"]["argv"][:3] == ["python3", "-m", "runnerlib.gh_watch"]

    d = scratch()
    b = board(d)
    harmless = {**a, "body": {"type": "run_command",
                              "argv": ["/bin/echo", "gh-watch: 0 event(s)"]}}
    engine_seam.heartbeat(b)
    spawns, firings = engine.tick(b, [engine.validate_action(harmless)], None, d)

    assert spawns == [] and [f["outcome"] for f in firings] == ["fired"]
    reports = b.peek(topic="runner", kind="report")
    assert reports[0]["payload"]["pointer"] == "gh-watch: 0 event(s)"


# --------------------------------------------------------------------------- the walk


def test_story_walk_intake_to_assembly():
    """One story, start to final review, driven only by events at the board."""
    d = scratch()
    b, n, acts = board(d), seeded(d), actions()
    # the gh-watch action would shell out on every heartbeat; the walk is about
    # the stage graph, so it runs a harmless command instead
    acts = [a if a["name"] != "gh-watch" else engine.validate_action(
        {**a, "body": {"type": "run_command", "argv": ["/bin/true"]}}) for a in acts]

    def walk():
        return engine.tick(b, acts, n, d)

    # -- S0: a registered story is a command on the board ---------------------
    b.write("cadre", "stories", "story:nex-1", "command",
            {"target": "stage:intake", "story": "nex-1", "repo": "o/r",
             "slice": "", "pr": ""})
    spawns, _ = walk()
    assert [s["node"] for s in spawns] == ["intake"]
    assert spawns[0]["version"] == n.active("intake")["version"]
    assert "--story" in spawns[0]["argv"] and "nex-1" in spawns[0]["argv"]

    # once-only: the started signal the emitter wrote closes the guard
    b.write("cadre", "stories", "story:nex-1", "command",
            {"target": "stage:intake", "story": "nex-1", "repo": "o/r"})
    assert walk().spawns == []

    # -- intake finished: no spawn, the driver's merge is the next move -------
    stage_signal(b, stage="intake", status="finished")
    spawns, _ = walk()
    assert spawns == []
    phases = [e["payload"].get("phase") for e in b.peek(topic="stages", kind="signal")]
    assert "interrogate" in phases

    # -- the driver merges the planning PR (gh-watch reports it) --------------
    gh_signal(b, role="planning", state="merged", flow="contracts", pr=7)
    spawns, _ = walk()
    assert [s["node"] for s in spawns] == ["contracts"]

    # -- slice chain: contract -> tests -> build ------------------------------
    gh_signal(b, role="contract", state="merged", slice="core", pr=8)
    assert [s["node"] for s in walk().spawns] == ["tests"]
    gh_signal(b, role="tests", state="merged", slice="core", pr=9)
    spawns, _ = walk()
    assert [s["node"] for s in spawns] == ["build"]
    # the slice rode the whole way through in the payload
    started = [e for e in b.peek(topic="stages", kind="signal")
               if e["payload"].get("stage") == "build"]
    assert started[0]["payload"]["slice"] == "core"

    gh_signal(b, role="build", state="merged", slice="core", pr=10)
    assert walk().spawns == []              # a built slice is not a built story
    assert any(e["payload"]["message"] == "slice core built"
               for e in b.peek(topic="stages", kind="notify"))

    # -- the aggregate: every planned slice built -> assembly -----------------
    gh_signal(b, role="all-built", state="merged", pr=7)
    spawns, _ = walk()
    assert [s["node"] for s in spawns] == ["assembly"]
    gh_signal(b, role="all-built", state="merged", pr=7)
    assert walk().spawns == []              # guarded: assembly runs once

    # -- assembly finished -> the driver, on HITL -----------------------------
    stage_signal(b, stage="assembly", status="finished", pr=7)
    walk()
    hitl = [json.loads(l) for l in (d / "hitl-outbox.jsonl").read_text().splitlines()]
    assert len(hitl) == 1
    assert "Final review" in hitl[0]["summary"] and "nex-1" in hitl[0]["summary"]
    assert hitl[0]["detail"]["artifact"] == "final-nex-1.html"
    assert hitl[0]["key"] == "story:nex-1"

    # -- the human merges the final PR: the act that ships it -----------------
    gh_signal(b, role="final", state="merged", pr=11)
    assert walk().spawns == []
    assert any(e["payload"].get("phase") == "done"
               for e in b.peek(topic="stages", kind="signal"))


def test_risk_high_spawns_triage_and_requests_review():
    d = scratch()
    b, n = board(d), seeded(d)
    acts = [by_name("risk-high-to-triage")]

    # a green PR nobody graded high does nothing
    gh_signal(b, role="build", state="open", status="blocked", slice="core", pr=8,
              url="https://github.com/o/r/pull/8")
    assert engine.tick(b, acts, n, d).spawns == []

    b.write("cadre", "github", "story:nex-1", "signal",
            {"status": "blocked", "state": "open", "risk": "high", "story": "nex-1",
             "repo": "o/r", "role": "build", "slice": "core", "pr": 8,
             "url": "https://github.com/o/r/pull/8"})
    spawns, firings = engine.tick(b, acts, n, d)

    assert [s["node"] for s in spawns] == ["risk-triage"]
    assert spawns[0]["model"] == n.active("risk-triage")["model"]
    # the hold is a decision to make, not a PR to read: the review-requested
    # event the artifact surface consumes (replacing board_events.emit)
    review = b.peek(topic="review", kind="notify")
    assert len(review) == 1
    assert review[0]["payload"]["status"] == "awaiting-review"
    # templated payload fields come out as strings — `{payload[pr]}` is a
    # format call, not a copy. run_spawn_spec coerces the PR back to an int.
    assert review[0]["payload"]["pr"] == "8" and review[0]["payload"]["risk"] == "high"
    assert firings[0]["outcome"] == "fired"


def test_summons_route_to_interrogate_and_revise():
    d = scratch()
    b, n = board(d), seeded(d)
    acts = [by_name("summon-to-interrogate"), by_name("summon-to-revise")]

    for target, pr in (("stage:interrogate", 7), ("stage:revise", 9)):
        b.write("cadre", "github", "story:nex-1", "command",
                {"target": target, "story": "nex-1", "repo": "o/r", "pr": pr,
                 "role": "planning" if pr == 7 else "tests", "slice": ""})
    spawns, _ = engine.tick(b, acts, n, d)
    assert sorted(s["node"] for s in spawns) == ["interrogate", "revise"]


def test_stage_failure_reaches_a_human():
    d = scratch()
    b, n = board(d), seeded(d)
    acts = [by_name("stage-failed-to-driver")]

    stage_signal(b, stage="build", status="failed", slice="core")
    # and the engine's OWN failure shape, which carries no story/stage fields —
    # the summary must still render rather than failing into another failure
    b.write("cadre", "stages", "story:nex-2", "signal",
            {"status": "failed", "action": "tests-merged-to-build",
             "error": "boom", "trigger_event": "abc"})

    _, firings = engine.tick(b, acts, n, d)
    assert [f["outcome"] for f in firings] == ["fired", "fired"]
    lines = [json.loads(l) for l in (d / "hitl-outbox.jsonl").read_text().splitlines()]
    assert [l["key"] for l in lines] == ["story:nex-1", "story:nex-2"]


# --------------------------------------------------------------------------- seam


def test_adopt_story_writes_once_and_does_not_restart_live_stories():
    d = scratch()
    b = board(d)

    fresh = {"repo": "o/r", "status": "intaking", "phase": "interrogate", "title": "t"}
    assert len(engine_seam.adopt_story(b, "nex-1", fresh)) == 1
    assert engine_seam.adopt_story(b, "nex-1", fresh) == []      # once only

    # a story the legacy path already carried past intake is ADOPTED: the state
    # marker lands first so the intake action's once-only guard stays shut
    live = {"repo": "o/r", "status": "active", "phase": "slices", "planning_pr": 7}
    written = engine_seam.adopt_story(b, "nex-2", live)
    assert [e["kind"] for e in written] == ["signal", "command"]

    n = seeded(d)
    spawns, _ = engine.tick(b, [by_name("story-to-intake")], n, d)
    assert [s["key"] for s in spawns] == ["story:nex-1"]         # nex-2 not restarted


def test_spawn_spec_becomes_a_stage_run():
    """The engine hands back a spec; the daemon's existing spawn machinery runs
    it. This checks the translation, with _run_stage replaced by a recorder."""
    d = scratch()
    b, n = board(d), seeded(d)

    class FakeCfg:
        data_dir = d
        claude = {"bin": "claude"}

        def model_for(self, stage):
            return "opus"

        def effort_for(self, stage):
            return "high"

    class FakeReg:
        data = {"stories": {"nex-1": {"repo": "o/r", "status": "active"}}}

    gh_signal(b, role="tests", state="merged", slice="core", pr=9)
    spawns, _ = engine.tick(b, [by_name("tests-merged-to-build")], n, d)

    calls = []
    engine_seam.run_spawn_spec(FakeCfg(), FakeReg(), None, b, lambda *a: None,
                               lambda *a: calls.append(a), spawns[0])
    (_cfg, _reg, _ghc, slug, _story, action) = calls[0]
    assert slug == "nex-1"
    assert action["stage"] == "build" and action["slice"] == "core"
    assert action["pr"] == 9                       # a string PR becomes an int
    assert action["model"] == n.active("build")["model"]
    assert action["node_version"] == n.active("build")["version"]
    # the registry is authoritative at runtime: the ACTIVE prompt text travels
    assert action["prompt_template"] == n.active("build")["prompt"]


def test_stage_finished_is_a_noop_without_the_flag():
    import os
    d = scratch()

    class FakeCfg:
        data_dir = d
        claude = {"bin": "claude"}

        def model_for(self, stage):
            return "opus"

        def effort_for(self, stage):
            return "high"

    was = os.environ.pop("CADRE_ENGINE", None)
    try:
        assert engine_seam.stage_finished(FakeCfg(), "nex-1",
                                          {"stage": "build"}, True) is None
        os.environ["CADRE_ENGINE"] = "1"
        ev = engine_seam.stage_finished(
            FakeCfg(), "nex-1",
            {"stage": "build", "slice": "core", "pr": 9, "run_dir": "/x/run-7",
             "action": {"node_version": "abc"}}, False)
        assert ev["kind"] == "signal" and ev["key"] == "story:nex-1"
        assert ev["payload"]["status"] == "failed"     # both outcomes are written
        assert ev["payload"]["stage"] == "build" and ev["payload"]["slice"] == "core"
    finally:
        engine_seam.reset()
        os.environ.pop("CADRE_ENGINE", None)
        if was is not None:
            os.environ["CADRE_ENGINE"] = was


# --------------------------------------------------------------------------- gh-watch


def test_gh_watch_seen_state_is_its_own():
    """The legacy poller and the watcher observe the same repo while the flag is
    a flag; sharing a seen-set would mean whichever ran first blinded the other."""
    d = scratch()
    gh_watch.Seen(d / gh_watch.SEEN_FILE).save()  # baseline pass done
    seen = gh_watch.Seen(d / gh_watch.SEEN_FILE)
    assert seen.take("merged:o/r:7") is True
    assert seen.take("merged:o/r:7") is False
    seen.save()
    assert gh_watch.Seen(d / gh_watch.SEEN_FILE).take("merged:o/r:7") is False


def test_gh_watch_reports_merges_and_summons():
    d = scratch()
    b = board(d)

    class FakeGH:
        def pulls(self, repo, base=None, head=None, etag=True):
            if head:
                return [{"number": 20, "state": "open", "merged_at": None,
                         "head": {"ref": "feat/nex-1"}}]
            return [{"number": 7, "state": "closed", "merged_at": "now",
                     "head": {"ref": "pipe/nex-1/planning"}},
                    {"number": 9, "state": "open", "merged_at": None,
                     "head": {"ref": "pipe/nex-1/tests-core"}}]

        def pr(self, repo, number):
            if number == 7:
                return {"body": "```cadre-manifest\n"
                                '{"slices": [{"name": "core", "nodes": ["tests", "build"]}]}\n'
                                "```"}
            return {"body": "Risk: high", "mergeable": True,
                    "head": {"sha": "deadbeef"}}

        def issue_comments_since(self, repo, since):
            return [{"id": 101, "body": "@claude please revise",
                     "user": {"login": "BrandonPerez-Dev"},
                     "issue_url": "https://api.github.com/repos/o/r/issues/9"},
                    {"id": 102, "body": "@claude " + "<!-- pipeline-run -->",
                     "user": {"login": "bot"},
                     "issue_url": "https://api.github.com/repos/o/r/issues/9"}]

        def review_comments_since(self, repo, since):
            return []

    class FakeReg:
        data = {"stories": {"nex-1": {
            "repo": "o/r", "status": "active", "phase": "slices",
            "feature_branch": "feat/nex-1", "since": "2026-01-01T00:00:00Z",
            "planning_pr": 7, "slices": {}, "plan_slices": None}}}

        def stories(self, status="active"):
            return self.data["stories"]

    gh_watch.Seen(d / "seen.json").save()  # baseline pass done
    events = gh_watch.watch(b, FakeGH(), FakeReg(), gh_watch.Seen(d / "seen.json"))
    kinds = [(e["kind"], e["payload"].get("target") or e["payload"].get("role"))
             for e in events]

    assert ("signal", "planning") in kinds
    assert ("signal", "tests") in kinds          # the risk-high report
    assert ("command", "stage:revise") in kinds
    # the manifest declares no contract node -> the unified-spec branch
    planning = next(e for e in events if e["payload"].get("role") == "planning")
    assert planning["payload"]["flow"] == "slices"
    assert planning["payload"]["state"] == "merged"
    # the agent's own comment never buys a stage run (loop guard)
    assert sum(1 for k in kinds if k == ("command", "stage:revise")) == 1

    # and it is idempotent within one seen-file
    gh_watch.Seen(d / "seen2.json").save()  # baseline pass done
    seen = gh_watch.Seen(d / "seen2.json")
    gh_watch.watch(b, FakeGH(), FakeReg(), seen)
    assert gh_watch.watch(b, FakeGH(), FakeReg(), seen) == []


def test_ready_events_fill_the_contract_less_hole():
    """The first engine-only run stalled at phase->slices: no contract merge
    exists in the contract-less flow, so nothing could spawn tests. gh_watch's
    ready events fill exactly that hole — and ONLY that hole: a slice whose
    predecessor has a real PR keeps the merge-event fast path."""
    d = scratch()
    b = board(d)

    class FakeGH:
        def pulls(self, repo, base=None, head=None, etag=True):
            return []

        def issue_comments_since(self, repo, since):
            return []

        def review_comments_since(self, repo, since):
            return []

    story = {
        "repo": "o/r", "status": "active", "phase": "slices",
        "feature_branch": "feat/nex-1", "since": "2026-01-01T00:00:00Z",
        "planning_pr": 7,
        "plan_slices": [{"name": "a", "nodes": ["tests", "build"]},
                        {"name": "b", "nodes": ["tests", "build"]}],
        # what _seed_manifest leaves behind: contract exempt, no contract PR
        "slices": {"a": {"contract_merged": True},
                   "b": {"contract_merged": True}},
    }

    class FakeReg:
        data = {"stories": {"nex-1": story}}

        def stories(self, status="active"):
            return self.data["stories"]

    gh_watch.Seen(d / "seen.json").save()  # baseline pass done
    seen = gh_watch.Seen(d / "seen.json")
    events = gh_watch.watch(b, FakeGH(), FakeReg(), seen)
    ready = [e for e in events if e["payload"].get("state") == "ready"]
    assert {(e["payload"]["role"], e["payload"]["slice"]) for e in ready} \
        == {("tests", "a"), ("tests", "b")}

    # the engine turns each ready event into a tests spawn
    spawns, _ = engine.tick(b, actions(), seeded(d), d)
    assert [s["node"] for s in spawns].count("tests") == 2

    # idempotent: the next pass emits nothing new
    again = gh_watch.watch(b, FakeGH(), FakeReg(), seen)
    assert [e for e in again if e["payload"].get("state") == "ready"] == []

    # a slice with a REAL contract PR is the merge chain's business, not ours
    story["slices"]["c"] = {"contract_merged": True, "contract_pr": 5}
    story["plan_slices"].append({"name": "c", "nodes": ["tests", "build"]})
    more = gh_watch.watch(b, FakeGH(), FakeReg(), seen)
    assert [e for e in more if e["payload"].get("state") == "ready"] == []


# --------------------------------------------------------------------------- engine-only


def test_mode_parses_off_shadow_and_only():
    for value, expected in ((None, "off"), ("", "off"), ("0", "off"), ("off", "off"),
                            ("1", "shadow"), ("shadow", "shadow"),
                            ("only", "only"), ("ONLY", "only"), (" only ", "only")):
        with engine_mode(value):
            assert engine_seam.mode() == expected, value
            assert engine_seam.enabled() is (expected != "off")

    # an unrecognised truthy value reads as shadow, never as only: shadow's
    # failure mode is a dropped duplicate, only's is a story nobody dispatches
    with engine_mode("yes-please"):
        assert engine_seam.mode() == "shadow"


def test_engine_only_skips_legacy_stage_dispatch_but_keeps_registry_effects():
    """A story in a state the legacy path WOULD dispatch: `_poll_story` spawns
    nothing, the engine spawns the same stage from the same fact on the board,
    and the non-spawn dispatch effect (story_done) still lands in the registry."""
    import pipeline

    d = scratch()
    b, n = board(d), seeded(d)
    story = {"repo": "o/r", "status": "active", "phase": "slices",
             "feature_branch": "feat/nex-1", "planning_pr": 7, "plan_slices": [],
             "prs_cache": {}, "slices": {}, "active_runs": {}, "retry_events": [],
             "iterations": {"interrogate": 0, "revise": {}}}
    reg = FakeReg({"nex-1": story})
    cfg = FakeCfg(d)

    events = [{"kind": "pr_merged", "role": "tests", "slice": "core", "pr": 9},
              {"kind": "pr_merged", "role": "final", "pr": 11}]
    spawned, orig = [], pipeline._run_stage
    pipeline._run_stage = lambda *a, **kw: spawned.append(a[5])
    orig_collect, orig_avail = pipeline.poller.collect_events, pipeline.surface_mod.available
    pipeline.poller.collect_events = lambda *a: (list(events), 0)
    pipeline.surface_mod.available = lambda: False
    try:
        # legacy (flag off): the tests merge dispatches a build run
        pipeline._poll_story(cfg, reg, None, "nex-1", story)
        assert [a["stage"] for a in spawned] == ["build"]
        assert story["status"] == "done"          # story_done fired too

        spawned.clear()
        story["status"], story["phase"] = "active", "slices"   # same state, again
        assert pipeline.dispatcher.dispatch(story, events[0], cfg.limits)["type"] \
            == "run_stage", "the fixture must be a state legacy WOULD dispatch"
        with engine_mode("only"):
            pipeline._poll_story(cfg, reg, None, "nex-1", story)
        assert spawned == [], "engine-only must not spawn from the legacy dispatcher"
        # the registry effect is NOT a spawn and is not the engine's to make
        assert story["status"] == "done"
    finally:
        pipeline._run_stage = orig
        pipeline.poller.collect_events = orig_collect
        pipeline.surface_mod.available = orig_avail

    # ...and the same merge, as gh_watch puts it on the board, spawns build
    gh_signal(b, role="tests", state="merged", slice="core", pr=9)
    spawns, _ = engine.tick(b, [by_name("tests-merged-to-build")], n, d)
    assert [s["node"] for s in spawns] == ["build"]


def test_round_cap_refuses_at_the_cap_and_writes_an_escalate():
    """The interim cap (engine-only): the firing log is the counter, and the
    refusal is an escalate event, not a silent stop."""
    d = scratch()
    b, n = board(d), seeded(d)
    cfg = FakeCfg(d, max_rounds=2)
    acts = [by_name("summon-to-revise")]
    reg = FakeReg({"nex-1": {"repo": "o/r", "status": "active"}})
    calls = []

    def summon(pr):
        b.write("cadre", "github", "story:nex-1", "command",
                {"target": "stage:revise", "story": "nex-1", "repo": "o/r",
                 "pr": pr, "role": "tests", "slice": "core"})
        spawns, _ = engine.tick(b, acts, n, d)
        for spec in spawns:
            engine_seam.run_spawn_spec(cfg, reg, None, b, lambda *a: None,
                                       lambda *a: calls.append(a), spec)

    with engine_mode("only"):
        summon(9)
        summon(9)
        assert len(calls) == 2                  # both rounds ran
        summon(9)
        assert len(calls) == 2, "the third round is past the cap of 2"
        esc = b.peek(topic="stages", kind="escalate")
        assert len(esc) == 1
        assert "revise hit 2 rounds (cap 2)" in esc[0]["payload"]["reason"]
        assert esc[0]["key"] == "story:nex-1" and esc[0]["payload"]["pr"] == 9

        # the cap is per PR for revise — a different PR starts its own count
        summon(10)
        assert len(calls) == 3

    # and it reaches the driver rather than sitting on the board unread
    _, firings = engine.tick(b, [by_name("escalated-to-driver")], n, d)
    assert [f["outcome"] for f in firings] == ["fired"]
    hitl = [json.loads(l) for l in (d / "hitl-outbox.jsonl").read_text().splitlines()]
    assert "Pipeline escalation" in hitl[0]["summary"]


def test_round_cap_is_not_enforced_in_shadow_mode():
    """Shadow keeps the legacy dispatcher's cap; enforcing it here too would
    double-count the rounds."""
    d = scratch()
    b, n = board(d), seeded(d)
    spec = {"node": "revise", "action": "summon-to-revise", "key": "story:nex-1",
            "firing_id": 999}
    for _ in range(9):
        b.log_firing("summon-to-revise",
                     b.write("cadre", "github", "story:nex-1", "command",
                             {"target": "stage:revise", "pr": 9})["id"])
    with engine_mode("1"):
        assert engine_seam.refuse_past_cap(FakeCfg(d, 2), b, lambda *a: None,
                                           spec, "nex-1", 9) is False
    with engine_mode("only"):
        assert engine_seam.refuse_past_cap(FakeCfg(d, 2), b, lambda *a: None,
                                           spec, "nex-1", 9) is True


def test_story_text_rides_the_intake_command_into_the_prompt():
    """The gap adoption cannot close: the registry never held the story text, so
    it enters with the story and travels payload -> spawn spec -> rendered S0."""
    d = scratch()
    n = seeded(d)
    cfg, text = FakeCfg(d), "Add a --dry-run flag to the exporter."
    story = {"repo": "o/r", "status": "intaking", "title": "dry run",
             "variant": "change-spec"}
    reg = FakeReg({"nex-9": story})

    with engine_mode("only"):
        ev = engine_seam.intake_command(cfg, "nex-9", story, text,
                                        story_url="https://linear.app/x")
        assert ev["payload"]["story_text"] == text
        b = engine_seam.state(cfg)["board"]

        # the command already exists, so adoption adds nothing and cannot
        # overwrite it with a text-less one
        assert engine_seam.adopt_story(b, "nex-9", story) == []
        assert engine_seam.intake_command(cfg, "nex-9", story, "other") is None

        spawns, _ = engine.tick(b, [by_name("story-to-intake")], n, d)
        assert [s["node"] for s in spawns] == ["intake"]

        calls = []
        engine_seam.run_spawn_spec(cfg, reg, None, b, lambda *a: None,
                                   lambda *a: calls.append(a), spawns[0])
    action = calls[0][5]
    assert action["extra_vars"]["story_text"] == text
    assert action["extra_vars"]["story_url"] == "https://linear.app/x"
    # _run_stage renders the registry's ACTIVE template with those vars — the
    # story text has to survive that substitution, which is the whole point
    rendered = Template(action["prompt_template"]).safe_substitute(
        {"story_text": text, **action["extra_vars"]})
    assert text in rendered
    assert "$story_text" not in rendered


# --------------------------------------------------------------------------- preflight


def test_preflight_detects_a_missing_node():
    d = scratch()
    checks = {c.name: c for c in preflight.run(d, seed=True, skip_tools=True)}
    assert all(c.ok for c in checks.values()), \
        [(c.name, c.detail) for c in checks.values() if not c.ok]
    # 9 stages + the eval judge + workflow #3's `task` node. The task node
    # joined the count when the engine started LOADING actions-dialogue.json:
    # a deployment that has never had a task submitted has never run
    # `tasks.seed`, so without this row engine-only mode reads as ready while
    # the first dialogue command spawns a node that does not exist.
    assert "nodes active (11)" in checks
    assert "actions actions-dialogue.json" in checks
    assert checks["engine action set"].detail.endswith(
        "actions-pipeline.json, actions-dialogue.json")

    # drop one node from the registry and re-check WITHOUT re-seeding: a
    # deployment whose registry lost a node must not read as ready
    nodes = Nodes(d)
    del nodes.index["nodes"]["build"]
    nodes._save()
    again = preflight.run(d, seed=False, skip_tools=True)
    failed = [c for c in again if not c.ok]
    assert [c.name for c in failed] == ["nodes active (11)",
                                        "actions actions-pipeline.json"]
    assert "missing: build" in failed[0].detail
    assert "build" in failed[1].detail          # an action spawns a node that is gone
    assert "NOT safe to run" in preflight.render(again)

    # a node that exists but has no ACTIVE version is the other failure shape
    # seed_nodes can leave behind (a recorded version is not a promoted one)
    d2 = scratch()
    preflight.run(d2, seed=True, skip_tools=True)
    n2 = Nodes(d2)
    del n2.index["nodes"]["tests"]["active_version"]
    n2._save()
    rows = {c.name: c for c in preflight.run(d2, seed=False, skip_tools=True)}
    assert "no active version: tests" in rows["nodes active (11)"].detail


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pipeline-as-actions tests: all passed")


def test_first_run_is_a_silent_baseline():
    """First gh_watch run must observe silently: a fresh seen-file records
    history without emitting, so a redeploy can never replay old merges into
    live stage spawns (the 2026-09-10 stray-spawn incident)."""
    from runnerlib.gh_watch import Seen
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "gh-watch-seen.json"
        first = Seen(p)
        assert first.baseline
        assert not first.take("merged:r:1")   # recorded, not emitted
        first.save()
        second = Seen(p)
        assert not second.baseline
        assert not second.take("merged:r:1")  # already known
        assert second.take("merged:r:2")      # genuinely new -> emit


def test_surface_mirrored_driver_comment_is_a_summon():
    """The surface mirror posts the driver's words with no summon token — that
    comment must summon anyway, or the revise loop silently dead-ends (found
    live on the first engine-only run, 2026-09-11: feedback mirrored to the
    planning PR and nothing ever fired)."""
    from runnerlib import dispatcher, poller
    mirrored = f"{dispatcher.DRIVER_PREFIX} tighten slice 2's validation path"
    plain = "looks interesting, following along"
    tokened = "@claude please revisit the flow"
    marked = f"{dispatcher.DRIVER_PREFIX} x {dispatcher.AGENT_MARKER}"

    def summons(body):
        if dispatcher.AGENT_MARKER in body:
            return False
        return bool(poller.SUMMON_RE.search(body)
                    or body.startswith(dispatcher.DRIVER_PREFIX))

    assert summons(mirrored)
    assert summons(tokened)
    assert not summons(plain)      # a bystander comment still needs the token
    assert not summons(marked)     # agent-marked never summons

    # legacy path takes the same rule
    prs = {"7": {"role": "planning", "slice": None}}
    got = poller._classify_body(mirrored, "driver", 1, 7, prs, "issue_comment")
    assert got and got[0]["kind"] == "summon"
    assert poller._classify_body(plain, "someone", 2, 7, prs, "issue_comment") == []


# --------------------------------------------------------------------------- spec surface


def _spec_story(**kw):
    return {"repo": "o/r", "status": "active", "phase": "planning",
            "feature_branch": "feat/nex-1", "planning_pr": 7,
            "prs_cache": {"7": {"role": "planning", "state": "open",
                                "head": "cadre/nex-1/planning"}},
            "slices": {}, "plan_slices": [], "active_runs": {}, "retry_events": [],
            "iterations": {"interrogate": 0, "revise": {}}, **kw}


def _surface_file(cfg, slug, mtime):
    import pipeline
    p = pipeline.surface_mod._dir(cfg) / f"spec-{slug}.html"
    p.write_text("<html>round</html>")
    os.utime(p, (mtime, mtime))
    return p


def test_each_round_is_archived_and_handed_to_the_next_session():
    """intake/interrogate/revise share one out-path, so round N−1 only exists
    if the reap copied it aside before the next round overwrote it."""
    import pipeline

    d = scratch()
    cfg, story = FakeCfg(d), _spec_story()
    art = _surface_file(cfg, "nex-1", mtime=200)

    first = pipeline._archive_spec_round(cfg, "nex-1", story, {"started": 100})
    assert first.name == "spec-nex-1-r1.html"
    assert first.read_text() == "<html>round</html>"
    assert story["spec_surface_round"] == 1
    assert story["spec_surface_prev"] == str(first)
    assert story["spec_surface_run"] == 100   # the freshness clock is the run's start

    # the next round overwrites the shared path; r1 must survive untouched
    art.write_text("<html>round two</html>")
    os.utime(art, (400, 400))
    second = pipeline._archive_spec_round(cfg, "nex-1", story, {"started": 300})
    assert second.name == "spec-nex-1-r2.html"
    assert first.read_text() == "<html>round</html>"
    assert story["spec_surface_round"] == 2
    assert story["spec_surface_prev"] == str(second)


def test_a_stage_that_wrote_nothing_does_not_burn_a_round():
    """Only a run that authored the surface advances the counter — otherwise a
    silent stage would republish round N−1 as round N."""
    import pipeline

    d = scratch()
    cfg, story = FakeCfg(d), _spec_story()
    _surface_file(cfg, "nex-1", mtime=100)
    assert pipeline._archive_spec_round(cfg, "nex-1", story, {"started": 500}) is None
    assert story.get("spec_surface_round") is None
    assert story["spec_surface_run"] == 500     # the clock still advances


def test_missing_surface_is_not_an_archive_failure():
    import pipeline

    d = scratch()
    cfg, story = FakeCfg(d), _spec_story()
    pipeline.surface_mod._dir(cfg)
    assert pipeline._archive_spec_round(cfg, "nex-1", story, {"started": 10}) is None


def test_stale_authored_surface_is_not_reopened():
    """The audit's finding: the authored-spec reopen path had no freshness
    check, so the driver could be shown round 1's briefing as current."""
    import pipeline

    d = scratch()
    cfg = FakeCfg(d)
    story = _spec_story(spec_surface_run=500)
    _surface_file(cfg, "nex-1", mtime=100)          # older than the last spec run
    assert not pipeline._spec_surface_fresh(
        pipeline.surface_mod._dir(cfg) / "spec-nex-1.html", story)

    opened, authored = [], []

    class FakeGH:
        def pr(self, repo, num):
            return {"state": "open", "title": "plan", "body": "b",
                    "head": {"sha": "deadbeef", "ref": "cadre/nex-1/planning"}}

        def get(self, *a, **kw):
            return []

    orig_avail, orig_open = pipeline.surface_mod.available, pipeline.surface_mod.open_session
    pipeline.surface_mod.available = lambda: True
    pipeline.surface_mod.open_session = lambda cfg_, path, kind, log_, **m: opened.append(path)
    try:
        pipeline._ensure_spec_surface(cfg, FakeReg({"nex-1": story}), FakeGH(),
                                      "nex-1", story)
    finally:
        pipeline.surface_mod.available = orig_avail
        pipeline.surface_mod.open_session = orig_open

    assert [Path(p).name for p in opened] == ["spec-nex-1-pr7.html"], \
        "a stale briefing must fall back to the templated author path"

    # ...and a surface written since that run is current again
    _surface_file(cfg, "nex-1", mtime=900)
    assert pipeline._spec_surface_fresh(
        pipeline.surface_mod._dir(cfg) / "spec-nex-1.html", story)


def test_no_recorded_spec_run_keeps_the_authored_surface():
    """Legacy stories have no clock to fail against — absence is not staleness."""
    import pipeline

    d = scratch()
    cfg, story = FakeCfg(d), _spec_story()
    _surface_file(cfg, "nex-1", mtime=1)
    assert pipeline._spec_surface_fresh(
        pipeline.surface_mod._dir(cfg) / "spec-nex-1.html", story)


def test_surface_prev_reaches_the_next_spec_session():
    """$surface_prev is what lets round N say what changed since round N−1."""
    import pipeline
    from runnerlib import runs as runs_mod

    d = scratch()
    cfg = FakeCfg(d)
    cfg.commit_identity = {"name": "cadre", "email": "c@x"}
    cfg.skills_source = d / "skills"
    cfg.workflow_skills_dir = None
    cfg.workflows_dir = ""
    cfg.claude = {"bin": "claude", "permission_mode": "bypassPermissions",
                  "timeout_seconds": 60}
    story = _spec_story(spec_surface_prev=str(d / "surfaces" / "spec-nex-1-r1.html"),
                        title="t", variant="change-spec", story_id="NEX-1", board={})
    prompts = []

    class FakeRuns:
        RunsBusy = runs_mod.RunsBusy

        @staticmethod
        def key_active(*a):
            return False

        @staticmethod
        def all_active(*a):
            return []

        @staticmethod
        def branch_held(*a):
            return False

        @staticmethod
        def add_worktree(*a):
            return None

        @staticmethod
        def remove_worktree(*a):
            return True, ""

        @staticmethod
        def spawn(bin_, prompt, *a, **kw):
            prompts.append(prompt)
            return 4242

    saved = (pipeline.runs_mod, pipeline.claude_run.ensure_checkout,
             pipeline.claude_run.install_skills, pipeline._run_branch)
    pipeline.runs_mod = FakeRuns
    pipeline.claude_run.ensure_checkout = lambda *a, **kw: None
    pipeline.claude_run.install_skills = lambda *a, **kw: None
    pipeline._run_branch = lambda *a, **kw: ("cadre/nex-1/planning", "origin/main")
    try:
        for stage, expected in (("interrogate", story["spec_surface_prev"]),
                                ("build", "")):
            prompts.clear()
            pipeline._run_stage(cfg, FakeReg({"nex-1": story}), None, "nex-1", story,
                                {"type": "run_stage", "stage": stage,
                                 "prompt_template": "prev=[$surface_prev]"})
            assert prompts == [f"prev=[{expected}]"], stage
    finally:
        (pipeline.runs_mod, pipeline.claude_run.ensure_checkout,
         pipeline.claude_run.install_skills, pipeline._run_branch) = saved


def test_diff_primitive_is_in_the_theme_and_the_allowed_vocabulary():
    """Sessions may not style anything, so a diff is unauthorable unless the
    theme carries it AND _common.md names it as permitted."""
    css = (ROOT / "surface-theme.css").read_text()
    for rule in ("pre.diff", ".add", ".del", ".diff-caption"):
        assert rule in css, rule
    common = (PROMPTS / "_common.md").read_text()
    for token in ('<pre class="diff">', 'class="add"', 'class="del"', "diff-caption"):
        assert token in common, token
    # the deployed copy statusd serves must not drift from the source
    assert (ROOT / ".review-surface" / "surface-theme.css").read_text() == css


# --------------------------------------------------------------------------- agent command


def _spawn_harness(d, executed):
    """pipeline._run_stage with everything but the spawn stubbed out, and the
    real `runs.spawn` running against a recording Popen. What lands in
    `executed` is the argv the operating system would have been handed."""
    import pipeline
    from runnerlib import runs as runs_mod

    class RecordingPopen:
        pid = 4242

        def __init__(self, args, **kw):
            executed.append(args)

    class Runs:
        RunsBusy = runs_mod.RunsBusy
        key_active = staticmethod(lambda *a: False)
        all_active = staticmethod(lambda *a: [])
        branch_held = staticmethod(lambda *a: False)
        add_worktree = staticmethod(lambda *a: None)
        remove_worktree = staticmethod(lambda *a: (True, ""))
        spawn = staticmethod(runs_mod.spawn)

    cfg = FakeCfg(d)
    cfg.commit_identity = {}
    cfg.skills_source = d / "skills"
    cfg.workflow_skills_dir = None
    cfg.workflows_dir = ""
    saved = (pipeline.runs_mod, pipeline.claude_run.ensure_checkout,
             pipeline.claude_run.install_skills, pipeline._run_branch,
             runs_mod.subprocess.Popen)
    pipeline.runs_mod = Runs
    pipeline.claude_run.ensure_checkout = lambda *a, **kw: None
    pipeline.claude_run.install_skills = lambda *a, **kw: None
    pipeline._run_branch = lambda *a, **kw: ("cadre/nex-1/build-core", "origin/main")
    runs_mod.subprocess.Popen = RecordingPopen

    def restore():
        (pipeline.runs_mod, pipeline.claude_run.ensure_checkout,
         pipeline.claude_run.install_skills, pipeline._run_branch,
         runs_mod.subprocess.Popen) = saved

    return pipeline, cfg, restore


def test_the_nodes_command_is_the_command_that_runs():
    """The portability claim: swap the agent CLI by editing the node.

    A node whose command names `my-agent` is followed all the way to the
    process — engine body -> spawn spec -> _run_stage -> runs.spawn — with no
    Claude Code flag surviving anywhere on the way. The legacy path (no node
    spec) still composes the flags it always did.
    """
    d = scratch()
    b, n = board(d), seeded(d)
    # the ONE edit: this node is invoked by another CLI, with its own grammar
    n.register("build", n.active("build")["prompt"], "opus",
               "my-agent --file {prompt} {session} {permission} "
               "--task {payload[story]}", replace=True)

    gh_signal(b, role="tests", state="merged", slice="core", pr=9)
    spawns, _ = engine.tick(b, [by_name("tests-merged-to-build")], n, d)
    # the registry's command is in the spec, with the spawn-time placeholders
    # still unresolved — they name values that do not exist until a process does
    assert spawns[0]["argv"][:3] == ["my-agent", "--file", "{prompt}"]
    assert "{session}" in spawns[0]["argv"] and "{permission}" in spawns[0]["argv"]

    story = _spec_story(title="t", variant="change-spec", story_id="NEX-1", board={})
    reg = FakeReg({"nex-1": story})
    executed = []
    pipeline, cfg, restore = _spawn_harness(d, executed)
    try:
        engine_seam.run_spawn_spec(cfg, reg, None, b, lambda *a: None,
                                   pipeline._run_stage, spawns[0])
        run = list(story["active_runs"].values())[0]
        # the wrapper shell that captures stdout/stderr/exit is the daemon's,
        # not the node's — the node's command starts after it
        assert executed[0][:2] == ["/bin/sh", "-c"] and executed[0][3] == "sh"
        argv = executed[0][4:]

        assert argv[:2] == ["timeout", str(cfg.claude["timeout_seconds"])]
        assert argv[2:4] == ["my-agent", "--file"]
        # the rendered prompt travels as ONE argument, whatever is in it
        assert argv[4] == (Path(run["run_dir"]) / "prompt.txt").read_text()
        # {session} became the flag pair this run's session needs, {permission}
        # the config's permission mode — expansions, not one token each
        assert argv[5:7] == ["--session-id", run["session_id"]]
        assert argv[7:] == ["--dangerously-skip-permissions", "--task", "nex-1"]
        # nothing Claude Code-shaped survives in the invocation itself (the
        # prompt is prose and may say anything)
        assert not any("claude" in a for a in argv[:4] + argv[5:]), argv

        # ...and the legacy path — no node spec — is untouched
        executed.clear()
        story["active_runs"] = {}
        pipeline._run_stage(cfg, reg, None, "nex-1", story,
                            {"type": "run_stage", "stage": "build", "slice": "core"})
        legacy = executed[0][4:]
        assert legacy[:4] == ["timeout", "7200", "claude", "-p"]
        assert legacy[5:11] == ["--model", "opus", "--effort", "high",
                                "--output-format", "json"]
        assert legacy[-1] == "--dangerously-skip-permissions"
    finally:
        restore()


def test_a_revived_session_re_enters_through_the_same_command():
    """Revival is the other half of the contract: the node's command carries
    `{session}`, so resuming a stopped session is the same command with
    `--resume` instead of `--session-id` — not a second, claude-shaped argv."""
    from runnerlib import runs as runs_mod

    argv = ["my-agent", "--file", "{prompt}", "{session}", "{permission}"]
    fresh = runs_mod.expand_spawn_argv(argv, {
        "prompt": "do the thing", "session": runs_mod.session_args("sid-1", False),
        "permission": runs_mod.permission_args("bypass")})
    revived = runs_mod.expand_spawn_argv(argv, {
        "prompt": "carry on", "session": runs_mod.session_args("sid-1", True),
        "permission": runs_mod.permission_args("acceptEdits")})

    assert fresh == ["my-agent", "--file", "do the thing",
                     "--session-id", "sid-1", "--dangerously-skip-permissions"]
    assert revived == ["my-agent", "--file", "carry on",
                       "--resume", "sid-1", "--permission-mode", "acceptEdits"]
