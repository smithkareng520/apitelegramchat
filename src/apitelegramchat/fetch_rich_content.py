# fetch_rich_content.py — fetch_url 的面向模型 Telegram Rich HTML 提取引擎
#
# 目标（两类受众严格分离）：
#   - 【模型上下文】execute_fetch_url 的返回值：忠实于原网页文档顺序的
#     Telegram HTML——标题/段落/列表/表格/链接/图片/视频/播放器都出现在它们
#     在原页面上的原始位置；轮播图（swiper/carousel/gallery 等容器）识别为
#     <tg-slideshow>。绝不把媒体集中堆到末尾"媒体区"。
#   - 【Telegram 工具 UI】由 tool_executors.format_tool_result 单独生成，
#     保持与历史版本相同的简单展示（标题 + 来源域名链接），本模块不管 UI。
#
# 实现链路：
#   1. trafilatura XML（保留链接/图片/格式/表格及其相对顺序）→ Telegram HTML 块；
#   2. 原始 HTML DOM 单次文档序遍历，收集带"文档位置"（order_idx/path）的媒体：
#      内嵌 <video>/<audio>、<iframe>/<embed> 播放器（规范化为观看链接）、
#      懒加载图片（data-src/srcset）；
#   3. 把每个正文块锚定到 DOM 元素（文本前向贪心匹配 / 图片 URL 匹配），
#      将 trafilatura 丢弃的媒体按原始位置插回正文流；
#   4. 轮播图检测：同容器内 >=2 张图片 → <tg-slideshow>（保持原位置）；
#   5. 固定字符预算内"整块截断"（绝不截断在标签中间）。
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
import logging
import re
from dataclasses import dataclass
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
FETCH_RICH_MAX_LEN = 20000
# 正文（含原位插入的媒体）占用的预算。页头（<h3> 标题 + 来源链接，最长约
# 330）+ 截断提示（约 30）后仍低于 FETCH_RICH_MAX_LEN，且留有充足余量
# 低于 MAX_TOOL_RESPONSE_LEN(16000)。
# 历史教训：曾设为 11000，内容丰富的页面（如维基百科条目）会把靠后的
# 表格（各话列表等）整块截掉——预算压缩（见 _demote_same_origin_links）
# 与本预算必须协同工作。
FETCH_BODY_MAX_LEN = 19400

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
# 1) DOM 媒体收集（带文档顺序位置，供"原位插入"使用）
# ---------------------------------------------------------------------------


@dataclass
class DomMedia:
    """DOM 中的单个媒体资源，携带文档位置信息。

    order_idx: 在 tree.iter() 文档序遍历中的序号（决定插入位置）。
    path:      lxml getpath() 的规范 XPath（用于内容容器边界与轮播分组）。
    kind:      video / audio / embed / image。
    carousel:  所属轮播容器的 path（无则 None）。
    skip:      轮播归并处理后置 True（不再单独插入正文流）。
    """

    order_idx: int
    path: str
    kind: str
    url: str
    label: str = ""
    provider: str = ""
    carousel: Optional[str] = None
    skip: bool = False
    boilerplate: bool = False  # 位于 nav/footer/aside 等样板区域内


def _parse_dom(html_text: str):
    """解析原始 HTML 为 lxml 树（容错：utf-8 recover → 裸 fromstring → None）。"""
    if _lxml_html is None or not html_text:
        return None
    try:
        parser = _lxml_html.HTMLParser(recover=True, encoding="utf-8", huge_tree=True)
        return _lxml_html.fromstring(html_text, parser=parser)
    except Exception:
        try:
            return _lxml_html.fromstring(html_text)
        except Exception as e:
            logger.debug(f"[fetch_rich] DOM 解析失败: {e}")
            return None


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


# 轮播/画廊容器特征（class/id/role/data-component 文本）。
_CAROUSEL_HINT_RE = re.compile(
    r"(swiper|carousel|slider|slideshow|gallery|slick|splide|glide|flickity|owl|slides)",
    re.IGNORECASE,
)


