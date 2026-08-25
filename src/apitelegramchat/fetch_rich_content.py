# fetch_rich_content.py — fetch_url 结果的 Telegram Rich Message HTML 提取引擎
#
# 职责：
#   1. 将 trafilatura 的 XML 输出（含 <ref>/<graphic>/<list>/<table>/<hi> 等结构）
#      转换为项目系统提示词中定义的 Telegram HTML 子集；
#   2. 从原始 HTML 中提取 trafilatura 会丢失的媒体资源：内嵌 <video>/<audio>、
#      <iframe>/<embed> 播放器（YouTube/Bilibili/Vimeo 等，规范化为可读链接）、
#      懒加载图片（data-src/srcset）、Open Graph 视频与图片、JSON-LD VideoObject；
#   3. 组装最终 fetch_url 工具结果：标题 + 来源链接 + 正文富 HTML + 媒体区块，
#      并在固定字符预算内做"整块截断"（绝不截断在标签中间）。
#
# 设计约束（对齐系统提示词与 rich_message_builder 的解析规则）：
#   - 媒体（<img>/<video>/<audio>/<figure>）必须作为独立块级元素，严禁出现在
#     <p>/<li>/<td>/行内容器中 → 转换时统一"提升为兄弟块"；
#   - <li> 与表格单元格内仅允许行内格式元素 → 嵌套列表/媒体一律提升到列表之后；
#   - <tg-slideshow> 内只放裸 <img src="..."/>，不放 <figure>；
#   - 失败结果（"失败：xxx"）仍由 search_engine 以纯文本生成，本模块只负责成功路径；
#   - 输出总长度受 FETCH_RICH_MAX_LEN 约束，保证低于 tool_executors 的
#     MAX_TOOL_RESPONSE_LEN（16000），避免朴素切片截断破坏 HTML 结构。
from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse, urlsplit, urlunsplit

try:
    from lxml import etree as _etree
    from lxml import html as _lxml_html
except Exception:  # pragma: no cover - lxml 为硬依赖，仅防御性兜底
    _etree = None
    _lxml_html = None

logger = logging.getLogger(__name__)

# fetch 结果的 HTML 总预算。必须小于 tool_executors.MAX_TOOL_RESPONSE_LEN(16000)，
# 这样 _truncate_tool_result 的朴素切片永远不会作用在 fetch_url 的 HTML 上。
FETCH_RICH_MAX_LEN = 14000
# 正文（不含媒体区）占用的预算，给标题/链接/媒体区留余量。
FETCH_BODY_MAX_LEN = 11000

# 媒体数量上限：防止图库/相册类页面把工具结果塞满 <img>。
MAX_IMAGES = 8
MAX_VIDEOS = 4
MAX_EMBEDS = 5
MAX_AUDIOS = 2
MAX_LINK_TEXT_LEN = 120
MAX_CAPTION_LEN = 200

# 被判定为装饰性/跟踪用途的图片文件名特征。
_ICONISH_NAME_RE = re.compile(
    r"(sprite|spacer|pixel|blank|separator|divider|avatar|badge|icon|logo|favicon|"
    r"button|arrow_left|arrow_right|loading|placeholder|1x1|transparent|rating|star)",
    re.IGNORECASE,
)
# 允许作为 <img src> 的扩展名（无扩展名时也放行，交给渲染端处理）。
_IMAGE_EXT_OK = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".heic", ".svg",
}
_VIDEO_EXT = {".mp4", ".webm", ".ogg", ".ogv", ".mov", ".m4v", ".m3u8", ".ts"}
_AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".flac", ".opus"}

# 已知 iframe 播放器 → 规范化观看链接 的匹配规则。
# 每条：(host 正则, path 正则, 构造函数/None, 提供方标签)。
_EMBED_RULES = [
    (
        re.compile(r"(^|\.)(youtube(-nocookie)?|youtube\.googleapis)\.com$", re.I),
        re.compile(r"^/embed/([\w-]{6,})"),
        lambda m: f"https://www.youtube.com/watch?v={m.group(1)}",
        "YouTube",
    ),
    (
        re.compile(r"^player\.vimeo\.com$", re.I),
        re.compile(r"^/video/(\d+)"),
        lambda m: f"https://vimeo.com/{m.group(1)}",
        "Vimeo",
    ),
    (
        re.compile(r"(^|\.)dailymotion\.com$", re.I),
        re.compile(r"^/embed/video/([\w-]+)"),
        lambda m: f"https://www.dailymotion.com/video/{m.group(1)}",
        "Dailymotion",
    ),
    (
        re.compile(r"(^|\.)bilibili\.com$", re.I),
        # /blackboard/html5player.html?bvid=BV... 或 /player.html?aid=...
        re.compile(r"^/(?:blackboard/html5player\.html|player\.html)$"),
        None,  # 参数级处理（bvid/aid），见 _canonicalize_embed
        "Bilibili",
    ),
    (
        re.compile(r"(^|\.)youku\.com$", re.I),
        re.compile(r"^/embed/([\w=]+)"),
        lambda m: f"https://v.youku.com/v_show/id_{m.group(1)}.html",
        "优酷",
    ),
]

