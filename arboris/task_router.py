"""Task-type routing: pick a model tier and a brevity mode by what the
prompt is asking for, not by how many words it has.

This is the replacement for word-count-only routing, and it is
deliberately rule-based and testable, not a learned classifier.
A learned router needs logged outcomes (which tier succeeded,
escalation rate, follow-up rate) that Arboris does not have yet;
rules can be read, argued with, and fixed from a single failing
example.

Task types, in priority order (first match wins):

- code: fenced code, or an explicit code request / debugging request.
- math: an explicit calculation, equation, or multi-number word problem.
- creative: story, poem, song, brainstorm, open-ended invention.
- reasoning: comparison, trade-off, evaluation, "should I", "why"
  with an argument to make. Comparison is reasoning, not a fact.
- steps: how-to / guide / tutorial requests.
- definition: "what is X", "define X", "what does X mean".
- fact: who / when / where / capital / author style lookups, and
  other short, single-answer questions.
- other: anything else. Falls back to word-count routing, so an
  unknown prompt is never forced into a wrong task type.

Tier is the requested model tier, not a guarantee: the server only
uses the small model when small-model routing is also enabled.
Mode is the brevity mode (sprout/canopy/full). Format is a short
suffix appended to the brevity system prompt when task routing is
on, so the model gets a task-appropriate shape, not just a
sentence count. Suffixes are kept under ten words so the system
prompt stays tiny.

Two cases the old word-count router got backwards, on purpose:

- A long, easy fact ("Could you please tell me, in your own words,
  what the capital of France is?") is still a fact: small tier,
  sprout mode, however many polite words surround it.
- A short, hard question ("Prove the Pythagorean theorem.") is
  still reasoning: primary tier, full mode, however few words it has.
"""

import re
from dataclasses import dataclass

from .router import route_mode

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_WORDS = re.compile(r"[A-Za-z0-9']+")


def _words(prompt):
    return len(_WORDS.findall(prompt))


def _has(prompt, *phrases):
    lowered = prompt.lower()
    return any(p in lowered for p in phrases)


def _count_numbers(prompt):
    return len(re.findall(r"\d+(?:\.\d+)?", prompt))


@dataclass(frozen=True)
class TaskRoute:
    task_type: str
    tier: str  # "small" or "primary": requested tier, see module docstring
    mode: str  # "sprout" | "canopy" | "full"
    format_suffix: str
    fallback: bool = False  # True when this came from word-count routing


_FORMAT = {
    "fact": "Answer with the fact directly, no preamble.",
    "definition": "Define it in one or two sentences.",
    "steps": "Use a short numbered list.",
    "reasoning": "Give the conclusion first, then the reasons briefly.",
    "math": "Show the calculation briefly, then the answer.",
    "code": "Give the code first, then a brief note.",
    "creative": "",
    "other": "",
}


def classify_task(prompt):
    """Return the task type for a prompt, or "other"."""
    prose = _FENCE.sub(" ", prompt)
    lowered = prose.lower()
    # Code first: a fenced block is code even if the words around it
    # look like a fact question ("what does this code do?").
    if (
        "```" in prompt
        or re.search(r"write a(?:n)? \w+ function", lowered)
        or _has(
            lowered, "write code", "write a function", "debug", "fix this code",
            "fix the bug", "refactor", "write a program", "write a script",
        )
    ):
        return "code"
    # Math: an explicit operator, a calculation verb with numbers, or
    # a word problem with several numbers asking "how many" / "in total".
    # This runs before fact, so a multi-number word problem is math,
    # not a lookup, even though it contains "how many".
    if (
        re.search(r"\d+\s*[+\-*/^%]\s*\d+", prose)
        or _has(
            lowered, "calculate", "solve for", "solve the equation",
            "what is the derivative", "what is the integral",
        )
        or (_count_numbers(prose) >= 3 and _has(lowered, "how many", "in total"))
    ):
        return "math"
    # Specific fact phrases run before generic "what is", so "what is
    # the capital of France?" is a fact, while "what is DNS?" is a
    # definition. Generic fact markers are checked after definition.
    if _has(lowered, "capital of", "who wrote", "who invented", "what year", "which country"):
        return "fact"
    # "story" and "poem" need word boundaries: "history" contains
    # "story" as a substring, and must not classify as creative.
    if _has(
        lowered, "write a story", "write a poem", "write a song",
        "brainstorm", "give me ideas", "invent a",
    ) or re.search(r"\b(story|poem)\b", lowered) or re.search(
        r"\b(?:write|compose|create|draft|make|sing)\b.{0,80}\b(?:haiku|song|lyrics|sonnet|limerick)\b", lowered
    ):
        return "creative"
    # Comparison and evaluation markers come before steps, so "how to
    # compare X and Y" is reasoning, not a how-to. "Step by step" and
    # "in detail" are deliberately NOT here: they are steps complexity
    # signals, handled in route_task, not a task type on their own.
    if _has(
        lowered, "compare", "pros and cons", "versus", "vs.", "trade-off",
        "trade-offs", "which is better", "should i", "evaluate",
        "analyse", "analyze", "explain why", "why does", "why do",
        "prove", "derive",
    ):
        return "reasoning"
    if _has(
        lowered, "how to", "how do i", "how can i", "steps to",
        "guide to", "tutorial", "instructions for",
    ):
        return "steps"
    if _has(lowered, "what is", "what does", "define", "meaning of") or lowered.startswith("explain "):
        return "definition"
    if _has(
        lowered, "who", "when", "where", "capital of", "who wrote",
        "who invented", "how many", "what year", "which country",
    ):
        return "fact"
    return "other"


