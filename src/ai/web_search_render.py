"""web_search 工具结果的解析与 Telegram Rich HTML 渲染。

历史上这套逻辑长在 ``tool_executors.format_tool_result`` 内部，并且只
保留 title+link、丢掉 snippet。现在拆出来：

- 解析 ``execute_web_search`` 返回的多 section envelope（search / images
  / videos / lens）为结构化字典；
- 用与 fetch_url / wikipedia 一致的视觉语言（``<b>`` 标题、
  ``<code>`` 来源徽标、``<a>`` 链接）渲染每个 section；search 模式为
  紧凑列表：不渲染 section 头（引擎/条数已由折叠块摘要展示）与摘要
  snippet，只保留标题链接 + 域名/时间/评分徽标；images / videos /
  lens 仍带各自头行；
- 失败 / 旧格式 / 空 envelope 各自兜底，保证总能拿到合法 HTML。

刻意避免引入 project 内部的重型模块（``api_client`` / ``subagent_tool``
等），便于在测试中独立验证。
"""
from __future__ import annotations

import re

from markdown_converter import render_telegram_fragment as convert_markdown_to_telegram_html


# ---------- 正则与常量 ----------
# execute_web_search 的 envelope 由 _format_search_results /
# _format_image_results / _format_video_results / _format_lens_results
# 拼接而成，每段以一个 emoji 头行起始：
#   🔍 [成功: Serper / Google] 搜索「query」的结果（N/M）：
#   🖼️ [成功: Serper Images] 搜图「query」的结果（N/M）：
#   🎬 [成功: Serper Videos] 搜视频「query」的结果（N/M）：
#   🔎 [成功: Serper Lens] 以图搜图「image_url」的结果（N/M）：
_WEB_SEARCH_SECTION_HEADER_RE = re.compile(
    r'(?m)^(🔍|🖼️|🎬|🔎)\s+\[成功:\s*([^\]]+)\]\s*(.*)$'
)
_WEB_SEARCH_ITEM_START_RE = re.compile(r'(?m)^(\d+)\.\s+')
# 字段 → 正则映射。命名约定（让 AI 不再混淆 URL 类型）：
#   链接  → 网页 URL（search 模式：结果本身就是页面）
#   页面  → 来源页面 URL（images/videos/lens：独立于媒体 URL）
#   图片  → 图片直链（images/lens）
#   封面  → 视频封面图直链（videos）
#   视频  → 视频媒体直链（videos，可塞进 <video src>）
#   时长  → 视频时长（videos，如 20:40）
#   频道  → 视频发布频道（videos）
#   时间  → 发布时间（search/videos）
#   评分  → 评分（search，如 4.3 ⭐ (30740 评价)）
_WEB_SEARCH_FIELD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("title",       re.compile(r'^标题：\s*(.*)$')),
    ("snippet",     re.compile(r'^摘要：\s*(.*)$')),
    ("link",        re.compile(r'^链接：\s*(.*)$')),
    ("page_link",   re.compile(r'^页面：\s*(.*)$')),
    ("image_url",   re.compile(r'^图片：\s*(.*)$')),
    ("cover",       re.compile(r'^封面：\s*(.*)$')),
    ("video_url",   re.compile(r'^视频：\s*(.*)$')),
    ("source",      re.compile(r'^来源：\s*(.*)$')),
    ("channel",     re.compile(r'^频道：\s*(.*)$')),
    ("duration",    re.compile(r'^时长：\s*(.*)$')),
    ("date",        re.compile(r'^时间：\s*(.*)$')),
    ("rating",      re.compile(r'^评分：\s*(.*)$')),
)
_WEB_SEARCH_MODE_BY_EMOJI = {
    "🔍": "search",
    "🖼️": "images",
    "🎬": "videos",
    "🔎": "lens",
}
_WEB_SEARCH_EMOJI_BY_MODE = {v: k for k, v in _WEB_SEARCH_MODE_BY_EMOJI.items()}


