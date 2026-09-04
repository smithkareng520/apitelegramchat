# -*- coding: utf-8 -*-
"""tg-document 富文本降级问题修复的回归测试。

运行方式（项目根目录）：
    PYTHONPATH=src python3 tests/test_tgdoc_fix.py

覆盖：
1. Markdown 链接包裹 URL 的解包（日志中的真实 R2 签名 URL 案例）
2. _strip_invalid_media_urls 对 <tg-document> 的清洗 + 自闭合跨块匹配回归
3. _demote_all_media_to_links 的 tg-document 定向降级
4. _rich_message_html_payload 端到端行为
5. sendRichHtmlMessage / send_rich_message_draft 错误分类逻辑（纯字符串模拟）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from apitelegramchat.utils import (
    _unwrap_markdown_link_url,
    _unwrap_markdown_link_urls,
    _strip_invalid_media_urls,
    _demote_all_media_to_links,
    _rich_message_html_payload,
    _rich_message_plain_text_fallback,
)

# 日志中的真实 R2 签名 URL（含 &、%2F 等）
REAL_URL = (
    "https://a94baf2f82dbdc55044f54eb19838b0c.r2.cloudflarestorage.com/dearella/"
    "telegram/BQACAgUAAxkBAAJyMGqZ_djNSVwbOUEgMlDDIHknbAiwAAKmLAACZbTRVK4lZgHrS-xqPQQ"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=5e1dc48ef14f62c26496ff38f2155b55%2F20260904%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260904T160807Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=b2029350c1e8d2e438b0ea5c811950245bd27599e17ed0385c5aa3c95083243a"
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


def classify_media_kinds(body_lower: str) -> set:
    """复刻 sendRichHtmlMessage 中的错误分类逻辑（纯字符串，便于回归）。"""
    media_kinds: set = set()
    if "rich_message_photo_" in body_lower:
        media_kinds.add("img")
    if "rich_message_video_" in body_lower:
        media_kinds.add("video")
    if "rich_message_audio_" in body_lower:
        media_kinds.add("audio")
    if "rich_message_document_" in body_lower:
        media_kinds.add("tg-document")
    return media_kinds


def draft_should_demote(body_lower: str) -> bool:
    """复刻 send_rich_message_draft 中的 media_not_found 判断。"""
    return (
        "rich_message_photo_no_media_found" in body_lower
        or "rich_message_video_no_media_found" in body_lower
        or "rich_message_document_" in body_lower
    )


print("== 1. _unwrap_markdown_link_url 单元测试 ==")
check("日志案例：[url](url) 解包",
      _unwrap_markdown_link_url(f"[{REAL_URL}]({REAL_URL})") == REAL_URL)
check("图片 Markdown ![alt](url) 解包",
      _unwrap_markdown_link_url(f"![截图]({REAL_URL})") == REAL_URL)
check("带标题 [text](url) 解包",
      _unwrap_markdown_link_url("[日志文件](https://a.com/f.pdf)") == "https://a.com/f.pdf")
check("普通 URL 不受影响",
      _unwrap_markdown_link_url(REAL_URL) == REAL_URL)
check("文件名不受影响",
      _unwrap_markdown_link_url("document_xxx.pdf") == "document_xxx.pdf")
check("空值安全", _unwrap_markdown_link_url("") == "")
check("不完整 Markdown 不误伤",
      _unwrap_markdown_link_url("[未闭合](https://a.com") == "[未闭合](https://a.com")

print("== 2. _unwrap_markdown_link_urls 属性重写 ==")
md_src = f'<tg-document src="[{REAL_URL}]({REAL_URL})"/>'
out = _unwrap_markdown_link_urls(md_src)
check("自闭合 tg-document src 解包", out == f'<tg-document src="{REAL_URL}"/>', out[:80])
out = _unwrap_markdown_link_urls('<a href="[https://a.com/x](https://a.com/x)">链接</a>')
check("a href 解包", out == '<a href="https://a.com/x">链接</a>', out)
out = _unwrap_markdown_link_urls("<video src='[https://a.com/v.mp4](https://a.com/v.mp4)'></video>")
check("单引号 video src 解包", out == "<video src='https://a.com/v.mp4'></video>", out)
normal = '<p>正文里的 [链接](https://a.com) 不动</p><img src="https://b.com/i.jpg"/>'
check("正文 Markdown 与正常属性不受影响", _unwrap_markdown_link_urls(normal) == normal)

print("== 3. _strip_invalid_media_urls 清洗 tg-document ==")
out = _strip_invalid_media_urls('<p>x</p><tg-document src="document_xxx"/>')
check("文件名 src 的自闭合 tg-document 被删除", "tg-document" not in out and "<p>x</p>" in out, out)
out = _strip_invalid_media_urls('<tg-document src="file_id_12345"></tg-document>')
check("文件名 src 的容器 tg-document 被整块删除", "tg-document" not in out, out)
good = '<figure><tg-document src="https://a.com/doc.pdf"></tg-document><figcaption>方案</figcaption></figure>'
check("合法 tg-document 原样保留", _strip_invalid_media_urls(good) == good)
mixed = '<tg-document src="bad_name.pdf"/><tg-document src="https://a.com/good.pdf"></tg-document>'
out = _strip_invalid_media_urls(mixed)
check("自闭合坏标签不跨块吞并合法容器（回归）", "good.pdf" in out and "bad_name" not in out, out)
out = _strip_invalid_media_urls(
    '<figure><tg-document src="bad_name.pdf"></tg-document><figcaption>说明文字</figcaption></figure>')
check("坏 document 剥离后 figure 保留 figcaption", "说明文字" in out and "tg-document" not in out, out)

print("== 4. _demote_all_media_to_links 定向降级 ==")
mixed_html = (
    "<p>看这个</p>"
    '<figure><img src="https://example.com/a.jpg"/><figcaption>图片</figcaption></figure>'
    '<figure><tg-document src="https://example.com/bad.pdf"></tg-document>'
    "<figcaption>文档说明</figcaption></figure>"
)
out = _demote_all_media_to_links(mixed_html, {"tg-document"})
check("img 不被牵连", '<img src="https://example.com/a.jpg"/>' in out, out)
check("tg-document 被降级为 <a>", "<tg-document" not in out, out)
check("figcaption 转为链接文本", '<a href="https://example.com/bad.pdf"><b>文档说明</b></a>' in out, out)
check("语义名 document 别名生效",
      "<tg-document" not in _demote_all_media_to_links(mixed_html, {"document"}))
out = _demote_all_media_to_links(mixed_html, {"img"})
check("只降级 img 时 document 不动", "<tg-document" in out and '<a href="https://example.com/a.jpg"' in out, out)
md_doc = f'<figure><tg-document src="[{REAL_URL}]({REAL_URL})"/><figcaption>日志文件</figcaption></figure>'
out = _demote_all_media_to_links(md_doc, {"tg-document"})
check("Markdown 包裹 src 降级后 href 为真实 URL",
      out == f'<a href="{REAL_URL}"><b>日志文件</b></a>', out[:120])
bare_doc = '<p>a</p><tg-document src="https://a.com/doc.pdf"/>'
out = _demote_all_media_to_links(bare_doc, None)
check("无 kinds 时裸 document 也降级并给默认文案",
      '<a href="https://a.com/doc.pdf"><b>📄 查看文档 · a.com</b></a>' in out, out)
out = _demote_all_media_to_links('<tg-document src="bad.pdf"/>', {"tg-document"})
check("非法 src 的 document 降级时删除", "tg-document" not in out and "bad.pdf" not in out, out)
video_keep = '<figure><video src="https://a.com/v.mp4"></video><figcaption>v</figcaption></figure>'
out = _demote_all_media_to_links(video_keep + '<tg-document src="https://a.com/d.pdf"/>', {"tg-document"})
check("video 不被 document 失败牵连", "<video" in out and "<tg-document" not in out, out)

print("== 5. _rich_message_html_payload 端到端（日志真实案例） ==")
log_html = (
    "<details><summary>检查 tg-document 用法</summary><p>说明文字</p></details>"
    "<p>明白了！用法如下：</p>"
    f'<figure><tg-document src="[{REAL_URL}]({REAL_URL})"/>'
    "<figcaption>message (2).txt — OpenRouter prompt cache 日志</figcaption></figure>"
)
payload = _rich_message_html_payload(log_html)["html"]
check("tg-document 保留（未降级为纯文本）", "<tg-document" in payload, payload[:120])
check("src 已解包并按 HTML 规范转义为 &amp; 形态", f'src="{ESCAPED_URL}"' in payload)
check("figcaption 保留", "OpenRouter prompt cache 日志" in payload)
e2e_bad = '<p>文档在此</p><figure><tg-document src="document_xxx.pdf"></tg-document><figcaption>文件</figcaption></figure>'
payload = _rich_message_html_payload(e2e_bad)["html"]
check("非法 document 端到端被剥离", "tg-document" not in payload and "文档在此" in payload, payload)

print("== 6. 错误分类逻辑（复刻 sendRichHtmlMessage / draft 判断） ==")
log_error_body = '{"ok":false,"error_code":400,"description":"Bad Request: RICH_MESSAGE_DOCUMENT_URL_INVALID"}'.lower()
check("document 错误进入媒体分类", classify_media_kinds(log_error_body) == {"tg-document"})
check("document 错误触发 draft 立即降级重试", draft_should_demote(log_error_body))
photo_err = "Bad Request: RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND".lower()
check("photo 错误分类不变", classify_media_kinds(photo_err) == {"img"})
video_err = "Bad Request: RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND".lower()
check("video 错误分类不变", classify_media_kinds(video_err) == {"video"} and draft_should_demote(video_err))
plain_err = "Bad Request: RICH_MESSAGE_CONTENT_REQUIRED".lower()
check("content_required 不误入媒体分类", classify_media_kinds(plain_err) == set())

print("== 7. 既有行为回归 ==")
check("plain fallback 仍工作",
      _rich_message_plain_text_fallback('<p>hi <b>there</b></p>') == "<p>hi there</p>")
img_video = '<p>t</p><img src="https://a.com/i.jpg"/><video src="https://a.com/v.mp4"></video>'
check("img/video 清洗行为不变", _strip_invalid_media_urls(img_video) == img_video)
out = _strip_invalid_media_urls('<img src="photo_AgACAgUA.jpg"/>')
check("文件名 img 仍被删除", "img" not in out.lower(), out)
vv = '<video src="bad.mp4"/><video src="https://a.com/good.mp4"></video>'
out = _strip_invalid_media_urls(vv)
check("video 自闭合不跨块吞并容器（回归）", "good.mp4" in out and "bad.mp4" not in out, out)

print()
print(f"结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