def _is_hidden_element(el) -> bool:
    """过滤隐藏 / 零尺寸的跟踪型媒体元素。"""
    style = (el.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    if (el.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if "hidden" in (el.get("class") or "").lower():
        return True
    if el.get("width") == "0" or el.get("height") == "0":
        return True
    return False


def _find_carousel_ancestor(el) -> Optional[str]:
    """返回轮播容器的 XPath。

    取 body 以下【最外层】的轮播特征祖先：swiper 结构中每个 .swiper-slide
    项自身也匹配特征，但各项 path 不同无法分组；只有共享的外层容器
    （.swiper / .gallery 等）才能把同轮播的图片归到同一组。
    """
    outermost: Optional[str] = None
    try:
        for anc in el.iterancestors():
            if _local_name(anc) in ("body", "html"):
                break
            hint = " ".join(filter(None, (
                anc.get("class"), anc.get("id"), anc.get("role"), anc.get("data-component"),
            )))
            if hint and _CAROUSEL_HINT_RE.search(hint):
                try:
                    outermost = anc.getroottree().getpath(anc)
                except Exception:
                    break
    except Exception:
        return outermost
    return outermost


_IMG_LAZY_ATTRS = (
    "src", "data-src", "data-original", "data-lazy-src", "data-actualsrc",
    "data-echo", "data-url", "data-image", "data-original-src",
)

_MEDIA_KIND_CAPS = {"video": MAX_VIDEOS, "audio": MAX_AUDIOS, "embed": MAX_EMBEDS, "image": MAX_IMAGES}


def _collect_dom_media(tree, base_url: str) -> list[DomMedia]:
    """单次文档序遍历收集全部媒体，携带 order_idx/path/carousel 位置信息。

    覆盖：内嵌 <video>/<source>、<audio>/<source>、<iframe>/<embed> 播放器
    （规范化为观看链接）、<img>（懒加载 data-* 属性与 srcset）。
    不收集 OG/JSON-LD 元数据媒体——它们不在页面文档流中，无法"原位"呈现；
    标题等元数据仍通过 extract_title_from_html 单独使用。
    """
    media: list[DomMedia] = []
    seen: set[str] = set()
    counts = {"video": 0, "audio": 0, "embed": 0, "image": 0}
    try:
        root_tree = tree.getroottree()
    except Exception:
        root_tree = None

    for order_idx, el in enumerate(tree.iter()):
        tag = _local_name(el)
        kind: Optional[str] = None
        url: Optional[str] = None
        label = ""
        provider = ""

        if tag in ("video", "audio"):
            kind = tag
            raw = el.get("src")
            if not raw:
                raw = next((s.get("src") for s in el.iter("source") if s.get("src")), None)
            url = _sanitize_url(raw, base_url)
        elif tag in ("iframe", "embed"):
            raw = el.get("src") or el.get("data-src")
            if raw:
                resolved = _canonicalize_embed(raw, base_url)
                if resolved:
                    url, provider = resolved
                    label = (el.get("title") or el.get("aria-label") or "").strip()[:MAX_LINK_TEXT_LEN]
                    kind = "embed"
        elif tag == "img":
            raw = None
            for attr in _IMG_LAZY_ATTRS:
                val = el.get(attr)
                if val and not val.strip().startswith("data:"):
                    raw = val
                    break
            if raw is None:
                best = _pick_srcset_best(el.get("srcset") or el.get("data-srcset"))
                if best:
                    raw = best
            url = _sanitize_url(raw, base_url)
            if url and not _is_probably_decorative(url):
                kind = "image"
                label = (el.get("alt") or "").strip()[:MAX_CAPTION_LEN]
                # 父级 <figure> 的 <figcaption> 作为图注。
                try:
                    parent = el.getparent()
                    if parent is not None and _local_name(parent) == "figure":
                        for sib in parent:
                            if _local_name(sib) == "figcaption":
                                cap = "".join(sib.itertext()).strip()[:MAX_CAPTION_LEN]
                                if cap:
                                    label = label or cap
                                break
                except Exception:
                    pass

        if not kind or not url or url in seen:
            continue
        if _is_hidden_element(el):
            continue
        if counts[kind] >= _MEDIA_KIND_CAPS[kind]:
            continue
        seen.add(url)
        counts[kind] += 1
        try:
            path = root_tree.getpath(el) if root_tree is not None else ""
        except Exception:
            path = ""
        media.append(DomMedia(
            order_idx=order_idx, path=path, kind=kind, url=url,
            label=label, provider=provider, carousel=_find_carousel_ancestor(el),
            boilerplate=_in_boilerplate(el),
        ))
    return media


# 样板区域标签：这些容器内的媒体（导航图、页脚 widget 等）不属于正文内容。
_BOILERPLATE_TAGS = frozenset({"nav", "footer", "aside"})


def _in_boilerplate(el) -> bool:
    """媒体元素是否位于 nav/footer/aside 样板容器内。

    注意 header 不算样板：文章内的 <header> 常包含标题与题图，
    属于正文内容；页面级 header 的 logo 等由装饰图过滤兜底。
    """
    try:
        for anc in el.iterancestors():
            if _local_name(anc) in _BOILERPLATE_TAGS:
                return True
    except Exception:
        return False
    return False




# ---------------------------------------------------------------------------
# 2) trafilatura XML → Telegram Rich HTML
# ---------------------------------------------------------------------------

# trafilatura <hi rend="#b #i"> → Telegram 标签映射。
# rend token 全集来自 trafilatura 1.12.2 htmlprocessing.REND_TAG_MAPPING 实测：
#   #b(b/strong) #i(i/em) #u(u) #t(kbd/samp/tt/var 等宽) #sub(sub) #sup(sup)；
# 删除线走独立的 <del> 元素（见 _convert_inline_element），rend="overstrike"
# 在多数版本的输出中已被清理，此处仅作防御性兜底。
_REND_MAP = {
    "#b": "b", "b": "b", "bold": "b",
    "#i": "i", "i": "i", "italic": "i", "em": "i",
    "#u": "u", "u": "u", "underline": "u",
    "#s": "s", "s": "s", "strike": "s", "del": "s", "overstrike": "s",
    "#t": "code", "t": "code",
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

    if tag == "del":
        # trafilatura 把 <s>/<del>/<strike> 统一转成 <del>（删除线），
        # 映射为 Telegram 的 <s>；空内容时不输出空标签。
        return (f"<s>{inner}</s>" if inner.strip() else inner), media_blocks

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
# 3) 结果组装（文档顺序原位插入媒体 + 轮播分组 + 预算内整块截断）
# ---------------------------------------------------------------------------

_TAG_TEXT_RE = re.compile(r"<[^>]+>")

# 正文块内媒体 src 提取（仅 src，不含 href——文本链接不算媒体已存在）。
_BLOCK_MEDIA_SRC_RE = re.compile(
    r'<(?:img|video|audio)\b[^>]*?\bsrc\s*=\s*"([^"]+)"', re.IGNORECASE
)


def _block_media_srcs(block: str) -> list[str]:
    """提取块内 <img>/<video>/<audio> 的 src 属性（已反转义）。"""
    return [_html.unescape(u) for u in _BLOCK_MEDIA_SRC_RE.findall(block or "")]


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


def _norm_text(text: str) -> str:
    """归一化文本用于锚点匹配：去全部空白（含全角空格）。"""
    return re.sub(r"[\s\u3000]+", "", text or "")


# 可作为锚点候选的 DOM 块级标签（不含 div/body 等容器，避免锚点过度前移）。
_ANCHOR_CANDIDATE_TAGS = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre",
    "td", "th", "figcaption",
})


def _anchor_text_match(block_text: str, cand_text: str) -> bool:
    """块文本与 DOM 候选文本的匹配判定。

    - 完全相等：直接匹配（覆盖短标题，如 4 字中文 h1）。
    - 候选文本是块文本的前缀 / 被包含：覆盖 trafilatura 合并段落场景。
    - 块文本是候选文本的前缀：覆盖 trafilatura 截断段落场景。
    """
    if not block_text or not cand_text:
        return False
    if block_text == cand_text:
        return True
    if len(cand_text) >= 8 and (block_text.startswith(cand_text) or cand_text in block_text):
        return True
    if len(block_text) >= 8 and cand_text.startswith(block_text):
        return True
    return False


def _anchor_entries(entries: list[dict], tree, media: list[DomMedia]) -> list[dict]:
    """为每个正文块确定 DOM 锚点（order/path）。

    策略：
      1. 文本匹配：DOM 文档序候选元素（p/h*/li/…）文本 vs 块可见文本，
         前向贪心（指针只前进，块本身按文档顺序产出）。
      2. 媒体 URL 匹配：纯图片/视频块（无文本）用其 src 在 DOM 媒体表中
         的位置作为锚点——图片块因此获得精确的原位锚定。
      3. 都失败：锚点为 None（交错时沿用上一个块的锚点）。
    """
    url_pos: dict[str, tuple[int, str]] = {}
    for m in media:
        url_pos.setdefault(m.url, (m.order_idx, m.path))

    cands: list[tuple[int, str, str]] = []
    try:
        root_tree = tree.getroottree()
    except Exception:
        root_tree = None
    if root_tree is not None:
        for order_idx, el in enumerate(tree.iter()):
            if _local_name(el) in _ANCHOR_CANDIDATE_TAGS:
                txt = _norm_text(_html.unescape("".join(el.itertext())))
                if len(txt) >= 4:
                    try:
                        cands.append((order_idx, root_tree.getpath(el), txt))
                    except Exception:
                        continue

    ptr = 0
    for entry in entries:
        block_text = _norm_text(_html.unescape(_TAG_TEXT_RE.sub("", entry["html"])))
        anchor: Optional[tuple[int, str]] = None
        if block_text:
            for j in range(ptr, len(cands)):
                cand_order, cand_path, cand_text = cands[j]
                if _anchor_text_match(block_text, cand_text):
                    anchor = (cand_order, cand_path)
                    ptr = j + 1
                    break
        if anchor is None:
            for src in _block_media_srcs(entry["html"]):
                if src in url_pos:
                    anchor = url_pos[src]
                    break
        if anchor is not None:
            entry["order"], entry["path"] = anchor
    return entries


def _is_standalone_img_block(block: str) -> bool:
    """块是否为单个图片块（<img/> 或 <figure><img/>…</figure>）。"""
    if not block:
        return False
    if len(re.findall(r"<img\b", block, re.IGNORECASE)) != 1:
        return False
    return bool(re.match(r"^<(img|figure)\b", block.strip(), re.IGNORECASE))


def _group_carousel_runs(entries: list[dict], url_to_carousel: dict[str, str]) -> list[dict]:
    """把"连续的、同轮播容器"的图片块合并成一个 <tg-slideshow> 条目。"""
    out: list[dict] = []
    run: list[dict] = []

    def _flush():
        nonlocal run
        if len(run) >= 2:
            imgs = []
            for e in run:
                srcs = _block_media_srcs(e["html"])
                if srcs:
                    imgs.append(f'<img src="{esc_attr(srcs[0])}"/>')
            if len(imgs) >= 2:
                out.append({
                    "html": "<tg-slideshow>" + "".join(imgs) + "</tg-slideshow>",
                    "order": run[0]["order"],
                    "path": run[0]["path"],
                })
                run = []
                return
        out.extend(run)
        run = []

    for entry in entries:
        srcs = _block_media_srcs(entry["html"])
        carousel_key = url_to_carousel.get(srcs[0]) if srcs else None
        if carousel_key and _is_standalone_img_block(entry["html"]):
            if run and run[-1].get("carousel") == carousel_key:
                run.append({**entry, "carousel": carousel_key})
                continue
            _flush()
            run = [{**entry, "carousel": carousel_key}]
            continue
        _flush()
        out.append(entry)
    _flush()
    return out


def _render_dom_media_block(m: DomMedia) -> str:
    """把 DOM 收集的媒体渲染为块级 Telegram HTML（在原位插入）。"""
    if m.kind == "video":
        cap = esc((m.label or "").strip()[:MAX_CAPTION_LEN])
        if cap:
            return f'<figure><video src="{esc_attr(m.url)}"/><figcaption>{cap}</figcaption></figure>'
        return f'<video src="{esc_attr(m.url)}"/>'
    if m.kind == "audio":
        cap = esc((m.label or "").strip()[:MAX_CAPTION_LEN])
        if cap:
            return f'<figure><audio src="{esc_attr(m.url)}"/><figcaption>{cap}</figcaption></figure>'
        return f'<audio src="{esc_attr(m.url)}"/>'
    if m.kind == "embed":
        provider = m.provider or "嵌入内容"
        label = (m.label or "").strip()
        link_text = f"{provider} · {label}" if label else provider
        link_text = link_text[:MAX_LINK_TEXT_LEN]
        return f'<p>▶ <a href="{esc_attr(m.url)}">{esc(link_text)}</a></p>'
    # image
    cap = esc((m.label or "").strip()[:MAX_CAPTION_LEN])
    if cap:
        return f'<figure><img src="{esc_attr(m.url)}"/><figcaption>{cap}</figcaption></figure>'
    return f'<img src="{esc_attr(m.url)}"/>'


def _sort_entries_by_anchor(entries: list[dict]) -> list[dict]:
    """按锚点 order 稳定排序正文块，使块本身回到 DOM 文档顺序。

    trafilatura 有时会把 <graphic> 等元素挪到 XML 末尾（例如段落之后的
    收尾位置），导致块顺序偏离原始页面。锚点排序后正文块与其 DOM 位置
    一致。无锚点块沿用前一个块的 order（稳定排序保持其相对位置）。
    """
    keyed: list[tuple[int, int, dict]] = []
    last: Optional[int] = None
    for idx, entry in enumerate(entries):
        order = entry.get("order")
        if order is None:
            order = last if last is not None else -1
        else:
            last = order
        keyed.append((order, idx, entry))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in keyed]


