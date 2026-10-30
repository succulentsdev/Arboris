"""Tests for the Phase 2 live backend (server/app.py).

No API key and no network to any provider: every model call goes to a
FakeProvider. HTTP tests bind a throwaway server on 127.0.0.1 (loopback
only). Run from the repository root:
    python -m unittest discover -s tests -v
"""

import http.client
import io
import json
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arboris.brevity import ELLIPSIS, apply_brevity, apply_brevity_with_flag  # noqa: E402
from arboris.providers import BREVITY_SYSTEM, MAX_TOKENS, Completion, ProviderError  # noqa: E402
from arboris.router import route_mode  # noqa: E402
from arboris.shrink import shrink  # noqa: E402
from server.app import (  # noqa: E402
    DEFAULT_FALLBACKS,
    DEFAULT_MODEL,
    MSG_EMPTY,
    MSG_ESCALATION_FAILED,
    MSG_KEY_REJECTED,
    MSG_MODEL_NOT_FOUND,
    MSG_NO_CREDITS,
    MSG_NOT_CONFIGURED,
    MSG_ORIGIN,
    MSG_RATE_LIMITED,
    MSG_PROVIDER_RATE_LIMITED,
    MSG_UNREACHABLE,
    SINGLE_MODEL_REASON,
    Backend,
    ChatRequest,
    ClientError,
    Config,
    RateLimiter,
    chunk_text,
    make_server,
    parse_origins,
    parse_chat_request,
    parse_sse,
    sse_event,
)

# A sentinel placeholder, NOT a real key: tests assert it never leaks.
SENTINEL_KEY = "sk-TEST-SENTINEL-not-a-real-key"
SESSION = "test-session-0001"
LONG_ANSWER = (
    "DNS turns names into IP addresses. Your browser asks a resolver. "
    "The resolver asks root servers. Then it asks the TLD servers. "
    "Then the authoritative server answers. The result is cached. "
    "Caching makes later lookups fast. That is the whole trick."
)


class FakeProvider:
    """Records every call; returns a fixed completion or raises."""

    def __init__(self, model, text=LONG_ANSWER, avg_logprob=None,
                 completion_tokens=50, prompt_tokens=10, error=None,
                 logprobs_error=None, finish_reason=None,
                 reasoning_tokens=None):
        self.model = model
        self.text = text
        self.logprobs_error = logprobs_error  # raised only when logprobs=True
        self.finish_reason = finish_reason
        self.avg_logprob = avg_logprob
        self.completion_tokens = completion_tokens
        self.prompt_tokens = prompt_tokens
        self.error = error
        self.reasoning_tokens = reasoning_tokens
        self.calls = []

    def complete_ex(self, prompt, system=None, max_tokens=None, logprobs=False,
                    stop=None, temperature=None, reasoning_effort=None):
        self.calls.append({"prompt": prompt, "system": system,
                           "max_tokens": max_tokens, "logprobs": logprobs,
                           "stop": stop, "temperature": temperature,
                           "reasoning_effort": reasoning_effort})
        if self.error is not None:
            raise self.error
        if logprobs and self.logprobs_error is not None:
            raise self.logprobs_error
        return Completion(
            text=self.text,
            completion_tokens=self.completion_tokens,
            prompt_tokens=self.prompt_tokens,
            avg_logprob=self.avg_logprob if logprobs else None,
            finish_reason=self.finish_reason,
            reasoning_tokens=self.reasoning_tokens,
        )


PRIMARY = DEFAULT_MODEL
# Only used by the opt-in cascade tests; not a model choice for Phase 2.
ESCALATION = "example/escalation-model"


def make_backend(primary=None, big=None, baseline=None, **config_overrides):
    """A Backend whose models are fakes. Returns (backend, primary, big).

    Default config: single model (google/gemma-4-31b-it:free), no escalation model and
    no fallback models (these tests predate the fallback chain and assume
    one primary model; FallbackTests covers the chain), so big must never
    be built. Pass escalation_model=ESCALATION to enable
    the cascade. The primary model's first instance answers the pipeline;
    if baseline is given, later primary instances (the baseline call) use it.
    """
    config_overrides.setdefault("fallback_models", ())
    config = Config(api_key=SENTINEL_KEY, **config_overrides)
    primary = primary or FakeProvider(config.model, avg_logprob=-0.2)
    big = big or FakeProvider(config.escalation_model, text="Escalated answer. Second sentence.",
                              completion_tokens=30, prompt_tokens=12)
    made = {"primary": 0}

    def factory(model):
        if model == config.model:
            made["primary"] += 1
            # Each request builds one primary provider for the pipeline; a
            # second one in the same request is the baseline call.
            if baseline is not None and made["primary"] % 2 == 0:
                return baseline
            return primary
        if config.cascade_enabled and model == config.escalation_model:
            return big
        raise AssertionError(f"unexpected model {model}")

    backend = Backend(config, provider_factory=factory, log_stream=io.StringIO(), quiet=True)
    return backend, primary, big


def make_cascade_backend(primary=None, big=None, **config_overrides):
    """make_backend with a DIFFERENT escalation model, so the cascade runs."""
    return make_backend(primary=primary, big=big, escalation_model=ESCALATION,
                        **config_overrides)


class PipelineTests(unittest.TestCase):
    PROMPT = "Could you please explain DNS in order to understand it?"

    def test_shrink_route_brevity_pipeline(self):
        backend, small, big = make_backend()
        result = backend.chat(ChatRequest(prompt=self.PROMPT, session_id=SESSION))
        shrunk = shrink(self.PROMPT).text
        mode = route_mode(shrunk)
        self.assertEqual(len(small.calls), 1)
        call = small.calls[0]
        self.assertEqual(call["prompt"], shrunk)             # PromptShrink ran
        self.assertNotIn("please", call["prompt"].lower())
        self.assertEqual(call["system"], BREVITY_SYSTEM[mode])  # brevity system prompt
        self.assertEqual(call["max_tokens"], MAX_TOKENS[mode])  # token cap
        self.assertTrue(call["logprobs"])                    # logprobs requested
        self.assertEqual(big.calls, [])
        meta = result["meta"]
        self.assertEqual(meta["mode"], mode)
        self.assertEqual(meta["mode_source"], "auto")
        self.assertEqual(result["answer"], apply_brevity(LONG_ANSWER, mode))  # safety-net trim
        self.assertTrue(meta["trimmed"])
        self.assertEqual(meta["model"], PRIMARY)
        self.assertEqual(meta["primary_model"], PRIMARY)
        self.assertFalse(meta["escalated"])
        self.assertEqual(meta["escalation_reason"], SINGLE_MODEL_REASON)
        self.assertEqual(meta["avg_logprob"], -0.2)
        self.assertFalse(meta["cache_hit"])
        self.assertEqual((meta["input_tokens"], meta["output_tokens"]), (10, 50))
        self.assertEqual(meta["tokens_source"], "provider-reported")
        self.assertIsNone(meta["baseline"])
        self.assertFalse(meta["provider_streaming"])
        self.assertIsInstance(meta["latency_ms"], int)

    def test_explicit_mode_overrides_routing(self):
        backend, small, _ = make_backend()
        result = backend.chat(ChatRequest(prompt="Explain DNS", mode="full"))
        self.assertEqual(route_mode("Explain DNS"), "sprout")
        self.assertEqual(result["meta"]["mode"], "forest")
        self.assertEqual(result["meta"]["mode_source"], "explicit")
        self.assertEqual(small.calls[0]["max_tokens"], MAX_TOKENS["full"])

    def test_tokens_none_when_provider_does_not_report(self):
        small = FakeProvider(PRIMARY, avg_logprob=-0.1,
                             completion_tokens=None, prompt_tokens=None)
        backend, _, _ = make_backend(primary=small)
        meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
        self.assertIsNone(meta["output_tokens"])
        self.assertEqual(meta["tokens_source"], "not reported by provider")


class SingleModelTests(unittest.TestCase):
    """Default behaviour: one primary model, never an escalation call."""

    def test_low_logprob_does_not_escalate_without_escalation_model(self):
        small = FakeProvider(PRIMARY, avg_logprob=-5.0)
        built = []

        def factory(model):
            built.append(model)
            return small

        backend = Backend(Config(api_key=SENTINEL_KEY), provider_factory=factory,
                          log_stream=io.StringIO(), quiet=True)
        meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
        self.assertEqual(built, [PRIMARY])  # exactly one provider, one call
        self.assertEqual(len(small.calls), 1)
        self.assertTrue(small.calls[0]["logprobs"])
        self.assertFalse(meta["escalated"])
        self.assertEqual(meta["model"], PRIMARY)
        self.assertEqual(meta["avg_logprob"], -5.0)
        self.assertEqual(meta["escalation_reason"], SINGLE_MODEL_REASON)
        self.assertEqual((meta["input_tokens"], meta["output_tokens"]), (10, 50))

    def test_escalation_model_equal_to_primary_is_single_model(self):
        cfg = Config.from_env({"ARBORIS_API_KEY": "placeholder",
                               "ARBORIS_ESCALATION_MODEL": PRIMARY})
        self.assertFalse(cfg.cascade_enabled)
        small = FakeProvider(PRIMARY, avg_logprob=-5.0)
        backend, _, _ = make_backend(primary=small, escalation_model=PRIMARY)
        meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
        self.assertFalse(meta["escalated"])
        self.assertEqual(len(small.calls), 1)

    def test_primary_failure_is_plain_error_not_escalation_message(self):
        small = FakeProvider(PRIMARY, error=ProviderError("timed out"))
        backend, _, _ = make_backend(primary=small)
        with self.assertRaises(ClientError) as ctx:
            backend.chat(ChatRequest(prompt="Explain DNS"))
        self.assertEqual((ctx.exception.status, ctx.exception.message), (502, MSG_UNREACHABLE))


class CacheTests(unittest.TestCase):
    def test_cache_hit_returns_zero_new_tokens(self):
        backend, small, _ = make_backend()
        first = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        second = backend.chat(ChatRequest(prompt="explain dns!", session_id=SESSION))
        self.assertEqual(len(small.calls), 1)  # no second model call
        meta = second["meta"]
        self.assertTrue(meta["cache_hit"])
        self.assertEqual(meta["new_output_tokens"], 0)
        self.assertEqual(meta["output_tokens"], 0)
        self.assertEqual(meta["input_tokens"], 0)
        self.assertIsNone(meta["model"])
        self.assertEqual(meta["cached_from_model"], PRIMARY)
        self.assertEqual(second["answer"], first["answer"])

    def test_cache_is_per_session_and_mode_aware(self):
        backend, small, _ = make_backend()
        backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        backend.chat(ChatRequest(prompt="Explain DNS", session_id="other-session-02"))
        backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION, mode="full"))
        self.assertEqual(len(small.calls), 3)

    def test_no_session_id_never_caches(self):
        backend, small, _ = make_backend()
        backend.chat(ChatRequest(prompt="Explain DNS"))
        result = backend.chat(ChatRequest(prompt="Explain DNS"))
        self.assertEqual(len(small.calls), 2)
        self.assertFalse(result["meta"]["cache_hit"])

    def test_cache_hit_reports_original_escalation(self):
        small = FakeProvider(PRIMARY, avg_logprob=-3.0)
        backend, _, big = make_cascade_backend(primary=small)
        backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        meta = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))["meta"]
        self.assertTrue(meta["cache_hit"])
        self.assertFalse(meta["escalated"])  # no escalation happened on this request
        self.assertTrue(meta["cached_answer_was_escalated"])
        self.assertEqual(meta["cached_from_model"], ESCALATION)
        self.assertEqual(len(big.calls), 1)


