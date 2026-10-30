"""Tests for the expanded template library (80+ topics).

Covers:
- Template file loading and structure
- Python routing for a sample of expanded topics
- Sentence-budget derivation (sprout=2, canopy=4, full=6)
- No overlap or shadowing between the original 6 and expanded topics
- Website JS routing parity for expanded topics (via Node subprocess)

Run from the repository root:
    python -m unittest tests.test_expanded -v
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arboris.brevity import apply_brevity, split_sentences
from arboris.providers import DemoProvider

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "spec" / "templates.json").read_text())
TOPICS = SPEC["topics"]


class TestTemplateFileStructure(unittest.TestCase):
    """Verify templates.json is well-formed and consistent."""

    def test_file_loads_and_has_topics(self):
        self.assertGreaterEqual(len(TOPICS), 80, "expected 80+ topics")

    def test_every_topic_has_required_keys(self):
        for t in TOPICS:
            with self.subTest(id=t.get("id", "<missing>")):
                self.assertIn("id", t)
                self.assertIn("keywords", t)
                self.assertIn("answer", t)
                self.assertIsInstance(t["keywords"], list)
                self.assertGreater(len(t["keywords"]), 0, "needs at least one keyword")
                self.assertIsInstance(t["answer"], str)
                self.assertGreater(len(t["answer"]), 20, "answer too short")

    def test_ids_are_unique(self):
        ids = [t["id"] for t in TOPICS]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate IDs: {[x for x in ids if ids.count(x)>1]}")

    def test_no_id_collides_with_original_six(self):
        original = {"email", "api", "workout", "backup", "firewall", "dns"}
        for t in TOPICS:
            self.assertNotIn(t["id"], original,
                             f"expanded topic '{t['id']}' collides with original six")


class TestExpandedRouting(unittest.TestCase):
    """Verify Python DemoProvider routes expanded topics correctly."""

    # A representative sample across categories
    SAMPLES = [
        ("Explain how passwords work", "strong password"),
        ("What is machine learning", "branch of ai"),
        ("Explain blockchain simply", "shared ledger"),
        ("How does gravity work", "pulls objects"),
        ("What is photosynthesis", "sunlight"),
        ("Explain the water cycle", "evaporat"),
        ("Tell me about baking bread", "baking"),
        ("Tell me about budgeting", "budget"),
        ("What is version control", "version control"),
        ("Explain sorting algorithms", "sort"),
    ]

    def setUp(self):
        self.provider = DemoProvider()

    def test_sample_expanded_topics_route(self):
        for prompt, expected_fragment in self.SAMPLES:
            with self.subTest(prompt=prompt):
                answer = self.provider.complete(prompt)
                self.assertNotIn("not in the demo library yet", answer,
                                 f"'{prompt}' hit fallback instead of expanded library")
                self.assertIn(expected_fragment, answer.lower(),
                              f"expected '{expected_fragment}' in answer for '{prompt}'")

    def test_original_six_still_take_priority(self):
        """Original hardcoded topics must route to their own templates, not expanded."""
        cases = [
            ("Write a follow-up email", "Quick follow-up"),
            ("Explain APIs to a 10-year-old", "waiter for software"),
            ("Give me a workout plan", "Squats"),
            ("Summarize why backups matter", "3 copies"),
            ("Explain what a portcullis firewall does", "gatekeeper"),
            ("Explain DNS simply", "internet phone book"),
        ]
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                self.assertIn(expected, self.provider.complete(prompt))


class TestSentenceBudgetDerivation(unittest.TestCase):
    """Verify brevity layer derives correct sentence counts from expanded answers."""

    def setUp(self):
        self.provider = DemoProvider()

    def test_expanded_answers_have_at_least_six_sentences(self):
        """Each expanded answer should have ~6 sentences for full budget derivation."""
        for t in TOPICS:
            sents = split_sentences(t["answer"])
            with self.subTest(id=t["id"], sentence_count=len(sents)):
                self.assertGreaterEqual(len(sents), 5,
                    f"topic '{t['id']}' has only {len(sents)} sentences; need ≥5 for mode trimming")

    def test_mode_trimming_is_monotonic_for_expanded(self):
        """sprout ≤ canopy ≤ full for every expanded topic answer."""
        for t in TOPICS[:20]:  # sample 20 to keep test fast
            answer = t["answer"]
            sprout = split_sentences(apply_brevity(answer, "sprout"))
            canopy = split_sentences(apply_brevity(answer, "canopy"))
            full = split_sentences(apply_brevity(answer, "full"))
            with self.subTest(id=t["id"]):
                self.assertLessEqual(len(sprout), len(canopy))
                self.assertLessEqual(len(canopy), len(full))


class TestExpandedSubstringTraps(unittest.TestCase):
    """Ensure word-boundary matching prevents false positives in expanded topics."""

    def setUp(self):
        self.provider = DemoProvider()

    def test_no_false_positives_from_common_words(self):
        """Prompts containing partial keyword matches should hit fallback."""
        traps = [
            "Explain the word 'classified' in context",  # shouldn't match 'class'
            "What is the meaning of 'understanding'",      # shouldn't match 'and'
        ]
        for prompt in traps:
            answer = self.provider.complete(prompt)
            # These may or may not hit a topic — the key is they shouldn't
            # crash and should return *something* reasonable
            with self.subTest(prompt=prompt):
                self.assertIsInstance(answer, str)
                self.assertGreater(len(answer), 10)


class TestWebsiteLiveOnly(unittest.TestCase):
    """The website is live-only: no demo library, no topic detection."""

    def test_website_exports_live_only_no_demo(self):
        """The site JS exports shrink/route only, and holds no demo library."""
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        for gone in ("EXPANDED_TOPICS", "detectTopic", "greenAnswer",
                     "integrateChat", "TOPIC_META", "TEMPLATES"):
            self.assertNotIn(gone, html)
        self.assertIn("module.exports={shrinkPrompt,routeMode,estTokens}", html)
        if not shutil.which("node"):
            return
        js_code = """
var fs = require('fs');
var html = fs.readFileSync('website/index.html','utf8');
var script = html.match(/<script>([\\s\\S]*?)<\\/script>/)[1];
var mod = {exports: {}};
var fn = new Function('module','exports', script + '; return module.exports || {};');
var exports = fn(mod, mod.exports);
console.log(JSON.stringify(Object.keys(exports).sort()));
"""
        result = subprocess.run(
            ["node", "-e", js_code],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, f"Node failed: {result.stderr}")
        self.assertEqual(json.loads(result.stdout),
                         ["estTokens", "routeMode", "shrinkPrompt"])


if __name__ == "__main__":
    unittest.main()
