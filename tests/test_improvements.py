"""Tests for the 0.5.0 fixes: brevity structure, provider hardening,
honest footprint accounting, atomic sessions, auto-routing, and the
shared routing spec checked against the website."""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arboris.brevity import apply_brevity, split_sentences, strip_filler_clauses
from arboris.cli import main
from arboris.footprint import CO2_PER_TOKEN_G, footprint
from arboris.providers import (
    DemoProvider, OpenAICompatibleProvider, ProviderError, TOKEN_BUDGET,
)
from arboris.router import route_mode
from arboris.session import Session

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "spec" / "routing_cases.json").read_text())


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(text="ok", usage=None):
    body = {"choices": [{"message": {"content": text}}]}
    if usage:
        body["usage"] = usage
    return FakeResponse(json.dumps(body).encode())


class TestBrevityStructure(unittest.TestCase):
    def test_filler_phrase_inside_a_sentence_is_not_cut_out(self):
        text = "Please let me know if you have any questions about the report."
        self.assertEqual(strip_filler_clauses(text), text)

    def test_whole_filler_sentence_is_still_dropped(self):
        self.assertEqual(
            apply_brevity("Please let me know if you have any questions. Done.", "full"),
            "Done.",
        )

    def test_abbreviations_and_initials_do_not_split(self):
        parts = split_sentences("Use e.g. Python or ask Dr. Smith. J. Doe agrees. It works.")
        self.assertEqual(len(parts), 3)

    def test_list_items_keep_their_line_breaks(self):
        out = apply_brevity("Steps:\n- one\n- two\n- three.", "full")
        self.assertEqual(out, "Steps:\n- one\n- two\n- three.")

    def test_list_items_each_count_toward_the_budget(self):
        out = apply_brevity("- a\n- b\n- c\n- d", "sprout")
        self.assertEqual(out, "- a\n- b")

    def test_sprout_email_keeps_subject_and_signature(self):
        out = apply_brevity(DemoProvider().complete("write a follow-up email"), "sprout")
        self.assertTrue(out.startswith("Subject: Quick follow-up"))
        self.assertTrue(out.endswith("Thanks,\nGrant"))

    def test_code_fence_is_never_split(self):
        text = "Intro.\n\n```py\nx = 1. y = 2. z\n```\n\nMore. Extra. Even more."
        out = apply_brevity(text, "canopy")
        self.assertIn("```py\nx = 1. y = 2. z\n```", out)

    def test_non_string_answer_raises_value_error(self):
        with self.assertRaises(ValueError):
            apply_brevity(None, "full")


class TestProviderHardening(unittest.TestCase):
    def provider(self):
        return OpenAICompatibleProvider("https://api.example.test/v1", api_key="k")

    def test_plain_http_is_rejected_except_localhost(self):
        with self.assertRaises(ValueError):
            OpenAICompatibleProvider("http://api.example.test/v1", api_key="k")
        OpenAICompatibleProvider("http://localhost:11434/v1", api_key="k")

    def test_system_prompt_and_max_tokens_are_sent(self):
        with mock.patch("urllib.request.urlopen", return_value=reply()) as urlopen:
            self.provider().complete_ex("hi", system="Be brief.", max_tokens=50)
        body = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(body["max_tokens"], 50)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "Be brief."})

    def test_unconstrained_call_sends_neither(self):
        with mock.patch("urllib.request.urlopen", return_value=reply()) as urlopen:
            self.provider().complete_ex("hi")
        body = json.loads(urlopen.call_args[0][0].data)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(len(body["messages"]), 1)

    def test_usage_is_parsed(self):
        usage = {"completion_tokens": 12, "prompt_tokens": 3}
        with mock.patch("urllib.request.urlopen", return_value=reply("x", usage)):
            comp = self.provider().complete_ex("hi")
        self.assertEqual((comp.completion_tokens, comp.prompt_tokens), (12, 3))

    def test_null_content_is_a_provider_error(self):
        fake = FakeResponse(b'{"choices":[{"message":{"content":null}}]}')
        with mock.patch("urllib.request.urlopen", return_value=fake):
            with self.assertRaises(ProviderError):
                self.provider().complete("hi")

    def test_socket_timeout_is_a_provider_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=socket.timeout("slow")):
            with self.assertRaises(ProviderError):
                self.provider().complete("hi")

    def test_invalid_utf8_is_a_provider_error(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"\xff\xfe")):
            with self.assertRaises(ProviderError):
                self.provider().complete("hi")


