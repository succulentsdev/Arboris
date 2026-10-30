"""No-model gate: answer pure arithmetic without calling a model.

This is the "don't default to the LLM" step from the efficiency
research. A prompt that is only an arithmetic expression has a
deterministic answer; sending it to a 31B model burns tokens, time,
and quota for a result a parser computes exactly, with 0 tokens and
0 estimated energy. FrugalGPT's lesson is the same shape: the cheapest
correct answer wins, and the cheapest correct answer here is not a
model at all.

Rules, deliberately strict so a wrong local answer is impossible by
construction:
- After stripping a leading "what is / calculate / compute / work
  out" and a trailing "?", the prompt must contain no letters at all,
  only digits, spaces, + - * / % ( ) and a decimal point.
- No "**" (exponent), no "//", no exponent notation, parentheses
  must balance, length at most 100 characters.
- Division or modulo by zero falls through to the model (returns
  None), never an error and never a guessed answer.
- Results are formatted as integers when integral, otherwise to
  6 significant digits. That rounding is stated, not hidden: a
  repeating decimal is an approximation, and the answer shows the
  equation so the user can check it.

Anything that fails these rules returns None, and the normal model
pipeline runs unchanged. This module never executes prompt text:
it parses it with a small recursive-descent parser.
"""

import re

