"""Surface feedback parsing: `poll --json` emits one JSON line; tokens are
authoritative, everything else is driver feedback. The old TOON parsing lost
real feedback twice (digit-only uid assumption, unquoted bare cells) — JSON
exists so that class of bug cannot recur."""

import json
import unittest
from pathlib import Path

from runnerlib import dispatcher, surface
from runnerlib.surface import _classify_prompts, _parse_feedback


def poll_line(status, prompts):
    return json.dumps({"file": "/x/a.html", "status": status,
                       "prompts": prompts}) + "\n"


class ParseTest(unittest.TestCase):
    def test_decision_and_free_text(self):
        raw = poll_line("feedback", [
            {"uid": "1", "prompt": "CADRE_DECISION gate=risk_hold story=nex-158 "
                                   "pr=124 verdict=approve", "tag": "choice"},
            {"uid": "2", "prompt": "this section worries me a little", "tag": "div"},
        ])
        payload, structured, free = _parse_feedback(raw)
        self.assertEqual(payload["status"], "feedback")
        self.assertEqual(structured, [{"type": "decision", "gate": "risk_hold",
                                       "story": "nex-158", "pr": 124,
                                       "verdict": "approve", "uid": "1",
                                       "selector": "", "tag": "choice",
                                       "anchor": ""}])
        self.assertEqual(free, [{"text": "this section worries me a little",
                                 "uid": "2", "selector": "", "tag": "div",
                                 "anchor": ""}])

    def test_answer_multiline_and_commas(self):
        raw = poll_line("feedback", [
            {"uid": "", "prompt": "CADRE_ANSWER ticket=nex-151-ab12cd34 :: "
                                  "go with the flow registry shape,\nsecond line"},
        ])
        _, structured, free = _parse_feedback(raw)
        self.assertEqual(structured[0]["ticket"], "nex-151-ab12cd34")
        self.assertIn("second line", structured[0]["text"])
        self.assertEqual(free, [])

    def test_non_json_raw_is_none(self):
        payload, structured, free = _parse_feedback("session:\n  status: opened\n")
        self.assertIsNone(payload)
        self.assertEqual((structured, free), ([], []))

    def test_classify_only_nonempty(self):
        structured, free = _classify_prompts([{"prompt": ""}, {"prompt": "  "},
                                              {"prompt": "real note"}])
        self.assertEqual(structured, [])
        self.assertEqual([f["text"] for f in free], ["real note"])

    def test_annotation_anchor_survives(self):
        """The audit's finding: the payload carries uid/selector/tag/text and
        only `prompt` was kept, so the agent got a floating sentence."""
        raw = poll_line("feedback", [
            {"uid": "7", "prompt": "this contract is too loose", "tag": "li",
             "selector": "section:nth-of-type(3) li:nth-child(2)",
             "text": "the parser accepts any well-formed row"},
        ])
        _, _, free = _parse_feedback(raw)
        self.assertEqual(free, [{
            "text": "this contract is too loose", "uid": "7", "tag": "li",
            "selector": "section:nth-of-type(3) li:nth-child(2)",
            "anchor": "the parser accepts any well-formed row"}])

    def test_answer_carries_its_anchor(self):
        raw = poll_line("feedback", [
            {"uid": "3", "prompt": "CADRE_ANSWER ticket=nex-1-ab :: go with B",
             "tag": "choice", "selector": "form", "text": "Which storage shape?"},
        ])
        _, structured, _ = _parse_feedback(raw)
        self.assertEqual(structured[0]["anchor"], "Which storage shape?")
        self.assertEqual(structured[0]["selector"], "form")


class QuoteTest(unittest.TestCase):
    """The anchor rides the PR mirror as a blockquote, on its own budget."""

    def test_quote_is_a_blockquote_on_every_line(self):
        self.assertEqual(surface._quote("one\ntwo"), "> one\n> two\n\n")

    def test_no_anchor_means_no_quote(self):
        self.assertEqual(surface._quote(""), "")

    def test_anchor_truncates_separately_from_the_driver_text(self):
        long_anchor = "x" * 900
        driver_text = "y" * 1400
        quoted = surface._quote(long_anchor)
        self.assertLessEqual(len(quoted), surface.ANCHOR_LIMIT + 8)
        # the driver's own 1500-char budget is untouched by a huge anchor
        body = f"{quoted}{driver_text[:1500]}"
        self.assertIn("y" * 1400, body)


