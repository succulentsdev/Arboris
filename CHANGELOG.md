# Changelog
- **Unreleased -- Website design pass, static hero image, and UI transparency (2026-10-30):**
  - Extracted the 905 KB inline base64 hero JPEG from `website/index.html` into `website/hero.jpg` (~663 KB binary JPEG) and updated `server/app.py` to serve `GET /hero.jpg` with `Cache-Control: public, max-age=86400`, cutting `website/index.html` from ~958 KB to ~33 KB.
  - Fixed malformed double `<head>`/`<body>` markup at the top of `website/index.html`, added `lang="en"`, removed ~180 lines of dead CSS/JS from the pre-0.9.0 demo layout, and fixed broken mobile CSS selectors.
  - Added interactive verbosity selector (`Auto`, `Sprout`, `Canopy`, `Forest`) above the chat composer; `Auto` displays the live predicted route (`Auto · Sprout/Canopy/Forest`) while typing and sends explicit `mode` overrides when selected.
  - Added collapsible `✂ PromptShrink −N tok` diff pill on sent user messages (`makeDiffRow()`) so users can inspect removed filler phrases after sending.
  - Added safe, zero-dependency Markdown formatting (`renderMarkdown`) for assistant answers (fenced code blocks, inline code, lists, bold/italic, tables) while escaping raw HTML before formatting.
  - Surfaced richer backend telemetry in answer badges and the totals bar: `reasoning_tokens`, `reasoning_effort`, `token_budget`, `answer_score`, `long_input_pruned`, `cache_fallback`, styled energy receipt pills, and cumulative `CO2e (est.)` in `#totals-live`.
  - Replaced the permanently hidden `.main` / `.sidebar` DOM block with a visible `#methodology` 3-card reference section (routing & brevity modes, output-weighted energy formula, and Finland `europe-north1` context) and linked it from the header.
  - Aligned client-side `shrinkSegment()` rule order in `website/index.html` with `arboris/shrink.py` (`verbose` phrase replacements before `fillers`).
- **Unreleased -- Reasoning effort caps and reasoning_tokens transparency (2026-10-30):**
  - L2 lever: opt-in `reasoning_effort` caps per mode (sprout→low, canopy→low, full→medium). When `ARBORIS_REASONING_EFFORT=1`, the server sends `reasoning_effort` in the provider payload, reducing hidden reasoning tokens on compatible models (o1, o3 via OpenRouter). Off by default; does not affect non-reasoning models.
  - `reasoning_tokens` transparency: providers now parse `completion_tokens_details.reasoning_tokens` from responses. These are INCLUDED in `completion_tokens` (not additive), so no energy formula change. Reported in meta, energy receipt, and the receipt note when present.
  - Providers, cascade, footprint, and server all forward the new fields end to end. All cache/local-calc paths report `reasoning_tokens: None`.
  - 15 new tests cover effort sent/omitted per config, per-mode mapping, reasoning_tokens in meta/receipt/note, local-calc and cache paths, provider payload forwarding, and provider response parsing.
- **Unreleased -- Repo hygiene and docs (2026-10-29):**
  - Main README rewritten in plain language and corrected: it said 0.8.0, Phase 2 local only, and a demo website, all of which are out of date. It now describes 0.9.0, the live-only website, and the hosted Cloud Run backend, and states the branch rule (main stable, work via dev).
  - Server README corrected the same way: hosted, not local only; no demo mode; deployment section now describes the real Cloud Run service, with Finland as a future move.
  - Added `.github/workflows/test.yml`: Python tests on 3.9 and 3.12 plus the site conformance check, on push and pull request to main or dev.
  - Added patch files, the redeploy script, and `results/` to `.gitignore`, so a blanket `git add` cannot sweep local artifacts into a commit again.
  - No AI authorship references were found in shipped docs or code to remove. The only Claude string in the repo is a model id (`anthropic/claude-sonnet-4`) used as test data in a model-size test, not a credit, so it stays.

- **Unreleased -- Server startup error prefix (2026-10-29):**
  - Server startup config errors now print `Error:` and exit 2, matching the CLI (gate G2). Previously it printed `Configuration error:`.

- **Unreleased -- Website is live only (2026-10-29):**
  - Removed the demo library, demo mode, topic browser, sample chips, demo totals and demo history from `website/index.html`. The page no longer contains the 95 pre-written topics (the original six templates plus 89 expanded topics), `detectTopic`, `greenAnswer`, `integrateChat`, or any demo answer path. Every question goes to the backend; if the backend is unavailable, the answer bubble shows the backend error, with no offline fallback and no switch back to a demo.
  - Live turns, live totals, the typing preview, Copy, More detail, Export (live answers only), settings (backend URL, Measure baseline) and live persistence under `arboris-live-v1` are unchanged in behaviour. Old demo keys (`arboris-demo-v1`, `greensearch-demo-v1`) are deleted on load; live keys still migrate from the old `greensearch-live-*` names once.
  - All site copy that described demo answers is rewritten live-only (sidebar, How dialog, footer, settings note, Finland panel). The stats strip no longer lists topics.
  - `website/README.md` and `website/SELFHOST.md` are rewritten for the live site (they previously described only the demo). Three tests that asserted demo behaviour are updated to assert live-only behaviour instead; the Python offline provider (`arboris --demo`, canned answers for 95 topics) is unchanged — only the website is live-only.
  - This push also includes the website must-fixes from the entry below (stats sourced, small-screen header/Send fixes, dark header backing, settings popover focus, savings pill colour, unified Send, meta/OG/favicon), which were built locally but not previously pushed.