class OptionalCascadeTests(unittest.TestCase):
    """Only when ARBORIS_ESCALATION_MODEL is a DIFFERENT model."""

    def test_escalates_when_logprob_below_threshold(self):
        small = FakeProvider(PRIMARY, avg_logprob=-2.0)
        backend, _, big = make_cascade_backend(primary=small)
        result = backend.chat(ChatRequest(prompt="Explain DNS"))
        meta = result["meta"]
        self.assertTrue(meta["escalated"])
        self.assertEqual(meta["model"], ESCALATION)
        self.assertEqual(meta["primary_model"], PRIMARY)
        self.assertEqual(result["answer"], "Escalated answer. Second sentence.")
        # Same system prompt and cap for the escalation call.
        self.assertEqual(big.calls[0]["system"], small.calls[0]["system"])
        self.assertEqual(big.calls[0]["max_tokens"], small.calls[0]["max_tokens"])
        self.assertEqual(big.calls[0]["prompt"], small.calls[0]["prompt"])
        # Both calls' tokens are counted: they were both generated.
        self.assertEqual(meta["output_tokens"], 50 + 30)
        self.assertEqual(meta["input_tokens"], 10 + 12)

    def test_no_escalation_at_or_above_threshold(self):
        for avg in (-0.5, -1.0):
            with self.subTest(avg=avg):
                small = FakeProvider(PRIMARY, avg_logprob=avg)
                backend, _, big = make_cascade_backend(primary=small)
                meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
                self.assertFalse(meta["escalated"])
                self.assertEqual(big.calls, [])

    def test_never_escalates_when_logprobs_missing(self):
        small = FakeProvider(PRIMARY, avg_logprob=None)
        backend, _, big = make_cascade_backend(primary=small)
        meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
        self.assertFalse(meta["escalated"])
        self.assertIsNone(meta["avg_logprob"])
        self.assertIn("logprobs unavailable", meta["escalation_reason"])
        self.assertEqual(big.calls, [])

    def test_threshold_comes_from_config(self):
        small = FakeProvider(PRIMARY, avg_logprob=-0.5)
        backend, _, big = make_cascade_backend(primary=small, cascade_threshold=-0.25)
        self.assertTrue(backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]["escalated"])
        self.assertEqual(len(big.calls), 1)

    def test_escalation_failure_does_not_silently_downgrade(self):
        small = FakeProvider(PRIMARY, text="LOW-CONFIDENCE ANSWER.", avg_logprob=-3.0)
        big = FakeProvider(ESCALATION, error=ProviderError(
            "Could not reach live provider at https://openrouter.ai/api/v1: timed out"))
        backend, _, _ = make_cascade_backend(primary=small, big=big)
        with self.assertRaises(ClientError) as ctx:
            backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.message, MSG_ESCALATION_FAILED)
        self.assertNotIn("LOW-CONFIDENCE", ctx.exception.message)
        # Nothing was cached either: a retry calls the models again.
        with self.assertRaises(ClientError):
            backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        self.assertEqual(len(small.calls), 2)


class BaselineTests(unittest.TestCase):
    def test_baseline_off_by_default(self):
        backend, small, _ = make_backend()
        backend.chat(ChatRequest(prompt="Explain DNS"))
        self.assertEqual(len(small.calls), 1)

    def test_measure_baseline_makes_one_unconstrained_call(self):
        baseline = FakeProvider(PRIMARY, completion_tokens=200, prompt_tokens=14)
        backend, small, _ = make_backend(baseline=baseline)
        prompt = "Could you please explain DNS?"
        meta = backend.chat(ChatRequest(prompt=prompt, measure_baseline=True))["meta"]
        self.assertEqual(len(baseline.calls), 1)
        call = baseline.calls[0]
        self.assertEqual(call["prompt"], prompt)  # original, unshrunk
        self.assertIsNone(call["system"])
        self.assertIsNone(call["max_tokens"])
        self.assertEqual(meta["baseline"]["saved_output_tokens"], 200 - 50)
        self.assertEqual(meta["baseline"]["saved_total_tokens"], (200 + 14) - (50 + 10))
        self.assertEqual(meta["baseline"]["model"], PRIMARY)  # primary model, not a smaller one

    def test_baseline_failure_keeps_answer(self):
        baseline = FakeProvider(PRIMARY, error=ProviderError("HTTP 500: boom"))
        backend, _, _ = make_backend(baseline=baseline)
        result = backend.chat(ChatRequest(prompt="Explain DNS", measure_baseline=True))
        self.assertTrue(result["answer"])
        self.assertIn("error", result["meta"]["baseline"])
        self.assertNotIn("boom", json.dumps(result))