def _interleave(entries: list[dict], dropped: list[tuple[int, str]]) -> list[str]:
    """按锚点顺序把 dropped 媒体块插入正文块流。"""
    result: list[str] = []
    pending = sorted(dropped, key=lambda t: t[0])
    pi = 0
    last_order: Optional[int] = None
    for entry in entries:
        order = entry.get("order")
        cur = order if order is not None else last_order
        if order is not None:
            last_order = order
        if cur is not None:
            while pi < len(pending) and pending[pi][0] < cur:
                result.append(pending[pi][1])
                pi += 1
        result.append(entry["html"])
    while pi < len(pending):
        result.append(pending[pi][1])
        pi += 1
    return result


def _fallback_paragraph_blocks(fallback_text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", fallback_text or "") if p.strip()]
    if not paragraphs and fallback_text:
        paragraphs = [fallback_text.strip()]
    return [f"<p>{esc(p[:2000])}</p>" for p in paragraphs[:40]]


_SAME_ORIGIN_LINK_RE = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.DOTALL)


def _demote_same_origin_links(blocks: list[str], base_url: str) -> list[str]:
    """把指向同源（同 host）的 <a> 链接降级为纯锚文本，跨域链接保留。

    使用场景：正文超出字符预算时的无损压缩。维基百科等站点的内链 URL
    是百分号编码（一个 CJK 字符展开为 9 个 ASCII 字符），单条链接即可
    占用上百字符，数百条内链常常吃掉 40% 以上的预算，导致靠后的表格
    被整块截断。内链的导航价值远低于其体积成本——降级只去掉 href，
    锚文本（含行内格式标签）原样保留，信息几乎无损。

    匹配范围限定为本转换器产出的规整形态 `<a href="...">…</a>`（无
    其他属性），跨域链接、页头来源链接（不在正文块中）均不受影响。
    """
    origin = (urlparse(base_url).netloc or "").lower()
    if not origin:
        return blocks

    def _demote(m: "re.Match") -> str:
        href = m.group(1)
        host = (urlparse(href).netloc or "").lower()
        # netloc 不受 HTML 实体转义影响（&amp; 只出现在 query 里）。
        if host == origin:
            return m.group(2)
        return m.group(0)

    return [_SAME_ORIGIN_LINK_RE.sub(_demote, b) for b in blocks]