# 这些域名的 iframe 无法转换成"观看页"，但作为外链列出仍然有价值。
_EMBED_HOST_LABELS = [
    (re.compile(r"(^|\.)(twitter|x)\.com$", re.I), "X / Twitter"),
    (re.compile(r"(^|\.)(facebook|fb)\.com$", re.I), "Facebook"),
    (re.compile(r"(^|\.)instagram\.com$", re.I), "Instagram"),
    (re.compile(r"(^|\.)tiktok\.com$", re.I), "TikTok"),
    (re.compile(r"(^|\.)soundcloud\.com$", re.I), "SoundCloud"),
    (re.compile(r"(^|\.)ted\.com$", re.I), "TED"),
    (re.compile(r"(^|\.)open\.spotify\.com$", re.I), "Spotify"),
    (re.compile(r"(^|\.)music\.163\.com$", re.I), "网易云音乐"),
    (re.compile(r"(^|\.)iqiyi\.com$", re.I), "爱奇艺"),
    (re.compile(r"(^|\.)(tencentvideo|v\.qq)\.com$", re.I), "腾讯视频"),
    (re.compile(r"(^|\.)douyin\.com$", re.I), "抖音"),
    (re.compile(r"(^|\.)kuaishou\.com$", re.I), "快手"),
    (re.compile(r"(^|\.)weibo\.com$", re.I), "微博"),
]


def esc(text) -> str:
    """转义文本节点的 < > &（lxml 已把实体解码为纯文本，标准转义即可）。"""
    if text is None:
        return ""
    return _html.escape(str(text), quote=False)


def esc_attr(value) -> str:
    """转义将写入 href/src 等属性的值。"""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _sanitize_url(raw: Optional[str], base_url: str = "") -> Optional[str]:
    """规范化 URL：仅 http/https、去掉 fragment、基于 base_url 补全相对路径。"""
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or candidate.startswith("#"):
        return None
    low = candidate[:32].lower()
    if low.startswith((
        "javascript:", "data:", "blob:", "vbscript:", "about:",
        "file:", "mailto:", "tel:", "cid:",
    )):
        return None
    if base_url:
        try:
            candidate = urljoin(base_url, candidate)
        except Exception:
            return None
    try:
        parts = urlsplit(candidate)
    except Exception:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")) or candidate


def _pick_srcset_best(srcset: Optional[str]) -> Optional[str]:
    """从 srcset 中选择描述尺寸最大的候选（启发式：数值最大者通常最清晰）。"""
    if not srcset:
        return None
    best_url, best_w = None, -1
    for part in srcset.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        url = tokens[0]
        width = -1
        for tok in tokens[1:]:
            m = re.match(r"^(\d+(?:\.\d+)?)x$", tok)
            if m:
                width = int(float(m.group(1)) * 1000)
                break
            m = re.match(r"^(\d+)w$", tok)
            if m:
                width = int(m.group(1))
                break
        if width > best_w:
            best_url, best_w = url, width
    return best_url


def _is_probably_decorative(url: str) -> bool:
    """过滤图标 / 间距图 / 跟踪像素等装饰性图片。"""
    try:
        path = urlsplit(url).path.lower()
    except Exception:
        return True
    name = path.rsplit("/", 1)[-1]
    if _ICONISH_NAME_RE.search(name):
        return True
    # 1x1 / 0x0 尺寸特征（含路径中的尺寸段）。
    if re.search(r"(?:^|[/_-])(?:1x1|0x0|2x2)(?:[._/-]|$)", path):
        return True
    return False


# ---------------------------------------------------------------------------
# 1) 媒体提取（原始 HTML / OG / JSON-LD）
# ---------------------------------------------------------------------------


@dataclass
class MediaAsset:
    url: str
    label: str = ""
    source: str = ""   # video / embed / audio / image
    provider: str = ""  # embed 专用：提供方标签


@dataclass
class PageMedia:
    videos: list[MediaAsset] = field(default_factory=list)   # 可直接播放的视频文件
    embeds: list[MediaAsset] = field(default_factory=list)   # iframe/嵌入播放器 → 观看链接
    audios: list[MediaAsset] = field(default_factory=list)   # 可直接播放的音频文件
    images: list[MediaAsset] = field(default_factory=list)   # 页面图片（含懒加载/OG）
    og_title: str = ""
    og_description: str = ""

    def has_any(self) -> bool:
        return bool(self.videos or self.embeds or self.audios or self.images)


