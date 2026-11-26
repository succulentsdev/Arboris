"""The website inline script must only call functions it defines.

Regression guard: liveMetaRow called modeTag for months while no
definition existed, so every live answer threw before its meta row,
totals and persistence ran. The conformance script never caught it
because it only exercises the exported pure functions.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Genuine JS builtins and browser globals the page may call
# (including constructors used with `new`).
BUILTINS = {
    "AbortController", "Array", "Blob", "Boolean", "CustomEvent", "Date",
    "decodeURIComponent", "encodeURIComponent", "Error", "Event",
    "File", "FileReader", "FormData", "Headers", "Image", "isFinite",
    "isNaN", "JSON", "Map", "Math", "MutationObserver", "Number",
    "Object", "parseFloat", "parseInt", "Promise", "RangeError",
    "RegExp", "Request", "Response", "Set", "String", "Symbol",
    "TextDecoder", "TypeError", "Uint8Array", "URL", "URLSearchParams",
    "alert", "clearInterval", "clearTimeout", "fetch", "queueMicrotask",
    "requestAnimationFrame", "setInterval", "setTimeout",
    "structuredClone", "XMLHttpRequest",
}

KEYWORDS = {
    "catch", "else", "for", "function", "if", "in", "new", "return",
    "switch", "typeof", "var", "void", "while", "with", "yield",
}


def _strip_strings_and_comments(src):
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "\"'`":
            quote = c
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    break
                j += 1
            i = min(j + 1, n)
        else:
            out.append(c)
            i += 1
    return "".join(out)


class SiteCallsTests(unittest.TestCase):
    def test_inline_script_calls_only_defined_functions(self):
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        src = html.split("<script>", 1)[1].split("</script>", 1)[0]
        clean = _strip_strings_and_comments(src)
        defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", clean))
        defined |= set(re.findall(
            r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:function\b|\(|async\b)", clean))
        defined |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=\s*function\b", clean))
        called = set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", clean))
        called -= defined | BUILTINS | KEYWORDS
        self.assertEqual(called, set(), f"undefined calls in website script: {sorted(called)}")


if __name__ == "__main__":
    unittest.main()
