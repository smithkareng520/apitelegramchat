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
from markdown_converter import render_telegram_fragment as convert_markdown_to_telegram_html

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
        f"<details open><summary>{convert_markdown_to_telegram_html(title)}</summary>"
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
        return convert_markdown_to_telegram_html(str(value))
    if isinstance(value, str):
        clean = _trim_ui_value(value)
        if _looks_like_http_url(value):
            return f'<a href="{value.strip()}">打开链接</a>'
        return convert_markdown_to_telegram_html(clean)
    if depth >= 2:
        return f"<code>{convert_markdown_to_telegram_html(_compact_json(value))}</code>"
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
                    f"<details><summary>{convert_markdown_to_telegram_html(_trim_ui_value(title, 80))}</summary>"
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
                f"<tr><td><b>{convert_markdown_to_telegram_html(_display_key(key))}</b></td>"
                f"<td>{_render_structured_value(item, depth=depth + 1)}</td></tr>"
            )
        if len(visible_items) > _UI_MAX_FIELDS:
            rows.append(f"<tr><td colspan=\"2\"><i>其余 {len(visible_items) - _UI_MAX_FIELDS} 个字段已折叠</i></td></tr>")
        return "<table bordered striped>" + "".join(rows) + "</table>"
    return convert_markdown_to_telegram_html(_trim_ui_value(value))


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


def _poi_biz_ext(poi: dict) -> dict:
    """Return the AMap ``biz_ext`` object when present."""
    value = poi.get("biz_ext")
    return value if isinstance(value, dict) else {}


def _poi_photo_url(poi: dict) -> str:
    """从 POI 的 photos 里取首张图的 URL（缺失/形状异常返回空串）。

    gaode_mcp 直连后 UI 拿到的是未清洗的完整载荷：模型视图裁剪掉
    photos（不帮助文本选点），但用户视图保留 —— POI 卡片顶部用它
    渲染首张实景图（见 ``_render_poi_photo``）。
    """
    photos = poi.get("photos")
    if not isinstance(photos, list):
        return ""
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        for key in ("url", "image", "imgurl", "src"):
            value = photo.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _poi_value(poi: dict, *keys: str) -> str:
    """Read a non-empty POI field from top level or known nested containers."""
    containers = [poi]
    biz_ext = _poi_biz_ext(poi)
    if biz_ext:
        containers.append(biz_ext)
    for key in keys:
        for container in containers:
            value = container.get(key)
            if value not in (None, "", [], {}):
                return _trim_ui_value(value, 180)
    return ""


def _poi_biz_ext_value(poi: dict, key: str) -> str:
    """Read one of AMap's business-detail fields from ``biz_ext`` or top-level."""
    return _poi_value(poi, key)


def _poi_category(poi: dict) -> str:
    """Collapse AMap's semicolon-separated type hierarchy to a concise label."""
    raw = _poi_value(poi, "type", "atag")
    if not raw:
        return ""
    parts = [part.strip() for part in re.split(r"[;；]", raw) if part.strip()]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    # Keep the broad class + most specific useful label; the middle hierarchy
    # is usually implementation detail and makes Telegram cards unnecessarily tall.
    if parts[-1] == parts[0]:
        return parts[0]
    return f"{parts[0]}/{parts[-1]}"


def _poi_tags(poi: dict) -> str:
    """Return concise feature tags without exposing raw category codes."""
    raw = _poi_value(poi, "tag")
    if not raw:
        return ""
    parts = [part.strip() for part in re.split(r"[,，;；]", raw) if part.strip()]
    unique: list[str] = []
    for part in parts:
        if part not in unique:
            unique.append(part)
    return "、".join(unique[:5])


def _poi_hours(poi: dict) -> str:
    """Read whichever business-hours field the upstream AMap/MCP shape provides."""
    for key in (
        "opentime2", "open_time", "opentime", "business_hours",
        "opening_hours", "hours",
    ):
        value = _poi_value(poi, key)
        if value:
            return value
    return ""


def _phone_link(phone: str) -> str:
    """Make one simple phone value clickable; keep compound values as text."""
    text = _trim_ui_value(phone, 80)
    candidates = [item.strip() for item in re.split(r"[;,；，/]+", text) if item.strip()]
    if len(candidates) != 1:
        return _escape_text(text)
    number = candidates[0]
    compact = re.sub(r"[^0-9+]", "", number)
    if not compact or len(re.sub(r"\D", "", compact)) < 5:
        return _escape_text(number)
    return f'<a href="tel:{html.escape(compact, quote=True)}">{_escape_text(number)}</a>'


def _escape_text(value: object) -> str:
    """Escape data as plain text; do not interpret POI names as Markdown."""
    return html.escape(str(value if value is not None else ""), quote=False)