def _meta_content(tree, names: set[str]) -> str:
    for meta in tree.iter("meta"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").strip().lower()
        if key in names:
            content = (meta.get("content") or "").strip()
            if content:
                return content
    return ""


def _canonicalize_embed(raw_url: str, base_url: str) -> Optional[tuple[str, str]]:
    """把 iframe/embed 的 src 规范化为观看页链接。返回 (url, provider_label)。"""
    url = _sanitize_url(raw_url, base_url)
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except Exception:
        return None
    host = (parts.hostname or "").lower()
    path = parts.path or "/"

    for host_re, path_re, build, label in _EMBED_RULES:
        if not host_re.search(host):
            continue
        m = path_re.match(path)
        if m and build is not None:
            return build(m), label
        if label == "Bilibili":
            qs = parse_qs(parts.query)
            bvid = (qs.get("bvid") or qs.get("BV") or [None])[0]
            if bvid:
                return f"https://www.bilibili.com/video/{bvid}", "Bilibili"
            aid = (qs.get("aid") or [None])[0]
            if aid:
                return f"https://www.bilibili.com/video/av{aid}", "Bilibili"
    # 无规则匹配：已知社交/媒体站点或一般 http(s) iframe，原样作为外链。
    for host_re, label in _EMBED_HOST_LABELS:
        if host_re.search(host):
            return url, label
    return url, "嵌入内容"


def _walk_jsonld(node, media: PageMedia, base_url: str, depth: int = 0):
    """递归遍历 JSON-LD（含 @graph / 数组），收集 VideoObject/AudioObject/ImageObject。"""
    if depth > 6 or node is None:
        return
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, media, base_url, depth + 1)
        return
    if not isinstance(node, dict):
        return
    node_type = node.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    types = {str(t).lower() for t in types if t}
    name = str(node.get("name") or node.get("headline") or "").strip()
    if "videoobject" in types:
        for key in ("contentUrl", "embedUrl"):
            raw = node.get(key)
            if isinstance(raw, str) and raw:
                url = _sanitize_url(raw, base_url)
                if url:
                    media.videos.append(MediaAsset(url=url, label=name, source="video"))
                    break
    if "audioobject" in types:
        raw = node.get("contentUrl")
        if isinstance(raw, str) and raw:
            url = _sanitize_url(raw, base_url)
            if url:
                media.audios.append(MediaAsset(url=url, label=name, source="audio"))
    if "imageobject" in types:
        raw = node.get("contentUrl") or node.get("url")
        if isinstance(raw, str) and raw:
            url = _sanitize_url(raw, base_url)
            if url and not _is_probably_decorative(url):
                media.images.append(MediaAsset(url=url, label=name, source="image"))
    for key in ("@graph", "hasPart", "mainEntity", "subjectOf", "associatedMedia"):
        child = node.get(key)
        if child:
            _walk_jsonld(child, media, base_url, depth + 1)


_IMG_LAZY_ATTRS = (
    "src", "data-src", "data-original", "data-lazy-src", "data-actualsrc",
    "data-echo", "data-url", "data-image", "data-original-src",
)


def extract_embedded_media(html_text: str, base_url: str) -> PageMedia:
    """从原始 HTML 提取内嵌视频 / 播放器 / 音频 / 图片（含 OG 与 JSON-LD）。"""
    media = PageMedia()
    if _lxml_html is None or not html_text:
        return media
    try:
        parser = _lxml_html.HTMLParser(recover=True, encoding="utf-8", huge_tree=True)
        tree = _lxml_html.fromstring(html_text, parser=parser)
    except Exception:
        try:
            tree = _lxml_html.fromstring(html_text)
        except Exception as e:
            logger.debug(f"[fetch_rich] 媒体提取解析失败: {e}")
            return media

    media.og_title = _meta_content(tree, {"og:title", "twitter:title"})
    media.og_description = _meta_content(
        tree, {"og:description", "twitter:description", "description"}
    )

    seen: set[str] = set()

    def _seen(url: str) -> bool:
        if url in seen:
            return True
        seen.add(url)
        return False

    # ---- <video> / <source> ----
    for video_el in tree.iter("video"):
        candidates: list[str] = []
        if video_el.get("src"):
            candidates.append(video_el.get("src"))
        for source_el in video_el.iter("source"):
            if source_el.get("src"):
                candidates.append(source_el.get("src"))
        poster = _sanitize_url(video_el.get("poster"), base_url)
        if poster and not _is_probably_decorative(poster) and not _seen(poster):
            media.images.append(MediaAsset(url=poster, label="视频封面", source="image"))
        for raw in candidates:
            url = _sanitize_url(raw, base_url)
            if not url or _seen(url):
                continue
            media.videos.append(MediaAsset(url=url, label="", source="video"))
            if len(media.videos) >= MAX_VIDEOS:
                break

    # ---- <audio> / <source> ----
    for audio_el in tree.iter("audio"):
        candidates = []
        if audio_el.get("src"):
            candidates.append(audio_el.get("src"))
        for source_el in audio_el.iter("source"):
            if source_el.get("src"):
                candidates.append(source_el.get("src"))
        for raw in candidates:
            url = _sanitize_url(raw, base_url)
            if url and not _seen(url):
                media.audios.append(MediaAsset(url=url, label="", source="audio"))
                if len(media.audios) >= MAX_AUDIOS:
                    break

    # ---- <iframe> / <embed> 播放器 ----
    for tag in ("iframe", "embed"):
        for el in tree.iter(tag):
            raw = el.get("src") or el.get("data-src")
            if not raw:
                continue
            resolved = _canonicalize_embed(raw, base_url)
            if not resolved:
                continue
            url, provider = resolved
            if _seen(url):
                continue
            title = (el.get("title") or el.get("aria-label") or "").strip()
            media.embeds.append(
                MediaAsset(url=url, label=title[:MAX_LINK_TEXT_LEN], source="embed", provider=provider)
            )
            if len(media.embeds) >= MAX_EMBEDS:
                break

    # ---- Open Graph 视频 / 音频 ----
    og_video = _sanitize_url(
        _meta_content(tree, {"og:video", "og:video:secure_url", "og:video:url", "twitter:player:stream"}),
        base_url,
    )
    if og_video and not _seen(og_video):
        media.videos.append(MediaAsset(url=og_video, label="", source="video"))

    og_audio = _sanitize_url(_meta_content(tree, {"og:audio", "og:audio:secure_url"}), base_url)
    if og_audio and not _seen(og_audio):
        media.audios.append(MediaAsset(url=og_audio, label="", source="audio"))

    # ---- JSON-LD ----
    for script in tree.iter("script"):
        if (script.get("type") or "").strip().lower() != "application/ld+json":
            continue
        payload = (script.text or "").strip()
        if not payload:
            continue
        try:
            _walk_jsonld(json.loads(payload), media, base_url)
        except Exception:
            continue

    # ---- <img>（含懒加载属性与 srcset）----
    for img_el in tree.iter("img"):
        raw = None
        for attr in _IMG_LAZY_ATTRS:
            val = img_el.get(attr)
            if val and not val.strip().startswith("data:"):
                raw = val
                break
        if raw is None:
            best = _pick_srcset_best(img_el.get("srcset") or img_el.get("data-srcset"))
            if best:
                raw = best
        if raw is None:
            continue
        url = _sanitize_url(raw, base_url)
        if not url or _is_probably_decorative(url) or _seen(url):
            continue
        alt = (img_el.get("alt") or "").strip()
        media.images.append(MediaAsset(url=url, label=alt[:MAX_CAPTION_LEN], source="image"))
        if len(media.images) >= MAX_IMAGES:
            break

    # ---- Open Graph 图片（放最后，作为兜底）----
    og_image = _sanitize_url(
        _meta_content(tree, {"og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"}),
        base_url,
    )
    if og_image and not _is_probably_decorative(og_image) and not _seen(og_image):
        media.images.append(MediaAsset(url=og_image, label="页面主图", source="image"))

    # ---- 数量上限裁剪 ----
    media.videos = media.videos[:MAX_VIDEOS]
    media.embeds = media.embeds[:MAX_EMBEDS]
    media.audios = media.audios[:MAX_AUDIOS]
    media.images = media.images[:MAX_IMAGES]
    return media