class TestFootprint(unittest.TestCase):
    def test_default_factor_is_the_cited_output_token_anchor(self):
        # 0.0006 Wh per output token (Epoch AI 2025) x 400 g/kWh.
        self.assertEqual(CO2_PER_TOKEN_G, 0.00024)
        self.assertEqual(footprint(100, "full")["eco_co2_g"], 0.024)

    def test_grid_intensity_scales_co2_not_tokens(self):
        base = footprint(100, "full")
        low = footprint(100, "full", grid_g_per_kwh=30)
        self.assertEqual(base["eco_tokens"], low["eco_tokens"])
        self.assertAlmostEqual(low["eco_co2_g"], base["eco_co2_g"] * 30 / 400, places=4)

    def test_assumed_baseline_percentage_depends_only_on_mode(self):
        self.assertEqual(
            footprint(10, "sprout")["saved_pct"], footprint(500, "sprout")["saved_pct"]
        )
        self.assertEqual(footprint(10, "sprout")["baseline_kind"], "assumed")

    def test_measured_baseline_is_used_and_labelled(self):
        fp = footprint(50, "sprout", baseline_tokens=100)
        self.assertEqual((fp["standard_tokens"], fp["saved_pct"]), (100, 50))
        self.assertEqual(fp["baseline_kind"], "measured")

    def test_measured_baseline_smaller_than_reply_never_goes_negative(self):
        fp = footprint(80, "sprout", baseline_tokens=40)
        self.assertEqual((fp["saved_pct"], fp["saved_co2_g"]), (0, 0.0))

    def test_non_positive_factors_raise(self):
        with self.assertRaises(ValueError):
            footprint(10, "full", wh_per_token=0)


class TestAtomicSession(unittest.TestCase):
    FP = {"eco_tokens": 1, "standard_tokens": 2, "saved_pct": 50, "saved_co2_g": 0.1}

    def test_save_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Session(); s.record("p", "a", "sprout", self.FP)
            s.save(str(Path(tmp) / "s.json"))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["s.json"])

    def test_failed_save_keeps_the_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            s = Session(); s.record("p", "a", "sprout", self.FP)
            s.save(str(path))
            before = path.read_text()
            s.calls.append({"unserialisable": object()})
            with self.assertRaises(TypeError):
                s.save(str(path))
            self.assertEqual(path.read_text(), before)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)


class TestRouting(unittest.TestCase):
    def test_python_router_meets_the_shared_spec(self):
        for case in SPEC["cases"]:
            with self.subTest(prompt=case["prompt"][:40]):
                self.assertEqual(route_mode(case["prompt"]), case["mode"])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_website_meets_the_shared_spec(self):
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "site_conformance.js")],
            capture_output=True, text=True, check=True,
        )
        got = json.loads(result.stdout)
        self.assertEqual(got["routes"], [c["mode"] for c in SPEC["cases"]])
        self.assertEqual(
            got["shrinks"], [c["out"] for c in SPEC["shrink_cases_site_only"]]
        )


