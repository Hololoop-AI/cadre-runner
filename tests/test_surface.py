"""Surface feedback parsing: `poll --json` emits one JSON line; tokens are
authoritative, everything else is driver feedback. The old TOON parsing lost
real feedback twice (digit-only uid assumption, unquoted bare cells) — JSON
exists so that class of bug cannot recur."""

import json
import unittest

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
