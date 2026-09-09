"""Tests for the emoji font fallback helpers used by the PDF skill.

ReportLab has no automatic font fallback and the production Kaiti CJK font
contains zero emoji glyphs, so generated PDFs garble emoji into boxes.
The skill now vendors a monochrome Noto Emoji TTF and ships coverage-driven
helpers; these tests pin that behavior.
"""

import sys
from pathlib import Path

import pytest

PDF_SKILL_DIR = Path(".claude/skills/pdf")
EMOJI_FONT_FILE = PDF_SKILL_DIR / "fonts" / "NotoEmoji-Regular.ttf"


@pytest.fixture()
def emoji_font_module():
    sys.path.insert(0, str(PDF_SKILL_DIR / "scripts"))
    import emoji_font

    return emoji_font


# ---------------------------------------------------------------------------
# Static / packaging checks
# ---------------------------------------------------------------------------

def test_vendored_emoji_font_exists_and_is_truetype():
    assert EMOJI_FONT_FILE.is_file(), "skill must vendor NotoEmoji-Regular.ttf"
    with EMOJI_FONT_FILE.open("rb") as fh:
        head = fh.read(4)
    assert head == b"\x00\x01\x00\x00", "must be a TrueType (glyf) font, not CFF/CBDT"


def test_emoji_font_module_prefers_env_then_vendored_copy(emoji_font_module):
    vendored = emoji_font_module.resolve_emoji_font_path()
    assert vendored.name == emoji_font_module.EMOJI_FONT_FILENAME
    # The vendored copy next to the script must be one of the candidates.
    assert vendored.is_file()


def test_emoji_preferred_blocks_cover_common_dingbats(emoji_font_module):
    for codepoint in (0x2705, 0x274C, 0x2728, 0x26A0, 0x2B50, 0x1F3AF, 0x1F4CA):
        assert emoji_font_module._in_emoji_preferred_blocks(codepoint)


# ---------------------------------------------------------------------------
# Pure run-splitting logic (no ReportLab needed)
# ---------------------------------------------------------------------------

def _widths(chars: str) -> dict:
    return {ord(ch): 600 for ch in chars}


BASE = _widths("中文完成失败abc123 →①★±×÷≠≈°□")
EMOJI = _widths("✅❌✨🎯📊🚀\u200d\ufe0f")


def test_split_font_runs_routes_emoji_to_emoji_font(emoji_font_module):
    runs = emoji_font_module.split_font_runs("完成✅失败❌", BASE, EMOJI)
    assert runs == [("完成", "base"), ("✅", "emoji"), ("失败", "base"), ("❌", "emoji")]


def test_split_font_runs_keeps_cjk_and_math_in_base_font(emoji_font_module):
    runs = emoji_font_module.split_font_runs("中文abc→①±×÷", BASE, EMOJI)
    assert all(which == "base" for _, which in runs)
    assert "".join(chunk for chunk, _ in runs) == "中文abc→①±×÷"


def test_split_font_runs_merges_adjacent_same_font_runs(emoji_font_module):
    runs = emoji_font_module.split_font_runs("✅✅❌", BASE, EMOJI)
    assert runs == [("✅✅❌", "emoji")]


def test_split_font_runs_drops_missing_by_default(emoji_font_module):
    missing = []
    runs = emoji_font_module.split_font_runs("ab\U0001F9FFc", BASE, EMOJI,
                                             missing_report=missing)
    assert "".join(chunk for chunk, _ in runs) == "abc"
    assert missing == ["\U0001F9FF"]  # U+1F9FF is in no font here


def test_split_font_runs_keep_and_placeholder_policies(emoji_font_module):
    text = "a\U0001F9FFb"
    kept = emoji_font_module.split_font_runs(text, BASE, EMOJI, on_missing="keep")
    assert "".join(chunk for chunk, _ in kept) == "a\U0001F9FFb"
    placed = emoji_font_module.split_font_runs(text, BASE, EMOJI,
                                               on_missing="placeholder")
    assert "".join(chunk for chunk, _ in placed) == "a□b"


def test_strip_unrenderable_chars(emoji_font_module):
    assert emoji_font_module.strip_unrenderable_chars("中文✅\U0001F9FF", BASE, EMOJI) == "中文✅"


def test_invalid_policy_rejected(emoji_font_module):
    with pytest.raises(ValueError):
        emoji_font_module.split_font_runs("x", BASE, EMOJI, on_missing="nuke")


# ---------------------------------------------------------------------------
# ReportLab integration (skipped when reportlab/font is unavailable)
# ---------------------------------------------------------------------------

def test_register_and_markup_end_to_end(emoji_font_module):
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph
    from reportlab.lib.styles import getSampleStyleSheet

    emoji_font_module.register_emoji_font()
    assert emoji_font_module.font_missing_chars("✅🎯📊", "EmojiMono") == ()

    # Register a base CJK font under the production name; the production
    # image has ukai.ttc, dev machines may fall back to any CJK TTF.
    import cjk_font  # from the skill scripts dir via sys.path

    if cjk_font.REPORTLAB_CJK_FONT.is_file():
        cjk_font.register_reportlab_cjk_font("CJKKai")
    else:
        for fallback in (
            "/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf",
            "/usr/share/fonts/truetype/lxgw-wenkai/LXGWWenKai-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/chinese/NotoSansSC[wght].ttf",
        ):
            if Path(fallback).is_file():
                pdfmetrics.registerFont(TTFont("CJKKai", fallback))
                break
        else:
            pytest.skip("no CJK font available for the base font in this env")

    markup = emoji_font_module.to_fallback_markup("进度✅ 100% <目标> & 🚀")
    assert '<font name="EmojiMono">✅</font>' in markup
    assert "&lt;目标&gt; &amp;" in markup  # XML escaping still applied
    # Paragraph must accept the markup without raising.
    Paragraph(markup, getSampleStyleSheet()["Normal"])

    # stringWidth on the emoji font is real (glyphs present, not notdef).
    assert pdfmetrics.stringWidth("✅", "EmojiMono", 12) > 0
    assert reportlab.Version  # touch to appease linters about the import
