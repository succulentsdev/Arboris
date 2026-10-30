"""Brevity layer: trim a draft answer to a sentence budget.

Clause-level only. Whole filler sentences are dropped; words are never
deleted from inside a sentence, because that garbles meaning.

Structure is preserved: paragraph breaks, single line breaks, list items,
fenced code blocks, an email "Subject:" line and a sign-off all survive
trimming. List items and code blocks each count as one unit of the budget.
"""

import re

BUDGETS = {"sprout": 2, "canopy": 4, "full": 6}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "vs.", "dr.", "mr.", "mrs.", "ms.", "prof.",
    "st.", "inc.", "ltd.", "approx.", "fig.", "no.",
}
_LIST_ITEM = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")
_FREE_HEADER = re.compile(r"^\s*(?:subject|re)\s*:", re.IGNORECASE)
_SIGN_OFF = re.compile(
    r"^\s*(?:thanks|thank you|best|best regards|regards|kind regards|"
    r"sincerely|cheers)\s*[,.!]?\s*$",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```.*?```", re.DOTALL)
# A complete sentence ends in . ! ? (or an ellipsis), optionally followed
# by closing quotes, brackets or markdown emphasis markers.
_COMPLETE_END = re.compile(r"[.!?\u2026][\"'\u201d\u2019)\]*_]*$")
ELLIPSIS = "\u2026"

# Whole filler sentences. A sentence is dropped only if it matches in full,
# so "Please let me know if you have any questions about the report." is
# kept intact instead of being cut down to "about the report."
_FILLER_SENTENCES = [
    r"i hope you(?: are|'re) doing well[.!]?",
    r"please let me know if you have any questions[.!]?",
    r"feel free to ask if you have any (?:further |other )?questions[.!]?",
    r"hope this helps[.!]?",
    r"i hope this helps[.!]?",
    r"thanks so much for reaching out[.!]?",
    r"thank you so much in advance[.!]?",
    r"certainly[.!]",
    r"sure(?: thing)?[.!]",
    r"of course[.!]",
    r"absolutely[.!]",
    r"great question[.!]",
    r"that(?:'s| is) a great question[.!]",
    r"i(?:'d| would) be happy to help(?: with that)?[.!]",
    r"happy to help[.!]",
]
_FILLER_FULL = [re.compile(p, re.IGNORECASE) for p in _FILLER_SENTENCES]
_FILLER_LEAD = re.compile(
    r"^(?:as an ai(?: language model| assistant)?,\s*"
    r"|(?:certainly|sure|of course|absolutely)[,!]\s+(?:here(?:'s| is) (?:the answer|a brief explanation):\s*)?"
    r"|here(?:'s| is) a (?:concise|brief|quick) (?:answer|explanation|overview):\s*)",
    re.IGNORECASE,
)


def split_sentences(text):
    """Split text into sentences, dropping empties.

    Does not split after common abbreviations ("e.g.", "Dr.") or a single
    capital initial, nor before a lowercase letter.
    """
    pieces = [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]
    merged = []
    for piece in pieces:
        if merged:
            last_token = merged[-1].split()[-1].lower()
            is_initial = re.fullmatch(r"[A-Z]\.", merged[-1].split()[-1]) is not None
            if (
                last_token in _ABBREVIATIONS
                or is_initial
                or piece[0].islower()
            ):
                merged[-1] = merged[-1] + " " + piece
                continue
        merged.append(piece)
    return merged


def _is_filler(sentence):
    return any(p.fullmatch(sentence.strip()) for p in _FILLER_FULL)


def _clean_sentence(sentence):
    """Drop a leading 'As an AI language model,' and re-capitalise."""
    out = _FILLER_LEAD.sub("", sentence)
    if out != sentence and out:
        out = out[0].upper() + out[1:]
    return out


def strip_filler_clauses(text):
    """Remove whole filler sentences, keeping line structure."""
    lines_out = []
    for line in text.split("\n"):
        if _LIST_ITEM.match(line) or not line.strip():
            lines_out.append(line.rstrip())
            continue
        kept = [
            _clean_sentence(s)
            for s in split_sentences(re.sub(r"[ \t]+", " ", line))
            if not _is_filler(s)
        ]
        lines_out.append(" ".join(kept))
    return "\n".join(lines_out).strip()


def _paragraph_units(paragraph):
    """Return a paragraph as a list of lines, each a list of
    (text, counts_toward_budget) units."""
    lines = []
    after_sign_off = False
    for line in paragraph.split("\n"):
        if not line.strip():
            continue
        if after_sign_off and len(line.split()) <= 4 and not re.search(r"[.!?]$", line.strip()):
            # The signer's name under "Thanks," belongs to the sign-off.
            lines.append([(line.strip(), False, "keep")])
            after_sign_off = False
            continue
        after_sign_off = bool(_SIGN_OFF.match(line))
        if _LIST_ITEM.match(line):
            lines.append([(line.rstrip(), True, "\n")])
        elif _FREE_HEADER.match(line) or _SIGN_OFF.match(line):
            lines.append([(line.strip(), False, "\n")])
        else:
            cleaned = strip_filler_clauses(line)
            units = [(s, True, " ") for s in split_sentences(cleaned)]
            if units:
                lines.append(units)
    return lines


def apply_brevity(text, mode="full"):
    """Return text trimmed to the mode's sentence budget.

    Raises ValueError on an unknown mode or non-string input. Never
    returns a half-sentence, and never splits a fenced code block. See
    apply_brevity_with_flag for how a cut-off final sentence is handled.
    """
    return apply_brevity_with_flag(text, mode)[0]


def apply_brevity_with_flag(text, mode="full"):
    """Like apply_brevity, but returns (trimmed_text, truncated).

    A model that hits its token cap stops mid-sentence. If the last kept
    prose sentence does not end in . ! ? (closing quotes, brackets or
    emphasis markers allowed), it is dropped when another unit survives,
    or, when it is the only unit, kept with an ellipsis appended so the
    cut is visible. truncated is True in either case. List items, code
    blocks, headers and sign-offs are never treated as fragments.
    """
    if mode not in BUDGETS:
        raise ValueError(
            f"unknown mode {mode!r}; expected one of {sorted(BUDGETS)}"
        )
    if not isinstance(text, str):
        raise ValueError("answer text must be a string")

    # Swap code fences for placeholders so they stay whole.
    fences = []

    def _stash(match):
        fences.append(match.group(0))
        return f"\x00{len(fences) - 1}\x00"

    protected = _FENCE.sub(_stash, text)
    paragraphs = [
        p.strip() for p in re.split(r"\n\s*\n", protected.strip()) if p.strip()
    ]

    remaining = BUDGETS[mode]
    out_paragraphs = []  # paragraphs -> lines -> (text, counts, sep) units
    for paragraph in paragraphs:
        kept_lines = []
        for line in _paragraph_units(paragraph):
            kept_units = []
            for unit_text, counts, _sep in line:
                if counts:
                    if remaining <= 0:
                        break
                    remaining -= 1
                elif remaining <= 0 and _sep != "keep" and not _SIGN_OFF.match(unit_text):
                    # Free header: keep only while there is budget left.
                    break
                kept_units.append((unit_text, counts, _sep))
            if kept_units:
                kept_lines.append(kept_units)
        if kept_lines:
            out_paragraphs.append(kept_lines)

    truncated = _fix_trailing_fragment(out_paragraphs)
    result = "\n\n".join(
        "\n".join(" ".join(u[0] for u in line) for line in para)
        for para in out_paragraphs
    )
    for i, block in enumerate(fences):
        result = result.replace(f"\x00{i}\x00", block)
    return result, truncated


def _fix_trailing_fragment(paragraphs):
    """Drop or mark a cut-off final prose sentence in place. True if changed."""
    if not paragraphs:
        return False
    last_line = paragraphs[-1][-1]
    unit_text, counts, sep = last_line[-1]
    if (not counts or sep != " " or "\x00" in unit_text
            or _COMPLETE_END.search(unit_text.rstrip())):
        return False
    others = sum(u[1] for para in paragraphs for line in para for u in line) - 1
    if others > 0:
        last_line.pop()
        if not last_line:
            paragraphs[-1].pop()
            if not paragraphs[-1]:
                paragraphs.pop()
    else:
        last_line[-1] = (unit_text.rstrip() + ELLIPSIS, counts, sep)
    return True
