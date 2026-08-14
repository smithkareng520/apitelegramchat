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


def normalize_rich_media_html(html_content: str) -> str:
    """规范化模型可确定无误的富媒体标签，不改变有效媒体或文本结构。

    GIF 是图片资源。若模型将 URL 路径以 ``.gif`` 结尾的资源写成完整的
    ``<video>`` 标签，本函数会将该标签原位改写为 ``<img>``，同时保留外层
    ``<figure>``、图注和其他媒体。非 GIF 视频保持不变；非 HTTP(S) 图片则按
    项目既有规则移除。
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

    normalized = _VIDEO_TAG_RE.sub(normalize_video, html_content)
    return _NON_HTTP_IMAGE_TAG_RE.sub("", normalized)
