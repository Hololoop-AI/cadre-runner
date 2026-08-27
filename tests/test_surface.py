"""Surface feedback parsing: the poll output is TOON in two observed shapes
(inline CSV-ish rows and YAML-ish blocks); tokens must survive both, and free
annotations must come through as feedback."""

import unittest

from runnerlib.surface import _parse_feedback


INLINE = '''session:
  file: /home/x/hold-nex-158-pr124.html
  status: feedback
prompts[2]{uid,prompt,selector,tag,text}:
  "1","CADRE_DECISION gate=risk_hold story=nex-158 pr=124 verdict=approve","html > body","choice","Risk-hold verdict: approve"
  "2","this section worries me a little","html > body > div","div","Reviewer report"
'''

BLOCK = '''session:
  status: feedback
prompts[1]:
  - uid: "1"
    prompt: "CADRE_ANSWER ticket=nex-151-ab12cd34 :: go with the flow registry shape\\nsecond line"
    selector: "html > body > form"
    tag: choice
'''


class ParseTest(unittest.TestCase):
    def test_inline_decision_and_free_text(self):
        structured, free = _parse_feedback(INLINE)
        self.assertEqual(structured, [{"type": "decision", "gate": "risk_hold",
                                       "story": "nex-158", "pr": 124,
                                       "verdict": "approve"}])
        self.assertIn("this section worries me a little", free)

    def test_block_answer_multiline(self):
        structured, free = _parse_feedback(BLOCK)
        self.assertEqual(len(structured), 1)
        self.assertEqual(structured[0]["type"], "answer")
        self.assertEqual(structured[0]["ticket"], "nex-151-ab12cd34")
        self.assertIn("second line", structured[0]["text"])
        self.assertEqual(free, [])

    def test_no_feedback_is_empty(self):
        self.assertEqual(_parse_feedback("session:\n  status: opened\n"), ([], []))

    def test_empty_uid_cell(self):
        # API-posted prompts carry an EMPTY uid cell — the real shape that
        # silently lost the first end-to-end answer on fedora-1.
        raw = ('prompts[1]{uid,prompt,selector,tag,text}:\n'
               '  "","CADRE_ANSWER ticket=nex-158-4a82e20c :: charrette again",'
               '"",choice,"Answer: charrette"\n')
        structured, free = _parse_feedback(raw)
        self.assertEqual(structured[0]["ticket"], "nex-158-4a82e20c")
        self.assertEqual(structured[0]["text"], "charrette again")


if __name__ == "__main__":
    unittest.main()