# ---------------------------------------------------------------------------
# 2) trafilatura XML → Telegram Rich HTML
# ---------------------------------------------------------------------------

# trafilatura <hi rend="#b #i"> → Telegram 标签映射。
_REND_MAP = {
    "#b": "b", "b": "b", "bold": "b",
    "#i": "i", "i": "i", "italic": "i", "em": "i",
    "#u": "u", "u": "u", "underline": "u",
    "#s": "s", "s": "s", "strike": "s", "del": "s",
    "#sup": "sup", "sup": "sup",
    "#sub": "sub", "sub": "sub",
    "#code": "code", "code": "code",
    "#mark": "mark", "mark": "mark",
}


def _rend_to_tags(rend: Optional[str]) -> list[str]:
    if not rend:
        return []
    tags: list[str] = []
    for token in rend.replace(",", " ").split():
        tag = _REND_MAP.get(token) or _REND_MAP.get(token.lstrip("#"))
        if tag and tag not in tags:
            tags.append(tag)
    return tags


class _ConvertContext:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.used_media_urls: set[str] = set()


def _local_name(el) -> str:
    name = getattr(el, "tag", "")
    if not isinstance(name, str):
        return ""
    return name.rsplit("}", 1)[-1].lower()


def _convert_inline_element(el, ctx: "_ConvertContext") -> tuple[str, list[str]]:
    """转换单个行内元素（含其标签）。返回 (行内 HTML, 需提升的媒体块列表)。"""
    tag = _local_name(el)
    inner, media_blocks = _convert_inline_content(el, ctx)

    if tag == "hi":
        tags = _rend_to_tags(el.get("rend"))
        if not tags:
            return inner, media_blocks
        open_seq = "".join(f"<{t}>" for t in tags)
        close_seq = "".join(f"</{t}>" for t in reversed(tags))
        return f"{open_seq}{inner}{close_seq}", media_blocks

    if tag == "ref":
        target = _sanitize_url(el.get("target"), ctx.base_url)
        if target:
            text = inner.strip() or esc(target[:MAX_LINK_TEXT_LEN])
            return f'<a href="{esc_attr(target)}">{text}</a>', media_blocks
        return inner, media_blocks

    if tag == "code":
        return f"<code>{inner}</code>", media_blocks

    if tag == "lb":
        return "<br/>", media_blocks

    if tag in ("graphic", "media"):
        block = _render_media_element(el, ctx)
        return ("", [block]) if block else ("", [])

    # 未知行内元素：透明处理。
    return inner, media_blocks


def _convert_inline_content(el, ctx: "_ConvertContext") -> tuple[str, list[str]]:
    """转换元素的混合内容（el.text + 子元素 + 各 tail）。"""
    parts: list[str] = []
    media_blocks: list[str] = []
    if el.text:
        parts.append(esc(el.text))
    for child in el:
        frag, blocks = _convert_inline_element(child, ctx)
        media_blocks.extend(blocks)
        if frag:
            parts.append(frag)
        if child.tail:
            parts.append(esc(child.tail))
    return "".join(parts), media_blocks


