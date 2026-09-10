"""Tests for the emoji font fallback helpers used by the PDF skill.

ReportLab has no automatic font fallback and the production Kaiti CJK font
contains zero emoji glyphs, so generated PDFs garble emoji into boxes.

The monochrome Noto Emoji TTF is NOT committed to this repo: the Dockerfile
downloads a pinned, checksum-verified copy into the image at build time (see
the "Emoji handling" section of SKILL.md). So the font is only guaranteed to
be present when running inside the built image (or APITELEGRAMCHAT_REPORTLAB_EMOJI_FONT
points at a local copy for dev/testing) — tests that need the actual font
bytes are skipped when neither is available, rather than asserting a vendored
file that no longer exists by design.
"""

import sys
from pathlib import Path

import pytest

PDF_SKILL_DIR = Path(".claude/skills/pdf")


@pytest.fixture()
def emoji_font_module():
    sys.path.insert(0, str(PDF_SKILL_DIR / "scripts"))
    import emoji_font

    return emoji_font


def _available_emoji_font_path(emoji_font_module):
    """Return the resolved font path, or None if not present in this env."""
    try:
        return emoji_font_module.resolve_emoji_font_path()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Static / packaging checks
# ---------------------------------------------------------------------------

def test_repo_does_not_vendor_the_emoji_font():
    """The font is installed by the Dockerfile, not committed to the repo."""
    assert not (PDF_SKILL_DIR / "fonts" / "NotoEmoji-Regular.ttf").exists(), (
        "NotoEmoji-Regular.ttf must not be committed under the skill directory; "
        "it is downloaded by the Dockerfile at build time instead"
    )


def test_emoji_font_is_truetype_when_available(emoji_font_module):
    path = _available_emoji_font_path(emoji_font_module)
    if path is None:
        pytest.skip("emoji font not installed in this environment (expected outside Docker)")
    with path.open("rb") as fh:
        head = fh.read(4)
    assert head == b"\x00\x01\x00\x00", "must be a TrueType (glyf) font, not CFF/CBDT"


def test_missing_font_raises_actionable_error(emoji_font_module, monkeypatch):
    # Force every candidate to miss so the error path itself is exercised
    # regardless of whether this environment happens to have the font.
    monkeypatch.setattr(emoji_font_module, "_EMOJI_FONT_CANDIDATES", ("",))
    with pytest.raises(FileNotFoundError, match="Dockerfile downloads"):
        emoji_font_module.resolve_emoji_font_path()


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


def test_base_font_can_be_resolved_from_style_like_object(emoji_font_module):
    class Style:
        fontName = "CJKKai"

    assert emoji_font_module._resolve_base_font(Style()) == "CJKKai"
    assert emoji_font_module._resolve_base_font("BodyFont") == "BodyFont"


def test_invalid_base_font_style_is_rejected(emoji_font_module):
    with pytest.raises(TypeError):
        emoji_font_module._resolve_base_font(object())


def test_split_font_runs_routes_emoji_to_emoji_font(emoji_font_module):
    runs = emoji_font_module.split_font_runs("完成✅失败❌", BASE, EMOJI)
    assert runs == [("完成", "base"), ("✅", "emoji"), ("失败", "base"), ("❌", "emoji")]


def test_split_font_runs_keeps_cjk_and_math_in_base_font(emoji_font_module):
    runs = emoji_font_module.split_font_runs("中文abc→①±×÷", BASE, EMOJI)
    assert all(which == "base" for _, which in runs)
    assert "".join(chunk for chunk, _ in runs) == "中文abc→①±×÷"




def test_split_font_runs_keeps_emoji_grapheme_clusters_together(emoji_font_module):
    base = _widths("ab□")
    emoji = _widths("👨👩👧👦\u200d")
    text = "a👨\u200d👩\u200d👧\u200d👦b"
    runs = emoji_font_module.split_font_runs(text, base, emoji)
    assert runs == [("a", "base"), ("👨\u200d👩\u200d👧\u200d👦", "emoji"), ("b", "base")]

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

    if _available_emoji_font_path(emoji_font_module) is None:
        pytest.skip("emoji font not installed in this environment (expected outside Docker)")

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

    styles = getSampleStyleSheet()
    styles["Normal"].fontName = "CJKKai"
    markup = emoji_font_module.to_fallback_markup("进度✅ 100% <目标> & 🚀", styles["Normal"])
    assert '<font name="EmojiMono">✅</font>' in markup
    assert "&lt;目标&gt; &amp;" in markup  # XML escaping still applied
    # Paragraph must accept the markup without raising.
    Paragraph(markup, styles["Normal"])
    para = emoji_font_module.safe_paragraph("完成 ✅", styles["Normal"])
    assert para is not None

    # stringWidth on the emoji font is real (glyphs present, not notdef).
    assert pdfmetrics.stringWidth("✅", "EmojiMono", 12) > 0
    assert reportlab.Version  # touch to appease linters about the import