def build_model_facing_html(
    url: str,
    html_text: str,
    body_blocks: Optional[list[str]] = None,
    title: str = "",
    fallback_text: str = "",
) -> Optional[str]:
    """组装 fetch_url 返回给模型的 Telegram HTML（忠实于原页面文档顺序）。

    结构：
      <h3>标题</h3>
      <p>🔗 来源链接</p>
      正文块……（图片/视频/播放器/音频在它们的原始位置；轮播图为 slideshow）
    不存在任何"集中的媒体区"。

    参数：
      body_blocks: 已转换的正文块（None 时内部用 trafilatura 提取）。
      title: 页面标题（og:title 优先，调用方提取）。
      fallback_text: trafilatura 提取失败时的纯文本兜底。
    """
    if _lxml_html is None or not html_text:
        return None

    if body_blocks is None:
        body_blocks = extract_body_blocks(html_text, url)
    blocks = [b for b in body_blocks if b and b.strip()]
    if not blocks and fallback_text:
        blocks = _fallback_paragraph_blocks(fallback_text)

    domain = urlparse(url).netloc or url
    header_parts: list[str] = []
    clean_title = esc((title or "").strip()[:200])
    if clean_title:
        header_parts.append(f"<h3>{clean_title}</h3>")
    header_parts.append(f'<p>🔗 <a href="{esc_attr(url)}">{esc(domain)}</a></p>')

    # 首个正文标题与页面标题重复时去掉，避免连续两个相同标题。
    if blocks and clean_title:
        m = re.match(r"^<h([1-6])>(.*?)</h\1>$", blocks[0].strip(), re.DOTALL)
        if m and _normalize_heading_text(m.group(2)) == _normalize_heading_text(title):
            blocks = blocks[1:]

    entries: list[dict] = [{"html": b, "order": None, "path": None} for b in blocks]
    dropped: list[tuple[int, str]] = []

    tree = _parse_dom(html_text)
    if tree is not None:
        try:
            media = _collect_dom_media(tree, url)
            # 1) 锚定正文块位置，并按锚点恢复 DOM 文档顺序。
            entries = _anchor_entries(entries, tree, media)
            entries = _sort_entries_by_anchor(entries)
            # 2) 轮播处理。
            entries, dropped = _apply_carousels(entries, media)
        except Exception as e:
            logger.debug(f"[fetch_rich] 媒体原位插入失败（退化为纯正文块）: {e}")
            entries = [{"html": b, "order": None, "path": None} for b in blocks]
            dropped = []

    final_blocks = _interleave(entries, dropped)
    if not final_blocks:
        return None

    # 预算感知压缩：正文超出预算时，先把同源链接降级为纯文本（保留锚
    # 文本与行内格式，仅去掉冗长的 href）。维基百科类页面的内链 URL 是
    # 百分号编码，单条即可达百字符，常常吃掉 40% 以上的预算——降级后
    # 信息几乎无损，而靠后的表格（如各话列表）得以保留在预算内。
    if sum(len(b) + 1 for b in final_blocks) > FETCH_BODY_MAX_LEN:
        final_blocks = _demote_same_origin_links(final_blocks, url)

    kept_blocks, was_truncated = _truncate_blocks(final_blocks, FETCH_BODY_MAX_LEN)
    parts = header_parts + kept_blocks
    if was_truncated:
        parts.append("<p>…（正文过长，已截断）</p>")

    result = "\n".join(parts)
    if len(result) > FETCH_RICH_MAX_LEN:
        parts2, trunc2 = _truncate_blocks(parts, FETCH_RICH_MAX_LEN - 60)
        if trunc2:
            parts2.append("<p>…（内容过长，已截断）</p>")
        result = "\n".join(parts2)
    return result


