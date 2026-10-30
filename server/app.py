#!/usr/bin/env python3
"""Arboris live backend: POST /api/chat and GET /api/health.

Standard library only. All secrets and model choices come from the
environment; nothing secret is ever accepted from, or returned to, the
client. See server/README.md for the full env var table and run steps.

Pipeline per chat request, in order:
  1. PromptShrink the prompt (arboris.shrink).
  2. Session cache check (arboris.cache), per client session id,
     mode-aware. A hit makes no model call: 0 new output tokens.
  3. Auto routing (arboris.router), unless the client sent a mode.
  4. One call to the primary model (ARBORIS_MODEL, default
     google/gemma-4-31b-it:free, a named general chat model) with the
     brevity system prompt and token cap, logprobs
     requested. By default that is the only model call: escalated=false,
     reason "single model: no separate escalation model configured".
     If that call fails with HTTP 429/404/502/503 (or 402 when the next
     model is free), each ARBORIS_MODEL_FALLBACKS model is tried in
     order until one answers; 401 fails at once. If a provider rejects
     logprobs, the same model is retried once without them first.
     Optional cascade: only if ARBORIS_ESCALATION_MODEL is set to a
     DIFFERENT model does the logprob-gated cascade run
     (arboris.cascade): if avg_logprob is present and below the
     threshold, the escalation model is called with the SAME system
     prompt and cap. Missing logprobs never escalate. An escalation
     failure is an error to the client, never a silent fallback to the
     primary answer.
  5. apply_brevity as a safety-net trim (arboris.brevity). A
     cut-off final sentence is dropped (or marked with an ellipsis), and
     meta "truncated" says so.

Delivery: the provider is called WITHOUT streaming, because step 5
needs the complete text to trim (and, with the optional cascade, step 4
needs the complete answer and its logprobs before it can decide whether
to escalate; streaming raw provider tokens would show the user a
low-confidence answer that might then be replaced). The finished answer is then sent to the browser as
Server-Sent Events (chunk events, then one meta event, then done), or
as one JSON object when the client sends "stream": false. Every meta
payload says which path was used ("delivery") and that provider-side
streaming is off ("provider_streaming": false).
"""

import argparse
import collections
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arboris.brevity import apply_brevity_with_flag  # noqa: E402
from arboris.cache import SessionCache, cache_key  # noqa: E402
from arboris.cascade import CascadeResult, run_cascade  # noqa: E402
from arboris.providers import (  # noqa: E402
    BREVITY_SYSTEM,
    MAX_TOKENS,
    OpenAICompatibleProvider,
    ProviderError,
)
from arboris.router import route_mode  # noqa: E402
from arboris.task_router import QUALITY_THRESHOLDS, TASK_TEMPERATURES, difficulty_score, route_task  # noqa: E402
from arboris.long_input import prune_long_prompt  # noqa: E402
from arboris.local_calc import try_local_calc  # noqa: E402
from arboris.scorer import score_answer  # noqa: E402
from arboris.footprint import energy_receipt  # noqa: E402
from arboris.shrink import shrink, shrink_v2  # noqa: E402

# ---------------------------------------------------------------------------
# Plain-language client messages. These are the ONLY error texts a client
# ever sees: no provider detail, no URL, no key, no traceback.
# ---------------------------------------------------------------------------

MSG_NOT_CONFIGURED = "Live backend is not configured yet (no API key on the server)"
MSG_MISCONFIGURED = "Live backend is misconfigured on the server, check its settings"
MSG_UNREACHABLE = "The model provider could not be reached, try again"
MSG_RATE_LIMITED = "Too many requests, try again later"
MSG_KEY_REJECTED = "The model provider rejected the API key, check the key on the server"
MSG_NO_CREDITS = (
    "The model provider reports no credits for this paid model: add credits "
    "or switch ARBORIS_MODEL to a free model"
)
MSG_MODEL_NOT_FOUND = (
    "The model was not found or is deprecated at the provider: check the "
    "ARBORIS_MODEL id"
)
MSG_ESCALATION_FAILED = (
    "The escalation model could not be reached, so no answer was returned "
    "(the primary model's low-confidence answer was not used), try again"
)
MSG_EMPTY = "The model returned no answer, try again"
MSG_ORIGIN = "This page's origin is not allowed to use this backend"
MSG_INTERNAL = "Something went wrong on the server, try again"
MSG_NOT_FOUND = "Not found"

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# A named general chat model. openrouter/free (OpenRouter's router over its
# free models) is NOT the primary: it can route to non-chat models, e.g. a
# content-safety classifier answered a hamburger question with "User
# Safety: safe", and fallback only triggers on errors, not wrong-type
# answers. Free models are rate-limited and rotate, hence the chain below,
# with the router kept only as the last resort.
DEFAULT_MODEL = "google/gemma-4-31b-it:free"
DEFAULT_FALLBACKS = (
    "liquid/lfm-2.5-2.6b:free",
    "thinkingmachines/inkling-small:free",
    "poolside/laguna-xs-2.1:free",
    "openrouter/free",
)
# Provider HTTP codes that move on to the next model. 402 is handled
# separately (only towards a free model); 401 never falls back.
FALLBACK_STATUSES = (429, 404, 502, 503)
SINGLE_MODEL_REASON = "single model: no separate escalation model configured"
DEFAULT_ORIGINS = ("http://localhost:8000", "http://127.0.0.1:8000")
DEFAULT_PORT = 8000

MAX_BODY_BYTES = 16 * 1024
MAX_PROMPT_CHARS = 4000
MAX_SESSIONS = 1000
MAX_CACHE_ENTRIES_PER_SESSION = 200
CHUNK_WORDS = 3

MODES = ("sprout", "canopy", "full")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Fields a client must never send. The key lives on the server only, and
# model choice is server configuration.
_FORBIDDEN_FIELDS = {
    "api_key", "apikey", "key", "token", "authorization", "secret",
    "openai_api_key", "arboris_api_key", "greensearch_api_key",
    "model", "base_url",
}

