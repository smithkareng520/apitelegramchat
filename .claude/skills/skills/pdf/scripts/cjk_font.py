"""Font-free CJK helpers for the PDF skill (small-image friendly).

The Docker image installs **no font files**. ReportLab already ships the
Adobe CID-keyed CJK fonts as *references* (no font file needed): the PDF
stores the character codes and the viewer (Acrobat, Chrome, Edge, Preview,
iOS/Android viewers, ...) draws them with a CJK font installed on the
reader's machine. Nothing is embedded, so generated PDFs stay tiny.

Trade-offs to remember:
* The glyph shapes depend on the viewer's system font.
* Server-side rasterisation (pdftoppm / pdf2image) inside this image has no
  CJK font, so Chinese shows as blanks there. Verify text with
  ``pypdf`` / ``pdftotext`` instead of page images.

Optional escape hatch: if a deployment mounts a TrueType font and sets
``APITELEGRAMCHAT_REPORTLAB_CJK_FONT`` (and ``..._SUBFONT_INDEX`` for TTC),
that font is embedded instead. It is never required.
"""

from __future__ import annotations

import os
from pathlib import Path

# language -> built-in ReportLab CID font (no file on disk needed)
CID_FONTS = {
    "zh": "STSong-Light",         # Simplified Chinese (Adobe-GB1)
    "zh-tw": "MSung-Light",       # Traditional Chinese (Adobe-CNS1)
    "ja": "HeiseiMin-W3",         # Japanese (Adobe-Japan1)
    "ko": "HYSMyeongJo-Medium",   # Korean (Adobe-Korea1)
}
DEFAULT_CJK_FONT_NAME = "CJK"

_override = os.environ.get("APITELEGRAMCHAT_REPORTLAB_CJK_FONT", "").strip()
REPORTLAB_CJK_FONT = Path(_override) if _override else None
REPORTLAB_CJK_SUBFONT_INDEX = int(
    os.environ.get("APITELEGRAMCHAT_REPORTLAB_CJK_SUBFONT_INDEX", "0")
)


def register_reportlab_cjk_font(name: str = DEFAULT_CJK_FONT_NAME, lang: str = "zh") -> str:
    """Register a CJK font and return the name to use as ``fontName``.

    Uses the optional TrueType override when configured, otherwise the
    built-in CID font for ``lang`` (``zh``, ``zh-tw``, ``ja``, ``ko``); in that case the returned name is the
    CID font's own name (e.g. ``STSong-Light``), not ``name``.
    Also registers a font family so ``<b>``/``<i>`` inside Paragraph markup
    keep the CJK font instead of switching to Helvetica (which has no CJK).
    """
    from reportlab.pdfbase import pdfmetrics

    if REPORTLAB_CJK_FONT is not None:
        from reportlab.pdfbase.ttfonts import TTFont

        if not REPORTLAB_CJK_FONT.is_file():
            raise FileNotFoundError(
                f"APITELEGRAMCHAT_REPORTLAB_CJK_FONT points to a missing file: "
                f"{REPORTLAB_CJK_FONT}. Unset it to use the built-in CID font."
            )
        pdfmetrics.registerFont(
            TTFont(name, str(REPORTLAB_CJK_FONT), subfontIndex=REPORTLAB_CJK_SUBFONT_INDEX)
        )
    else:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        try:
            cid_name = CID_FONTS[lang]
        except KeyError:
            raise ValueError(f"lang must be one of {sorted(CID_FONTS)}, got {lang!r}") from None
        pdfmetrics.registerFont(UnicodeCIDFont(cid_name))
        name = cid_name  # built-in CID fonts are addressed by their own name
    pdfmetrics.registerFontFamily(name, normal=name, bold=name, italic=name, boldItalic=name)
    return name


def font_supports_text(text: str, name: str = DEFAULT_CJK_FONT_NAME) -> tuple[str, ...]:
    """Characters a *embedded* TrueType override cannot draw.

    Built-in CID fonts carry no glyph table, so coverage is up to the viewer;
    for them this returns ``()``.
    """
    from reportlab.pdfbase import pdfmetrics

    font = pdfmetrics.getFont(name)
    widths = getattr(getattr(font, "face", None), "charWidths", None)
    if not widths:
        return ()
    return tuple(dict.fromkeys(ch for ch in text if ord(ch) not in widths))


def assert_cjk_runtime() -> None:
    """Fail early only when an explicitly configured font file is missing."""
    if REPORTLAB_CJK_FONT is not None and not REPORTLAB_CJK_FONT.is_file():
        raise FileNotFoundError(f"Missing configured CJK font: {REPORTLAB_CJK_FONT}")


def register_fonts(cjk_name: str = DEFAULT_CJK_FONT_NAME, lang: str = "zh") -> str:
    """Register the CJK font; returns the font name to use in ``fontName=``.

    There is no emoji font any more: run text through
    ``emoji_font.to_fallback_markup()`` / ``safe_paragraph()`` /
    ``draw_mixed_string()`` so emoji are replaced or dropped instead of
    rendering as black boxes.

    ReportLab's default styles use Helvetica (no CJK). Always build styles
    with ``fontName=<returned name>``.
    """
    return register_reportlab_cjk_font(cjk_name, lang)
