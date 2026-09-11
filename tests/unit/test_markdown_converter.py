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
    sanitize_tg_buttons,
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


def test_horizontal_rule_and_literal_triple_star():
    # `---` is treated as the explicit divider syntax supported by the rich-text layer.
    assert convert("---") == "<hr/>"
    # A standalone `***` is intentionally preserved as visible text.  Treating it as
    # `<hr/>` produces a message with no visible content in clients that render only
    # textual blocks, which looks like the message disappeared.
    assert convert("***") == "***"
    assert convert("前缀 *** 后缀") == "前缀 *** 后缀"


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
    # 代码内容转义与 _escape_prose 同语义：只转义裸 &、<、>；
    # pre/code 文本内容中的引号是普通字符，无需也不应转义为 &#x27;。
    expected_code = _escape_prose("print('hi')\nprint('line2')")
    assert convert(text) == (
        f'<pre><code class="language-python">{expected_code}</code></pre>'
    )


def test_code_block_without_language():
    text = "```\nplain <code>\n```"
    assert convert(text) == f"<pre><code>{_escape_prose('plain <code>')}</code></pre>"


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


def test_mixed_existing_code_block_and_markdown_afterwards():
    text = (
        "<pre><code>print(1)</code></pre>\n\n"
        "**基本信息**\n\n"
        "| 模块 | 内容 |\n|------|------|\n| **姓名** | 张三 |"
    )
    out = convert(text)
    assert '<pre><code>print(1)</code></pre>' in out
    assert '<b>基本信息</b>' in out
    assert '<table bordered striped>' in out
    assert '<td><b>姓名</b></td>' in out


def test_mixed_existing_details_and_markdown_afterwards():
    text = '<details open><summary>工具</summary><p>内容</p></details>\n\n**结果**'
    out = convert(text)
    assert '<details open><summary>工具</summary><p>内容</p></details>' in out
    assert '<b>结果</b>' in out


def test_mixed_existing_paragraph_and_markdown():
    text = '<p>已有 HTML</p>\n\n- item'
    out = convert(text)
    assert out == '<p>已有 HTML</p>\n\n<ul><li>item</li></ul>'


def test_unterminated_code_fence_does_not_parse_inner_markdown():
    text = '```python\n**not bold**'
    out = convert(text)
    assert '<b>not bold</b>' not in out
    assert '**not bold**' in out


def test_conversion_exception_has_readable_plaintext_fallback(monkeypatch):
    import markdown_converter as module

    def boom(_text):
        raise RuntimeError('simulated parser failure')

    monkeypatch.setattr(module, '_convert_mixed_document', boom)
    out = module.convert_markdown_to_telegram_html(
        '<p>说明</p> 链接 [文档](https://example.com) 与 **重点**'
    )
    assert '说明' in out
    assert 'https://example.com' in out
    assert '&lt;p&gt;' not in out
    assert '<b>' not in out


def test_nested_pre_inside_existing_details_keeps_structure():
    text = (
        '<details><summary>命令</summary>\n'
        '<pre><code>echo **not markdown**</code></pre>\n'
        '**结果**</details>'
    )
    out = convert(text)
    assert '<details><summary>命令</summary>' in out
    assert '<pre><code>echo **not markdown**</code></pre>' in out
    assert '<b>结果</b></details>' in out


def test_spoiler_and_autolink_markdown():
    out = convert('||秘密|| 与 <https://example.com>')
    assert '<tg-spoiler>秘密</tg-spoiler>' in out
    assert '<a href="https://example.com">https://example.com</a>' in out


# ---------------------------------------------------------------------
# 行内元素
# ---------------------------------------------------------------------
def test_bold_variants():
    assert convert("**加粗**") == "<b>加粗</b>"
    assert convert("__加粗__") == "<b>加粗</b>"

def test_multiple_bold_tokens_in_one_message():
    assert convert("**第一处** 和 **第二处**") == "<b>第一处</b> 和 <b>第二处</b>"


def test_bare_double_and_triple_stars_are_visible_text():
    assert convert("**") == "**"
    assert convert("***") == "***"
    assert convert("****") == "****"




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
    # 行内代码内含真实 HTML 标签：占位符嵌套必须全部回填，无 \x00 残留；
    # 标签按字面转义展示（&lt;b&gt;），不参与外层 HTML 解析。
    # （原断言 "<b>" in out 写反了：转义后的输出不可能含裸 <b>。）
    out = convert("用 `<b>` 与 `</b>` 包裹")
    assert "\x00" not in out
    assert "<code>" in out and "&lt;b&gt;" in out and "&lt;/b&gt;" in out