- **Unreleased -- Website must-fixes (2026-10-29):**
  - Stats strip is now sourced: "95 topics" (demo library: 6 hand-written + 89 more, matching the rest of the site) replaces "89+", and "32% fewer input tokens" (corpus: PromptShrink v1 saved 80 of 247 input tokens) replaces the unsourced "~40% fewer tokens".
  - Small screens: the hero input gets `min-width: 0` so it no longer pushes Send off-screen; under 540 px the header stays on one row with smaller icon buttons and the hero landing gets top clearance; under 400 px Send and the input shrink instead of clipping.
  - Dark-mode header over the photo gets a stronger gradient plus backdrop blur, so header icons and the LIVE pill stay readable.
  - The closed settings popover is now `visibility: hidden` as well as transparent, so its fields are no longer focusable while closed. The backend URL field and settings inputs get a real focus outline instead of a border-colour change only.
  - The savings pill (`.spill`, "-N% tokens · X CO2e saved") is teal-green instead of coral, so savings no longer read as red. The mid-savings amber variant is unchanged.
  - Send uses the same coral background and dark ink in both themes (it was coral in dark mode, teal in light mode).
  - Added a meta description, Open Graph tags, and an inline-SVG favicon (no external request).
  - Not done here, needs a decision: the photo is a 905 KB base64 background in the single-file page, so AVIF/WebP + srcset does not apply directly; that LCP fix means splitting the photo out of the page, which changes how the site is deployed. CLS was not re-measured in this batch; no Playwright pass is claimed.

- **0.9.0 -- Renamed from GreenSearch to Arboris (2026-10-29):**
  - The Python package is now `arboris` (`import arboris`, `python -m arboris`), the CLI command is `arboris`, the PyPI distribution name is `arboris`, and the optional extra is `arboris[tokenizer]`. Environment variables are `ARBORIS_*` (for example `ARBORIS_API_KEY`, `ARBORIS_MODEL`, `ARBORIS_BACKEND_ORIGIN`). The site's localStorage keys are `arboris-demo-v1`, `arboris-live-v1` and `arboris-live-backend-url`. Docs, docstrings, the CLI banner, the session report heading, the server's log prefix and `Server` header all say Arboris.
  - One-release compatibility, removed after 0.9.0: a `greensearch` shim package forwards every `greensearch.<module>` to the same `arboris.<module>` object and raises a DeprecationWarning; `python -m greensearch` and the `greensearch` command run the Arboris CLI; the server reads each `GREENSEARCH_*` variable when its `ARBORIS_*` twin is unset or blank (an `ARBORIS_*` value always wins) and logs the legacy names it used at startup; the live provider accepts `GREENSEARCH_API_KEY` when `ARBORIS_API_KEY` is unset; the site moves chats saved under the old `greensearch-*` keys to the new keys on first load and never overwrites a new key that already holds data.
  - Removed: the `ECOWRAPPER_API_KEY` fallback, which was promised for one version at the 0.3.0 rename.
  - `server/CLOUDRUN.md` uses the service name `arboris` and secret `arboris-api-key`, and adds a migration section: Cloud Run cannot rename a service, so the existing `greensearch` service keeps running on its old variables until a new `arboris` service (optionally in europe-north1) is verified and the old one deleted. Hosting region covers the server only, never the model's inference.
  - Not renamed here: the GitHub repository (the owner renames it in GitHub settings; GitHub redirects the old URL, so the site's links keep working) and the existing Cloud Run service.
  - `tests/test_greensearch.py` is now `tests/test_arboris.py`; `tests/test_rename.py` covers the shim, the env fallbacks and the storage migration.

- **Unreleased -- Output-weighted energy estimates (2026-10-29):**
  - Energy is now output-weighted everywhere: output tokens x 0.0006 Wh plus input tokens x 0.0006 / 800 Wh, at the same assumed 400 g CO2e/kWh. 0.0006 Wh per output token is derived from Epoch AI's 2025 estimate (about 0.3 Wh for a GPT-4o query with 500 output tokens); 1/800 is the midpoint of the 750-850x output-to-input energy ratio Ruf and Detyniecki measured on vLLM (HotCarbon '26). The old default, 0.0045 Wh for every token, priced answers 7 to 9 times above the 2025 published estimates. Every energy, CO2e and water figure drops accordingly; token counts and token savings are unchanged.
  - `energy_receipt` adds `factors.weighting`, `wh_per_input_token` and `input_output_energy_ratio`, rounds to 6 decimals so small answers no longer round to zero, and rejects a non-positive ratio. `footprint()` (output tokens only) uses the new default; `CO2_PER_TOKEN_G` is now 0.00024.
  - Website: demo CO2e uses the same output-weighted formula; the reply pill now reads "-N% tokens · X mg CO2e saved"; small values show in mg or µg instead of "0.00g"; live receipts format Wh and CO2e the same way. The phone-charge comparison is removed: it treated one charge as 0.005 g CO2e, roughly a thousand times too small. The remaining comparison is seconds of a 9 W LED at the same assumed grid intensity.
  - README, server README, website README and the in-page methodology now describe the output-weighted formula and its sources.

- **Unreleased -- Energy receipt citations corrected (2026-10-29):**
  - The receipt's `wh_per_token_range` is now one cited range for every model, 0.0006 to 0.005 Wh per generated token, derived from two published anchors: Epoch AI (Feb 2025, about 0.3 Wh for a GPT-4o query with 500 output tokens) and Luccioni, Jernite and Strubell (ACM FAccT 2024, about 0.05 kWh per 1,000 ten-token generations on models up to 11B). The default 0.0045 Wh/token sits inside it, near the high end. The per-size ranges added on 2026-10-28 are removed: their cited sources did not state those numbers, which broke the honest-methodology rule.
  - `model_size_tier` now reads a parameter count from the model id only, prefers the active count for mixture-of-experts ids ("30b-a3b"), sizes "8x7b" ids by their total, ignores quantisation tags such as "4bit", and returns "unknown" instead of guessing from brand names. The tier is reported for information and does not change any factor.
  - The receipt reports `grid_region: "unknown"` and its note says the inference region is not disclosed, so the grid factor is illustrative (added 2026-10-28, now covered by tests).
  - New tests cover the range, the unknown region, and thirteen model-id cases.