def _render_poi_cards(payload: object) -> str | None:
    """Render AMap POIs as compact, information-dense Telegram HTML cards.

    gaode_mcp 直连后 UI 拿到未清洗的完整载荷：每张卡片顶部可渲染首张
    实景图（photos），其余字段聚焦帮人选点 / 联络的信息，
    原始坐标仍然不进卡片（用户可以在地图 App 里搜名字）。
    """
    pois = _find_poi_records(payload)
    if not pois:
        return None

    total = len(pois)
    visible = pois[:8]
    cards: list[str] = [f"<p><b>📍 找到 {total} 个地点</b></p>"]
    for index, poi in enumerate(visible, start=1):
        name = _poi_value(poi, "name", "title") or f"地点 {index}"
        category = _poi_category(poi)
        distance = _poi_value(poi, "distance")
        rating = _poi_biz_ext_value(poi, "rating")
        cost = _poi_biz_ext_value(poi, "cost")
        address = _poi_value(poi, "address", "formatted_address")
        tel = _poi_value(poi, "tel", "phone")
        hours = _poi_hours(poi)
        tags = _poi_tags(poi)
        business_area = _poi_value(poi, "business_area")
        website = _poi_value(poi, "website")

        meta: list[str] = []
        if category:
            meta.append(_escape_text(category))
        if distance:
            meta.append(f"距 {_escape_text(_format_distance(distance))}")
        if rating:
            meta.append(f"★ {_escape_text(rating)}")
        if cost:
            meta.append(f"人均 ¥{_escape_text(cost)}")

        lines = [f"<b>{index}. {_escape_text(name)}</b>"]
        if meta:
            lines.append(" · ".join(meta))
        if address:
            lines.append(f"📍 {_escape_text(address)}")
        if tel:
            lines.append(f"☎️ {_phone_link(tel)}")
        if hours:
            lines.append(f"🕒 {_escape_text(hours)}")
        if business_area:
            lines.append(f"商圈：{_escape_text(business_area)}")
        if tags:
            lines.append(f"特色：{_escape_text(tags)}")
        if website and _looks_like_http_url(website):
            safe_url = html.escape(website.strip(), quote=True)
            lines.append(f'🌐 <a href="{safe_url}">官网</a>')

        photo_html = _render_poi_photo(poi)
        if photo_html:
            cards.append(photo_html)
        # One compact block per POI; no raw coordinate line.
        cards.append(f"<p>{'<br/>'.join(lines)}</p>")

    if total > len(visible):
        cards.append(
            f"<p><i>其余 {total - len(visible)} 个地点未在卡片中展开；模型仍可读取完整结果并按条件筛选。</i></p>"
        )
    return "".join(cards)


