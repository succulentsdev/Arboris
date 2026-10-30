"""Arboris CLI: concise answers with a footprint comparison.

Installed entry points (see pyproject.toml):
  arboris "explain DNS simply" --mode sprout
  python -m arboris "explain DNS simply" --mode sprout
  python cli.py "explain DNS simply" --mode sprout   (legacy, same code)

Demo mode (offline, no key, no network):
  arboris --demo
  arboris --demo --mode canopy --report-out demo-report.md

Without --provider live, runs the offline demo provider: no key, no
network, no model. Live mode needs ARBORIS_API_KEY in the
environment. With --session-file, calls persist to a JSON file across
runs and the printed session totals accumulate; without it, each run
is a single-call session. All user-facing failures exit 2 with a
readable "Error:" line, never a traceback.
"""

import argparse
import sys

from arboris.brevity import apply_brevity
from arboris.cache import cache_key
from arboris.shrink import shrink
from arboris.tokenizer import TokenizerError, count_tokens
from arboris.tokens import est_tokens
from arboris.footprint import GRID_G_PER_KWH, WH_PER_TOKEN, footprint
from arboris.router import route_mode
from arboris.providers import (
    BREVITY_SYSTEM,
    MAX_TOKENS,
    TOKEN_BUDGET,
    DemoProvider,
    OpenAICompatibleProvider,
    ProviderError,
)
from arboris.session import Session

# The demo runs the same six topics the site demo covers, plus one
# uncovered topic to show the honest generic fallback. It always uses
# the offline demo provider, so it works with no key and no network.
DEMO_PROMPTS = [
    ("email", "Write a polite follow-up email to a client who missed our call"),
    ("API", "Explain APIs to a 10-year-old"),
    ("workout", "Give me a 3-day workout plan for beginners at home"),
    ("backup", "Summarize why backups matter for a small office"),
    ("firewall", "Explain what a portcullis firewall does and why a small business should care, in plain English"),
    ("DNS", "Explain DNS simply"),
    ("generic", "Explain quantum computing simply"),
]


def _load_session(session_file):
    """Return (session, error_message_or_None) for a session file."""
    if not session_file:
        return Session(), None
    try:
        return Session.load(session_file), None
    except ValueError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, f"Could not read session file {session_file}: {exc}"


def _save_session(session, session_file):
    """Return an error message or None after persisting a session."""
    if not session_file:
        return None
    try:
        session.save(session_file)
    except OSError as exc:
        return f"Could not write session file {session_file}: {exc}"
    return None


def _write_report(session, report_out):
    """Return an error message or None after writing a report."""
    if not report_out:
        return None
    try:
        with open(report_out, "w") as handle:
            handle.write(session.markdown_report())
    except OSError as exc:
        return f"Could not write report {report_out}: {exc}"
    return None


