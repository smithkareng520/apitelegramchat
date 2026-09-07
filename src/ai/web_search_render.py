"""web_search 工具结果的解析与 Telegram Rich HTML 渲染。

历史上这套逻辑长在 ``tool_executors.format_tool_result`` 内部，并且只
保留 title+link、丢掉 snippet。现在拆出来：

- 解析 ``execute_web_search`` 返回的多 section envelope（search / images
  / videos / lens）为结构化字典（用于摘要行统计结果数）；
- 卡片正文与 bash / text_editor 保持同一视觉语言：``Input``（搜索词等
  实际输入）+ ``Output``（原始返回信封）两个 ``<pre><code>`` 等宽代码
  面板，不再把返回内容当作富文本直接铺开展示——搜索结果的原始信封是
  程序输出而非排版正文，等宽面板既不会丢字段也不会被超长内容撑爆；
- 失败 / 旧格式 / 空 envelope 各自兜底，保证总能拿到合法 HTML。

刻意避免引入 project 内部的重型模块（``api_client`` / ``subagent_tool``
等），仅复用轻量的 ``tool_ui_render`` 渲染原语（与 bash / text_editor
同一套），便于在测试中独立验证。
"""
from __future__ import annotations

import re

from tool_ui_render import _render_editor_quote, _truncate_ui_lines_head_tail


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


# ---------- 渲染（与 bash / text_editor 同一规范） ----------
def _format_search_input(fn_args: dict) -> str:
    """把工具调用的实际输入拼成 Input 面板文本。

    与 bash（Input=命令原文）、text_editor（Input=路径/编辑内容）对齐，
    web_search 的 Input 展示模型真正传入的检索参数：query / image_url，
    以及非默认的 mode。字段全部缺失时返回空串（调用方据此省略 Input
    面板，避免出现空引用块）。
    """
    args = fn_args or {}
    lines: list[str] = []
    query = str(args.get("query") or "").strip()
    if query:
        lines.append(f"query: {query}")
    image_url = str(args.get("image_url") or "").strip()
    if image_url:
        lines.append(f"image_url: {image_url}")
    mode = args.get("mode")
    if isinstance(mode, (list, tuple)):
        mode_text = ", ".join(str(m).strip() for m in mode if str(m).strip())
    else:
        mode_text = str(mode or "").strip()
    if mode_text and mode_text != "search":
        lines.append(f"mode: {mode_text}")
    return "\n".join(lines)


def format_web_search_result(fn_args: dict, result_str: str) -> tuple[str, str]:
    """Format the web_search tool result for the Telegram rich draft.

    展示规范与 bash / text_editor 一致：

    - ``Input`` 面板：本次搜索的实际输入（query / image_url / mode）；
    - ``Output`` 面板：execute_web_search 的原始返回信封，保头保尾
      截断（报错与统计信息几乎总在信封头部或末尾）；
    - 摘要行仍保留 ``query N results`` / ``Search failed`` 的旧口径，
      供工具折叠块标题与工具组统计使用。
    """
    args = fn_args or {}
    query = str(args.get("query") or "").strip()
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

    # ---- details_html：Input + Output 等宽代码面板 ----
    input_text = _format_search_input(args)
    details_html = ""
    if input_text:
        details_html += _render_editor_quote("Input", input_text)
    details_html += _render_editor_quote(
        "Output", text, truncator=_truncate_ui_lines_head_tail
    )
    return summary, details_html


__all__ = [
    "parse_web_search_sections",
    "format_web_search_result",
]