def _render_media_element(el, ctx: "_ConvertContext") -> Optional[str]:
    """<graphic>/<media> → <img/> 或 <video/>/<audio/> 独立块。"""
    tag = _local_name(el)
    raw_src = el.get("src") or el.get("target") or el.get("url")
    url = _sanitize_url(raw_src, ctx.base_url)
    if not url or url in ctx.used_media_urls:
        return None
    ctx.used_media_urls.add(url)

    if tag == "graphic":
        if _is_probably_decorative(url):
            return None
        alt = (el.get("alt") or "").strip()
        caption = (el.get("title") or alt)[:MAX_CAPTION_LEN]
        if caption:
            return (
                f'<figure><img src="{esc_attr(url)}"/>'
                f"<figcaption>{esc(caption)}</figcaption></figure>"
            )
        return f'<img src="{esc_attr(url)}"/>'

    # <media>：按 mime 或扩展名判断类型。
    mime = (el.get("mime") or "").lower()
    path = urlsplit(url).path
    path_ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path else ""
    if mime.startswith("video/") or (not mime and path_ext in _VIDEO_EXT):
        return f'<video src="{esc_attr(url)}"/>'
    if mime.startswith("audio/") or (not mime and path_ext in _AUDIO_EXT):
        return f'<audio src="{esc_attr(url)}"/>'
    return None


# 仅由标点/空白组成的"碎片段落"（维基百科信息框等场景会产生 <p>：</p>）。
_PUNCT_ONLY_RE = re.compile(r"^[\s\u3000、：:，。；;·•—–\-\|/\\()（）\[\]【】<>«»“”‘’'\"`~!！?？…*#+= ]+$")


def _is_punct_only(text: str) -> bool:
    """判断可见文本是否只有标点/空白（用于过滤碎片段落）。"""
    if not text:
        return True
    return bool(_PUNCT_ONLY_RE.match(text))


def _render_table(el, ctx: "_ConvertContext") -> str:
    rows_html: list[str] = []
    has_visible_text = False
    for row in el:
        if _local_name(row) != "row":
            continue
        cells: list[str] = []
        for cell in row:
            if _local_name(cell) != "cell":
                continue
            # 单元格内仅允许行内格式元素；单元格里的媒体按约束丢弃。
            inner, _media = _convert_inline_content(cell, ctx)
            if not _is_punct_only(re.sub(r"<[^>]+>", "", inner).strip()):
                has_visible_text = True
            attrs = []
            for attr in ("colspan", "rowspan"):
                val = (cell.get(attr) or "").strip()
                if val.isdigit() and val != "1":
                    attrs.append(f'{attr}="{int(val)}"')
            attr_str = (" " + " ".join(attrs)) if attrs else ""
            role = (cell.get("role") or "").strip().lower()
            if role in ("head", "header"):
                cells.append(f"<td{attr_str}><b>{inner}</b></td>")
            else:
                cells.append(f"<td{attr_str}>{inner}</td>")
        if cells:
            rows_html.append("<tr>" + "".join(cells) + "</tr>")
    if not rows_html or not has_visible_text:
        # 空表格（如维基百科信息框的布局表格）不输出。
        return ""
    return f'<table bordered striped>{"".join(rows_html)}</table>'


def _render_list(el, ctx: "_ConvertContext") -> tuple[str, list[str]]:
    """渲染 <list>。<li> 仅承载行内内容；嵌套列表与媒体提升为列表之后的兄弟块。"""
    rend = (el.get("rend") or "ul").strip().lower()
    outer = "ol" if rend in ("ol", "ol#", "ordered") else "ul"
    items: list[str] = []
    after_blocks: list[str] = []

    for item in el:
        if _local_name(item) != "item":
            continue
        inline_parts: list[str] = []
        if item.text:
            inline_parts.append(esc(item.text))
        for child in item:
            tag = _local_name(child)
            if tag == "list":
                nested_html, nested_after = _render_list(child, ctx)
                if nested_html:
                    after_blocks.append(nested_html)
                after_blocks.extend(nested_after)
            else:
                frag, media_blocks = _convert_inline_element(child, ctx)
                after_blocks.extend(media_blocks)
                if frag:
                    inline_parts.append(frag)
            if child.tail:
                inline_parts.append(esc(child.tail))
        content = "".join(inline_parts).strip()
        # li 内不能放裸 <p>（行内容器约束）；文本为空但存在提升块时也保留占位。
        items.append(f"<li>{content}</li>" if content else "<li>—</li>")

    if not items:
        return "", after_blocks
    return f"<{outer}>{''.join(items)}</{outer}>", after_blocks


# 这些 trafilatura 元素在容器层级出现时应视为"行内片段"，与前后兄弟合并成段，
# 而不是各自渲染成独立块（维基百科 favor_precision 输出会把 <ref> 直接挂在
# <main> 下，尾巴文本散落为 ：、等碎片）。
_CONTAINER_BLOCK_CHILDREN = frozenset({
    "p", "head", "header", "list", "table", "quote", "code", "graphic", "media",
    "figure", "comments", "comment", "doc", "main", "article", "body", "front", "div",
})


