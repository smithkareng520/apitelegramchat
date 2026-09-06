# =====================================================================
# tests/unit/test_markdown_converter.py — Markdown → Telegram Rich HTML
# =====================================================================
# 被测关键路径：模型输出 → 用户可见消息的渲染层。
# 覆盖：标题/强调/删除线/行内代码/代码块/链接/图片/列表/引用/表格/水平线、
#       HTML 实体幂等（不二次转义）、URL 与 snake_case 保护、混合 HTML 直通。
# =====================================================================
import html as html_lib

import pytest

from markdown_converter import (
    _escape_prose,
    convert_markdown_to_telegram_html as convert,
)


# ---------------------------------------------------------------------
# 透传与边界
# ---------------------------------------------------------------------
def test_plain_text_without_markdown_unchanged():
    assert convert("你好，世界") == "你好，世界"
    assert convert("2026-09-07 发布 v2.2.0") == "2026-09-07 发布 v2.2.0"


def test_empty_and_whitespace_returned_as_is():
    assert convert("") == ""
    assert convert("   \n  ") == "   \n  "


def test_a_b_c_guard_no_spurious_italic():
    # 星号夹在单词中间不应被转成斜体（历史 bug 场景）
    assert convert("a*b*c") == "a*b*c"


def test_snake_case_identifiers_not_italicized():
    assert convert("使用 `some_var_name` 与 some_other_name") == (
        "使用 <code>some_var_name</code> 与 some_other_name"
    )


# ---------------------------------------------------------------------
# 块级元素
# ---------------------------------------------------------------------
@pytest.mark.parametrize("level", range(1, 7))
def test_headings_all_levels(level):
    marks = "#" * level
    assert convert(f"{marks} 标题{level}") == f"<h{level}>标题{level}</h{level}>"


def test_heading_with_inline_bold():
    assert convert("# Title **bold**") == "<h1>Title <b>bold</b></h1>"


def test_horizontal_rule():
    assert convert("---") == "<hr/>"
    assert convert("***") == "<hr/>"


def test_unordered_list_various_markers():
    text = "- item1\n* item2\n+ item3"
    assert convert(text) == "<ul><li>item1</li><li>item2</li><li>item3</li></ul>"


def test_ordered_list():
    text = "1. 第一步\n2. 第二步"
    assert convert(text) == "<ol><li>第一步</li><li>第二步</li></ol>"


def test_blockquote_with_inline_format():
    text = "> 引用 **加粗**"
    assert convert(text) == "<blockquote>引用 <b>加粗</b></blockquote>"


def test_code_block_with_language_and_escaping():
    text = "```python\nprint('hi')\nprint('line2')\n```"
    expected_code = html_lib.escape("print('hi')\nprint('line2')")
    assert convert(text) == (
        f'<pre><code class="language-python">{expected_code}</code></pre>'
    )


def test_code_block_without_language():
    text = "```\nplain <code>\n```"
    assert convert(text) == f"<pre><code>{html_lib.escape('plain <code>')}</code></pre>"


def test_unterminated_code_block_passthrough():
    # 现行为：未闭合的代码围栏不满足完整 ```…``` 模式，整体透传不转换
    text = "```python\nprint(1)"
    assert convert(text) == text


def test_table_structure():
    text = "| Name | Value |\n|---|---|\n| a | 1 |"
    assert convert(text) == (
        "<table bordered striped><tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>a</td><td>1</td></tr></table>"
    )


def test_table_with_inline_markdown_in_cells():
    text = "| K | V |\n| - | - |\n| **bold** | `code` |"
    out = convert(text)
    assert "<th>K</th><th>V</th>" in out
    assert "<td><b>bold</b></td>" in out
    assert "<td><code>code</code></td>" in out


# ---------------------------------------------------------------------
# 行内元素
# ---------------------------------------------------------------------
def test_bold_variants():
    assert convert("**加粗**") == "<b>加粗</b>"
    assert convert("__加粗__") == "<b>加粗</b>"


