"""Emoji font helpers for ReportLab PDF generation.

Root cause of emoji "乱码" (black boxes / notdef glyphs) in generated PDFs:

* ReportLab has **no automatic font fallback**. Every character is drawn
  with the single font selected for that text object; a glyph missing from
  that font renders as an empty box.
* The production CJK font (AR PL UKai / ``ukai.ttc``) contains **zero
  emoji glyphs**, so any emoji character (✅ ❌ 🎯 📊 ✨ 🚀 …) that reaches
  ReportLab turns into garbage.
* System color-emoji fonts (e.g. ``NotoColorEmoji.ttf``) use CBDT/CBLC
  bitmap outlines which ReportLab ``TTFont`` cannot embed, so they are not
  a solution either.

Fix provided by this module:

* A **monochrome** Noto Emoji TTF is installed into the production image by
  the ``Dockerfile`` (downloaded at build time, not committed to this
  repo — see the "Emoji handling" section of ``SKILL.md``). It has real
  TrueType ``glyf`` outlines, so ReportLab can embed it like any other font.
* ``register_emoji_font()`` registers it alongside the CJK font.
* ``to_fallback_markup()`` converts plain mixed text into ReportLab
  Paragraph markup, wrapping emoji runs in ``<font name="EmojiMono">``
  tags. Coverage is decided from the *actual registered fonts*, so
  characters that no font can render are dropped (configurable) instead
  of garbling the output. It accepts either a font name or a ReportLab
  ``ParagraphStyle`` for the base font.
* ``safe_paragraph()`` is the preferred high-level API: pass plain text and
  a normal ``ParagraphStyle`` and it handles the fallback markup for you.
* Font-run splitting is grapheme-cluster aware, so ZWJ emoji, variation
  selectors, skin-tone modifiers, and flags are kept together where possible.
* ``draw_mixed_string()`` / ``string_width_mixed()`` provide the same
  fallback for low-level ``canvas.drawString`` code, where markup tags
  are not available.
* ``strip_unrenderable()`` removes characters that no registered font
  covers (handy for canvas tables or pure-ASCII contexts).

All splitting logic is available as pure functions taking explicit
character-width maps, so it can be unit-tested without ReportLab.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Font discovery
# --------------------------------------------------------------------------

# Monochrome Noto Emoji. A *monochrome* TTF is required because ReportLab
# can only embed TrueType glyf outlines; the color CBDT font shipped for
# LibreOffice must never be fed to ReportLab.
#
# This font is NOT committed to the repo. The Dockerfile downloads it at
# build time (pinned version, verified checksum) into a system font
# directory — see the "Emoji handling" section of SKILL.md.
EMOJI_FONT_FILENAME = "NotoEmoji-Regular.ttf"

_EMOJI_FONT_CANDIDATES = (
    # 1. Explicit runtime override (set in the Docker image).
    os.environ.get("APITELEGRAMCHAT_REPORTLAB_EMOJI_FONT", ""),
    # 2. Production image path: installed by the Dockerfile into its own
    #    directory (kept separate from distro-managed font dirs so it is
    #    never shadowed or purged by an unrelated fontconfig package).
    "/usr/share/fonts/truetype/noto-emoji-mono/" + EMOJI_FONT_FILENAME,
    # 3. Distros that happen to package the monochrome emoji font under a
    #    standard path.
    "/usr/share/fonts/truetype/noto/" + EMOJI_FONT_FILENAME,
    "/usr/share/fonts/truetype/emoji/" + EMOJI_FONT_FILENAME,
    # 4. Local dev fallback: a copy placed next to this script's skill dir
    #    (e.g. manually downloaded for local testing outside Docker). Not
    #    part of the repo and not required for production.
    str(Path(__file__).resolve().parent.parent / "fonts" / EMOJI_FONT_FILENAME),
)

# Unicode blocks where the emoji font wins even when the CJK font also has
# the glyph: these codepoints are "emoji presentation" by default and the
# Kaiti dingbats look thin and dated next to modern text.
EMOJI_PREFERRED_BLOCKS = (
    (0x2300, 0x23FF),   # Misc Technical   (⏰ ⌛ ⏳ ⌚)
    (0x2600, 0x26FF),   # Misc Symbols     (⚠ ☀ ⚡ ☑ …)
    (0x2700, 0x27BF),   # Dingbats         (✅ ❌ ✨ ✔ ❗ ❓ ➜)
    (0x2B00, 0x2BFF),   # Misc Symbols & Arrows (⭐ ⬆ ⬛)
    (0x1F000, 0x1FAFF), # SMP emoji planes (🎯 📊 🚀 👍 🇨🇳 …)
)

DEFAULT_EMOJI_FONT_NAME = "EmojiMono"
DEFAULT_CJK_FONT_NAME = "CJKKai"


def resolve_emoji_font_path() -> Path:
    """Return the first existing monochrome emoji font candidate.

    Raises ``FileNotFoundError`` with actionable text when nothing is found.
    """
    for candidate in _EMOJI_FONT_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
    raise FileNotFoundError(
        "Missing monochrome emoji font for ReportLab. Checked: "
        + ", ".join(c for c in _EMOJI_FONT_CANDIDATES if c)
        + ". The Dockerfile downloads NotoEmoji-Regular.ttf into the image "
        "at build time (it is not committed to this repo); rebuild the "
        "image, or set APITELEGRAMCHAT_REPORTLAB_EMOJI_FONT to a local "
        "copy for dev/testing."
    )


def register_emoji_font(name: str = DEFAULT_EMOJI_FONT_NAME) -> str:
    """Register the monochrome emoji font (installed by the Dockerfile) with ReportLab."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    path = resolve_emoji_font_path()
    pdfmetrics.registerFont(TTFont(name, str(path)))
    return name