class ErrorMappingTests(unittest.TestCase):
    def assert_clean(self, text):
        for leak in (SENTINEL_KEY, "Traceback", "openrouter", "Errno", "Bearer", "HTTP 401"):
            self.assertNotIn(leak, text)

    def test_no_key_is_503_plain(self):
        backend = Backend(Config(api_key=""), log_stream=io.StringIO(), quiet=True)
        with self.assertRaises(ClientError) as ctx:
            backend.chat(ChatRequest(prompt="Explain DNS"))
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.message, MSG_NOT_CONFIGURED)

    def test_provider_errors_map_to_plain_messages(self):
        cases = [
            (ProviderError("Could not reach live provider at https://openrouter.ai/api/v1: "
                           "[Errno -2] Name or service not known"), 502, MSG_UNREACHABLE),
            (ProviderError(f"Live provider returned HTTP 401: Invalid key {SENTINEL_KEY}"),
             401, MSG_KEY_REJECTED),
            (ProviderError("Live provider returned HTTP 402: Insufficient credits"),
             402, MSG_NO_CREDITS),
            (ProviderError("Live provider returned HTTP 404: No endpoints found for "
                           "qwen/qwen3-8b"), 404, MSG_MODEL_NOT_FOUND),
            (ProviderError("upstream said no", status=404), 404, MSG_MODEL_NOT_FOUND),
            (ProviderError("Live provider returned HTTP 503: overloaded"), 502, MSG_UNREACHABLE),
            (ProviderError("Live provider returned HTTP 500: boom"), 502, MSG_UNREACHABLE),
            (ProviderError("Live provider timed out after 60s."), 502, MSG_UNREACHABLE),
            (ProviderError("Live provider returned HTTP 429: slow down"), 429, MSG_PROVIDER_RATE_LIMITED),
        ]
        for error, status, message in cases:
            with self.subTest(error=str(error)):
                small = FakeProvider(PRIMARY, error=error)
                backend, _, _ = make_backend(primary=small)
                with self.assertRaises(ClientError) as ctx:
                    backend.chat(ChatRequest(prompt="Explain DNS"))
                self.assertEqual((ctx.exception.status, ctx.exception.message), (status, message))
                self.assert_clean(ctx.exception.message)

    def test_invalid_requests_are_400(self):
        bad = [
            b"not json",
            b"[1, 2]",
            b'{"prompt": ""}',
            b'{"prompt": "   "}',
            b'{"prompt": 5}',
            b'{"prompt": "hi", "mode": "huge"}',
            b'{"prompt": "hi", "session_id": "x"}',
            b'{"prompt": "hi", "stream": "yes"}',
            b'{"prompt": "hi", "api_key": "anything"}',
            b'{"prompt": "hi", "model": "gpt-4o"}',
            json.dumps({"prompt": "x" * 4001}).encode(),
        ]
        for body in bad:
            with self.subTest(body=body[:40]):
                with self.assertRaises(ClientError) as ctx:
                    parse_chat_request(body)
                self.assertEqual(ctx.exception.status, 400)

    def test_forest_alias_and_defaults(self):
        req = parse_chat_request(b'{"prompt": "hi", "mode": "forest"}')
        self.assertEqual(req.mode, "full")
        self.assertTrue(req.stream)
        self.assertFalse(req.measure_baseline)

    def test_config_rejects_wildcard_origin_and_bad_values(self):
        for env in ({"ARBORIS_BACKEND_ORIGIN": "*"},
                    {"ARBORIS_BACKEND_ORIGIN": "http://localhost:8000,*"},
                    {"ARBORIS_CASCADE_THRESHOLD": "0.5"},
                    {"ARBORIS_CASCADE_THRESHOLD": "abc"},
                    {"ARBORIS_RATE_LIMIT_PER_HOUR": "-1"}):
            with self.subTest(env=env):
                with self.assertRaises(ValueError):
                    Config.from_env(env)

    def test_origin_list_splits_on_commas_and_whitespace(self):
        want = ("https://a.example", "https://b.example")
        cases = {
            "comma only": "https://a.example,https://b.example",
            "space only": "https://a.example https://b.example",
            "mixed comma and space": " https://a.example ,\n\thttps://b.example, ",
            "trailing slashes": "https://a.example/, https://b.example//",
            "repeats": "https://a.example https://a.example/ https://b.example",
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.assertEqual(parse_origins(raw), want)
                cfg = Config.from_env({"ARBORIS_BACKEND_ORIGIN": raw})
                self.assertEqual(cfg.allowed_origins, want)
        # Separators only = unset = the localhost defaults.
        for raw in ("", " , ,", " / "):
            with self.subTest(raw=raw):
                self.assertEqual(Config.from_env({"ARBORIS_BACKEND_ORIGIN": raw})
                                 .allowed_origins, ("http://localhost:8000", "http://127.0.0.1:8000"))
        # A wildcard is still refused, whatever the separator.
        for raw in ("https://a.example *", "https://a.example,\t*"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    Config.from_env({"ARBORIS_BACKEND_ORIGIN": raw})

    def test_config_defaults(self):
        cfg = Config.from_env({})
        self.assertFalse(cfg.configured)
        self.assertEqual(cfg.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(cfg.model, "openrouter/free")
        self.assertEqual(cfg.escalation_model, "openrouter/free")
        self.assertEqual(cfg.fallback_models, DEFAULT_FALLBACKS)
        # The free router is only the last resort, and the primary is not repeated.
        self.assertEqual(DEFAULT_FALLBACKS, (
            "google/gemma-4-31b-it:free",))
        self.assertNotIn(DEFAULT_MODEL, DEFAULT_FALLBACKS)
        self.assertFalse(cfg.cascade_enabled)
        self.assertFalse(hasattr(cfg, "model_4b"))
        self.assertEqual(cfg.cascade_threshold, -1.0)
        self.assertEqual(cfg.rate_limit_per_hour, 30)
        self.assertEqual(cfg.rate_limit_per_day, 100)
        self.assertEqual(cfg.global_limit_per_day, 800)
        self.assertFalse(cfg.allow_paid_models)
        self.assertEqual(cfg.allowed_origins, ("http://localhost:8000", "http://127.0.0.1:8000"))
        self.assertFalse(cfg.debug_log)
        cfg = Config.from_env({"ARBORIS_MODEL": "other/model:free"})
        self.assertEqual((cfg.model, cfg.escalation_model), ("other/model:free", "other/model:free"))
        self.assertFalse(cfg.cascade_enabled)
        cfg = Config.from_env({"ARBORIS_ESCALATION_MODEL": "big/model:free"})
        self.assertEqual((cfg.model, cfg.escalation_model), (DEFAULT_MODEL, "big/model:free"))
        self.assertTrue(cfg.cascade_enabled)
        # The old 4B/8B variables are no longer read.
        cfg = Config.from_env({"ARBORIS_MODEL_4B": "qwen/qwen3-4b",
                               "ARBORIS_MODEL_8B": "x/y"})
        self.assertEqual((cfg.model, cfg.escalation_model), (DEFAULT_MODEL, DEFAULT_MODEL))

    def test_prompts_not_logged_by_default(self):
        log = io.StringIO()
        backend, _, _ = make_backend()
        backend.log_stream, backend.quiet = log, False
        backend.chat(ChatRequest(prompt="SECRET-PROMPT-TEXT about DNS"))
        self.assertNotIn("SECRET-PROMPT-TEXT", log.getvalue())
        backend.config.debug_log = True
        result = backend.chat(ChatRequest(prompt="SECRET-PROMPT-TEXT about DNS"))
        self.assertIn("SECRET-PROMPT-TEXT", log.getvalue())  # stderr-style stream only
        self.assertNotIn("SECRET-PROMPT-TEXT", json.dumps(result["meta"]))


class RateLimiterTests(unittest.TestCase):
    def test_triggers_and_window_expires(self):
        now = [1000.0]
        limiter = RateLimiter(3, window_seconds=3600, clock=lambda: now[0])
        self.assertEqual([limiter.allow("1.2.3.4") for _ in range(4)], [True, True, True, False])
        self.assertTrue(limiter.allow("5.6.7.8"))  # per client
        now[0] += 3600
        self.assertTrue(limiter.allow("1.2.3.4"))

    def test_zero_disables(self):
        limiter = RateLimiter(0)
        self.assertTrue(all(limiter.allow("ip") for _ in range(500)))


class SSEFormatTests(unittest.TestCase):
    def test_build_and_parse_sample_stream(self):
        answer = "DNS is the phone book.\nIt maps names to numbers — \"quotes\" too."
        chunks = chunk_text(answer)
        self.assertEqual("".join(chunks), answer)
        stream = b"".join(
            [sse_event("chunk", {"text": c}) for c in chunks]
            + [sse_event("meta", {"mode": "sprout", "escalated": False}), sse_event("done", {})]
        ).decode("utf-8")
        # Wire format: every event is "event: X\ndata: <one line of JSON>\n\n".
        blocks = stream.split("\n\n")
        self.assertEqual(blocks[-1], "")
        for block in blocks[:-1]:
            self.assertRegex(block, r"\Aevent: (chunk|meta|done)\ndata: \{.*\}\Z")
        events = parse_sse(stream)
        self.assertEqual([e for e, _ in events], ["chunk"] * len(chunks) + ["meta", "done"])
        self.assertEqual("".join(d["text"] for e, d in events if e == "chunk"), answer)
        self.assertEqual(events[-2][1], {"mode": "sprout", "escalated": False})

    def test_parse_ignores_comments_and_crlf(self):
        text = ': keep-alive\r\n\r\nevent: chunk\r\ndata: {"text":"a"}\r\n\r\n'
        self.assertEqual(parse_sse(text), [("chunk", {"text": "a"})])


class HTTPTests(unittest.TestCase):
    """Real HTTP over loopback against a server with fake providers."""

    def start(self, backend):
        server = make_server(backend, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        payload = json.dumps(body).encode() if isinstance(body, dict) else body
        hdrs = {"Content-Type": "application/json"} if payload is not None else {}
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        return resp, data

    def test_sse_stream_end_to_end(self):
        backend, small, _ = make_backend()
        port = self.start(backend)
        resp, data = self.request(port, "POST", "/api/chat",
                                  {"prompt": "Explain DNS", "session_id": SESSION},
                                  {"Origin": "http://localhost:8000"})
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/event-stream"))
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "http://localhost:8000")
        events = parse_sse(data)
        names = [e for e, _ in events]
        self.assertGreater(names.count("chunk"), 1)
        self.assertEqual(names[-2:], ["meta", "done"])
        text = "".join(d["text"] for e, d in events if e == "chunk")
        self.assertEqual(text, apply_brevity(LONG_ANSWER, "sprout"))
        meta = events[-2][1]
        for k in ("mode", "model", "escalated", "cache_hit", "input_tokens",
                  "output_tokens", "latency_ms"):
            self.assertIn(k, meta)
        self.assertEqual(meta["delivery"], "sse")
        self.assertFalse(meta["provider_streaming"])
        self.assertNotIn(SENTINEL_KEY, data)

    def test_json_fallback(self):
        backend, _, _ = make_backend()
        port = self.start(backend)
        resp, data = self.request(port, "POST", "/api/chat", {"prompt": "Explain DNS", "stream": False})
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("application/json"))
        obj = json.loads(data)
        self.assertEqual(obj["meta"]["delivery"], "json")
        self.assertTrue(obj["answer"])

    def test_cors_rejects_unlisted_origin(self):
        backend, small, _ = make_backend()
        port = self.start(backend)
        evil = {"Origin": "https://evil.example"}
        resp, data = self.request(port, "OPTIONS", "/api/chat", None, evil)
        self.assertEqual(resp.status, 403)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        resp, data = self.request(port, "POST", "/api/chat", {"prompt": "Explain DNS"}, evil)
        self.assertEqual(resp.status, 403)
        self.assertEqual(json.loads(data), {"error": MSG_ORIGIN})
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(small.calls, [])  # rejected before any model call

    def test_cors_allows_listed_origin_never_wildcard(self):
        backend, _, _ = make_backend(allowed_origins=("https://app.example",))
        port = self.start(backend)
        resp, _ = self.request(port, "OPTIONS", "/api/chat", None, {
            "Origin": "https://app.example", "Access-Control-Request-Method": "POST"})
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "https://app.example")
        self.assertIn("Content-Type", resp.getheader("Access-Control-Allow-Headers"))
        resp, _ = self.request(port, "OPTIONS", "/api/chat", None, {"Origin": "http://localhost:8000"})
        self.assertEqual(resp.status, 403)

    def test_cors_space_separated_env_list_matches_exactly(self):
        cfg = Config.from_env({"ARBORIS_API_KEY": SENTINEL_KEY,
                               "ARBORIS_BACKEND_ORIGIN": "https://a.example/ https://b.example"})
        backend, _, _ = make_backend(allowed_origins=cfg.allowed_origins)
        port = self.start(backend)
        for origin in ("https://a.example", "https://b.example"):
            with self.subTest(origin=origin):
                resp, _ = self.request(port, "OPTIONS", "/api/chat", None, {"Origin": origin})
                self.assertEqual(resp.status, 204)
                self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), origin)
        # Exact match only: no prefix, suffix or joined-string matches.
        for origin in ("https://a.example.evil", "https://evil.a.example",
                       "https://a.example https://b.example"):
            with self.subTest(origin=origin):
                resp, _ = self.request(port, "OPTIONS", "/api/chat", None, {"Origin": origin})
                self.assertEqual(resp.status, 403)
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))

    def test_rate_limit_triggers_over_http(self):
        backend, small, _ = make_backend(rate_limit_per_hour=2)
        port = self.start(backend)
        statuses = []
        for _ in range(3):
            resp, data = self.request(port, "POST", "/api/chat", {"prompt": "Explain DNS"})
            statuses.append(resp.status)
        self.assertEqual(statuses, [200, 200, 429])
        self.assertEqual(json.loads(data), {"error": MSG_RATE_LIMITED})
        self.assertEqual(resp.getheader("Retry-After"), "3600")
        self.assertEqual(len(small.calls), 2)

    def test_paid_model_blocked_without_explicit_allow(self):
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_MODEL": "other/model"})
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_MODEL_FALLBACKS": "other/model"})
        cfg = Config.from_env({"ARBORIS_MODEL": "other/model",
                               "ARBORIS_ALLOW_PAID_MODELS": "1"})
        self.assertTrue(cfg.allow_paid_models)
        self.assertEqual(cfg.model, "other/model")

    def test_daily_per_ip_limit_triggers_over_http(self):
        backend, small, _ = make_backend(rate_limit_per_hour=0, rate_limit_per_day=2)
        port = self.start(backend)
        statuses = []
        for _ in range(3):
            resp, _ = self.request(port, "POST", "/api/chat", {"prompt": "Explain DNS"})
            statuses.append(resp.status)
        self.assertEqual(statuses, [200, 200, 429])
        self.assertEqual(resp.getheader("Retry-After"), "86400")
        self.assertEqual(len(small.calls), 2)

    def test_global_daily_limit_triggers_over_http(self):
        backend, small, _ = make_backend(rate_limit_per_hour=0, rate_limit_per_day=0,
                                         global_limit_per_day=2)
        port = self.start(backend)
        statuses = []
        for _ in range(3):
            resp, _ = self.request(port, "POST", "/api/chat", {"prompt": "Explain DNS"})
            statuses.append(resp.status)
        self.assertEqual(statuses, [200, 200, 429])
        self.assertEqual(resp.getheader("Retry-After"), "86400")
        self.assertEqual(len(small.calls), 2)

    def test_no_key_over_http_and_health(self):
        backend = Backend(Config(api_key=""), log_stream=io.StringIO(), quiet=True)
        port = self.start(backend)
        resp, data = self.request(port, "POST", "/api/chat", {"prompt": "Explain DNS"})
        self.assertEqual(resp.status, 503)
        self.assertEqual(json.loads(data), {"error": MSG_NOT_CONFIGURED})
        resp, data = self.request(port, "GET", "/api/health")
        self.assertEqual(resp.status, 200)
        health = json.loads(data)
        self.assertFalse(health["configured"])
        self.assertEqual(health["model"], DEFAULT_MODEL)
        self.assertEqual(health["escalation_model"], DEFAULT_MODEL)
        self.assertEqual(health["fallbacks"], list(DEFAULT_FALLBACKS))
        self.assertFalse(health["cascade_enabled"])
        self.assertNotIn("model_4b", health)
        self.assertNotIn("model_8b", health)

    def test_error_bodies_never_contain_key_or_detail(self):
        small = FakeProvider(PRIMARY, error=ProviderError(
            f"Live provider returned HTTP 401: bad key {SENTINEL_KEY} at https://openrouter.ai"))
        backend, _, _ = make_backend(primary=small)
        port = self.start(backend)
        resp, data = self.request(port, "POST", "/api/chat", {"prompt": "Explain DNS"})
        self.assertEqual(resp.status, 401)
        self.assertEqual(json.loads(data), {"error": MSG_KEY_REJECTED})
        resp, health = self.request(port, "GET", "/api/health")
        for body in (data, health):
            self.assertNotIn(SENTINEL_KEY, body)
            self.assertNotIn("Traceback", body)
        self.assertTrue(json.loads(health)["configured"])

    def test_unexpected_exception_is_plain_500(self):
        def boom(model):
            raise RuntimeError("internal detail /home/secret")
        backend = Backend(Config(api_key=SENTINEL_KEY), provider_factory=boom,
                          log_stream=io.StringIO(), quiet=True)
        port = self.start(backend)
        resp, data = self.request(port, "POST", "/api/chat", {"prompt": "Explain DNS"})
        self.assertEqual(resp.status, 500)
        self.assertNotIn("internal detail", data)
        self.assertNotIn("Traceback", data)

    def test_bad_body_and_unknown_path(self):
        backend, _, _ = make_backend()
        port = self.start(backend)
        resp, data = self.request(port, "POST", "/api/chat", b"{nope")
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(data), {"error": "Request body must be valid JSON"})
        resp, _ = self.request(port, "POST", "/api/other", {"prompt": "x"})
        self.assertEqual(resp.status, 404)

    def test_serves_index_and_hero_image(self):
        backend, _, _ = make_backend()
        port = self.start(backend)
        resp, html = self.request(port, "GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type"))
        self.assertIn("Arboris", html)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/hero.jpg")
        img_resp = conn.getresponse()
        img_bytes = img_resp.read()
        conn.close()
        self.assertEqual(img_resp.status, 200)
        self.assertEqual(img_resp.getheader("Content-Type"), "image/jpeg")
        self.assertIn("max-age=86400", img_resp.getheader("Cache-Control") or "")
        self.assertTrue(img_bytes.startswith(b"\xff\xd8\xff"))


