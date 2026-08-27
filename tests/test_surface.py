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


if __name__ == "__main__":
    unittest.main()
