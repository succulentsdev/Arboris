"""Tests for the Arboris harness (stdlib unittest only).

Run from the repository root:
    python -m unittest discover -s tests -v

These tests cover Step 1's fixes specifically: the empty-prompt crash,
word-boundary routing, the six demo topics, and mode ordering. They do
not test the portfolio site demo, whose code lives in the site artifact
and still uses substring routing — see README.md, "Site demo vs harness".
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arboris.brevity import apply_brevity, split_sentences
from arboris.footprint import footprint
from arboris.providers import DemoProvider
from arboris.tokens import est_tokens

ROOT = Path(__file__).resolve().parents[1]

TOPICS = {
    "email": ("Write a polite follow-up email to a client who missed our call", "Quick follow-up"),
    "api": ("Explain APIs to a 10-year-old", "waiter for software"),
    "workout": ("Give me a 3-day workout plan for beginners at home", "Squats"),
    "backup": ("Summarize why backups matter for a small office", "3 copies"),
    "firewall": ("Explain what a portcullis firewall does", "gatekeeper"),
    "dns": ("Explain DNS simply", "internet phone book"),
}


class TestDemoProvider(unittest.TestCase):
    def setUp(self):
        self.provider = DemoProvider()

    def test_empty_prompt_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.provider.complete("")

    def test_whitespace_only_prompt_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.provider.complete("   \n  ")

    def test_none_prompt_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.provider.complete(None)

    def test_single_character_prompt_does_not_crash(self):
        answer = self.provider.complete("a")
        self.assertIn("not in the demo library yet", answer)

    def test_each_topic_routes_to_its_template(self):
        for _name, (prompt, expected) in TOPICS.items():
            with self.subTest(prompt=prompt):
                self.assertIn(expected, self.provider.complete(prompt))

    def test_substring_traps_do_not_route_to_api(self):
        # "capital", "rapid", and "scraping" all contain "api" as a
        # substring; none of them is a question about APIs.
        for prompt in ("What is capital?", "Explain rapid growth", "Explain web scraping"):
            with self.subTest(prompt=prompt):
                self.assertNotIn("waiter for software", self.provider.complete(prompt))

    def test_generic_fallback_is_honest_not_an_answer(self):
        answer = self.provider.complete("Explain quantum computing simply")
        self.assertIn("not in the demo library yet", answer)
        self.assertNotIn("covered in the essentials", answer)

    def test_long_generic_prompt_topic_is_truncated(self):
        answer = self.provider.complete("x" * 200)
        self.assertIn("…", answer)
        self.assertLess(len(answer), 1000)


class TestBrevityAndModes(unittest.TestCase):
    def test_mode_budgets_are_2_4_6(self):
        long_text = "One. Two. Three. Four. Five. Six. Seven. Eight."
        self.assertEqual(len(split_sentences(apply_brevity(long_text, "sprout"))), 2)
        self.assertEqual(len(split_sentences(apply_brevity(long_text, "canopy"))), 4)
        self.assertEqual(len(split_sentences(apply_brevity(long_text, "full"))), 6)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            apply_brevity("Hello.", "forest")

    def test_topic_token_counts_are_monotonic_and_sprout_is_shortest(self):
        provider = DemoProvider()
        for name, (prompt, _expected) in TOPICS.items():
            draft = provider.complete(prompt)
            counts = {
                mode: est_tokens(apply_brevity(draft, mode))
                for mode in ("sprout", "canopy", "full")
            }
            with self.subTest(topic=name, counts=counts):
                self.assertLessEqual(counts["sprout"], counts["canopy"])
                self.assertLessEqual(counts["canopy"], counts["full"])
                self.assertLess(counts["sprout"], counts["full"])

    def test_email_keeps_paragraph_shape(self):
        answer = apply_brevity(DemoProvider().complete("write a follow-up email"), "full")
        self.assertIn("\n\n", answer)
        self.assertIn("Thanks,", answer)

    def test_filler_clauses_strip_cleanly(self):
        text = "I hope you are doing well. Here is the answer."
        self.assertEqual(apply_brevity(text, "full"), "Here is the answer.")
        chatty = "Certainly! Great question! DNS translates domain names to IP addresses. Hope this helps!"
        self.assertEqual(apply_brevity(chatty, "sprout"), "DNS translates domain names to IP addresses.")
        lead_chatty = "Sure, here is a brief explanation: photosynthesis converts light into chemical energy."
        self.assertEqual(apply_brevity(lead_chatty, "sprout"), "Photosynthesis converts light into chemical energy.")


class TestTokensAndFootprint(unittest.TestCase):
    def test_est_tokens_minimum_is_one(self):
        self.assertEqual(est_tokens(""), 1)

    def test_footprint_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            footprint(10, "forest")

    def test_footprint_saved_pct_is_between_0_and_100(self):
        result = footprint(50, "sprout")
        self.assertTrue(0 <= result["saved_pct"] <= 100)
        self.assertGreater(result["standard_tokens"], result["eco_tokens"])


class TestEnergyReceipt(unittest.TestCase):
    def test_receipt_arithmetic_and_water_range(self):
        # Output-weighted: 50 out x 0.0006 Wh + 10 in x 0.0006 / 800 Wh
        # = 0.0300075 Wh; at 400 g/kWh = 0.012003 g; water = Wh x WUE.
        from arboris.footprint import energy_receipt
        receipt = energy_receipt(10, 50, model="m", model_tier="primary")
        self.assertEqual(receipt["status"], "estimate")
        self.assertEqual(receipt["total_tokens"], 60)
        self.assertEqual(receipt["energy_wh"], 0.030007)
        self.assertEqual(receipt["co2e_g"], 0.012003)
        self.assertEqual((receipt["water_ml_low"], receipt["water_ml_high"]), (0.003001, 0.009002))
        self.assertEqual(receipt["factors"]["input_output_energy_ratio"], 800)
        self.assertEqual(receipt["factors"]["grid_g_per_kwh"], 400.0)

    def test_output_tokens_dominate_the_estimate(self):
        from arboris.footprint import energy_receipt
        long_question = energy_receipt(800, 0)["energy_wh"]
        one_output_token = energy_receipt(0, 1)["energy_wh"]
        # 800 input tokens cost the same as 1 output token.
        self.assertEqual(long_question, one_output_token)
        self.assertEqual(one_output_token, 0.0006)

    def test_receipt_rejects_non_positive_ratio(self):
        from arboris.footprint import energy_receipt
        with self.assertRaises(ValueError):
            energy_receipt(10, 50, input_output_ratio=0)

    def test_receipt_cache_hit_is_zero(self):
        from arboris.footprint import energy_receipt
        receipt = energy_receipt(0, 0, cache_hit=True)
        self.assertEqual((receipt["energy_wh"], receipt["co2e_g"]), (0.0, 0.0))
        self.assertIn("Cache hit", receipt["note"])

    def test_receipt_states_unknown_region_and_anchor_range(self):
        from arboris.footprint import (WH_PER_TOKEN, WH_PER_TOKEN_RANGE,
                                           energy_receipt)
        receipt = energy_receipt(10, 50, model="google/gemma-4-31b-it:free")
        factors = receipt["factors"]
        self.assertEqual(factors["grid_region"], "unknown")
        self.assertIn("Inference region unknown", receipt["note"])
        low, high = factors["wh_per_token_range"]
        self.assertEqual((low, high), WH_PER_TOKEN_RANGE)
        self.assertLess(low, high)
        # The default factor must sit inside the cited range.
        self.assertTrue(low <= WH_PER_TOKEN <= high)
        self.assertIn("Epoch AI", factors["wh_per_token_range_label"])
        self.assertEqual(receipt["model_size_tier"], "medium")

    def test_model_size_tier_reads_parameter_counts(self):
        from arboris.footprint import model_size_tier
        cases = {
            "google/gemma-4-31b-it:free": "medium",
            "meta-llama/llama-3.1-8b-instruct": "small",
            "qwen/qwen2.5-1.5b-instruct": "small",
            "meta-llama/llama-3.1-405b": "large",
            "openai/gpt-oss-120b": "large",
            # Mixture of experts: active parameters win when stated.
            "qwen/qwen3-30b-a3b": "small",
            "qwen/qwen3-235b-a22b": "medium",
            # Experts x size is sized by the total as an upper bound.
            "mistralai/mixtral-8x7b-instruct": "medium",
            # No parameter count in the id: no guess from brand names.
            "openai/gpt-4o-mini": "unknown",
            "anthropic/claude-sonnet-4": "unknown",
            # "4bit" is a quantisation tag, not a size.
            "some-model-4bit": "unknown",
            "": "unknown",
            None: "unknown",
        }
        for name, tier in cases.items():
            with self.subTest(model=name):
                self.assertEqual(model_size_tier(name), tier)

    def test_receipt_unavailable_invents_nothing(self):
        from arboris.footprint import energy_receipt
        receipt = energy_receipt(None, 50)
        self.assertEqual(receipt["status"], "unavailable")
        self.assertIsNone(receipt["energy_wh"])
        self.assertIsNone(receipt["co2e_g"])
        self.assertIsNone(receipt["water_ml_low"])


class TestCli(unittest.TestCase):
    def test_cli_empty_prompt_exits_2_without_traceback(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "cli.py"), "", "--mode", "sprout"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Traceback", result.stdout)

    def test_cli_dns_demo_returns_dns_template(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "cli.py"), "Explain DNS simply", "--mode", "sprout"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("internet phone book", result.stdout)


if __name__ == "__main__":
    unittest.main()