def _run_one(provider, session, prompt, mode_arg, opts=None):
    """Run one prompt through a provider, record it, and print it.

    Returns an error message or None.

    Two-sided accounting:
      Input side:  PromptShrink trims the prompt; saved_input_tokens is the
                   difference, credited before any model call.
      Output side: the model receives the shrunk prompt. If live and
                   constrained (default), a system-prompt + max_tokens cap
                   limits generation. The footprint uses provider-reported
                   token counts when available.
    """
    opts = opts or {}

    def _count(text):
        return count_tokens(text, backend=opts.get("tokenizer", "estimate"))

    # --- Input side: shrink the prompt ---
    try:
        sr = shrink(prompt, count_fn=_count)
    except TokenizerError as exc:
        return str(exc)
    shrunk_prompt = sr.text
    mode = route_mode(shrunk_prompt) if mode_arg == "auto" else mode_arg
    mode_label = f"{mode} (auto)" if mode_arg == "auto" else mode
    live = getattr(provider, "live", False)
    constrain = live and not opts.get("no_constrain")

    # --- Session cache (ported from the website demo) ---
    # Cascade runs bypass the cache: a cached answer would hide whether
    # escalation happened, and which model produced the answer.
    effective_no_cache = opts.get("no_cache") or opts.get("cascade")
    key = cache_key(shrunk_prompt, mode)
    if not effective_no_cache:
        entry = session.cache.lookup(key)
        if entry is not None:
            answer = entry["answer"]
            fp = footprint(
                0, mode,
                baseline_tokens=entry["standard_tokens"],
                wh_per_token=opts.get("wh_per_token", WH_PER_TOKEN),
                grid_g_per_kwh=opts.get("grid_g_per_kwh", GRID_G_PER_KWH),
            )
            fp["baseline_kind"] = "cache-hit"
            fp["cache_hit"] = True
            fp["shrink_saved_tokens"] = sr.saved_tokens
            fp["shrink_original_tokens"] = sr.original_tokens
            fp["shrink_changed"] = sr.changed
            session.record(prompt, answer, mode, fp)
            print(f"[{provider.name} | mode: {mode_label}]\n")
            print("Cache hit — 0 new output tokens, reused answer.\n")
            print(answer)
            if sr.changed:
                print(
                    f"\nPromptShrink: {sr.original_tokens} → {sr.shrunk_tokens} input tokens "
                    f"(saved {sr.saved_tokens})"
                )
            else:
                print(f"\nPromptShrink: no change ({sr.original_tokens} input tokens)")
            print(
                f"Output: 0 vs {fp['standard_tokens']} standard (cache-hit baseline) | "
                f"CO2e: 0g vs {fp['standard_co2_g']}g (est.) | "
                f"Saved: 100% / {fp['saved_co2_g']}g (est.)"
            )
            return None

    # --- Output side: call the model ---
    # Build the system prompt: brevity instruction + optional token budget.
    system = BREVITY_SYSTEM[mode] if constrain else None
    if system and opts.get("token_budget"):
        budget = TOKEN_BUDGET.get(mode)
        if budget is not None:
            system = system + f" Use under {budget} tokens."
    cascade_result = None
    try:
        if opts.get("cascade"):
            if not live:
                return "Cascade requires a live provider with logprobs; the demo provider has no confidence signal."
            from arboris.cascade import run_cascade
            escalation = OpenAICompatibleProvider(
                provider.base_url, api_key=provider.api_key,
                model=opts.get("escalation_model", "gpt-4o"),
            )
            cascade_result = run_cascade(
                provider, escalation, shrunk_prompt,
                system=system,
                max_tokens=MAX_TOKENS[mode] if constrain else None,
                threshold=opts.get("cascade_threshold", -1.0),
            )
            comp = cascade_result.completion
        else:
            comp = provider.complete_ex(
                shrunk_prompt,
                system=system,
                max_tokens=MAX_TOKENS[mode] if constrain else None,
            )
        base = None
        if opts.get("measure_baseline"):
            base = provider.complete_ex(prompt)
    except (ValueError, ProviderError) as exc:
        return str(exc)

    answer = apply_brevity(comp.text, mode)

    # Token accounting: use provider counts when available; fall back to
    # the selected tokenizer (estimate by default, real if requested).
    # Never mix counted and estimated in the same comparison.
    out_tokens = _count(answer)
    baseline_tokens = None
    if comp.completion_tokens is not None:
        out_tokens = comp.completion_tokens
    if base is not None:
        if base.completion_tokens is not None and comp.completion_tokens is not None:
            baseline_tokens = base.completion_tokens
        else:
            out_tokens = _count(answer)
            baseline_tokens = _count(base.text)

    fp = footprint(
        out_tokens, mode,
        baseline_tokens=baseline_tokens,
        wh_per_token=opts.get("wh_per_token", WH_PER_TOKEN),
        grid_g_per_kwh=opts.get("grid_g_per_kwh", GRID_G_PER_KWH),
    )
    # Attach input-side saving to the footprint dict so it travels to the report.
    fp["shrink_saved_tokens"] = sr.saved_tokens
    fp["shrink_original_tokens"] = sr.original_tokens
    fp["shrink_changed"] = sr.changed

    if not effective_no_cache:
        session.cache.store(key, mode, answer, fp["standard_tokens"])
    session.record(prompt, answer, mode, fp)
    print(f"[{provider.name} | mode: {mode_label}]\n")
    if cascade_result is not None:
        print(f"Cascade: {cascade_result.reason} "
              f"(primary {cascade_result.primary_model}, "
              f"escalation {cascade_result.escalation_model})\n")
    print(answer)

    # Print a clear two-line breakdown: input side then output side.
    if sr.changed:
        print(
            f"\nPromptShrink: {sr.original_tokens} → {sr.shrunk_tokens} input tokens "
            f"(saved {sr.saved_tokens})"
        )
    else:
        print(f"\nPromptShrink: no change ({sr.original_tokens} input tokens)")
    print(
        f"Output: {fp['eco_tokens']} vs {fp['standard_tokens']} standard "
        f"({fp['baseline_kind']} baseline) | "
        f"CO2e: {fp['eco_co2_g']}g vs {fp['standard_co2_g']}g (est.) | "
        f"Saved: {fp['saved_pct']}% / {fp['saved_co2_g']}g (est.)"
    )
    if fp["baseline_kind"] == "assumed":
        print("  (assumed baseline: saved % depends only on mode, not content;",
              "use --measure-baseline for a real comparison)")
    return None


