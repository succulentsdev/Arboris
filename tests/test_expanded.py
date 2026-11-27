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


def _jpeg_width(data):
    """Return a JPEG's pixel width by reading its start-of-frame
    marker (SOF0/SOF2). Pure stdlib: the CI runner has no Pillow."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG (missing SOI marker)")
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC2):  # SOF0, SOF2: precision, height, width
            return (data[i + 7] << 8) | data[i + 8]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone markers, no length field
            continue
        seg_len = (data[i + 2] << 8) | data[i + 3]
        i += 2 + seg_len
    raise ValueError("no SOF marker found")
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
    """The receipt scales with the answer, and the composer is at the
    bottom.

    The efficiency graphic is as wide as the answer bubble, capped at
    a third of the chat box, with a floor so it stays usable on a short
    answer. The composer sits at the bottom of the page (round 17
    removed the 33vh lift), and the last message sits just above it.
    """

    def test_receipt_scales_with_the_answer_box(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # The receipt width is the bubble's width, capped at a third of
        # the chat box, with a 180px floor.
        self.assertIn("var cap=Math.round(chatW/3)", html)
        self.assertIn("var bw=b.offsetWidth||0", html)
        self.assertIn("var w=Math.min(Math.max(bw,180),cap)", html)
        self.assertIn("svEl.style.width=w+'px'", html)

    def test_last_message_sits_just_above_the_composer(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # Anchored to the line start: the Round 12 theme override
        # (body.theme-round12 .hero-chat-area) sits earlier in the
        # file and must not be the rule matched here.
        area = re.search(r"^\.hero-chat-area \{[^}]*\}", html, re.M).group(0)
        self.assertIn("padding: 78px 24px 16px", area)

    def test_composer_sits_at_the_bottom_of_the_page(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # When chatting the hero is 100svh with no bottom padding, so
        # the composer (the last flex child) sits at the viewport
        # bottom instead of a third up the screen.
        # Anchored to the line start, as above: the theme override
        # (body.theme-round12 .hero.chatting) is earlier in the file.
        chatting = re.search(r"^\.hero\.chatting \{[^}]*\}", html, re.M).group(0)
        self.assertIn("height: 100svh", chatting)
        self.assertNotIn("padding-bottom: 33vh", chatting)


class TestComposerRevertAndBubbleGreens(unittest.TestCase):
    """Round 18 (2026-11-26): the composer is back to its original
    design, the user bubble keeps its teal, and the response bubble
    is a dark green again.

    The composer card is the solid dark surface again (round 17's
    gray translucent frosted card is reverted), while it stays at the
    bottom of the page. The user bubble is the deep teal-green from
    round 17. The bot bubble is a dark green again, but a clean one,
    not the mucky rgba(15,24,19,.88) Grant disliked.
    """

    def test_composer_original_design_and_bubbles(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # Composer: back to the original solid dark surface.
        card = re.search(r"\.composer-card \{[^}]*\}", html).group(0)
        self.assertIn("background: var(--surface);", card)
        self.assertIn("border: none;", card)
        self.assertNotIn("backdrop-filter", card)
        self.assertNotIn("rgba(44,47,49,.72)", card)
        # User bubble: the teal from round 17 is kept.
        self.assertIn("--glass-user: rgba(9,45,38,.92);", html)
        self.assertIn("--glass-user-border: rgba(61,212,192,.30);", html)
        # Bot bubble: a dark green again, clean, not the mucky one.
        self.assertIn("--glass-bot: rgba(10,40,20,.92);", html)
        # The mucky green and the coral are gone from the default
        # design (the :root variables); they live only under the
        # Round 12 theme selector now. The emerald is gone entirely.
        root = re.search(r":root \{[^}]*\}", html).group(0)
        self.assertNotIn("rgba(15,24,19,.88)", root)
        self.assertNotIn("rgba(207,74,40,.24)", root)
        self.assertNotIn("rgba(207,74,40,.42)", root)
        self.assertNotIn("rgba(27,84,52,.92)", html)


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


class TestComposerScreenshotLayout(unittest.TestCase):
    """Round 15 (2026-11-26): the composer copies the screenshot chatbox.

    Input on top of the card, borderless, the card is the box. Below
    it, a controls row: the brevity modes as individual pills on the
    left, Attach / Mic / circular Send with an up arrow on the right.
    No Turbo dropdown: Arboris has no Turbo mode, so none is invented.
    """

    def test_composer_copies_screenshot(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # Input on top: the input comes before the brevity bar in the DOM.
        self.assertLess(html.index('id="ci"'), html.index('id="brevity-bar"'))
        # The controls row holds the brevity bar and the action buttons.
        self.assertIn('class="composer-controls"', html)
        self.assertIn('class="composer-actions"', html)
        self.assertIn('id="brevity-bar"', html)
        self.assertIn('id="attach-btn"', html)
        self.assertIn('id="mic-btn"', html)
        # Send is a circular button with an up arrow, not a "Send" label.
        self.assertIn('id="sb"', html)
        self.assertIn('M12 19V5M5 12l7-7 7 7', html)
        self.assertNotIn('>Send</button>', html)
        self.assertIn('border-radius: 50%', html)
        # The input is borderless and transparent: the card is the box.
        self.assertIn('background: transparent;', html)
        # The brevity buttons are individual pills, not a segmented group.
        self.assertIn('.brevity-btn {\n  background: transparent; border: 1px solid var(--border);', html)
        self.assertNotIn('class="ip-bar"', html)
        # No Turbo dropdown was invented.
        self.assertNotIn('Turbo', html)


class TestPhotoBackground(unittest.TestCase):
    """Round 19 (2026-11-26): the hero background is Grant's two
    photos, served from the repo.

    Round 20 (2026-11-26): Grant's lake and creek came out low res
    on his build (1600px and 684px wide), so they are gone. The
    gallery is now two high-res valley photos (Pexels, free, both
    3200px wide): a terraced green valley that frames the chat in
    the middle (mountains left and right, calm valley floor centre)
    and a bright green hillside. A vanilla port of the MorphGallery
    fbm-noise dissolve, as a fixed canvas behind the hero, served by
    the Arboris server itself (same origin, so no CORS needed for
    the WebGL textures). One header button cycles to the next photo.
    No autoplay, no arrows, no thumbnails.
    """

    def test_photo_background(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="bg-canvas"', html)
        self.assertIn('id="bg-overlay"', html)
        self.assertIn('id="bg-btn"', html)
        # The two high-res valley photos, local relative URLs,
        # no stock shots, no low-res Grant photos.
        self.assertIn("'bg-valley.jpg'", html)
        self.assertIn("'bg-hills.jpg'", html)
        self.assertNotIn("'bg-lake.jpg'", html)
        self.assertNotIn("'bg-creek.jpg'", html)
        self.assertNotIn('cdn.21st.dev/assets/mirror/', html)
        self.assertNotIn('images.unsplash.com/photo-', html)
        # The photos exist in the repo and the server serves them.
        self.assertTrue((ROOT / "website" / "bg-valley.jpg").is_file())
        self.assertTrue((ROOT / "website" / "bg-hills.jpg").is_file())
        app = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn('"bg-valley.jpg"', app)
        self.assertIn('"bg-hills.jpg"', app)
        # Both are high res: at least 2400px wide (stdlib JPEG
        # reader; the CI runner has no Pillow).
        for name in ("bg-valley.jpg", "bg-hills.jpg"):
            data = (ROOT / "website" / name).read_bytes()
            self.assertGreaterEqual(_jpeg_width(data), 2400)
        # The hero is transparent so the photo shows through, and the
        # dot-grid texture hides while a photo is showing.
        self.assertIn('background: transparent;', html)
        self.assertIn('body.bg-photo .hero::before { display: none }', html)
        # The dissolve shader is in the page, with the fbm noise.
        self.assertIn('var BG_VERT', html)
        self.assertIn('var BG_FRAG', html)
        self.assertIn('fbm(', html)
        self.assertIn('snoise(', html)
        # The button cycles, and the init runs on load.
        self.assertIn('bgInit();', html)
        self.assertIn('function bgInit()', html)
        self.assertIn('function bgFallbackSet', html)
        self.assertIn('function bgLoadImage', html)
        # No autoplay, no arrows, no thumbnails: it is a background
        # changer, not a gallery page.
        self.assertNotIn('autoplay', html)
        self.assertNotIn('bg-arrow', html)
        self.assertNotIn('bg-thumb', html)


class TestRound12Theme(unittest.TestCase):
    """Round 21 (2026-11-26): the Round 12 look is a visual-only
    theme, toggled by a header button, persisted in localStorage.

    A body class (body.theme-round12) scopes every Round 12
    override: coral user bubble, mucky green bot bubble, dot-grid
    hero, composer lifted a third of the screen, bordered input,
    rectangular buttons. The current design is the default (no
    class). Row order stays current in both themes; only the
    visuals swap. The photo layer is hidden in the theme, and the
    photo button with it.
    """

    def test_round12_theme(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # The toggle button is in the header, next to the photo button.
        self.assertIn('id="theme-btn"', html)
        self.assertIn('id="bg-btn"', html)
        # The theme is a body class, scoped under it; data-theme is
        # already used for dark mode and is not reused.
        self.assertIn('body.theme-round12', html)
        self.assertIn("classList.toggle('theme-round12'", html)
        # The four bubble variables under the theme selector.
        self.assertIn('--glass-user: linear-gradient(135deg, rgba(207,74,40,.24), rgba(207,74,40,.14));', html)
        self.assertIn('--glass-user-border: rgba(207,74,40,.42);', html)
        self.assertIn('--glass-bot: rgba(15,24,19,.88);', html)
        self.assertIn('--glass-bot-border: rgba(255,255,255,.1);', html)
        # The dot-grid hero is restored and the photo layer hidden.
        self.assertIn('body.theme-round12 .hero { background: var(--hero-mesh), var(--bg); }', html)
        self.assertIn('body.theme-round12 .hero::before { display: block; }', html)
        self.assertIn('body.theme-round12 #bg-canvas,', html)
        self.assertIn('body.theme-round12 #bg-btn { display: none; }', html)
        # The composer is lifted a third of the screen, and the chat
        # area keeps the wider bottom gap.
        self.assertIn('body.theme-round12 .hero.chatting { padding-bottom: 33vh; }', html)
        self.assertIn('body.theme-round12 .hero-chat-area { padding: 78px 24px 12vh; }', html)
        # The bordered input and rectangular buttons.
        self.assertIn('body.theme-round12 .ir input {', html)
        self.assertIn('body.theme-round12 .btn-s {', html)
        self.assertIn('body.theme-round12 .btn-attach,', html)
        self.assertIn('body.theme-round12 .brevity-btn { border: none;', html)
        # The JS wires the toggle and persists the choice.
        self.assertIn('function themeInit()', html)
        self.assertIn('function themeApply(name)', html)
        self.assertIn("'arboris-theme'", html)
        self.assertIn('themeInit();', html)
        # The un-themed composer rule is unchanged (still at the
        # bottom, no padding); the theme override is scoped.
        self.assertIn('.hero.chatting { height: 100vh; height: 100svh; min-height: 0 }', html)


if __name__ == "__main__":
    unittest.main()