def route_task(prompt):
    """Route a prompt by task type.

    Returns a TaskRoute. For "other", mode comes from word-count
    routing and fallback is True, so unknown prompts behave exactly
    as they did before task routing existed.
    """
    task_type = classify_task(prompt)
    prose = _FENCE.sub(" ", prompt)
    words = _words(prose)
    numbers = _count_numbers(prose)
    lowered = prose.lower()

    if task_type == "fact":
        return TaskRoute("fact", "small", "sprout", _FORMAT["fact"])
    if task_type == "definition":
        mode = "sprout" if words <= 12 else "canopy"
        return TaskRoute("definition", "small", mode, _FORMAT["definition"])
    if task_type == "steps":
        complex_steps = (
            words >= 35 or "?" in prose and prose.count("?") >= 2
            or _has(lowered, "in detail", "step by step")
        )
        if complex_steps:
            return TaskRoute("steps", "primary", "full", _FORMAT["steps"])
        return TaskRoute("steps", "small", "canopy", _FORMAT["steps"])
    if task_type == "reasoning":
        # Comparisons get a table, not a prose list: the format follows
        # the task subtype, not just the task type. Other reasoning
        # keeps the conclusion-first suffix.
        if _has(lowered, "compare", "versus", "vs.", "pros and cons",
                "which is better"):
            return TaskRoute("reasoning", "primary", "full",
                             "Give the conclusion first, then a comparison table.")
        return TaskRoute("reasoning", "primary", "full", _FORMAT["reasoning"])
    if task_type == "math":
        # One number pair and one operation is simple; a word problem
        # with several numbers, or an explicit multi-step request, is not.
        complex_math = numbers >= 3 or _has(lowered, "step by step", "show your working")
        if complex_math:
            return TaskRoute("math", "primary", "full", _FORMAT["math"])
        return TaskRoute("math", "small", "canopy", _FORMAT["math"])
    if task_type == "code":
        if _has(lowered, "debug", "fix", "error", "bug"):
            return TaskRoute("code", "primary", "full", _FORMAT["code"])
        return TaskRoute("code", "primary", "canopy", _FORMAT["code"])
    if task_type == "creative":
        return TaskRoute("creative", "primary", "full", _FORMAT["creative"])
    # other: word-count fallback, exactly as before.
    mode = route_mode(prompt)
    tier = "small" if mode == "sprout" else "primary"
    return TaskRoute("other", tier, mode, _FORMAT["other"], fallback=True)


# RouteLLM-style difficulty score: a single 0-1 number, so the routing
# threshold is tunable instead of baked into the task types. This is
# a feature-based score, not a learned router: it has no training
# data, and it says so. It is calibrated to agree with route_task's
# tiers at threshold 0.5 (see the golden-set invariant test), so
# turning difficulty routing on with the default threshold changes
# nothing; moving the threshold is what moves traffic between tiers.
# Quality targets are presets for that threshold: eco 0.7 (more
# small-model answers), balanced 0.5 (the task-type tiers as-is),
# quality 0.3 (more primary answers).
_DIFFICULTY_BASE = {
    "fact": 0.1, "definition": 0.25, "steps": 0.4, "math": 0.4,
    "code": 0.75, "reasoning": 0.85, "creative": 0.85,
}
_DIFFICULTY_OTHER = {"sprout": 0.4, "canopy": 0.55, "full": 0.7}
QUALITY_THRESHOLDS = {"eco": 0.7, "balanced": 0.5, "quality": 0.3}
# Task temperatures, opt-in only (ARBORIS_TASK_TEMPERATURE).
# Factual tasks get a low temperature for consistency; creative gets
# a high one for variety; steps/reasoning sit in the middle. "other"
# gets no temperature (provider default), because an unknown task
# type has no basis for a temperature choice. These are starting
# points to judge live, not sourced constants.
TASK_TEMPERATURES = {
    "fact": 0.2, "definition": 0.2, "math": 0.2, "code": 0.2,
    "steps": 0.5, "reasoning": 0.5, "creative": 0.8,
}


def difficulty_score(prompt):
    """Return a 0-1 difficulty score for a prompt.

    Small means the small model is expected to suffice; large means
    the primary model. At threshold 0.5 the score reproduces
    route_task's tier for every golden case.
    """
    task_type = classify_task(prompt)
    if task_type == "other":
        return _DIFFICULTY_OTHER[route_mode(prompt)]
    prose = _FENCE.sub(" ", prompt)
    words = _words(prose)
    numbers = _count_numbers(prose)
    lowered = prose.lower()
    score = _DIFFICULTY_BASE[task_type]
    if words >= 35:
        score += 0.2
    elif words <= 12 and task_type in ("fact", "definition"):
        score -= 0.1
    if prose.count("?") >= 2:
        score += 0.15
    if numbers >= 3:
        score += 0.1
    if _has(lowered, "in detail", "step by step", "show your working"):
        score += 0.1
    if task_type == "code" and _has(lowered, "debug", "fix", "error", "bug"):
        score += 0.1
    return round(max(0.0, min(1.0, score)), 2)
