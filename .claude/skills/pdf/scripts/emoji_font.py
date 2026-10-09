"""Emoji-safe text helpers for ReportLab (no emoji font required).

ReportLab cannot draw colour emoji and the image ships no emoji font, so any
emoji that reaches a Paragraph/canvas becomes a black box. These helpers make
text safe *before* it reaches ReportLab:

* common status emoji are replaced by plain symbols that CJK fonts have
  (``✅`` -> ``√``, ``❌`` -> ``×``, ``⚠️`` -> ``(!)`` ...);
* every other emoji (including ZWJ sequences, skin tones, flags, keycaps) is
  removed as one unit, and the removed text is recorded in ``missing_report``
  so callers can mention it;
* output is XML-escaped when it is going into a Paragraph.

Pure functions, standard library only.
"""

from __future__ import annotations

import re

DEFAULT_CJK_FONT_NAME = "CJK"

# Replacements for common emoji / dingbats (keys are single base characters;
# variation selectors are stripped before lookup).
REPLACEMENTS = {
    "✅": "√", "✔": "√", "☑": "√", "✓": "√", "🆗": "OK",
    "❌": "×", "✖": "×", "✗": "×", "✘": "×", "❎": "×", "🚫": "×",
    "⚠": "(!)", "❗": "!", "❕": "!", "❓": "?", "❔": "?",
    "⭐": "★", "🌟": "★",
    "➜": "->", "➡": "->", "⬅": "<-", "⬆": "↑", "⬇": "↓", "▶": ">", "◀": "<",
    "➕": "+", "➖": "-",
    "🔴": "●", "🟢": "●", "🟡": "●", "🔵": "●", "⚫": "●", "⚪": "○",
}

_VS = {0xFE0E, 0xFE0F}                      # variation selectors
_ZWJ = 0x200D
_KEYCAP = 0x20E3
_SKIN = range(0x1F3FB, 0x1F400)
_REGIONAL = range(0x1F1E6, 0x1F200)
_TAGS = range(0xE0020, 0xE0080)

# Blocks treated as emoji/pictographs when no replacement exists.
_EMOJI_BLOCKS = (
    (0x2190, 0x21FF),   # arrows (only the emoji-style ones are in REPLACEMENTS)
    (0x2300, 0x23FF), (0x2460, 0x24FF), (0x25A0, 0x25FF), (0x2600, 0x27BF),
    (0x2900, 0x297F), (0x2B00, 0x2BFF), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x1F000, 0x1FAFF),
)
# Of those, keep the ordinary typographic ones that CJK fonts normally have.
_KEEP = set("←↑→↓↔↕①②③④⑤⑥⑦⑧⑨⑩●○■□▲△▼▽◆◇★☆")


def _is_emoji_base(ch: str) -> bool:
    cp = ord(ch)
    if ch in _KEEP:
        return False
    return any(lo <= cp <= hi for lo, hi in _EMOJI_BLOCKS)


def _cluster_end(text: str, i: int) -> int:
    """Index just past the emoji cluster starting at ``i``."""
    n = len(text)
    cp = ord(text[i])
    j = i + 1
    if cp in _REGIONAL and j < n and ord(text[j]) in _REGIONAL:  # flag
        return j + 1
    while j < n:
        c = ord(text[j])
        if c in _VS or c in _SKIN or c == _KEYCAP or c in _TAGS:
            j += 1
        elif c == _ZWJ and j + 1 < n and (_is_emoji_base(text[j + 1]) or ord(text[j + 1]) in _VS):
            j += 2
        else:
            break
    return j


def sanitize_text(text: str, missing_report: list | None = None) -> str:
    """Replace or drop emoji so the text is safe for any non-emoji font."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        cp = ord(ch)
        # keycap sequence: digit/#/* + (VS) + U+20E3  ->  the plain character
        if ch in "0123456789#*":
            j = i + 1
            while j < n and ord(text[j]) in _VS:
                j += 1
            if j < n and ord(text[j]) == _KEYCAP:
                out.append(ch)
                i = j + 1
                continue
        if cp in _VS or cp == _ZWJ or cp in _SKIN or cp in _TAGS or cp == _KEYCAP:
            i += 1  # stray modifier
            continue
        if _is_emoji_base(ch) or cp in _REGIONAL:
            end = _cluster_end(text, i)
            cluster = text[i:end]
            if cp not in _REGIONAL and ch in REPLACEMENTS:
                out.append(REPLACEMENTS[ch])
            else:
                if missing_report is not None:
                    missing_report.append(cluster)
                # drop one adjacent space so "a 🚀 b" becomes "a b"
                if out and out[-1].endswith(" ") and end < n and text[end] == " ":
                    end += 1
                elif not out and end < n and text[end] == " ":
                    end += 1
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_XML_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def to_fallback_markup(
    text: str,
    base_font=None,
    emoji_font=None,
    on_missing: str = "drop",
    missing_report: list | None = None,
) -> str:
    """Sanitise ``text`` and XML-escape it for ``Paragraph``.

    ``base_font`` / ``emoji_font`` / ``on_missing`` are accepted only so old
    call sites keep working; they are ignored. The Paragraph's own style
    decides the font (use ``fontName`` from ``cjk_font.register_fonts()``).
    """
    if on_missing not in ("drop", "keep", "placeholder"):
        raise ValueError(f"invalid on_missing policy: {on_missing!r}")
    return sanitize_text(text, missing_report).translate(_XML_ESCAPES)


def safe_paragraph(text: str, style, missing_report: list | None = None, allow_markup: bool = False):
    """``Paragraph`` built from emoji-safe text.

    By default the text is XML-escaped (plain text in, safe output). Pass
    ``allow_markup=True`` when ``text`` already contains Paragraph tags such
    as ``<b>``; you are then responsible for escaping ``&``, ``<`` and ``>``
    inside the content.
    """
    from reportlab.platypus import Paragraph

    if allow_markup:
        return Paragraph(sanitize_text(text, missing_report), style)
    return Paragraph(to_fallback_markup(text, missing_report=missing_report), style)


def string_width_mixed(text: str, size: float, base_font: str = DEFAULT_CJK_FONT_NAME, **_ignored) -> float:
    """Width of the sanitised text in ``base_font``."""
    from reportlab.pdfbase import pdfmetrics

    return pdfmetrics.stringWidth(sanitize_text(text), base_font, size)


def draw_mixed_string(canvas, x: float, y: float, text: str, size: float,
                      base_font: str = DEFAULT_CJK_FONT_NAME, **_ignored) -> float:
    """Draw sanitised text on a canvas; returns the x after the last glyph."""
    from reportlab.pdfbase import pdfmetrics

    clean = sanitize_text(text)
    canvas.setFont(base_font, size)
    canvas.drawString(x, y, clean)
    return x + pdfmetrics.stringWidth(clean, base_font, size)


def ensure_emoji_coverage(text: str, **_ignored) -> tuple[str, ...]:
    """Emoji clusters that will be dropped (no replacement) from ``text``."""
    report: list[str] = []
    sanitize_text(text, report)
    return tuple(dict.fromkeys(report))
