"""富消息媒体兜底清理子系统（自 utils.py 拆出）。

非法媒体 URL 剥离、观看页视频降级为链接、幻灯片拆解、选择性/全量
媒体降级，以及 InputRichMessage HTML payload 构造。
"""

import re
import html
from typing import Optional

import aiohttp

from markdown_converter import convert_markdown_to_telegram_html

from core.text_utils import _SMART_AMP_PATTERN, escape_html

import logging

logger = logging.getLogger(__name__)


def _rich_message_html_payload(html_content: str) -> dict:
    """构造符合 InputRichMessage 规范的 HTML 富消息。

    在交付给 Telegram 前，依次跑两道兜底清理：

    1. ``_strip_invalid_media_urls``：剥离 ``src`` 不是合法 http(s) URL
       的 ``<img>``/``<video>``/``<audio>`` 标签。处理 LLM 把附件
       file_name（如 ``photo_AgACAgUA.jpg``）或 file_id 误当成 URL 的情况，
       Telegram 会以 ``RICH_MESSAGE_PHOTO_URL_INVALID`` 拒绝整条消息。

    2. ``_demote_watch_page_videos``：把 ``<video src="WATCH_PAGE_URL">``
       块降级为 ``<a href>`` 链接。处理 LLM 把 YouTube / Bilibili 等
       观看页 URL 误当直链视频嵌入 ``<video src>`` 的情况——这种 URL
       看着合法（``http(s)://`` 开头），但 Telegram 去抓会拿到 HTML
       页面，以 ``RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND`` 拒绝整条消息。
       降级后保留模型生成的 figcaption 文本，让用户仍可点击跳转观看页。
    """
    # 0. Markdown → Telegram HTML 兜底转换。必须排在媒体清理之前：
    #    Markdown 图片 ![alt](url) 此时才会变成 <img src>，从而同样接受
    #    下面两道 URL 合法性检查；若放在之后，伪 URL 图片会绕过校验并
    #    导致整条消息被 Telegram 拒绝。
    #    模型已按提示词输出纯 HTML 时，转换是幂等 no-op。
    normalized = convert_markdown_to_telegram_html(html_content)
    if normalized != html_content:
        # 该转换在流式草稿路径上每帧都会命中（同一条草稿每 0.65s 刷一次），
        # 用 INFO 记录会产生日志刷屏——真实日志里同一行重复了数十次，既淹没
        # 了有效信息，也让 logging 本身成为热路径开销。降级为 DEBUG。
        logger.debug(
            "sendRichMessage 兜底转换：检测到 Markdown 语法，已转为 Telegram HTML。"
            "原始长度=%s，转换后长度=%s",
            len(html_content),
            len(normalized),
        )

    cleaned = _strip_invalid_media_urls(normalized)
    demoted = _demote_watch_page_videos(cleaned)
    if demoted != normalized:
        logger.warning(
            "sendRichMessage 兜底清理：检测到伪 URL 或观看页 URL 媒体块，"
            "已剥离/降级以保证消息送达。原始长度=%s，清理后长度=%s",
            len(normalized),
            len(demoted),
        )
    return {
        "html": demoted,
        "skip_entity_detection": True,
    }


# 匹配完整的 <img ...> 标签（含可选自闭合斜杠），直到第一个 >。
# 不处理 <a href>，因为锚点的非法 href 不会让 Telegram 整条拒绝，
# 且剥离锚点会丢失链接文本。
_MEDIA_SRC_RE = re.compile(
    r'<img\b[^>]*?/?>',
    re.IGNORECASE,
)

# 已知的"观看页"URL 模式——这些 URL 永远不可能是直链视频文件，
# 但模型有时会把它们误嵌入 <video src>。命中后整块降级为 <a> 链接，
# 而不是直接删除，避免丢失模型给的视频标题/figcaption 文本。
#
# 直链视频文件的特征是：URL 末尾通常是 .mp4/.webm/.mov/.m3u8/.ts 等
# 视频扩展名，或者来自已知视频 CDN（googlevideo.com、bilivideo.com、
# akamaized.net 等）。这里走"否定式"判定——只要 URL 命中观看页模式，
# 就一定不是直链。
#
# 顺序无所谓，命中任一即认定为观看页。
_WATCH_PAGE_URL_PATTERNS = (
    # YouTube watch / embed / shorts —— 注意 watch 后面通常跟 ? 而不是 /
    re.compile(r'youtube\.com/watch\b', re.IGNORECASE),
    re.compile(r'youtube\.com/embed/', re.IGNORECASE),
    re.compile(r'youtube\.com/shorts/', re.IGNORECASE),
    re.compile(r'youtube\.com/live/', re.IGNORECASE),
    re.compile(r'youtu\.be/', re.IGNORECASE),
    # Bilibili 观看页
    re.compile(r'bilibili\.com/video/', re.IGNORECASE),
    re.compile(r'bilibili\.com/bangumi/play/', re.IGNORECASE),
    re.compile(r'b23\.tv/', re.IGNORECASE),
    # Vimeo / Dailymotion / Twitch / TikTok / Facebook / X / Nico
    re.compile(r'vimeo\.com/\d', re.IGNORECASE),
    re.compile(r'dailymotion\.com/video/', re.IGNORECASE),
    re.compile(r'twitch\.tv/videos/', re.IGNORECASE),
    re.compile(r'tiktok\.com/@', re.IGNORECASE),
    re.compile(r'facebook\.com/(?:watch|reel)/', re.IGNORECASE),
    re.compile(r'fb\.watch/', re.IGNORECASE),
    re.compile(r'(?:twitter|x)\.com/[^/]+/status/', re.IGNORECASE),
    re.compile(r'nico(?:video)?\.[a-z]+/watch/', re.IGNORECASE),
)