def _render_container(el, ctx: "_ConvertContext") -> list[str]:
    """渲染容器元素：块级子元素逐个输出，行内子元素与尾巴合并为段落。"""
    out: list[str] = []
    if el.text and el.text.strip():
        out.append(f"<p>{esc(el.text.strip())}</p>")

    inline_acc: list[str] = []

    def _flush_inline():
        text = "".join(inline_acc).strip()
        if text and not _is_punct_only(re.sub(r"<[^>]+>", "", text)):
            out.append(f"<p>{text}</p>")
        inline_acc.clear()

    for child in el:
        if _local_name(child) in _CONTAINER_BLOCK_CHILDREN:
            _flush_inline()
            out.extend(_render_block(child, ctx))
        else:
            # 行内元素出现在容器层级：与前后兄弟合并成一个段落。
            frag, media_blocks = _convert_inline_element(child, ctx)
            out.extend(media_blocks)
            if frag:
                inline_acc.append(frag)
            if child.tail:
                inline_acc.append(esc(child.tail))
    _flush_inline()
    return out


def _render_block(el, ctx: "_ConvertContext") -> list[str]:
    """把 trafilatura XML 的块级元素渲染为 Telegram HTML 块列表。"""
    tag = _local_name(el)
    out: list[str] = []

    if tag in ("doc", "main", "article", "body", "front", "div", "xml"):
        return _render_container(el, ctx)

    if tag in ("header", "head"):
        rend = (el.get("rend") or "").strip().lower()
        m = re.match(r"^h(\d)$", rend)
        level = int(m.group(1)) if m else 2
        level = max(1, min(6, level))
        inner, media_blocks = _convert_inline_content(el, ctx)
        text = inner.strip()
        if text and not _is_punct_only(re.sub(r"<[^>]+>", "", text)):
            out.append(f"<h{level}>{text}</h{level}>")
        out.extend(media_blocks)
        return out

    if tag == "p":
        inner, media_blocks = _convert_inline_content(el, ctx)
        text = inner.strip()
        # 碎片段落（如维基百科信息框拆出的 <p>：</p>）不输出。
        if text and not _is_punct_only(re.sub(r"<[^>]+>", "", text)):
            out.append(f"<p>{text}</p>")
        out.extend(media_blocks)
        return out

    if tag == "list":
        rendered, after_blocks = _render_list(el, ctx)
        if rendered:
            out.append(rendered)
        out.extend(after_blocks)
        return out

    if tag == "quote":
        inner, media_blocks = _convert_inline_content(el, ctx)
        text = inner.strip()
        if text and not _is_punct_only(re.sub(r"<[^>]+>", "", text)):
            out.append(f"<blockquote>{text}</blockquote>")
        out.extend(media_blocks)
        return out

    if tag == "code":
        # 块级代码：trafilatura 把 <pre> 输出为 main 直接子级的 <code>。
        code_text = "".join(el.itertext())
        if code_text.strip():
            out.append(f"<pre><code>{esc(code_text)}</code></pre>")
        return out

    if tag == "table":
        rendered = _render_table(el, ctx)
        if rendered:
            out.append(rendered)
        return out

    if tag in ("graphic", "media", "figure"):
        if tag == "figure":
            # trafilatura XML 一般不输出 figure；防御性支持。
            media_el = None
            caption = ""
            for child in el:
                child_tag = _local_name(child)
                if child_tag in ("graphic", "media"):
                    media_el = child
                elif child_tag == "caption":
                    caption = "".join(child.itertext()).strip()[:MAX_CAPTION_LEN]
            if media_el is not None:
                block = _render_media_element(media_el, ctx)
                if block and caption and block.startswith("<img"):
                    src = esc_attr(media_el.get("src") or "")
                    out.append(
                        f'<figure><img src="{src}"/>'
                        f"<figcaption>{esc(caption)}</figcaption></figure>"
                    )
                elif block:
                    out.append(block)
            return out
        block = _render_media_element(el, ctx)
        if block:
            out.append(block)
        return out

    if tag in ("comments", "comment"):
        return out  # 评论区不输出

    if tag == "lb":
        return out

    # 未知块级元素：按容器语义透明递归（行内子元素同样合并成段）。
    return _render_container(el, ctx)


def trafilatura_xml_to_rich_html(xml_text: str, base_url: str = "") -> list[str]:
    """trafilatura XML 输出 → Telegram Rich HTML 块列表。失败返回 []。"""
    if _etree is None or not xml_text or not xml_text.strip():
        return []
    try:
        root = _etree.fromstring(
            xml_text.encode("utf-8"),
            parser=_etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True),
        )
    except Exception as e:
        logger.debug(f"[fetch_rich] XML 解析失败: {e}")
        return []
    ctx = _ConvertContext(base_url)
    try:
        return _render_block(root, ctx)
    except Exception as e:
        logger.warning(f"[fetch_rich] XML→HTML 转换异常: {e}")
        return []


# ---------------------------------------------------------------------------
# 2.5) 正文 XML 提取策略（含中文页面退化检测）
# ---------------------------------------------------------------------------

_XML_BLOCK_RE = re.compile(r"<(?:p|head|list|table|quote|code|graphic|media)\b")
_RAW_HTML_BLOCK_RE = re.compile(r"<(?:p|h[1-6]|li|blockquote|pre|table)\b", re.IGNORECASE)


def _xml_block_count(xml_text: str) -> int:
    return len(_XML_BLOCK_RE.findall(xml_text or ""))


