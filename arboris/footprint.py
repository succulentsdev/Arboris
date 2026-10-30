"""CO2e estimation from token counts, weighted toward output tokens.

Every figure is an ARITHMETIC ESTIMATE, not a measurement:

    Wh         = output_tokens x wh_per_token
                 + input_tokens x wh_per_token / INPUT_OUTPUT_ENERGY_RATIO
    grams CO2e = Wh / 1000 x grid_g_per_kwh

Generating a token costs far more than reading one, so output tokens
(which include any hidden reasoning tokens the provider reports) carry the
factor and input tokens carry 1/800 of it. Both factors and the grid
intensity are assumptions you can override; the defaults are derived from
published figures (sources below), not measured for the model Arboris
calls. Treat output as a way to compare answer lengths, never as carbon
accounting.

The "standard" (verbose) baseline is either:
  - assumed: concise tokens x a per-mode multiplier (3.4 / 2.6 / 1.9). With
    this baseline the saved percentage depends only on the mode, not on the
    content, so it demonstrates the arithmetic and proves nothing; or
  - measured: pass baseline_tokens from a real unconstrained completion.
"""

import re

# Default energy per GENERATED token: 0.0006 Wh. Derived from Epoch AI,
# "How much energy does ChatGPT use?" (Josh You, Feb 2025): about 0.3 Wh
# for a GPT-4o query with 500 output tokens, so 0.3 / 500 = 0.0006. It is
# the low end of the published range below. Until 2026-10-29 the default
# was 0.0045 Wh for every token, input and output alike, which priced a
# 500-token answer near the 2023 estimates (about 3 Wh per query) rather
# than the 2025 ones (0.24 to 0.34 Wh).
WH_PER_TOKEN = 0.0006
GRID_G_PER_KWH = 400.0
CO2_PER_TOKEN_G = round(WH_PER_TOKEN / 1000 * GRID_G_PER_KWH, 10)  # 0.00024

# One input token costs a small fraction of one output token. Ruf and
# Detyniecki, "The Cost of Context" (HotCarbon '26), measured an output
# token at about 850x the energy of an input token at 1k context on vLLM,
# narrowing to about 750x at 112k context (7-8B models, one A100). 800 is
# the midpoint, applied as a constant; for very long inputs on less
# optimised stacks the true input share is higher.
INPUT_OUTPUT_ENERGY_RATIO = 800

# Published anchors for energy per generated token, shown as a range.
# Neither end is a measurement of the model Arboris calls; both are
# DERIVED here by dividing a published per-query figure by its stated
# number of generated tokens:
#   - Low end, about 0.0006 Wh/token: Epoch AI, "How much energy does
#     ChatGPT use?" (Josh You, Feb 2025) estimates about 0.3 Wh for a
#     GPT-4o query with 500 output tokens (about 100B active parameters
#     assumed, H100 hardware). 0.3 / 500 = 0.0006.
#   - High end, about 0.005 Wh/token: Luccioni, Jernite and Strubell,
#     "Power Hungry Processing" (ACM FAccT 2024) report text generation
#     at about 0.05 kWh per 1,000 inferences of 10 new tokens, on models
#     up to 11B parameters in a research setup. 0.05 Wh / 10 = 0.005.
# The default WH_PER_TOKEN (0.0006) is the low end. Google's
# Aug 2025 figure (0.24 Wh per median Gemini text prompt) is not used as
# an anchor because it states no token count. The range is the same for
# every model: no source gives per-size figures Arboris could cite, and
# OpenRouter does not disclose hardware, batching or region.
WH_PER_TOKEN_RANGE = (0.0006, 0.005)
WH_PER_TOKEN_RANGE_LABEL = (
    "derived from published anchors: Epoch AI 2025 (GPT-4o-class) to "
    "Luccioni et al. 2024 (models up to 11B, research setup)"
)

