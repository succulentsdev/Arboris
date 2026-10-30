"""Provider interface: where answers come from.

Two providers, kept strictly separate so demo output can never be
mistaken for a live model response:

- DemoProvider: offline, keyword canned answers covering the same six
  topics as the portfolio site demo (email, API, workout, backup,
  firewall/Portcullis, DNS), plus 89 more from spec/templates.json
  (technology, science, math, history, business, health, cooking,
  home maintenance, programming). No network, no key, no model. For
  trying the harness. Note one deliberate difference from the site:
  the site uses authored mode-specific variants, while this provider
  returns one full draft and the brevity layer trims it to the mode
  budget.
- OpenAICompatibleProvider: POSTs to a /chat/completions endpoint with a
  bearer key from the environment (ARBORIS_API_KEY). Only used when
  the user explicitly asks for live mode and supplies a key and base URL.
  Standard library only (urllib).
"""

import json
import os
import pathlib
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional


class ProviderError(Exception):
    """A live-provider failure, raised instead of leaking a traceback.

    The CLI catches this and exits 2 with the message. HTTP status,
    connection failures, timeouts, and malformed responses all become
    this one readable error type.

    status is the provider's HTTP status code when there was one (None
    for connection failures, timeouts and malformed responses), so
    callers can branch on it without parsing the message.
    """

    def __init__(self, message="", status=None, empty_content=False, reasoning_limited=False):
        super().__init__(message)
        self.status = status
        # True when the provider answered HTTP 200 with null/empty text.
        # Callers treat this as transient and retryable (next model, or a
        # retry in the baseline study), unlike a 401/402/404, which would
        # not help. Observed with Gemma returning empty content once.
        self.empty_content = empty_content
        self.reasoning_limited = reasoning_limited


@dataclass
class Completion:
    """A provider reply plus whatever usage the provider reported."""

    text: str
    completion_tokens: Optional[int] = None  # tokens actually generated
    prompt_tokens: Optional[int] = None
    # Mean log-probability of the generated tokens, when the provider
    # reported logprobs. None means "no confidence signal available" --
    # never invent one from answer length or any other proxy.
    avg_logprob: Optional[float] = None
    # choices[0].finish_reason as reported ("stop", "length", ...), or None.
    # "length" means the token cap cut the answer off.
    finish_reason: Optional[str] = None
    # The response's top-level "model" field, when reported. A router id
    # such as openrouter/free may report the model it routed to here.
    response_model: Optional[str] = None
    # Reasoning tokens from completion_tokens_details.reasoning_tokens,
    # when the provider reports them.  These are INCLUDED in
    # completion_tokens (they are not additive), so the energy formula
    # does not change; this is a transparency breakdown.
    reasoning_tokens: Optional[int] = None
    # Actual provider cap, including any reasoning headroom for the free router.
    max_tokens_requested: Optional[int] = None


# Generation-side brevity: asking the model for a short answer (and capping
# its length) is what saves compute. Trimming a long answer afterwards only
# changes what is displayed; the tokens were already generated.
BREVITY_SYSTEM = {
    "sprout": "Answer in at most 2 short sentences.",
    "canopy": "Answer in at most 4 short sentences.",
    "full": "Answer in at most 6 short sentences.",
}
MAX_TOKENS = {"sprout": 120, "canopy": 240, "full": 400}

# L3 explicit token budget: a soft "use under N tokens" instruction
# appended to the system prompt, opt-in via ARBORIS_TOKEN_BUDGET.
# Values are set slightly below MAX_TOKENS so the model aims to finish
# before the hard cap truncates.  Too-tight budgets can paradoxically
# raise token usage (TALE effect), so these are conservative.
TOKEN_BUDGET = {"sprout": 100, "canopy": 200, "full": 350}


def _matches(prompt, *patterns):
    """True if any pattern matches on a word boundary.

    Word-boundary matching matters: plain substring matching routed
    "capital" and "rapid growth" to the API answer, because both contain
    "api" as a substring. ``\\bapi\\b`` does not match inside those words.
    """
    return any(re.search(pattern, prompt, flags=re.IGNORECASE) for pattern in patterns)


# ---------------------------------------------------------------------------
# Expanded template library loaded from spec/templates.json
# ---------------------------------------------------------------------------

_EXPANDED_TEMPLATES = None  # lazy-loaded list of {"id", "keywords", "answer"}


def _load_templates():
    """Load the expanded template library once, on first use."""
    global _EXPANDED_TEMPLATES
    if _EXPANDED_TEMPLATES is not None:
        return _EXPANDED_TEMPLATES

    # Resolve path relative to the package: ../spec/templates.json
    spec_path = pathlib.Path(__file__).resolve().parent.parent / "spec" / "templates.json"
    try:
        with open(spec_path, encoding="utf-8") as f:
            data = json.load(f)
        _EXPANDED_TEMPLATES = data.get("topics", [])
    except (OSError, json.JSONDecodeError):
        _EXPANDED_TEMPLATES = []
    return _EXPANDED_TEMPLATES


