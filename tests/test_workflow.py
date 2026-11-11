"""Exercise actual LangGraph routing using synthetic provider functions."""

import unittest

from inkwise.core import DIMENSIONS
from inkwise.workflow import build_workflow


class WorkflowTests(unittest.TestCase):
    def graph(self, confirmed=True, rules=True, disputed=False):
        self.calls = 0

        def score(state):
            self.calls += 1
            return {"initial": dict.fromkeys(DIMENSIONS, 3)}

        return build_workflow(
            ocr=lambda s: {"ocr_confirmed": confirmed},
            retrieve=lambda s: {"rules_available": rules},
            score=score,
            reflect=lambda s: {"revised": dict.fromkeys(DIMENSIONS, 1 if disputed else 3), "evidence_valid": True},
        )

    def test_ocr_uncertainty_blocks_scoring(self):
        result = self.graph(confirmed=False).invoke({"text": "合成作文"})
        self.assertEqual(result["status"], "needs_text_review")
        self.assertEqual(self.calls, 0)

    def test_missing_rules_blocks_scoring(self):
        result = self.graph(rules=False).invoke({"text": "合成作文"})
        self.assertEqual(result["status"], "rules_unavailable")
        self.assertEqual(self.calls, 0)

    def test_dispute_has_bounded_rounds(self):
        result = self.graph(disputed=True).invoke({"text": "合成作文"})
        self.assertEqual(result["round"], 2)
        self.assertEqual(self.calls, 2)
        self.assertEqual(result["status"], "teacher_review")

    def test_agreement_still_requires_teacher(self):
        result = self.graph().invoke({"text": "合成作文"})
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["status"], "teacher_review")


if __name__ == "__main__":
    unittest.main()