class EntryPointAndSiteTests(unittest.TestCase):
    def test_help_lists_env_vars(self):
        out = subprocess.run([sys.executable, str(ROOT / "server" / "app.py"), "--help"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0)
        for name in ("ARBORIS_API_KEY", "ARBORIS_BASE_URL", "ARBORIS_MODEL",
                     "ARBORIS_ESCALATION_MODEL", "ARBORIS_MODEL_FALLBACKS",
                     "ARBORIS_CASCADE_THRESHOLD", "ARBORIS_BACKEND_ORIGIN",
                     "ARBORIS_RATE_LIMIT_PER_HOUR", "ARBORIS_DEBUG_LOG", "PORT"):
            self.assertIn(name, out.stdout)
        self.assertIn("google/gemma-4-31b-it:free", out.stdout)
        self.assertIn(DEFAULT_FALLBACKS[0], out.stdout)
        self.assertNotIn("MODEL_4B", out.stdout)
        self.assertNotIn("qwen3-4b", out.stdout)

    def test_website_live_only(self):
        # Live-only site: no mode toggle, no demo store, every prompt goes
        # to the backend. The baseline checkbox stays, unchecked by default.
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="live-toggle"', html)
        self.assertNotIn('id="demo-pill"', html)
        self.assertNotIn("mode-toggle", html)
        self.assertNotIn("STORE_KEY='arboris-demo-v1'", html)
        self.assertNotIn("EXPANDED_TOPICS", html)
        baseline = re.search(r'<input[^>]*id="live-baseline"[^>]*>', html)
        self.assertIsNotNone(baseline)
        self.assertNotIn("checked", baseline.group(0))
        self.assertIn("arboris-live-backend-url", html)
        self.assertIn("http://localhost:8000/api/chat", html)
        self.assertIn("All prompts go to this backend", html)
        # The page must never handle a provider key.
        self.assertNotIn("Authorization", html)
        self.assertNotRegex(html, r"api_key|apiKey|ARBORIS_API_KEY")
        # Hero image is extracted to website/hero.jpg (no 900KB base64 JPEG in index.html).
        self.assertNotIn("data:image/jpeg;base64,", html)
        self.assertIn("url('hero.jpg')", html)
        self.assertTrue((ROOT / "website" / "hero.jpg").is_file())
        # Brevity selector bar, methodology section, and valid head structure.
        self.assertIn('id="brevity-bar"', html)
        self.assertIn('id="methodology"', html)
        self.assertEqual(html.lower().count("<head>"), 1)
        self.assertEqual(html.lower().count("<body>"), 1)


    def test_env_example_has_no_real_key(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        match = re.search(r"^ARBORIS_API_KEY=(.*)$", text, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertNotRegex(match.group(1), r"sk-[A-Za-z0-9]{10,}")
        self.assertIn(match.group(1).strip(), ("", "replace-me"))
        self.assertRegex(text, r"(?m)^ARBORIS_MODEL=" + re.escape(DEFAULT_MODEL) + "$")
        self.assertRegex(text, r"(?m)^ARBORIS_MODEL_FALLBACKS=" + re.escape(
            ",".join(DEFAULT_FALLBACKS)) + "$")
        self.assertNotIn("MODEL_4B", text)


def chain_backend(providers, **config_overrides):
    """A Backend whose models are the given {model: FakeProvider} fakes.

    The first key is the primary model, the rest are the fallbacks in
    order. Returns (backend, built, log): built lists every model the
    backend constructed a provider for, log is the server log stream.
    """
    models = list(providers)
    config_overrides.setdefault("model", models[0])
    config_overrides.setdefault("fallback_models", tuple(models[1:]))
    config = Config(api_key=SENTINEL_KEY, **config_overrides)
    built = []

    def factory(model):
        built.append(model)
        if model not in providers:
            raise AssertionError(f"unexpected model {model}")
        return providers[model]

    log = io.StringIO()
    backend = Backend(config, provider_factory=factory, log_stream=log, quiet=False)
    return backend, built, log


def http_error(code, detail="detail"):
    return ProviderError(f"Live provider returned HTTP {code}: {detail}", status=code)


FREE_A = "liquid/lfm-2.5-2.6b:free"
FREE_B = "thinkingmachines/inkling-small:free"
PAID = "example/paid-model"


class FallbackTests(unittest.TestCase):
    def test_default_chat_chain_reaches_router_without_agent_only_rejection(self):
        built = []
        def factory(model):
            built.append(model)
            if model == "thinkingmachines/inkling-small:free":
                return FakeProvider(model, error=http_error(403, "only available on agentic harnesses"))
            if model == "openrouter/free":
                return FakeProvider(model, text="A seed takes root. A new leaf opens.")
            return FakeProvider(model, error=http_error(429))
        backend = Backend(Config(api_key=SENTINEL_KEY), provider_factory=factory,
                          log_stream=io.StringIO(), quiet=True)
        result = backend.chat(ChatRequest(prompt="Write a poem about a seed"))
        self.assertEqual(result["meta"]["model"], "openrouter/free")
        self.assertTrue(result["answer"])
        self.assertNotIn("thinkingmachines/inkling-small:free", built)

    def test_provider_throttling_does_not_blame_the_users_app_request_limit(self):
        dead = FakeProvider(PRIMARY, error=http_error(429))
        backend, _, _ = chain_backend({PRIMARY: dead})
        with self.assertRaises(ClientError) as ctx:
            backend.chat(ChatRequest(prompt="Write a poem about a seed"))
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("provider", ctx.exception.message.lower())
        self.assertNotEqual(ctx.exception.message, MSG_RATE_LIMITED)

    def test_fallback_answers_after_primary_404(self):
        dead = FakeProvider("qwen/qwen3-8b", error=http_error(404, "deprecated"))
        good = FakeProvider(FREE_A, text="Fallback answer. Second sentence.", avg_logprob=-0.3)
        backend, built, log = chain_backend({"qwen/qwen3-8b": dead, FREE_A: good})
        result = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        meta = result["meta"]
        self.assertEqual(result["answer"], "Fallback answer. Second sentence.")
        self.assertEqual(meta["model"], FREE_A)          # the model that answered
        self.assertEqual(meta["primary_model"], FREE_A)
        self.assertEqual(meta["requested_model"], "qwen/qwen3-8b")
        self.assertTrue(meta["fallback_used"])
        self.assertEqual(built, ["qwen/qwen3-8b", FREE_A])
        self.assertEqual(len(good.calls), 1)
        self.assertTrue(good.calls[0]["logprobs"])
        text = log.getvalue()
        self.assertIn("primary call failed: HTTP 404 model=qwen/qwen3-8b", text)
        self.assertIn(f"answered by fallback model={FREE_A}", text)
        self.assertNotIn("deprecated", text)              # no provider detail
        self.assertNotIn("Explain DNS", text)              # no prompt
        self.assertNotIn(SENTINEL_KEY, text)
        # The cache remembers the answering model.
        again = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))["meta"]
        self.assertTrue(again["cache_hit"])
        self.assertEqual(again["cached_from_model"], FREE_A)
        self.assertFalse(again["truncated"])

    def test_retries_each_fallback_code_and_stops_at_first_success(self):
        for code in (429, 404, 502, 503):
            with self.subTest(code=code):
                first = FakeProvider(PRIMARY, error=http_error(code))
                second = FakeProvider(FREE_A, error=http_error(429))
                third = FakeProvider(FREE_B, text="Third model. Done.")
                unused = FakeProvider("unused/model:free")
                backend, built, log = chain_backend(
                    {PRIMARY: first, FREE_A: second, FREE_B: third,
                     "unused/model:free": unused})
                meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
                self.assertEqual(meta["model"], FREE_B)
                self.assertEqual(built, [PRIMARY, FREE_A, FREE_B])
                self.assertEqual(unused.calls, [])
                self.assertIn(f"fallback call failed: HTTP 429 model={FREE_A}", log.getvalue())

    def test_401_does_not_fall_back(self):
        bad_key = FakeProvider(PRIMARY, error=http_error(401, f"bad key {SENTINEL_KEY}"))
        fallback = FakeProvider(FREE_A)
        backend, built, log = chain_backend({PRIMARY: bad_key, FREE_A: fallback})
        with self.assertRaises(ClientError) as ctx:
            backend.chat(ChatRequest(prompt="Explain DNS"))
        self.assertEqual((ctx.exception.status, ctx.exception.message), (401, MSG_KEY_REJECTED))
        self.assertIn("key", ctx.exception.message.lower())
        self.assertEqual(built, [PRIMARY])
        self.assertEqual(fallback.calls, [])
        self.assertNotIn(SENTINEL_KEY, log.getvalue())

    def test_unknown_failure_does_not_fall_back(self):
        timeout = FakeProvider(PRIMARY, error=ProviderError("Live provider timed out after 60s."))
        fallback = FakeProvider(FREE_A)
        backend, built, log = chain_backend({PRIMARY: timeout, FREE_A: fallback})
        with self.assertRaises(ClientError) as ctx:
            backend.chat(ChatRequest(prompt="Explain DNS"))
        self.assertEqual((ctx.exception.status, ctx.exception.message), (502, MSG_UNREACHABLE))
        self.assertEqual(fallback.calls, [])
        self.assertIn(f"primary call failed: no HTTP status model={PRIMARY}", log.getvalue())

    def test_402_falls_back_to_free_model_only(self):
        broke = FakeProvider(PAID, error=http_error(402, "Insufficient credits"))
        free = FakeProvider(FREE_A, text="Free answer.")
        backend, built, _ = chain_backend({PAID: broke, FREE_A: free})
        self.assertEqual(backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]["model"], FREE_A)

        broke = FakeProvider(PAID, error=http_error(402))
        other_paid = FakeProvider("example/other-paid")
        backend, built, _ = chain_backend({PAID: broke, "example/other-paid": other_paid})
        with self.assertRaises(ClientError) as ctx:
            backend.chat(ChatRequest(prompt="Explain DNS"))
        self.assertEqual((ctx.exception.status, ctx.exception.message), (402, MSG_NO_CREDITS))
        self.assertEqual(other_paid.calls, [])
        self.assertEqual(built, [PAID])

        # openrouter/free counts as free.
        broke = FakeProvider(PAID, error=http_error(402))
        router = FakeProvider("openrouter/free", text="Router answer.")
        backend, _, _ = chain_backend({PAID: broke, "openrouter/free": router})
        self.assertEqual(backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]["model"],
                         "openrouter/free")

    def test_all_fail_returns_first_error_unless_later_is_more_specific(self):
        cases = [
            ((429, 404), 429, MSG_PROVIDER_RATE_LIMITED),       # first is specific: kept
            ((503, 404), 404, MSG_MODEL_NOT_FOUND),    # later specific beats generic
            ((502, 503), 502, MSG_UNREACHABLE),        # all generic: first
            ((503, 429), 429, MSG_PROVIDER_RATE_LIMITED),
        ]
        for codes, status, message in cases:
            with self.subTest(codes=codes):
                providers = {PRIMARY: FakeProvider(PRIMARY, error=http_error(codes[0])),
                             FREE_A: FakeProvider(FREE_A, error=http_error(codes[1]))}
                backend, built, _ = chain_backend(providers)
                with self.assertRaises(ClientError) as ctx:
                    backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
                self.assertEqual((ctx.exception.status, ctx.exception.message),
                                 (status, message))
                self.assertEqual(built, [PRIMARY, FREE_A])

    def test_fallback_list_parsing(self):
        cfg = Config.from_env({"ARBORIS_MODEL": "a/b:free",
                               "ARBORIS_MODEL_FALLBACKS": " x/y:free , ,a/b:free, z/w:free ,x/y:free"})
        self.assertEqual(cfg.fallback_models, ("x/y:free", "z/w:free"))
        cfg = Config.from_env({"ARBORIS_MODEL_FALLBACKS": "none"})
        self.assertEqual(cfg.fallback_models, ())
        cfg = Config.from_env({"ARBORIS_MODEL_FALLBACKS": "   "})
        self.assertEqual(cfg.fallback_models, DEFAULT_FALLBACKS)
        cfg = Config.from_env({"ARBORIS_MODEL": DEFAULT_FALLBACKS[0]})
        self.assertNotIn(DEFAULT_FALLBACKS[0], cfg.fallback_models)
        self.assertEqual(len(cfg.fallback_models), len(DEFAULT_FALLBACKS) - 1)

    def test_cascade_primary_side_falls_back_escalation_does_not(self):
        dead = FakeProvider(PRIMARY, error=http_error(429))
        low = FakeProvider(FREE_A, avg_logprob=-3.0)
        big = FakeProvider(ESCALATION, text="Escalated answer. Second sentence.")
        backend, built, _ = chain_backend({PRIMARY: dead, FREE_A: low, ESCALATION: big},
                                          escalation_model=ESCALATION)
        meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
        self.assertTrue(meta["escalated"])
        self.assertEqual(meta["model"], ESCALATION)
        self.assertEqual(meta["primary_model"], FREE_A)
        # An escalation failure is still the escalation error, no fallback.
        dead = FakeProvider(PRIMARY, error=http_error(429))
        low = FakeProvider(FREE_A, avg_logprob=-3.0)
        big = FakeProvider(ESCALATION, error=http_error(429))
        backend, built, _ = chain_backend({PRIMARY: dead, FREE_A: low, ESCALATION: big},
                                          escalation_model=ESCALATION)
        with self.assertRaises(ClientError) as ctx:
            backend.chat(ChatRequest(prompt="Explain DNS"))
        self.assertEqual((ctx.exception.status, ctx.exception.message),
                         (502, MSG_ESCALATION_FAILED))
        self.assertEqual(len(big.calls), 1)