def _match_expanded(prompt):
    """Return the answer from the expanded library, or None.

    Iterates through all expanded topics and returns the first match.
    The original six hardcoded topics in DemoProvider._answer take
    priority over this function.
    """
    templates = _load_templates()
    for topic in templates:
        keywords = topic.get("keywords", [])
        if _matches(prompt, *keywords):
            return topic.get("answer")
    return None


class DemoProvider:
    """Offline canned answers. No network, no model."""

    name = "demo (offline)"
    live = False

    def complete(self, prompt):
        return self.complete_ex(prompt).text

    def complete_ex(self, prompt, system=None, max_tokens=None, logprobs=False,
                    stop=None, temperature=None, reasoning_effort=None):
        """Offline provider ignores system/max_tokens/stop/temperature/reasoning_effort.

        It also has no logprobs: avg_logprob stays None, so a cascade
        must not escalate (or claim confidence) from a demo answer.
        """
        if prompt is None or not prompt.strip():
            raise ValueError("Empty prompt: type a prompt first.")
        return Completion(text=self._answer(prompt))

    def _answer(self, prompt):
        p = prompt.lower()
        if _matches(p, r"\bemail\b", r"\bfollow-up\b", r"\bfollow up\b"):
            return (
                "Subject: Quick follow-up\n\nHi — just circling back on our call. "
                "Happy to reschedule at your convenience, or send a summary by email "
                "if easier. What works best for you?\n\nThanks,\nGrant"
            )
        if _matches(p, r"\bapi\b", r"\bapis\b"):
            return (
                "An API is a waiter for software: your app orders from a menu, and "
                "the API brings back exactly what was asked for. Your app never "
                "needs to see how the other program works inside. It just gets "
                "the result. That separation is why apps can use maps, payments, "
                "or weather data without building those systems themselves, and "
                "why a change inside one program does not have to break the other."
            )
        if _matches(p, r"\bworkout\b", r"\bexercise\b"):
            return (
                "Day 1: Squats, push-ups, glute bridges — 3×10 each. "
                "Day 2: Rest or walk 20 min. "
                "Day 3: Lunges, wall push-ups, dead bugs — 3×10. "
                "Rest 60s between sets, keep form clean, and add reps weekly. "
                "Start lighter than you think you need to, stop a set if your "
                "form breaks down, and repeat the same three days next week "
                "before adding weight or reps."
            )
        if _matches(p, r"\bbackup\b", r"\bbackups\b"):
            return (
                "Backups matter because hardware fails, files get deleted, and "
                "ransomware encrypts, usually at the worst time. "
                "A tested backup turns a business-ending day into a bad morning. "
                "Rule of thumb: 3 copies, 2 media types, 1 off-site. "
                "The key word is tested: a backup you have never restored from "
                "is a hope, not a plan, so restore a file now and then to prove "
                "it works."
            )
        if _matches(p, r"\bfirewall\b", r"\bportcullis\b"):
            return (
                "A portcullis firewall is a gatekeeper for a network: it checks "
                "what software is running and flags known-vulnerable versions. "
                "Portcullis does this through your AI agent instead of a "
                "dashboard. It runs a daily check and stays silent when "
                "everything is clean. When something is at risk, it explains "
                "the risk in plain English. For a small business, that means "
                "catching one outdated, exploitable program before it becomes "
                "a breach, without hiring a full security team."
            )
        if _matches(p, r"\bdns\b", r"\bdomain name\b"):
            return (
                "DNS is the internet phone book: it turns a name like "
                "example.com into the number (IP address) a computer needs to "
                "reach it. You type the name, DNS looks up the matching address, "
                "and your browser connects there, so you never have to memorize "
                "numbers. If DNS cannot find a name, the site will not load "
                "even when your connection is fine, which is why a DNS problem "
                "can look like the whole internet is down."
            )
        # --- Expanded template library (89 topics) ---
        expanded = _match_expanded(p)
        if expanded:
            return expanded
        # --- Fallback for truly unrecognized prompts ---
        cleaned = " ".join(prompt.split())
        topic = cleaned[:80].strip() + "…" if len(cleaned) > 80 else cleaned
        return (
            f"That topic is not in the demo library yet. You asked about: "
            f"'{topic}'. Try rephrasing with a clearer keyword, or switch "
            f"to live mode for a real model response. The demo covers 95 "
            f"topics: the original six (email, APIs, workouts, backups, "
            f"firewalls/Portcullis, DNS) plus 89 more across technology, "
            f"science, math, history, business, health, cooking, home "
            f"maintenance, and programming."
        )


