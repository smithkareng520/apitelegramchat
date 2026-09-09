"""Shared CJK font helpers for the PDF skill.

ReportLab requires a TrueType-outline font for this workflow. The production
image therefore uses Arphic Song for PDF embedding and Noto Sans CJK SC for
LibreOffice/DOCX rendering.
"""

from __future__ import annotations

import os
from pathlib import Path

REPORTLAB_CJK_FONT = Path(
    os.environ.get(
        "APITELEGRAMCHAT_REPORTLAB_CJK_FONT",
        "/usr/share/fonts/truetype/arphic-gbsn00lp/gbsn00lp.ttf",
    )
)
LO_CJK_FONT = Path(
    os.environ.get(
        "APITELEGRAMCHAT_CJK_FONT_FILE",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    )
)


def register_reportlab_cjk_font(name: str = "CJKSong") -> str:
    """Register the production TrueType CJK font with ReportLab."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if not REPORTLAB_CJK_FONT.is_file():
        raise FileNotFoundError(
            f"Missing ReportLab CJK font: {REPORTLAB_CJK_FONT}. "
            "Install the production font package instead of downloading a font at runtime."
        )

    pdfmetrics.registerFont(TTFont(name, str(REPORTLAB_CJK_FONT)))
    return name


def assert_cjk_runtime() -> None:
    """Fail early when production CJK font resources are not present."""
    missing = [p for p in (REPORTLAB_CJK_FONT, LO_CJK_FONT) if not p.is_file()]
    if missing:
        paths = ", ".join(str(p) for p in missing)
        raise FileNotFoundError(f"Missing production CJK font resources: {paths}")