class SmallModelRoutingTests(unittest.TestCase):
    """Opt-in small-model routing: sprout prompts try the small model first.

    Default small model is FREE_A (liquid/lfm-2.5-2.6b:free, 2.6B). The
    chain_backend helper makes PRIMARY the primary and FREE_A a fallback
    model; routing reorders them to small-first and deduplicates.
    """

    def test_config_defaults_and_env(self):
        cfg = Config.from_env({})
        self.assertFalse(cfg.small_model_routing)
        self.assertEqual(cfg.small_model, FREE_A)
        cfg = Config.from_env({"ARBORIS_SMALL_MODEL_ROUTING": "1"})
        self.assertTrue(cfg.small_model_routing)
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_SMALL_MODEL": "other/model"})
        cfg = Config.from_env({"ARBORIS_SMALL_MODEL": "other/model",
                               "ARBORIS_ALLOW_PAID_MODELS": "1"})
        self.assertEqual(cfg.small_model, "other/model")

    def test_sprout_prompt_uses_small_model_first(self):
        small = FakeProvider(FREE_A, text="Small answer. Second sentence.", avg_logprob=-0.3)
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, built, _ = chain_backend(
            {PRIMARY: primary, FREE_A: small},
            small_model_routing=True, small_model=FREE_A)
        result = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        meta = result["meta"]
        self.assertEqual(result["answer"], "Small answer. Second sentence.")
        self.assertEqual(len(small.calls), 1)
        self.assertEqual(len(primary.calls), 0)
        self.assertEqual(meta["model"], FREE_A)
        self.assertEqual(meta["model_tier"], "small")
        self.assertTrue(meta["routed_to_small"])
        self.assertFalse(meta["fallback_used"])
        self.assertEqual(built, [FREE_A])

    def test_canopy_prompt_uses_primary(self):
        small = FakeProvider(FREE_A, text="Small answer. Second sentence.", avg_logprob=-0.3)
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = chain_backend(
            {PRIMARY: primary, FREE_A: small},
            small_model_routing=True, small_model=FREE_A)
        prompt = "Explain DNS in simple terms for someone who has never heard of it before today"
        result = backend.chat(ChatRequest(prompt=prompt, session_id=SESSION))
        meta = result["meta"]
        self.assertEqual(meta["mode"], "canopy")
        self.assertFalse(meta["routed_to_small"])
        self.assertEqual(meta["model"], PRIMARY)
        self.assertEqual(meta["model_tier"], "primary")
        self.assertEqual(len(small.calls), 0)
        self.assertEqual(len(primary.calls), 1)

    def test_small_failure_falls_back_to_primary(self):
        small = FakeProvider(FREE_A, error=http_error(429, "slow down"))
        primary = FakeProvider(PRIMARY, text="Primary answer. Second sentence.", avg_logprob=-0.2)
        backend, built, _ = chain_backend(
            {PRIMARY: primary, FREE_A: small},
            small_model_routing=True, small_model=FREE_A)
        result = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        meta = result["meta"]
        self.assertEqual(result["answer"], "Primary answer. Second sentence.")
        self.assertEqual(meta["model"], PRIMARY)
        self.assertTrue(meta["routed_to_small"])
        self.assertEqual(meta["model_tier"], "primary")
        self.assertFalse(meta["fallback_used"])  # primary is not a fallback
        self.assertEqual(built, [FREE_A, PRIMARY])

    def test_routing_off_sprout_uses_primary(self):
        small = FakeProvider(FREE_A, text="Small answer. Second sentence.", avg_logprob=-0.3)
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = chain_backend({PRIMARY: primary, FREE_A: small})
        result = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        meta = result["meta"]
        self.assertFalse(meta["routed_to_small"])
        self.assertEqual(meta["model"], PRIMARY)
        self.assertEqual(len(small.calls), 0)
        self.assertEqual(len(primary.calls), 1)

    def test_small_cache_hit_keeps_tier(self):
        small = FakeProvider(FREE_A, text="Small answer. Second sentence.", avg_logprob=-0.3)
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = chain_backend(
            {PRIMARY: primary, FREE_A: small},
            small_model_routing=True, small_model=FREE_A)
        first = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        second = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        self.assertFalse(first["meta"]["cache_hit"])
        self.assertTrue(second["meta"]["cache_hit"])
        self.assertEqual(second["meta"]["cached_from_model"], FREE_A)
        self.assertEqual(second["meta"]["model_tier"], "small")
        self.assertEqual(len(small.calls), 1)  # second request made no model call


class EmptyContentTests(unittest.TestCase):
    """HTTP 200 with null/empty text falls back, instead of erroring out."""

    def test_empty_content_falls_back_to_next_model(self):
        empty = FakeProvider(PRIMARY, error=ProviderError(
            "Live provider returned no text content (null or empty message).",
            empty_content=True))
        good = FakeProvider(FREE_A, text="Fallback answer. Second sentence.", avg_logprob=-0.3)
        backend, built, _ = chain_backend({PRIMARY: empty, FREE_A: good})
        result = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        self.assertEqual(result["answer"], "Fallback answer. Second sentence.")
        self.assertEqual(result["meta"]["model"], FREE_A)
        self.assertTrue(result["meta"]["fallback_used"])
        self.assertEqual(built, [PRIMARY, FREE_A])

    def test_all_empty_returns_clear_message(self):
        empty = FakeProvider(PRIMARY, error=ProviderError(
            "Live provider returned no text content (null or empty message).",
            empty_content=True))
        backend, _, _ = chain_backend({PRIMARY: empty})
        with self.assertRaises(ClientError) as ctx:
            backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        self.assertEqual((ctx.exception.status, ctx.exception.message), (502, MSG_EMPTY))