class TestCliHonesty(unittest.TestCase):
    def run_cli(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_auto_mode_reports_the_resolved_mode(self):
        code, out, _ = self.run_cli(["Explain DNS simply", "--mode", "auto"])
        self.assertEqual(code, 0)
        self.assertIn("mode: sprout (auto)", out)

    def test_demo_warns_when_a_prompt_is_ignored(self):
        code, _out, err = self.run_cli(["--demo", "some prompt", "--mode", "sprout"])
        self.assertEqual(code, 0)
        self.assertIn("ignores the prompt", err)

    def test_assumed_baseline_is_labelled_in_output(self):
        _code, out, _ = self.run_cli(["Explain DNS simply", "--mode", "sprout"])
        self.assertIn("assumed baseline", out)

    def test_measure_baseline_demo_is_labelled_measured(self):
        _code, out, _ = self.run_cli(["Explain DNS simply", "--mode", "sprout", "--measure-baseline"])
        self.assertIn("measured baseline", out)

    def test_bad_energy_factor_exits_2(self):
        code, _out, err = self.run_cli(["hi", "--wh-per-token", "0"])
        self.assertEqual(code, 2)
        self.assertIn("must be positive", err)

    def test_live_measure_baseline_makes_two_calls_and_uses_real_usage(self):
        replies = [
            reply("Short answer.", {"completion_tokens": 10}),
            reply("A very long unconstrained answer. " * 3, {"completion_tokens": 40}),
        ]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as urlopen:
            code, out, _ = self.run_cli(
                ["explain it", "--provider", "live", "--mode", "sprout", "--measure-baseline"],
                env={"ARBORIS_API_KEY": "k"},
            )
        self.assertEqual(code, 0)
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("Output: 10 vs 40 standard (measured baseline)", out)

    def test_live_no_constrain_sends_no_system_prompt(self):
        with mock.patch("urllib.request.urlopen", return_value=reply("Hi.")) as urlopen:
            self.run_cli(["hello", "--provider", "live", "--no-constrain"],
                         env={"ARBORIS_API_KEY": "k"})
        body = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(len(body["messages"]), 1)
        self.assertNotIn("max_tokens", body)

    def test_live_default_constrains_generation(self):
        with mock.patch("urllib.request.urlopen", return_value=reply("Hi.")) as urlopen:
            self.run_cli(["hello", "--provider", "live", "--mode", "sprout"],
                         env={"ARBORIS_API_KEY": "k"})
        body = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["max_tokens"], 120)

    def test_null_model_content_exits_2_without_traceback(self):
        fake = FakeResponse(b'{"choices":[{"message":{"content":null}}]}')
        with mock.patch("urllib.request.urlopen", return_value=fake):
            code, _out, err = self.run_cli(
                ["hello", "--provider", "live"], env={"ARBORIS_API_KEY": "k"}
            )
        self.assertEqual(code, 2)
        self.assertIn("Error:", err)


class TestTokenBudget(unittest.TestCase):
    """L3: explicit token budget instruction in system prompt."""

    def test_budget_values_are_below_max_tokens(self):
        from arboris.providers import MAX_TOKENS
        for mode in ("sprout", "canopy", "full"):
            self.assertLess(TOKEN_BUDGET[mode], MAX_TOKENS[mode])

    def test_budget_appended_to_system_prompt_in_cli(self):
        """--token-budget appends 'Use under N tokens.' to system prompt."""
        with mock.patch("urllib.request.urlopen", return_value=reply("Short.")) as urlopen:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"ARBORIS_API_KEY": "k"}):
                with redirect_stdout(out), redirect_stderr(err):
                    main(["hello", "--provider", "live", "--mode", "sprout", "--token-budget"])
        body = json.loads(urlopen.call_args[0][0].data)
        system = body["messages"][0]["content"]
        self.assertIn("Use under 100 tokens.", system)
        self.assertTrue(system.startswith("Answer in at most 2 short sentences."))

    def test_no_budget_without_flag(self):
        """Without --token-budget, system prompt has no budget instruction."""
        with mock.patch("urllib.request.urlopen", return_value=reply("Short.")) as urlopen:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"ARBORIS_API_KEY": "k"}):
                with redirect_stdout(out), redirect_stderr(err):
                    main(["hello", "--provider", "live", "--mode", "sprout"])
        body = json.loads(urlopen.call_args[0][0].data)
        system = body["messages"][0]["content"]
        self.assertNotIn("Use under", system)

    def test_budget_per_mode(self):
        """Each mode gets its own budget value."""
        for mode_name, expected_budget in TOKEN_BUDGET.items():
            with mock.patch("urllib.request.urlopen", return_value=reply("Ok.")) as urlopen:
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, {"ARBORIS_API_KEY": "k"}):
                    with redirect_stdout(out), redirect_stderr(err):
                        main(["hello", "--provider", "live", "--mode", mode_name, "--token-budget"])
            body = json.loads(urlopen.call_args[0][0].data)
            system = body["messages"][0]["content"]
            self.assertIn(f"Use under {expected_budget} tokens.", system)

    def test_no_budget_with_no_constrain(self):
        """--no-constrain disables system prompt entirely, so no budget."""
        with mock.patch("urllib.request.urlopen", return_value=reply("Hi.")) as urlopen:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"ARBORIS_API_KEY": "k"}):
                with redirect_stdout(out), redirect_stderr(err):
                    main(["hello", "--provider", "live", "--token-budget", "--no-constrain"])
        body = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(len(body["messages"]), 1)  # no system message


if __name__ == "__main__":
    unittest.main()
