"""Tests for scripts/quality_check.py and the quality golden set.

Run from the repository root: python -m unittest tests.test_quality_check
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.quality_check import check_all, check_case, load_cases


class TestQualityCheck(unittest.TestCase):
    def test_pass_fail_missing(self):
        cases = [
            {"prompt": "What is the capital of France?", "task_type": "fact",
             "must_contain": ["Paris"]},
            {"prompt": "What is 12 + 7?", "task_type": "math",
             "must_contain": ["19"]},
            {"prompt": "What is DNS?", "task_type": "definition",
             "must_contain": ["domain"]},
        ]
        answers = {
            "What is the capital of France?": "Paris is the capital of France.",
            "What is 12 + 7?": "12 + 7 = 20",
        }
        results = check_all(cases, answers)
        self.assertEqual([r["status"] for r in results], ["PASS", "FAIL", "MISSING"])
        self.assertEqual(results[1]["missing"], ["19"])

    def test_matching_is_case_insensitive(self):
        status, _ = check_case(
            {"prompt": "x", "must_contain": ["Paris"]}, "paris is in france.")
        self.assertEqual(status, "PASS")

    def test_golden_set_has_sixty_two_cases_across_types(self):
        cases = load_cases(Path(__file__).resolve().parents[1] / "spec" / "quality_cases.json")
        self.assertEqual(len(cases), 62)
        types = {c["task_type"] for c in cases}
        self.assertTrue({"fact", "definition", "steps", "reasoning", "math", "code", "creative", "long_input"} <= types)
        for case in cases:
            with self.subTest(prompt=case["prompt"]):
                self.assertTrue(case["must_contain"], "every case needs at least one key fact")


if __name__ == "__main__":
    unittest.main()
