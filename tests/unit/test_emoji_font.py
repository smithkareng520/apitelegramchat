'''Tests for the font-free emoji sanitiser used by the PDF skill.'''

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(".claude/skills/pdf/scripts")


@pytest.fixture()
def emoji_font():
    sys.path.insert(0, str(SCRIPTS))
    import emoji_font

    return emoji_font


def test_repo_does_not_vendor_any_font():
    assert not (Path(".claude/skills/pdf") / "fonts").exists()


def test_common_status_emoji_become_plain_symbols(emoji_font):
    assert emoji_font.sanitize_text("完成 ✅ 失败 ❌ 注意 ⚠️") == "完成 √ 失败 × 注意 (!)"


def test_other_emoji_dropped_as_whole_clusters_and_reported(emoji_font):
    report = []
    out = emoji_font.sanitize_text("a 🚀 b 👍🏽 c 🇸🇬 d 👨‍👩‍👧‍👦 e", report)
    assert out == "a b c d e"
    assert report == ["🚀", "👍🏽", "🇸🇬", "👨‍👩‍👧‍👦"]


def test_keycap_and_heart_with_variation_selector(emoji_font):
    assert emoji_font.sanitize_text("1️⃣ 第一") == "1 第一"
    assert emoji_font.sanitize_text("❤️爱") == "爱"


def test_cjk_math_and_typographic_symbols_untouched(emoji_font):
    s = "中文abc→①±×÷≠≈★"
    assert emoji_font.sanitize_text(s) == s


def test_markup_is_escaped_unless_allowed(emoji_font):
    assert emoji_font.to_fallback_markup("a & <b> ✨") == "a &amp; &lt;b&gt; "
    from reportlab.lib.styles import getSampleStyleSheet

    st = getSampleStyleSheet()["Normal"]
    para = emoji_font.safe_paragraph("<b>x</b> 🚀", st, allow_markup=True)
    assert "x" in para.getPlainText()


def test_legacy_arguments_still_accepted(emoji_font):
    assert emoji_font.to_fallback_markup("好 ✅", "CJK", "EmojiMono", on_missing="drop") == "好 √"
    with pytest.raises(ValueError):
        emoji_font.to_fallback_markup("x", on_missing="nuke")


def test_ensure_emoji_coverage_lists_dropped(emoji_font):
    assert emoji_font.ensure_emoji_coverage("✅ 🚀 🚀") == ("🚀",)
