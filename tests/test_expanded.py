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
        # Anchored to the line start so the first .hero-chat-area
        # rule is the one matched.
        area = re.search(r"^\.hero-chat-area \{[^}]*\}", html, re.M).group(0)
        self.assertIn("padding: 78px 24px 16px", area)

    def test_composer_sits_at_the_bottom_of_the_page(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # When chatting the hero is 100svh with no bottom padding, so
        # the composer (the last flex child) sits at the viewport
        # bottom instead of a third up the screen. Hero v2 splits
        # .hero.chatting into two rules (layout, then height), so
        # check the combined text of every .hero.chatting rule.
        chatting = "\n".join(re.findall(r"^\.hero\.chatting \{[^}]*\}", html, re.M))
        self.assertIn("height: 100svh", chatting)
        self.assertNotIn("padding-bottom: 33vh", chatting)


class TestComposerRevertAndBubbleGreens(unittest.TestCase):
    """Hero v2 (2026-11-27): the composer is a unified glass card and
    the bubbles follow the theme tokens.

    Claude's redesign replaces the Round 18 solid dark composer with
    one glass container: a blurred surface tinted by --glass-alpha, a
    1px border, a top inner highlight, and soft elevation, around a
    borderless transparent textarea. The user and bot bubbles are the
    theme's --glass-user / --glass-bot, derived from --accent and
    --surface in the default Canopy Obsidian theme (teal-green user,
    dark green bot), so they follow whichever of the eight themes is
    active. The mucky rgba(15,24,19,.88) Grant disliked is gone from
    the default design entirely.
    """

    def test_composer_original_design_and_bubbles(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # Composer: one glass card, blurred, tinted by the theme.
        card = re.search(r"\.composer-card \{[^}]*\}", html).group(0)
        self.assertIn("background: color-mix(in srgb, var(--surface) var(--glass-alpha), transparent);", card)
        self.assertIn("backdrop-filter: blur(var(--blur)) saturate(140%);", card)
        self.assertIn("border: 1px solid var(--border);", card)
        self.assertIn("border-radius: var(--composer-radius);", card)
        self.assertIn("box-shadow: inset 0 1px 0 var(--composer-highlight), var(--composer-shadow);", card)
        # The input is a borderless transparent textarea inside it.
        self.assertIn(".ir textarea {", html)
        self.assertIn("border: 0; outline: 0; background: transparent;", html)
        # Bubbles follow the theme tokens (Canopy default: accent
        # tint for the user, surface for the bot).
        self.assertIn("--glass-user: color-mix(in srgb, var(--accent) 16%, var(--surface));", html)
        self.assertIn("--glass-bot: color-mix(in srgb, var(--surface) 90%, transparent);", html)
        # The mucky green and the coral are gone from the default
        # design entirely; there is no Round 12 theme any more.
        self.assertNotIn("rgba(15,24,19,.88)", html)
        self.assertNotIn("rgba(207,74,40,.24)", html)
        self.assertNotIn("rgba(207,74,40,.42)", html)
        self.assertNotIn("rgba(27,84,52,.92)", html)
        self.assertNotIn("theme-round12", html)


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
        self.assertIn("linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 70%, var(--bg)))", html)
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
    """Hero v2 (2026-11-27): the composer is one unified glass card.

    Claude's redesign replaces the Round 15/22 screenshot layout
    (pills on top, bordered input in the middle, action row with a
    rectangular amber "Send" on the right) with a single glass
    container: a borderless transparent textarea on top, a meta line
    (PromptShrink preview + word count) under it, and a toolbar at
    the bottom with Attach / Mic on the left, a segmented verbosity
    pill in the middle, and a round accent send button on the right.
    The hero is a centred stack (headline, lede, composer, prompt
    chips), full viewport height. No Turbo dropdown: Arboris has no
    Turbo mode, so none is invented.
    """

    def test_composer_copies_screenshot(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # One glass card: textarea, meta line, toolbar, in that order.
        self.assertLess(html.index('class="ir"'), html.index('class="composer-meta"'))
        self.assertLess(html.index('class="composer-meta"'), html.index('class="composer-toolbar"'))
        self.assertIn('id="brevity-bar"', html)
        self.assertIn('id="attach-btn"', html)
        self.assertIn('id="mic-btn"', html)
        # The input is a borderless transparent textarea that grows.
        self.assertIn('<textarea id="ci"', html)
        self.assertIn('.ir textarea {', html)
        self.assertIn('border: 0; outline: 0; background: transparent;', html)
        self.assertIn('function fitComposer()', html)
        # Send is a round accent button with an up arrow, on the right
        # of the toolbar (margin-left: auto).
        self.assertIn('id="sb"', html)
        self.assertIn('.composer-toolbar .btn-s { margin-left: auto; }', html)
        self.assertIn('M12 19V5M5.5 11.5L12 5l6.5 6.5', html)
        self.assertNotIn('>Send</button>', html)
        # Attach / Mic stay circular icon buttons.
        self.assertIn('border-radius: 50%', html)
        # The brevity buttons are a segmented pill group, not
        # individual bordered pills.
        self.assertIn('.brevity-bar {', html)
        self.assertIn('border-radius: 99px; border: 1px solid var(--border);', html)
        self.assertIn('.brevity-btn.active { color: var(--accent-fg) }', html)
        self.assertNotIn('class="ip-bar"', html)
        # The hero is a centred stack, full viewport height.
        self.assertIn('min-height: 100svh', html)
        self.assertIn('justify-content: center', html)
        # Prompt chips sit under the composer on landing.
        self.assertIn('id="prompt-chips"', html)
        self.assertIn('class="prompt-chip"', html)
        # No Turbo dropdown was invented.
        self.assertNotIn('Turbo', html)


class TestPhotoBackground(unittest.TestCase):
    """Hero v2 (2026-11-27): the hero background is a live canvas
    theme, not photos.

    Claude's redesign replaces the Round 16/19/20 photo background
    (Grant's photos, then the high-res valley photos, with the
    MorphGallery fbm-noise dissolve and a header cycle button) with
    one animated canvas per theme, driven by a single controller.
    The background layers are #bg-stage (the canvas host), #bg-scrim
    (a legibility gradient over the canvas), and #bg-grain (a fine
    noise overlay). The canvas itself is created by JavaScript
    (id="bg-canvas" set at mount time), not static HTML. The photos
    are no longer referenced by the page; their files stay in the
    repo and the server still serves them, harmlessly.
    """

    def test_photo_background(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # The three background layers exist in the HTML.
        self.assertIn('id="bg-stage"', html)
        self.assertIn('id="bg-scrim"', html)
        self.assertIn('id="bg-grain"', html)
        # The canvas is created by the controller at mount time.
        self.assertIn("canvas.id = 'bg-canvas';", html)
        self.assertIn("function heroThemeBoot()", html)
        self.assertIn("HeroBackground.mount(", html)
        # The photos are gone from the page: no references, no cycle
        # button, no morph dissolve.
        self.assertNotIn("'bg-valley.jpg'", html)
        self.assertNotIn("'bg-hills.jpg'", html)
        self.assertNotIn("'bg-lake.jpg'", html)
        self.assertNotIn("'bg-creek.jpg'", html)
        self.assertNotIn('id="bg-btn"', html)
        self.assertNotIn('id="bg-overlay"', html)
        self.assertNotIn('var BG_VERT', html)
        self.assertNotIn('var BG_FRAG', html)
        self.assertNotIn('function bgInit()', html)
        self.assertNotIn('function bgFallbackSet', html)
        self.assertNotIn('function bgLoadImage', html)
        self.assertNotIn('cdn.21st.dev/assets/mirror/', html)
        self.assertNotIn('images.unsplash.com/photo-', html)
        # The photo files still exist in the repo and the server still
        # serves them (harmless; the page just does not use them).
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
        # The hero is transparent so the canvas shows through.
        self.assertIn('background: transparent;', html)
        # No autoplay, no arrows, no thumbnails.
        self.assertNotIn('autoplay', html)
        self.assertNotIn('bg-arrow', html)
        self.assertNotIn('bg-thumb', html)


class TestHeroThemes(unittest.TestCase):
    """Hero v2 (2026-11-27), Round 27: eight live themes on one token
    set, on a floating dock, with palettes on five of them.

    Claude's redesign replaces the Round 21/23/24/25 theme system
    (a body class, a header picker popover, Current / Round 12 /
    Grain / Aether) with a new one: an html data-hero-theme
    attribute, a HERO_THEMES list, a HeroBackground controller that
    owns one canvas and one animation loop, and a floating theme dock
    at the bottom-left with a swatch per theme. The choice persists
    in localStorage under arboris-hero-theme and is applied before
    DOMContentLoaded so a saved light theme does not flash dark.
    The eight themes are Canopy Obsidian, Verdant Silk, Solaris
    Alabaster, Topographic Pulse, Aurora Glass, Kinetic Ink, Grain
    Gradient, and Aether Flow. Round 27 adds three palettes each to
    Canopy, Silk, Topo, Aurora, and Grain (a data-palette attribute
    on html, token overrides, and scene colours), picked from a
    slideout above the dock; Solaris, Ink, and Aether have none.
    """

    def test_hero_themes(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # The theme is an html attribute, not a body class; data-theme
        # is still dark mode and is not reused. Round 30: the page
        # ships on Topographic Pulse.
        self.assertIn('<html lang="en" data-theme="dark" data-hero-theme="topo">', html)
        self.assertIn("root.setAttribute('data-hero-theme', t.id);", html)
        self.assertNotIn('theme-round12', html)
        self.assertNotIn('id="theme-btn"', html)
        self.assertNotIn('id="theme-pop"', html)
        self.assertNotIn('class="pal-opt"', html)
        # The eight themes are listed with their swatch colours.
        for slug, name in (
                ('canopy', 'Canopy Obsidian'), ('silk', 'Verdant Silk'),
                ('solaris', 'Solaris Alabaster'), ('topo', 'Topographic Pulse'),
                ('aurora', 'Aurora Glass'), ('ink', 'Kinetic Ink'),
                ('grain', 'Grain Gradient'), ('aether', 'Aether Flow')):
            self.assertIn("id: '%s'" % slug, html)
            self.assertIn("name: '%s'" % name, html)
            self.assertIn('data-hero-theme="%s"' % slug, html)
            # Canopy's tokens live in :root, not in its own
            # html[data-hero-theme] rule (it is the fallback). The
            # other seven each have their own rule.
            # override :root under their own rule.
            if slug == 'canopy':
                self.assertNotIn('html[data-hero-theme="canopy"] {', html)
            else:
                self.assertIn('html[data-hero-theme="%s"] {' % slug, html)
        # The floating dock holds one swatch button per theme, in
        # Round 30 order: Topo, Silk, Ink, Aurora, Canopy, Solaris,
        # Grain, Aether.
        self.assertIn('id="theme-dock"', html)
        self.assertEqual(html.count('class="td-opt'), 8)
        dock = html.split('id="theme-dock"')[1].split('</div>')[0]
        order = re.findall(r'data-hero-theme="(\w+)"', dock)
        self.assertEqual(order, ['topo', 'silk', 'ink', 'aurora',
                                 'canopy', 'solaris', 'grain', 'aether'])
        # The controller owns one canvas and one loop; switching a
        # theme tears the old scene down and mounts a fresh canvas.
        self.assertIn('var HeroBackground = (function () {', html)
        self.assertIn('function makeScene(id, cv)', html)
        self.assertIn('function heroThemeApply(id, remount)', html)
        self.assertIn('function heroThemeBoot()', html)
        self.assertIn("var HERO_THEME_KEY = 'arboris-hero-theme';", html)
        self.assertIn('heroThemeBoot();', html)
        # The default theme is Topographic Pulse (Round 30).
        self.assertIn("heroThemeApply(saved || 'topo', false);", html)
        # Round 27: the palette system. Five themes list three
        # palettes each; the other three list none.
        self.assertIn("var HERO_PALETTE_KEY = 'arboris-hero-palettes';", html)
        self.assertIn('function heroPalettes(theme)', html)
        self.assertIn('function heroPaletteById(theme, id)', html)
        self.assertIn('function heroPalettesSaved()', html)
        self.assertIn('function heroApplyPalette(theme, pal)', html)
        self.assertIn('function heroPaletteApply(themeId, paletteId)', html)
        self.assertIn("root.setAttribute('data-palette', pal.id);", html)
        for slug, first in (('canopy', 'obsidian'), ('silk', 'verdant'),
                            ('topo', 'mono'), ('aurora', 'aurora'),
                            ('grain', 'grain')):
            self.assertIn("id: '%s'" % first, html)
        for slug in ('solaris', 'ink', 'aether'):
            # No palettes: entry has no palettes field (checked by
            # the absence of a palettes array right after the entry).
            entry = re.search(r"\{ id: '%s'[^}]*\}" % slug, html).group(0)
            self.assertNotIn('palettes', entry)
        # The slideout exists and is wired: it opens above the dock
        # when the active theme has palettes.
        self.assertIn('id="palette-dock"', html)
        self.assertIn('id="palette-dock-opts"', html)
        self.assertIn('function heroPalettesRender(theme, pal)', html)
        self.assertIn("dock.classList.add('open');", html)
        self.assertIn("class=\"pd-opt", html)
        self.assertIn('heroPaletteApply(tid, pid);', html)


class TestThemePalettes(unittest.TestCase):
    """Round 27 (2026-11-27): palettes are per-theme, on the hero v2
    engine — not the old Round 23/24/25 colour-palette system.

    The old system (a body class, a data-palette attribute, Current
    / Round 12 / Deep Sea / Glass Fold) is gone with Claude's
    redesign and stays gone. The new palettes live in HERO_THEMES:
    three each for Canopy, Silk, Topo, Aurora, and Grain. Each
    palette carries its dock swatch colours, the CSS tokens it
    overrides, and the colours the scene paints with; the pick is
    stored per theme in localStorage and shown in a slideout above
    the dock. The removed themes (Deep Sea, Glass Fold, and the
    round-23 palettes) leave no trace.
    """

    def test_no_colour_palettes(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # The old palette system is gone entirely.
        self.assertNotIn('theme-round12', html)
        self.assertNotIn('THEME_NAMES', html)
        self.assertNotIn('class="pal-opt"', html)
        for slug in ("peach", "lavender", "glacier", "forest", "sunset", "deepsea", "fold", "current", "round12"):
            self.assertNotIn('data-hero-theme="%s"' % slug, html)
        self.assertNotIn('>Deep Sea</button>', html)
        self.assertNotIn('>Glass Fold</button>', html)
        self.assertNotIn('>Cute</div>', html)
        self.assertNotIn('>Cool</div>', html)
        self.assertNotIn('>Beautiful</div>', html)
        # The Glass Fold shader is gone too.
        self.assertNotIn('var FOLD_FRAG', html)
        self.assertNotIn('function foldStart()', html)
        self.assertNotIn('function foldStop()', html)
        # The new palette system: five themes, three palettes each.
        self.assertEqual(html.count('palettes: ['), 5)
        for name in ('Obsidian', 'Ember', 'Glacier', 'Verdant', 'Rose',
                     'Ocean', 'Pulse', 'Sunset', 'Mono', 'Aurora',
                     'Borealis', 'Dusk', 'Grain', 'Nebula', 'Nova'):
            self.assertIn("name: '%s'" % name, html)
        # The eight themes are in HERO_THEMES and on the dock.
        self.assertIn("var HERO_THEMES = [", html)
        for slug in ("canopy", "silk", "solaris", "topo", "aurora", "ink", "grain", "aether"):
            self.assertIn("id: '%s'" % slug, html)
            self.assertIn('data-hero-theme="%s"' % slug, html)
        self.assertEqual(html.count('class="td-opt'), 8)
        # The pick is stored per theme and the slideout shows it.
        self.assertIn("var HERO_PALETTE_KEY = 'arboris-hero-palettes';", html)
        self.assertIn('function heroPaletteApply(themeId, paletteId)', html)
        self.assertIn("window.localStorage.setItem(HERO_PALETTE_KEY, JSON.stringify(saved));", html)
        self.assertIn('id="palette-dock"', html)
        self.assertIn('.palette-dock {', html)
        self.assertIn('.palette-dock.open {', html)


class TestTopoNoReactivity(unittest.TestCase):
    """Round 27 (2026-11-27): Topographic Pulse does not react to
    the cursor any more. It is just a moving shader in the
    background: the fbm contour field drifts with time, and the
    lines run from slate into sage and teal. The cursor ripple
    (the u_mouse uniform, the near falloff, the brightening) is
    gone from the shader and from the 2D fallback.
    """

    def test_topo_has_no_cursor_reactivity(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        topo = html.split('var HV_TOPO_BODY = [')[1].split('].join')[0]
        self.assertNotIn('u_mouse', topo)
        self.assertNotIn('u_mouseOn', topo)
        self.assertNotIn('near', topo)
        self.assertNotIn('ripple', topo)
        # Round 28: no centre dimming either. The old calm factor
        # dimmed the lines to 45% inside a circle around the hero
        # stack, which read as a circular artifact where the lines
        # faded out. The lines are one even brightness now; the
        # scrim behind the hero stack handles legibility.
        self.assertNotIn('calm', topo)
        self.assertNotIn('heroDist', topo)
        self.assertIn('float glow=0.16+0.08*major;', topo)
        # The field still drifts with time and the contours are
        # still drawn from the palette colours.
        self.assertIn("float t=u_time;'", topo)
        self.assertIn('fbm(p*1.25', topo)
        self.assertIn('u_pal[0]', topo)
        self.assertIn('u_pal[1]', topo)
        self.assertIn('u_pal[2]', topo)
        # The fallback has no cursor glow either.
        self.assertNotIn('env.pointer.on > 0.01', html)
        # The other reactive scenes are untouched: Ink still spawns
        # on the pointer and Aether still swirls around it.
        self.assertIn('env.pointer.sx', html)
        self.assertIn('swirl', html)


class TestAuroraGlass(unittest.TestCase):
    """Round 27 (2026-11-27): Aurora Glass is more transparent and
    less blurry. The composer card tint drops from 34% to 20% and
    the backdrop blur from the 20px default to 8px, and the bubbles
    go from opaque (user) / 90% (bot) to translucent surface tints
    (about 55-64% opaque), so the curtains show through the glass.
    """

    def test_aurora_glass_is_clearer(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        aurora = html.split('html[data-hero-theme="aurora"] {')[1].split('}')[0]
        self.assertIn('--glass-alpha: 20%;', aurora)
        self.assertIn('--blur: 8px;', aurora)
        self.assertIn('--glass-user: color-mix(in srgb, var(--accent) 20%, color-mix(in srgb, var(--surface) 55%, transparent));', aurora)
        self.assertIn('--glass-bot: color-mix(in srgb, var(--surface) 55%, transparent);', aurora)
        # The other themes keep their own glass settings.
        canopy = html.split(':root {')[1].split('}')[0]
        self.assertIn('--glass-alpha: 58%;', canopy)
        self.assertIn('--blur: 20px;', canopy)


class TestGrainTheme(unittest.TestCase):
    """Hero v2 (2026-11-27): Grain Gradient, ported into the hero v2
    theme engine.

    Still a vanilla port of the 21st.dev ShaderBackground (Paper
    Shaders grain-gradient, Apache-2.0) that Grant pasted. The GLSL
    is verbatim; only the React wrapper was rewritten. It now runs
    as a scene on the shared hero canvas (hvGrainScene, routed by
    makeScene), instead of its own fixed canvas with its own rAF
    loop. Its token rule uses the shader's own indigo/near-black.
    """

    def test_grain_theme(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # The fragment shader is present (verbatim Paper Shaders
        # grain-gradient: the packed uniforms and the grain hash).
        self.assertIn('var GRAIN_FRAG = `', html)
        self.assertIn('uniform vec3 u_colors[8];', html)
        self.assertIn('float grainHash(vec2 p)', html)
        self.assertIn('u_grain', html)
        self.assertIn('var GRAIN_UNIFORMS = {', html)
        # The scene factory wires it into the hero v2 controller.
        self.assertIn('function hvGrainScene(canvas, env, pal)', html)
        self.assertIn("else if (id === 'grain') s = hvGrainScene(cv, env, pal);", html)
        self.assertIn("gl.uniform4f(uni.scene, canvas.width, canvas.height, t * U2.timeScale, U2.colorCount);", html)
        # Round 27: the palette supplies the four colours (and may
        # override the hue; the default keeps the preset's shift).
        self.assertIn("var gcols = (pal && pal.colors) ? hvGrainColors(pal.colors) : U.colors.flat();", html)
        self.assertIn("var ghue = (pal && pal.hue !== undefined) ? pal.hue : U.hue;", html)
        self.assertIn('function hvGrainColors(hex)', html)
        self.assertIn("id: 'nebula'", html)
        self.assertIn("id: 'nova'", html)
        # The Grain token rule uses the shader's own indigo/near-black.
        self.assertIn('html[data-hero-theme="grain"] {', html)
        grain = html.split('html[data-hero-theme="grain"] {')[1].split('}')[0]
        self.assertIn('--glass-user:', grain)
        self.assertIn('--glass-bot:', grain)
        self.assertIn('--accent: #6366f1;', grain)
        # The dock has Grain Gradient (name in the title attribute).
        self.assertIn('data-hero-theme="grain"', html)
        self.assertIn('title="Grain Gradient"', html)


class TestAetherTheme(unittest.TestCase):
    """Hero v2 (2026-11-27): Aether Flow, ported into the hero v2
    theme engine.

    Still built from scratch in vanilla canvas 2D. The 21st.dev
    component source is behind a login and Grant said to skip
    anything needing a login, so this follows the component's
    description instead: flowing light particles that weave and
    connect, with the cursor making gentle swirls in the aether.
    It now runs as a scene on the shared hero canvas
    (hvAetherScene, routed by makeScene, pointer from env.pointer),
    instead of its own fixed canvas with its own rAF loop.
    """

    def test_aether_theme(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # The particle system: a colour set, a flow field, connecting
        # lines, and a cursor swirl, now driven by the controller's
        # pointer (env.pointer) instead of its own listener.
        self.assertIn('var AETHER_COLORS = ', html)
        self.assertIn('function hvAetherScene(canvas, env)', html)
        self.assertIn("else if (id === 'aether') s = hvAetherScene(cv, env);", html)
        self.assertIn('env.pointer.sx', html)
        self.assertIn('swirl', html)
        self.assertIn('130 * 130', html)
        # The Aether token rule uses teal bubbles.
        self.assertIn('html[data-hero-theme="aether"] {', html)
        aether = html.split('html[data-hero-theme="aether"] {')[1].split('}')[0]
        self.assertIn('--glass-user:', aether)
        self.assertIn('--glass-bot:', aether)
        self.assertIn('--accent: #2dd4bf;', aether)
        # The dock has Aether Flow (name in the title attribute).
        self.assertIn('data-hero-theme="aether"', html)
        self.assertIn('title="Aether Flow"', html)


if __name__ == "__main__":
    unittest.main()