class ShrinkVersionTests(unittest.TestCase):
    """PromptShrink version is server config: v1 by default, v2 opt-in."""

    def test_config_defaults_and_invalid(self):
        cfg = Config.from_env({})
        self.assertEqual(cfg.shrink_version, "v1")
        cfg = Config.from_env({"ARBORIS_SHRINK_VERSION": "v2"})
        self.assertEqual(cfg.shrink_version, "v2")
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_SHRINK_VERSION": "v3"})

    def test_v2_shrinks_live_prompt(self):
        # v1 leaves the article in; v2 (gated telegraphic) drops it.
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary, shrink_version="v2")
        result = backend.chat(ChatRequest(prompt="Explain APIs to a 10-year-old", session_id=SESSION))
        self.assertEqual(primary.calls[0]["prompt"], "Explain APIs to 10-year-old")
        self.assertEqual(result["meta"]["shrink"]["version"], "v2")

    def test_v1_is_default_in_meta(self):
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary)
        result = backend.chat(ChatRequest(prompt="Explain APIs to a 10-year-old", session_id=SESSION))
        self.assertEqual(primary.calls[0]["prompt"], "Explain APIs to a 10-year-old")
        self.assertEqual(result["meta"]["shrink"]["version"], "v1")


class EnergyReceiptTests(unittest.TestCase):
    """Every live answer carries a per-answer energy receipt in meta."""

    def test_normal_answer_receipt(self):
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)  # 10 in, 50 out
        backend, _, _ = make_backend(primary=primary)
        result = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        energy = result["meta"]["energy"]
        self.assertEqual(energy["status"], "estimate")
        self.assertEqual((energy["input_tokens"], energy["output_tokens"]), (10, 50))
        # Output-weighted: 50 x 0.0006 + 10 x 0.0006 / 800 = 0.0300075 Wh.
        self.assertEqual((energy["energy_wh"], energy["co2e_g"]), (0.030007, 0.012003))
        self.assertEqual((energy["water_ml_low"], energy["water_ml_high"]), (0.003001, 0.009002))
        self.assertEqual(energy["factors"]["weighting"], "output")
        self.assertEqual(energy["model"], PRIMARY)

    def test_cache_hit_receipt_is_zero(self):
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary)
        backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        result = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        self.assertTrue(result["meta"]["cache_hit"])
        energy = result["meta"]["energy"]
        self.assertEqual((energy["energy_wh"], energy["co2e_g"]), (0.0, 0.0))
        self.assertTrue(energy["cache_hit"])

    def test_unreported_tokens_give_unavailable_receipt(self):
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2,
                               completion_tokens=None, prompt_tokens=None)
        backend, _, _ = make_backend(primary=primary)
        result = backend.chat(ChatRequest(prompt="Explain DNS", session_id=SESSION))
        energy = result["meta"]["energy"]
        self.assertEqual(energy["status"], "unavailable")
        self.assertIsNone(energy["energy_wh"])

    def test_grid_factor_config(self):
        cfg = Config.from_env({})
        self.assertEqual(cfg.grid_g_per_kwh, 400.0)
        cfg = Config.from_env({"ARBORIS_GRID_G_PER_KWH": "30"})
        self.assertEqual(cfg.grid_g_per_kwh, 30.0)
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_GRID_G_PER_KWH": "0"})
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_GRID_G_PER_KWH": "abc"})


class TaskRoutingTests(unittest.TestCase):
    """Task routing (opt-in): task type overrides word count, only for auto mode."""

    def test_config_defaults_and_flag(self):
        self.assertFalse(Config.from_env({}).task_routing)
        self.assertTrue(Config.from_env(
            {"ARBORIS_TASK_ROUTING": "1"}).task_routing)

    def test_long_fact_goes_small_despite_word_count(self):
        # Word-count routing would call this canopy (20+ words); task
        # routing calls it a fact and sends it small, in sprout mode.
        small = FakeProvider(FREE_A, text="Paris.", avg_logprob=-0.2)
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, built, _ = chain_backend(
            {FREE_A: small, PRIMARY: primary},
            model=PRIMARY, fallback_models=(FREE_A,),
            small_model=FREE_A, small_model_routing=True, task_routing=True)
        # chain_backend builds providers in chain order, not dict order;
        # the first built model is the one that answered.
        result = backend.chat(ChatRequest(
            prompt="Could you please tell me, in your own words and whenever "
                   "you have a moment, what the capital of France actually is?",
            session_id=SESSION))
        self.assertEqual(result["meta"]["task_type"], "fact")
        self.assertEqual(result["meta"]["task_tier"], "small")
        self.assertEqual(result["meta"]["routed_by"], "task")
        self.assertEqual(result["meta"]["mode"], "sprout")
        self.assertTrue(result["meta"]["routed_to_small"])

    def test_short_hard_question_goes_primary_despite_word_count(self):
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = make_backend(
            primary=primary, small_model_routing=True, task_routing=True)
        result = backend.chat(ChatRequest(
            prompt="Prove the Pythagorean theorem.", session_id=SESSION))
        self.assertEqual(result["meta"]["task_type"], "reasoning")
        self.assertEqual(result["meta"]["mode"], "forest")
        self.assertFalse(result["meta"]["routed_to_small"])
        self.assertIn("conclusion first", primary.calls[0]["system"])

    def test_off_preserves_word_count_behavior(self):
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary, task_routing=False)
        result = backend.chat(ChatRequest(
            prompt="Prove the Pythagorean theorem.", session_id=SESSION))
        self.assertEqual(result["meta"]["routed_by"], "word_count")
        self.assertIsNone(result["meta"]["task_type"])
        self.assertEqual(result["meta"]["mode"], "sprout")

    def test_explicit_mode_wins_over_task_routing(self):
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary, task_routing=True)
        result = backend.chat(ChatRequest(
            prompt="What is the capital of France?", session_id=SESSION, mode="full"))
        self.assertEqual(result["meta"]["routed_by"], "explicit")
        self.assertEqual(result["meta"]["mode"], "forest")
        self.assertEqual(result["meta"]["task_type"], "fact")

    def test_task_tier_small_needs_small_routing_enabled(self):
        # Task routing on, small-model routing off: the task tier is
        # reported, but the primary model still answers.
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = make_backend(
            primary=primary, small_model_routing=False, task_routing=True)
        result = backend.chat(ChatRequest(
            prompt="What is the capital of France?", session_id=SESSION))
        self.assertEqual(result["meta"]["task_tier"], "small")
        self.assertFalse(result["meta"]["routed_to_small"])
        self.assertEqual(result["meta"]["model"], PRIMARY)


class LogprobsFallbackTests(unittest.TestCase):
    def test_retries_same_model_without_logprobs(self):
        picky = FakeProvider(PRIMARY, avg_logprob=-0.2, logprobs_error=http_error(
            400, "This model does not support Logprobs"))
        fallback = FakeProvider(FREE_A)
        backend, built, log = chain_backend({PRIMARY: picky, FREE_A: fallback})
        meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
        self.assertEqual([c["logprobs"] for c in picky.calls], [True, False])
        self.assertEqual(fallback.calls, [])
        self.assertEqual(meta["model"], PRIMARY)
        self.assertIsNone(meta["avg_logprob"])
        self.assertFalse(meta["escalated"])
        self.assertIn("retrying once without logprobs", log.getvalue())

    def test_logprob_retry_then_cascade_does_not_fire(self):
        picky = FakeProvider(PRIMARY, avg_logprob=-5.0,
                             logprobs_error=ProviderError("logprobs not supported"))
        big = FakeProvider(ESCALATION)
        backend, _, _ = chain_backend({PRIMARY: picky, ESCALATION: big},
                                      escalation_model=ESCALATION)
        meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
        self.assertFalse(meta["escalated"])
        self.assertIsNone(meta["avg_logprob"])
        self.assertIn("logprobs unavailable", meta["escalation_reason"])
        self.assertEqual(big.calls, [])

    def test_unrelated_error_is_not_retried_without_logprobs(self):
        dead = FakeProvider(PRIMARY, error=http_error(404))
        backend, _, _ = chain_backend({PRIMARY: dead}, fallback_models=())
        with self.assertRaises(ClientError):
            backend.chat(ChatRequest(prompt="Explain DNS"))
        self.assertEqual(len(dead.calls), 1)

    def test_failed_logprob_retry_moves_to_fallback(self):
        picky = FakeProvider(PRIMARY, error=http_error(429, "logprobs and rate limit"))
        fallback = FakeProvider(FREE_A, text="Fallback answer.")
        backend, _, _ = chain_backend({PRIMARY: picky, FREE_A: fallback})
        meta = backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]
        self.assertEqual([c["logprobs"] for c in picky.calls], [True, False])
        self.assertEqual(meta["model"], FREE_A)


class FragmentTests(unittest.TestCase):
    def test_drops_trailing_fragment_after_complete_sentence(self):
        text = "Machine learning finds patterns in data. Machine learning is a type"
        self.assertEqual(apply_brevity_with_flag(text, "sprout"),
                         ("Machine learning finds patterns in data.", True))
        self.assertEqual(apply_brevity(text, "sprout"), "Machine learning finds patterns in data.")

    def test_single_fragment_gets_ellipsis(self):
        self.assertEqual(apply_brevity_with_flag("Machine learning is a type", "sprout"),
                         ("Machine learning is a type" + ELLIPSIS, True))

    def test_complete_endings_are_kept(self):
        for text in ('He said "stop."', "It works (mostly.)", "Really?", "Yes!",
                     "It is **done.**", "First. Second?"):
            with self.subTest(text=text):
                self.assertEqual(apply_brevity_with_flag(text, "full"), (text, False))

    def test_lists_code_and_sign_offs_are_not_fragments(self):
        for text in ("Steps:\n- install it\n- run it",
                     "Use this:\n\n```\nls -la\n```",
                     "Hi there.\n\nThanks,\nGrant"):
            with self.subTest(text=text):
                self.assertFalse(apply_brevity_with_flag(text, "full")[1])

    def test_server_marks_truncated(self):
        small = FakeProvider(PRIMARY, text="Machine learning is a type", completion_tokens=120)
        backend, _, _ = make_backend(primary=small)
        result = backend.chat(ChatRequest(prompt="Explain machine learning", mode="sprout"))
        self.assertEqual(result["answer"], "Machine learning is a type" + ELLIPSIS)
        self.assertTrue(result["meta"]["truncated"])

        small = FakeProvider(PRIMARY, text="ML learns from data. Machine learning is a type")
        backend, _, _ = make_backend(primary=small)
        result = backend.chat(ChatRequest(prompt="Explain machine learning", mode="sprout"))
        self.assertEqual(result["answer"], "ML learns from data.")
        self.assertTrue(result["meta"]["truncated"])

    def test_finish_reason_length_marks_truncated(self):
        small = FakeProvider(PRIMARY, text="Complete sentence.", finish_reason="length")
        backend, _, _ = make_backend(primary=small)
        self.assertTrue(backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]["truncated"])
        small = FakeProvider(PRIMARY, text="Complete sentence.", finish_reason="stop")
        backend, _, _ = make_backend(primary=small)
        self.assertFalse(backend.chat(ChatRequest(prompt="Explain DNS"))["meta"]["truncated"])


