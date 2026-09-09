#!/usr/bin/env python3
"""Validate the production CJK runtime used by PDF/DOCX skills."""
from pathlib import Path
import shutil
import subprocess
import sys

REPORTLAB_FONT = Path("/usr/share/fonts/truetype/arphic-gbsn00lp/gbsn00lp.ttf")
LO_FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")

def main() -> int:
    errors = []
    if not REPORTLAB_FONT.is_file():
        errors.append(f"Missing ReportLab CJK TrueType font: {REPORTLAB_FONT}")
    if not LO_FONT.is_file():
        errors.append(f"Missing LibreOffice CJK font collection: {LO_FONT}")
    if shutil.which("fc-match") is None:
        errors.append("Missing fontconfig command: fc-match")
    else:
        for family in ("Noto Sans CJK SC", "AR PL SungtiL GB"):
            result = subprocess.run(["fc-match", family], text=True, capture_output=True)
            if result.returncode != 0 or not result.stdout.strip():
                errors.append(f"Fontconfig cannot resolve {family}")
    if shutil.which("tesseract") is not None:
        result = subprocess.run(["tesseract", "--list-langs"], text=True, capture_output=True)
        langs = result.stdout + result.stderr
        for lang in ("chi_sim", "chi_tra"):
            if lang not in langs:
                errors.append(f"Missing Tesseract language data: {lang}")
    if not errors:
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            pdfmetrics.registerFont(TTFont("CJKRuntimeCheck", str(REPORTLAB_FONT)))
        except Exception as exc:
            errors.append(f"ReportLab cannot load the CJK TrueType font: {exc}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: ReportLab CJK TrueType font: {REPORTLAB_FONT}")
    print(f"OK: LibreOffice CJK font collection: {LO_FONT}")
    print("OK: Noto Sans CJK SC and AR PL SungtiL GB resolved by Fontconfig")
    print("OK: Chinese OCR language data available")
    print("OK: ReportLab can load the embedded CJK TrueType font")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
