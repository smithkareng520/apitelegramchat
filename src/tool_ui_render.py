"""工具结果卡片 UI 渲染工具箱（自 tool_executors.py 拆出）。

结构化 JSON → POI 卡 / 地图卡 / 路线卡 / 距离卡；bash 结果信封
解析与终端回放渲染；编辑器结果引用块；行宽/行数裁剪与转义。
全部为纯函数，供 tool_result_format 与 bash_session 复用。
"""

import os
import re
import json
import html
from typing import Callable
from urllib.parse import urlparse

from token_budget import truncate_to_token_budget
from utils import escape_html

import logging

logger = logging.getLogger(__name__)


_ANSI_ESCAPE_RE = re.compile(
    r'\x1B(?:'
    r'\][^\x07\x1b]*(?:\x07|\x1b\\)|'  # OSC (Operating System Command)
    r'\[[0-?]*[ -/]*[@-~]|'            # CSI (Control Sequence Introducer)
    r'[@-Z\\-_]'                       # Other 7-bit C1 sequences
    r')'
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (colors, cursor navigation, hyperlinks, OSC titles)."""
    if not text:
        return ""
    return _ANSI_ESCAPE_RE.sub('', text)


_UI_TAIL_LINES = 10
_UI_VALUE_TOKEN_BUDGET = 120
_UI_MAX_FIELDS = 10
_SENSITIVE_RESULT_KEYS = {
    "authorization", "token", "access_token", "api_key", "apikey", "secret",
    "password", "cookie", "signature", "x-amz-signature",
}


def _tail_text_lines(text: str, count: int = _UI_TAIL_LINES) -> tuple[list[str], int, int]:
    """Return the visible tail together with its one-based first line and total lines."""
    lines = (text or "").rstrip("\n").splitlines()
    total = len(lines)
    if total <= count:
        return lines, 1, total
    return lines[-count:], total - count + 1, total


def _numbered_text(text: str, *, max_lines: int = _UI_TAIL_LINES) -> str:
    lines, first_line, total = _tail_text_lines(text, max_lines)
    if not lines:
        return "(无输出)"
    # 单行宽度裁剪：行号 gutter 之外，超宽行同样会把预览面板/整条草稿撑爆。
    # 裁剪放在编号之前，保证“行号 │ 行首…行尾”结构完整。
    lines = [_clip_ui_line(line) for line in lines]
    width = len(str(max(total, first_line + len(lines) - 1)))
    prefix = "…\n" if first_line > 1 else ""
    body = "\n".join(
        f"{line_no:>{width}} │ {line}" for line_no, line in enumerate(lines, start=first_line)
    )
    return prefix + body


def _render_code_panel(
    title: str,
    text: str,
    *,
    max_lines: int = _UI_TAIL_LINES,
    add_line_numbers: bool = True,
) -> str:
    if add_line_numbers:
        display = _numbered_text(text, max_lines=max_lines)
    else:
        lines, _, _ = _tail_text_lines(text, max_lines)
        display = "\n".join(_clip_ui_line(line) for line in lines) if lines else "(无输出)"
    # 不带 style 属性：Telegram Rich Message 只认标签语义，内联 CSS
    # （background/font-family/white-space…）会被整体丢弃，留着只是噪音。
    # 等宽与空白保留由 <pre> 标签本身保证。转义用严格策略（& 无条件转义），
    # 因为这里承载的是程序原始输出而非 HTML 片段。
    return (
        f"<details open><summary>{escape_html(title)}</summary>"
        f"<pre><code>{_escape_code_text(display)}</code></pre></details>"
    )


def _trim_ui_value(value: object, token_budget: int = _UI_VALUE_TOKEN_BUDGET) -> str:
    text = str(value if value is not None else "")
    text = re.sub(r"\s+", " ", text).strip()
    return truncate_to_token_budget(text, token_budget, suffix="…")


def _looks_like_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _display_key(key: object) -> str:
    raw = str(key)
    labels = {
        "name": "名称", "title": "标题", "address": "地址", "location": "坐标",
        "city": "城市", "district": "区域", "type": "类型", "distance": "距离",
        "duration": "预计时长", "price": "价格", "tel": "电话", "website": "网站",
        "status": "状态", "message": "说明", "count": "数量", "total": "总数",
        "id": "ID", "url": "链接", "url_name": "链接名称", "formatted_address": "标准地址",
    }
    return labels.get(raw.lower(), raw.replace("_", " "))


def _compact_json(value: object, token_budget: int = _UI_VALUE_TOKEN_BUDGET) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = str(value)
    return _trim_ui_value(encoded, token_budget)


def _render_structured_value(value: object, *, depth: int = 0) -> str:
    if value is None:
        return "<i>—</i>"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return escape_html(str(value))
    if isinstance(value, str):
        clean = _trim_ui_value(value)
        if _looks_like_http_url(value):
            return f'<a href="{value.strip()}">打开链接</a>'
        return escape_html(clean)
    if depth >= 2:
        return f"<code>{escape_html(_compact_json(value))}</code>"
    if isinstance(value, list):
        if not value:
            return "<i>无</i>"
        if all(not isinstance(item, (dict, list)) for item in value):
            items = "".join(f"<li>{_render_structured_value(item, depth=depth + 1)}</li>" for item in value[:8])
            suffix = f"<li>…另有 {len(value) - 8} 项</li>" if len(value) > 8 else ""
            return f"<ul>{items}{suffix}</ul>"
        cards = []
        for index, item in enumerate(value[:6], start=1):
            if isinstance(item, dict):
                title = next(
                    (item.get(k) for k in ("name", "title", "address", "id") if item.get(k) not in (None, "")),
                    f"项目 {index}",
                )
                cards.append(
                    f"<details><summary>{escape_html(_trim_ui_value(title, 80))}</summary>"
                    f"{_render_structured_value(item, depth=depth + 1)}</details>"
                )
            else:
                cards.append(f"<p>{_render_structured_value(item, depth=depth + 1)}</p>")
        if len(value) > 6:
            cards.append(f"<p><i>其余 {len(value) - 6} 项已折叠</i></p>")
        return "".join(cards)
    if isinstance(value, dict):
        rows = []
        visible_items = [
            (key, item) for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_RESULT_KEYS
        ]
        for key, item in visible_items[:_UI_MAX_FIELDS]:
            rows.append(
                f"<tr><td><b>{escape_html(_display_key(key))}</b></td>"
                f"<td>{_render_structured_value(item, depth=depth + 1)}</td></tr>"
            )
        if len(visible_items) > _UI_MAX_FIELDS:
            rows.append(f"<tr><td colspan=\"2\"><i>其余 {len(visible_items) - _UI_MAX_FIELDS} 个字段已折叠</i></td></tr>")
        return "<table bordered striped>" + "".join(rows) + "</table>"
    return escape_html(_trim_ui_value(value))


def _parse_structured_payload(result_str: str) -> object | None:
    """Parse a JSON document *or* a stream of adjacent JSON objects from MCP text."""
    raw = (result_str or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    if not raw:
        return None

    # Some MCP adapters concatenate text blocks without delimiters, for example
    # ``{...POI 1...}{...POI 2...}``. JSONDecoder.raw_decode lets us retain every
    # object instead of falling back to an unreadable raw transcript.
    decoder = json.JSONDecoder()
    values: list[object] = []
    cursor = 0
    length = len(raw)
    while cursor < length:
        while cursor < length and raw[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break
        if raw[cursor] not in "[{":
            next_object = min(
                [index for index in (raw.find("{", cursor), raw.find("[", cursor)) if index >= 0],
                default=-1,
            )
            if next_object < 0:
                break
            cursor = next_object
        try:
            value, next_cursor = decoder.raw_decode(raw, cursor)
        except (json.JSONDecodeError, TypeError):
            break
        if isinstance(value, (dict, list)):
            values.append(value)
        cursor = next_cursor

    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _find_poi_records(payload: object) -> list[dict] | None:
    """Find AMap-like POI records in direct, wrapped, or concatenated MCP output."""
    if isinstance(payload, dict):
        for key in ("pois", "poi", "results", "items"):
            candidate = payload.get(key)
            if (
                isinstance(candidate, list)
                and candidate
                and all(isinstance(item, dict) for item in candidate)
                and any("name" in item or "address" in item for item in candidate)
            ):
                return candidate
        for key in ("data", "result", "payload"):
            nested = payload.get(key)
            found = _find_poi_records(nested)
            if found:
                return found
        if "name" in payload and ("address" in payload or "typecode" in payload or "location" in payload):
            return [payload]
    elif isinstance(payload, list):
        direct_records = [item for item in payload if isinstance(item, dict)]
        if direct_records and any("name" in item or "address" in item for item in direct_records):
            return direct_records
        for item in direct_records:
            found = _find_poi_records(item)
            if found:
                return found
    return None


def _poi_photo_url(poi: dict) -> str:
    photos = poi.get("photos")
    if isinstance(photos, dict):
        candidate = photos.get("url") or photos.get("photo_url")
        return str(candidate).strip() if _looks_like_http_url(candidate) else ""
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, dict):
                candidate = photo.get("url") or photo.get("photo_url")
                if _looks_like_http_url(candidate):
                    return str(candidate).strip()
    return ""


def _poi_value(poi: dict, *keys: str) -> str:
    for key in keys:
        value = poi.get(key)
        if value not in (None, "", [], {}):
            return _trim_ui_value(value, 180)
    return ""


def _render_poi_cards(payload: object) -> str | None:
    pois = _find_poi_records(payload)
    if not pois:
        return None

    total = len(pois)
    visible = pois[:8]
    cards: list[str] = [
        f"<p><b>📍 找到 {total} 个地点</b><br/><i>按名称、地址和图片整理；点击地点可展开详情。</i></p>"
    ]
    for index, poi in enumerate(visible, start=1):
        name = _poi_value(poi, "name", "title") or f"地点 {index}"
        address = _poi_value(poi, "address", "formatted_address")
        location = _poi_value(poi, "location")
        distance = _poi_value(poi, "distance")
        alias = _poi_value(poi, "alias")
        rating = _poi_value(poi, "rating")
        level = _poi_value(poi, "level")
        opening_hours = _poi_value(poi, "opentime2", "open_time")
        poi_type = _poi_value(poi, "type")
        typecode = _poi_value(poi, "typecode")
        poi_id = _poi_value(poi, "id", "poi_id")
        photo_url = _poi_photo_url(poi)
        summary = f"📍 {index}. {name}"
        details_open = " open" if index <= 2 else ""
        body: list[str] = []
        if photo_url and index <= 3:
            body.append(
                f'<figure><img src="{photo_url}"/>'
                f'<figcaption><a href="{photo_url}">查看地点图片</a></figcaption></figure>'
            )
        if address:
            body.append(f"<p><b>地址</b><br/>{escape_html(address)}</p>")
        if distance:
            body.append(f"<p><b>距离</b> {escape_html(distance)}</p>")
        if alias:
            body.append(f"<p><b>别名</b> {escape_html(alias)}</p>")
        if rating or level:
            quality = "　".join(part for part in (f"评分 {rating}" if rating else "", f"等级 {level}" if level else "") if part)
            body.append(f"<p><b>评价</b> {escape_html(quality)}</p>")
        if opening_hours:
            body.append(f"<details><summary>开放时间</summary><p>{escape_html(opening_hours)}</p></details>")
        if location:
            body.append(f"<p><b>坐标</b> <code>{escape_html(location)}</code></p>")
        metadata: list[str] = []
        if poi_type:
            metadata.append(f"分类：{escape_html(poi_type)}")
        if typecode:
            metadata.append(f"分类编码：<code>{escape_html(typecode)}</code>")
        if poi_id:
            metadata.append(f"POI ID：<code>{escape_html(poi_id)}</code>")
        if photo_url and index > 3:
            metadata.append(f'<a href="{photo_url}">查看地点图片</a>')
        if metadata:
            body.append("<details><summary>更多信息</summary><p>" + "<br/>".join(metadata) + "</p></details>")
        cards.append(f"<details{details_open}><summary>{escape_html(summary)}</summary>{''.join(body)}</details>")

    if total > len(visible):
        cards.append(f"<p><i>其余 {total - len(visible)} 个地点已省略，模型仍可读取完整结果并按你的需求继续筛选。</i></p>")
    return "".join(cards)


def _int_value(value: object) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _format_distance(value: object) -> str:
    meters = _int_value(value)
    if meters is None:
        return _trim_ui_value(value, 40) or "—"
    if meters >= 1000:
        return f"{meters / 1000:.1f} 公里"
    return f"{meters} 米"


def _format_duration(value: object) -> str:
    seconds = _int_value(value)
    if seconds is None:
        return _trim_ui_value(value, 40) or "—"
    minutes = max(1, round(seconds / 60))
    if minutes >= 60:
        return f"{minutes // 60} 小时 {minutes % 60} 分钟"
    return f"约 {minutes} 分钟"


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list_of_dicts(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _render_map_location_card(payload: object, tool_name: str) -> str | None:
    data = _dict(payload)
    records = _list_of_dicts(data.get("return"))
    if tool_name == "geocode" and records:
        cards: list[str] = [f"<p><b>📍 已解析 {len(records)} 个位置</b></p>"]
        for index, record in enumerate(records[:5], start=1):
            location = _poi_value(record, "location")
            area = " · ".join(
                part for part in (_poi_value(record, "province", "provice"), _poi_value(record, "city"), _poi_value(record, "district")) if part
            )
            level = _poi_value(record, "level")
            body = []
            if area:
                body.append(f"<p><b>区域</b><br/>{escape_html(area)}</p>")
            if location:
                body.append(f"<p><b>坐标</b><br/><code>{escape_html(location)}</code></p>")
            if level:
                body.append(f"<p><b>匹配级别</b> {escape_html(level)}</p>")
            cards.append(f"<details{' open' if index == 1 else ''}><summary>位置 {index}{' · ' + escape_html(location) if location else ''}</summary>{''.join(body) or '<p><i>上游未返回可展示字段。</i></p>'}</details>")
        return "".join(cards)

    return None


def _collect_route_steps(path: dict, limit: int = 8) -> list[str]:
    steps = _list_of_dicts(path.get("steps"))
    rendered = []
    for step in steps[:limit]:
        instruction = _poi_value(step, "instruction")
        if instruction:
            rendered.append(instruction)
    return rendered


def _render_route_path(path: dict, index: int, *, open_first: bool = False) -> str:
    distance = _format_distance(path.get("distance"))
    duration = _format_duration(path.get("duration"))
    steps = _collect_route_steps(path)
    body = f"<p><b>路程</b> {escape_html(distance)}　<b>预计</b> {escape_html(duration)}</p>"
    if steps:
        items = "".join(f"<li>{escape_html(step)}</li>" for step in steps)
        total = len(_list_of_dicts(path.get("steps")))
        suffix = f"<p><i>其余 {total - len(steps)} 步已折叠</i></p>" if total > len(steps) else ""
        body += f"<details><summary>导航步骤（{total}）</summary><ol>{items}</ol>{suffix}</details>"
    return f"<details{' open' if open_first else ''}><summary>方案 {index} · {escape_html(distance)} · {escape_html(duration)}</summary>{body}</details>"


def _render_transit_plan(transit: dict, index: int) -> str:
    duration = _format_duration(transit.get("duration"))
    walking = _format_distance(transit.get("walking_distance"))
    bus_segments: list[str] = []
    for segment in _list_of_dicts(transit.get("segments")):
        bus = _dict(segment.get("bus"))
        for line in _list_of_dicts(bus.get("buslines")):
            line_name = _poi_value(line, "name")
            departure = _poi_value(_dict(line.get("departure_stop")), "name")
            arrival = _poi_value(_dict(line.get("arrival_stop")), "name")
            if line_name:
                route = " → ".join(part for part in (departure, arrival) if part)
                bus_segments.append(f"{line_name}{'（' + route + '）' if route else ''}")
    body = f"<p><b>预计</b> {escape_html(duration)}　<b>步行</b> {escape_html(walking)}</p>"
    if bus_segments:
        body += "<p><b>乘车</b></p><ol>" + "".join(f"<li>{escape_html(item)}</li>" for item in bus_segments[:5]) + "</ol>"
        if len(bus_segments) > 5:
            body += f"<p><i>其余 {len(bus_segments) - 5} 段已折叠</i></p>"
    else:
        body += "<p><i>该方案以步行为主。</i></p>"
    return f"<details{' open' if index == 1 else ''}><summary>方案 {index} · {escape_html(duration)} · 步行 {escape_html(walking)}</summary>{body}</details>"


def _render_map_route_card(payload: object) -> str | None:
    data = _dict(payload)
    route = _dict(data.get("route"))
    if not route and isinstance(data.get("data"), dict):
        route = _dict(data.get("data"))
    if not route:
        return None
    origin = _poi_value(route, "origin")
    destination = _poi_value(route, "destination")
    title = "<p><b>🧭 路线规划</b>"
    if origin and destination:
        title += f"<br/><code>{escape_html(origin)}</code> → <code>{escape_html(destination)}</code>"
    title += "</p>"
    transits = _list_of_dicts(route.get("transits"))
    if transits:
        cards = "".join(_render_transit_plan(item, index) for index, item in enumerate(transits[:4], start=1))
        if len(transits) > 4:
            cards += f"<p><i>其余 {len(transits) - 4} 个公交方案已折叠</i></p>"
        return title + cards
    paths = _list_of_dicts(route.get("paths"))
    if paths:
        cards = "".join(_render_route_path(item, index, open_first=index == 1) for index, item in enumerate(paths[:3], start=1))
        if len(paths) > 3:
            cards += f"<p><i>其余 {len(paths) - 3} 个路线方案已折叠</i></p>"
        return title + cards
    return None


def _render_distance_card(payload: object) -> str | None:
    records = _list_of_dicts(_dict(payload).get("results"))
    if not records:
        return None
    rows = []
    for record in records[:12]:
        origin_id = _poi_value(record, "origin_id") or "—"
        dest_id = _poi_value(record, "dest_id") or "—"
        rows.append(
            f"<tr><td>起点 {escape_html(origin_id)} → 终点 {escape_html(dest_id)}</td>"
            f"<td>{escape_html(_format_distance(record.get('distance')))}</td>"
            f"<td>{escape_html(_format_duration(record.get('duration')))}</td></tr>"
        )
    suffix = f"<p><i>其余 {len(records) - 12} 条结果已折叠</i></p>" if len(records) > 12 else ""
    return (
        f"<p><b>📏 距离测量</b><br/><i>共 {len(records)} 条结果</i></p>"
        "<table bordered striped><tr><th>路线</th><th>距离</th><th>预计</th></tr>"
        + "".join(rows) + "</table>" + suffix
    )


def _render_map_payload(payload: object, tool_name: str) -> str | None:
    poi_cards = _render_poi_cards(payload)
    if poi_cards:
        return poi_cards
    if tool_name in {"geocode", "regeocode"}:
        card = _render_map_location_card(payload, tool_name)
        if card:
            return card
    if tool_name == "distance":
        card = _render_distance_card(payload)
        if card:
            return card
    if tool_name == "route":
        card = _render_map_route_card(payload)
        if card:
            return card
    return None


def _render_structured_payload(result_str: str, *, map_tool: str) -> str | None:
    payload = _parse_structured_payload(result_str)
    if payload is None:
        return None
    # _render_map_payload 内部已先尝试 POI 卡片，无需在此重算同一纯函数。
    map_card = _render_map_payload(payload, map_tool)
    if map_card:
        return map_card
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)) and len(payload) <= 3:
        payload = payload["data"]
    return (
        "<p><b>结构化结果</b><br/><i>已将服务返回转换为可阅读字段；详情可展开查看。</i></p>"
        + _render_structured_value(payload)
    )


# 所有工具的完成态展示统一走 text_editor 风格的 Input/Output 引用块；
# Input 或 Output 任一超过这个行数都做截断，避免长内容把消息撑爆。
_TOOL_UI_MAX_LINES = 20
# 单行宽度上限。只有行数预算是不够的：minified JS / 单行 JSON / base64 /
# `jq -c` 这类“无换行大文本”一行就能有几万字符，20 行预算形同虚设；
# 而超宽行进入 <pre><code> 后成为 Rich Message 里不可分割的单块
# （rollover 只能在块边界切分），最终把草稿撑到超过滚动预算，消息被
# 迫分裂成多条。因此在垂直（行数）之外再设水平（单行宽度）预算。
_TOOL_UI_MAX_LINE_CHARS = max(80, int(os.getenv("TOOL_UI_MAX_LINE_CHARS", "240")))
# <pre> 块的最终总量兑底（原始字符数，转义前）。正常路径远达不到：工具卡片
# 已被行数×行宽双重钳住；此值只拦截直接把大文本塞进 <pre> 的旁路调用。
_PRE_BLOCK_MAX_CHARS = max(_TOOL_UI_MAX_LINE_CHARS * 4, int(os.getenv("PRE_BLOCK_MAX_CHARS", "8000")))


def _clip_ui_line(line: str, max_chars: int | None = None) -> str:
    """超宽行保头保尾：关键信息可能在一行的任意位置。

    行内的报错片段（长 traceback 行末尾的异常消息、断言 diff 行的
    ``+ expected - actual``）经常位于行尾，纯头部截断同样会丢掉它；
    因此与 bash 输出同级地采取“头 2/3 + 尾 1/3”，中间以说明替代。
    """
    limit = _TOOL_UI_MAX_LINE_CHARS if max_chars is None else max_chars
    if len(line) <= limit:
        return line
    head_len = max(1, (limit * 2) // 3)
    tail_len = max(1, limit - head_len)
    omitted = len(line) - head_len - tail_len
    return f"{line[:head_len]}…（本行过长，省略 {omitted} 字符）…{line[-tail_len:]}"


def _clip_ui_lines(text: str) -> str:
    """对一段已定稿的多行文本逐行做宽度裁剪（行数不再变动）。"""
    if not text:
        return text
    if len(text) <= _TOOL_UI_MAX_LINE_CHARS:
        return text  # 快路径：整体都不超宽，无需逐行
    return "\n".join(_clip_ui_line(line) for line in text.splitlines())


def _truncate_ui_lines(text: str, max_lines: int = _TOOL_UI_MAX_LINES) -> str:
    """Keep only the first max_lines lines; append a truncation note if cut.

    行数截断后再对**保留的行**做单行宽度裁剪（先选窗口、后裁剪，被丢弃
    的行不做无用功）。行数 × 行宽给出硬上界：单个卡片最坏约
    ``max_lines × (_TOOL_UI_MAX_LINE_CHARS + 说明开销)`` 字符，不再可能
    出现“一行拖垮整条草稿”的情况。
    """
    text = text if isinstance(text, str) else str(text or "")
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return _clip_ui_lines(text)
    kept = "\n".join(_clip_ui_line(line) for line in lines[:max_lines])
    return f"{kept}\n…（已截断，共 {len(lines)} 行，仅显示前 {max_lines} 行）"


def _truncate_ui_lines_head_tail(text: str, max_lines: int = _TOOL_UI_MAX_LINES) -> str:
    """Keep the first ~60% and the last ~40% lines; note the omitted middle.

    Bash 输出的报错/摘要几乎总在结尾，纯头部截断会让用户在卡片里看不到
    失败原因；这里保头也保尾，中间以说明行代替。保留的行同样做单行宽度
    裁剪（与 :func:`_truncate_ui_lines` 相同的水平预算）。
    """
    text = text if isinstance(text, str) else str(text or "")
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return _clip_ui_lines(text)
    head_lines = max(1, int(max_lines * 0.6))
    tail_lines = max(1, max_lines - head_lines - 1)  # 预留 1 行给省略说明
    omitted = len(lines) - head_lines - tail_lines
    head = "\n".join(_clip_ui_line(line) for line in lines[:head_lines])
    tail = "\n".join(_clip_ui_line(line) for line in lines[-tail_lines:])
    return f"{head}\n…（已截断，共 {len(lines)} 行，省略中间 {omitted} 行）\n{tail}"


def _escape_code_text(text: str) -> str:
    """严格转义代码/终端文本中的 HTML 特殊字符（``&``、``<``、``>`` 一律转义）。

    与 ``utils.escape_html`` 的「智能 ampersand」策略不同：那里为了不破坏
    调用方自己拼的 ``&amp;``/``&#39;`` 实体，会跳过看起来像实体的 ``&``。
    但工具结果是**程序的原始输出**，不是 HTML 片段——命令若打印了字面量
    ``&amp;lt;`` 或 ``&amp;amp;``，智能策略会放行、Telegram 再解析回 ``<``/``&``，
    用户看到的就不是命令真实的输出。因此这里对 ``&`` 无条件转义，保证
    终端输出逐字节可见。
    """
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_code_text(text: str) -> str:
    """把纯文本渲染为**保留缩进与空白**的等宽代码块。

    为什么必须是 ``<pre><code>`` 而不是 ``<blockquote>``：
    Telegram Rich Message 的 ``blockquote`` 是 RichText 容器，按普通 HTML
    文本流排版——连续空格会被折叠成一个、行首缩进被吃掉，且使用比例字体
    （每个字符宽度不同）。因此即使把换行显式转成 ``<br/>``，代码的缩进层级
    和列对齐仍然全部丢失。``<pre>`` 是预格式化块：空白逐字保留、等宽字体
    渲染，是唯一能正确承载终端输出、diff、源码与行号的容器。

    ``<pre>`` 内不能再用 ``<br/>`` 换行——换行符本身即换行；插入 ``<br/>``
    反而会多出一个空行。

    总量兜底：``<pre>`` 是 Rich Message 里不可分割的块（rollover 只能在块
    边界切分），任何调用路径都不该往里塞超大文本。上层的
    ``_truncate_ui_lines*`` 已对工具卡片做了行数×行宽预算，但仍有旁路
    （如 weather 失败时 ``result_str[:60000]`` 直通此处），这里做最后一道
    防线。注意必须在**转义前**对原文裁剪：若对转义后的文本下刀，可能切在
    ``&amp;`` 实体中间，产生裸 ``&`` 打坏 Telegram 的 HTML 解析。
    """
    raw = text if isinstance(text, str) else str(text or "")
    if len(raw) > _PRE_BLOCK_MAX_CHARS:
        head_len = (_PRE_BLOCK_MAX_CHARS * 2) // 3
        tail_len = _PRE_BLOCK_MAX_CHARS - head_len
        omitted = len(raw) - head_len - tail_len
        raw = (
            f"{raw[:head_len]}\n…[内容过长，中间约 {omitted} 字符已省略]…\n"
            f"{raw[-tail_len:]}"
        )
    body = _escape_code_text(raw)
    return f"<pre><code>{body}</code></pre>"


def _render_editor_quote(label: str, value: str, truncator: Callable[[str], str] = _truncate_ui_lines) -> str:
    """Render a tool's input or output as a monospace code block that preserves indentation.

    截断到 ``_TOOL_UI_MAX_LINES`` 行后放进 ``<pre><code>``：文件摘录、终端
    回放、diff 与行号 gutter（``12 │ code``）都依赖等宽字体和逐字保留的
    空白才能对齐，旧实现用 ``<blockquote>`` + ``<br/>`` 会把缩进折叠掉。
    """
    text = value if isinstance(value, str) else str(value or "")
    if not text:
        text = "(empty)"
    else:
        text = truncator(text)
    return f"<p><b>{escape_html(label)}</b></p>{_render_code_text(text)}"


def _render_media_failure_result(result_str: str, fallback: str) -> str:
    """Render media-generation failures in the same quote format as text_editor.

    Some providers return HTML fragments in their error payloads. Convert those fragments
    to readable plain text before quoting so the user sees a clean, non-nested error block.
    """
    raw = result_str if isinstance(result_str, str) else str(result_str or "")
    raw = html.unescape(raw)
    # 修复 BUG：原先写的是 r"<br\\s*/?\\s*>" —— 在 raw-string 里 \\s 是字面量 "\s"
    # 而非正则的空白匹配，导致这个 <br> 替换实际上从未生效。
    # 改成 r"<br\s*/?\s*>" 后才会正确匹配 <br>、<br/>、<br />。
    raw = re.sub(r"<br\s*/?\s*>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", "", raw)
    message = html.unescape(raw).strip() or fallback
    return _render_editor_quote("Result", message)


def _format_image_generation_result(
    result_str: str,
    *,
    operation_en: str,
    operation_zh: str,
    failure_summary: str,
    failure_fallback: str,
) -> tuple[str, str]:
    """Render image generation and image editing results with one stable template."""
    if "✅" in result_str:
        lines = result_str.splitlines()
        urls = [line.strip() for line in lines if line.strip().startswith(("http://", "https://"))]
        if urls:
            count = len(urls)
            summary = f"🎨 {operation_en} {count} image" + ("" if count == 1 else "s")
            # R2 presigned URL 含 & 查询参数，HTML 属性值里必须转义，否则
            # Telegram 解析器可能把 &X-Amz-... 当实体名起点截断 URL（缺
            # 签名参数会被 R2 以 403 拒绝）。先 escape 再内插。
            img_tags = "".join(f'<img src="{html.escape(url, quote=True)}"/>' for url in urls)
            link_items = "".join(
                f'<li><a href="{html.escape(url, quote=True)}">图片 {index + 1}</a></li>'
                for index, url in enumerate(urls)
            )
            caption = f"{operation_zh} {count} 张图片：<ul>{link_items}</ul>"
            if count == 1:
                details_html = f"<figure>{img_tags}<figcaption>{caption}</figcaption></figure>"
            else:
                details_html = f"<tg-slideshow>{img_tags}<figcaption>{caption}</figcaption></tg-slideshow>"
            return summary, details_html
    return failure_summary, _render_media_failure_result(result_str, failure_fallback)


def _parse_bash_envelope(result_str: str) -> tuple[str, str, str] | None:
    """解析终端回放式信封 → ``(command, exit_code, output)``；非信封返回 None。

    新格式::

        /abs/cwd$ <command>          ← 提示符行，命令可跨多行
        Exit code: <code>
        <output>

    判定规则：
    - 首行以 ``Command: `` 开头的是旧式元数据信封（历史会话存量结果），
      走旧解析路径，这里直接返回 None；
    - 首行不含 ``$ ``、或后续找不到 ``Exit code: `` 行的（超时/拒绝等
      纯错误文本）同样返回 None，由调用方走兜底渲染。

    已知边界（仅影响 UI 草稿展示，不影响回传模型的文本）：命令本身
    含有以 ``Exit code: `` 开头的行时会被提前截断——与旧解析器对
    ``Cwd: `` 行的脆弱性同级，可接受。
    """
    lines = (result_str or "").splitlines()
    if not lines or lines[0].startswith("Command: ") or "$ " not in lines[0]:
        return None
    command = lines[0].split("$ ", 1)[1]
    exit_code = ""
    output_start = None
    for idx in range(1, len(lines)):
        line = lines[idx]
        if line.startswith("Exit code: "):
            exit_code = line.removeprefix("Exit code: ")
            output_start = idx + 1
            break
        command += "\n" + line
    if output_start is None:
        return None
    return command, exit_code, "\n".join(lines[output_start:])


def _extract_bash_command_from_envelope(result_str: str) -> str:
    """从终端回放式信封里恢复命令文本（兜底路径）。

    命令跟在首行提示符（``/abs/cwd$ ``）之后，可跨多行，直到
    ``Exit code: `` 行为止。旧式元数据信封（``Command: …\nCwd: …``）已无
    生产者且本项目不重放历史会话，相关兼容解析已移除。
    """
    parsed = _parse_bash_envelope(result_str)
    return parsed[0] if parsed else ""


def _render_bash_result(result_str: str, fn_args: dict | None = None) -> str:
    """Render bash calls the same way as text_editor: quote-formatted Input and Output.

    修复：Input 必须优先取工具调用参数里的原始 command。旧实现从结果信封
    逐行解析 ``Command: `` 前缀，多行命令（如 ``python3 -c "…"`` 跨行）只剩
    第一行 —— 用户在草稿富文本里看到 Input 显示成 ``python3 -c "`` 的截断
    假象就来自这里（并非被过滤，而是解析丢了后续行）。

    Input 块只展示实际输入（命令本身）；模型提供的意图描述（_description）
    由卡片摘要行（``🖥 意图``）单独呈现，不混入 Input，避免重复与语义混淆。

    信封格式现为终端回放式（``/abs/cwd$ cmd`` + ``Exit code:`` + 输出）；
    旧式元数据信封与无信封文本（restart 确认、拒绝原因等）仍可渲染。
    """
    command = ""
    if isinstance(fn_args, dict):
        raw_command = fn_args.get("command")
        if isinstance(raw_command, str):
            command = raw_command
    parsed = _parse_bash_envelope(result_str)
    if parsed:
        env_command, exit_code, output = parsed
        if not command:
            command = env_command
        output_text = output
        if exit_code:
            output_text = f"[exit code {exit_code}]\n{output}"
        # Input 展示原始命令；Output 保头保尾：报错信息几乎总在末尾。
        return (
            _render_editor_quote("Input", command)
            + _render_editor_quote("Output", output_text, truncator=_truncate_ui_lines_head_tail)
        )
    # 无信封文本（restart 确认、拒绝原因、超时等）：若有命令可展示则带 Input，
    # 否则只渲染 Output，避免出现空 Input 引用块。
    if not command:
        command = _extract_bash_command_from_envelope(result_str)
    if not command:
        return _render_editor_quote("Output", result_str)
    return _render_editor_quote("Input", command) + _render_editor_quote("Output", result_str)


def _editor_result_summary(result_str: str) -> str:
    """Discard internal snapshot metadata; front-end Output shows the result text."""
    message, _marker, _snapshot = (result_str or "").partition("Latest file snapshot (tail 10):\n")
    return message.strip() or result_str or ""


def _render_editor_result(command: str, path: str, result_str: str, arguments: dict | None = None) -> str:
    """Render text-editor calls as explicit, quote-formatted Input and Output."""
    arguments = arguments or {}
    if command == "view":
        # text_editor 不声明 _description（意图）参数：Input 直接展示实际
        # 输入（目标路径 + 可选行范围），与写操作展示真实参数的策略一致。
        view_input = str(path or "").strip()
        view_range = arguments.get("view_range")
        if isinstance(view_range, (list, tuple)) and len(view_range) == 2:
            view_input = f"{view_input} (lines {view_range[0]}-{view_range[1]})".strip()
        if not view_input:
            view_input = "Inspect the requested text file."
        return _render_editor_quote("Input", view_input) + _render_editor_quote("Output", result_str)

    if result_str.startswith("Error:"):
        return _render_editor_quote("Result", result_str)

    input_field = {
        "str_replace": "new_str",
        "create": "file_text",
        "insert": "insert_text",
    }.get(command)
    input_value = arguments.get(input_field, "") if input_field else ""
    output = _editor_result_summary(result_str)
    return _render_editor_quote("Input", input_value) + _render_editor_quote("Output", output)

def extract_domain(url: str) -> str:
    if not url:
        return "unknown"
    parsed = urlparse(url)
    return parsed.netloc or parsed.path.split('/')[0]

# =====================================================================
# Persistent runtime state
# =====================================================================