- **Unreleased -- Efficiency brief, second wave (2026-10-28):**
  - Expand-on-demand (brief #3): `POST /api/chat` accepts `expand_of` (the original prompt). An expansion always goes full mode and primary tier, with "Explain in detail." instead of the task format suffix, and meta reports `expansion` and `expanded_from`. The site adds a "More detail" button under each live answer (not on expansions themselves), which sends a second, primary-model call; the first answer stays answer-first by design. Expansion counts are in the live totals (counted, element not yet in the totals bar) and in the server log.
  - Format by task, continued (brief #4): comparison reasoning now gets "Give the conclusion first, then a comparison table." instead of the generic conclusion-first suffix. Stop sequences, task temperatures, and a max_tokens safety net are all opt-in: `GREENSEARCH_TASK_STOP_SEQUENCES=1` sends stop `["\n\n"]` for fact/definition prompts only (never for expansions), `GREENSEARCH_TASK_TEMPERATURE=1` sends 0.2 for fact/definition/math/code, 0.5 for steps/reasoning, 0.8 for creative, provider default for other, and `GREENSEARCH_MAX_TOKENS_MULTIPLIER` (default 1.0, max 4) scales the per-mode cap so it can be a safety net instead of the budget. Provider, cascade, chain, and recorder forward stop/temperature only when set, so providers without those parameters keep working. Meta reports `max_tokens_used`, `temperature`, and `stop_sequences`.
  - Escalation on evidence, continued (brief #5): a cached answer that was truncated or scored below 0.3 is not reused on a repeat; the model is called again, and if that call fails the cached answer is returned as a fallback (meta `cache_fallback: true`). The 0.3 bar is stricter than the 0.5 escalation threshold, so only clearly bad answers bypass the cache. Semantic cache hits apply the same bad-answer rule, without the fallback (a similar answer is not a safe fallback). Meta also reports `questions` and `multi_part` on every answer, for future routing measurement.
  - Long inputs (brief #6): `greensearch/long_input.py` plus opt-in `GREENSEARCH_LONG_INPUT_PRUNING=1` (threshold `GREENSEARCH_LONG_INPUT_MAX_CHARS`, default 1500, minimum 500). First/last sentences plus sentences with digits, questions, quoted strings, proper nouns, or code are kept; other sentences are dropped, with a pruning note appended so the model knows input was pruned. Only the model prompt is pruned; routing, cache key, and the original question use the unpruned prompt. Normal chat prompts are untouched when the flag is off, and short prompts are untouched even when it is on.
  - Measurement (brief #7): `scripts/task_stats.py` aggregates server log lines by task type (counts, cache/local/escalation/expansion rates, total and average tokens per task, average score, average latency), in table or JSON form. Server log lines for cache hits, semantic hits, cache fallbacks, and local answers now include task and tier, and model lines include expansion, so the script can count them. Follow-up rate within a session is still not derivable from logs (the server never logs session IDs); expansion and re-ask flags in meta are the proxy.
  - Not built, on purpose: a learned router (still no logged outcomes; task_stats is the collection step that makes it possible later), a judge-model cascade, live baseline measurement (parked by Grant, needs his key and a free-pool window), self-hosting (Phase 3 fork), and multi-turn history (server stays single-turn; the brief's low-value verdict for this wrapper still holds).

- **Unreleased -- Efficiency follow-ups: single-word fix, log enrichment, quality golden set (2026-10-28):**
  - Single-word answers are no longer marked truncated when the provider explicitly finished: brevity's "Paris" -> "Paris…" mark is kept only when the finish reason is not "stop" (notably "length", or unreported). A multi-word fragment still keeps its mark. This closes the parked single-word "…" side effect from the live bug-fix push, server-side only; the CLI and brevity module are untouched.
  - Usage log lines now include task type, model tier, and answer score (`task=… tier=… score=…`), so routed answers can be judged from Cloud Run logs without a new tool.
  - Quality golden set (`spec/quality_cases.json`, 30 prompts across fact, definition, steps, reasoning, math, code, creative, each with key facts a correct answer must contain) plus `scripts/quality_check.py`, a stdlib-only scorer for saved live answers (pass/fail/missing, exit 1 unless every case passes). The judge is deliberately weak and transparent (case-insensitive substring match); it catches missing facts, it does not judge style or reasoning, and creative cases only check topic. It is preparation for the brief's before/after measurement, not a measurement itself: do not run it against demo canned answers.
  - Not built, on purpose: a learned router (no logged outcomes yet), a judge-model cascade (a second model call), stop-sequence changes (brevity already controls length; no measured need), and long-input extractive pruning (that belongs to the summarizer thread, not the chat wrapper).

- **Unreleased -- FrugalGPT / RouteLLM adaptations, opt-in (2026-10-28):**
  - Transparent answer scorer (`greensearch/scorer.py`, FrugalGPT-style but local, no model call, no learned black box): logprob, completeness, format, and coverage components, weighted 0.4/0.3/0.15/0.15, reported in meta as `answer_score` on every new answer. It triggers escalation only when `GREENSEARCH_SCORER_ESCALATION=1` (off by default) and the score is below `GREENSEARCH_SCORE_THRESHOLD` (default 0.5); unlike logprobs, it works when logprobs are unavailable. The logprob cascade keeps its old behaviour when scorer escalation is off.
  - Difficulty score (`difficulty_score` in `greensearch/task_router.py`, RouteLLM-style but feature-based, not learned): a 0-1 score calibrated to reproduce the task tiers exactly at threshold 0.5 (golden-set invariant test), so `GREENSEARCH_DIFFICULTY_ROUTING=1` alone changes nothing; moving `GREENSEARCH_ROUTER_THRESHOLD` (0-1) or `GREENSEARCH_QUALITY_TARGET` (`eco` 0.7, `balanced` uses the threshold, `quality` 0.3) is what moves traffic between tiers. Requires task routing on; meta reports `difficulty_score`, `difficulty_tier`, and `router_threshold`.
  - No-model gate (`greensearch/local_calc.py`): pure arithmetic is parsed (never executed) and answered locally with 0 tokens and 0 estimated energy; division by zero, any letters, `**`, `//`, unbalanced parentheses, or prompts over 100 characters fall through to the model. Meta reports `local_answer: true`, `model_tier: "local"`.
  - Budget-aware escalation: `GREENSEARCH_MAX_CASCADE_OUTPUT_TOKENS` (0 disables) stops escalation when the primary answer already used more than that many output tokens, even on a low-confidence trigger; unreported tokens never block escalation.
  - Conservative lexical semantic cache (`GREENSEARCH_SEMANTIC_CACHE=1`, off by default): fact/definition prompts with difflib ratio at least 0.90 and token overlap at least 0.80 against a cached prompt in the same session, with identical numbers, reuse the cached answer. String similarity, not an embedding model, so it misses true paraphrases rather than risking a wrong answer. Meta reports `semantic_cache_hit` and `semantic_similarity`.
  - Website: live answers now render the energy receipt and task meta as tags under each answer (energy Wh / gCO2e / water range with "(est.)", task type and tier, "answered locally", "semantic cache hit"), and the same information in the Markdown export line. Read-only display of existing meta only; no input, layout, or request behaviour changed. This was a functional change under the 2026-10-28 "build out the whole website" instruction.
  - Not built, on purpose: a learned router (no logged outcomes to train on yet), a judge-model cascade (a second model call, more expensive than the scorer), and any change to the brevity system prompt (answer-first is already in the task format suffixes; changing the global prompt would drift the baseline for no measured gain).

- **Unreleased -- Task-type routing, opt-in (2026-10-28):**
  - New `greensearch/task_router.py`: rule-based, testable classification into fact, definition, steps, reasoning, math, code, creative, and other, with a golden set in `spec/task_cases.json` (19 cases). It replaces word-count routing for auto mode only when `GREENSEARCH_TASK_ROUTING=1` (off by default); unknown prompts fall back to word-count routing, and an explicit client mode always wins.
  - Two cases word-count routing got backwards, now fixed: a long, easy fact ("Could you please tell me... what the capital of France is?") goes small/sprout regardless of polite word count, and a short, hard question ("Prove the Pythagorean theorem.") goes primary/full regardless of brevity. When task routing is on, the task tier replaces the sprout rule for small-model routing (small-model routing must still be enabled separately).
  - Task-appropriate format suffixes (under ten words each, e.g. facts answer directly with no preamble, steps use a numbered list) are appended to the brevity system prompt, and meta reports `task_type`, `task_tier`, and `routed_by` on every answer, so routed answers can be judged before task routing is left on. Website JS routing is untouched; live server routing is what changed.

- **Unreleased -- Per-answer energy receipt (2026-10-28):**
  - Every live answer now carries `meta.energy`: total tokens (provider-reported input + output), estimated Wh (0.0045 Wh/token), gCO2e at the configured grid factor (`GREENSEARCH_GRID_G_PER_KWH`, default 400, illustrative not measured), and cooling water as a range (WUE 0.1-0.3 L/kWh, generation water excluded), with the factors and basis stated in the receipt itself. Basis is always "estimate", never carbon accounting.
  - Cache hits report 0 Wh / 0 g / 0 mL with a note (no new model call). If the provider did not report usage, status is "unavailable" and no number is invented. Escalated answers cover both calls. No region is claimed: OpenRouter does not disclose inference location.
  - Site rendering (done in the FrugalGPT / RouteLLM batch below): live answers now show the receipt as tags under each answer, plus the same information in the Markdown export line. Not a collapsible panel; the tags carry the same figures with "(est.)" and the full note on hover.

- **Unreleased -- Empty-content fallback + PromptShrink v2 server switch (2026-10-28):**
  - Reliability: a model returning HTTP 200 with null/empty text no longer ends the request. The server chain now treats it as transient and tries the next model, and only if every model returns empty does it return "The model returned no answer, try again" (502). Previously that error had no HTTP status, so the chain stopped at the first model. The baseline study retries empty content the same way, with a short 3s delay instead of the 429 backoff.
  - Efficiency, opt-in: new `GREENSEARCH_SHRINK_VERSION` (default `v1`, `v2` to enable, anything else refuses to start). With `v2`, live requests use `shrink_v2()` (39% vs 32% saved on the corpus); meta reports the shrink version used, so v1-vs-v2 live answers can be judged without a code change. Default is still v1 until that judgment is made.

- **Unreleased -- Website restored to pre-breakage version (2026-10-28):**
  - `website/index.html` restored byte-for-byte to commit `e911b6d6` (UNDERSTORY, live-first copy applied), the version live at the PromptShrink v2 report, before the hide cleanup, live-only deletion, hero-removal, and third-version changes that broke the chat.
  - Restored: hero photo + title, sidebar (How + Finland panels), features section, main chat input, Live toggle (on by default on `*.run.app`, off elsewhere), 95-topic demo library as fallback, and demo conversation memory. Google Fonts stay removed, as in `e911b6d6`.
  - Site tests reverted to match: `test_website_live_mode_off_by_default` and the expanded-topic JS parity test are back; the live-only assertions are gone. Server safeguards and small-model routing are untouched (server-only, still unpushed separately).

- **Unreleased -- Small-model routing, opt-in (2026-10-28):**
  - New `GREENSEARCH_SMALL_MODEL_ROUTING` (off by default) and `GREENSEARCH_SMALL_MODEL` (default `liquid/lfm-2.5-2.6b:free`, 2.6B). When on, sprout prompts (short, simple, per `route_mode`) try the small model first, then the primary chain (31B Gemma + fallbacks) on failure. Canopy/full prompts always use the primary chain.
  - This is the biggest efficiency lever so far: energy per token scales roughly with model size, so a 2.6B answer for a simple question costs far less than a 31B answer. No quality claim is made for the small model: routing is a length heuristic, not a difficulty measurement, and it stays off until live answers are judged.
  - Meta reports `routed_to_small` and `model_tier` (`small`/`primary`) on every answer, including cache hits. `fallback_used` still means the answer came from the fallback list, not from the primary or the routed small model. Session cache keys gain a small tier prefix, so small and primary cached answers never mix.
  - Paid guard extended: a non-free small model also refuses to start without `GREENSEARCH_ALLOW_PAID_MODELS=1`. Health and startup log report the small model and whether routing is on.

- **Unreleased -- POC abuse safeguards (2026-10-28):**
  - Per-IP caps tightened: 30/hour (was 100) plus a new 100/day per-IP cap, and a new 800/day global cap across all IPs, all rolling windows, all env-overridable (0 disables each). The global cap sits below OpenRouter's free-model daily quota (1000/day after a $10 credit), so abuse burns the day's allowance, not the credit balance.
  - Paid models blocked by default: server refuses to start on a non-free primary, fallback, or escalation model unless `GREENSEARCH_ALLOW_PAID_MODELS=1` is set deliberately. Default chain stays free-only, so the $10 credit is not spent by normal traffic.
  - Usage logging: one stderr line per chat (Cloud Run logs) with model, input/output tokens, cache hit, escalation, latency; no prompt text unless debug logging is on.
  - Honest limits, documented in server/README.md: caps are in-memory and per-process, so they multiply by instance count on Cloud Run and reset on restart/deploy. This is proof-of-concept protection, not commercial metering.
  - Kept from before: CORS allowlist only, 4000-character prompt cap, 16 KB body cap, client-sent keys/models/URLs rejected, session cache (200 entries per session).

- **Unreleased -- Live-only site (demo library removed 2026-10-28):**
  - Deleted from website/index.html: EXPANDED_TOPICS (89 topics), TEMPLATES, TOPIC_META, detectTopic, greenAnswer, integrateChat, demo send/regenerate/edit, demo totals/history, topic browser, chips, sidebar, hero input, stats bar, demo persistence (saveState/loadState), restore note, and dead demo helpers. File ~1.10 MB -> ~0.97 MB (photo data URI unchanged).
  - Site is live-only: no Live toggle, no demo pill, no demo fallback; every question goes to the backend via sendLive. Live totals are the only totals; live URL + Measure baseline controls stay (config, not conversation memory).
  - No conversation memory: nothing is saved or restored; on load the site deletes any old greensearch-demo-v1 session from localStorage. Live answers were already never written to localStorage.
  - Kept as requested: "How Arboris trims the cost of every conversation" features section, hero title/sub, header, live totals, chat, main input/Send, footer, How dialog (rewritten live-only, no demo sections).
  - Welcome message changed to "Ask anything."
  - Tests updated for the intentional change: TestWebsiteExpandedParity parity test replaced with a demo-library-removed assertion; test_website_live_mode_off_by_default replaced with test_website_live_only. Python DemoProvider and Python expanded-topic tests are untouched (harness only, not the site).

- **Unreleased -- Live-only cleanup (demo UI hidden, not deleted):**
  - Hidden via one CSS block (delete it to restore): hero input/chips/scroll hint (duplicates the main input), stats bar (unmeasured marketing numbers), features section, sidebar (How-it-works panel, Topic browser, Finland panel), sample-question chips under the main input.
  - Demo totals strip now hidden in live mode and shown in demo mode (was always visible); live totals are the only totals in live mode.
  - Welcome message neutralised to "Hi, I'm Arboris. Ask me anything." (no demo-topic list).
  - Demo JS bones untouched: greenAnswer, 95-topic data, renderTopics, demo answers, How dialog (header button) all still in the file, just not rendered visibly. File size unchanged (~1.1 MB) because nothing was deleted.

- **Unreleased -- PromptShrink v2 (opt-in, not yet switched on):**
  - New `shrink_v2()` in `greensearch/shrink.py`: v1, plus redundancy removal (consecutive duplicate words, exact duplicate sentences, code fences untouched), plus telegraphic rewriting gated to simple single-sentence prompts (at most 25 words, no fence/newline/colon structure): articles (a/an/the) and "to/for me|you|us" removed. Numbers, constraint words, question words, and keywords are never removed, and multi-sentence/creative prompts get redundancy only.
  - `shrink()` is unchanged, so the CLI and baseline study keep v1 numbers until a switch is explicitly approved. Tokenizer-aware counting is via the existing `count_fn` argument (pass `count_tokens` with backend="tiktoken" for real tokens).
  - New quality harness `scripts/shrink_quality.py`: reports tokens saved AND answers changed (DemoProvider, offline). On the 12-prompt corpus (estimate tokens): v1 32% saved, v2 39% saved (17 extra tokens), 0 answers changed v2 vs v1. That answer check is canned-keyword only, not a live-model quality judgement.

- **Unreleased -- UNDERSTORY design pass, live-first:**
  - Website replaced with UNDERSTORY design pass (deep-green hero, hero input + chips, features section, same landscape photo embedded, Playfair/DM Sans look via system serif/sans fallbacks).
  - Live-first copy applied on top (same substance as the previous live-first rewrite): How dialog is "Live first, demo as fallback", live backend already running, measured baseline still not run, hosted backend us-central1 not Finland, Live on by default on run.app, off elsewhere, demo as fallback.
  - Fixes to pass the existing suite: hero input bridge guarded for non-browser (Node) execution, Google Fonts removed (privacy copy says no outside fonts; v0.7 removed them), page-copy em dashes removed (canned-answer escapes untouched).
  - Previous landscape main site kept at ~/workspace/your_files/arboris-redesign-options/previous-landscape-main.html (local only, not in repo).

- **Unreleased -- Baseline study does not retry a daily-cap 429:**
  - A `free-models-per-day` 429 now aborts immediately instead of retrying, because OpenRouter counts failed attempts toward the daily free-model quota. Per-minute 429s still retry with backoff. Held from push pending a working baseline run.

- **Unreleased -- Baseline study survives free-model throttling:**
  - `scripts/baseline_study.py` now retries HTTP 429 only (15s, 30s, 60s, 120s, 240s backoff) and waits 3s between live calls; other errors still abort immediately. Previously the first 429 killed the whole 24-call run and wrote nothing.
  - New flags: `--retries`, `--retry-delay`, `--inter-call-delay`. Server behaviour unchanged (it already has its own fallback logic).

- **Unreleased -- Live-first copy rewrite:**
  - Website copy is now live-first, with canned demo kept as fallback (code unchanged, only copy and the Live default changed).
  - How dialog: "Canned answers, not a live model" is now "Live first, demo as fallback"; the live study section now says the hosted backend is already running with a configured key, and what is still missing is a systematic measured baseline (BASELINE.md still uses canned numbers, no live savings invented).
  - Live mode is on by default on the hosted site (run.app, same-origin backend) and off by default elsewhere; footer, hero, and mode notes updated accordingly. Demo answers remain pre-written for 95 topics, used when Live is off or the backend fails.
  - Honesty corrections: hosted backend currently runs in us-central1, not Finland (Finland is a future lever, not a current claim); live token counts are provider-reported, demo counts are still estimates, and live savings vs baseline are measured only when Measure baseline is on.

- **Unreleased -- Arboris landscape redesign (to push):**
  - Website completely redesigned from the TaniScan-inspired canopy option, with Grant's landscape photo (mountain forest at sunset, 2400x1350) as the full-bleed background instead of the AI canopy, embedded in the page as a data URI (the GitHub snapshot push cannot carry binary files, so the photo is not a separate repo asset).
  - Green filter turned way down: scrim was a uniform 78% dark green, now a gradient mostly 30-35%, 70% only at the very top for nav/headline readability, 50% at the bottom for the chat. The photo shows through clearly; white text over the bright sunset sky is the tightest contrast case and still needs Grant's browser pass.
  - Live mode is now always visible: a pill in the header nav ("Live: off" / solid green "LIVE"), opening a slim row under the nav with the backend URL field, baseline checkbox, warning, and live totals. It is no longer buried in a settings drawer.
  - No big totals boxes: savings as small inline metadata under each answer and small leader-line stats, per the earlier brief. All 95 topics, demo + live mode, PromptShrink preview, Copy/Regenerate/Edit, history, and How-it-works kept.
  - Option 2 (Vario-inspired canopy/landscape) is NOT pushed and is kept as a fallback: ~/workspace/your_files/arboris-redesign-options/option-2-landscape.html (and forest-canopy-vario.html).

- **Unreleased -- Phase 2 (local only, not deployed, not pushed):**
  - Model, CORS and site pass (local only; version stays 0.8.0):
    - Default model is now `google/gemma-4-31b-it:free`, a named general
      chat model (server default, `.env.example`, `--help`, docs).
      `GREENSEARCH_MODEL_FALLBACKS` default is now
      `liquid/lfm-2.5-2.6b:free,thinkingmachines/inkling-small:free,poolside/laguna-xs-2.1:free,openrouter/free`:
      gemma moved out (it is the primary) and `openrouter/free` is the
      last resort only. Why: as primary, `openrouter/free` routed a
      hamburger question to a content-safety model that answered "User
      Safety: safe...". The router can pick non-chat free models, and
      fallback only triggers on errors, not on wrong-type answers. This
      supersedes the `openrouter/free` default noted below.
    - CORS: `GREENSEARCH_BACKEND_ORIGIN` now splits on commas and/or
      whitespace (`re.split(r"[,\s]+")`, empties and repeats dropped), so
      a space-separated list no longer becomes one unmatchable origin.
      Trailing slashes are still stripped; matching is still exact and
      `*` is still refused. `server/CLOUDRUN.md` now uses an env-vars YAML
      file by default and warns that `--set-env-vars` replaces the whole
      env list (a single-var `--set-env-vars` wiped the CORS allowlist in
      production) while `--update-env-vars` keeps the rest. `env.yaml` is
      git- and docker-ignored.
    - Website renamed to **Arboris** in everything a visitor sees (title,
      wordmark, totals label, sidebar, footer, How-it-works dialog,
      welcome, copied stats, Markdown export and its file name
      `arboris-chat-*.md`). Package, CLI, repo, localStorage keys and API
      paths stay `greensearch`.
    - Website redesign (CSS only): one restrained green accent (buttons,
      LIVE pill, links, focus rings, savings tags), 17px base text, larger
      bubbles, a 56px input and Send/Clear, more whitespace, softer borders
      and shadows, a divider-free totals row, lighter metadata tags. All
      controls kept. Text contrast is 4.5:1 or better in both themes, by
      calculation only (not checked in a browser).
    - Live metadata row now shows `meta.provider_model` when present and
      different from `meta.model`, and a "truncated" tag when
      `meta.truncated` is true.
    - `scripts/baseline_study.py` `--model` default is now
      `google/gemma-4-31b-it:free` (was `gpt-4o-mini`). BASELINE.md has a
      "NOT YET MEASURED" section with the local run steps. No live
      baseline was run; the canned numbers are unchanged.
    - Tests: 160 to 162 (comma, space, mixed, trailing-slash and repeat
      origin parsing; exact matching of a space-separated list over HTTP;
      the new default model and fallback order).
  - Cloud Run prep (not deployed, no GCP account used): `Dockerfile`
    (Python 3.12-slim, stdlib only, binds 0.0.0.0 and reads $PORT),
    `.dockerignore` (never ships `.env`), and `server/CLOUDRUN.md`
    (project, Secret Manager key, deploy, CORS allowlist update, and
    teardown steps). Website live URL now defaults to the same origin
    on `*.run.app` hosts, so a Cloud Run deployment works without
    editing the URL field; everywhere else it still defaults to
    localhost, and a saved URL always wins.
  - Fixed after the first local live run (2026-10-27, OpenRouter key in a
    local `.env`), tested with fake providers only:
    - Default model is now `openrouter/free` (server default and
      `.env.example`). It is OpenRouter's router over its free models, not
      a named model; free models are rate-limited and rotate. The old
      default `qwen/qwen3-8b` was deprecated on OpenRouter on 2026-10-09.
      `GREENSEARCH_MODEL` still overrides it.
    - New `GREENSEARCH_MODEL_FALLBACKS` (comma-separated; default
      `liquid/lfm-2.5-2.6b:free,google/gemma-4-31b-it:free,thinkingmachines/inkling-small:free,poolside/laguna-xs-2.1:free`;
      `none` disables). The primary call (and the primary side of the
      optional cascade) tries each fallback in order on HTTP 429, 404, 502
      or 503, and on 402 only when the next model is free. 401 fails at
      once. `meta.model` reports the model that answered; new meta fields
      `requested_model`, `fallback_used` and `provider_model` (the
      response's own `model` field, passed through, not verified live).
      `/api/health` lists `fallbacks`. Escalation failures are unchanged.
    - Logprobs fallback: if the primary call with logprobs fails with an
      error mentioning logprobs, the same model is retried once without
      them; the cascade then does not fire for that request.
    - Fragment fix: `apply_brevity` drops a final sentence cut off at the
      token cap (it showed "Machine learning is a type"), or appends `…`
      when the fragment is the whole answer. New
      `apply_brevity_with_flag` returns `(text, truncated)`; `apply_brevity`
      still returns a string. New `meta.truncated` is true for that case or
      when the provider reports `finish_reason: "length"` (now parsed into
      `Completion.finish_reason`, default None).
    - Error messages: provider 401, 402 and 404 now get their own status
      and message (key rejected; no credits for a paid model; model not
      found or deprecated) instead of the generic 502. 429 and the generic
      502 are unchanged. Logs record the HTTP status and model of every
      failed attempt, never the key, the prompt or the provider's text.
      `ProviderError` now carries `status`.
    - Tests: 140 to 160.
  - Built: `server/app.py`, a stdlib-only backend with `POST /api/chat`
    and `GET /api/health` that also serves the website at `/`. The key,
    models, threshold, CORS origins, rate limit and debug logging are read
    from env only. The pipeline reuses shrink → cache (per session id,
    mode-aware) → router → one call to the single primary model,
    Qwen3-8B (`GREENSEARCH_MODEL`, default `qwen/qwen3-8b`), with logprobs
    requested and no escalation call → `apply_brevity`. The
    `run_cascade` path (same system prompt and cap, escalation only on a
    present, below-threshold mean logprob, an escalation failure returns
    502 with no answer) runs only if `GREENSEARCH_ESCALATION_MODEL` is set
    to a different model. Answers go out as SSE chunk/meta/done events, or JSON
    with `"stream": false`. The provider is called without streaming
    because the brevity trim needs the full answer first; meta
    reports `delivery` and `provider_streaming: false`. Errors are plain
    messages with no provider detail or key. The server has an in-memory
    per-IP rate limit (default 100/h), an exact-origin CORS allowlist (`*`
    refused), prompts kept out of logs by default, and an opt-in
    measured baseline. `providers.py` is unchanged.
  - Built: website Live mode, a toggle that is off on every load, so the
    demo is unchanged when it is off. It adds a LIVE pill, a "not
    browser-only" banner, an editable backend URL stored under
    `greensearch-live-backend-url`, the raw prompt sent with no client
    double-shrink, streamed rendering, and model, escalation, cache-hit,
    token and latency tags. Backend errors show as chat messages. A
    separate LIVE totals row keeps live numbers out of the demo totals.
    A Measure baseline checkbox (off by default, doubles API cost) is
    included. The How-it-works privacy text now distinguishes demo mode
    (no network) from live mode.
  - Built: `tests/test_live_backend.py` (41 tests, fake providers, no key,
    no network beyond loopback), `server/README.md` and `.env.example`
    (placeholders only). Tests: 99 to 140.
  - Changed: model decision. Phase 2 now uses Qwen3-8B only
    (`qwen/qwen3-8b`), the model we already have data on.
    `GREENSEARCH_MODEL_4B` / `GREENSEARCH_MODEL_8B` are replaced by one
    `GREENSEARCH_MODEL`; `GREENSEARCH_ESCALATION_MODEL` is an optional
    override that defaults to the primary model, and while the two are
    equal no escalation call is made (`escalated: false`, reason "single
    model: no separate escalation model configured"). `/api/health` now
    reports `model`, `escalation_model` and `cascade_enabled` instead of
    `model_4b` / `model_8b`; the measured baseline uses the primary model.
    The 4B-first pipeline and the Qwen3-4B vs 8B comparison were dropped
    by the user. `scripts/compare_qwen.py` remains in the tree but is not
    needed for the model decision.
  - Not verified: no real provider call has been made (no key), and the
    Live-mode UI has not been clicked through in a real browser; its
    request and stream code was run under Node against the local server.
  - Still blocked: a provider API key, a provider data-policy check
    (retention and training) before any privacy claim, a hosting account (Cloud Run europe-north1, Vercel or
    Cloudflare, noted as future options only), and a domain.
    `pyproject.toml` version not bumped.
- **0.8.0 (2026-10-27, expanded demo library):** expanded the offline
  demo from 6 to 95 topics: `spec/templates.json` adds 89 canned
  topics (technology, science, math, history, business, health,
  cooking, home maintenance, programming), lazy-loaded by
  `DemoProvider` with the original six keeping priority, and inlined
  into the website demo with Python/JS routing parity. Audit fixes
  applied during the port: `waterycle` renamed to `watercycle`,
  `periodicTable` renamed to `periodictable`, and a `climate action`
  keyword added to the climate-action topic (previously it matched
  only carbon-footprint/sustainability/recycling phrasing). Fallback
  wording changed to "not in the demo library yet" with the accurate
  95-topic count; existing fallback tests updated to match. Medical
  topics (first aid, vaccine, nutrition, sleep, stress, etc.) were
  spot-checked: general information only, with doctor/professional
  and emergency-services prompts where relevant, not specific medical
  advice. Still canned answers, not a model. Tests: 89 to 99.
- **0.7.0 (2026-10-27, audit + cache + tokenizer + cascade):** audit
  fixes (website Google Fonts removed so "nothing leaves the page" is
  true, changelog corpus figure corrected 30% to 32%, README test
  count and PromptShrink table corrected, redundant baseline ternary
  removed); baseline study script (`scripts/baseline_study.py`) and
  BASELINE.md with a canned 12-prompt canopy study (input 32%, total
  11% saved) — the live study is blocked only on an API key and the
  script exits 2 without one rather than fabricating results;
  website session cache ported to the harness (`greensearch/cache.py`,
  persisted in the session file, mode-aware key, `--no-cache` to
  bypass, cache runs bypassed when `--cascade` is on); real tokenizer
  as an optional extra (`--tokenizer tiktoken`, `greensearch[tokenizer]`,
  verified: "Hello world" 2 vs estimate 3, Chinese 5 vs estimate 1);
  confidence-gated cascade (`greensearch/cascade.py`, `--cascade`,
  `--escalation-model`, `--cascade-threshold` default -1.0) that
  escalates only on a real mean logprob below threshold, never
  escalates when logprobs are unavailable, and refuses cascade in
  demo mode. Tests: 75 to 89.
- **0.6.0 (2026-10-26, PromptShrink + corpus):** PromptShrink is now a
  first-class module (`greensearch/shrink.py`) wired into the CLI and session
  report; the footprint now accounts for both input and output tokens; a fixed
  test corpus (`spec/corpus.json`, 12 prompts) verifies shrunk forms and
  measures input saving (32% across the corpus with canned answers: 80 of 247 tokens); site
  PromptShrink spec extended to cover previously-broken cases ("simply",
  "clearly", "in detail", repeated numbers/words). Tests: 71 to 75.
- **0.5.0 (2026-10-26, review pass):** fixed filler stripping that could
  leave half a sentence; sentence splitting now respects abbreviations;
  list items, line breaks, code fences, email subject and sign-off survive
  trimming; live provider sends a brevity system prompt and a token cap
  (generation-side brevity), records provider-reported usage, rejects
  plain-http base URLs (except localhost), and turns null content, read
  timeouts (a separate class before Python 3.10) and invalid UTF-8 into a
  clean `Error:` exit; `--measure-baseline` compares against a real
  unconstrained reply; footprint factors are now explicit and overridable
  (`--wh-per-token`, `--grid-g-per-kwh`) and the baseline is labelled
  assumed or measured; `--mode auto` routes by prompt, with the rules
  shared with the site through `spec/routing_cases.json`; session files
  are written atomically; site: PromptShrink no longer deletes "simply" or
  "clearly" (instructions) or collapses repeated numbers/words. Tests: 37
  to 71.
- **0.4.0 (2026-10-26, standalone program):** packaged as an
  installable program — `pyproject.toml` with a `greensearch`
  console script, `python -m greensearch` entry point, legacy
  `python cli.py` shim, MIT LICENSE; new offline `--demo` mode
  (all six topics plus the generic fallback, with demo totals, no
  key or network required); CLI refactored into the package
  (`greensearch/cli.py`) with shared session/report handling.
- **0.3.0 (2026-10-26, Step 3, harness part):** renamed
  `eco-wrapper` / `ecowrapper` to `greensearch` / `greensearch`
  (folder, package, imports, CLI description, session report header);
  live-provider key is now `GREENSEARCH_API_KEY`, with
  `ECOWRAPPER_API_KEY` as a one-version fallback; cascade scope
  decided and documented — no cascade, confidence gating, caching,
  or per-model energy measurement is implemented, and those are
  listed as future work with prerequisites, not current features.
  Site part of Step 3 (panel routing + changelog claim) is complete:
  site routing is now word-boundary and plural-aware, the v1.2
  changelog claim is corrected, and Grant confirmed on 2026-10-26
  that the concurrent site changes in that build are good to go.
- **0.2.0 (2026-10-24, Step 2):** sessions persist with
  `--session-file` (JSON, `{"version": 1, "calls": [...]}`; missing file
  starts empty, corrupt file exits 2 with a readable error instead of
  discarding data); live-provider failures become a single
  `ProviderError` — HTTP status + provider message, connection failure,
  timeout, or malformed response — and the CLI exits 2 with an
  `Error:` line, never a traceback; report/session write failures also
  exit 2 cleanly. Test suite grows to 29 tests.
- **0.1.1 (2026-10-24, Step 1):** empty-prompt crash fixed (CLI now
  exits 2 with a readable error, no traceback); routing switched from
  substring to word-boundary matching; workout, firewall, and DNS
  templates added to the demo provider, matching the site's content;
  generic fallback made honest instead of echoing the prompt as an
  answer; test suite added (18 tests).
- **0.1.0:** initial harness — brevity layer, token/footprint
  estimation, demo + live providers, session reports.

Name history: Renamed from the EcoGPT concept to GreenSearch on 2026-10-24 > following business research (the real EcoGPT at ecogpt.com publishes no > per-query energy figure — measured, open methodology is this project's > opening). The harness was renamed in Step 3 (2026-10-26) from > `eco-wrapper` / `ecowrapper` to `greensearch` / `greensearch`. The old > folder is gone; a moved-note README is left at the old location only > if one was created during the move. The live-provider key is now > `GREENSEARCH_API_KEY`; `ECOWRAPPER_API_KEY` still works as a > one-version fallback.
