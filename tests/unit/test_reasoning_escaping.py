# =====================================================================
# tests/unit/test_reasoning_escaping.py — 思考内容转义回归测试
# =====================================================================
# 回归背景（2026-09 线上故障，见 问题.txt 日志）：
#
#   模型思考中会复述系统提示词的标签白名单（"Allowed tags: <b>, <p>…"），
#   旧版 _render_reasoning_html 直接把思考原文交给 Markdown 转换器：
#   「无 Markdown 语法」的文本被短路透传、形似标签的片段被当作"已有 HTML"
#   原样保留，嵌入 <details> 后形成几十层非法嵌套，Telegram 以 400
#   RICH_MESSAGE_DEPTH_INVALID 拒收整条消息（草稿判死 → 纯文本兜底）。
#   同时思考里的 <tg-button> 字样每帧 flush 都触发 sanitize_tg_buttons
#   的 WARNING（一次回复刷 26 条）。
#
# 本文件锁定修复后的行为：思考原文先幂等转义再走 Markdown 转换。
# =====================================================================
import html as html_lib
import logging
import re

import pytest

from ai.rich_message_builder import RichMessageBuilder, _render_reasoning_html


# 问题.txt 里模型思考原文的等价重构（复述标签白名单 + 提到 <tg-button>）
_LOG_LIKE_REASONING = (
    "We need to answer the question. Must follow system instructions: output "
    "format must be HTML with allowed tags, no markdown. Probably use <p> for "
    "paragraph, maybe <b> for emphasis. Use <tg-button> maybe not needed.\n\n"
    "Allowed tags: <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <del>, "
    "<tg-spoiler>, <code>, <mark>, <sub>, <sup>. Also block tags: <h1>..<h6>, "
    '<p>,</p><hr/><p>, <ul><li>, <ol><li>, <blockquote>, <details><summary>, '
    '<aside>, <footer>, <pre><code class="language-xxx">, <table> etc.\n\n'
    "That's it."
)

# 思考正文中允许出现的真实标签（由 Markdown 转换器生成）
_MARKDOWN_GENERATED = {"p", "b", "i", "s", "code", "pre", "ul", "ol", "li",
                       "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr",
                       "blockquote", "details", "summary", "a"}


def _raw_tag_names(html: str) -> set:
    return {m.group(1).lower()
            for m in re.finditer(r"<([A-Za-z][\w:-]*)", html)}


def _max_nesting_depth(html: str) -> int:
    """近似 Telegram 解析器的开标签栈深度（用于深度回归断言）。"""
    void = {"br", "hr", "img"}
    depth = max_depth = 0
    for m in re.finditer(r"<(/?)([A-Za-z][\w:-]*)[^>]*?(/?)>", html):
        tag = m.group(2).lower()
        if tag in void or m.group(3):
            continue
        if m.group(1):
            depth = max(0, depth - 1)
        else:
            depth += 1
            max_depth = max(max_depth, depth)
    return max_depth


def test_reasoning_tag_mentions_become_literals():
    """思考中复述的标签必须以实体字面量出现，而不是真实标签。"""
    out = _render_reasoning_html(_LOG_LIKE_REASONING)
    for mention in ("&lt;strong&gt;", "&lt;tg-spoiler&gt;", "&lt;mark&gt;",
                    "&lt;sub&gt;", "&lt;sup&gt;", "&lt;em&gt;", "&lt;ins&gt;",
                    "&lt;del&gt;", "&lt;aside&gt;", "&lt;footer&gt;",
                    "&lt;table&gt;", "&lt;tg-button&gt;", "&lt;hr/&gt;"):
        assert mention in out, f"缺少字面量 {mention}，输出：{out[:300]}"


def test_reasoning_no_unexpected_raw_tags():
    """渲染产物中不得出现 Markdown 转换器生成之外的任何真实标签。"""
    out = _render_reasoning_html(_LOG_LIKE_REASONING)
    leaked = _raw_tag_names(out) - _MARKDOWN_GENERATED
    assert not leaked, f"思考正文泄漏了未转义标签：{leaked}"


def test_reasoning_nesting_depth_bounded():
    """嵌入 <details> 后解析栈深度必须有界（旧版达 40 层 → DEPTH_INVALID）。"""
    body = _render_reasoning_html(_LOG_LIKE_REASONING)
    built = f"<details><summary>thinking</summary>\n{body}\n</details>"
    depth = _max_nesting_depth(built)
    assert depth <= 4, f"思考块嵌套深度 {depth} 超界，会触发 RICH_MESSAGE_DEPTH_INVALID"


