# -*- coding: utf-8 -*-
"""验证「游离媒体标签中和 + 级联兜底」修复的测试脚本。

背景（2026-09-05 线上案例）：
  模型在回答正文写了  使用 `<tg-document>` 标签发送：  —— 裸的
  <tg-document> 字面量（无 src、无闭合）被 Telegram 当真实标签解析，
  整条消息以 RICH_MESSAGE_DOCUMENT_INVALID 拒绝；定向降级只替换了规范的
  <figure><tg-document>，字面量仍在 → 降级后仍失败 → 整条回复丢失。

覆盖：
1. _neutralize_stray_media_tags 单元测试（无 src / 未闭合 / 孤立闭标签 /
   合法媒体对不受影响 / data-src 负向环视）
2. 反引号 code span（行内 + ``` 围栏 + 未闭合围栏）内的裸标签转义
3. _rich_message_html_payload 端到端：日志真实 payload 复现案例
4. 上一轮修复回归：Markdown 包裹 URL 解包、_strip_invalid_media_urls
   跨块匹配、定向降级
5. 级联兜底模拟：定向降级结果仍含游离标签时，payload 清洗可救活
"""
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"),
)

from apitelegramchat.utils import (
    _neutralize_stray_media_tags,
    _escape_raw_tags_in_code_spans,
    _unwrap_markdown_link_url,
    _unwrap_markdown_link_urls,
    _strip_invalid_media_urls,
    _demote_all_media_to_links,
    _rich_message_html_payload,
    _rich_message_plain_text_fallback,
)

# 新日志中的真实 R2 签名 URL（801_router_password 文件）
REAL_URL = (
    "https://a94baf2f82dbdc55044f54eb19838b0c.r2.cloudflarestorage.com/dearella/"
    "telegram/BQACAgUAAxkBAAJ0_mqa-LuQKCOY3_mZgs3KDuA9KtdsAAKGJgACZbTZVENGbpeRvgbHPQQ"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=5e1dc48ef14f62c26496ff38f2155b55%2F20260904%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260904T165839Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=6c4efaa9ef01413d0698a7eb2a65d0b83fe22d85700b2d15ac7af9703fcdc769"
)

# v3（2026-09-05）：payload 层新增媒体 src 属性裸 & → &amp; 幂等转义
# （_escape_media_src_ampersands），断言需按转义后形态比对。
ESCAPED_URL = REAL_URL.replace("&", "&amp;")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def has_raw_stray_doc(html: str) -> bool:
    """是否存在"裸 <tg-document> 开标签且不是 规范<figure>内带src对"的形态。"""
    import re
    # 粗略判定：存在 <tg-document 开标签，但整个文本中
    # "带 src 的开标签紧跟闭标签"配对数 < 开标签总数
    opens = re.findall(r'<tg-document\b[^>]*>', html, re.IGNORECASE)
    balanced = re.findall(r'<tg-document\b[^>]*>\s*</tg-document\s*>|<tg-document\b[^>]*/>', html, re.IGNORECASE)
    return len(opens) > len(balanced)


print("== 1. _neutralize_stray_media_tags 单元测试 ==")
check("裸 <tg-document> 转义为字面文本",
      _neutralize_stray_media_tags("使用 <tg-document> 标签发送") == "使用 &lt;tg-document&gt; 标签发送")
check("带属性无 src 的开标签转义",
      _neutralize_stray_media_tags('<tg-document class="x">文本') == '&lt;tg-document class="x"&gt;文本')
check("合法开闭对（带 src）保留",
      _neutralize_stray_media_tags(f'<tg-document src="{REAL_URL}"></tg-document>')
      == f'<tg-document src="{REAL_URL}"></tg-document>')
check("自闭合（带 src）保留",
      _neutralize_stray_media_tags(f'<tg-document src="{REAL_URL}"/>')
      == f'<tg-document src="{REAL_URL}"/>')
check("自闭合（无 src）转义",
      _neutralize_stray_media_tags('<tg-document/>') == '&lt;tg-document/&gt;')
check("未闭合开标签（带 src）转义，后续合法对保留",
      _neutralize_stray_media_tags('<tg-document src="https://a.com/x">t <tg-document src="https://b.com/y"></tg-document>')
      == '&lt;tg-document src="https://a.com/x"&gt;t <tg-document src="https://b.com/y"></tg-document>')
check("孤立闭标签转义",
      _neutralize_stray_media_tags('前文 </tg-document> 后文') == '前文 &lt;/tg-document&gt; 后文')
check("无 src 开闭对全部转义、内文保留",
      _neutralize_stray_media_tags('<tg-document>说明</tg-document>')
      == '&lt;tg-document&gt;说明&lt;/tg-document&gt;')
check("<img> 无 src 转义",
      _neutralize_stray_media_tags('看 <img> 这里') == '看 &lt;img&gt; 这里')
