"""Surface feedback parsing: `poll --json` emits one JSON line; tokens are
authoritative, everything else is driver feedback. The old TOON parsing lost
real feedback twice (digit-only uid assumption, unquoted bare cells) — JSON
exists so that class of bug cannot recur."""

import json
import unittest

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
                                       "verdict": "approve"}])
        self.assertEqual(free, ["this section worries me a little"])

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
        structured, free = _classify_prompts(["", "  ", "real note"])
        self.assertEqual(structured, [])
        self.assertEqual(free, ["real note"])


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
        return hold.read_text(), ask.read_text()

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