class MirrorTest(unittest.TestCase):
    """What actually reaches the board and the PR for one annotated note."""

    def _consume(self, prompts, meta):
        import tempfile
        import types
        from pathlib import Path
        events, comments = [], []

        class FakeGH:
            def comment(self, repo, pr, body):
                comments.append((repo, pr, body))

        with tempfile.TemporaryDirectory() as td:
            cfg = types.SimpleNamespace(data_dir=Path(td))
            orig_emit, orig_cli = surface.board_events.emit, surface._run_cli
            surface.board_events.emit = lambda kind, **kw: events.append((kind, kw))
            surface._run_cli = lambda *a, **kw: poll_line("feedback", prompts)
            try:
                surface._consume(cfg, None, FakeGH(), lambda m: None,
                                 str(Path(td) / "a.html"), meta)
            finally:
                surface.board_events.emit = orig_emit
                surface._run_cli = orig_cli
        return events, comments

    def test_board_event_and_pr_comment_carry_the_anchor(self):
        events, comments = self._consume(
            [{"uid": "9", "prompt": "narrow this", "tag": "p", "selector": "p#x",
              "text": "any well-formed row is accepted"}],
            {"story": "nex-1", "pr": 12, "repo": "o/r"})
        kinds = dict((k, v) for k, v in events)
        self.assertEqual(kinds["surface_feedback"]["anchor"],
                         "any well-formed row is accepted")
        self.assertEqual(kinds["surface_feedback"]["selector"], "p#x")
        body = comments[0][2]
        self.assertTrue(body.startswith(dispatcher.DRIVER_PREFIX),
                        "the mirror must still read as a driver summon")
        self.assertIn("> any well-formed row is accepted", body)
        self.assertIn("narrow this", body)