def test_inline_code_and_code_block_idempotent_under_second_pass():
    # 回归（sendRichMessage 双重转换）：_rich_message_html_payload 在发送前
    # 会对已转换 HTML 再跑一遍转换器。行内代码/代码块内容若用
    # html.escape 转义，已有实体 &lt; 会被二次转义成 &amp;lt;（用户看到
    # 字面量 "&lt;"）。修复后两遍转换结果必须完全一致。
    once = convert("<details><summary>使用 `&lt;tg-time&gt;` 标签</summary></details>")
    twice = convert(once)
    assert once == twice
    assert "&amp;lt;" not in twice
    assert "<code>&lt;tg-time&gt;</code>" in twice


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


def test_bare_unpaired_tg_button_escaped_to_literal():
    # 本次 bug 的直接场景：模型按用户要求原样输出裸 <tg-button>。
    # 必须转义为字面量文本，而不是原样透传给发送层。
    out = convert("<tg-button>\n</tg-button>")
    assert out == "&lt;tg-button&gt;\n&lt;/tg-button&gt;"
    assert "<tg-button" not in out

def test_valid_tg_button_url_passthrough():
    button = '<tg-button type="url" url="https://example.com" style="success">打开官网</tg-button>'
    assert convert(button) == button

def test_valid_tg_button_copy_text_passthrough():
    button = '<tg-button type="copy_text" text="复制的文本">复制</tg-button>'
    assert convert(button) == button

def test_tg_button_missing_url_escaped():
    out = convert('<tg-button type="url">打开官网</tg-button>')
    assert "&lt;tg-button" in out and "&lt;/tg-button&gt;" in out
    assert "<tg-button" not in out

def test_tg_button_missing_type_escaped():
    out = convert('<tg-button url="https://example.com">打开官网</tg-button>')
    assert out == '&lt;tg-button url="https://example.com"&gt;打开官网&lt;/tg-button&gt;'
    # 转义后内部文本保留，用户仍能看到按钮上的文字
    assert "打开官网" in out

def test_tg_button_empty_label_escaped():
    out = convert('<tg-button type="url" url="https://example.com"></tg-button>')
    assert "&lt;tg-button" in out
    assert "<tg-button" not in out

def test_tg_button_invalid_url_scheme_escaped():
    out = convert('<tg-button type="url" url="javascript:alert(1)">点我</tg-button>')
    assert "<tg-button" not in out
    assert "javascript" in out  # 原文以字面量形式保留

def test_tg_button_stray_close_escaped():
    assert convert("文字</tg-button>") == "文字&lt;/tg-button&gt;"

def test_tg_button_truly_unclosed_escaped_but_inner_kept():
    # 只有开标签、没有 </tg-button>：开标签转义为字面量，后面的文字保留
    out = convert('<tg-button type="url" url="https://example.com">没有闭合')
    assert out == '&lt;tg-button type="url" url="https://example.com"&gt;没有闭合'

def test_tg_button_nested_escaped_together():
    out = convert(
        "<tg-button><tg-button type=\"url\" url=\"https://x.com\">内层</tg-button></tg-button>"
    )
    assert "<tg-button" not in out
    assert "内层" in out

def test_tg_button_inside_details_escaped():
    # 接近真实失败消息的形态：AI 推理包在 <details> 里，末尾跟着裸按钮
    html = (
        "<details><summary>推理</summary><p>正文</p></details>\n"
        "<tg-button>\n</tg-button>"
    )
    out = convert(html)
    assert "<details><summary>推理</summary><p>正文</p></details>\n" in out
    assert "&lt;tg-button&gt;" in out
    assert "<tg-button>" not in out

def test_tg_button_escaped_already_idempotent():
    once = convert("<tg-button>\n</tg-button>")
    assert convert(once) == once
    assert "&amp;lt;" not in once

def test_tg_button_with_markdown_elsewhere_preserved():
    text = "点击 **这里**\n<tg-button type=\"url\" url=\"https://e.com\">按钮</tg-button>"
    out = convert(text)
    assert "<b>这里</b>" in out
    assert '<tg-button type="url" url="https://e.com">按钮</tg-button>' in out

def test_tg_button_entity_form_untouched():
    assert sanitize_tg_buttons("&lt;tg-button&gt;") == "&lt;tg-button&gt;"
    assert sanitize_tg_buttons("普通文本") == "普通文本"
    assert sanitize_tg_buttons("") == ""