# (name, default shown in --help and README, description)
ENV_VARS = [
    ("ARBORIS_API_KEY", "(unset)",
     "Provider API key. Server-side only. Unset = /api/chat returns 503."),
    ("ARBORIS_BASE_URL", DEFAULT_BASE_URL,
     "OpenAI-compatible base URL (https; http only for localhost)."),
    ("ARBORIS_MODEL", DEFAULT_MODEL, "Primary model; answers every request."),
    ("ARBORIS_MODEL_FALLBACKS", ",".join(DEFAULT_FALLBACKS),
     "Comma-separated models tried in order when the primary call fails "
     "(429/404/502/503, or 402 towards a free model). 'none' disables."),
    ("ARBORIS_ESCALATION_MODEL", "(= ARBORIS_MODEL: no escalation)",
     "Optional. Set to a DIFFERENT model to enable the logprob cascade."),
    ("ARBORIS_CASCADE_THRESHOLD", "-1.0",
     "Mean-logprob escalation threshold, must be <= 0. Cascade only."),
    ("ARBORIS_BACKEND_ORIGIN", ",".join(DEFAULT_ORIGINS),
     "CORS allowlist of exact origins, separated by commas and/or spaces; "
     "trailing slashes are stripped. '*' is refused."),
    ("ARBORIS_RATE_LIMIT_PER_HOUR", "30",
     "Chat requests per client IP per rolling hour. 0 disables the limit."),
    ("ARBORIS_RATE_LIMIT_PER_DAY", "100",
     "Chat requests per client IP per rolling day (86400s). 0 disables."),
    ("ARBORIS_GLOBAL_LIMIT_PER_DAY", "800",
     "Chat requests in total per rolling day, all IPs combined. 0 disables. "
     "POC guard: keeps one abusive IP, or many IPs, from burning the "
     "OpenRouter free quota (1000/day after a $10 credit) or paid credits."),
    ("ARBORIS_ALLOW_PAID_MODELS", "off",
     "1/true/yes/on allows a non-free primary, fallback, or escalation model. "
     "Off by default: a paid model must be enabled deliberately, so a config "
     "mistake cannot drain credits."),
    ("ARBORIS_SMALL_MODEL", "liquid/lfm-2.5-2.6b:free",
     "Small model tried first for sprout prompts when small-model routing "
     "is on. 2.6B vs the 31B primary: the efficiency lever, not a quality "
     "claim. Must be free unless ARBORIS_ALLOW_PAID_MODELS is on."),
    ("ARBORIS_SMALL_MODEL_ROUTING", "off",
     "1/true/yes/on routes sprout prompts (short, simple, per route_mode) "
     "to ARBORIS_SMALL_MODEL first, with the primary chain as fallback. "
     "Off by default: enable live, judge small-model answers, then decide."),
    ("ARBORIS_SHRINK_VERSION", "v1",
     "PromptShrink version for live requests: v1 (default) or v2 "
     "(redundancy + gated telegraphic, 39% vs 32% saved on the corpus). "
     "v2 is opt-in only: flip it live, judge v1-vs-v2 answers, then decide. "
     "Any other value refuses to start."),
    ("ARBORIS_GRID_G_PER_KWH", "400",
     "Grid carbon intensity for the per-answer energy receipt, in "
     "gCO2e/kWh. Default 400 is illustrative, not measured: OpenRouter "
     "does not disclose where inference ran, so no region is claimed. "
     "A non-positive value refuses to start."),
    ("ARBORIS_TASK_ROUTING", "off",
     "1/true/yes/on routes by task type (fact, definition, steps, "
     "reasoning, math, code, creative, other) instead of word count, "
     "when the client did not choose a mode explicitly. Unknown "
     "prompts fall back to word-count routing. Off by default: "
     "enable live, judge routed answers, then decide. Meta reports "
     "task_type, task_tier, and routed_by on every answer."),
    ("ARBORIS_DIFFICULTY_ROUTING", "off",
     "1/true/yes/on lets a 0-1 difficulty score override the task "
     "tier when task routing is on: score below the threshold goes "
     "small, at or above goes primary. The score is a transparent "
     "feature formula, not a learned router, and at the default "
     "0.5 threshold it reproduces the task tiers exactly, so "
     "turning it on alone changes nothing. Moving the threshold is "
     "what moves traffic. Meta reports difficulty_score."),
    ("ARBORIS_ROUTER_THRESHOLD", "0.5",
     "Difficulty-score threshold for small vs primary, 0-1. "
     "Overridden by ARBORIS_QUALITY_TARGET=eco (0.7) or "
     "=quality (0.3) when that target is set."),
    ("ARBORIS_QUALITY_TARGET", "balanced",
     "Routing preset: eco (threshold 0.7, more small-model answers), "
     "balanced (uses ARBORIS_ROUTER_THRESHOLD), quality (0.3, "
     "more primary answers). Only affects routing when difficulty "
     "routing is on. Any other value refuses to start."),
    ("ARBORIS_SCORER_ESCALATION", "off",
     "1/true/yes/on lets the transparent answer scorer trigger "
     "escalation when its score is below ARBORIS_SCORE_THRESHOLD. "
     "Off by default: the score is still reported in meta, but only "
     "the logprob cascade can escalate. Unlike logprobs, the scorer "
     "works when logprobs are unavailable."),
    ("ARBORIS_SCORE_THRESHOLD", "0.5",
     "Answer-score escalation threshold, 0-1. Only used when "
     "ARBORIS_SCORER_ESCALATION is on."),
    ("ARBORIS_MAX_CASCADE_OUTPUT_TOKENS", "0",
     "Budget gate: if the primary answer already used more than "
     "this many output tokens, no escalation happens, even on a "
     "low-confidence trigger. 0 disables. Provider-reported tokens "
     "only; unreported tokens never block escalation."),
    ("ARBORIS_MAX_TOKENS_MULTIPLIER", "1.0",
     "Multiplier on the per-mode max_tokens cap (120/240/400 for "
     "sprout/canopy/full). 1.0 is the current behaviour. Above 1.0, "
     "max_tokens becomes a safety net well above the expected budget, "
     "with format suffixes and (optionally) stop sequences doing the "
     "length control instead of the cap. Must be > 0 and at most 4; "
     "anything else refuses to start. Meta reports max_tokens_used."),
    ("ARBORIS_TASK_TEMPERATURE", "off",
     "1/true/yes/on sends a temperature by task type (fact, "
     "definition, math, code 0.2; steps, reasoning 0.5; creative "
     "0.8; other: provider default) when task routing is on. Off "
     "by default: no temperature is sent, so the provider default "
     "applies. Meta reports temperature."),
    ("ARBORIS_TASK_STOP_SEQUENCES", "off",
     "1/true/yes/on sends stop=[\"\\n\\n\"] for fact and definition "
     "prompts when task routing is on, so the model ends at a "
     "paragraph boundary instead of running on. Off by default: "
     "no stop sequence is sent. Meta reports stop_sequences."),
    ("ARBORIS_LONG_INPUT_PRUNING", "off",
     "1/true/yes/on enables conservative extractive pruning for "
     "prompts longer than ARBORIS_LONG_INPUT_MAX_CHARS: first "
     "and last sentences, plus sentences with digits, questions, "
     "quoted strings, proper nouns, or code, are kept; other "
     "sentences are dropped, and a pruning note is appended so "
     "the model knows input was pruned. Off by default: normal "
     "chat prompts are untouched. Meta reports long_input_pruned, "
     "pruned_sentences, and pruned_chars."),
    ("ARBORIS_LONG_INPUT_MAX_CHARS", "1500",
     "Prompt length above which long-input pruning runs, when "
     "pruning is on. Must be at least 500; anything else refuses "
     "to start."),
    ("ARBORIS_SEMANTIC_CACHE", "off",
     "1/true/yes/on enables a conservative lexical semantic cache: "
     "a fact/definition prompt with a difflib ratio at least 0.90 "
     "and token overlap at least 0.80 against a cached prompt in "
     "the same session, with identical numbers, reuses the cached "
     "answer. This is string similarity, not an embedding model, "
     "so it misses true paraphrases rather than risking a wrong "
     "answer. Meta reports semantic_cache_hit and "
     "semantic_similarity when it fires."),
    ("ARBORIS_DEBUG_LOG", "off",
     "1/true/yes/on logs prompts to local stderr only, never to responses."),
    ("PORT", str(DEFAULT_PORT), "Listen port (also --port)."),
]