class AgentReplyTest(unittest.TestCase):
    """Replies go back onto the surface, and the poll they ride must not eat
    feedback the driver had already queued."""

    def _cfg(self, td, open_session=True):
        import types
        from pathlib import Path
        cfg = types.SimpleNamespace(data_dir=Path(td))
        art = surface._dir(cfg) / "spec-nex-1.html"
        art.write_text("<html></html>")
        surface._save_sessions(cfg, {str(art): {"kind": "spec_review",
                                                "open": open_session,
                                                "story": "nex-1"}})
        return cfg, art

    def test_posts_the_reply_and_drains_queued_feedback(self):
        import tempfile
        calls, events = [], []

        def fake_cli(args, timeout=25):
            calls.append(args)
            return poll_line("feedback", [{"uid": "1", "prompt": "one more thing",
                                           "tag": "p"}])

        with tempfile.TemporaryDirectory() as td:
            cfg, art = self._cfg(td)
            orig_cli, orig_avail = surface._run_cli, surface.available
            orig_emit = surface.board_events.emit
            surface._run_cli = fake_cli
            surface.available = lambda: True
            surface.board_events.emit = lambda kind, **kw: events.append((kind, kw))
            try:
                sent = surface.agent_reply(cfg, None, None, lambda m: None,
                                           art, "round 2 done")
            finally:
                surface._run_cli, surface.available = orig_cli, orig_avail
                surface.board_events.emit = orig_emit

        self.assertTrue(sent)
        self.assertIn("--agent-reply", calls[0])
        self.assertIn("round 2 done", calls[0])
        self.assertIn("--timeout-ms", calls[0])  # must never block the daemon
        self.assertIn("surface_feedback", [k for k, _ in events])

    def test_closed_session_and_missing_cli_are_no_ops(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cfg, art = self._cfg(td, open_session=False)
            orig_avail = surface.available
            surface.available = lambda: True
            try:
                self.assertFalse(surface.agent_reply(cfg, None, None,
                                                     lambda m: None, art, "hi"))
                surface.available = lambda: False
                self.assertFalse(surface.agent_reply(cfg, None, None,
                                                     lambda m: None, art, "hi"))
            finally:
                surface.available = orig_avail

    def test_cli_failure_never_raises(self):
        import tempfile

        def boom(args, timeout=25):
            raise OSError("no such session")

        with tempfile.TemporaryDirectory() as td:
            cfg, art = self._cfg(td)
            orig_cli, orig_avail = surface._run_cli, surface.available
            surface._run_cli, surface.available = boom, lambda: True
            try:
                self.assertFalse(surface.agent_reply(cfg, None, None,
                                                     lambda m: None, art, "hi"))
            finally:
                surface._run_cli, surface.available = orig_cli, orig_avail


class TemplateTest(unittest.TestCase):
    """The onsubmit attribute is double-quoted HTML: any double quote inside
    it truncates the handler and the form falls back to a GET navigation —
    the real form did exactly that on fedora-1 while curl tests passed."""

    def _artifacts(self, tmp):
        import types
        from runnerlib import surface
        cfg = types.SimpleNamespace(data_dir=tmp)
        hold = surface.author_risk_hold(
            cfg, "nex-158", 123, {"title": "t", "body": "Risk: high\nbecause"},
            "contracts", files=[{"filename": "a.py", "additions": 1, "deletions": 2}])
        ask = surface.author_question(cfg, {"ticket": "nex-1-abc", "story": "nex-1",
                                            "question": "q?", "recommendation": "r"})
        spec = surface.author_spec_review(
            cfg, "nex-158", 78, {"title": "plan", "body": "narrative"},
            [{"path": "spec/a.md", "text": "# slice a\ncontract"}])
        return hold.read_text(), ask.read_text(), spec.read_text()

    def test_onsubmit_attributes_have_no_double_quotes(self):
        import re
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            for html in self._artifacts(Path(td)):
                for m in re.finditer(r'onsubmit="([^"]*)"', html):
                    handler = m.group(1)
                    self.assertIn("queuePrompt", handler,
                                  "handler truncated by an embedded double quote")
                    self.assertIn("event.preventDefault()", handler)


if __name__ == "__main__":
    unittest.main()


def test_open_session_prefers_the_quiet_http_path(tmp_path, monkeypatch):
    """The daemon must not pop a browser tab for every surface it opens: the
    running server gets a plain POST, and the CLI (which opens a tab) is only
    the cold-start fallback."""
    from runnerlib import surface as sf
    calls = {"cli": 0, "http": 0}
    monkeypatch.setattr(sf, "_create_session_quietly",
                        lambda p: (calls.__setitem__("http", calls["http"] + 1)
                                   or "/session/abc123"))
    monkeypatch.setattr(sf, "_run_cli",
                        lambda a, timeout=25: (calls.__setitem__("cli", calls["cli"] + 1)
                                               or 'url: "http://x/session/zzz"'))
    monkeypatch.setattr(sf.board_events, "emit", lambda *a, **k: None)

    class Cfg:
        data_dir = tmp_path
    art = tmp_path / "page.html"
    art.write_text("<!doctype html>")
    sf.open_session(Cfg(), art, "task", lambda *a: None)
    assert calls == {"http": 1, "cli": 0}
    sess = sf.sessions(Cfg())
    assert sess[str(art)]["key"] == "abc123"


# ------------------------------------------------------------ stranded guard

def _strand_consume(tmp_path, monkeypatch, meta, prompts, status="feedback"):
    """One consume pass on a registered session; returns (events, cfg)."""
    from runnerlib import surface as sf
    events = []
    monkeypatch.setattr(sf.board_events, "emit",
                        lambda kind, **kw: events.append((kind, kw)))
    monkeypatch.setattr(sf, "_run_cli",
                        lambda *a, **kw: poll_line(status, prompts))

    class Cfg:
        data_dir = tmp_path
    art = tmp_path / "hitl-d7-workspace.html"
    art.write_text("<!doctype html>")
    sf._save_sessions(Cfg(), {str(art): {"open": True, **meta}})
    sf._consume(Cfg(), None, None, lambda m: None, str(art), meta)
    return events, Cfg(), str(art)


def test_feedback_with_no_task_and_no_pr_is_dead_lettered_verbatim(tmp_path, monkeypatch):
    """The black-hole page (three discussion pages, 2026-09): an external
    session with no task and no PR had its feedback consumed into board events
    nobody reads. The whole batch must land verbatim in a dead-letter file and
    a board event must name that file."""
    prompts = [{"uid": "4", "prompt": "Option B, but keep the fold per project",
                "tag": "p", "text": "Which grouping?"},
               {"uid": "", "prompt": "and a second, longer answer " + "x" * 3000}]
    events, cfg, art = _strand_consume(
        tmp_path, monkeypatch, {"kind": "external", "project": "hitl",
                                "title": "d7 — workspace"}, prompts)
    from runnerlib import surface as sf
    stranded = [kw for k, kw in events if k == "surface_stranded"]
    assert len(stranded) == 1
    dead = Path(stranded[0]["dead_letter"])
    assert dead.parent == sf.stranded_dir(cfg) and dead.exists()
    rec = json.loads(dead.read_text())
    assert rec["prompts"] == prompts          # verbatim — the 3000-char tail too
    assert rec["artifact"] == art and "no PR" in rec["reason"]
    assert rec["raw"].startswith("{")
    # the existing board record still fires; the guard adds, never replaces
    assert "surface_feedback" in [k for k, _ in events]
    # and the fleet can see it
    row = sf.status_list(cfg)[0]
    assert row["stranded"] == [dead.name]


def test_routed_feedback_is_not_stranded(tmp_path, monkeypatch):
    """A PR-mirrored page and a token-only batch both have an owner."""
    from runnerlib import surface as sf

    class GH:
        def comment(self, *a):
            pass
    events = []
    monkeypatch.setattr(sf.board_events, "emit",
                        lambda kind, **kw: events.append(kind))
    monkeypatch.setattr(sf, "_run_cli", lambda *a, **kw: poll_line(
        "feedback", [{"prompt": "narrow this"}]))

    class Cfg:
        data_dir = tmp_path
    sf._consume(Cfg(), None, GH(), lambda m: None, str(tmp_path / "a.html"),
                {"kind": "spec_review", "story": "s", "pr": 3, "repo": "o/r"})
    assert "surface_stranded" not in events
    assert not sf.stranded_dir(Cfg()).exists() or not any(sf.stranded_dir(Cfg()).iterdir())

    # decision/answer tokens carry their own routing, so no strand either
    assert sf._strand_reason({"kind": "ask"}, []) == ""
    assert sf._strand_reason({"kind": "external"}, [{"text": "  "}]) == ""


def test_task_pages_strand_only_without_a_task_id():
    from runnerlib import surface as sf
    assert sf._strand_reason({"kind": "task", "task": "t-1"}, [{"text": "x"}]) == ""
    assert "no task id" in sf._strand_reason({"kind": "task"}, [])
    # a notice has a story but nothing mirrors its notes — the ask's rule
    # (no task id, no PR) calls that stranded too
    assert sf._strand_reason({"kind": "notice", "story": "s"}, [{"text": "x"}])


def test_strand_outlives_the_session_until_the_file_is_removed(tmp_path, monkeypatch):
    """'Send & End' is how a final batch gets stranded: the session closes in
    the same poll. The badge must survive that, and deleting the dead letter
    (the driver has routed it) is what clears it — no new state anywhere."""
    events, cfg, art = _strand_consume(
        tmp_path, monkeypatch, {"kind": "external", "project": "hitl"},
        [{"prompt": "last word"}], status="ended")
    from runnerlib import surface as sf
    assert sf.sessions(cfg)[art]["open"] is False
    rows = sf.status_list(cfg)
    assert len(rows) == 1 and rows[0]["stranded"]
    (sf.stranded_dir(cfg) / rows[0]["stranded"][0]).unlink()
    assert sf.status_list(cfg) == []


def test_dead_letter_write_failure_never_raises(tmp_path, monkeypatch):
    from runnerlib import surface as sf
    monkeypatch.setattr(sf, "stranded_dir", lambda cfg: (_ for _ in ()).throw(OSError("ro")))
    logs = []

    class Cfg:
        data_dir = tmp_path
    assert sf._dead_letter(Cfg(), logs.append, "/x/a.html", {}, "{}", {}, "r") is None
    assert "dead-letter write failed" in logs[0]


# ------------------------------------------------------- adoption of orphans

def _adopt_run(tmp_path, monkeypatch, meta, status, prompts, submit=None):
    """One tick-time adoption pass over a single registered session."""
    from runnerlib import surface as sf
    from runnerlib import tasks as tasks_mod
    events, submitted = [], []
    monkeypatch.setattr(sf.board_events, "emit",
                        lambda kind, **kw: events.append((kind, kw)))
    monkeypatch.setattr(sf, "agent_status", lambda key: status)
    monkeypatch.setattr(sf, "_run_cli", lambda *a, **kw: poll_line("feedback", prompts))
    monkeypatch.setattr(tasks_mod, "submit_task", submit or (
        lambda cfg, text, cwd=None, title=None:
        (submitted.append({"text": text, "cwd": cwd}) or "task-adopted-1")))

    class Reg:
        data = {"stories": {}}
        saved = False

        def save(self):
            Reg.saved = True

    class Cfg:
        data_dir = tmp_path
    art = tmp_path / "hitl-d1-topology.html"
    art.write_text("<!doctype html>")
    sf._save_sessions(Cfg(), {str(art): {"open": True, "key": "abc", **meta}})
    sf._adopt_unowned(Cfg(), Reg(), lambda m: None, sf.sessions(Cfg()))
    return events, submitted, Cfg(), str(art), Reg


def test_queued_feedback_with_no_listener_gets_a_dialogue_automatically(tmp_path, monkeypatch):
    """The driver's rule: the system recognises this itself. A page registered
    presence-only collects answers no loop is waiting for — 'queued, no agent
    listening'. The daemon must consume that batch once and dispatch a
    dialogue that owns the page from then on, with no human routing it."""
    events, submitted, cfg, art, Reg = _adopt_run(
        tmp_path, monkeypatch,
        {"kind": "external", "project": "hitl", "title": "d1 — topology"},
        {"status": "open", "presence": "waiting", "pending_prompts": 2},
        [{"uid": "1", "prompt": "Option B — own store", "tag": "choice",
          "text": "Where does state live?"},
         {"uid": "2", "prompt": "and ship the thin signals first"}])
    from runnerlib import surface as sf

    assert len(submitted) == 1
    ask = submitted[0]["text"]
    assert art in ask                       # the page it must rewrite in place
    assert "Option B — own store" in ask    # the driver's words travel with it
    assert "ship the thin signals first" in ask
    assert "Where does state live?" in ask  # ...under what they were attached to
    assert submitted[0]["cwd"] == str(tmp_path)

    row = sf.sessions(cfg)[art]
    assert row["kind"] == "task" and row["task"] == "task-adopted-1"
    assert Reg.saved                        # page ownership survives a restart
    assert [kw for k, kw in events if k == "surface_adopted"]


def test_adoption_leaves_owned_and_quiet_sessions_alone(tmp_path, monkeypatch):
    """Polling consumes, so adoption must fire ONLY on the stranded state: a
    page someone owns, or one with nothing queued, must never be touched."""
    for meta, status in (
            ({"kind": "task", "task": "t-1"},
             {"status": "open", "presence": "waiting", "pending_prompts": 3}),
            ({"kind": "external"},
             {"status": "open", "presence": "working", "pending_prompts": 1}),
            ({"kind": "external"},
             {"status": "open", "presence": "waiting", "pending_prompts": 0}),
            ({"kind": "external"}, {"status": "ended", "pending_prompts": 4}),
            ({"kind": "external"}, None)):          # server unreachable
        _, submitted, _, _, _ = _adopt_run(
            tmp_path, monkeypatch, meta, status, [{"prompt": "hi"}])
        assert submitted == [], (meta, status)


def test_a_consumed_batch_is_never_lost_when_dispatch_fails(tmp_path, monkeypatch):
    """Adoption consumes before it dispatches. If the dispatch then fails, the
    driver's words must land in the dead-letter file rather than evaporate."""
    def boom(*a, **kw):
        raise RuntimeError("board down")

    events, submitted, cfg, art, _ = _adopt_run(
        tmp_path, monkeypatch, {"kind": "external"},
        {"status": "open", "presence": "waiting", "pending_prompts": 1},
        [{"prompt": "my answer"}], submit=boom)
    strand = [kw for k, kw in events if k == "surface_stranded"]
    assert strand and "adoption dispatch failed" in strand[0]["reason"]
    assert "my answer" in Path(strand[0]["dead_letter"]).read_text()


def test_reopening_a_page_keeps_where_it_belongs(tmp_path, monkeypatch):
    """A page's project, title and role outlive the turn that opens it. An
    adopted discussion used to fall out of its project group and render as a
    bare task id the moment its new dialogue wrote a round."""
    from runnerlib import surface as sf
    monkeypatch.setattr(sf, "_create_session_quietly", lambda p: "/session/k1")
    monkeypatch.setattr(sf.board_events, "emit", lambda *a, **kw: None)

    class Cfg:
        data_dir = tmp_path
    art = tmp_path / "hitl-d1-topology.html"
    art.write_text("<!doctype html>")
    sf._save_sessions(Cfg(), {str(art): {
        "kind": "external", "open": True, "project": "hitl",
        "title": "d1 — topology", "role": "discussion"}})

    sf.open_session(Cfg(), art, "task", lambda m: None, task="task-1")
    row = sf.sessions(Cfg())[str(art)]
    assert row["project"] == "hitl" and row["title"] == "d1 — topology"
    assert row["role"] == "discussion"
    assert row["kind"] == "task" and row["task"] == "task-1"

    # an explicit new title still wins — carrying is a fallback, not a lock
    sf.open_session(Cfg(), art, "task", lambda m: None, title="d1 (DECIDED)")
    assert sf.sessions(Cfg())[str(art)]["title"] == "d1 (DECIDED)"