def _raw_html_block_count(html_text: str) -> int:
    return len(_RAW_HTML_BLOCK_RE.findall(html_text or ""))


def extract_body_blocks(html_text: str, base_url: str = "") -> list[str]:
    """用 trafilatura 提取正文并转为 Telegram HTML 块列表。

    提取策略（针对已知退化场景做了两级回退）：
      1. 默认参数提取；
      2. 结果为空 → 用 favor_recall=True 重试（稀疏页面）；
      3. 结果"退化"（块数 ≤1，而原始 HTML 明明有 ≥3 个块级元素）→
         用 favor_precision=True 重试，取结构更多的一份。
         典型场景：中文等无空格语言会击穿 trafilatura 基于词数的启发式，
         走 justext 回退路径，把多段落合并成单段并丢失行内格式/链接。
    """
    try:
        import trafilatura  # noqa: PLC0415 - 延迟导入避免硬依赖
    except Exception:
        return []
    kwargs = dict(
        output_format="xml",
        include_comments=False,
        include_tables=True,
        include_images=True,
        include_links=True,
        include_formatting=True,
    )
    xml_text = None
    try:
        xml_text = trafilatura.extract(html_text, **kwargs)
    except Exception as e:
        logger.debug(f"[fetch_rich] trafilatura XML 提取失败: {e}")
    if not xml_text:
        try:
            xml_text = trafilatura.extract(html_text, favor_recall=True, **kwargs)
        except Exception as e:
            logger.debug(f"[fetch_rich] favor_recall 提取失败: {e}")
    if (
        xml_text
        and _xml_block_count(xml_text) <= 1
        and _raw_html_block_count(html_text) >= 3
    ):
        try:
            alt = trafilatura.extract(html_text, favor_precision=True, **kwargs)
        except Exception as e:
            alt = None
            logger.debug(f"[fetch_rich] favor_precision 提取失败: {e}")
        if alt and _xml_block_count(alt) > _xml_block_count(xml_text):
            xml_text = alt
    if not xml_text:
        return []
    return trafilatura_xml_to_rich_html(xml_text, base_url)


# ---------------------------------------------------------------------------
# 3) 结果组装（标题 + 来源 + 正文 + 媒体区，预算内整块截断）
# ---------------------------------------------------------------------------


def _dedupe_keep_order(items: list[MediaAsset]) -> list[MediaAsset]:
    seen: set[str] = set()
    result: list[MediaAsset] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        result.append(item)
    return result


_SRC_ATTR_RE = re.compile(r'\b(?:src|href)\s*=\s*"([^"]+)"', re.IGNORECASE)


def _urls_embedded_in_blocks(blocks: list[str]) -> set[str]:
    """收集正文块里已经出现过的 src/href URL（用于媒体区去重）。"""
    urls: set[str] = set()
    for block in blocks:
        for raw in _SRC_ATTR_RE.findall(block):
            urls.add(_html.unescape(raw))
    return urls


_TAG_TEXT_RE = re.compile(r"<[^>]+>")


def _normalize_heading_text(text: str) -> str:
    return re.sub(r"\s+", "", _TAG_TEXT_RE.sub("", text or "")).lower()


def _truncate_blocks(blocks: list[str], max_len: int) -> tuple[list[str], bool]:
    """按最外层块截断到 max_len 以内；绝不截断在标签中间。"""
    kept: list[str] = []
    total = 0
    for block in blocks:
        if total + len(block) + 1 <= max_len:
            kept.append(block)
            total += len(block) + 1
        else:
            break
    return kept, len(kept) < len(blocks)


