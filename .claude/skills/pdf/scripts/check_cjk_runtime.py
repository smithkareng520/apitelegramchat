#!/usr/bin/env python3
"""Validate the production CJK/emoji runtime used by PDF/DOCX skills."""
from pathlib import Path
import shutil
import subprocess
import sys

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from emoji_font import EMOJI_FONT_FILENAME, resolve_emoji_font_path  # noqa: E402

REPORTLAB_FONT = Path("/usr/share/fonts/truetype/arphic/ukai.ttc")
LO_FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
REPORTLAB_SUBFONT_INDEX = 0
SAMPLE = "日文かなカナ電撃焼約漢字・ー"
EMOJI_SAMPLE = "✅❌✨🎯📊🚀⭐⚠✔"


def main() -> int:
    errors = []
    emoji_font_path = None
    try:
        emoji_font_path = resolve_emoji_font_path()
    except FileNotFoundError as exc:
        errors.append(str(exc))
    if not REPORTLAB_FONT.is_file():
        errors.append(f"Missing ReportLab Kaiti CJK TrueType Collection: {REPORTLAB_FONT}")
    if not LO_FONT.is_file():
        errors.append(f"Missing LibreOffice CJK font collection: {LO_FONT}")
    if shutil.which("fc-match") is None:
        errors.append("Missing fontconfig command: fc-match")
    else:
        for family in ("Noto Sans CJK SC", "AR PL UKai CN", "AR PL SungtiL GB", "Noto Color Emoji"):
            result = subprocess.run(["fc-match", family], text=True, capture_output=True)
            if result.returncode != 0 or not result.stdout.strip():
                errors.append(f"Fontconfig cannot resolve {family}")
    if shutil.which("tesseract") is not None:
        result = subprocess.run(["tesseract", "--list-langs"], text=True, capture_output=True)
        langs = result.stdout + result.stderr
        for lang in ("chi_sim", "chi_tra"):
            if lang not in langs:
                errors.append(f"Missing Tesseract language data: {lang}")

    if REPORTLAB_FONT.is_file() and not errors:
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            pdfmetrics.registerFont(
                TTFont(
                    "CJKRuntimeCheck",
                    str(REPORTLAB_FONT),
                    subfontIndex=REPORTLAB_SUBFONT_INDEX,
                )
            )
            font = pdfmetrics.getFont("CJKRuntimeCheck")
            char_widths = getattr(font.face, "charWidths", {})
            missing = "".join(ch for ch in SAMPLE if ord(ch) not in char_widths)
            if missing:
                errors.append(f"ReportLab Kaiti font is missing sample glyphs: {missing}")

            # Emoji font: must be a TrueType glyf font embeddable by ReportLab.
            if emoji_font_path is not None:
                try:
                    pdfmetrics.registerFont(TTFont("EmojiRuntimeCheck", str(emoji_font_path)))
                    emoji_font = pdfmetrics.getFont("EmojiRuntimeCheck")
                    emoji_widths = getattr(emoji_font.face, "charWidths", {})
                    missing_emoji = "".join(
                        ch for ch in EMOJI_SAMPLE if ord(ch) not in emoji_widths
                    )
                    if missing_emoji:
                        errors.append(
                            f"Monochrome emoji font is missing sample glyphs: {missing_emoji}"
                        )
                except Exception as exc:
                    errors.append(f"ReportLab cannot load the emoji font: {exc}")
        except Exception as exc:
            errors.append(f"ReportLab cannot load the Kaiti CJK font: {exc}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: ReportLab Kaiti CJK TTC: {REPORTLAB_FONT} [subfont {REPORTLAB_SUBFONT_INDEX}]")
    print(f"OK: Monochrome emoji font for ReportLab: {emoji_font_path} ({EMOJI_FONT_FILENAME})")
    print(f"OK: LibreOffice CJK font collection: {LO_FONT}")
    print("OK: AR PL UKai CN, Noto Sans CJK SC, AR PL SungtiL GB, and Noto Color Emoji resolved by Fontconfig")
    print("OK: Japanese kana and representative Han glyphs present in the ReportLab font")
    print(f"OK: Emoji sample glyphs present in the monochrome emoji font ({EMOJI_SAMPLE})")
    print("OK: Chinese OCR language data available")
    print("OK: ReportLab can load and embed the Kaiti TrueType collection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