# --------------------------------------------------------------------------
# Coverage inspection
# --------------------------------------------------------------------------

def _font_char_widths(name: str) -> dict:
    """Character-width map of a registered ReportLab font."""
    from reportlab.pdfbase import pdfmetrics

    font = pdfmetrics.getFont(name)
    return getattr(font.face, "charWidths", {})


def font_missing_chars(text: str, name: str) -> tuple[str, ...]:
    """Characters in ``text`` that the named ReportLab font cannot render."""
    widths = _font_char_widths(name)
    return tuple(dict.fromkeys(ch for ch in text if ord(ch) not in widths))


def _in_emoji_preferred_blocks(codepoint: int) -> bool:
    return any(lo <= codepoint <= hi for lo, hi in EMOJI_PREFERRED_BLOCKS)


# --------------------------------------------------------------------------
# Pure run-splitting logic (unit-testable without ReportLab)
# --------------------------------------------------------------------------

def _iter_graphemes(text: str) -> list[str]:
    """Split text into Unicode grapheme clusters.

    ``regex`` is a small, dependency-light Unicode helper and gives us ``\\X``
    support. Keep a conservative codepoint fallback for isolated utility use.
    """
    try:
        import regex
    except ImportError:
        return list(text)
    return regex.findall(r"\X", text)


def split_font_runs(
    text: str,
    base_widths: dict,
    emoji_widths: dict,
    on_missing: str = "drop",
    missing_report: list | None = None,
) -> list[tuple[str, str]]:
    """Split ``text`` into ``(chunk, which_font)`` runs.

    ``which_font`` is ``"base"`` or ``"emoji"``. A character goes to the
    emoji font when the base font cannot render it, or when it lives in an
    emoji-preferred block and the emoji font covers it.

    ``on_missing`` controls characters covered by *neither* font:
      * ``"drop"``       – silently remove (default; a notdef box is worse)
      * ``"keep"``       – leave in a base run (will render as a box)
      * ``"placeholder"``– replace with ``U+FFFD``-free ``□`` if the base
        font covers it, otherwise drop.

    Characters dropped under ``"drop"``/``"placeholder"`` are appended to
    ``missing_report`` (when provided) so callers can log what was lost.
    """
    if on_missing not in ("drop", "keep", "placeholder"):
        raise ValueError(f"invalid on_missing policy: {on_missing!r}")

    runs: list[tuple[str, str]] = []

    def append(chunk: str, which: str) -> None:
        if not chunk:
            return
        if runs and runs[-1][1] == which:
            runs[-1] = (runs[-1][0] + chunk, which)
        else:
            runs.append((chunk, which))

    for cluster in _iter_graphemes(text):
        codepoints = tuple(ord(ch) for ch in cluster)
        base_ok = all(cp in base_widths for cp in codepoints)
        emoji_ok = all(cp in emoji_widths for cp in codepoints)
        has_emoji_preferred = any(_in_emoji_preferred_blocks(cp) for cp in codepoints)

        if emoji_ok and (not base_ok or has_emoji_preferred):
            append(cluster, "emoji")
            continue
        if base_ok:
            append(cluster, "base")
            continue
        if emoji_ok:
            append(cluster, "emoji")
            continue

        # Mixed-support clusters (rare, but possible for custom fonts): fall
        # back to codepoint-level routing rather than dropping the whole cluster.
        for ch in cluster:
            cp = ord(ch)
            ch_base_ok = cp in base_widths
            ch_emoji_ok = cp in emoji_widths
            if ch_emoji_ok and (not ch_base_ok or _in_emoji_preferred_blocks(cp)):
                append(ch, "emoji")
            elif ch_base_ok:
                append(ch, "base")
            else:
                if on_missing == "keep":
                    append(ch, "base")
                elif on_missing == "placeholder" and ord("□") in base_widths:
                    append("□", "base")
                else:
                    append("", "base")
                if missing_report is not None:
                    missing_report.append(ch)
    return runs


def strip_unrenderable_chars(
    text: str,
    base_widths: dict,
    emoji_widths: dict,
) -> str:
    """Remove characters that neither font can render."""
    return "".join(
        ch for ch in text
        if ord(ch) in base_widths or ord(ch) in emoji_widths
    )


# --------------------------------------------------------------------------
# ReportLab-facing helpers
# --------------------------------------------------------------------------