def _render_poi_photo(poi: dict) -> str | None:
    """渲染 POI 首张实景图（仅 http/https URL；不合法或缺失时返回 None）。"""
    url = _poi_photo_url(poi)
    if not url or not _looks_like_http_url(url):
        return None
    safe_url = html.escape(url.strip(), quote=True)
    # img 独立成段并限制显示宽度：多张卡片叠加时不会把草稿撑爆。
    return f'<p><img src="{safe_url}" maxwidth="480" rounded/></p>'


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
    """地理编码结果卡片：地址→坐标，内容本就短小（区域+坐标+匹配级别），
    不需要逐条折叠，直接平铺展示即可一次看完。

    gaode_mcp 原生 maps_geo 响应里结果数组叫 ``geocodes``。
    """
    data = _dict(payload)
    records = _list_of_dicts(data.get("geocodes")) or _list_of_dicts(data.get("return"))
    if tool_name in {"maps_geo", "maps_regeocode"} and records:
        cards: list[str] = [f"<p><b>📍 已解析 {len(records)} 个位置</b></p>"]
        for index, record in enumerate(records[:5], start=1):
            location = _poi_value(record, "location")
            area = " · ".join(
                part for part in (_poi_value(record, "province", "provice"), _poi_value(record, "city"), _poi_value(record, "district")) if part
            )
            level = _poi_value(record, "level")
            lines = [f"<b>{index}.</b>" + (f" {convert_markdown_to_telegram_html(area)}" if area else "")]
            if location:
                lines.append(f"<code>{convert_markdown_to_telegram_html(location)}</code>")
            if level:
                lines.append(f"匹配级别 {convert_markdown_to_telegram_html(level)}")
            cards.append(f"<p>{'<br/>'.join(lines)}</p>")
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
    body = f"<p><b>路程</b> {convert_markdown_to_telegram_html(distance)}　<b>预计</b> {convert_markdown_to_telegram_html(duration)}</p>"
    if steps:
        items = "".join(f"<li>{convert_markdown_to_telegram_html(step)}</li>" for step in steps)
        total = len(_list_of_dicts(path.get("steps")))
        suffix = f"<p><i>其余 {total - len(steps)} 步已折叠</i></p>" if total > len(steps) else ""
        body += f"<details><summary>导航步骤（{total}）</summary><ol>{items}</ol>{suffix}</details>"
    return f"<details{' open' if open_first else ''}><summary>方案 {index} · {convert_markdown_to_telegram_html(distance)} · {convert_markdown_to_telegram_html(duration)}</summary>{body}</details>"


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
    body = f"<p><b>预计</b> {convert_markdown_to_telegram_html(duration)}　<b>步行</b> {convert_markdown_to_telegram_html(walking)}</p>"
    if bus_segments:
        body += "<p><b>乘车</b></p><ol>" + "".join(f"<li>{convert_markdown_to_telegram_html(item)}</li>" for item in bus_segments[:5]) + "</ol>"
        if len(bus_segments) > 5:
            body += f"<p><i>其余 {len(bus_segments) - 5} 段已折叠</i></p>"
    else:
        body += "<p><i>该方案以步行为主。</i></p>"
    return f"<details{' open' if index == 1 else ''}><summary>方案 {index} · {convert_markdown_to_telegram_html(duration)} · 步行 {convert_markdown_to_telegram_html(walking)}</summary>{body}</details>"


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
        title += f"<br/><code>{convert_markdown_to_telegram_html(origin)}</code> → <code>{convert_markdown_to_telegram_html(destination)}</code>"
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
            f"<tr><td>起点 {convert_markdown_to_telegram_html(origin_id)} → 终点 {convert_markdown_to_telegram_html(dest_id)}</td>"
            f"<td>{convert_markdown_to_telegram_html(_format_distance(record.get('distance')))}</td>"
            f"<td>{convert_markdown_to_telegram_html(_format_duration(record.get('duration')))}</td></tr>"
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
    if tool_name in {"maps_geo", "maps_regeocode"}:
        card = _render_map_location_card(payload, tool_name)
        if card:
            return card
    if tool_name == "maps_distance":
        card = _render_distance_card(payload)
        if card:
            return card
    if tool_name in {"maps_direction_bicycling", "maps_direction_walking", "maps_direction_driving", "maps_direction_transit_integrated"}:
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

    与 ``markdown_converter.convert_markdown_to_telegram_html`` 不同：那是
    markdown → HTML 转换器，会把 ``*``、``_``、``` ` ```、``[]()`` 等语法
    转成真实标签，且对完全不含 markdown 语法的文本直接短路、不转义任何
    字符。但工具结果是**程序的原始输出**，不是 markdown/HTML 片段——命令
    输出里字面量的 ``*`` ``_`` ``&amp;`` 等都不该被解析或放行。因此这里
    保留一份独立、无条件的逐字符转义，保证终端输出逐字节原样可见。
    """
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_code_text(text: str, *, language: str | None = None) -> str:
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
    class_attr = f' class="language-{html.escape(language, quote=True)}"' if language else ""
    return f"<pre><code{class_attr}>{body}</code></pre>"


def _render_editor_quote(label: str, value: str, truncator: Callable[[str], str] = _truncate_ui_lines, *, language: str | None = None) -> str:
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
    return f"<p><b>{convert_markdown_to_telegram_html(label)}</b></p>{_render_code_text(text, language=language)}"


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


def _parse_bash_envelope(result_str: str) -> tuple[str, str] | None:
    """解析 Bash 模型结果信封 → ``(cwd, output)``。"""
    lines = (result_str or "").splitlines()
    if not lines or not lines[0].startswith("Cwd: "):
        return None
    return lines[0].removeprefix("Cwd: "), "\n".join(lines[1:])

def _render_bash_result(result_str: str, fn_args: dict | None = None) -> str:
    """Render bash input/output as syntax-highlighted Bash code blocks."""
    command = ""
    if isinstance(fn_args, dict):
        raw_command = fn_args.get("command")
        if isinstance(raw_command, str):
            command = raw_command

    parsed = _parse_bash_envelope(result_str)
    output = parsed[1] if parsed is not None else result_str
    if command:
        return (
            _render_editor_quote("Input", command, language="bash")
            + _render_editor_quote("Output", output, truncator=_truncate_ui_lines_head_tail, language="bash")
        )
    return _render_editor_quote("Output", output, language="bash")


def _editor_result_summary(result_str: str) -> str:
    """Discard internal snapshot metadata; front-end Output shows the result text."""
    message, _marker, _snapshot = (result_str or "").partition("Latest file snapshot (tail 10):\n")
    return message.strip() or result_str or ""


def _render_editor_result(command: str, path: str, result_str: str, arguments: dict | None = None) -> str:
    """Render text-editor calls as explicit, quote-formatted Input and Output."""
    arguments = arguments or {}
    if command == "view":
        # text_editor 不声明 description（意图）参数：Input 直接展示实际
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


# Persistent runtime state