# Cooling water use effectiveness, litres per kWh, for modern closed-loop
# systems. Shown as a range, never a single number: older evaporative
# systems are 1.7-2.5 L/kWh, and electricity-generation water (about
# 4.52 L/kWh in most models) is excluded here, so the water figures are
# cooling water only, not total water.
WUE_L_PER_KWH_LOW = 0.1
WUE_L_PER_KWH_HIGH = 0.3


_ACTIVE_PARAMS_RE = re.compile(r"(?<![a-z0-9.])a(\d+(?:\.\d+)?)b(?![a-z0-9])")
_EXPERTS_RE = re.compile(r"(?<![a-z0-9.])(\d+)x(\d+(?:\.\d+)?)b(?![a-z0-9])")
_PARAMS_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b(?![a-z0-9])")


def model_size_tier(model_name):
    """Size tier read from a parameter count in the model id, else "unknown".

    Returns "small" (under 10B), "medium" (10B to 70B), "large" (over
    70B) or "unknown". Energy scales with ACTIVE parameters, so a
    mixture-of-experts id that states them ("qwen3-30b-a3b") is sized by
    the active count. "8x7b" style ids state experts x size, not the
    active count, so they are sized by the total as an upper bound. Ids
    with no parameter count (most hosted models) are "unknown": no tier
    is guessed from a brand or product name. The tier is information
    only; it does not change the energy factor.
    """
    if not model_name:
        return "unknown"
    name = str(model_name).lower()
    match = _ACTIVE_PARAMS_RE.search(name)
    if match:
        params_b = float(match.group(1))
    else:
        match = _EXPERTS_RE.search(name)
        if match:
            params_b = int(match.group(1)) * float(match.group(2))
        else:
            match = _PARAMS_RE.search(name)
            if not match:
                return "unknown"
            params_b = float(match.group(1))
    if params_b < 10:
        return "small"
    if params_b <= 70:
        return "medium"
    return "large"