def test_reasoning_tg_button_mention_no_warning(caplog):
    """思考里的 <tg-button> 字样不应再触发 sanitize_tg_buttons 的 WARNING。"""
    with caplog.at_level(logging.WARNING, logger="markdown_converter"):
        for _ in range(3):  # 模拟多帧 flush 重复渲染同一思考文本
            _render_reasoning_html(_LOG_LIKE_REASONING)
    btn = [r for r in caplog.records if "tg-button" in r.getMessage()]
    assert not btn, f"思考渲染触发了 {len(btn)} 条 tg-button WARNING（应为 0）"


def test_reasoning_already_escaped_entities_idempotent():
    """模型已按提示词输出的实体不得二次转义（旧无条件逐字符转义的缺陷）。"""
    out = _render_reasoning_html("看到 &lt;tg-button&gt; 与 &amp; 符号")
    assert "&lt;tg-button&gt;" in out
    assert "&amp;" in out
    assert "&amp;lt;" not in out       # 实体被二次转义
    assert "&amp;amp;" not in out


def test_reasoning_markdown_still_renders():
    """转义不得破坏思考中的 Markdown 渲染（粗体/行内代码/列表/围栏）。"""
    out = _render_reasoning_html("**结论** 与 `x < y`")
    assert "<b>结论</b>" in out
    assert "<code>x &lt; y</code>" in out

    out = _render_reasoning_html("- 第一条\n- 第二条")
    assert "<ul><li>第一条</li><li>第二条</li></ul>" in out

    out = _render_reasoning_html("```html\n<p>demo</p>\n```")
    assert "<pre><code" in out
    assert "&lt;p&gt;demo&lt;/p&gt;" in out      # 代码内容按字面量展示
    assert "<p>demo</p>" not in out              # 不得作为真实标签出现


def test_reasoning_comparison_expression_safe():
    """"a < b && c > d" 之类比较表达式必须完整转义（旧版 <b && c > 会被当标签）。"""
    out = _render_reasoning_html("判断 a < b && c > d 是否成立")
    assert "a &lt; b &amp;&amp; c &gt; d" in out
    # 去掉真实标签与已转义实体后，正文不得残留裸 <
    assert "<" not in re.sub(r"<[^>]*>", "", out).replace("&lt;", "")


def test_reasoning_summary_escapes_angle_brackets():
    """<summary> 摘要必须严格转义（docstring 契约；旧版裸 < 直达 <summary>）。"""
    summary = RichMessageBuilder._get_reasoning_summary(None, "x < 5, y > 3 & <b>粗体</b>")
    assert "<" not in summary
    assert "&lt;" in summary and "&gt;" in summary and "&amp;" in summary


def test_reasoning_summary_truncates_and_escapes():
    summary = RichMessageBuilder._get_reasoning_summary(None, "<i>" + "长" * 40)
    # 转义后字节长度会膨胀；可见字符数（unescape 后）不得超过 31（30 + 省略号）
    assert len(html_lib.unescape(summary)) <= 31
    assert "<i>" not in summary and "&lt;i&gt;" in summary


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_reasoning_empty_returns_empty(empty):
    assert _render_reasoning_html(empty) == ""


def test_tool_summaries_escape_html_injection():
    """工具 description/summary 是纯文本，不能注入 details 树结构。"""
    builder = RichMessageBuilder(123)
    idx = builder.start_new_tool_group()
    builder._tool_groups[idx]["outer_summary"] = '<details><summary>evil</summary></details>'
    builder._tool_groups[idx]["items"] = [{
        "id": "x",
        "summary": '</details><details><summary>evil',
        "details_html": "<p>ok</p>",
        "status": "done",
    }]
    out = builder._build_tool_group_html(builder._tool_groups[idx])
    assert "&lt;details&gt;" in out
    assert "</details><details><summary>evil" not in out
    assert out.count("<details>") == 2


def test_model_text_html_examples_are_literals():
    """最终回复里的 HTML 示例不能成为真正的嵌套 Rich Message 结构。"""
    from ai.rich_message_builder import _render_model_text_html
    out = _render_model_text_html(
        "示例：<details><summary>标题</summary><p><b>内容</b></p></details>\n\n"
        "同时保留 **Markdown 粗体**。"
    )
    assert "&lt;details&gt;" in out
    assert "&lt;summary&gt;" in out
    assert "&lt;b&gt;" in out
    assert "<b>Markdown 粗体</b>" in out
    assert out.count("<details>") == 0
