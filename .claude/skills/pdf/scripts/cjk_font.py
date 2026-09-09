"""Shared CJK font helpers for the PDF skill.

ReportLab can embed both standalone TrueType fonts and TrueType collections
(TTC). The production PDF path uses AR PL UKai CN, a free Kaiti-style CJK
font with Japanese kana and broad Han coverage.
"""

from __future__ import annotations

import os
from pathlib import Path

# AR PL UKai is a Kaiti-style TrueType collection. Subfont 0 is the CN flavor.
# Keep the path configurable so downstream images can relocate the font without
# patching the skill again.
REPORTLAB_CJK_FONT = Path(
    os.environ.get(
        "APITELEGRAMCHAT_REPORTLAB_CJK_FONT",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
    )
)
REPORTLAB_CJK_SUBFONT_INDEX = int(
    os.environ.get("APITELEGRAMCHAT_REPORTLAB_CJK_SUBFONT_INDEX", "0")
)
LO_CJK_FONT = Path(
    os.environ.get(
        "APITELEGRAMCHAT_CJK_FONT_FILE",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    )
)


def register_reportlab_cjk_font(name: str = "CJKKai") -> str:
    """Register the production Kaiti-style CJK font with ReportLab.

    The default resource is the first (CN) face in ``ukai.ttc``. ReportLab's
    TTFont parser supports TTC resources through ``subfontIndex``.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if not REPORTLAB_CJK_FONT.is_file():
        raise FileNotFoundError(
            f"Missing ReportLab Kaiti CJK font: {REPORTLAB_CJK_FONT}. "
            "Install the production `fonts-arphic-ukai` package instead of "
            "downloading a font at runtime."
        )

    pdfmetrics.registerFont(
        TTFont(
            name,
            str(REPORTLAB_CJK_FONT),
            subfontIndex=REPORTLAB_CJK_SUBFONT_INDEX,
        )
    )
    return name


def font_supports_text(text: str, name: str = "CJKKai") -> tuple[str, ...]:
    """Return characters missing from a registered ReportLab font."""
    font = pdfmetrics_get_font(name)
    char_widths = getattr(font.face, "charWidths", {})
    return tuple(dict.fromkeys(ch for ch in text if ord(ch) not in char_widths))


def pdfmetrics_get_font(name: str):
    """Resolve a registered ReportLab font without importing private internals."""
    from reportlab.pdfbase import pdfmetrics

    return pdfmetrics.getFont(name)


def assert_cjk_runtime() -> None:
    """Fail early when production CJK font resources are not present."""
    missing = [p for p in (REPORTLAB_CJK_FONT, LO_CJK_FONT) if not p.is_file()]
    if missing:
        paths = ", ".join(str(p) for p in missing)
        raise FileNotFoundError(f"Missing production CJK font resources: {paths}")