_XML_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def _resolve_base_font(base_font_or_style: object | str | None) -> str:
    """Resolve a ReportLab font name from a font name or ParagraphStyle-like object."""
    if base_font_or_style is None:
        return DEFAULT_CJK_FONT_NAME
    if isinstance(base_font_or_style, str):
        return base_font_or_style
    font_name = getattr(base_font_or_style, "fontName", None)
    if isinstance(font_name, str) and font_name:
        return font_name
    raise TypeError(
        "base_font must be a font name string or a ReportLab ParagraphStyle "
        "with a fontName attribute"
    )


def to_fallback_markup(
    text: str,
    base_font: str | object = DEFAULT_CJK_FONT_NAME,
    emoji_font: str = DEFAULT_EMOJI_FONT_NAME,
    on_missing: str = "drop",
    missing_report: list | None = None,
) -> str:
    """Convert mixed CJK/Latin/emoji text into safe Paragraph markup.

    ``base_font`` may be either a registered font name or a ReportLab
    ``ParagraphStyle``. Passing a style is recommended because it keeps the
    font configuration in one place. The result is XML-escaped and emoji runs
    are wrapped in
    ``<font name="EmojiMono">`` tags so ReportLab's Paragraph engine swaps
    fonts mid-string. Use it for every Paragraph that may contain emoji::

        markup = to_fallback_markup("进度 ✅ 100% 🚀")
        story.append(Paragraph(markup, styles["Normal"]))

    Requires both fonts to be registered first (see ``register_fonts`` in
    ``cjk_font.py`` / :func:`register_emoji_font`).
    """
    base_font = _resolve_base_font(base_font)
    base_widths = _font_char_widths(base_font)
    emoji_widths = _font_char_widths(emoji_font)
    runs = split_font_runs(
        text, base_widths, emoji_widths,
        on_missing=on_missing, missing_report=missing_report,
    )
    parts: list[str] = []
    for chunk, which in runs:
        escaped = chunk.translate(_XML_ESCAPES)
        if which == "emoji":
            parts.append(f'<font name="{emoji_font}">{escaped}</font>')
        else:
            parts.append(escaped)
    return "".join(parts)


def safe_paragraph(
    text: str,
    style: object,
    *,
    emoji_font: str = DEFAULT_EMOJI_FONT_NAME,
    on_missing: str = "drop",
    missing_report: list[str] | None = None,
) -> object:
    """Create a ReportLab Paragraph with automatic CJK/emoji font fallback.

    This is the preferred high-level API for new PDF code. Pass a normal
    ``ParagraphStyle`` after calling ``cjk_font.register_fonts()``; callers no
    longer need to manually build fallback markup or remember the base font
    name separately.
    """
    from reportlab.platypus import Paragraph

    markup = to_fallback_markup(
        text,
        style,
        emoji_font=emoji_font,
        on_missing=on_missing,
        missing_report=missing_report,
    )
    return Paragraph(markup, style)


def string_width_mixed(
    text: str,
    size: float,
    base_font: str | object = DEFAULT_CJK_FONT_NAME,
    emoji_font: str = DEFAULT_EMOJI_FONT_NAME,
    on_missing: str = "drop",
) -> float:
    """Width of mixed text when drawn with per-run font fallback."""
    from reportlab.pdfbase import pdfmetrics

    base_font = _resolve_base_font(base_font)
    base_widths = _font_char_widths(base_font)
    emoji_widths = _font_char_widths(emoji_font)
    total = 0.0
    for chunk, which in split_font_runs(text, base_widths, emoji_widths,
                                        on_missing=on_missing):
        font = emoji_font if which == "emoji" else base_font
        total += pdfmetrics.stringWidth(chunk, font, size)
    return total


def draw_mixed_string(
    canvas,
    x: float,
    y: float,
    text: str,
    size: float,
    base_font: str | object = DEFAULT_CJK_FONT_NAME,
    emoji_font: str = DEFAULT_EMOJI_FONT_NAME,
    on_missing: str = "drop",
) -> float:
    """Draw mixed text on a canvas, switching fonts per run.

    Returns the x position just after the last glyph, so callers can chain
    multiple strings or right-align by measuring with
    :func:`string_width_mixed` first.
    """
    from reportlab.pdfbase import pdfmetrics

    base_font = _resolve_base_font(base_font)
    base_widths = _font_char_widths(base_font)
    emoji_widths = _font_char_widths(emoji_font)
    for chunk, which in split_font_runs(text, base_widths, emoji_widths,
                                        on_missing=on_missing):
        font = emoji_font if which == "emoji" else base_font
        canvas.setFont(font, size)
        canvas.drawString(x, y, chunk)
        x += pdfmetrics.stringWidth(chunk, font, size)
    return x


def ensure_emoji_coverage(
    text: str,
    base_font: str = DEFAULT_CJK_FONT_NAME,
    emoji_font: str = DEFAULT_EMOJI_FONT_NAME,
) -> tuple[str, ...]:
    """Characters that will be dropped from ``text`` during rendering."""
    base_widths = _font_char_widths(base_font)
    emoji_widths = _font_char_widths(emoji_font)
    return tuple(dict.fromkeys(
        ch for ch in text
        if ord(ch) not in base_widths and ord(ch) not in emoji_widths
    ))
