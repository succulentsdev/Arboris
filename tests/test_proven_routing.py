"""Tests for the FrugalGPT / RouteLLM adaptations: transparent scorer,
difficulty score with tunable threshold, no-model arithmetic gate,
and budget-aware cascade escalation.

Run from the repository root: python -m unittest tests.test_proven_routing
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arboris.cascade import run_cascade
from arboris.local_calc import try_local_calc
from arboris.providers import Completion
from arboris.scorer import score_answer
from arboris.task_router import difficulty_score, route_task


class _Fake:
    def __init__(self, text, avg_logprob=None, tokens=None, model="fake"):
        self.text = text
        self.avg_logprob = avg_logprob
        self.tokens = tokens
        self.model = model
        self.calls = 0

    def complete_ex(self, prompt, system=None, max_tokens=None, logprobs=False):
        self.calls += 1
        return Completion(text=self.text, avg_logprob=self.avg_logprob,
                          completion_tokens=self.tokens)


class TestScorer(unittest.TestCase):
    def test_good_answer_scores_high(self):
        result = score_answer("What is the capital of France?",
                              "Paris is the capital of France.",
                              task_type="fact", avg_logprob=-0.2)
        self.assertGreaterEqual(result["score"], 0.8)

    def test_empty_answer_scores_zero(self):
        self.assertEqual(score_answer("hi", "  ")["score"], 0.0)

    def test_truncated_answer_scores_low(self):
        result = score_answer("Explain DNS simply",
                              "DNS translates domain names",
                              task_type="definition", finish_reason="length")
        self.assertLess(result["components"]["completeness"], 0.5)
        self.assertIn("cut off", " ".join(result["reasons"]))

    def test_steps_without_list_scores_low_on_format(self):
        result = score_answer("How to reset a router?",
                              "Just turn it off and on again.",
                              task_type="steps")
        self.assertLess(result["components"]["format"], 0.5)

    def test_logprob_unknown_is_neutral_not_zero(self):
        result = score_answer("What is DNS?", "DNS is a naming system.",
                              task_type="definition", avg_logprob=None)
        self.assertTrue(result["logprob_unknown"])
        self.assertEqual(result["components"]["logprob"], 0.5)


class TestDifficultyScore(unittest.TestCase):
    def test_golden_invariant_at_default_threshold(self):
        # At threshold 0.5 the difficulty score must reproduce
        # route_task's tier for every golden case, so difficulty
        # routing with the default threshold changes nothing.
        cases = json.loads(
            (Path(__file__).resolve().parents[1] / "spec" / "task_cases.json"
             ).read_text())["cases"]
        self.assertGreaterEqual(len(cases), 15)
        for case in cases:
            with self.subTest(prompt=case["prompt"]):
                route = route_task(case["prompt"])
                score = difficulty_score(case["prompt"])
                self.assertEqual(score < 0.5, route.tier == "small")

    def test_easy_fact_low_hard_reasoning_high(self):
        self.assertLess(difficulty_score("What is the capital of France?"), 0.5)
        self.assertGreater(difficulty_score("Prove the Pythagorean theorem."), 0.5)

    def test_threshold_presets_ordering(self):
        # Eco routes more to small, quality routes more to primary.
        simple_steps = difficulty_score("How do I reset my router?")
        self.assertGreater(simple_steps, 0.3)  # quality would move it to primary
        self.assertLess(simple_steps, 0.7)     # eco would keep it small


class TestLocalCalc(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(try_local_calc("What is 12 + 7?"), "12 + 7 = 19")

    def test_precedence_and_parentheses(self):
        self.assertEqual(try_local_calc("2 + 3 * 4"), "2 + 3 * 4 = 14")
        self.assertEqual(try_local_calc("(2 + 3) * 4"), "(2 + 3) * 4 = 20")

    def test_division_by_zero_falls_through(self):
        self.assertIsNone(try_local_calc("What is 1 / 0?"))

    def test_words_or_code_fall_through(self):
        self.assertIsNone(try_local_calc("What is 12 + 7 apples?"))
        self.assertIsNone(try_local_calc("12 + 7; import os"))
        self.assertIsNone(try_local_calc("What is the capital of France?"))
        self.assertIsNone(try_local_calc("12"))
        self.assertIsNone(try_local_calc("2 ** 10"))

    def test_percentages_and_unit_conversions(self):
        self.assertEqual(try_local_calc("What is 15% of 240?"), "15% of 240 = 36")
        self.assertEqual(try_local_calc("12.5% of 80"), "12.5% of 80 = 10")
        self.assertEqual(try_local_calc("Convert 100 km to miles"), "100 km = 62.1371 mi")
        self.assertEqual(try_local_calc("100 C in F"), "100 °C = 212 °F")
        self.assertEqual(try_local_calc("32 F to C"), "32 °F = 0 °C")
        self.assertEqual(try_local_calc("2048 MB to GB"), "2048 MB = 2 GB")
        self.assertEqual(try_local_calc("60 mph to km/h"), "60 mph = 96.5606 km/h")
        self.assertEqual(try_local_calc("1 kWh to J"), "1 kWh = 3600000 J")
        self.assertEqual(try_local_calc("120 min to hours"), "120 min = 2 hr")
        # Incompatible unit categories fall through to the model.
        self.assertIsNone(try_local_calc("Convert 100 km to kg"))

    def test_base_and_color_conversions(self):
        self.assertEqual(try_local_calc("Convert 255 to hex"), "255 = 0xff")
        self.assertEqual(try_local_calc("0xff in decimal"), "0xff = 255")
        self.assertEqual(try_local_calc("42 in binary"), "42 = 0b101010")
        self.assertEqual(try_local_calc("0b101010 to dec"), "0b101010 = 42")
        self.assertEqual(try_local_calc("Convert #cf4a28 to rgb"), "#cf4a28 = rgb(207, 74, 40)")
        self.assertEqual(try_local_calc("rgb(207, 74, 40) to hex"), "rgb(207, 74, 40) = #cf4a28")


class TestCascadeScorerAndBudget(unittest.TestCase):
    def test_scorer_escalates_without_logprobs(self):
        primary = _Fake("Paris", avg_logprob=None)
        escalation = _Fake("Paris is the capital of France.", model="fake-large")
        result = run_cascade(
            primary, escalation, "What is the capital of France?",
            score_fn=lambda prompt, comp: {"score": 0.2, "reasons": ["low"]},
            score_threshold=0.5, score_escalates=True)
        self.assertTrue(result.escalated)
        self.assertEqual(escalation.calls, 1)

    def test_scorer_reports_but_does_not_escalate_by_default(self):
        primary = _Fake("Paris", avg_logprob=-0.2)
        escalation = _Fake("longer", model="fake-large")
        result = run_cascade(
            primary, escalation, "What is the capital of France?",
            score_fn=lambda prompt, comp: {"score": 0.2, "reasons": ["low"]},
            score_threshold=0.5, score_escalates=False)
        self.assertFalse(result.escalated)
        self.assertEqual(result.score, 0.2)
        self.assertEqual(escalation.calls, 0)

    def test_budget_gate_blocks_escalation(self):
        primary = _Fake("x", avg_logprob=-2.5, tokens=500)
        escalation = _Fake("y", model="fake-large")
        result = run_cascade(primary, escalation, "hi", threshold=-1.0,
                             max_total_tokens=100)
        self.assertFalse(result.escalated)
        self.assertIn("budget", result.reason)
        self.assertEqual(escalation.calls, 0)

    def test_budget_gate_allows_small_answers(self):
        primary = _Fake("x", avg_logprob=-2.5, tokens=50)
        escalation = _Fake("y", model="fake-large")
        result = run_cascade(primary, escalation, "hi", threshold=-1.0,
                             max_total_tokens=100)
        self.assertTrue(result.escalated)
        self.assertEqual(escalation.calls, 1)


if __name__ == "__main__":
    unittest.main()