class ProviderParsingTests(unittest.TestCase):
    """OpenAICompatibleProvider response parsing, with urlopen stubbed out."""

    def call(self, body=None, http_code=None):
        import urllib.error
        from unittest import mock
        from arboris.providers import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider("https://example.invalid/v1",
                                            api_key=SENTINEL_KEY, model="m")
        if http_code is not None:
            err = urllib.error.HTTPError("u", http_code, "x", {},
                                         io.BytesIO(b'{"error":{"message":"nope"}}'))
            patch = mock.patch("urllib.request.urlopen", side_effect=err)
        else:
            resp = io.BytesIO(json.dumps(body).encode())
            resp.__enter__ = lambda *a: resp
            resp.__exit__ = lambda *a: False
            patch = mock.patch("urllib.request.urlopen", return_value=resp)
        with patch:
            return provider.complete_ex("hi")

    def test_finish_reason_and_model_parsed(self):
        comp = self.call({"model": "routed/model:free", "choices": [
            {"message": {"content": "Hi."}, "finish_reason": "length"}]})
        self.assertEqual((comp.finish_reason, comp.response_model), ("length", "routed/model:free"))
        comp = self.call({"choices": [{"message": {"content": "Hi."}}]})
        self.assertEqual((comp.finish_reason, comp.response_model), (None, None))

    def test_http_error_carries_status(self):
        with self.assertRaises(ProviderError) as ctx:
            self.call(http_code=404)
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("HTTP 404", str(ctx.exception))

    def test_reasoning_tokens_parsed(self):
        comp = self.call({"choices": [{"message": {"content": "Hi."}}],
                          "usage": {"completion_tokens": 50, "prompt_tokens": 10,
                                    "completion_tokens_details": {"reasoning_tokens": 30}}})
        self.assertEqual(comp.reasoning_tokens, 30)

    def test_reasoning_tokens_none_when_absent(self):
        comp = self.call({"choices": [{"message": {"content": "Hi."}}],
                          "usage": {"completion_tokens": 50, "prompt_tokens": 10}})
        self.assertIsNone(comp.reasoning_tokens)

    def test_reasoning_effort_forwarded_in_payload(self):
        """reasoning_effort is sent in the request payload when provided."""
        import urllib.error
        from unittest import mock
        from arboris.providers import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider("https://example.invalid/v1",
                                            api_key=SENTINEL_KEY, model="m")
        resp = io.BytesIO(json.dumps({"choices": [{"message": {"content": "Hi."}}]}).encode())
        resp.__enter__ = lambda *a: resp
        resp.__exit__ = lambda *a: False
        captured = {}

        def fake_urlopen(req, **kw):
            captured["payload"] = json.loads(req.data.decode())
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            provider.complete_ex("hi", reasoning_effort="low")
        self.assertEqual(captured["payload"]["reasoning_effort"], "low")

    def test_reasoning_effort_omitted_when_none(self):
        """reasoning_effort is not in payload when not provided."""
        import urllib.error
        from unittest import mock
        from arboris.providers import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider("https://example.invalid/v1",
                                            api_key=SENTINEL_KEY, model="m")
        resp = io.BytesIO(json.dumps({"choices": [{"message": {"content": "Hi."}}]}).encode())
        resp.__enter__ = lambda *a: resp
        resp.__exit__ = lambda *a: False
        captured = {}

        def fake_urlopen(req, **kw):
            captured["payload"] = json.loads(req.data.decode())
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            provider.complete_ex("hi")
        self.assertNotIn("reasoning_effort", captured["payload"])


if __name__ == "__main__":
    unittest.main()


class ProvenRoutingServerTests(unittest.TestCase):
    """Server wiring for the FrugalGPT / RouteLLM adaptations."""

    def test_config_defaults_and_env(self):
        cfg = Config.from_env({})
        self.assertFalse(cfg.difficulty_routing)
        self.assertEqual(cfg.router_threshold, 0.5)
        self.assertEqual(cfg.quality_target, "balanced")
        self.assertFalse(cfg.scorer_escalation)
        self.assertEqual(cfg.score_threshold, 0.5)
        self.assertEqual(cfg.max_cascade_output_tokens, 0)
        self.assertFalse(cfg.semantic_cache)
        eco = Config.from_env({"ARBORIS_QUALITY_TARGET": "eco"})
        self.assertEqual(eco.router_threshold_effective, 0.7)
        quality = Config.from_env({"ARBORIS_QUALITY_TARGET": "quality"})
        self.assertEqual(quality.router_threshold_effective, 0.3)
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_ROUTER_THRESHOLD": "1.5"})
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_QUALITY_TARGET": "fast"})

    def test_local_calc_bypasses_model(self):
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary)
        result = backend.chat(ChatRequest(prompt="What is 12 + 7?", session_id=SESSION))
        self.assertEqual(result["answer"], "12 + 7 = 19")
        self.assertTrue(result["meta"]["local_answer"])
        self.assertEqual(result["meta"]["model_tier"], "local")
        self.assertEqual(result["meta"]["output_tokens"], 0)
        self.assertEqual(result["meta"]["energy"]["energy_wh"], 0)
        self.assertEqual(primary.calls, [])

    def test_local_calc_fallthrough_calls_model(self):
        primary = FakeProvider(PRIMARY, text="Paris.", avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary)
        backend.chat(ChatRequest(prompt="What is 12 + 7 apples?", session_id=SESSION))
        self.assertEqual(len(primary.calls), 1)

    def test_difficulty_threshold_moves_simple_steps(self):
        # Simple steps: task tier small, difficulty 0.4. With quality
        # (threshold 0.3) it goes primary; with eco (0.7) it stays small.
        small = FakeProvider(FREE_A, text="Restart it.", avg_logprob=-0.2)
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2)
        backend, _, _ = chain_backend(
            {FREE_A: small, PRIMARY: primary},
            model=PRIMARY, fallback_models=(FREE_A,),
            small_model=FREE_A, small_model_routing=True, task_routing=True,
            difficulty_routing=True, quality_target="quality")
        result = backend.chat(ChatRequest(prompt="How do I reset my router?",
                                          session_id=SESSION))
        self.assertEqual(result["meta"]["difficulty_tier"], "primary")
        self.assertFalse(result["meta"]["routed_to_small"])
        self.assertEqual(result["meta"]["difficulty_score"], 0.4)

    def test_answer_score_reported_without_escalation(self):
        primary = FakeProvider(PRIMARY, text="Paris is the capital of France.",
                               avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary, task_routing=True)
        result = backend.chat(ChatRequest(prompt="What is the capital of France?",
                                          session_id=SESSION))
        self.assertIsNotNone(result["meta"]["answer_score"])
        self.assertGreaterEqual(result["meta"]["answer_score"], 0.5)

    def test_scorer_escalates_without_logprobs(self):
        primary = FakeProvider(PRIMARY, text="x", avg_logprob=None)
        big = FakeProvider(ESCALATION, text="Escalated answer. Second sentence.")
        backend, _, _ = make_cascade_backend(
            primary=primary, big=big, scorer_escalation=True, score_threshold=0.9)
        result = backend.chat(ChatRequest(prompt="Explain DNS simply",
                                          session_id=SESSION))
        self.assertTrue(result["meta"]["escalated"])
        self.assertEqual(len(big.calls), 1)

    def test_semantic_cache_hit_and_number_guard(self):
        primary = FakeProvider(PRIMARY, text="Paris.", avg_logprob=-0.2)
        backend, _, _ = make_backend(
            primary=primary, task_routing=True, semantic_cache=True)
        first = backend.chat(ChatRequest(prompt="What is the capital of France?",
                                         session_id=SESSION))
        self.assertFalse(first["meta"]["semantic_cache_hit"])
        second = backend.chat(ChatRequest(prompt="What is capital of France?",
                                          session_id=SESSION))
        self.assertTrue(second["meta"]["semantic_cache_hit"])
        self.assertEqual(second["answer"], "Paris.")
        self.assertEqual(second["meta"]["output_tokens"], 0)
        # A different number is a different question: no semantic hit.
        third = backend.chat(ChatRequest(prompt="What is the capital of France 2?",
                                         session_id=SESSION))
        self.assertFalse(third["meta"]["semantic_cache_hit"])

    def test_single_word_answer_not_marked_truncated_on_stop(self):
        primary = FakeProvider(PRIMARY, text="Paris", avg_logprob=-0.2,
                               finish_reason="stop")
        backend, _, _ = make_backend(primary=primary, task_routing=True)
        result = backend.chat(ChatRequest(prompt="What is the capital of France?",
                                          session_id=SESSION))
        self.assertEqual(result["answer"], "Paris")
        self.assertFalse(result["meta"]["truncated"])

    def test_single_word_answer_keeps_mark_on_length(self):
        primary = FakeProvider(PRIMARY, text="Paris", avg_logprob=-0.2,
                               finish_reason="length")
        backend, _, _ = make_backend(primary=primary, task_routing=True)
        result = backend.chat(ChatRequest(prompt="What is the capital of France?",
                                          session_id=SESSION))
        self.assertTrue(result["meta"]["truncated"])

    def test_expansion_goes_full_primary_with_detail_prompt(self):
        primary = FakeProvider(PRIMARY, text="A long detailed answer. Second sentence.",
                               avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary, small_model_routing=True,
                                     task_routing=True)
        result = backend.chat(ChatRequest(
            prompt="Explain in more detail: What is DNS?",
            session_id=SESSION, expand_of="What is DNS?"))
        meta = result["meta"]
        self.assertTrue(meta["expansion"])
        self.assertEqual(meta["expanded_from"], "What is DNS?")
        self.assertEqual(meta["mode"], "forest")
        self.assertEqual(meta["routed_by"], "expansion")
        self.assertFalse(meta["routed_to_small"])
        self.assertIn("Explain in detail.", primary.calls[0]["system"])

    def test_expansion_request_validation(self):
        from server.app import parse_chat_request
        req = parse_chat_request(json.dumps(
            {"prompt": "Explain in more detail: What is DNS?",
             "expand_of": "What is DNS?"}).encode())
        self.assertEqual(req.expand_of, "What is DNS?")
        with self.assertRaises(ClientError):
            parse_chat_request(json.dumps(
                {"prompt": "hi", "expand_of": ""}).encode())
        with self.assertRaises(ClientError):
            parse_chat_request(json.dumps(
                {"prompt": "hi", "expand_of": 42}).encode())

    def test_multi_part_meta(self):
        primary = FakeProvider(PRIMARY, text="DNS and TCP explained.",
                               avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary)
        meta = backend.chat(ChatRequest(
            prompt="What is DNS? What does TCP mean?",
            session_id=SESSION))["meta"]
        self.assertEqual(meta["questions"], 2)
        self.assertTrue(meta["multi_part"])

    def test_truncated_cached_answer_is_not_reused(self):
        primary = FakeProvider(PRIMARY, text="DNS is", avg_logprob=-0.2,
                               finish_reason="length")
        backend, _, _ = make_backend(primary=primary)
        first = backend.chat(ChatRequest(prompt="What is DNS?",
                                         session_id=SESSION))
        self.assertTrue(first["meta"]["truncated"])
        primary.text = "DNS is a naming system. Second sentence."
        primary.finish_reason = "stop"
        second = backend.chat(ChatRequest(prompt="What is DNS?",
                                          session_id=SESSION))
        self.assertFalse(second["meta"]["cache_hit"])
        self.assertIsNotNone(second["meta"]["cache_bypass_reason"])
        self.assertIn("naming system", second["answer"])
        self.assertEqual(len(primary.calls), 2)

    def test_failed_fresh_call_falls_back_to_cached_answer(self):
        primary = FakeProvider(PRIMARY, text="DNS is", avg_logprob=-0.2,
                               finish_reason="length")
        backend, _, _ = make_backend(primary=primary)
        backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        primary.error = http_error(502)
        result = backend.chat(ChatRequest(prompt="What is DNS?",
                                          session_id=SESSION))
        self.assertTrue(result["meta"]["cache_fallback"])
        # The cached answer is the brevity-trimmed answer, ellipsis included.
        self.assertEqual(result["answer"], "DNS is…")

    def test_max_tokens_multiplier_and_validation(self):
        cfg = Config.from_env({})
        self.assertEqual(cfg.max_tokens_multiplier, 1.0)
        self.assertFalse(cfg.task_temperature)
        self.assertFalse(cfg.task_stop_sequences)
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_MAX_TOKENS_MULTIPLIER": "0"})
        with self.assertRaises(ValueError):
            Config.from_env({"ARBORIS_MAX_TOKENS_MULTIPLIER": "5"})
        primary = FakeProvider(PRIMARY, text="Paris.", avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary, task_routing=True,
                                     max_tokens_multiplier=2.0)
        result = backend.chat(ChatRequest(prompt="What is the capital of France?",
                                          session_id=SESSION))
        self.assertEqual(result["meta"]["max_tokens_used"], 240)
        self.assertEqual(primary.calls[0]["max_tokens"], 240)

    def test_task_temperature_by_type(self):
        primary = FakeProvider(PRIMARY, text="Paris.", avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary, task_routing=True,
                                     task_temperature=True)
        result = backend.chat(ChatRequest(prompt="What is the capital of France?",
                                          session_id=SESSION))
        self.assertEqual(result["meta"]["temperature"], 0.2)
        self.assertEqual(primary.calls[0]["temperature"], 0.2)

    def test_long_input_pruning_on_and_off(self):
        plain = ("The weather was pleasant and the road was long and winding "
                 "through the hills and valleys of the countryside. ")
        prompt = ("Here is a long document about Mars. " + plain * 20
                  + "In 1969, Apollo landed on the Moon. "
                  + "That is the end of the document.")
        self.assertGreater(len(prompt), 1500)
        primary = FakeProvider(PRIMARY, text="A summary. Second sentence.",
                               avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary, long_input_pruning=True)
        result = backend.chat(ChatRequest(prompt=prompt,
                                          session_id=SESSION))
        self.assertTrue(result["meta"]["long_input_pruned"])
        self.assertGreater(result["meta"]["pruned_sentences"], 0)
        self.assertIn("Pruned for efficiency", primary.calls[0]["prompt"])
        self.assertNotIn("The weather was pleasant",
                         primary.calls[0]["prompt"])

    def test_task_stop_only_for_fact_and_definition(self):
        primary = FakeProvider(PRIMARY, text="Paris.", avg_logprob=-0.2)
        backend, _, _ = make_backend(primary=primary, task_routing=True,
                                     task_stop_sequences=True)
        fact = backend.chat(ChatRequest(prompt="What is the capital of France?",
                                        session_id=SESSION))
        self.assertEqual(fact["meta"]["stop_sequences"], ["\n\n"])
        self.assertEqual(primary.calls[-1]["stop"], ["\n\n"])
        steps = backend.chat(ChatRequest(prompt="How do I reset my router?",
                                         session_id=SESSION))
        self.assertIsNone(steps["meta"]["stop_sequences"])
        self.assertIsNone(primary.calls[-1]["stop"])