def _looks_like_watch_page(url: str) -> bool:
    """判定 URL 是否为视频观看页（不可作为 <video src> 直链）。"""
    if not url:
        return False
    u = url.strip()
    if not (u.lower().startswith("http://") or u.lower().startswith("https://")):
        return False
    # 命中观看页模式即认定非直链
    for pat in _WATCH_PAGE_URL_PATTERNS:
        if pat.search(u):
            return True
    return False


def _strip_invalid_media_urls(html_content: str) -> str:
    """剥离 src 不是合法 http(s) URL 的 <img>/<video>/<audio> 标签。

    Telegram Rich Message 要求 ``<img src>`` 必须是公开可访问的 http(s)
    URL；LLM 偶尔会把附件 file_name（如 ``photo_AgACAgUA.jpg``）或 file_id
    当作 URL 写入，会被服务端以 ``RICH_MESSAGE_PHOTO_URL_INVALID`` 拒绝。

    本函数逐个扫描 ``<img>``/``<video>``/``<audio>`` 标签的 ``src`` 属性，
    若不以 ``http://`` 或 ``https://`` 开头，则：

      * 对 ``<img .../>`` 自闭合形式：直接删除整个标签；
      * 对 ``<video>...</video>`` / ``<audio>...</audio>`` 容器形式：
        删除整个开闭标签对（包含内部内容），避免出现孤立结束标签；
      * 若外层是 ``<figure>`` 且剥离后 figure 内既无 ``<img>``/``<video>``
        也无 ``<figcaption>``，则一并删除该空 figure。

    另外，对 ``<video src="WATCH_PAGE_URL">`` 还会做"观看页降级"：
    若 URL 命中 YouTube / Bilibili / Vimeo / 抖音 / Twitter / Facebook
    等观看页模式（即非直链视频文件），把整个 ``<figure>`` 块降级为
    ``<a href="URL">CAPTION</a>``，避免 Telegram 以
    ``RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND`` 拒绝整条消息——同时保留模型
    生成的视频标题/figcaption 文本，让用户仍可点击跳转观看页。
    """
    if not html_content:
        return ""
    def _extract_src(tag_text: str) -> str:
        m = re.search(r'\bsrc\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
                      tag_text, re.IGNORECASE)
        if not m:
            return ""
        return (m.group(2) or m.group(3) or m.group(4) or "").strip()

    def _is_valid_url(url: str) -> bool:
        u = (url or "").strip().lower()
        return bool(u) and u.startswith(("http://", "https://"))

    # 先处理容器型 <video>...</video> / <audio>...</audio>：
    # 起始标签 src 非法时，连同内部内容一起删掉。
    def _strip_container(text: str, tag: str) -> str:
        pattern = re.compile(
            rf'<{tag}\b[^>]*>.*?</{tag}\s*>|<{tag}\b[^>]*/>',
            re.IGNORECASE | re.DOTALL,
        )

        def _check(m: re.Match) -> str:
            block = m.group(0)
            src = _extract_src(block)
            if _is_valid_url(src):
                return block  # 合法 URL，保留
            return ""  # 非法 URL，整块删除

        return pattern.sub(_check, text)

    result = html_content
    for tag in ("video", "audio"):
        result = _strip_container(result, tag)

    # 再处理 <img .../> 自闭合形式
    def _strip_img(text: str) -> str:
        def _check(m: re.Match) -> str:
            block = m.group(0)
            src = _extract_src(block)
            if _is_valid_url(src):
                return block
            return ""
        # <img ...> 不一定有自闭合斜杠，统一处理
        return _MEDIA_SRC_RE.sub(_check, text)

    result = _strip_img(result)

    # 清理空的 <figure>...</figure>（剥离后只剩空白）
    figure_pattern = re.compile(
        r'<figure\b[^>]*>(.*?)</figure\s*>',
        re.IGNORECASE | re.DOTALL,
    )

    def _clean_empty_figure(m: re.Match) -> str:
        inner = m.group(1) or ""
        # 仍然有 img/video/audio/figcaption 就保留
        has_media = re.search(
            r'<(img|video|audio|figcaption)\b',
            inner,
            re.IGNORECASE,
        )
        if has_media:
            return m.group(0)
        # 全空白：直接删
        if not inner.strip():
            return ""
        return m.group(0)

    result = figure_pattern.sub(_clean_empty_figure, result)
    return result


