# Arboris chat website (live only)

The live site, Python package and CLI are all named **Arboris** (renamed from GreenSearch in 0.9.0). This page has no demo mode and no pre-written answers: every question goes to an Arboris backend, which holds the API key. The page never sees a key.

A ChatGPT-style site (`website/index.html` plus the static background image `website/hero.jpg`). On the hosted site the backend serves both `/index.html` and `/hero.jpg`, so the backend URL defaults to the same origin (`/api/chat`). On your own machine the default is `http://localhost:8000/api/chat`, i.e. `python server/app.py`. You can enter a different backend URL in the settings popover. See [SELFHOST.md](SELFHOST.md) for hosting it.

## What it does

- Chat input, multi-turn in one session. Every send goes to the backend as the raw text; the backend runs PromptShrink, its session cache, routing, the small-model cascade and the brevity trim, so nothing is shrunk twice.
- **Verbosity selector** (`Auto`, `Sprout`, `Canopy`, `Forest`): `Auto` routes each prompt automatically and displays the live predicted mode (`Auto · Sprout`, `Auto · Canopy`, or `Auto · Forest`) as you type, or you can override the mode for any turn.
- A typing preview under the input shows what PromptShrink would trim (client-side estimate only; the backend shrinks on send), and each sent question includes a collapsible **`✂ PromptShrink −N tok`** pill that expands an inline diff of removed filler phrases.
- Live answers stream back (SSE) with safe, zero-dependency Markdown formatting (code blocks, inline code, lists, bold/italic, tables) and show meta tags: model used (plus the model the provider reports, when that differs), whether it escalated, cache hits, provider-reported tokens, hidden `reasoning_tokens` (and `reasoning_effort` when enabled), `token_budget`, `answer_score`, latency, and a **truncated** marker when the answer hit the token cap.
- Live totals (provider-reported): live answers, input tokens, output tokens, estimated CO2e (`CO2e (est.)`), cache hits, escalations, and measured savings when the baseline checkbox is on.
- Per-answer actions: **Copy** copies the answer text; **More detail** asks for a longer answer to the same question (a second, primary-model call).
- Settings popover: backend URL, and **Measure baseline** (off by default; makes a second, unconstrained call per message to measure real savings, which doubles API cost).
- **Methodology & Finland section**: three reference cards below the chat fold covering routing & brevity modes, the output-weighted energy/CO2e formula, and the Google Cloud Finland (`europe-north1`, 98% carbon-free energy, grid 30 gCO2eq/kWh) reference. The section states clearly that the hosted backend runs in `us-central1`, not Finland, so Finland numbers do not change the live CO2 numbers. Clean-energy hosting is a separate lever.

### Saved in your browser (localStorage)

- Live turns are saved under the key **`arboris-live-v1`** after every completed answer. On reload the transcript is restored as it was, with no re-counting of totals.
- Old demo keys (`arboris-demo-v1`, `greensearch-demo-v1`) are deleted on load, not migrated. Live keys migrate from the old `greensearch-live-v1` / `greensearch-live-backend-url` names once, never overwriting a new key that already holds data.
- Corrupt or unreadable saved data is ignored. If storage is disabled (some private modes), the chat still works but won't persist.
- **Clear** (the ✕ next to Send) empties the chat and deletes the `arboris-live-v1` key. You can also delete it in your browser's dev tools (Application → Local Storage).

### Export

**Export** in the header downloads the transcript as a readable Markdown file named `arboris-chat-YYYY-MM-DD-HHMMSS.md`. It lists each live question and answer with its meta line (model, tokens, escalation, cache hit, baseline when measured). The file is built in the browser as a local Blob, with no server involved beyond the answers themselves.

### How Arboris works

The **How Arboris works** button in the header opens an in-page dialog, with no new page and no external link. It explains, in plain language, that answers come from a real model through the backend, that live token counts are provider-reported, that CO2e is an output-weighted estimate (not a measurement of any data centre), and that the baseline is assumed unless Measure baseline is on. It also notes that the live baseline study script (`scripts/baseline_study.py`) exists but has no systematic live results yet, so no live savings figure is claimed until it is run.

### Mobile and accessibility

- Buttons, message actions and the chat input are at least 44px tall (Send, Clear and the chat input 56px), and every control has a visible focus outline. The closed settings popover is not focusable.
- Icon buttons (theme, copy stats, clear, export, how-it-works, close) have aria-labels. Message action labels name the question they act on.
- Only new live answers are announced, through one polite live region. Re-rendering after a restore does not re-announce the whole transcript, and the cache flash is hidden from screen readers.
- Under 860px wide, the sidebar collapses behind a "Stats & how it works" toggle instead of a cramped strip.
- `prefers-reduced-motion` turns off CSS animation.
- Dark and light themes share one restrained accent (buttons, LIVE pill, links, focus rings, savings tags). Every text/background pair in both palettes was checked at 4.5:1 or better (WCAG AA) by calculation; it has not been checked in a browser.
- Base text is 17px (16px on phones), with larger chat bubbles, softer borders and shadows, and a divider-free totals row.

## Baseline, stated, not assumed

Live baseline per message, when Measure baseline is on: a second, unconstrained call to the same model, with no trimming, routing or reuse. When it is off, no live savings figure is claimed.
Live per message: trimmed question tokens + routed answer tokens (provider-reported), or trimmed question only on a cache hit.
CO2e is output-weighted: output tokens x 0.0006 Wh x 400 g/kWh (0.00024 g each), input tokens at 1/800 of that. All CO2e figures are estimates, not measurements.

## What it does not do

- No offline or demo answers. If the backend is unavailable, the answer bubble shows the backend error. There is no fallback to pre-written text.
- No topic browser, no sample chips, no edit or regenerate of live turns. (More detail is the only way to get a longer answer to the same question.)
- No measured ChatGPT or Gemini comparison. "Regular LLM" in the site means the verbose/full version of the same answer, as defined above.
- No Finland hosting yet. A live version in Finland would need an explicit model, key, and region choice from Grant.
- Saved chats live only in the browser that made them. They don't sync between devices and are lost if site data is cleared.

## Test

The inline script is checked from the repo root by `tests/site_conformance.js` (run through `python -m unittest discover -s tests`). Those tests take the first `<script>` block and run it in Node, so the pure logic must stay in that first block, and the DOM code must stay behind its `typeof document` guard. Manual checks worth repeating after edits:

- "Explain DNS simply" routes Sprout in the conformance output, and the typing preview shows the PromptShrink preview.
- Send a question with the backend running: a live answer streams in with meta tags, and the LIVE totals row updates.
- Reload: the live transcript and totals come back unchanged. Clear, then reload: everything is empty.
- Export with live turns: the downloaded file lists live answers only, with provider-reported tokens.
- With the backend down: the answer bubble shows the backend error, with no demo switch.

PromptShrink keeps words that carry instructions ("simply", "clearly") and does not collapse repeated numbers or words.

## Sources for Finland numbers

- Google Cloud region carbon data: https://cloud.google.com/sustainability/region-carbon
- Google 2026 Environmental Report: https://sustainability.google/files/google-2026-environmental-report/