_ALLOWED = re.compile(r"^[\d\s+\-*/().%]+$")
_PREFIX = re.compile(
    r"^(?:please\s+)?(?:what is|calculate|compute|work out|convert|how many|how much is)\s+",
    re.IGNORECASE,
)
_PERCENT_OF_RE = re.compile(
    r"^(-?\d+(?:\.\d+)?)\s*%\s*of\s+(-?\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)
_UNIT_CONV_RE = re.compile(
    r"^(-?\d+(?:\.\d+)?)\s*([a-zA-Z°]+)\s+(?:to|in|into|as)\s+([a-zA-Z°]+)$",
    re.IGNORECASE,
)

# Canonical unit aliases -> (category, canonical_unit)
_UNIT_ALIASES = {
    "km": ("len", "km"), "kilometer": ("len", "km"), "kilometers": ("len", "km"), "kilometre": ("len", "km"), "kilometres": ("len", "km"),
    "mi": ("len", "mi"), "mile": ("len", "mi"), "miles": ("len", "mi"),
    "m": ("len", "m"), "meter": ("len", "m"), "meters": ("len", "m"), "metre": ("len", "m"), "metres": ("len", "m"),
    "ft": ("len", "ft"), "foot": ("len", "ft"), "feet": ("len", "ft"),
    "cm": ("len", "cm"), "centimeter": ("len", "cm"), "centimeters": ("len", "cm"),
    "in": ("len", "in"), "inch": ("len", "in"), "inches": ("len", "in"),
    "mm": ("len", "mm"), "millimeter": ("len", "mm"), "millimeters": ("len", "mm"),
    "kg": ("mass", "kg"), "kilogram": ("mass", "kg"), "kilograms": ("mass", "kg"), "kilo": ("mass", "kg"), "kilos": ("mass", "kg"),
    "lb": ("mass", "lb"), "lbs": ("mass", "lb"), "pound": ("mass", "lb"), "pounds": ("mass", "lb"),
    "g": ("mass", "g"), "gram": ("mass", "g"), "grams": ("mass", "g"),
    "oz": ("mass", "oz"), "ounce": ("mass", "oz"), "ounces": ("mass", "oz"),
    "l": ("vol", "L"), "liter": ("vol", "L"), "liters": ("vol", "L"), "litre": ("vol", "L"), "litres": ("vol", "L"),
    "gal": ("vol", "gal"), "gallon": ("vol", "gal"), "gallons": ("vol", "gal"),
    "c": ("temp", "°C"), "°c": ("temp", "°C"), "celsius": ("temp", "°C"),
    "f": ("temp", "°F"), "°f": ("temp", "°F"), "fahrenheit": ("temp", "°F"),
    "k": ("temp", "K"), "kelvin": ("temp", "K"),
    "gb": ("data", "GB"), "gigabyte": ("data", "GB"), "gigabytes": ("data", "GB"),
    "mb": ("data", "MB"), "megabyte": ("data", "MB"), "megabytes": ("data", "MB"),
    "tb": ("data", "TB"), "terabyte": ("data", "TB"), "terabytes": ("data", "TB"),
}

# Base factors per category (to meters, grams, liters, MB)
_BASE_FACTOR = {
    "km": 1000.0, "m": 1.0, "cm": 0.01, "mm": 0.001,
    "mi": 1609.344, "ft": 0.3048, "in": 0.0254,
    "kg": 1000.0, "g": 1.0, "lb": 453.59237, "oz": 28.349523125,
    "L": 1.0, "gal": 3.785411784,
    "TB": 1048576.0, "GB": 1024.0, "MB": 1.0,
}


def _fmt_num(value):
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return format(value, ".6g")


def _try_unit_or_percent(expr):
    m_pct = _PERCENT_OF_RE.match(expr)
    if m_pct:
        pct = float(m_pct.group(1))
        total = float(m_pct.group(2))
        val = (pct / 100.0) * total
        return f"{_fmt_num(pct)}% of {_fmt_num(total)} = {_fmt_num(val)}"

    m_unit = _UNIT_CONV_RE.match(expr)
    if m_unit:
        num = float(m_unit.group(1))
        u1 = _UNIT_ALIASES.get(m_unit.group(2).lower())
        u2 = _UNIT_ALIASES.get(m_unit.group(3).lower())
        if not u1 or not u2 or u1[0] != u2[0]:
            return None
        cat, c1 = u1
        _, c2 = u2
        if c1 == c2:
            return f"{_fmt_num(num)} {c1} = {_fmt_num(num)} {c2}"
        if cat == "temp":
            if c1 == "°C":
                c_val = num
            elif c1 == "°F":
                c_val = (num - 32.0) * 5.0 / 9.0
            else:
                c_val = num - 273.15
            if c2 == "°C":
                out = c_val
            elif c2 == "°F":
                out = c_val * 9.0 / 5.0 + 32.0
            else:
                out = c_val + 273.15
            return f"{_fmt_num(num)} {c1} = {_fmt_num(out)} {c2}"
        base_val = num * _BASE_FACTOR[c1]
        out = base_val / _BASE_FACTOR[c2]
        return f"{_fmt_num(num)} {c1} = {_fmt_num(out)} {c2}"
    return None


def _clean(prompt):
    s = prompt.strip()
    s = _PREFIX.sub("", s)
    s = s.rstrip("?!").strip()
    return s


class _Parser:
    def __init__(self, text):
        self.tokens = re.findall(r"\d+(?:\.\d+)?|[+\-*/%()]", text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def parse(self):
        value = self._expr()
        if self.pos != len(self.tokens):
            raise ValueError("trailing tokens")
        return value

    def _expr(self):
        value = self._term()
        while self.peek() in ("+", "-"):
            op = self.take()
            rhs = self._term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def _term(self):
        value = self._factor()
        while self.peek() in ("*", "/", "%"):
            op = self.take()
            rhs = self._factor()
            if op == "*":
                value = value * rhs
            elif op == "/":
                if rhs == 0:
                    raise ZeroDivisionError
                value = value / rhs
            else:
                if rhs == 0:
                    raise ZeroDivisionError
                value = value % rhs
        return value

    def _factor(self):
        tok = self.peek()
        if tok == "-":
            self.take()
            return -self._factor()
        if tok == "(":
            self.take()
            value = self._expr()
            if self.take() != ")":
                raise ValueError("unbalanced parenthesis")
            return value
        if tok is not None and re.fullmatch(r"\d+(?:\.\d+)?", tok):
            self.take()
            return float(tok)
        raise ValueError("unexpected token")


def try_local_calc(prompt):
    """Return a local answer string for pure arithmetic, percentages, or unit conversions, or None.

    Examples:
      "What is 12 + 7?" -> "12 + 7 = 19"
      "What is 15% of 240?" -> "15% of 240 = 36"
      "Convert 100 km to miles" -> "100 km = 62.1371 mi"
    """
    expr = _clean(prompt)
    if not expr or len(expr) > 100:
        return None
    special = _try_unit_or_percent(expr)
    if special is not None:
        return special
    if "**" in expr or "//" in expr:
        return None
    if expr.count("(") != expr.count(")"):
        return None
    if not _ALLOWED.fullmatch(expr):
        return None
    if not any(op in expr for op in "+-*/%"):
        return None
    try:
        value = _Parser(expr).parse()
    except (ValueError, ZeroDivisionError):
        return None
    if not (value == value and abs(value) != float("inf")):
        return None
    shown = re.sub(r"\s+", " ", expr).strip()
    if abs(value - round(value)) < 1e-9:
        result = str(int(round(value)))
    else:
        result = format(value, ".6g")
    return f"{shown} = {result}"