# ---------- 解析 ----------
def parse_web_search_sections(result_str: str) -> list[dict]:
    """Parse execute_web_search envelope into structured sections.

    Returns a list of dicts, each shaped as::

        {
            "mode": "search" | "images" | "videos" | "lens" | "error" | "text",
            "engine": str,         # e.g. "Serper / Google"
            "query": str,          # the query string (empty for lens)
            "success": int,        # number of successful results
            "requested": int,      # number requested
            "items": list[dict],   # parsed items
            "raw": str,            # raw section text (fallback)
        }
    """
    text = str(result_str or "").strip()
    if not text:
        return []

    if text.startswith(("❌", "失败")):
        return [{"mode": "error", "raw": text, "items": []}]

    header_matches = list(_WEB_SEARCH_SECTION_HEADER_RE.finditer(text))
    if not header_matches:
        return [{"mode": "text", "raw": text, "items": []}]

    sections: list[dict] = []
    for i, m in enumerate(header_matches):
        start = m.start()
        end = header_matches[i + 1].start() if i + 1 < len(header_matches) else len(text)
        section_text = text[start:end].strip()

        emoji = m.group(1)
        engine = (m.group(2) or "").strip()
        rest = (m.group(3) or "").strip()
        mode = _WEB_SEARCH_MODE_BY_EMOJI.get(emoji, "text")

        # 头行剩余部分形如：搜索「query」的结果（N/M）：
        query = ""
        qm = re.search(r'[「『\"](.+?)[」』\"]', rest)
        if qm:
            query = qm.group(1).strip()
        success = 0
        requested = 0
        cm = re.search(r'[（(]\s*(\d+)\s*/\s*(\d+)\s*[）)]', rest)
        if cm:
            success = int(cm.group(1))
            requested = int(cm.group(2))

        if "\n" in section_text:
            body = section_text.split("\n", 1)[1]
        else:
            body = ""

        item_starts = list(_WEB_SEARCH_ITEM_START_RE.finditer(body))
        items: list[dict] = []
        for j, im in enumerate(item_starts):
            its = im.start()
            ite = item_starts[j + 1].start() if j + 1 < len(item_starts) else len(body)
            chunk = body[its:ite].strip()
            # 第一行形如 "1. 标题：xxx"，去掉 "N. " 前缀，使 field 正则
            # （'^标题：' 等）能命中。后续行已无该前缀，不受影响。
            chunk = re.sub(r'^\d+\.\s+', '', chunk, count=1)
            entry: dict[str, str] = {}
            for line in chunk.split("\n"):
                line = line.strip()
                if not line:
                    continue
                for key, pat in _WEB_SEARCH_FIELD_PATTERNS:
                    fm = pat.match(line)
                    if fm:
                        entry[key] = fm.group(1).strip()
                        break
            if entry:
                items.append(entry)

        sections.append({
            "mode": mode,
            "engine": engine,
            "query": query,
            "success": success,
            "requested": requested,
            "items": items,
            "raw": section_text,
        })
    return sections


# ---------- 渲染辅助 ----------
def _domain_of(url: str) -> str:
    """Extract host (without scheme/path) for use as a source badge."""
    if not url:
        return ""
    m = re.match(r'https?://([^/\s]+)', url)
    if m:
        return m.group(1)
    return url


def _href(url: str) -> str:
    """Escape a URL for safe embedding into an href attribute.

    KNOWN RISK: this now routes through
    ``convert_markdown_to_telegram_html`` (the project's HTML-escaping
    utility ``escape_html`` was removed and all call sites were switched
    to the markdown converter). That converter is designed to parse
    markdown syntax and to preserve pre-existing HTML-looking tags, not
    to safely escape an arbitrary attribute value. A URL containing
    ``*``, ``_``, `` ` ``, or a ``[text](url)``-shaped fragment can be
    rewritten into real ``<i>``/``<b>``/``<a>`` tags, corrupting the
    ``href="..."`` attribute (unescaped ``<`` / stray ``"``) and
    potentially causing Telegram to reject the whole message. If URLs
    start breaking or messages start failing to send, this function is
    the first place to check — restoring a plain, non-markdown escaper
    here for URLs specifically would fix it.
    """
    if not url:
        return ""
    safe = convert_markdown_to_telegram_html(url)
    if '"' in safe:
        safe = safe.replace('"', '&quot;')
    return safe


def _section_header(section: dict) -> str:
    """Render the per-section header: emoji + query + count badge."""
    mode = section.get("mode", "text")
    query = section.get("query", "") or ""
    engine = section.get("engine", "") or ""
    success = section.get("success", 0) or 0
    requested = section.get("requested", 0) or 0
    emoji = _WEB_SEARCH_EMOJI_BY_MODE.get(mode, "🔍")

    parts: list[str] = [f"<b>{emoji} "]
    if mode == "lens":
        parts.append("以图搜图</b>")
    elif query:
        parts.append(f"「{convert_markdown_to_telegram_html(query)}」</b>")
    else:
        parts.append("搜索结果</b>")

    meta: list[str] = []
    if engine:
        meta.append(convert_markdown_to_telegram_html(engine))
    if success or requested:
        meta.append(f"{success}/{requested} 条")
    if meta:
        parts.append(f" <code>{' · '.join(meta)}</code>")
    return "".join(parts)


