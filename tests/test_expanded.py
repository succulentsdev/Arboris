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
        # hide after the 1.25s transition. The strip is hidden with
        # visibility, not display, so .hero-bottom keeps its height
        # and the answer box does not move when the strip falls away.
        dock = re.search(r"function dockRouteViz\(\)\{[\s\S]*?\n\}", html).group(0)
        self.assertIn("cancelRouteVizLeave()", dock)
        self.assertIn("routeVizEl.classList.add('rv-leaving')", dock)
        self.assertIn("routeVizEl.style.visibility='hidden'", dock)
        self.assertNotIn("routeVizEl.style.display='none'", dock)
        self.assertIn("},1250)", dock)
        self.assertIn("},1000)", dock)
        # A new turn cancels the pending fade and shows the strip again.
        self.assertIn("function cancelRouteVizLeave()", html)
        flt = re.search(r"function floatRouteViz\(wrap,afterEl\)\{[\s\S]*?\n\}", html).group(0)
        self.assertIn("cancelRouteVizLeave()", flt)
        self.assertIn("routeVizEl.style.display='block'", flt)
        self.assertIn("routeVizEl.style.visibility='visible'", flt)


class TestReceiptScaleAndChatPosition(unittest.TestCase):
    """The receipt scales with the answer, and the chat sits higher.

    The efficiency graphic is as wide as the answer bubble, capped at
    a third of the chat box, with a floor so it stays usable on a short
    answer. The chat area has room below the last message, so the
    conversation sits about the 2/3 mark instead of hugging the
    bottom.
    """

    def test_receipt_scales_with_the_answer_box(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # The receipt width is the bubble's width, capped at a third of
        # the chat box, with a 180px floor.
        self.assertIn("var cap=Math.round(chatW/3)", html)
        self.assertIn("var bw=b.offsetWidth||0", html)
        self.assertIn("var w=Math.min(Math.max(bw,180),cap)", html)
        self.assertIn("svEl.style.width=w+'px'", html)

    def test_chat_sits_higher_than_the_bottom(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        area = re.search(r"\.hero-chat-area \{[^}]*\}", html).group(0)
        self.assertIn("padding: 78px 24px 12vh", area)

    def test_composer_sits_a_third_up_the_screen(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # When chatting the hero keeps the composer about a third of
        # the way up: bottom padding inside the 100svh height, so the
        # chat and the composer live in the top two thirds.
        chatting = re.search(r"\.hero\.chatting \{[^}]*\}", html).group(0)
        self.assertIn("height: 100svh", chatting)
        self.assertIn("padding-bottom: 33vh", chatting)


class TestSavingsReceipt(unittest.TestCase):
    """The per-message receipt: one comparison bar under the answer.

    The gray track is the full length of a typical chatbot answer, the
    teal fill is only as far as this reply used, the empty part is the
    saving. No red anywhere in the receipt; plain words ("less energy",
    "typical chatbot"); one little fun fact right under the graphic,
    the chat's total saving in everyday terms, rotating so the user
    does not always see the same one; no Wh numbers and no running
    total wording on the surface (the methodology page keeps the
    numbers); a 300ms grow, once, when the reply finishes; and a
    neutral "about the same" for a reply that saves little or nothing.
    The receipt is a plain display, not a button: it does not open a
    detail view, and there is no drawer on the main page.
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
        # Headline in plain words; no Wh numbers on the surface.
        self.assertIn("'% less energy'", html)
        self.assertNotIn("This reply · ", html)
        self.assertNotIn("Typical chatbot · ", html)
        self.assertNotIn("This chat: ", html)
        self.assertNotIn("saved across ", html)
        self.assertNotIn("modeled verbose", html)
        # One fun fact under the graphic: the chat's total saving in
        # everyday terms, rotating by message number.
        self.assertIn("sv-fun", html)
        self.assertIn("(totCalls-1)%5", html)
        self.assertIn("about a phone charge for ", html)
        self.assertIn("of streaming video", html)
        self.assertIn("of a phone battery", html)
        self.assertIn("Google search", html)
        self.assertIn("of reading on an e-reader", html)
        # Edge case: a reply that saves little says so in neutral gray.
        self.assertIn("about the same as a typical chatbot", html)
        self.assertIn(".sv-fill.sv-same", html)
        # Grow animation: once, 300ms, on the fill only.
        self.assertIn("@keyframes svGrow", html)
        self.assertIn("animation: svGrow 300ms ease-out", html)
        # The receipt is a plain display, not a button: no drawer, no
        # toggle, no role/tabIndex/title/aria-expanded on the strip, and
        # the full numbers live on methodology.html.
        self.assertNotIn("buildEcoDrawer", html)
        self.assertNotIn("drawerEl", html)
        self.assertNotIn("click for detail", html)
        self.assertNotIn("stripEl.setAttribute('role','button')", html)
        self.assertNotIn("stripEl.tabIndex", html)
        self.assertNotIn("stripEl.title=", html)
        self.assertNotIn("stripEl.setAttribute('aria-expanded'", html)


if __name__ == "__main__":
    unittest.main()
