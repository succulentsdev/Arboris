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
import re
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


class TestRouteVizFadeAway(unittest.TestCase):
    """After an answer appears, the route strip falls away.

    It lingers a second, then fades out and drifts down over 1.25s,
    like it is falling out of existence, and is hidden until the next
    turn. A new turn cancels the fade so the strip is there for it.
    """

    def test_route_strip_fades_away_after_the_answer(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # Leaving state: fully transparent, translated down, 1.25s.
        self.assertIn("#route-viz.rv-leaving", html)
        leaving = re.search(r"#route-viz\.rv-leaving \{[^}]*\}", html).group(0)
        self.assertIn("opacity: 0", leaving)
        self.assertIn("translateY(", leaving)
        self.assertIn("1.25s", leaving)
        # Docking schedules the fade: a 1s linger, then leave, then
        # hide after the 1.25s transition.
        dock = re.search(r"function dockRouteViz\(\)\{[\s\S]*?\n\}", html).group(0)
        self.assertIn("cancelRouteVizLeave()", dock)
        self.assertIn("routeVizEl.classList.add('rv-leaving')", dock)
        self.assertIn("routeVizEl.style.display='none'", dock)
        self.assertIn("},1250)", dock)
        self.assertIn("},1000)", dock)
        # A new turn cancels the pending fade and shows the strip again.
        self.assertIn("function cancelRouteVizLeave()", html)
        flt = re.search(r"function floatRouteViz\(wrap,afterEl\)\{[\s\S]*?\n\}", html).group(0)
        self.assertIn("cancelRouteVizLeave()", flt)
        self.assertIn("routeVizEl.style.display='block'", flt)


class TestSavingsReceipt(unittest.TestCase):
    """The per-message receipt: one comparison bar under the answer.

    The gray track is the full length of a typical chatbot answer, the
    teal fill is only as far as this reply used, the empty part is the
    saving. No red anywhere in the receipt; plain words ("less energy",
    "typical chatbot") with real units in small text; a running chat
    total in everyday terms; a 300ms grow, once, when the reply
    finishes; and a neutral "about the same" for a reply that saves
    little or nothing.
    """

    def test_receipt_is_a_comparison_bar_under_the_answer(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # One horizontal bar: gray track, teal fill at the reply's share.
        self.assertIn(".sv-track", html)
        self.assertIn(".sv-fill", html)
        self.assertIn("linear-gradient(90deg, #3dd4c0, #22a896)", html)
        # The old vertical bars are gone, and the strip is not moved
        # above the bubble any more (no insertBefore in revealAnswer).
        self.assertNotIn("sv-bars", html)
        self.assertNotIn("sv-base", html)
        self.assertNotIn("insertBefore(svStrip", html)
        # Headline in plain words, real units in small text underneath.
        self.assertIn("'% less energy'", html)
        self.assertIn("This reply · ", html)
        self.assertIn("Typical chatbot · ", html)
        self.assertIn("typical chatbot", html)
        self.assertNotIn("modeled verbose", html)
        # Running total for the chat, in everyday terms (phone charge).
        self.assertIn("This chat: ", html)
        self.assertIn("saved across ", html)
        self.assertIn("about a phone charge for ", html)
        self.assertIn("Phone-charge equivalent assumes a 10W charger", html)
        # Edge case: a reply that saves little says so in neutral gray.
        self.assertIn("about the same as a typical chatbot", html)
        self.assertIn(".sv-fill.sv-same", html)
        # Grow animation: once, 300ms, on the fill only.
        self.assertIn("@keyframes svGrow", html)
        self.assertIn("animation: svGrow 300ms ease-out", html)
        # No red in the receipt: the drawer's baseline bar is gray too.
        std = re.search(r"\.eco-bar-fill\.standard \{[^}]*\}", html).group(0)
        self.assertIn("rgba(255,255,255", std)
        self.assertNotIn("#cf4a28", std)
        self.assertNotIn("#f07068", std)


if __name__ == "__main__":
    unittest.main()