def build_fetch_rich_result(
    url: str,
    title: str,
    body_blocks: list[str],
    media: PageMedia,
    fallback_text: str = "",
) -> str:
    """组装 fetch_url 的最终 Telegram HTML 工具结果。

    结构（全部为系统提示词允许的 Telegram HTML 子集）：
      <h3>标题</h3>
      <p>🔗 <a href=...>domain</a></p>
      正文块……（预算内整块截断）
      <h4>🎬 视频</h4> + <figure><video …/>
      <h4>📺 内嵌播放器</h4> + <ul>观看链接
      <h4>🎵 音频</h4> + <figure><audio …/>
      <h4>🖼️ 图片</h4> + <tg-slideshow>/<img/>
    """
    media.videos = _dedupe_keep_order(media.videos)
    media.embeds = _dedupe_keep_order(media.embeds)
    media.audios = _dedupe_keep_order(media.audios)
    media.images = _dedupe_keep_order(media.images)

    domain = urlparse(url).netloc or url
    header_parts: list[str] = []
    clean_title = esc((title or "").strip()[:200])
    if clean_title:
        header_parts.append(f"<h3>{clean_title}</h3>")
    header_parts.append(f'<p>🔗 <a href="{esc_attr(url)}">{esc(domain)}</a></p>')

    # ---- 正文区准备 ----
    body_blocks = [b for b in body_blocks if b and b.strip()]
    if not body_blocks and fallback_text:
        # trafilatura 失败时的兜底：纯文本分段。
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", fallback_text) if p.strip()]
        if not paragraphs:
            paragraphs = [fallback_text.strip()]
        body_blocks = [f"<p>{esc(p[:2000])}</p>" for p in paragraphs[:40]]

    # 正文里已经内联出现的媒体不再重复进媒体区（图片/视频/音频均如此）。
    embedded_urls = _urls_embedded_in_blocks(body_blocks)
    if embedded_urls:
        media.images = [m for m in media.images if m.url not in embedded_urls]
        media.videos = [m for m in media.videos if m.url not in embedded_urls]
        media.audios = [m for m in media.audios if m.url not in embedded_urls]

    # 首个正文标题与页面标题重复时去掉，避免连续两个相同标题。
    if body_blocks and clean_title:
        first = body_blocks[0]
        m = re.match(r"^<h([1-6])>(.*?)</h\1>$", first.strip(), re.DOTALL)
        if m and _normalize_heading_text(m.group(2)) == _normalize_heading_text(title):
            body_blocks = body_blocks[1:]

    # ---- 媒体区（优先保证完整进入结果）----
    media_parts: list[str] = []
    if media.videos:
        media_parts.append("<h4>🎬 视频</h4>")
        for v in media.videos:
            cap = esc((v.label or "").strip()[:MAX_CAPTION_LEN])
            if cap:
                media_parts.append(
                    f'<figure><video src="{esc_attr(v.url)}"/><figcaption>{cap}</figcaption></figure>'
                )
            else:
                media_parts.append(f'<video src="{esc_attr(v.url)}"/>')
    if media.embeds:
        media_parts.append("<h4>📺 内嵌播放器</h4><ul>")
        for embed in media.embeds:
            provider = embed.provider or "嵌入内容"
            label = (embed.label or "").strip()
            link_text = f"{provider} · {label}" if label else provider
            link_text = link_text[:MAX_LINK_TEXT_LEN]
            media_parts.append(f'<li>▶ <a href="{esc_attr(embed.url)}">{esc(link_text)}</a></li>')
        media_parts.append("</ul>")
    if media.audios:
        media_parts.append("<h4>🎵 音频</h4>")
        for a in media.audios:
            cap = esc((a.label or "").strip()[:MAX_CAPTION_LEN])
            if cap:
                media_parts.append(
                    f'<figure><audio src="{esc_attr(a.url)}"/><figcaption>{cap}</figcaption></figure>'
                )
            else:
                media_parts.append(f'<audio src="{esc_attr(a.url)}"/>')
    if media.images:
        # <tg-slideshow> 内只放裸 <img>（不带 figure/figcaption）。
        bare_imgs = [f'<img src="{esc_attr(img.url)}"/>' for img in media.images]
        if len(bare_imgs) >= 2:
            media_parts.append("<h4>🖼️ 图片</h4><tg-slideshow>" + "".join(bare_imgs) + "</tg-slideshow>")
        elif bare_imgs:
            media_parts.append("<h4>🖼️ 图片</h4>" + bare_imgs[0])

    media_len = sum(len(p) + 1 for p in media_parts)
    header_len = sum(len(p) + 1 for p in header_parts)
    body_budget = max(1000, FETCH_BODY_MAX_LEN - media_len - header_len)
    kept_blocks, was_truncated = _truncate_blocks(body_blocks, body_budget)

    parts = header_parts + kept_blocks + media_parts
    if was_truncated:
        parts.append("<p>…（正文过长，已截断）</p>")

    result = "\n".join(parts)
    if len(result) > FETCH_RICH_MAX_LEN:
        # 兜底：极端情况下（媒体区超预算）再按块裁剪一次。
        parts2, trunc2 = _truncate_blocks(parts, FETCH_RICH_MAX_LEN - 60)
        if trunc2:
            parts2.append("<p>…（内容过长，已截断）</p>")
        result = "\n".join(parts2)
    return result


def extract_title_from_html(html_text: str) -> str:
    """og:title / twitter:title 优先，其次 <title>。"""
    if _lxml_html is None or not html_text:
        return ""
    try:
        parser = _lxml_html.HTMLParser(recover=True, encoding="utf-8", huge_tree=True)
        tree = _lxml_html.fromstring(html_text, parser=parser)
    except Exception:
        return ""
    og = _meta_content(tree, {"og:title", "twitter:title"})
    if og:
        return og.strip()[:200]
    try:
        title_el = tree.find(".//title")
        if title_el is not None and title_el.text:
            return re.sub(r"\s+", " ", title_el.text).strip()[:200]
    except Exception:
        pass
    return ""


def build_fallback_text_from_html(html_text: str, limit: int = 6000) -> str:
    """提取不到结构化正文时的纯文本兜底（meta description + 段落文本）。"""
    if _lxml_html is None or not html_text:
        return ""
    try:
        parser = _lxml_html.HTMLParser(recover=True, encoding="utf-8", huge_tree=True)
        tree = _lxml_html.fromstring(html_text, parser=parser)
    except Exception:
        return ""
    desc = _meta_content(tree, {"og:description", "twitter:description", "description"})
    chunks: list[str] = []
    if desc:
        chunks.append(desc.strip())
    for el in tree.iter("p"):
        text = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
        # 阈值 10 字符：中文段落信息密度高，12 个汉字已是完整句子；
        # 按 20 英文词校准的阈值会把中文正文全部过滤掉。
        if len(text) >= 10:
            chunks.append(text)
        if sum(len(c) for c in chunks) >= limit:
            break
    return "\n\n".join(chunks)[:limit]