def energy_receipt(input_tokens, output_tokens, *, model=None,
                   model_tier=None, escalated=False, cache_hit=False,
                   reasoning_tokens=None,
                   wh_per_token=WH_PER_TOKEN, grid_g_per_kwh=GRID_G_PER_KWH,
                   wue_low=WUE_L_PER_KWH_LOW, wue_high=WUE_L_PER_KWH_HIGH,
                   input_output_ratio=INPUT_OUTPUT_ENERGY_RATIO):
    """Per-answer energy receipt: an arithmetic estimate, never accounting.

    Energy is output-weighted: output tokens x wh_per_token, plus input
    tokens x wh_per_token / input_output_ratio. Output tokens are the
    provider's completion count, which includes reasoning tokens.

    Returns a dict the server puts in meta["energy"] and the site can
    render as a receipt. When the provider did not report token usage,
    status is "unavailable" and no number is invented: energy, CO2e and
    water are all None, with the reason stated. A cache hit is 0 tokens,
    so it is 0 Wh / 0 g / 0 mL, with a note saying why.

    Water is cooling water only: energy_wh * WUE, in millilitres
    (Wh / 1000 = kWh, x L/kWh = litres, x 1000 = mL, so mL = Wh x WUE).
    Electricity-generation water is excluded and the note says so.
    Grid intensity is the configured factor, not a measured location:
    OpenRouter does not disclose where inference ran, so no region
    (Finland or otherwise) is claimed for the receipt.
    """
    if input_output_ratio <= 0:
        raise ValueError("input_output_ratio must be positive")
    size_tier = model_size_tier(model)
    base = {
        "basis": "estimate",
        "model": model,
        "model_tier": model_tier,
        "model_size_tier": size_tier,
        "escalated": escalated,
        "cache_hit": cache_hit,
        "factors": {
            "weighting": "output",
            "wh_per_token": wh_per_token,
            "wh_per_input_token": wh_per_token / input_output_ratio,
            "input_output_energy_ratio": input_output_ratio,
            "wh_per_token_range": list(WH_PER_TOKEN_RANGE),
            "wh_per_token_range_label": WH_PER_TOKEN_RANGE_LABEL,
            "grid_g_per_kwh": grid_g_per_kwh,
            "grid_region": "unknown",
            "wue_l_per_kwh_low": wue_low,
            "wue_l_per_kwh_high": wue_high,
        },
    }
    if input_tokens is None or output_tokens is None:
        return {
            **base,
            "status": "unavailable",
            "reason": ("Provider did not report token usage, so no energy "
                       "estimate was made."),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": None,
            "energy_wh": None,
            "co2e_g": None,
            "water_ml_low": None,
            "water_ml_high": None,
            "note": None,
        }
    if (wh_per_token <= 0 or grid_g_per_kwh <= 0 or wue_low <= 0
            or wue_high <= 0 or input_output_ratio <= 0):
        raise ValueError("energy factors must be positive")
    total = int(input_tokens) + int(output_tokens)
    wh = (int(output_tokens) * wh_per_token
          + int(input_tokens) * wh_per_token / input_output_ratio)
    note = ("Arithmetic estimate from provider-reported tokens, not a "
            "measurement and not carbon accounting. Output tokens carry "
            "the energy factor; input tokens count at 1/"
            f"{input_output_ratio:g} of it. Inference region "
            "unknown (provider does not disclose it), so the grid "
            "factor is illustrative. Water is cooling water only; "
            "electricity-generation water is excluded.")
    if reasoning_tokens is not None and reasoning_tokens > 0:
        note = (f"Of {int(output_tokens)} output tokens, "
                f"{reasoning_tokens} were reasoning tokens "
                f"(included in output, not additive). " + note)
    if cache_hit:
        note = ("Cache hit: no new model call, so this answer cost no new "
                "tokens or energy. " + note)
    if escalated:
        note = ("Total covers the primary call and the escalation call. " + note)
    return {
        **base,
        "status": "estimate",
        "reason": None,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total,
        "energy_wh": round(wh, 6),
        "co2e_g": round(wh / 1000 * grid_g_per_kwh, 6),
        "water_ml_low": round(wh * wue_low, 6),
        "water_ml_high": round(wh * wue_high, 6),
        "note": note,
    }


VERBOSE_MULTIPLIER = {"sprout": 3.4, "canopy": 2.6, "full": 1.9}


def footprint(out_tokens, mode, *, baseline_tokens=None,
              wh_per_token=WH_PER_TOKEN, grid_g_per_kwh=GRID_G_PER_KWH):
    """Compare a concise reply against a verbose baseline.

    Both sides are output tokens, so the full per-output-token factor
    applies to each.
    """
    if mode not in VERBOSE_MULTIPLIER:
        raise ValueError(
            f"unknown mode {mode!r}; expected one of {sorted(VERBOSE_MULTIPLIER)}"
        )
    if wh_per_token <= 0 or grid_g_per_kwh <= 0:
        raise ValueError("wh_per_token and grid_g_per_kwh must be positive")
    if baseline_tokens is None:
        std_tokens = round(out_tokens * VERBOSE_MULTIPLIER[mode])
        baseline_kind = "assumed"
    else:
        std_tokens = max(0, int(baseline_tokens))
        baseline_kind = "measured"
    g_per_token = wh_per_token / 1000 * grid_g_per_kwh
    eco_co2 = out_tokens * g_per_token
    std_co2 = std_tokens * g_per_token
    saved_pct = max(0, round((1 - out_tokens / std_tokens) * 100)) if std_tokens else 0
    return {
        "eco_tokens": out_tokens,
        "standard_tokens": std_tokens,
        "eco_co2_g": round(eco_co2, 6),
        "standard_co2_g": round(std_co2, 6),
        "saved_pct": saved_pct,
        "saved_co2_g": round(max(0.0, std_co2 - eco_co2), 6),
        "baseline_kind": baseline_kind,
        "wh_per_token": wh_per_token,
        "grid_g_per_kwh": grid_g_per_kwh,
    }