class OpenAICompatibleProvider:
    """Live provider via an OpenAI-compatible /chat/completions endpoint."""

    name = "openai-compatible (live)"
    live = True

    def __init__(self, base_url, api_key=None, model="gpt-4o-mini"):
        self.base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError(
                "base URL must use https (plain http is only allowed for "
                "localhost), because the API key is sent in a header."
            )
        # ARBORIS_API_KEY is canonical since the 0.3.0 rename.
        # ARBORIS_API_KEY is canonical since the 0.9.0 rename.
        # GREENSEARCH_API_KEY is accepted as a one-version fallback.
        self.api_key = (
            api_key
            or os.environ.get("ARBORIS_API_KEY", "")
            or os.environ.get("GREENSEARCH_API_KEY", "")
        )
        if not self.api_key:
            raise ValueError(
                "No API key: set ARBORIS_API_KEY or pass api_key explicitly."
            )
        self.model = model

    def complete(self, prompt):
        return self.complete_ex(prompt).text

    def complete_ex(self, prompt, system=None, max_tokens=None, logprobs=False,
                    stop=None, temperature=None, reasoning_effort=None,
                    attachments=None, history=None):
        if prompt is None or not prompt.strip():
            raise ValueError("Empty prompt: type a prompt first.")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend({"role": item["role"], "content": item["content"]} for item in history)
        content = prompt
        if attachments:
            content = [{"type": "text", "text": prompt}]
            for item in attachments:
                if item["type"].startswith("image/"):
                    content.append({"type": "image_url", "image_url": {"url": item["data"]}})
                elif item["type"] == "application/pdf":
                    content.append({"type": "file", "file": {"filename": item["name"], "file_data": item["data"]}})
                else:
                    content.append({"type": "text", "text": "Attached file: " + item["name"] + "\n" + item["text"]})
        messages.append({"role": "user", "content": content})
        payload = {"model": self.model, "messages": messages}
        if attachments and any(item["type"] == "application/pdf" for item in attachments):
            payload["plugins"] = [{"id": "file-parser", "pdf": {"engine": "cloudflare-ai"}}]
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if self.model == "openrouter/free":
            # The router may select a thinking model. Its hidden reasoning
            # shares max_tokens with the visible reply; 120 can yield no answer.
            # Minimal effort also works with models where reasoning is mandatory.
            payload["reasoning"] = {"effort": "minimal"}
            if max_tokens:
                payload["max_tokens"] = max(1024, max_tokens)
        if logprobs:
            payload["logprobs"] = True
            payload["top_logprobs"] = 1
        if stop:
            payload["stop"] = list(stop)
        if temperature is not None:
            payload["temperature"] = temperature
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read().decode(errors="replace")[:500]
                parsed = json.loads(raw) if raw else {}
                error = parsed.get("error") if isinstance(parsed, dict) else None
                if isinstance(error, dict) and error.get("message"):
                    detail = f": {error['message']}"
                elif raw:
                    detail = f": {raw}"
            except ValueError:
                detail = ""
            raise ProviderError(
                f"Live provider returned HTTP {exc.code}{detail}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"Could not reach live provider at {self.base_url}: {exc.reason}"
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            # socket.timeout is a separate class before Python 3.10.
            raise ProviderError("Live provider timed out after 60s.") from exc
        except OSError as exc:
            raise ProviderError(f"Connection to live provider failed: {exc}") from exc
        except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
            raise ProviderError(
                f"Live provider returned a response that is not valid JSON: {exc}"
            ) from exc
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(
                f"Live provider returned an unexpected response shape: {exc}"
            ) from exc
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("completion_tokens_details")
        details = details if isinstance(details, dict) else {}
        reasoning_tokens = _int_or_none(details.get("reasoning_tokens"))
        if not isinstance(text, str) or not text.strip():
            raise ProviderError(
                "Live provider returned no text content (null or empty message).",
                empty_content=True,
                reasoning_limited=(_first_choice(data).get("finish_reason") == "length"
                                   and reasoning_tokens is not None and reasoning_tokens > 0),
            )
        return Completion(
            text=text,
            completion_tokens=_int_or_none(usage.get("completion_tokens")),
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
            avg_logprob=_avg_logprob(data),
            finish_reason=_str_or_none(_first_choice(data).get("finish_reason")),
            response_model=_str_or_none(data.get("model")),
            reasoning_tokens=_reasoning_tokens(usage),
            max_tokens_requested=payload.get("max_tokens"),
        )


def _first_choice(data):
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return {}
    return choice if isinstance(choice, dict) else {}


def _str_or_none(value):
    return value if isinstance(value, str) and value else None


def _avg_logprob(data):
    """Mean logprob of generated tokens, or None if not reported.

    Chat-completions logprobs live at choices[0].logprobs.content[i].logprob.
    A missing, empty, or non-numeric list is not a confidence signal --
    it is None, and callers must treat it as "no signal".
    """
    try:
        content = data["choices"][0]["logprobs"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, list) or not content:
        return None
    values = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("logprob"), (int, float)):
            values.append(float(item["logprob"]))
    if not values:
        return None
    return sum(values) / len(values)


def _reasoning_tokens(usage):
    """Extract reasoning_tokens from completion_tokens_details, or None.

    OpenAI-compatible providers may report
    usage.completion_tokens_details.reasoning_tokens. These are INCLUDED
    in completion_tokens (not additive). None means "not reported".
    """
    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        return None
    return _int_or_none(details.get("reasoning_tokens"))


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