class ReasoningEffortTests(unittest.TestCase):
    """L2 lever: reasoning effort caps and reasoning_tokens transparency."""

    def test_reasoning_effort_sent_when_enabled(self):
        """When reasoning_effort=True, the provider receives the per-mode effort."""
        backend, primary, _ = make_backend(reasoning_effort=True)
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        # DNS routes to sprout → reasoning_effort should be "low"
        self.assertEqual(primary.calls[-1]["reasoning_effort"], "low")
        self.assertEqual(result["meta"]["reasoning_effort"], "low")

    def test_reasoning_effort_not_sent_when_disabled(self):
        """When reasoning_effort=False (default), no effort is sent."""
        backend, primary, _ = make_backend()
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        self.assertIsNone(primary.calls[-1]["reasoning_effort"])
        self.assertIsNone(result["meta"]["reasoning_effort"])

    def test_reasoning_effort_per_mode(self):
        """Each mode maps to the correct effort level."""
        from server.app import REASONING_EFFORT
        self.assertEqual(REASONING_EFFORT["sprout"], "low")
        self.assertEqual(REASONING_EFFORT["canopy"], "low")
        self.assertEqual(REASONING_EFFORT["full"], "medium")

    def test_reasoning_tokens_reported_in_meta(self):
        """When provider reports reasoning_tokens, they appear in meta."""
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2, reasoning_tokens=15)
        backend, _, _ = make_backend(primary=primary)
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        self.assertEqual(result["meta"]["reasoning_tokens"], 15)

    def test_reasoning_tokens_none_when_not_reported(self):
        """When provider does not report reasoning_tokens, meta has None."""
        backend, _, _ = make_backend()
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        self.assertIsNone(result["meta"]["reasoning_tokens"])

    def test_reasoning_tokens_in_energy_receipt(self):
        """reasoning_tokens appear in the energy receipt."""
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2, reasoning_tokens=10)
        backend, _, _ = make_backend(primary=primary)
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        energy = result["meta"]["energy"]
        self.assertEqual(energy["reasoning_tokens"], 10)

    def test_reasoning_tokens_in_energy_note(self):
        """reasoning_tokens get a breakdown line in the energy note."""
        primary = FakeProvider(PRIMARY, avg_logprob=-0.2,
                               completion_tokens=50, reasoning_tokens=20)
        backend, _, _ = make_backend(primary=primary)
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        note = result["meta"]["energy"]["note"]
        self.assertIn("20 were reasoning tokens", note)
        self.assertIn("included in output, not additive", note)

    def test_local_calc_has_reasoning_tokens_none(self):
        """Local-calc answers (no model call) have reasoning_tokens=None."""
        backend, _, _ = make_backend()
        result = backend.chat(ChatRequest(prompt="What is 2+2?", session_id=SESSION))
        self.assertIsNone(result["meta"]["reasoning_tokens"])

    def test_cache_hit_has_reasoning_tokens_none(self):
        """Exact cache hits have reasoning_tokens=None in meta."""
        backend, _, _ = make_backend()
        backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        self.assertIsNone(result["meta"]["reasoning_tokens"])


class TokenBudgetTests(unittest.TestCase):
    """L3 lever: explicit token budget instruction in system prompt."""

    def test_budget_appended_when_enabled(self):
        """When token_budget=True, system prompt includes 'Use under N tokens.'"""
        backend, primary, _ = make_backend(token_budget=True)
        backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        system = primary.calls[-1]["system"]
        self.assertIn("Use under 100 tokens.", system)

    def test_budget_not_appended_when_disabled(self):
        """When token_budget=False (default), no budget in system prompt."""
        backend, primary, _ = make_backend()
        backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        system = primary.calls[-1]["system"]
        self.assertNotIn("Use under", system)

    def test_budget_per_mode(self):
        """Each mode gets its own budget value in the system prompt."""
        from arboris.providers import TOKEN_BUDGET
        backend, primary, _ = make_backend(token_budget=True)
        for mode_name, expected in TOKEN_BUDGET.items():
            backend.chat(ChatRequest(prompt="hello", mode=mode_name, session_id=SESSION))
            system = primary.calls[-1]["system"]
            self.assertIn(f"Use under {expected} tokens.", system)

    def test_budget_in_meta(self):
        """Meta reports the budget value when enabled."""
        backend, _, _ = make_backend(token_budget=True)
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        self.assertEqual(result["meta"]["token_budget"], 100)

    def test_budget_none_in_meta_when_disabled(self):
        """Meta reports None when budget is off."""
        backend, _, _ = make_backend()
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        self.assertIsNone(result["meta"]["token_budget"])

    def test_budget_none_for_local_calc(self):
        """Local-calc answers have no budget (no model call)."""
        backend, _, _ = make_backend(token_budget=True)
        result = backend.chat(ChatRequest(prompt="2 + 2", session_id=SESSION))
        self.assertIsNone(result["meta"]["token_budget"])

    def test_budget_none_for_cache_hit(self):
        """Cache-hit answers have no budget (no model call)."""
        backend, _, _ = make_backend(token_budget=True)
        backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        result = backend.chat(ChatRequest(prompt="What is DNS?", session_id=SESSION))
        self.assertIsNone(result["meta"]["token_budget"])

    def test_config_env_var(self):
        """ARBORIS_TOKEN_BUDGET env var is parsed."""
        cfg = Config.from_env({"ARBORIS_TOKEN_BUDGET": "1"})
        self.assertTrue(cfg.token_budget)
        cfg2 = Config.from_env({})
        self.assertFalse(cfg2.token_budget)
