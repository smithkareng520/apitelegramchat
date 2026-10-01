from pathlib import Path
import inspect


def test_cjk_font_helper_uses_ttc_subfont():
    path = Path(".claude/skills/pdf/scripts/cjk_font.py")
    text = path.read_text(encoding="utf-8")
    assert "ukai.ttc" in text
    assert "subfontIndex=REPORTLAB_CJK_SUBFONT_INDEX" in text
    assert '"CJKKai"' in text


def test_reportlab_ttfont_accepts_ttc_subfont_index():
    from reportlab.pdfbase.ttfonts import TTFont

    assert "subfontIndex" in inspect.signature(TTFont).parameters


def test_sample_characters_are_documented_for_runtime_check():
    path = Path(".claude/skills/pdf/scripts/check_cjk_runtime.py")
    text = path.read_text(encoding="utf-8")
    assert "電撃焼約" in text
    assert "かなカナ" in text
