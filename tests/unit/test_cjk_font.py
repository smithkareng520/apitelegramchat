'''Tests for the font-free CJK helpers used by the PDF skill.'''

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(".claude/skills/pdf/scripts")


@pytest.fixture()
def cjk_font():
    sys.path.insert(0, str(SCRIPTS))
    import cjk_font

    return cjk_font


def test_builtin_cid_fonts_need_no_font_file(cjk_font):
    assert cjk_font.CID_FONTS["zh"] == "STSong-Light"
    assert cjk_font.REPORTLAB_CJK_FONT is None or cjk_font.REPORTLAB_CJK_FONT.suffix  # optional override only


def test_register_fonts_returns_usable_name_and_builds_pdf(cjk_font, tmp_path):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    from pypdf import PdfReader

    name = cjk_font.register_fonts()
    out = tmp_path / "t.pdf"
    style = ParagraphStyle("S", fontName=name, fontSize=12, leading=16)
    SimpleDocTemplate(str(out)).build([Paragraph("<b>你好</b>，世界", style)])
    assert "你好" in PdfReader(str(out)).pages[0].extract_text()
    assert out.stat().st_size < 20_000  # nothing embedded


def test_unknown_language_rejected(cjk_font):
    with pytest.raises(ValueError):
        cjk_font.register_reportlab_cjk_font(lang="xx")


def test_runtime_check_script_passes():
    import subprocess

    r = subprocess.run([sys.executable, str(SCRIPTS / "check_cjk_runtime.py")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