def _demote_watch_page_videos(html_content: str) -> str:
    """把 ``<video src="WATCH_PAGE_URL">`` 块降级为 ``<a href>`` 链接。

    在 ``_strip_invalid_media_urls`` 之后跑——前者只检查 URL 是否
    以 ``http(s)://`` 开头，无法识别"看着合法但其实是 HTML 观看页"的
    URL（如 ``https://www.bilibili.com/video/BVxxx``）。Telegram 收到
    这种 ``<video>`` 后会去抓 URL 当视频文件，拿到 HTML 页面，以
    ``RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND`` 拒绝整条消息。

    本函数扫描所有 ``<video src="...">`` 标签，若 URL 命中观看页模式，
    把整个 ``<figure><video src="URL"></video><figcaption>CAPTION</figcaption></figure>``
    降级为 ``<a href="URL">🎬 CAPTION</a>``。若没有外层 ``<figure>``，
    则把 ``<video>...</video>`` 单独替换为 ``<a>`` 链接。

    URL 看起来是直链（命中 .mp4/.webm/.mov/.m3u8/.ts/.ogg 等）时，
    保留原 ``<video>`` 不动——这种情况 Telegram 通常能正常播放。
    """
    if not html_content:
        return ""

    # 先匹配 <figure><video ...>...</video><figcaption>...</figcaption></figure>
    # 也兼容 <figure><video .../></figure>（自闭合）与 figcaption 在 video 之前。
    figure_video_re = re.compile(
        r'<figure\b[^>]*>(.*?)</figure\s*>',
        re.IGNORECASE | re.DOTALL,
    )
    video_in_figure_re = re.compile(
        r'<video\b[^>]*>.*?</video\s*>|<video\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_figure(m: re.Match) -> str:
        inner = m.group(1) or ""
        # 找到第一个 <video> 块
        vm = video_in_figure_re.search(inner)
        if not vm:
            return m.group(0)  # 没有 video，原样返回

        video_block = vm.group(0)
        src = _extract_attr(video_block, "src")
        if not src:
            return m.group(0)  # 无 src，保留给 _strip_invalid_media_urls 处理

        # 只有命中观看页模式才降级；否则保留原 <video>，
        # 让 Telegram 自行处理（直链成功就播，失败由后续兜底）。
        if not _looks_like_watch_page(src):
            return m.group(0)

        # 命中观看页模式 → 降级为 <a> 链接。
        # caption 优先取 <figcaption> 文本，退化到 domain。
        figcaption_re = re.compile(
            r'<figcaption\b[^>]*>(.*?)</figcaption\s*>',
            re.IGNORECASE | re.DOTALL,
        )
        figcaption_text = ""
        figm = figcaption_re.search(inner)
        if figm:
            figcaption_text = re.sub(r'<[^>]+>', '', figm.group(1)).strip()

        if not figcaption_text:
            domain = _media_url_domain(src)
            figcaption_text = "🎬 观看视频" + (f" · {domain}" if domain else "")

        # 把 video 块和 figcaption 都从 inner 里去掉，剩余内容（少见）追加在链接后
        rest = video_in_figure_re.sub("", inner)
        rest = figcaption_re.sub("", rest).strip()

        # href 走属性转义：观看页 URL 一般干净，但上游可能已做过一次转义，
        # escape_media_url_attr 会归一化后统一转义，避免双重转义/裸 &
        anchor = f'<a href="{escape_media_url_attr(src)}"><b>{escape_html(figcaption_text)}</b></a>'
        if not rest:
            return anchor
        return f"{anchor} {rest}"

    def _extract_attr(tag_text: str, attr: str) -> str:
        m = re.search(
            rf'\b{attr}\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
            tag_text, re.IGNORECASE,
        )
        if not m:
            return ""
        return (m.group(2) or m.group(3) or m.group(4) or "").strip()

    def _extract_caption_from_inner(inner: str) -> str:
        m = re.search(
            r'<figcaption\b[^>]*>(.*?)</figcaption\s*>',
            inner, re.IGNORECASE | re.DOTALL,
        )
        if m:
            text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if text:
                return text
        bare = re.sub(r'<[^>]+>', ' ', inner)
        bare = re.sub(r'\s+', ' ', bare).strip()
        return bare[:80] if bare else ""

    result = figure_video_re.sub(_replace_figure, html_content)

    # 再处理"裸" video（不在 <figure> 里的，或 figure 已经被上面处理过
    # 但 video 漏在 figure 外的零散情况）。
    bare_video_re = re.compile(
        r'<video\b[^>]*>.*?</video\s*>|<video\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_bare_video(m: re.Match) -> str:
        block = m.group(0)
        src = _extract_attr(block, "src")
        if not src:
            return block  # 留给 _strip_invalid_media_urls 删除
        if not _looks_like_watch_page(src):
            return block  # 不是观看页，保留
        # 退化 caption：取 video 块内文本，再不行就用 domain
        caption = _extract_caption_from_inner(block)
        if not caption:
            domain = _media_url_domain(src)
            caption = "🎬 观看视频" + (f" · {domain}" if domain else "")
        return f'<a href="{escape_media_url_attr(src)}"><b>{escape_html(caption)}</b></a>'

    result = bare_video_re.sub(_replace_bare_video, result)
    return result


def _extract_media_urls(html_content: str, media_kind: str) -> list[str]:
    """提取指定类型媒体的所有 src URL。
    
    Args:
        html_content: HTML 内容
        media_kind: 媒体类型，可以是 "img", "video", "audio"
    
    Returns:
        该类型所有媒体的 src URL 列表（按出现顺序）
    """
    if not html_content or not media_kind:
        return []
    
    urls = []
    # 匹配该类型的所有标签（包括自闭合和容器形式）
    pattern = re.compile(
        rf'<{media_kind}\b[^>]*>.*?</{media_kind}\s*>|<{media_kind}\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )
    
    for match in pattern.finditer(html_content):
        tag = match.group(0)
        # 提取 src 属性
        src_match = re.search(r'\bsrc\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag, re.IGNORECASE)
        if src_match:
            src = (src_match.group(2) or src_match.group(3) or src_match.group(4) or "").strip()
            if src:
                urls.append(src)

    return urls


# ---------- 媒体 URL 转义与降级锚点构造 ----------
def escape_media_url_attr(url: str) -> str:
    """URL 写入 href/src 等 HTML 属性前的转义（公开给其他模块复用）。

    先 ``html.unescape`` 归一化（容忍上游 src 已做过一次 ``&``→``&amp;``
    转义的情况），再统一转义 ``& < > "``。这保证两件事：

      1. 不会出现 ``&amp;amp;`` 双重转义（渲染出的 URL 里混进字面量
         ``&amp;``，链接直接失效）；
      2. 属性值里不会残留裸 ``&``——Telegram 的 HTML 解析器会把
         ``&X-Amz-Credential`` 之类的片段当作实体名起点，导致 URL 在
         属性值里被截断；而 R2 presigned URL 一旦缺失
         ``X-Amz-Signature`` 等签名参数，服务端会以 403 拒绝。
    """
    normalized = html.unescape(url or "")
    return (normalized
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def _escape_media_url_text(url: str) -> str:
    """URL 作为 ``<a>`` 可见文本前的转义（属性引号无需转义）。"""
    normalized = html.unescape(url or "")
    return (normalized
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))


def _media_url_domain(url: str) -> str:
    """提取 URL 的域名（netloc，含端口），无法解析时返回空串。

    先 ``html.unescape`` 归一化（容忍上游 src 已做过 ``&``→``&amp;``
    转义的情况），再取 ``scheme://`` 之后的 authority 部分；极罕见的
    ``user:pass@host`` 形态会去掉 userinfo 只留 host。
    """
    u = html.unescape((url or "").strip())
    m = re.match(r'^[A-Za-z][A-Za-z0-9+.\-]*://([^/?#\s]+)', u)
    if not m:
        return ""
    netloc = m.group(1)
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    return netloc


def _build_demoted_anchor(url: str) -> str:
    """构造媒体降级锚点：``<a href="完整带参数 URL">域名</a>``。

    href 必须是**完整原始 URL（含全部查询参数）**：降级意味着内嵌展示
    已经失败，用户点按 / 长按复制时拿到的必须是可直接打开的完整 URL——
    R2 presigned URL 缺失签名参数会被服务端以 403 拒绝。

    链接可见文本则只显示**域名**（2026-09-06 用户要求）：R2 presigned
    URL 动辄数百字符，整串铺在消息里会把对话刷得很长；Telegram 渲染
    ``<a>`` 时点按跳转走 href、长按菜单复制的也是 href，可见文本用域名
    即可保持消息整洁且不丢失任何恢复能力。旧的两种样式均已废弃——
    ``🖼 查看图片 · 域名``（文案噪声）与「文本=完整 URL」（太长）。
    域名解析失败（畸形 URL）时退回显示完整 URL，保证文本不空白。
    """
    href = escape_media_url_attr(url)
    domain = _media_url_domain(url)
    text = _escape_media_url_text(domain if domain else url)
    return f'<a href="{href}">{text}</a>'


# <tg-slideshow> 轮播容器。项目规范（fetch_rich_content.py）："tg-slideshow
# 内只放裸 <img src>"，渲染器不认其他元素——降级产生的 <a> 锚点若留在
# 容器内部会被直接吞掉（用户看到"图片没了、链接也没有"）。因此凡降级
# 容器内媒体，锚点必须移到容器外面。
_TG_SLIDESHOW_RE = re.compile(
    r'(<tg-slideshow\b[^>]*>)(.*?)(</tg-slideshow\s*>)',
    re.IGNORECASE | re.DOTALL,
)


def _slideshow_inner_has_media(inner: str) -> bool:
    return bool(re.search(r'<(?:img|video|audio)\b', inner or "", re.IGNORECASE))


def _unwrap_slideshow_inner(inner: str) -> str:
    """轮播容器内媒体已全部降级时的解包：剥掉残留的 figcaption 标签只留
    文本（figcaption 离开媒体容器不再合法），避免空 <tg-slideshow> 被服务端
    以"无媒体"拒绝。"""
    text = re.sub(
        r'<figcaption\b[^>]*>(.*?)</figcaption\s*>',
        lambda m: m.group(1),
        inner or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r'</?figcaption\s*>', '', text, flags=re.IGNORECASE)
    return text.strip()


def _demote_specific_media_url(html_content: str, media_kind: str, target_url: str) -> str:
    """只降级指定 URL 的媒体，保留其他媒体不变。
    
    降级产物是 ``<a href="完整带参数 URL">域名</a>``（见
    ``_build_demoted_anchor``）。若目标媒体位于 ``<tg-slideshow>`` 轮播
    容器内，锚点会移到容器外面——容器内只认裸 ``<img>``，锚点留在
    里面会被渲染器吞掉。
    
    Args:
        html_content: HTML 内容
        media_kind: 媒体类型 ("img", "video", "audio")
        target_url: 要降级的目标 URL
    
    Returns:
        处理后的 HTML（只有目标 URL 的媒体被降级为链接）
    """
    if not html_content or not target_url:
        return html_content
    
    def _extract_src(tag_text: str) -> str:
        m = re.search(r'\bsrc\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag_text, re.IGNORECASE)
        if not m:
            return ""
        return (m.group(2) or m.group(3) or m.group(4) or "").strip()
    
    def _norm_url(url: str) -> str:
        # 归一化后比较：容忍上游 src 已做 & → &amp; 转义导致的形式差异
        return html.unescape((url or "").strip())
    
    # 1) 处理 <figure> 内的目标媒体
    figure_re = re.compile(r'<figure\b[^>]*>(.*?)</figure\s*>', re.IGNORECASE | re.DOTALL)
    media_in_figure_re = re.compile(
        rf'<{media_kind}\b[^>]*?/?>.*?</{media_kind}\s*>|<{media_kind}\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )
    
    def _replace_figure(m: re.Match) -> str:
        inner = m.group(1) or ""
        mm = media_in_figure_re.search(inner)
        if not mm:
            return m.group(0)
        
        block = mm.group(0)
        src = _extract_src(block)
        
        # 只处理目标 URL
        if _norm_url(src) != _norm_url(target_url):
            return m.group(0)
        
        anchor = _build_demoted_anchor(src)
        
        # 移除媒体块和 figcaption，保留其他内容
        rest = inner.replace(block, "")
        rest = re.sub(r'<figcaption\b[^>]*>.*?</figcaption\s*>', '', rest, flags=re.IGNORECASE | re.DOTALL)
        rest = rest.strip()
        
        if rest:
            return f"{anchor} {rest}"
        return anchor
    
    result = figure_re.sub(_replace_figure, html_content)
    
    # 1.5) 处理 <tg-slideshow> 轮播容器内的目标媒体：从容器内移除该媒体，
    #      <a> 锚点放到容器外（容器内只认裸 <img>，锚点留在里面会被
    #      渲染器吞掉，用户只会看到剩下的 <img> 而拿不到链接）。
    media_block_re = re.compile(
        rf'<{media_kind}\b[^>]*?/?>.*?</{media_kind}\s*>|<{media_kind}\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )
    
    def _replace_slideshow(m: re.Match) -> str:
        open_tag, inner, close_tag = m.group(1), m.group(2) or "", m.group(3)
        removed_anchors: list[str] = []
        
        def _check(mm: re.Match) -> str:
            block = mm.group(0)
            src = _extract_src(block)
            if src and _norm_url(src) == _norm_url(target_url):
                removed_anchors.append(_build_demoted_anchor(src))
                return ""  # 从容器内移除
            return block
        
        new_inner = media_block_re.sub(_check, inner)
        if not removed_anchors:
            return m.group(0)
        
        if _slideshow_inner_has_media(new_inner):
            body = f"{open_tag}{new_inner}{close_tag}"
        else:
            # 容器内已无任何媒体：解包，避免空轮播被服务端拒绝
            body = _unwrap_slideshow_inner(new_inner)
        # 多个锚点用 <br/> 分行：可见文本都是域名时，紧挨着会粘成
        # 一串无法分辨的重复域名（br 在 Rich Message 白名单内）。
        return body + "<br/>".join(removed_anchors)
    
    result = _TG_SLIDESHOW_RE.sub(_replace_slideshow, result)
    
    # 2) 处理裸媒体（不在 <figure>/<tg-slideshow> 中的）
    bare_media_re = re.compile(
        rf'<{media_kind}\b[^>]*>.*?</{media_kind}\s*>|<{media_kind}\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )
    
    def _replace_bare(m: re.Match) -> str:
        block = m.group(0)
        src = _extract_src(block)
        
        # 只处理目标 URL
        if _norm_url(src) != _norm_url(target_url):
            return block
        
        return _build_demoted_anchor(src)
    
    result = bare_media_re.sub(_replace_bare, result)
    
    return result


async def _selective_media_fallback(
    session: aiohttp.ClientSession,
    base_url: str,
    original_payload: dict,
    html_content: str,
    media_kinds: set[str],
) -> Optional[int]:
    """逐个排查有问题的媒体，只降级有问题的那个，保留其他正常媒体。
    
    策略：
    1. 对每个报错的媒体类型，提取所有该类型的媒体 URL
    2. 逐个尝试只降级其中一个 URL，测试消息能否发送成功
    3. 找到有问题的 URL 后，返回成功的消息 ID
    4. 如果逐个都试完了还是失败，返回 None（让调用方执行全部降级兜底）
    
    Args:
        session: aiohttp 会话
        base_url: Telegram API base URL
        original_payload: 原始请求 payload
        html_content: 原始 HTML 内容
        media_kinds: 报错的媒体类型集合 (如 {"img", "video"})
    
    Returns:
        成功时返回 message_id (int)，失败返回 None
    """
    logger.info(
        "开始逐个排查有问题的媒体，类型: %s",
        sorted(media_kinds),
    )
    
    # 对每个媒体类型，提取所有 URL
    for kind in sorted(media_kinds):
        urls = _extract_media_urls(html_content, kind)
        if not urls:
            continue
        
        logger.debug("媒体类型 %s 共有 %d 个实例: %s", kind, len(urls), urls[:3])
        
        # 逐个尝试只降级其中一个
        for idx, url in enumerate(urls):
            test_html = _demote_specific_media_url(html_content, kind, url)
            if test_html == html_content:
                continue  # 没有变化，跳过
            
            test_payload = {
                **original_payload,
                "rich_message": _rich_message_html_payload(test_html),
            }
            
            logger.debug(
                "测试降级第 %d/%d 个 %s: %s",
                idx + 1, len(urls), kind, url[:80],
            )
            
            try:
                async with session.post(f"{base_url}/sendRichMessage", json=test_payload) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            msg_id = (data.get("result") or {}).get("message_id")
                            if isinstance(msg_id, int) and msg_id > 0:
                                logger.info(
                                    "成功定位有问题的媒体并只降级它: kind=%s url=%s",
                                    kind, url[:100],
                                )
                                return msg_id
                        except Exception as e:
                            logger.debug("解析响应失败: %s", e)
                        return True  # 成功但无法解析 message_id
            except Exception as e:
                logger.debug("测试请求异常: %s", e)
                continue
    
    logger.info("逐个排查完成，未找到单一问题媒体，将执行全部降级兜底")
    return None


def _demote_all_media_to_links(
    html_content: str,
    media_kinds: Optional[set[str]] = None,
) -> str:
    """按指定媒体类型把 ``<video>/<audio>/<img>`` 降级为 ``<a href>``。

    ``media_kinds`` 为 ``None`` 时保持兼容；传入 ``{"image"}``、
    ``{"video"}`` 或 ``{"audio"}`` 时只处理对应类型，避免某一种媒体
    失败时牵连同一条消息中的其他媒体。

    用于 ``sendRichMessage`` 因媒体 URL 在 Telegram 服务端抓取失败而拒绝
    整条消息时的**反应式兜底**——例如：

      * LLM 嵌入了 ``<video src="...upload.wikimedia.org/.../x.ogv.480p.vp9.webm">``，
        URL 在浏览器侧能拿到 200 + ``video/webm``，但 Telegram 媒体抓取
        服务对该格式 / 编解码 / host 的支持有限，返回
        ``RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND``，导致**整条消息丢失**；
      * LLM 嵌入了看似合法的 ``<img src>`` 但目标服务端拒绝 Telegram
        bot 的 User-Agent，返回 ``RICH_MESSAGE_PHOTO_URL_INVALID`` 等。

    与 ``_demote_watch_page_videos``（仅在 URL 命中已知观看页模式时降级）
    不同，本函数**无差别降级所有媒体**——只在初次发送已经被服务端拒绝
    之后才触发，因此激进策略是安全的：宁可丢失"内嵌播放器"也不可丢失
    整条用户可见的回复。

    降级规则（锚点统一由 ``_build_demoted_anchor`` 构造）：

      * 任何 ``<img>/<video>/<audio>`` → ``<a href="完整带参数 URL">域名</a>``
        ——href 保留完整原始 URL（含全部查询参数，点按/复制均可用），
        可见文本只显示域名保持消息整洁（完整 URL 铺在消息里太长）；
      * ``<tg-slideshow>`` 轮播容器内的媒体：从容器内移除，锚点放到容器
        外面（容器内只认裸 ``<img>``，锚点留在里面会被渲染器吞掉）；
        容器内媒体全部降级后解包容器，避免空轮播被服务端拒绝；
      * ``<figure>`` 内的媒体连同 figcaption 一起被锚点替换，figure 里的
        其他可见文本保留；
      * ``src`` 非法（非 http(s):// 开头）的媒体块直接整块删除——这种
        URL 在 ``_strip_invalid_media_urls`` 阶段也已被处理，这里是冗余
        兜底。
    """
    if not html_content:
        return ""
    if media_kinds is not None:
        media_kinds = {str(kind).lower() for kind in media_kinds}

    def _extract_src(tag_text: str) -> str:
        m = re.search(r'\bsrc\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
                      tag_text, re.IGNORECASE)
        if not m:
            return ""
        return (m.group(2) or m.group(3) or m.group(4) or "").strip()

    def _is_valid_url(url: str) -> bool:
        u = (url or "").strip().lower()
        return bool(u) and u.startswith(("http://", "https://"))

    def _strip_inner_tags(inner: str) -> str:
        """去掉 inner 里所有 <video>/<audio>/<img>/<figcaption> 标签，
        仅保留可能存在的其他内联文本。"""
        inner = re.sub(
            r'<(?:video|audio|img)\b[^>]*/?>|</?(?:video|audio|img)\s*>',
            '', inner, flags=re.IGNORECASE,
        )
        # figcaption 文本由调用方单独提取，这里把已取过文本的空标签也清掉
        inner = re.sub(
            r'<figcaption\b[^>]*>.*?</figcaption\s*>|</?figcaption\s*>',
            '', inner, flags=re.IGNORECASE | re.DOTALL,
        )
        return inner

    def _figcaption_text(inner: str) -> str:
        m = re.search(
            r'<figcaption\b[^>]*>(.*?)</figcaption\s*>',
            inner, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return ""
        text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        return text

    # 0) 处理 <tg-slideshow> 轮播容器：容器内只认裸 <img>（项目规范，
    #    见 fetch_rich_content.py），降级产生的 <a> 锚点必须移出容器——
    #    锚点留在容器内会被渲染器直接吞掉，用户只看到剩余 <img> 而拿不到
    #    链接。匹配类型的媒体全部移除后若容器内不再有任何媒体，解包容器。
    all_media_re = re.compile(
        r'<(video|audio)\b[^>]*>.*?</\1\s*>|<(video|audio|img)\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_slideshow(m: re.Match) -> str:
        open_tag, inner, close_tag = m.group(1), m.group(2) or "", m.group(3)
        anchors: list[str] = []

        def _check(mm: re.Match) -> str:
            block = mm.group(0)
            kind = (mm.group(1) or mm.group(2) or "").lower()
            if media_kinds is not None and kind not in media_kinds:
                return block
            src = _extract_src(block)
            if not src or not _is_valid_url(src):
                return ""  # 非法 src：整块删除（冗余兜底）
            anchors.append(_build_demoted_anchor(src))
            return ""

        new_inner = all_media_re.sub(_check, inner)
        if not anchors:
            return m.group(0)
        if _slideshow_inner_has_media(new_inner):
            body = f"{open_tag}{new_inner}{close_tag}"
        else:
            body = _unwrap_slideshow_inner(new_inner)
        # 多个锚点用 <br/> 分行：可见文本都是域名时，紧挨着会粘成
        # 一串无法分辨的重复域名（br 在 Rich Message 白名单内）。
        return body + "<br/>".join(anchors)

    result = _TG_SLIDESHOW_RE.sub(_replace_slideshow, html_content)

    # 1) 处理 <figure>...</figure> 内的媒体（含 figcaption）
    figure_re = re.compile(
        r'<figure\b[^>]*>(.*?)</figure\s*>',
        re.IGNORECASE | re.DOTALL,
    )
    media_in_figure_re = re.compile(
        r'<(video|audio|img)\b[^>]*?/?>.*?</\1\s*>|<(video|audio|img)\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_figure(m: re.Match) -> str:
        inner = m.group(1) or ""
        mm = media_in_figure_re.search(inner)
        if not mm:
            # 没有 media——保留原 figure（可能是纯文字 figure）
            return m.group(0)
        # 取 media 标签名（video / audio / img）
        kind = (mm.group(1) or mm.group(2) or "").lower()
        block = mm.group(0)
        src = _extract_src(block)
        # figcaption 文本先提取，不论 src 是否合法，都应作为可见内容保留
        cap_text = _figcaption_text(inner)
        if media_kinds is not None and kind not in media_kinds:
            return m.group(0)
        if not src or not _is_valid_url(src):
            # 非法 src：删除该 media 块，但保留 figcaption 文本作为可见内容
            rest = inner.replace(block, "")
            rest = _strip_inner_tags(rest).strip()
            if cap_text:
                rest = f"{cap_text} {rest}".strip() if rest else cap_text
            return rest or ""
        anchor = _build_demoted_anchor(src)
        # 把 media 块和 figcaption 都从 inner 里去掉，剩余文本追加在后
        rest = _strip_inner_tags(inner.replace(block, "")).strip()
        if rest:
            return f"{anchor} {rest}"
        return anchor

    result = figure_re.sub(_replace_figure, result)

    # 2) 处理裸 media（不在 <figure>/<tg-slideshow> 里的）
    bare_media_re = re.compile(
        r'<(video|audio)\b[^>]*>.*?</\1\s*>|<(video|audio|img)\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_bare_media(m: re.Match) -> str:
        block = m.group(0)
        kind = (m.group(1) or m.group(2) or "").lower()
        src = _extract_src(block)
        if media_kinds is not None and kind not in media_kinds:
            return block
        if not src or not _is_valid_url(src):
            return ""  # 非法 src：整块删除
        return _build_demoted_anchor(src)

    result = bare_media_re.sub(_replace_bare_media, result)

    # 3) 清理可能残留的空 <figure>...</figure>（内部 media 已被替换为 <a>，
    #    figure 容器不再需要）
    result = re.sub(
        r'<figure\b[^>]*>\s*</figure\s*>',
        '', result, flags=re.IGNORECASE,
    )
    return result


def strip_html_tags(text: str) -> str:
    if not text:
        return ""
    text = text.replace("<br/>", "\n").replace("<br>", "\n")
    return re.sub(r'<[^>]*>', '', text)


def _rich_message_plain_text_fallback(html_content: str) -> str:
    """将被 Rich Message 服务端拒绝的 HTML 降级为一个安全的段落。

    模型或工具输出有时仅包含内联标签，或在 ``details`` 中缺少块级内容。此类
    HTML 视觉上并非空白，但 Telegram 会返回 ``RICH_MESSAGE_CONTENT_REQUIRED``。
    这里保留其可见文本并转义为单个 ``<p>``，以保证用户不会因格式问题丢失回复。

    顺序很重要：先 strip 标签，再 unescape 实体，最后**重新转义**输出，避免
    上游 HTML 中的 ``&lt;script&gt;`` 被 unescape 后再次注入回最终 HTML。
    """
    visible_text = strip_html_tags(html_content or "")
    visible_text = html.unescape(visible_text)
    visible_text = re.sub(r"\s+", " ", visible_text).strip()
    if not visible_text:
        return ""
    # 重新转义，确保 unescape 出来的 <、>、& 不会被当作 HTML。
    visible_text = _SMART_AMP_PATTERN.sub('&amp;', visible_text)
    visible_text = visible_text.replace('<', '&lt;').replace('>', '&gt;')
    return f"<p>{visible_text}</p>"