def test_italic_variants():
    assert convert("*斜体*") == "<i>斜体</i>"
    assert convert("_斜体_") == "<i>斜体</i>"


def test_bold_italic_triple_star():
    assert convert("***重点***") == "<b><i>重点</i></b>"


def test_strikethrough():
    assert convert("~~废弃~~") == "<s>废弃</s>"


def test_code_span_with_pseudo_tag_no_placeholder_leak():
    # 回归：比较表达式曾被 <[^>]+> 误认成标签，回填后残留 \x00 占位符
    out = convert("运行 `a < b && c > d` 检查")
    assert "\x00" not in out
    assert out == "运行 <code>a &lt; b &amp;&amp; c &gt; d</code> 检查"


def test_code_span_with_real_tag_nested_unpark():
    # 行内代码内含真实 HTML 标签：占位符嵌套必须全部回填，无 \x00 残留
    out = convert("用 `<b>` 与 `</b>` 包裹")
    assert "\x00" not in out
    assert "<code>" in out and "<b>" in out


def test_inline_code_escapes_and_protects_content():
    assert convert("运行 `a < b && c > d`") == "运行 <code>a &lt; b &amp;&amp; c &gt; d</code>"
    # 行内代码内部的星号不参与强调解析
    assert convert("标记 `*not italic*`") == "标记 <code>*not italic*</code>"


def test_link_with_underscores_in_url():
    url = "https://example.com/a_b_c?x=1"
    assert convert(f"[我的链接]({url})") == f'<a href="{url}">我的链接</a>'


def test_link_text_escaped():
    assert convert("[a < b](https://e.com)") == '<a href="https://e.com">a &lt; b</a>'


def test_image_before_link():
    out = convert("![封面](https://img.example.com/pic_1.jpg)")
    assert out == '<img src="https://img.example.com/pic_1.jpg"/>'


def test_image_and_link_mixed():
    out = convert("![图](https://i.e.com/x.jpg) 与 [文](https://e.com/page_2)")
    assert '<img src="https://i.e.com/x.jpg"/>' in out
    assert '<a href="https://e.com/page_2">文</a>' in out


def test_mixed_html_and_markdown():
    text = "已有 <b>HTML</b> 与 **Markdown** 混排"
    assert convert(text) == "已有 <b>HTML</b> 与 <b>Markdown</b> 混排"


def test_pure_html_block_passthrough():
    text = "<p>完整 HTML 段落</p>"
    assert convert(text) == text


# ---------------------------------------------------------------------
# HTML 实体幂等（避免二次转义 — 用户报告的核心 bug）
# ---------------------------------------------------------------------
def test_escape_prose_bare_ampersand():
    assert _escape_prose("AT&T") == "AT&amp;T"


def test_escape_prose_preserves_existing_entities():
    assert _escape_prose("Tom &amp; Jerry") == "Tom &amp; Jerry"
    assert _escape_prose("&lt;tag&gt;") == "&lt;tag&gt;"
    assert _escape_prose("&#39;quoted&#39;") == "&#39;quoted&#39;"
    assert _escape_prose("&#x27;quoted&#x27;") == "&#x27;quoted&#x27;"


def test_escape_prose_angle_brackets():
    assert _escape_prose("a < b > c") == "a &lt; b &gt; c"


def test_bold_content_with_entity_survives_double_conversion():
    once = convert("**AT&T** 与 **Tom &amp; Jerry**")
    assert "<b>AT&amp;T</b>" in once
    assert "<b>Tom &amp; Jerry</b>" in once
    twice = convert(once)
    assert twice == once  # 幂等：转一次与转两次结果一致


def test_idempotency_on_rich_document():
    doc = (
        "# 标题 **加粗**\n\n"
        "正文 &amp; 符号 < 已转义\n\n"
        "- 列表 `code` 项\n"
        "> 引用 **重点**\n\n"
        "```python\nx = 1 < 2\n```\n\n"
        "[链接](https://e.com/a_b) — ~~删除~~"
    )
    once = convert(doc)
    assert convert(once) == once