class ClientError(Exception):
    """An error with an HTTP status and a plain message safe to show users."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _shrink_version(env):
    """Validate ARBORIS_SHRINK_VERSION: v1 or v2 only."""
    version = (env.get("ARBORIS_SHRINK_VERSION", "") or "").strip().lower() or "v1"
    if version not in ("v1", "v2"):
        raise ValueError("ARBORIS_SHRINK_VERSION must be v1 or v2")
    return version


def _grid_factor(env):
    """Validate ARBORIS_GRID_G_PER_KWH: a positive number, or 400."""
    raw = (env.get("ARBORIS_GRID_G_PER_KWH", "") or "").strip() or "400"
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("ARBORIS_GRID_G_PER_KWH must be a number") from None
    if value <= 0:
        raise ValueError("ARBORIS_GRID_G_PER_KWH must be positive")
    return value


def _unit_threshold(env, name, default):
    """Validate a 0-1 threshold env var, or its default."""
    raw = (env.get(name, "") or "").strip() or str(default)
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number between 0 and 1") from None
    if value != value or value < 0 or value > 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _quality_target(env):
    target = (env.get("ARBORIS_QUALITY_TARGET", "") or "").strip().lower() or "balanced"
    if target not in QUALITY_THRESHOLDS:
        raise ValueError("ARBORIS_QUALITY_TARGET must be eco, balanced, or quality")
    return target


def _long_input_max_chars(env):
    raw = (env.get("ARBORIS_LONG_INPUT_MAX_CHARS", "") or "").strip() or "1500"
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("ARBORIS_LONG_INPUT_MAX_CHARS must be a whole number") from None
    if value < 500:
        raise ValueError("ARBORIS_LONG_INPUT_MAX_CHARS must be at least 500")
    return value


def _max_tokens_multiplier(env):
    raw = (env.get("ARBORIS_MAX_TOKENS_MULTIPLIER", "") or "").strip() or "1.0"
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("ARBORIS_MAX_TOKENS_MULTIPLIER must be a number") from None
    if value != value or value <= 0 or value > 4:
        raise ValueError("ARBORIS_MAX_TOKENS_MULTIPLIER must be > 0 and at most 4")
    return value


_LEGACY_ENV_PREFIX = "GREENSEARCH_"
_ENV_PREFIX = "ARBORIS_"


def _with_legacy_env(env):
    """Return (env copy, legacy names used) with GREENSEARCH_* mapped to ARBORIS_*.

    The project was renamed from GreenSearch in 0.9.0. For one release a
    deployment that still sets GREENSEARCH_X keeps working: the value is
    used for ARBORIS_X when ARBORIS_X is unset or blank. An ARBORIS_X
    value always wins. The names used are returned so startup can log them.
    """
    merged = dict(env)
    used = []
    for name, value in env.items():
        if not name.startswith(_LEGACY_ENV_PREFIX):
            continue
        new_name = _ENV_PREFIX + name[len(_LEGACY_ENV_PREFIX):]
        if not str(merged.get(new_name, "")).strip():
            merged[new_name] = value
            used.append(name)
    return merged, tuple(sorted(used))


@dataclass
class Config:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    # Tried in order after the primary model fails; never contains it.
    fallback_models: tuple = DEFAULT_FALLBACKS
    # Empty or equal to model = no escalation call at all.
    escalation_model: str = ""
    cascade_threshold: float = -1.0
    allowed_origins: tuple = DEFAULT_ORIGINS
    rate_limit_per_hour: int = 30
    rate_limit_per_day: int = 100
    global_limit_per_day: int = 800
    allow_paid_models: bool = False
    small_model: str = "liquid/lfm-2.5-2.6b:free"
    small_model_routing: bool = False
    shrink_version: str = "v1"
    grid_g_per_kwh: float = 400.0
    task_routing: bool = False
    difficulty_routing: bool = False
    router_threshold: float = 0.5
    quality_target: str = "balanced"
    scorer_escalation: bool = False
    score_threshold: float = 0.5
    max_cascade_output_tokens: int = 0
    max_tokens_multiplier: float = 1.0
    task_temperature: bool = False
    task_stop_sequences: bool = False
    long_input_pruning: bool = False
    long_input_max_chars: int = 1500
    semantic_cache: bool = False
    debug_log: bool = False
    # GREENSEARCH_* names read because their ARBORIS_* twin was unset.
    legacy_env_names: tuple = ()

    @property
    def router_threshold_effective(self):
        """Eco/quality presets override the raw threshold; balanced uses it."""
        if self.quality_target in ("eco", "quality"):
            return QUALITY_THRESHOLDS[self.quality_target]
        return self.router_threshold

    def __post_init__(self):
        if not self.escalation_model:
            self.escalation_model = self.model
        self.fallback_models = parse_fallbacks(self.fallback_models, self.model)

    @property
    def configured(self):
        return bool(self.api_key)

    @property
    def cascade_enabled(self):
        """True only when a separate escalation model is configured."""
        return self.escalation_model != self.model

    @classmethod
    def from_env(cls, env=None):
        """Build a Config from environment variables.

        Raises ValueError with a plain message on an invalid value, so
        the server refuses to start instead of running misconfigured.
        """
        env, legacy_names = _with_legacy_env(
            os.environ if env is None else env)

        def get(name, default):
            value = env.get(name, "").strip()
            return value or default

        model = get("ARBORIS_MODEL", DEFAULT_MODEL)
        raw_fallbacks = env.get("ARBORIS_MODEL_FALLBACKS", "").strip()
        if not raw_fallbacks:
            fallbacks = DEFAULT_FALLBACKS
        elif raw_fallbacks.lower() == "none":
            fallbacks = ()
        else:
            fallbacks = raw_fallbacks.split(",")
        try:
            threshold = float(get("ARBORIS_CASCADE_THRESHOLD", "-1.0"))
        except ValueError:
            raise ValueError("ARBORIS_CASCADE_THRESHOLD must be a number, e.g. -1.0")
        if threshold != threshold or threshold > 0:  # NaN or positive
            raise ValueError("ARBORIS_CASCADE_THRESHOLD must be <= 0 (logprobs are <= 0)")
        def _int(name, default):
            try:
                value = int(get(name, str(default)))
            except ValueError:
                raise ValueError(f"{name} must be a whole number")
            if value < 0:
                raise ValueError(f"{name} must be 0 or more")
            return value
        rate = _int("ARBORIS_RATE_LIMIT_PER_HOUR", 30)
        rate_day = _int("ARBORIS_RATE_LIMIT_PER_DAY", 100)
        global_day = _int("ARBORIS_GLOBAL_LIMIT_PER_DAY", 800)
        origins = parse_origins(env.get("ARBORIS_BACKEND_ORIGIN", "")) or DEFAULT_ORIGINS
        if any(o == "*" or "*" in o for o in origins):
            raise ValueError("ARBORIS_BACKEND_ORIGIN must list exact origins; '*' is not allowed")
        config = cls(
            api_key=env.get("ARBORIS_API_KEY", "").strip(),
            base_url=get("ARBORIS_BASE_URL", DEFAULT_BASE_URL),
            model=model,
            fallback_models=fallbacks,
            escalation_model=get("ARBORIS_ESCALATION_MODEL", model),
            cascade_threshold=threshold,
            allowed_origins=origins,
            rate_limit_per_hour=rate,
            rate_limit_per_day=rate_day,
            global_limit_per_day=global_day,
            allow_paid_models=env.get("ARBORIS_ALLOW_PAID_MODELS", "").strip().lower()
            in ("1", "true", "yes", "on"),
            small_model=get("ARBORIS_SMALL_MODEL", "liquid/lfm-2.5-2.6b:free"),
            small_model_routing=env.get("ARBORIS_SMALL_MODEL_ROUTING", "").strip().lower()
            in ("1", "true", "yes", "on"),
            shrink_version=_shrink_version(env),
            grid_g_per_kwh=_grid_factor(env),
            task_routing=env.get("ARBORIS_TASK_ROUTING", "").strip().lower()
            in ("1", "true", "yes", "on"),
            difficulty_routing=env.get("ARBORIS_DIFFICULTY_ROUTING", "").strip().lower()
            in ("1", "true", "yes", "on"),
            router_threshold=_unit_threshold(env, "ARBORIS_ROUTER_THRESHOLD", 0.5),
            quality_target=_quality_target(env),
            scorer_escalation=env.get("ARBORIS_SCORER_ESCALATION", "").strip().lower()
            in ("1", "true", "yes", "on"),
            score_threshold=_unit_threshold(env, "ARBORIS_SCORE_THRESHOLD", 0.5),
            max_cascade_output_tokens=_int("ARBORIS_MAX_CASCADE_OUTPUT_TOKENS", 0),
            max_tokens_multiplier=_max_tokens_multiplier(env),
            task_temperature=env.get("ARBORIS_TASK_TEMPERATURE", "").strip().lower()
            in ("1", "true", "yes", "on"),
            task_stop_sequences=env.get("ARBORIS_TASK_STOP_SEQUENCES", "").strip().lower()
            in ("1", "true", "yes", "on"),
            long_input_pruning=env.get("ARBORIS_LONG_INPUT_PRUNING", "").strip().lower()
            in ("1", "true", "yes", "on"),
            long_input_max_chars=_long_input_max_chars(env),
            semantic_cache=env.get("ARBORIS_SEMANTIC_CACHE", "").strip().lower()
            in ("1", "true", "yes", "on"),
            debug_log=env.get("ARBORIS_DEBUG_LOG", "").strip().lower()
            in ("1", "true", "yes", "on"),
            legacy_env_names=legacy_names,
        )
        # Paid guard lives here, not in __post_init__: direct Config() is
        # the test harness (fake model names are not real paid models), while
        # server startup always goes through from_env, so a real deployment
        # cannot accidentally run a paid model without the explicit flag.
        if not config.allow_paid_models:
            seen = []
            for m in (config.model,) + config.fallback_models + (config.small_model,) + (
                    (config.escalation_model,) if config.cascade_enabled else ()):
                if not is_free_model(m) and m not in seen:
                    seen.append(m)
            if seen:
                raise ValueError(
                    "Paid model(s) configured without ARBORIS_ALLOW_PAID_MODELS: "
                    + ", ".join(seen)
                    + ". Set ARBORIS_ALLOW_PAID_MODELS=1 to allow paid models deliberately.")
        return config


def parse_fallbacks(models, primary):
    """Strip, drop empties, the primary model and repeats; keep order."""
    out = []
    for model in models:
        model = model.strip()
        if model and model != primary and model not in out:
            out.append(model)
    return tuple(out)


def parse_origins(raw):
    """Split a CORS allowlist on commas and/or whitespace, drop empties and
    repeats, keep order. Trailing slashes are stripped, so
    "https://example.com/" is stored (and matched) as "https://example.com";
    browsers never send a trailing slash in Origin. Matching stays exact."""
    out = []
    for origin in re.split(r"[,\s]+", raw or ""):
        origin = origin.rstrip("/")
        if origin and origin not in out:
            out.append(origin)
    return tuple(out)


def is_free_model(model):
    return model == "openrouter/free" or model.endswith(":free")


# ---------------------------------------------------------------------------
# Rate limiting and per-session caches (in memory, per process)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Rolling-window request counter per client key (the client IP).

    In memory only: it resets when the server restarts and is not shared
    between processes. limit <= 0 disables it.
    """

    def __init__(self, limit, window_seconds=3600.0, clock=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, client):
        if self.limit <= 0:
            return True
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault(client, collections.deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10000:  # drop idle clients so memory stays bounded
                for key in [k for k, v in self._hits.items() if not v]:
                    del self._hits[key]
            return True


class SessionStore:
    """session id -> SessionCache, least recently used evicted first."""

    def __init__(self, max_sessions=MAX_SESSIONS):
        self.max_sessions = max_sessions
        self._caches = collections.OrderedDict()
        self.lock = threading.Lock()

    def get(self, session_id):
        """Return the cache for session_id. Call with self.lock held."""
        cache = self._caches.get(session_id)
        if cache is None:
            cache = SessionCache()
            self._caches[session_id] = cache
            while len(self._caches) > self.max_sessions:
                self._caches.popitem(last=False)
        else:
            self._caches.move_to_end(session_id)
        return cache


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

@dataclass
class ChatRequest:
    prompt: str
    mode: str = "auto"
    session_id: Optional[str] = None
    stream: bool = True
    measure_baseline: bool = False
    # Expand-on-demand: the original prompt this request expands on.
    # It is a label and a routing instruction, not a privilege: an
    # expansion is equivalent to an explicit mode=full request (the
    # user could already send one), plus measurement. The prompt
    # itself is what the model sees; expand_of is what meta reports.
    expand_of: Optional[str] = None


def parse_chat_request(body):
    """Validate a JSON request body (bytes). Raises ClientError(400)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ClientError(400, "Request body must be valid JSON")
    if not isinstance(data, dict):
        raise ClientError(400, "Request body must be a JSON object")
    if any(str(k).lower() in _FORBIDDEN_FIELDS for k in data):
        raise ClientError(
            400,
            "Do not send an API key, model or provider URL; those are set on the server only",
        )
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ClientError(400, "Prompt is empty: type a question first")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ClientError(400, f"Prompt is too long (max {MAX_PROMPT_CHARS} characters)")
    mode = data.get("mode", "auto")
    if mode is None:
        mode = "auto"
    if mode == "forest":  # the website's name for full
        mode = "full"
    if mode != "auto" and mode not in MODES:
        raise ClientError(400, "Mode must be one of auto, sprout, canopy, full")
    session_id = data.get("session_id")
    if session_id is not None and (
        not isinstance(session_id, str) or not _SESSION_ID.match(session_id)
    ):
        raise ClientError(400, "session_id must be 8-64 letters, digits, '-' or '_'")
    stream = data.get("stream", True)
    measure = data.get("measure_baseline", False)
    if not isinstance(stream, bool) or not isinstance(measure, bool):
        raise ClientError(400, "stream and measure_baseline must be true or false")
    expand_of = data.get("expand_of")
    if expand_of is not None:
        if not isinstance(expand_of, str) or not expand_of.strip():
            raise ClientError(400, "expand_of must be a non-empty string")
        if len(expand_of) > MAX_PROMPT_CHARS:
            raise ClientError(400, f"expand_of is too long (max {MAX_PROMPT_CHARS} characters)")
    return ChatRequest(
        prompt=prompt, mode=mode, session_id=session_id,
        stream=stream, measure_baseline=measure, expand_of=expand_of,
    )


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

class _Recorder:
    """Wraps a provider to remember whether it was called and what it returned.

    run_cascade only returns the final completion; the server also needs
    the primary model's usage when it escalated, and needs to know whether
    a failure came from the escalation call.
    """

    def __init__(self, provider):
        self.provider = provider
        self.model = getattr(provider, "model", getattr(provider, "name", "unknown"))
        self.attempted = False
        self.completion = None

    def complete_ex(self, prompt, system=None, max_tokens=None, logprobs=False,
                    stop=None, temperature=None):
        self.attempted = True
        extra = {}
        if stop is not None:
            extra["stop"] = stop
        if temperature is not None:
            extra["temperature"] = temperature
        self.completion = self.provider.complete_ex(
            prompt, system=system, max_tokens=max_tokens, logprobs=logprobs, **extra
        )
        return self.completion


def _semantic_lookup(entries, key):
    """Conservative lexical semantic cache lookup.

    entries: a session cache's key -> entry dict. key: the exact
    cache key for the current prompt (with any small-tier prefix).
    Returns (entry, similarity) or (None, None).

    Similarity is difflib's ratio on the normalized prompts, plus a
    token-overlap guard, both high (0.90 / 0.80). The ratio is the
    primary gate: it already blocks a different subject word
    ("France" vs "Spain" scores ~0.81), while the overlap guard
    blocks prompts that share a long prefix but ask about different
    things. Numbers must match exactly: a prompt that differs only
    in a number is a different question, and returning the old
    answer would be wrong. This is string similarity, not an
    embedding model: it misses true paraphrases rather than risking
    a wrong answer.
    """
    import difflib
    if not key or ":" not in key:
        return None, None
    prefix, current = key.rsplit(":", 1)
    prefix += ":"
    current_tokens = set(current.split())
    current_numbers = sorted(re.findall(r"\d+(?:\.\d+)?", current))
    best = None
    best_ratio = 0.0
    for cand_key, entry in entries.items():
        if not cand_key.startswith(prefix) or not isinstance(entry, dict):
            continue
        candidate = cand_key[len(prefix):]
        if candidate == current:
            continue  # exact hits are handled by the exact lookup
        if sorted(re.findall(r"\d+(?:\.\d+)?", candidate)) != current_numbers:
            continue
        cand_tokens = set(candidate.split())
        overlap = (len(current_tokens & cand_tokens) / len(current_tokens | cand_tokens)
                   if current_tokens | cand_tokens else 0.0)
        if overlap < 0.80:
            continue
        ratio = difflib.SequenceMatcher(None, current, candidate).ratio()
        if ratio >= 0.90 and ratio > best_ratio:
            best, best_ratio = entry, ratio
    if best is None:
        return None, None
    return best, round(best_ratio, 3)


def _sum_or_none(values):
    """Sum provider-reported counts; None if any call did not report one."""
    if not values or any(v is None for v in values):
        return None
    return sum(values)


_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
_STATUS_MESSAGES = {
    401: MSG_KEY_REJECTED,
    402: MSG_NO_CREDITS,
    404: MSG_MODEL_NOT_FOUND,
    429: MSG_RATE_LIMITED,
}


def _http_status(exc):
    """The provider HTTP status of a ProviderError, or None."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    match = _HTTP_STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def _provider_error(exc):
    """Map a ProviderError to a plain ClientError. The detail is dropped."""
    if getattr(exc, "empty_content", False):
        return ClientError(502, MSG_EMPTY)
    status = _http_status(exc)
    if status in _STATUS_MESSAGES:
        return ClientError(status, _STATUS_MESSAGES[status])
    return ClientError(502, MSG_UNREACHABLE)


def _most_specific(errors):
    """First error, unless a later one has a specific code (401/402/404/429)
    and the first does not (e.g. a generic 502/503 or no status)."""
    chosen = errors[0]
    if _http_status(chosen) not in _STATUS_MESSAGES:
        for exc in errors[1:]:
            if _http_status(exc) in _STATUS_MESSAGES:
                return exc
    return chosen


def _status_text(exc):
    status = _http_status(exc)
    return f"HTTP {status}" if status is not None else "no HTTP status"


class _ModelChain:
    """The primary side of a request: the primary model, then fallbacks.

    Looks like a provider to run_cascade. On a ProviderError it retries
    the same model once without logprobs if the error mentions logprobs,
    then moves to the next model on 429/404/502/503, on empty content
    (HTTP 200 with null/empty text, a transient provider failure),
    and on 402 only towards a free model. 401 and any other error stop
    at once. After the call, model is the model that answered. Logs
    carry the HTTP status and model id only: never the key, the prompt
    or provider detail text.
    """

    def __init__(self, backend, models):
        self.backend = backend
        self.models = list(models)
        self.model = self.models[0]
        self.attempted = False
        self.completion = None

    def complete_ex(self, prompt, system=None, max_tokens=None, logprobs=False,
                    stop=None, temperature=None):
        self.attempted = True
        errors = []
        for i, model in enumerate(self.models):
            role = "primary" if i == 0 else "fallback"
            provider = self.backend._make(model)
            try:
                comp = self._call(provider, model, role, prompt, system,
                                  max_tokens, logprobs, stop, temperature)
            except ProviderError as exc:
                errors.append(exc)
                self.backend.log(f"{role} call failed: {_status_text(exc)} model={model}")
                status = _http_status(exc)
                nxt = self.models[i + 1] if i + 1 < len(self.models) else None
                if nxt is None or not (
                    status in FALLBACK_STATUSES or (status == 402 and is_free_model(nxt))
                    or getattr(exc, "empty_content", False)
                ):
                    break
                continue
            self.model = model
            self.completion = comp
            self.backend.log(f"answered by {role} model={model}")
            return comp
        raise _most_specific(errors)

    def _call(self, provider, model, role, prompt, system, max_tokens, logprobs,
              stop=None, temperature=None):
        extra = {}
        if stop is not None:
            extra["stop"] = stop
        if temperature is not None:
            extra["temperature"] = temperature
        try:
            return provider.complete_ex(
                prompt, system=system, max_tokens=max_tokens, logprobs=logprobs, **extra
            )
        except ProviderError as exc:
            if not logprobs or "logprob" not in str(exc).lower():
                raise
            self.backend.log(
                f"{role} call failed: {_status_text(exc)} model={model} "
                "(logprobs rejected); retrying once without logprobs"
            )
        return provider.complete_ex(
            prompt, system=system, max_tokens=max_tokens, logprobs=False, **extra
        )


class Backend:
    """Runs the chat pipeline. Holds config, caches and the rate limiter."""

    def __init__(self, config, provider_factory=None, clock=time.monotonic,
                 log_stream=None, quiet=False):
        self.config = config
        self.provider_factory = provider_factory or self._default_provider
        self.rate_limiter = RateLimiter(config.rate_limit_per_hour, 3600.0, clock)
        self.daily_limiter = RateLimiter(config.rate_limit_per_day, 86400.0, clock)
        self.global_limiter = RateLimiter(config.global_limit_per_day, 86400.0, clock)
        self.sessions = SessionStore()
        self.log_stream = log_stream if log_stream is not None else sys.stderr
        self.quiet = quiet

    def allow_request(self, client):
        """POC abuse guard, checked in order. Returns (allowed, retry_after_seconds).

        Hourly per-IP, then daily per-IP, then global daily. All three are
        in-memory and per-process: Cloud Run may run several processes, so
        the effective caps multiply, and they reset on restart/deploy.
        That is a proof-of-concept limit, not commercial-grade metering.
        """
        if not self.rate_limiter.allow(client):
            return False, 3600
        if not self.daily_limiter.allow(client):
            return False, 86400
        if not self.global_limiter.allow("global"):
            return False, 86400
        return True, 0

    def _default_provider(self, model):
        return OpenAICompatibleProvider(
            self.config.base_url, api_key=self.config.api_key, model=model
        )

    def log(self, message):
        if not self.quiet:
            print(f"[arboris-server] {message}", file=self.log_stream, flush=True)

    def debug(self, message):
        """Prompt-bearing log lines: only with ARBORIS_DEBUG_LOG, stderr only."""
        if self.config.debug_log:
            print(f"[arboris-server debug] {message}", file=self.log_stream, flush=True)

    def _make(self, model):
        try:
            return self.provider_factory(model)
        except ValueError:
            # e.g. a non-https base URL. The detail stays on the server.
            self.log("provider setup failed: check ARBORIS_BASE_URL")
            raise ClientError(503, MSG_MISCONFIGURED)

    def health(self):
        cfg = self.config
        return {
            "status": "ok",
            "configured": cfg.configured,
            "model": cfg.model,
            "fallbacks": list(cfg.fallback_models),
            "escalation_model": cfg.escalation_model,
            "cascade_enabled": cfg.cascade_enabled,
            "cascade_threshold": cfg.cascade_threshold,
            "rate_limit_per_hour": cfg.rate_limit_per_hour,
            "rate_limit_per_day": cfg.rate_limit_per_day,
            "global_limit_per_day": cfg.global_limit_per_day,
            "allow_paid_models": cfg.allow_paid_models,
            "small_model": cfg.small_model,
            "small_model_routing": cfg.small_model_routing,
            "shrink_version": cfg.shrink_version,
            "grid_g_per_kwh": cfg.grid_g_per_kwh,
            "task_routing": cfg.task_routing,
            "difficulty_routing": cfg.difficulty_routing,
            "router_threshold": cfg.router_threshold_effective,
            "quality_target": cfg.quality_target,
            "scorer_escalation": cfg.scorer_escalation,
            "score_threshold": cfg.score_threshold,
            "max_cascade_output_tokens": cfg.max_cascade_output_tokens,
            "max_tokens_multiplier": cfg.max_tokens_multiplier,
            "task_temperature": cfg.task_temperature,
            "task_stop_sequences": cfg.task_stop_sequences,
            "long_input_pruning": cfg.long_input_pruning,
            "long_input_max_chars": cfg.long_input_max_chars,
            "semantic_cache": cfg.semantic_cache,
            "provider_streaming": False,
        }

    def chat(self, req):
        """Run the pipeline. Returns {"answer": str, "meta": dict}.

        Raises ClientError with a plain message on any failure.
        """
        started = time.monotonic()
        cfg = self.config
        if not cfg.configured:
            raise ClientError(503, MSG_NOT_CONFIGURED)

        # 1. PromptShrink: v1 by default, v2 only when ARBORIS_SHRINK_VERSION=v2.
        sr = shrink_v2(req.prompt) if cfg.shrink_version == "v2" else shrink(req.prompt)
        prompt = sr.text
        if not prompt.strip():
            raise ClientError(400, "Prompt is empty after removing filler words: add a question")

        # 3 (needed for the cache key). Routing, unless the client chose a mode.
        # Task routing (opt-in) classifies by task type and overrides
        # word-count routing for auto mode only; an explicit client mode
        # always wins, and unknown prompts fall back to word count
        # inside route_task itself. The route is still reported in meta
        # for explicit modes, so routed answers can be judged later.
        # The difficulty score is computed for every prompt (it is
        # cheap and local) and reported in meta; it only changes the
        # tier when both task routing and difficulty routing are on.
        task_route = route_task(prompt) if cfg.task_routing else None
        diff_score = difficulty_score(prompt)
        is_expansion = bool(req.expand_of and req.expand_of.strip())
        if is_expansion:
            # Expand-on-demand: the user asked for more detail on an
            # earlier answer, so the first answer was answer-first by
            # design, and this second call is the detail. It always
            # goes full mode and primary tier: expanding with the
            # small model that gave the short answer would be a
            # second short answer. The task format suffix is replaced
            # by a detail instruction, so a fact suffix ("answer
            # directly") does not pull the detail back to one line.
            mode = "full"
            routed_by = "expansion"
            system_prompt = BREVITY_SYSTEM["full"] + " Explain in detail."
        elif req.mode == "auto" and task_route is not None:
            mode = task_route.mode
            routed_by = "task"
            system_prompt = BREVITY_SYSTEM[mode]
            if task_route.format_suffix:
                system_prompt = system_prompt + " " + task_route.format_suffix
        else:
            mode = route_mode(prompt) if req.mode == "auto" else req.mode
            routed_by = "explicit" if req.mode != "auto" else "word_count"
            system_prompt = BREVITY_SYSTEM[mode]
        self.debug(f"prompt={req.prompt!r} shrunk={prompt!r} mode={mode}")

        meta = {
            "mode": "forest" if mode == "full" else mode,
            "mode_source": "auto" if req.mode == "auto" else "explicit",
            "task_type": task_route.task_type if task_route is not None else None,
            "task_tier": task_route.tier if task_route is not None else None,
            "routed_by": routed_by,
            "difficulty_score": diff_score,
            "difficulty_tier": None,
            "difficulty_routing": cfg.difficulty_routing and routed_by == "task",
            "router_threshold": cfg.router_threshold_effective,
            "quality_target": cfg.quality_target,
            "local_answer": False,
            "semantic_cache_hit": False,
            "semantic_similarity": None,
            "cache_fallback": False,
            "cache_bypass_reason": None,
            "answer_score": None,
            "expansion": is_expansion,
            "expanded_from": req.expand_of if is_expansion else None,
            "questions": prompt.count("?"),
            "multi_part": prompt.count("?") >= 2,
            "long_input_pruned": False,
            "pruned_sentences": 0,
            "pruned_chars": None,
            "shrink": {
                "version": cfg.shrink_version,
                "original_tokens_est": sr.original_tokens,
                "shrunk_tokens_est": sr.shrunk_tokens,
                "saved_tokens_est": sr.saved_tokens,
                "changed": sr.changed,
            },
            "provider_streaming": False,
            "baseline": None,
        }

        # No-model gate: pure arithmetic is answered locally, with
        # 0 tokens and 0 estimated energy, before any model is called.
        # Anything that is not pure arithmetic falls through below.
        local_answer = try_local_calc(prompt)
        if local_answer is not None:
            meta.update({
                "task_type": "math",
                "task_tier": "local",
                "routed_by": "local",
                "local_answer": True,
                "cache_hit": False,
                "model": None,
                "model_tier": "local",
                "escalated": False,
                "escalation_reason": "local calculation: no model call",
                "avg_logprob": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "new_output_tokens": 0,
                "tokens_source": "local calculation: no model call, 0 new tokens",
                "trimmed": False,
                "truncated": False,
                "latency_ms": round((time.monotonic() - started) * 1000),
            })
            receipt = energy_receipt(0, 0, model=None, model_tier="local",
                                     grid_g_per_kwh=cfg.grid_g_per_kwh)
            receipt["note"] = ("Local calculation: no model call, so this "
                               "answer cost no new tokens or energy.")
            meta["energy"] = receipt
            self.log("chat local_calc in=0 out=0 task=math tier=local")
            return {"answer": local_answer, "meta": meta}

        # Small-model routing (opt-in): sprout prompts go to the small
        # model first, with the primary chain as fallback. "Sprout" here
        # is route_mode's heuristic (short, simple, no complexity markers),
        # not a measured difficulty score, so small-model answers need
        # live judging before routing is left on. With task routing on,
        # the task tier replaces the sprout rule: a long, easy fact
        # goes small, a short, hard question goes primary. With
        # difficulty routing on as well, the difficulty threshold
        # replaces the task tier. The cache key gains a small tier
        # prefix, so a small-model cached answer is never returned for
        # a primary-tier request, or vice versa.
        if is_expansion:
            use_small = False
        elif routed_by == "task":
            if cfg.difficulty_routing:
                diff_tier = ("small" if diff_score < cfg.router_threshold_effective
                             else "primary")
                meta["difficulty_tier"] = diff_tier
                use_small = (cfg.small_model_routing and diff_tier == "small"
                             and cfg.small_model != cfg.model)
            else:
                use_small = (cfg.small_model_routing and task_route.tier == "small"
                             and cfg.small_model != cfg.model)
        else:
            use_small = (cfg.small_model_routing and mode == "sprout"
                         and cfg.small_model != cfg.model)
        meta["routed_to_small"] = use_small
        meta["small_model"] = cfg.small_model if use_small else None

        # 2. Session cache (mode-aware key, per client session id).
        # A cached answer that was truncated, or scored below 0.3 when
        # it was produced, is not reused: repeating the prompt calls
        # the model again for a better answer. The 0.3 bar is stricter
        # than the 0.5 escalation threshold, so only clearly bad
        # answers bypass the cache, not merely mediocre ones. If the
        # fresh call then fails, the cached answer is returned as a
        # fallback (see the ProviderError handler below), so a bad
        # cached answer never turns into an error the user did not
        # have before.
        bad_cache_entry = None
        cache_bypass_reason = None
        key = cache_key(prompt, mode) if req.session_id else ""
        if key and use_small:
            key = "small:" + key
        if key:
            with self.sessions.lock:
                entry = self.sessions.get(req.session_id).lookup(key)
                entry = dict(entry) if entry else None
            if entry is not None:
                cached_score = entry.get("answer_score")
                if entry.get("truncated"):
                    cache_bypass_reason = "cached answer was truncated"
                elif (cached_score is not None and cached_score < 0.3):
                    cache_bypass_reason = f"cached answer scored {cached_score} (< 0.3)"
                if cache_bypass_reason is None:
                    meta.update({
                        "cache_hit": True,
                        "model": None,
                        "model_tier": "small" if use_small and entry.get("model") == cfg.small_model else "primary",
                        "cached_from_model": entry.get("model"),
                        "escalated": False,
                        "cached_answer_was_escalated": bool(entry.get("escalated")),
                        "escalation_reason": "cache hit: no model call",
                        "avg_logprob": None,
                        "answer_score": cached_score,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "new_output_tokens": 0,
                        "tokens_source": "cache hit: no model call, 0 new tokens",
                        "energy": energy_receipt(
                            0, 0, model=entry.get("model"),
                            model_tier="small" if use_small and entry.get("model") == cfg.small_model else "primary",
                            cache_hit=True,
                            grid_g_per_kwh=cfg.grid_g_per_kwh),
                        "trimmed": False,
                        "truncated": False,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                    })
                    self.log(f"chat cache_hit model={entry.get('model')} in=0 out=0 "
                         f"task={meta.get('task_type') or '-'} tier={meta.get('model_tier')}")
                    return {"answer": entry["answer"], "meta": meta}
                bad_cache_entry = entry
                self.log(f"chat cache_bypass reason={cache_bypass_reason}: calling model again")
            # Lexical semantic cache (opt-in): fact/definition prompts
            # only, same session, same mode/tier, identical numbers,
            # similarity at least 0.90. A miss costs nothing but a
            # local string comparison; a hit reuses a cached answer.
            if (bad_cache_entry is None and cfg.semantic_cache and task_route is not None
                    and task_route.task_type in ("fact", "definition")):
                with self.sessions.lock:
                    entries = dict(self.sessions.get(req.session_id).entries)
                sem_entry, similarity = _semantic_lookup(entries, key)
                if (sem_entry is not None and sem_entry.get("truncated")) or (
                        sem_entry is not None and sem_entry.get("answer_score") is not None
                        and sem_entry.get("answer_score") < 0.3):
                    # Same bad-answer rule as the exact cache, without
                    # the fallback: a similar (not identical) cached
                    # answer is not a safe fallback on model failure.
                    self.log("chat semantic_cache_bypass: cached answer was truncated or low-scored")
                    sem_entry = None
                if sem_entry is not None:
                    meta.update({
                        "cache_hit": True,
                        "semantic_cache_hit": True,
                        "semantic_similarity": similarity,
                        "model": None,
                        "model_tier": "small" if use_small and sem_entry.get("model") == cfg.small_model else "primary",
                        "cached_from_model": sem_entry.get("model"),
                        "escalated": False,
                        "cached_answer_was_escalated": bool(sem_entry.get("escalated")),
                        "escalation_reason": "semantic cache hit: no model call",
                        "avg_logprob": None,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "new_output_tokens": 0,
                        "tokens_source": "semantic cache hit: no model call, 0 new tokens",
                        "energy": energy_receipt(
                            0, 0, model=sem_entry.get("model"),
                            model_tier="small" if use_small and sem_entry.get("model") == cfg.small_model else "primary",
                            cache_hit=True,
                            grid_g_per_kwh=cfg.grid_g_per_kwh),
                        "trimmed": False,
                        "truncated": False,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                    })
                    self.log(f"chat semantic_cache_hit similarity={similarity} in=0 out=0 "
                             f"task={meta.get('task_type') or '-'} tier={meta.get('model_tier')}")
                    return {"answer": sem_entry["answer"], "meta": meta}

        # Long-input pruning (opt-in): only the model prompt is
        # pruned. Routing, the cache key, and scoring of the original
        # question use the unpruned prompt, so a pruned answer is
        # still cached under the prompt the user actually sent, and
        # a repeat returns it without re-pruning.
        model_prompt = prompt
        if cfg.long_input_pruning and len(prompt) > cfg.long_input_max_chars:
            model_prompt, dropped, _orig = prune_long_prompt(
                prompt, max_chars=cfg.long_input_max_chars)
            if dropped:
                meta["long_input_pruned"] = True
                meta["pruned_sentences"] = dropped
                meta["pruned_chars"] = len(model_prompt)

        # 4. Primary model (then fallbacks); logprob-gated escalation only
        #    if a separate escalation model is configured. With small-model
        #    routing, the small model is first in the chain, then the
        #    primary and its fallbacks, deduplicated in order.
        if use_small:
            chain_models = (cfg.small_model,) + tuple(
                m for m in (cfg.model,) + cfg.fallback_models if m != cfg.small_model)
        else:
            chain_models = (cfg.model,) + cfg.fallback_models
        primary = _ModelChain(self, chain_models)
        escalation = _Recorder(self._make(cfg.escalation_model)) if cfg.cascade_enabled else None
        # Length control, brief #4: max_tokens is a cap by default
        # (multiplier 1.0, unchanged behaviour). Above 1.0 it becomes
        # a safety net, with format suffixes and stop sequences doing
        # the length control. Temperature and stop sequences are
        # opt-in and task-gated: temperature only when task routing
        # is on, stop only for fact/definition, never for expansions
        # (a detail answer legitimately has paragraphs).
        effective_max_tokens = int(MAX_TOKENS[mode] * cfg.max_tokens_multiplier)
        temperature = (TASK_TEMPERATURES.get(task_route.task_type)
                       if cfg.task_temperature and task_route is not None else None)
        stop = (["\n\n"] if cfg.task_stop_sequences and not is_expansion
                and task_route is not None
                and task_route.task_type in ("fact", "definition") else None)
        meta["max_tokens_used"] = effective_max_tokens
        meta["temperature"] = temperature
        meta["stop_sequences"] = stop
        def _score(prompt_arg, comp):
            return score_answer(
                prompt_arg, comp.text, mode=mode,
                task_type=task_route.task_type if task_route is not None else None,
                avg_logprob=comp.avg_logprob,
                finish_reason=comp.finish_reason,
            )

        try:
            if escalation is not None:
                result = run_cascade(
                    primary, escalation, model_prompt,
                    system=system_prompt, max_tokens=effective_max_tokens,
                    threshold=cfg.cascade_threshold,
                    score_fn=_score,
                    score_threshold=cfg.score_threshold,
                    score_escalates=cfg.scorer_escalation,
                    max_total_tokens=cfg.max_cascade_output_tokens or None,
                    stop=stop, temperature=temperature,
                )
            else:
                comp = primary.complete_ex(
                    model_prompt, system=system_prompt, max_tokens=effective_max_tokens,
                    logprobs=True, stop=stop, temperature=temperature,
                )
                scored = _score(model_prompt, comp)
                result = CascadeResult(
                    completion=comp, escalated=False, avg_logprob=comp.avg_logprob,
                    reason=SINGLE_MODEL_REASON, primary_model=primary.model,
                    score=scored["score"], score_reasons=tuple(scored["reasons"]),
                )
        except ProviderError as exc:
            if escalation is not None and escalation.attempted:
                self.log("escalation call failed; no answer returned")
                raise ClientError(502, MSG_ESCALATION_FAILED)
            if bad_cache_entry is not None:
                # The fresh call failed, but a cached answer exists:
                # return it rather than turning a bad answer into an
                # error. Meta says so explicitly.
                meta.update({
                    "cache_hit": True,
                    "cache_fallback": True,
                    "cache_bypass_reason": cache_bypass_reason,
                    "model": None,
                    "model_tier": "small" if use_small and bad_cache_entry.get("model") == cfg.small_model else "primary",
                    "cached_from_model": bad_cache_entry.get("model"),
                    "escalated": False,
                    "escalation_reason": "fresh call failed; cached answer returned as fallback",
                    "avg_logprob": None,
                    "answer_score": bad_cache_entry.get("answer_score"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "new_output_tokens": 0,
                    "tokens_source": "cache fallback: no new model call, 0 new tokens",
                    "energy": energy_receipt(
                        0, 0, model=bad_cache_entry.get("model"),
                        model_tier="small" if use_small and bad_cache_entry.get("model") == cfg.small_model else "primary",
                        cache_hit=True,
                        grid_g_per_kwh=cfg.grid_g_per_kwh),
                    "trimmed": False,
                    "truncated": bool(bad_cache_entry.get("truncated")),
                    "latency_ms": round((time.monotonic() - started) * 1000),
                })
                self.log(f"chat cache_fallback: fresh call failed, cached answer returned "
                         f"task={meta.get('task_type') or '-'} tier={meta.get('model_tier')}")
                return {"answer": bad_cache_entry["answer"], "meta": meta}
            mapped = _provider_error(exc)
            self.log(f"no model answered; returning {mapped.status}")
            raise mapped
        except ValueError:
            self.log("pipeline rejected a value (see cascade/provider)")
            raise ClientError(500, MSG_INTERNAL)

        comp = result.completion
        calls = [primary.completion] + ([escalation.completion] if result.escalated else [])

        # 5. Safety-net trim.
        raw_text = comp.text.strip()
        answer, fragment_fixed = apply_brevity_with_flag(raw_text, mode)
        answer = answer.strip()
        if not answer:
            answer = raw_text  # nothing trimmable survived; show the model's text as-is
            fragment_fixed = False
        # Single-word exception: brevity marks a lone unit without
        # terminal punctuation as a fragment ("Paris" -> "Paris…").
        # A single word is a plausible complete answer (a fact, a yes/no,
        # a number), so when the provider explicitly finished ("stop")
        # it is treated as complete, not cut off. Any other finish
        # reason, including "length" and unreported, keeps the mark:
        # a multi-word fragment, or an unconfirmed finish, stays visibly
        # incomplete rather than silently looking finished.
        if (getattr(comp, "finish_reason", None) == "stop"
                and " " not in raw_text and answer == raw_text + "…"):
            answer = raw_text
            fragment_fixed = False
        output_tokens = _sum_or_none([c.completion_tokens for c in calls])
        input_tokens = _sum_or_none([c.prompt_tokens for c in calls])
        model_used = escalation.model if result.escalated else primary.model
        is_truncated = fragment_fixed or getattr(comp, "finish_reason", None) == "length"

        if key:
            with self.sessions.lock:
                cache = self.sessions.get(req.session_id)
                if key not in cache.entries and len(cache.entries) >= MAX_CACHE_ENTRIES_PER_SESSION:
                    cache.entries.pop(next(iter(cache.entries)))
                cache.store(key, mode, answer, output_tokens or 0)
                cache.entries[key].update(model=model_used, escalated=result.escalated,
                                          answer_score=result.score, truncated=is_truncated)

        meta.update({
            "cache_hit": False,
            "cache_bypass_reason": cache_bypass_reason,
            "model": model_used,
            "model_tier": "small" if use_small and primary.model == cfg.small_model else "primary",
            "primary_model": primary.model,
            "requested_model": cfg.model,
            # fallback_used keeps its old meaning: the answer came from
            # the fallback list, not from the primary and not from the
            # routed small model, both of which are intended choices.
            "fallback_used": primary.model != cfg.model and not (
                use_small and primary.model == cfg.small_model),
            # What the provider's response said answered (e.g. the model
            # openrouter/free routed to), or None if it did not say.
            "provider_model": getattr(comp, "response_model", None),
            "escalated": result.escalated,
            "escalation_reason": result.reason,
            "avg_logprob": result.avg_logprob,
            "answer_score": result.score,
            "score_reasons": list(result.score_reasons),
            "scorer_escalation": cfg.scorer_escalation,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "new_output_tokens": output_tokens,
            "tokens_source": "provider-reported" if output_tokens is not None
            else "not reported by provider",
            "energy": energy_receipt(
                input_tokens, output_tokens, model=model_used,
                model_tier="small" if use_small and primary.model == cfg.small_model else "primary",
                escalated=result.escalated,
                grid_g_per_kwh=cfg.grid_g_per_kwh),
            "trimmed": answer != raw_text,
            "truncated": is_truncated,
        })

        if req.measure_baseline:
            meta["baseline"] = self._baseline(
                req.prompt, input_tokens, output_tokens, primary.model)

        meta["latency_ms"] = round((time.monotonic() - started) * 1000)
        self.log(f"chat model={model_used} in={input_tokens} out={output_tokens} "
                 f"cache_hit=False escalated={result.escalated} "
                 f"expansion={is_expansion} "
                 f"task={task_route.task_type if task_route is not None else '-'} "
                 f"tier={'small' if use_small and primary.model == cfg.small_model else 'primary'} "
                 f"score={result.score} latency_ms={meta['latency_ms']}")
        return {"answer": answer, "meta": meta}

    def _baseline(self, original_prompt, input_tokens, output_tokens, model):
        """One extra, unconstrained call: original prompt, no system prompt,
        no token cap, same model that answered the primary call (a fallback
        if one was used). Doubles API cost; opt-in only. No fallback chain.
        A failure here never affects the answer already produced."""
        try:
            base = self._make(model).complete_ex(original_prompt)
        except (ProviderError, ValueError, ClientError):
            self.log("baseline call failed")
            return {"error": "Baseline call failed, savings were not measured"}
        out = {
            "model": model,
            "input_tokens": base.prompt_tokens,
            "output_tokens": base.completion_tokens,
            "saved_output_tokens": None,
            "saved_total_tokens": None,
            "note": "measured: unconstrained call vs Arboris call, provider-reported tokens",
        }
        if base.completion_tokens is not None and output_tokens is not None:
            out["saved_output_tokens"] = base.completion_tokens - output_tokens
            if base.prompt_tokens is not None and input_tokens is not None:
                out["saved_total_tokens"] = (
                    base.prompt_tokens + base.completion_tokens
                    - input_tokens - output_tokens
                )
        else:
            out["note"] = "provider did not report token counts, savings not measured"
        return out


# ---------------------------------------------------------------------------
# Server-Sent Events
# ---------------------------------------------------------------------------

def sse_event(event, data):
    """Encode one SSE event. data is JSON-encoded on a single line."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def parse_sse(text):
    """Parse an SSE stream into a list of (event, data) with data JSON-decoded."""
    events = []
    for block in re.split(r"\r?\n\r?\n", text):
        name, data_lines = "message", []
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            field_name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field_name == "event":
                name = value
            elif field_name == "data":
                data_lines.append(value)
        if data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


def chunk_text(text, words_per_chunk=CHUNK_WORDS):
    """Split text into chunks that join back to exactly the original text."""
    pieces = re.findall(r"\s*\S+\s*", text) or ([text] if text else [])
    return [
        "".join(pieces[i:i + words_per_chunk])
        for i in range(0, len(pieces), words_per_chunk)
    ]


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def make_handler(backend):
    site_index = ROOT / "website" / "index.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "Arboris"
        sys_version = ""

        # -- helpers --------------------------------------------------------

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            # No Origin header: not a cross-site browser request (curl, tests).
            return origin is None or origin.rstrip("/") in backend.config.allowed_origins

        def _client_ip(self):
            # Behind a reverse proxy (Cloud Run's load balancer, nginx, ...)
            # the socket peer is the proxy, not the visitor: per-IP rate
            # limiting must read X-Forwarded-For. Take the leftmost entry
            # (the original client as seen by the first proxy). Caveat: a
            # client can spoof this header to dodge the limit, so this stays
            # a POC-grade guard, not commercial metering.
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
            return self.client_address[0]

        def _cors_headers(self):
            origin = self.headers.get("Origin")
            if origin and origin.rstrip("/") in backend.config.allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

        def _send_json(self, status, obj, extra=None):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status, message, extra=None):
            self._send_json(status, {"error": message}, extra)

        def log_request(self, code="-", size="-"):
            # Method, path and status only: never a body, never a prompt.
            backend.log(f"{self.command} {self.path.split('?')[0]} -> {code}")

        def log_message(self, fmt, *args):
            backend.log(fmt % args)

        # -- verbs ----------------------------------------------------------

        def do_OPTIONS(self):
            path = self.path.split("?")[0]
            if path not in ("/api/chat", "/api/health"):
                return self._send_error(404, MSG_NOT_FOUND)
            if not self._origin_ok():
                return self._send_error(403, MSG_ORIGIN)
            self.send_response(204)
            self._cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/health":
                if not self._origin_ok():
                    return self._send_error(403, MSG_ORIGIN)
                return self._send_json(200, backend.health())
            if path in ("/", "/index.html") and site_index.is_file():
                body = site_index.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_error(404, MSG_NOT_FOUND)

        def do_POST(self):
            path = self.path.split("?")[0]
            if path != "/api/chat":
                return self._send_error(404, MSG_NOT_FOUND)
            if not self._origin_ok():
                return self._send_error(403, MSG_ORIGIN)
            allowed, retry_after = backend.allow_request(self._client_ip())
            if not allowed:
                return self._send_error(429, MSG_RATE_LIMITED,
                                        {"Retry-After": str(retry_after)})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._send_error(400, "Request body is missing")
            if length <= 0:
                return self._send_error(400, "Request body is missing")
            if length > MAX_BODY_BYTES:
                return self._send_error(413, "Request body is too large")
            body = self.rfile.read(length)
            try:
                req = parse_chat_request(body)
                result = backend.chat(req)
            except ClientError as exc:
                return self._send_error(exc.status, exc.message)
            except Exception as exc:  # never a traceback to the client
                backend.log(f"unexpected error: {type(exc).__name__}")
                return self._send_error(500, MSG_INTERNAL)

            meta = result["meta"]
            try:
                if req.stream:
                    meta["delivery"] = "sse"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Accel-Buffering", "no")
                    self._cors_headers()
                    self.end_headers()
                    for piece in chunk_text(result["answer"]):
                        self.wfile.write(sse_event("chunk", {"text": piece}))
                        self.wfile.flush()
                    self.wfile.write(sse_event("meta", meta))
                    self.wfile.write(sse_event("done", {}))
                    self.wfile.flush()
                else:
                    meta["delivery"] = "json"
                    self._send_json(200, result)
            except (BrokenPipeError, ConnectionResetError):
                backend.log("client disconnected mid-response")

    return Handler


def make_server(backend, host="127.0.0.1", port=DEFAULT_PORT):
    server = ThreadingHTTPServer((host, port), make_handler(backend))
    server.daemon_threads = True
    return server


def _help_epilog():
    width = max(len(name) for name, _, _ in ENV_VARS)
    lines = ["environment variables (all optional; secrets are read ONLY from here):"]
    for name, default, desc in ENV_VARS:
        lines.append(f"  {name.ljust(width)}  {desc}")
        lines.append(f"  {' ' * width}  default: {default}")
    lines += [
        "",
        "endpoints: POST /api/chat (SSE, or JSON with \"stream\": false),",
        "           GET /api/health, GET / (serves website/index.html).",
        "The API key is never accepted from clients and never returned.",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python server/app.py",
        description="Arboris live backend (local; stdlib only).",
        epilog=_help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1, local only)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"listen port (default $PORT or {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
        port = args.port if args.port is not None else int(os.environ.get("PORT", DEFAULT_PORT))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    backend = Backend(config)
    server = make_server(backend, args.host, port)
    if config.legacy_env_names:
        backend.log(
            "legacy environment names in use (rename to ARBORIS_*; support "
            "ends after 0.9.0): " + ", ".join(config.legacy_env_names))
    backend.log(
        f"listening on http://{args.host}:{server.server_address[1]} | "
        f"live: {'configured' if config.configured else 'NOT configured (no API key)'} | "
        f"model: {config.model} | "
        f"fallbacks: {', '.join(config.fallback_models) or 'none'} | "
        f"escalation: {config.escalation_model if config.cascade_enabled else 'off (single model)'} | "
        f"CORS: {', '.join(config.allowed_origins)} | "
        f"rate limit: {config.rate_limit_per_hour}/h, {config.rate_limit_per_day}/day per IP, "
        f"{config.global_limit_per_day}/day global | "
        f"paid models: {'ALLOWED' if config.allow_paid_models else 'blocked'} | "
        f"small routing: {'on (' + config.small_model + ')' if config.small_model_routing else 'off'} | "
        f"task routing: {'on' if config.task_routing else 'off'} | "
        f"difficulty routing: {'on (threshold ' + str(config.router_threshold_effective) + ')' if config.difficulty_routing else 'off'} | "
        f"scorer escalation: {'on' if config.scorer_escalation else 'off (report only)'} | "
        f"max_tokens multiplier: {config.max_tokens_multiplier} | "
        f"task temperature: {'on' if config.task_temperature else 'off'} | "
        f"task stop sequences: {'on' if config.task_stop_sequences else 'off'} | "
        f"long-input pruning: {'on' if config.long_input_pruning else 'off'} | "
        f"semantic cache: {'on' if config.semantic_cache else 'off'} | "
        f"shrink: {config.shrink_version} | "
        f"prompt logging: {'ON (stderr only)' if config.debug_log else 'off'}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