# ---------- 各 mode 渲染 ----------
def _render_search_items(items: list[dict]) -> str:
    """search 模式：紧凑 ol 列表，仅保留标题链接 + 域名/时间/评分徽标。

    刻意不渲染 snippet（摘要）：十条结果的摘要累计起来会把整张工具卡片
    撑得过长，且模型上下文里已保留完整摘要供回答使用，前端只需给出
    可点击的标题与来源即可。
    """
    parts: list[str] = ["<ol>"]
    for it in items:
        title = convert_markdown_to_telegram_html(it.get("title") or "无标题")
        link = it.get("link") or ""
        domain = _domain_of(link)
        date = it.get("date") or ""
        rating = it.get("rating") or ""
        # rating 行原始文本形如 `4.3 ⭐ (30740 评价)`，直接转义即可保留装饰
        rating_disp = convert_markdown_to_telegram_html(rating) if rating else ""
        card = "<li>"
        if link:
            card += f'<b><a href="{_href(link)}">{title}</a></b>'
        else:
            card += f"<b>{title}</b>"
        meta_bits: list[str] = []
        if domain:
            meta_bits.append(convert_markdown_to_telegram_html(domain))
        if date:
            meta_bits.append(convert_markdown_to_telegram_html(date))
        if rating_disp:
            meta_bits.append(rating_disp)
        if meta_bits:
            card += f" <code>{' · '.join(meta_bits)}</code>"
        card += "</li>"
        parts.append(card)
    parts.append("</ol>")
    return "".join(parts)


def _render_images_items(items: list[dict]) -> str:
    parts: list[str] = [
        '<table bordered striped cellpadding="3">',
        "<tr><th>#</th><th>标题</th><th>来源</th><th>图片</th></tr>",
    ]
    for idx, it in enumerate(items, 1):
        title = convert_markdown_to_telegram_html(it.get("title") or "无标题")
        source = convert_markdown_to_telegram_html(it.get("source") or "")
        img = it.get("image_url") or ""
        page = it.get("link") or it.get("page_link") or ""
        row = "<tr>"
        row += f"<td>{idx}</td>"
        if page:
            row += f'<td><a href="{_href(page)}">{title}</a></td>'
        else:
            row += f"<td>{title}</td>"
        row += f"<td><code>{source}</code></td>" if source else "<td>—</td>"
        if img:
            row += f'<td><a href="{_href(img)}">🖼️ 查看</a></td>'
        else:
            row += "<td>—</td>"
        row += "</tr>"
        parts.append(row)
    parts.append("</table>")
    return "".join(parts)


def _render_videos_items(items: list[dict]) -> str:
    """videos 模式：ol 卡片。

    展示元素：标题(链接到观看页) + 来源/频道/时长/时间/域名徽标 + 斜体摘要
              + 🎬 封面链接 + ▶️ 视频媒体链接。
    ▶️ 视频链接只在 video_url 字段非空时出现，且是唯一可安全嵌入
    <video src> 的 URL——明确与观看页 link 区分，避免 AI 误用。
    """
    parts: list[str] = ["<ol>"]
    for it in items:
        title = convert_markdown_to_telegram_html(it.get("title") or "无标题")
        # 新格式用 page_link；旧日志/缓存可能仍用 link，向后兼容
        page = it.get("page_link") or it.get("link") or ""
        snippet = convert_markdown_to_telegram_html(it.get("snippet") or "")
        source = convert_markdown_to_telegram_html(it.get("source") or "")
        channel = convert_markdown_to_telegram_html(it.get("channel") or "")
        duration = convert_markdown_to_telegram_html(it.get("duration") or "")
        date = convert_markdown_to_telegram_html(it.get("date") or "")
        cover = it.get("cover") or it.get("image_url") or ""
        video_url = it.get("video_url") or ""
        domain = _domain_of(page)
        card = "<li>"
        if page:
            card += f'<b><a href="{_href(page)}">{title}</a></b>'
        else:
            card += f"<b>{title}</b>"
        # 徽标顺序：时长 > 来源 > 频道 > 时间 > 域名
        meta_bits: list[str] = []
        if duration:
            meta_bits.append(duration)
        if source:
            meta_bits.append(source)
        if channel:
            meta_bits.append(channel)
        if date:
            meta_bits.append(date)
        if domain:
            meta_bits.append(convert_markdown_to_telegram_html(domain))
        if meta_bits:
            card += f" <code>{' · '.join(meta_bits)}</code>"
        card += "<br/>"
        if snippet:
            card += f"<i>{snippet}</i>"
        # 行尾链接组：封面 + 视频媒体。空隙用 · 分隔。
        links: list[str] = []
        if cover:
            links.append(f'<a href="{_href(cover)}">🎬 封面</a>')
        if video_url:
            links.append(f'<a href="{_href(video_url)}">▶️ 视频</a>')
        if links:
            if snippet:
                card += "<br/>"
            card += " · ".join(links)
        card += "</li>"
        parts.append(card)
    parts.append("</ol>")
    return "".join(parts)