check("<img src> 保留（URL 合法性交由 _strip_invalid_media_urls）",
      _neutralize_stray_media_tags('<img src="https://a.com/x.jpg"/>') == '<img src="https://a.com/x.jpg"/>')
check("data-src 不算 src（负向环视）",
      _neutralize_stray_media_tags('<img data-src="https://a.com/x.jpg">') == '&lt;img data-src="https://a.com/x.jpg"&gt;')
check("<video> 合法对保留",
      _neutralize_stray_media_tags('<video src="https://a.com/v.mp4"></video>')
      == '<video src="https://a.com/v.mp4"></video>')
check("裸 <video> 开闭均转义",
      _neutralize_stray_media_tags('<video>占位</video>')
      == '&lt;video&gt;占位&lt;/video&gt;')
check("figure 内规范 tg-document 整体不受影响",
      _neutralize_stray_media_tags(
          f'<figure><tg-document src="{REAL_URL}"></tg-document><figcaption>801_router_password</figcaption></figure>')
      == f'<figure><tg-document src="{REAL_URL}"></tg-document><figcaption>801_router_password</figcaption></figure>')
check("普通文本中的 a < b 不误伤",
      _neutralize_stray_media_tags('if a < b then') == 'if a < b then')
check("<a> 锚点不在扫描范围",
      _neutralize_stray_media_tags('<a href="https://a.com">x</a>')
      == '<a href="https://a.com">x</a>')
check("大写标签同样处理",
      _neutralize_stray_media_tags('<TG-DOCUMENT> 与 <TG-DOCUMENT/>') 
      == '&lt;TG-DOCUMENT&gt; 与 &lt;TG-DOCUMENT/&gt;')
check("空串安全", _neutralize_stray_media_tags("") == "")
check("无标签文本原样", _neutralize_stray_media_tags("纯文本内容") == "纯文本内容")


print("== 2. 反引号 code span 转义 ==")
check("行内 span 内的裸标签转义",
      _escape_raw_tags_in_code_spans('使用 `<tg-document>` 标签发送')
      == '使用 `&lt;tg-document&gt;` 标签发送')
check("行内 span 内完整标签示例转义",
      _escape_raw_tags_in_code_spans('`<figure><tg-document src="https://example.com/x.pdf"></tg-document></figure>`')
      == '`&lt;figure&gt;&lt;tg-document src="https://example.com/x.pdf"&gt;&lt;/tg-document&gt;&lt;/figure&gt;`')
check("span 内已有实体不二次转义",
      _escape_raw_tags_in_code_spans('`&lt;tg-document&gt;`') == '`&lt;tg-document&gt;`')
check("围栏内标签转义",
      _escape_raw_tags_in_code_spans('看示例：\n```\n<video src="https://a.com/v.mp4"></video>\n```\n完')
      == '看示例：\n```\n&lt;video src="https://a.com/v.mp4"&gt;&lt;/video&gt;\n```\n完')
check("未闭合围栏：其后内容按字面处理",
      _escape_raw_tags_in_code_spans('```python\n<img src="https://a.com/x.jpg">')
      == '```python\n&lt;img src="https://a.com/x.jpg"&gt;')
check("围栏外合法媒体不受影响",
      _escape_raw_tags_in_code_spans('前 `<tg-document>` 后 <video src="https://a.com/v.mp4"></video>')
      == '前 `&lt;tg-document&gt;` 后 <video src="https://a.com/v.mp4"></video>')
check("无反引号时原样返回",
      _escape_raw_tags_in_code_spans('<video src="https://a.com/v.mp4"></video>')
      == '<video src="https://a.com/v.mp4"></video>')


print("== 3. _rich_message_html_payload 端到端（新日志真实案例） ==")
LOG_PAYLOAD = (
    '<details><summary>用户希望我使用 `&lt;tg-document&gt;` 标签，配合链…</summary>\n'
    '<p>用户希望我使用 `&lt;tg-document&gt;` 标签，配合链接来发送文档。让我看看用户给的链接：<br/><br/>'
    'https://a94baf2f82dbdc55044f54eb19838b0c.r2.cloudflarestorage.com/dearella/telegram/BQACAgUAAxkBAAJ0_mqa'
    '?X-Amz-Algorithm=AWS4-HMAC-SHA256&amp;X-Amz-Expires=3600<br/><br/>'
    '根据系统指令中的媒体资源规则：<br/>'
    '- 文档：`&lt;figure&gt;&lt;tg-document src="https://example.com/document.pdf"&gt;&lt;/tg-document&gt;'
    '&lt;figcaption&gt;项目方案 PDF&lt;/figcaption&gt;&lt;/figure&gt;`<br/><br/>'
    '但是注意，这个链接是一个 text/plain 文件，不是 PDF。不过 `&lt;tg-document&gt;` 应该可以支持任何文件类型。<br/><br/>'
    '让我用正确的格式发送。</p>\n</details>\n'
    '明白了，使用 `<tg-document>` 标签发送：\n'
    f'<figure><tg-document src="{REAL_URL}"></tg-document><figcaption>801_router_password</figcaption></figure>'
)
payload = _rich_message_html_payload(LOG_PAYLOAD)
payload_html = payload["html"]
check("端到端：不再含游离裸 <tg-document> 开标签",
      not has_raw_stray_doc(payload_html))