def _run_demo(session, mode, opts=None):
    """Run the offline demo: all six topics plus a generic fallback."""
    provider = DemoProvider()
    print("[demo (offline) | Arboris demo]\n")
    print(
        "Six templated topics, plus one uncovered topic to show the "
        "honest fallback. No key, no network, no model.\n"
    )
    for topic, prompt in DEMO_PROMPTS:
        print(f"--- {topic}: {prompt}")
        error = _run_one(provider, session, prompt, mode, opts)
        if error:
            return error
        print()
    totals = session.totals
    print(
        f"Demo totals: {totals['calls']} calls | Eco tokens: "
        f"{totals['eco_tokens']} | Standard tokens (est.): "
        f"{totals['standard_tokens']} | CO2e saved (est.): "
        f"{totals['saved_co2_g']} g"
    )
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Arboris: concise answers, estimated footprint.")
    parser.add_argument("prompt", nargs="?", default=None,
                        help="Prompt to answer (omit with --demo)")
    parser.add_argument("--mode", choices=["sprout", "canopy", "full", "auto"], default="full",
                        help="Answer length; auto picks one from the prompt")
    parser.add_argument("--provider", choices=["demo", "live"], default="demo")
    parser.add_argument("--base-url", default="https://api.openai.com/v1")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--report-out", default=None,
                        help="Write a markdown session report to this path")
    parser.add_argument("--session-file", default=None,
                        help="JSON file to persist session calls across runs")
    parser.add_argument("--measure-baseline", action="store_true",
                        help="Compare against a real unconstrained reply (live: one extra API call)")
    parser.add_argument("--no-constrain", action="store_true",
                        help="Live: do not ask for a short answer; generate in full, then trim")
    parser.add_argument("--token-budget", action="store_true",
                        help="Add 'Use under N tokens.' to the system prompt (sprout 100, canopy 200, full 350)")
    parser.add_argument("--wh-per-token", type=float, default=WH_PER_TOKEN,
                        help="Assumed energy per generated token, in Wh (default %(default)s)")
    parser.add_argument("--grid-g-per-kwh", type=float, default=GRID_G_PER_KWH,
                        help="Assumed grid intensity, g CO2e per kWh (default %(default)s)")
    parser.add_argument("--cascade", action="store_true",
                        help="Live only: escalate to --escalation-model when the primary model's mean logprob is below --cascade-threshold")
    parser.add_argument("--escalation-model", default="gpt-4o",
                        help="Model to escalate to with --cascade (default %(default)s)")
    parser.add_argument("--cascade-threshold", type=float, default=-1.0,
                        help="Mean logprob below which --cascade escalates (default %(default)s; must be <= 0)")
    parser.add_argument("--tokenizer", choices=["estimate", "tiktoken"], default="estimate",
                        help="Token counter: word estimate (default) or real tiktoken count (requires pip install 'arboris[tokenizer]')")
    parser.add_argument("--no-cache", action="store_true",
                        help="Bypass the session cache (cached answers are reused verbatim and may be stale)")
    parser.add_argument("--demo", action="store_true",
                        help="Run the offline demo (all topics, no key needed)")
    args = parser.parse_args(argv)
    if args.wh_per_token <= 0 or args.grid_g_per_kwh <= 0:
        print("Error: --wh-per-token and --grid-g-per-kwh must be positive", file=sys.stderr)
        return 2
    if args.cascade_threshold > 0:
        print("Error: --cascade-threshold must be <= 0 (logprobs are negative or zero)", file=sys.stderr)
        return 2
    opts = {
        "measure_baseline": args.measure_baseline,
        "no_constrain": args.no_constrain,
        "token_budget": args.token_budget,
        "wh_per_token": args.wh_per_token,
        "grid_g_per_kwh": args.grid_g_per_kwh,
        "no_cache": args.no_cache,
        "tokenizer": args.tokenizer,
        "cascade": args.cascade,
        "escalation_model": args.escalation_model,
        "cascade_threshold": args.cascade_threshold,
    }

    if args.demo:
        session, error = _load_session(args.session_file)
        if error:
            print(f"Error: {error}", file=sys.stderr)
            return 2
        if args.prompt:
            print("Note: --demo ignores the prompt you gave.", file=sys.stderr)
        error = _run_demo(session, args.mode, opts)
        if error:
            print(f"Error: {error}", file=sys.stderr)
            return 2
        error = _save_session(session, args.session_file)
        if error:
            print(f"Error: {error}", file=sys.stderr)
            return 2
        print("\nNote: CO2e figures are estimates from token counts, not measurements.")
        error = _write_report(session, args.report_out)
        if error:
            print(f"Error: {error}", file=sys.stderr)
            return 2
        if args.report_out:
            print(f"Report written to {args.report_out}")
        return 0

    if not args.prompt:
        print("Error: a prompt is required unless --demo is given", file=sys.stderr)
        return 2

    if args.provider == "demo":
        provider = DemoProvider()
    else:
        try:
            provider = OpenAICompatibleProvider(args.base_url, model=args.model)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    session, error = _load_session(args.session_file)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    error = _run_one(provider, session, args.prompt, args.mode, opts)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    error = _save_session(session, args.session_file)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    print("\nNote: CO2e figures are estimates from token counts, not measurements.")
    if args.session_file:
        totals = session.totals
        print(
            f"Session totals: {totals['calls']} calls | Eco tokens: "
            f"{totals['eco_tokens']} | CO2e saved (est.): {totals['saved_co2_g']} g"
        )
    error = _write_report(session, args.report_out)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    if args.report_out:
        print(f"Report written to {args.report_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
