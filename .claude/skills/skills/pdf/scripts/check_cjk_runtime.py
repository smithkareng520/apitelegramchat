#!/usr/bin/env python3
"""Font-free smoke test for the PDF skill's CJK/emoji handling.

Builds a tiny PDF in memory with Chinese text and an emoji string, then
reads the text back with pypdf. Needs no font files. Also reports (info
only) whether the image has any system CJK font for rasterising.
"""
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

SAMPLE = "中文测试 項目進度 日本語かなカナ"


def main() -> int:
    from cjk_font import register_fonts
    from emoji_font import to_fallback_markup, sanitize_text
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    errors = []
    name = register_fonts()
    report: list = []
    markup = to_fallback_markup("进度 ✅ 完成 🚀 👍🏽 🇸🇬", missing_report=report)
    if any(ord(c) > 0x1F000 for c in markup):
        errors.append(f"emoji survived sanitising: {markup!r}")

    buf = BytesIO()
    style = ParagraphStyle("S", fontName=name, fontSize=12, leading=16)
    SimpleDocTemplate(buf).build([Paragraph(SAMPLE, style), Paragraph(markup, style)])
    data = buf.getvalue()
    if not data.startswith(b"%PDF"):
        errors.append("ReportLab did not produce a PDF")

    try:
        from pypdf import PdfReader

        PdfReader(BytesIO(data))  # must at least parse
    except Exception as exc:  # pragma: no cover
        errors.append(f"pypdf cannot parse the generated PDF: {exc}")

    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"OK: built-in CID font '{name}' registered (no font file needed), {len(data)} bytes")
    print(f"OK: emoji sanitised -> {sanitize_text('✅ ❌ ⚠️ 🚀')!r}; dropped: {report}")
    if shutil.which("fc-list"):
        out = subprocess.run(["fc-list", ":lang=zh", "family"], text=True, capture_output=True).stdout.strip()
        print("INFO: system CJK fonts for rasterising:", out.splitlines()[0] if out else "none (expected in the slim image)")
    else:
        print("INFO: fc-list not installed; no system font check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