check("端到端：反引号字面量被转义",
      '`&lt;tg-document&gt;`' in payload_html)
check("端到端：figure 内真实文档保留（src 转义为 &amp; 形态）",
      f'<tg-document src="{ESCAPED_URL}"></tg-document>' in payload_html)
check("端到端：figcaption 保留",
      '<figcaption>801_router_password</figcaption>' in payload_html)
check("端到端：details 内已有实体不被二次转义",
      '&amp;lt;' not in payload_html)
check("端到端：details 内 URL 的 &amp; 实体保持",
      'X-Amz-Algorithm=AWS4-HMAC-SHA256&amp;X-Amz-Expires=3600' in payload_html)
check("端到端：details 结构完整",
      payload_html.startswith('<details><summary>') and payload_html.rstrip().endswith('</figure>'))
payload2 = _rich_message_html_payload(payload_html)
check("端到端：幂等性（二次清洗结果不变）", payload2["html"] == payload_html)


print("== 4. 上一轮修复回归 ==")
check("回归：src 中 Markdown 包裹 URL 解包",
      _unwrap_markdown_link_url(f"[{REAL_URL}]({REAL_URL})") == REAL_URL)
md_html = f'<tg-document src="[{REAL_URL}]({REAL_URL})"/>'
check("回归：payload 对 Markdown 包裹 src 的 tg-document 解包",
      _rich_message_html_payload(md_html)["html"] == f'<tg-document src="{ESCAPED_URL}"/>')
cross_block = (
    f'<p>A</p><tg-document src="bad-name"/>'
    f'<figure><tg-document src="{REAL_URL}"></tg-document><figcaption>d</figcaption></figure>'
)
stripped = _strip_invalid_media_urls(cross_block)
check("回归：自闭合坏块删除不吞并后续合法容器",
      f'<tg-document src="{REAL_URL}"></tg-document>' in stripped and "bad-name" not in stripped)
check("回归：file_name 伪 URL 文档剥离",
      "fake" not in _strip_invalid_media_urls('<figure><tg-document src="document_xxx.pdf"></tg-document>'
                                              '<figcaption>c</figcaption></figure>'))
check("回归：定向降级 tg-document → <a>",
      _demote_all_media_to_links(
          f'<figure><tg-document src="{REAL_URL}"></tg-document><figcaption>801_router_password</figcaption></figure>',
          {"tg-document"},
      ) == f'<a href="{REAL_URL}"><b>801_router_password</b></a>')
check("回归：定向降级不影响其他媒体",
      "video" in _demote_all_media_to_links(
          '<video src="https://a.com/v.mp4"></video><tg-document src="https://b.com/d.pdf"/>',
          {"tg-document"},
      ))
check("回归：plain-text fallback 保持转义",
      _rich_message_plain_text_fallback('<p>a &lt; b</p>') == '<p>a &lt; b</p>')


print("== 5. 级联兜底模拟：定向降级后仍含游离标签的场景 ==")
# 模拟线上失败链：定向降级只处理了 figure，游离字面量仍在 demoted 结果里
demoted_like = (
    '<details><summary>x</summary>\n<p>`&lt;tg-document&gt;` 示例</p>\n</details>\n'
    '明白了，使用 `<tg-document>` 标签发送：\n'
    f'<a href="{REAL_URL}"><b>801_router_password</b></a>'
)
rescued = _rich_message_html_payload(demoted_like)["html"]
check("级联：定向降级结果中的游离字面量被中和",
      not has_raw_stray_doc(rescued) and '`&lt;tg-document&gt;`' in rescued)
check("级联：降级后的 <a> 链接保留",
      f'<a href="{REAL_URL}"><b>801_router_password</b></a>' in rescued)

# 全量降级：kinds=None 时游离无 src 字面量同样被处理（先中和后降级）
full_demote_input = '使用 <tg-document> 标签：<tg-document src="https://b.com/d.pdf"/>'
full_demoted = _demote_all_media_to_links(full_demote_input)
full_payload = _rich_message_html_payload(full_demoted)["html"]
check("级联：全量降级+payload 清洗后无游离标签",
      not has_raw_stray_doc(full_payload) and '<a href="https://b.com/d.pdf">' in full_payload)


print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