def _render_lens_items(items: list[dict]) -> str:
    parts: list[str] = ["<ol>"]
    for it in items:
        title = convert_markdown_to_telegram_html(it.get("title") or "无标题")
        page = it.get("page_link") or it.get("link") or ""
        source = convert_markdown_to_telegram_html(it.get("source") or "")
        img = it.get("image_url") or ""
        domain = _domain_of(page)
        card = "<li>"
        if page:
            card += f'<b><a href="{_href(page)}">{title}</a></b>'
        else:
            card += f"<b>{title}</b>"
        # 与 search/videos 渲染器一致：逐项转义后直接拼接，
        # 不再对 join 结果整体二次转义（source 已在上方转义过）。
        meta_bits: list[str] = []
        if source:
            meta_bits.append(source)
        if domain:
            meta_bits.append(convert_markdown_to_telegram_html(domain))
        if meta_bits:
            card += f" <code>{' · '.join(meta_bits)}</code>"
        card += "<br/>"
        if img:
            card += f'<a href="{_href(img)}">🖼️ 查看图片</a>'
        card += "</li>"
        parts.append(card)
    parts.append("</ol>")
    return "".join(parts)


_MODE_RENDERERS = {
    "search": _render_search_items,
    "images": _render_images_items,
    "videos": _render_videos_items,
    "lens": _render_lens_items,
}


def render_web_search_section(section: dict) -> str:
    """Render one parsed section into Telegram Rich HTML.

    search 模式刻意不渲染 section 头（🔍 「query」 引擎 · N/M 条）：
    折叠块摘要行已展示「搜索词 N results」，再渲染一遍只会把结果列表
    越撑越长；images / videos / lens 仍保留各自头行以区分列表来源。
    """
    mode = section.get("mode", "text")

    if mode == "error":
        raw = section.get("raw", "")
        snippet = convert_markdown_to_telegram_html(raw[:1000])
        return f"<b>❌ 搜索失败</b><br/><i>{snippet}</i>"

    if mode == "text":
        return convert_markdown_to_telegram_html(section.get("raw", "")[:60000])

    items = section.get("items", [])
    if not items:
        # search 模式无头行可省，「无结果」占位保留；其余模式带头行。
        if mode == "search":
            return "<i>无结果</i>"
        return f"{_section_header(section)}<br/><i>无结果</i>"

    if mode == "search":
        return _render_search_items(items)

    header = _section_header(section)
    renderer = _MODE_RENDERERS.get(mode)
    if renderer is None:
        return header + "<br/>" + convert_markdown_to_telegram_html(section.get("raw", "")[:2000])

    return header + "<br/>" + renderer(items)


def format_web_search_result(fn_args: dict, result_str: str) -> tuple[str, str]:
    """Format the web_search tool result for the Telegram rich draft.

    Replaces the legacy renderer that only kept title + link and dropped
    the snippet. Now:

    - parses the multi-mode envelope into structured sections,
    - renders each mode (search / images / videos / lens) with a
      mode-appropriate layout that mirrors the visual language used by
      fetch_url / wikipedia,
    - falls back to escaped raw text when the envelope cannot be parsed.
    """
    query = (fn_args or {}).get("query", "") or ""
    text = str(result_str or "")

    # ---- summary（供工具折叠块摘要行使用）----
    count_match = re.search(
        r'\[成功:[^\]]+\].*?[（(]\s*(\d+)\s*/\s*(\d+)\s*[）)]', text, re.S
    )
    if count_match:
        num_results = int(count_match.group(1))
    else:
        parsed = parse_web_search_sections(text)
        num_results = sum(len(s.get("items", [])) for s in parsed) if parsed else 0

    if text.lstrip().startswith("❌"):
        summary = "Search failed"
    elif query and num_results == 1:
        summary = f"{query} 1 result"
    elif query:
        summary = f"{query} {num_results} results"
    else:
        summary = "Searched the web"

    # ---- details_html ----
    sections = parse_web_search_sections(text)
    if not sections:
        return summary, convert_markdown_to_telegram_html(text[:60000])

    if len(sections) == 1:
        return summary, render_web_search_section(sections[0])

    # 多 section（多 mode 并发）：每个 section 渲染后用 <br/><br/> 隔开。
    # 不再嵌套 <details>——工具卡片本身已经在 <details><summary> 里了，
    # 再套一层折叠会让用户多一次展开才能看到 images/videos 结果。
    rendered = [
        render_web_search_section(s)
        for s in sections
        if s.get("mode") != "text"
    ]
    text_fallback = next((s for s in sections if s.get("mode") == "text"), None)
    if not rendered and text_fallback:
        return summary, convert_markdown_to_telegram_html(text_fallback.get("raw", "")[:60000])
    return summary, "<br/><br/>".join(rendered)


__all__ = [
    "parse_web_search_sections",
    "render_web_search_section",
    "format_web_search_result",
]