def _apply_carousels(entries: list[dict], media: list[DomMedia]) -> tuple[list[dict], list[tuple[int, str]]]:
    """轮播归并 + 收集需要原位插入的 dropped 媒体。

    返回 (新 entries, dropped 媒体块列表[(order_idx, html)])。

    规则：
      - 同一轮播容器内 >=2 张图：
        * >=2 张已出现在正文块 → 连续图片块由 _group_carousel_runs 合并为
          slideshow（保持原位置）；未出现的图片跳过（同轮播不重复）。
        * <2 张出现在正文块（如全部懒加载被 trafilatura 丢弃）→ 在轮播首图
          位置插入完整 <tg-slideshow>，并从正文块中移除已计入的单图块。
      - dropped 媒体（trafilatura 丢弃的视频/音频/播放器/图片）限制在
        "内容容器"（锚点元素的公共 XPath 前缀）内，页面导航/页脚等区域
        的媒体不插入。
    """
    kept_urls: set[str] = set()
    for entry in entries:
        kept_urls.update(_block_media_srcs(entry["html"]))

    url_to_carousel: dict[str, str] = {}
    by_carousel: dict[str, list[DomMedia]] = {}
    for m in media:
        if m.kind == "image" and m.carousel:
            url_to_carousel.setdefault(m.url, m.carousel)
            by_carousel.setdefault(m.carousel, []).append(m)

    # ---- 轮播归并决策 ----
    extra_slideshows: list[tuple[int, str, str]] = []  # (order, path, html)
    remove_urls: set[str] = set()
    for carousel_path, imgs in by_carousel.items():
        if len(imgs) < 2:
            continue
        kept = [i for i in imgs if i.url in kept_urls]
        if len(kept) >= 2:
            # run-grouping 会合并连续图片块；轮播内未出现的图不再单独插入。
            for i in imgs:
                if i.url not in kept_urls:
                    i.skip = True
        else:
            # 全量 slideshow 插入轮播首图位置；已计入的单图块从正文移除。
            for i in imgs:
                i.skip = True
            first = min(imgs, key=lambda i: i.order_idx)
            ordered = sorted(imgs, key=lambda i: i.order_idx)
            slide = "<tg-slideshow>" + "".join(
                f'<img src="{esc_attr(i.url)}"/>' for i in ordered
            ) + "</tg-slideshow>"
            extra_slideshows.append((first.order_idx, first.path, slide))
            remove_urls.update(i.url for i in kept)

    if remove_urls:
        kept_entries: list[dict] = []
        for entry in entries:
            srcs = _block_media_srcs(entry["html"])
            if srcs and all(s in remove_urls for s in srcs):
                continue
            kept_entries.append(entry)
        entries = kept_entries

    # ---- 连续同轮播图片块 → slideshow ----
    entries = _group_carousel_runs(entries, url_to_carousel)

    # ---- dropped 媒体（原位插入；nav/footer/aside 样板区域内的不插入）----
    dropped: list[tuple[int, str]] = []
    for m in media:
        if m.skip or m.url in kept_urls or m.boilerplate:
            continue
        dropped.append((m.order_idx, _render_dom_media_block(m)))
    for order, _path, slide_html in extra_slideshows:
        dropped.append((order, slide_html))
    return entries, dropped



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
