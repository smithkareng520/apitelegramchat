"""Telegram Rich Message 媒体标签的确定性规范化。"""

import html
import re
from urllib.parse import urlparse


_VIDEO_TAG_RE = re.compile(
    r'<video\b(?P<paired_attrs>[^>]*)>\s*</video\s*>|'
    r'<video\b(?P<void_attrs>[^>]*)/\s*>',
    re.IGNORECASE,
)
_SRC_ATTR_RE = re.compile(
    r'''\bsrc\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)')''',
    re.IGNORECASE,
)
_NON_HTTP_IMAGE_TAG_RE = re.compile(
    r'<img\s+[^>]*src="(?!(?:http|https):)[^"]*"[^>]*>',
    re.IGNORECASE,
)
# 模型有时会无视"禁止 Markdown"的指令，把工具返回的原始 URL 包进 Markdown
# 链接/图片语法（``[文本](URL)`` / ``![文本](URL)``），有时还会把 URL 里裸
# 露的 ``&`` 手误转义成 ``&amp;``（这本是只有写进 HTML 属性时才需要的处理）。
# Rich Message 不支持 Markdown，这类文本会被原样当作纯文字显示——用户看到
# 的要么是转义后的 URL 字符串，要么是不可点击的方括号文本。这里在标签规范化
# 阶段兜底识别并改写成正确的 HTML 媒体/链接标签，URL 部分统一先还原成原始
# 字符（unescape），再按 HTML 属性值语义转义恰好一次。
_MD_LINK_RE = re.compile(
    r'(?P<bang>!)?\[(?P<text>[^\]\n]*)\]\((?P<url>(?:https?://|//)[^\s()]+)\)',
    re.IGNORECASE,
)
_IMAGE_EXT_RE = re.compile(r'\.(?:png|jpe?g|gif|webp|bmp|svg)(?:$|[?#])', re.IGNORECASE)


def _normalize_markdown_media_links(html_content: str) -> str:
    """把模型误写的 Markdown 图片/链接语法改写为等价的 HTML 标签。"""
    if not html_content or "](" not in html_content:
        return html_content

    def _rewrite(match: re.Match) -> str:
        raw_url = match.group("url")
        # 先还原模型可能误加的 HTML 实体转义（如把 & 写成 &amp;），拿到未转义
        # 的原始 URL，再按"写入 HTML 属性"的语义转义恰好一次，避免残留转义
        # 或被二次转义成 &amp;amp;。
        clean_url = html.escape(html.unescape(raw_url), quote=True)
        is_image = bool(match.group("bang")) or bool(_IMAGE_EXT_RE.search(raw_url))
        if is_image:
            return f'<img src="{clean_url}"/>'
        text = (match.group("text") or "").strip() or "链接"
        return f'<a href="{raw_url}">{html.escape(text)}</a>'

    return _MD_LINK_RE.sub(_rewrite, html_content)


def normalize_rich_media_html(html_content: str) -> str:
    """规范化模型可确定无误的富媒体标签，不改变有效媒体或文本结构。

    GIF 是图片资源。若模型将 URL 路径以 ``.gif`` 结尾的资源写成完整的
    ``<video>`` 标签，本函数会将该标签原位改写为 ``<img>``，同时保留外层
    ``<figure>``、图注和其他媒体。非 GIF 视频保持不变；非 HTTP(S) 图片则按
    项目既有规则移除。同时兜底把模型误写的 Markdown 图片/链接语法改写为
    正确的 HTML 标签，并规范化其中 URL 的转义。
    """
    if not html_content:
        return html_content

    def normalize_video(match: re.Match) -> str:
        attrs = match.group("paired_attrs") or match.group("void_attrs") or ""
        src_match = _SRC_ATTR_RE.search(attrs)
        if not src_match:
            return match.group(0)

        src = src_match.group("double")
        if src is None:
            src = src_match.group("single")
        if not src or not urlparse(html.unescape(src)).path.lower().endswith(".gif"):
            return match.group(0)

        normalized_src = html.escape(html.unescape(src), quote=True)
        return f'<img src="{normalized_src}"/>'

    normalized = _normalize_markdown_media_links(html_content)
    normalized = _VIDEO_TAG_RE.sub(normalize_video, normalized)
    return _NON_HTTP_IMAGE_TAG_RE.sub("", normalized)
