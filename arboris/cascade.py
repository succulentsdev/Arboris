"""Confidence-gated cascade, done properly.

Rules, stated up front so they can be checked:
  1. The only confidence signal is the primary model's own token
     logprobs (mean logprob of the generated tokens). No signal is
     ever invented from answer length, word count, or any other proxy.
  2. If logprobs are unavailable (demo provider, or an endpoint that
     does not report them), the cascade does NOT escalate and does NOT
     claim confidence. It returns the primary answer with
     escalated=False and reason="logprobs unavailable".
  3. If mean logprob >= threshold, the primary answer stands.
  4. If mean logprob < threshold, the escalation model is called with
     the same system prompt and token cap, and its answer replaces the
     primary one. An escalation failure is raised, not silently
     downgraded to the low-confidence primary answer.
  5. A judge-model cascade is NOT implemented here. That would need a
     second model call with a judge prompt and a threshold for the
     judge's score, which is a separate, more expensive design.
  6. An optional transparent scorer (arboris.scorer, local, no
     model call) can also trigger escalation, and unlike logprobs it
     works when logprobs are unavailable. By default it only reports;
     it triggers escalation only when score_escalates is True.
  7. An optional output-token budget gate can stop escalation after
     an already-expensive primary answer. It never invents a shorter
     answer; it just keeps the primary one.

Threshold: --cascade-threshold, default -1.0 (mean token probability
about e^-1.0, roughly 37%). This is a starting point to tune against
real runs, not a sourced constant; BASELINE.md is where tuned values
should be recorded with their data.
"""

from dataclasses import dataclass

from arboris.providers import ProviderError


@dataclass
class CascadeResult:
    completion: object  # arboris.providers.Completion
    escalated: bool
    avg_logprob: object  # float or None
    reason: str
    primary_model: str
    escalation_model: str = ""
    score: object = None  # float or None: transparent scorer result, if scored
    score_reasons: tuple = ()


def _extract_score(scored):
    """Accept a float score or a scorer dict; return (score, reasons)."""
    if scored is None:
        return None, ()
    if isinstance(scored, dict):
        return scored.get("score"), tuple(scored.get("reasons") or ())
    return float(scored), ()


def run_cascade(primary, escalation, prompt, system=None, max_tokens=None,
                threshold=-1.0, score_fn=None, score_threshold=0.5,
                score_escalates=False, max_total_tokens=None,
                stop=None, temperature=None, reasoning_effort=None):
    """Run primary, escalate on low confidence, return a CascadeResult.

    Escalation triggers, in order:
    - mean logprob < threshold (the original rule), or
    - score_fn is provided, score_escalates is True, and the scorer
      score < score_threshold. This is the FrugalGPT-style path: it
      works even when logprobs are unavailable, because the scorer
      reads the prompt and the answer, not the logprobs.
    - max_total_tokens is a budget gate, not a trigger: if the primary
      answer already used more than that many output tokens, no
      escalation happens even when a trigger fired, because escalating
      would spend more on an already-expensive answer. None disables it.
    When score_fn is provided but score_escalates is False, the score
    is still computed and reported on the result; it just cannot
    trigger escalation. All new parameters default to the old
    behaviour, so existing callers are unchanged.
    """
    if threshold > 0:
        raise ValueError("cascade threshold must be <= 0 (logprobs are negative or zero)")
    # stop/temperature are forwarded only when set, so providers that
    # do not accept those parameters (test fakes, older wrappers) keep
    # working with the old call shape.
    extra = {}
    if stop is not None:
        extra["stop"] = stop
    if temperature is not None:
        extra["temperature"] = temperature
    if reasoning_effort is not None:
        extra["reasoning_effort"] = reasoning_effort
    primary_comp = primary.complete_ex(
        prompt, system=system, max_tokens=max_tokens, logprobs=True, **extra
    )
    avg = primary_comp.avg_logprob
    primary_name = getattr(primary, "model", getattr(primary, "name", "primary"))
    escalation_name = getattr(escalation, "model", getattr(escalation, "name", "escalation"))
    score, score_reasons = _extract_score(
        score_fn(prompt, primary_comp) if score_fn is not None else None
    )
    logprob_trigger = avg is not None and avg < threshold
    score_trigger = (
        score_escalates and score is not None and score < score_threshold
    )
    if not logprob_trigger and not score_trigger:
        if avg is None and score is None:
            reason = "logprobs unavailable: no escalation, primary answer stands"
        elif avg is None:
            reason = "logprobs unavailable, scorer did not trigger escalation"
        elif score is None:
            reason = f"mean logprob {avg:.3f} >= threshold {threshold}"
        else:
            reason = (f"mean logprob {avg:.3f} >= threshold {threshold}, "
                      f"score {score} >= {score_threshold}")
        return CascadeResult(
            completion=primary_comp, escalated=False, avg_logprob=avg,
            reason=reason, primary_model=primary_name,
            escalation_model=escalation_name, score=score,
            score_reasons=score_reasons,
        )
    # A trigger fired. The budget gate can still stop escalation.
    if (max_total_tokens is not None
            and primary_comp.completion_tokens is not None
            and primary_comp.completion_tokens > max_total_tokens):
        return CascadeResult(
            completion=primary_comp, escalated=False, avg_logprob=avg,
            reason=(f"escalation trigger fired but primary used "
                    f"{primary_comp.completion_tokens} output tokens, over "
                    f"budget {max_total_tokens}: primary answer stands"),
            primary_model=primary_name, escalation_model=escalation_name,
            score=score, score_reasons=score_reasons,
        )
    if logprob_trigger and score_trigger:
        why = (f"mean logprob {avg:.3f} < threshold {threshold} and "
               f"score {score} < {score_threshold}")
    elif logprob_trigger:
        why = f"mean logprob {avg:.3f} < threshold {threshold}: escalated"
    else:
        why = f"score {score} < {score_threshold}: escalated"
    # Low confidence: escalate. A failure here propagates (ProviderError
    # or ValueError) rather than silently returning the primary answer.
    esc_comp = escalation.complete_ex(prompt, system=system, max_tokens=max_tokens, **extra)
    return CascadeResult(
        completion=esc_comp, escalated=True, avg_logprob=avg, reason=why,
        primary_model=primary_name, escalation_model=escalation_name,
        score=score, score_reasons=score_reasons,
    )
