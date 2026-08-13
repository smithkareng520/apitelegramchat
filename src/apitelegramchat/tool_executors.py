# tool_executors.py
import asyncio
import os
import subprocess
import random
import aiohttp
import json
import time
from pathlib import Path
from apitelegramchat.workspace_paths import (
    workspace_root, workspace_workdir, runtime_cache_root, workspace_namespace,
    workspace_upload_root,
    is_inside_upload_or_download,
)
import re
import html
import logging
from typing import Optional, List
from urllib.parse import urlparse
from apitelegramchat.workspace_utils import (
    _get_workspace_lock,
    _ensure_runtime_workspace,
    fetch_from_download,
    stage_to_upload,
    list_download_files,
    list_upload_files,
)

from apitelegramchat.sandbox import (
    build_sandbox_argv, build_sandbox_env,
    watchdog, _preexec_sandbox,
    SANDBOX_TIMEOUT_SEC,
)

from apitelegramchat.config import (
    MAX_CONCURRENT_TOOLS,
    BASE_URL,
)

_TOOL_TIMEOUT_MARKER = "__TOOL_TIMEOUT__"

import shutil
from apitelegramchat.search_engine import (
    execute_web_search,
    execute_fetch_url,
    execute_wikipedia,
    execute_exchange_rate,
    execute_book_lookup,
    execute_weather,
    execute_news,
    execute_crypto_price,
    execute_ip_geo,
    execute_qr_code,
    execute_done,
    execute_generate_image,
    execute_generate_video,
    # 地图工具（全部委托给 amap-maps MCP）
    execute_geocode,
    execute_keyword_search,
    execute_nearby_search,
    execute_poi_details,
    execute_route,
    execute_distance,
    execute_text_editor,
)
from apitelegramchat.todo_tool import (
    execute_todo,
    render_todo_card,
)
from apitelegramchat.memory_tool import execute_memory, render_memory_card
from apitelegramchat.subagent_tool import execute_subagent, render_subagent_card
from apitelegramchat.utils import escape_html

logger = logging.getLogger(__name__)

# ---------- 信号量控制并发工具调用 ----------
tool_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TOOLS)

MAX_TOOL_RESPONSE_LEN = 16000

def _truncate_tool_result(result: str) -> str:
    if len(result) > MAX_TOOL_RESPONSE_LEN:
        return result[:MAX_TOOL_RESPONSE_LEN] + "\n…[内容过长已截断]"
    return result


_UI_TAIL_LINES = 10
_UI_MAX_VALUE_CHARS = 360
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
        display = "\n".join(lines) if lines else "(无输出)"
    return (
        f"<details open><summary>{escape_html(title)}</summary>"
        "<pre style=\"margin:6px 0 0;padding:10px 12px;background:#111827;color:#e5e7eb;"
        "border-radius:8px;white-space:pre;overflow:auto;font-family:ui-monospace,SFMono-Regular,"
        "Menlo,Monaco,Consolas,monospace;font-size:12px;line-height:1.55;\"><code>"
        f"{escape_html(display)}</code></pre></details>"
    )


def _trim_ui_value(value: object, limit: int = _UI_MAX_VALUE_CHARS) -> str:
    text = str(value if value is not None else "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


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


def _compact_json(value: object, limit: int = _UI_MAX_VALUE_CHARS) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = str(value)
    return _trim_ui_value(encoded, limit)


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
            safe_url = escape_html(value.strip())
            return f'<a href="{safe_url}">打开链接</a>'
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
            if isinstance(candidate, list) and candidate and all(isinstance(item, dict) for item in candidate):
                if any("name" in item or "address" in item for item in candidate):
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
            safe_photo = escape_html(photo_url)
            body.append(
                f'<figure><img src="{safe_photo}"/>'
                f'<figcaption><a href="{safe_photo}">查看地点图片</a></figcaption></figure>'
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
            safe_photo = escape_html(photo_url)
            metadata.append(f'<a href="{safe_photo}">查看地点图片</a>')
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

    if tool_name == "ip_geo":
        area = " · ".join(
            part for part in (_poi_value(data, "country"), _poi_value(data, "province", "provice"), _poi_value(data, "city"), _poi_value(data, "district")) if part
        )
        if area:
            return f"<p><b>🌐 IP 地理位置</b><br/>{escape_html(area)}</p>"
        return "<p><b>🌐 IP 地理位置</b><br/><i>上游未返回可识别的区域信息。</i></p>"

    if tool_name == "regeocode":
        area = " · ".join(
            part for part in (_poi_value(data, "province", "provice"), _poi_value(data, "city"), _poi_value(data, "district")) if part
        )
        if area:
            return f"<p><b>📍 坐标所属区域</b><br/>{escape_html(area)}</p>"
        return "<p><b>📍 坐标所属区域</b><br/><i>上游未返回可识别的地址组件。</i></p>"
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


def _render_weather_card(payload: object) -> str | None:
    data = _dict(payload)
    forecasts = _list_of_dicts(data.get("forecasts"))
    if not forecasts:
        return None
    city = _poi_value(data, "city") or "天气预报"
    rows = []
    for item in forecasts[:7]:
        daytime = f"{_poi_value(item, 'dayweather')} {_poi_value(item, 'daytemp')}℃".strip()
        nighttime = f"{_poi_value(item, 'nightweather')} {_poi_value(item, 'nighttemp')}℃".strip()
        wind = " ".join(part for part in (_poi_value(item, "daywind"), _poi_value(item, "daypower")) if part)
        rows.append(
            f"<tr><td>{escape_html(_poi_value(item, 'date'))}</td><td>{escape_html(daytime)}</td>"
            f"<td>{escape_html(nighttime)}</td><td>{escape_html(wind)}</td></tr>"
        )
    return (
        f"<p><b>🌤️ {escape_html(city)} 预报</b></p>"
        "<table bordered striped><tr><th>日期</th><th>白天</th><th>夜间</th><th>风力</th></tr>"
        + "".join(rows) + "</table>"
    )


def _render_map_payload(payload: object, tool_name: str | None) -> str | None:
    if not tool_name:
        return None
    poi_cards = _render_poi_cards(payload)
    if poi_cards:
        return poi_cards
    if tool_name in {"geocode", "ip_geo", "regeocode"}:
        card = _render_map_location_card(payload, tool_name)
        if card:
            return card
    if tool_name == "distance":
        card = _render_distance_card(payload)
        if card:
            return card
    if tool_name == "weather":
        card = _render_weather_card(payload)
        if card:
            return card
    if tool_name == "route":
        card = _render_map_route_card(payload)
        if card:
            return card
    return None


def _render_structured_payload(result_str: str, *, map_tool: str | None = None) -> str | None:
    payload = _parse_structured_payload(result_str)
    if payload is None:
        return None
    map_card = _render_map_payload(payload, map_tool)
    if map_card:
        return map_card
    poi_cards = _render_poi_cards(payload)
    if poi_cards:
        return poi_cards
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)) and len(payload) <= 3:
        payload = payload["data"]
    return (
        "<p><b>结构化结果</b><br/><i>已将服务返回转换为可阅读字段；详情可展开查看。</i></p>"
        + _render_structured_value(payload)
    )


# 所有工具的完成态展示统一走 text_editor 风格的 Input/Output 引用块；
# Input 或 Output 任一超过这个行数都做截断，避免长内容把消息撑爆。
_TOOL_UI_MAX_LINES = 20


def _truncate_ui_lines(text: str, max_lines: int = _TOOL_UI_MAX_LINES) -> str:
    """Keep only the first max_lines lines; append a truncation note if cut."""
    text = text if isinstance(text, str) else str(text or "")
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = "\n".join(lines[:max_lines])
    return f"{kept}\n…（已截断，共 {len(lines)} 行，仅显示前 {max_lines} 行）"


def _render_editor_quote(label: str, value: str) -> str:
    """Render a tool's input or output as a plain quoted text block, truncated to _TOOL_UI_MAX_LINES lines."""
    text = value if isinstance(value, str) else str(value or "")
    if not text:
        text = "(empty)"
    else:
        text = _truncate_ui_lines(text)
    quoted_text = escape_html(text).replace("\n", "<br/>")
    return f"<p><b>{escape_html(label)}</b></p><blockquote>{quoted_text}</blockquote>"


def _render_bash_result(result_str: str) -> str:
    """Render bash calls the same way as text_editor: quote-formatted Input and Output."""
    metadata, separator, output = (result_str or "").partition("Output:\n")
    command = ""
    exit_code = ""
    for line in metadata.splitlines():
        if line.startswith("Command: "):
            command = line.removeprefix("Command: ")
        elif line.startswith("Exit code: "):
            exit_code = line.removeprefix("Exit code: ")
    if not separator:
        # 没有标准的 "Output:" 分隔符时，把完整原始返回当作 Output 展示。
        return _render_editor_quote("Input", command) + _render_editor_quote("Output", result_str)
    output_text = output
    if exit_code:
        output_text = f"[exit code {exit_code}]\n{output}"
    return _render_editor_quote("Input", command) + _render_editor_quote("Output", output_text)


def _editor_result_summary(result_str: str) -> str:
    """Discard internal snapshot metadata; front-end Output shows the result text."""
    message, _marker, _snapshot = (result_str or "").partition("Latest file snapshot (tail 10):\n")
    return message.strip() or result_str or ""


def _render_editor_result(command: str, path: str, result_str: str, arguments: dict | None = None) -> str:
    """Render text-editor calls as explicit, quote-formatted Input and Output."""
    arguments = arguments or {}
    if command == "view":
        intent = str(arguments.get("_description") or "Inspect the requested text file.")
        return _render_editor_quote("Input", intent) + _render_editor_quote("Output", result_str)

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

_RUNTIME_STATE_FILENAME = "runtime.json"


def _runtime_state_path(chat_id: int, namespace: str | None = None) -> Path:
    return workspace_root(chat_id, namespace) / _RUNTIME_STATE_FILENAME


def _tool_version(exe: str) -> str | None:
    path = shutil.which(exe)
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2,
            check=False,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
                "LC_ALL": "C.UTF-8",
                "LANG": "C.UTF-8",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (proc.stdout or "").splitlines()
    return line[0].strip()[:300] if line else None


def _prepare_runtime_once(
    chat_id: int,
    cache_root: Path,
    namespace: str | None = None,
) -> dict:
    """Record host toolchain discovery once per persistent workspace.

    This deliberately does *not* install compilers per Bash invocation. The base image
    owns the toolchain; the workspace owns only reusable caches and a small manifest.
    """
    state_path = _runtime_state_path(chat_id, namespace)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and state.get("schema") == 1:
                return state
        except (OSError, ValueError, TypeError):
            pass

    tools = {
        name: {
            "path": shutil.which(name),
            "version": _tool_version(name),
        }
        for name in ("python3", "gcc", "g++", "clang", "clang++", "make", "cmake", "ccache")
    }
    state = {
        "schema": 1,
        "prepared": True,
        "cache_root": str(cache_root),
        "tools": tools,
    }
    try:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Unable to persist runtime state chat_id=%s: %s", chat_id, exc)
    return state


# =====================================================================
# BashSession —— 每会话独立沙箱
# =====================================================================
class BashSession:
    def __init__(self, chat_id: int, namespace: str | None = None):
        self.chat_id = chat_id
        self.namespace = workspace_namespace(chat_id, namespace)
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._started = False
        self.workspace = workspace_root(chat_id, self.namespace)
        self.workdir = workspace_workdir(chat_id, self.namespace)
        self._watchdog_task: Optional[asyncio.Task] = None
        self._runtime_state: Optional[dict] = None
        self._runtime_prepare_lock = asyncio.Lock()
        # cwd 必须由模型通过 `cd` 自己控制；选择使用 skill 后可进入
        # `skills/<skill_id>`，persistent bash 会保持当前目录与 shell 状态。
        # 跟踪上一次命令结束后的真实 PWD，用于在 upload/ 或 download/ 子树内
        # 拒绝执行下一条命令。None 表示尚未执行过命令，假定位于 workdir。
        self._last_cwd: Optional[str] = str(self.workdir.absolute())

    async def start(self):
        """启动 bash 进程，套上 Landlock 沙箱 + rlimit + no-new-privs"""
        if self.proc is not None and self.proc.returncode is None:
            return

        # workspace 目录权限 700，防跨 chat 读取
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workspace, 0o700)
        os.chmod(self.workdir, 0o700)

        # 新进程的 cwd 必然是 workdir；重置 _last_cwd，避免上一次会话
        # 残留的 cwd 状态误拒下一条命令。
        self._last_cwd = str(self.workdir.absolute())

        argv = build_sandbox_argv()
        env = build_sandbox_env(self.workspace, self.chat_id, self.namespace)
        cache_root = runtime_cache_root(self.chat_id, self.namespace)
        async with self._runtime_prepare_lock:
            if self._runtime_state is None:
                # One-time discovery per persistent workspace. Subsequent Bash restarts
                # reuse the manifest instead of "preparing" the toolchain again.
                self._runtime_state = await asyncio.to_thread(
                    _prepare_runtime_once, self.chat_id, cache_root, self.namespace
                )

        # ★ Landlock：把文件系统访问限制在该 chat 的 workspace 层，
        #   runtime/、skills/ 都在这里；R2 不再对工作区做全量同步。
        #   通过 functools.partial 把 workspace 路径传给 preexec。
        import functools
        preexec = functools.partial(
            _preexec_sandbox,
            str(self.workspace.absolute()),
        )

        logger.info(
            "Starting bash session chat_id=%s runtime_prepared=%s cache=%s",
            self.chat_id,
            bool(self._runtime_state and self._runtime_state.get("prepared")),
            cache_root,
        )

        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            text=False,
            bufsize=0,
            env=env,  # ★ 关键: 不传任何敏感变量
            cwd=str(self.workdir.absolute()),  # ★ 关键: 沙箱进程启动即位于 workspace root
            start_new_session=True,  # ★ 关键: 创建新会话，便于 killpg
            preexec_fn=preexec,  # Landlock + no-new-privs + rlimit
        )

        # 启动看门狗（fork bomb 防护）
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(
                watchdog(self.proc), name=f"watchdog-{self.chat_id}"
            )

        self._started = True

    # ===================== 命令安全检查（最小黑名单） =====================
    # 设计原则: 不限制语法（heredoc/管道/重定向/&&/|| 全部允许），
    #          只拦截极端灾难模式，剩余靠沙箱兜底
    _DANGEROUS_PATTERNS = [
        # rm -rf / 或 rm -rf /*
        (re.compile(r'\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*)\s+/(?:\s|$|\*)'),
         "rm -rf /"),
        # fork bomb
        (re.compile(r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'),
         "fork bomb"),
        # 写裸设备
        (re.compile(r'\bdd\s+if=\S+\s+of=/dev/(?!null|zero|random|urandom)'),
         "dd to raw device"),
        # mkfs 任意设备
        (re.compile(r'\bmkfs\.\w+\s+/dev/'),
         "mkfs on device"),
        # 写 /dev/mem /dev/kmem
        (re.compile(r'\bof=/dev/(mem|kmem|port)'),
         "write to kernel memory"),
        # :(){...} 的变体
        (re.compile(r'\.\s*\(\s*\)\s*\{'),
         "anonymous fork function"),
    ]

    # ===================== upload/ & download/ 子树保护 =====================
    # 这两棵子树是“产物暂存区”和“用户上传落地”，不允许 bash 在其中执行命令。
    # 主要威胁：模型 cd 进 upload/ 之后跑 `pip install`，会把整个依赖树装进
    # upload/，污染即将发给用户的产物；同理 download/ 也不允许被执行污染。
    #
    # 检测策略：
    #   1. 命令字符串里的 `cd` 目标若指向 upload/ 或 download/（任意前缀形式：
    #      `upload/`, `./upload/`, `../upload/`, `../upload/sub`, 绝对路径等）
    #      直接拒绝。
    #   2. 每次执行前检查 _last_cwd；若已经在 upload/ 或 download/ 内，拒绝执行
    #      并提示模型先 `cd` 回 workdir。
    _UPLOAD_DOWNLOAD_CD_PATTERN = re.compile(
        r"""(?:^|[\s;&|`(])       # 命令起始或分隔符
            cd\s+                 # cd 命令
            (?:['"]?)             # 可选引号
            (?:\./)?              # 可选 ./
            (?:\.\./)*            # 任意数量的 ../
            (?:upload|download)   # 目标目录名
            (?:/|['"]|\s|$)       # 后续分隔
        """,
        re.VERBOSE,
    )

    def _is_safe(self, command: str) -> bool:
        """最小黑名单，仅拦极端操作；其余靠沙箱"""
        if not command or not command.strip():
            return False
        for pattern, name in self._DANGEROUS_PATTERNS:
            if pattern.search(command):
                logger.warning(f"🚫 Bash rejected ({name}) chat_id={self.chat_id}: {command[:200]}")
                return False
        # 拒绝 cd 进入 upload/ 或 download/ 子树
        if self._UPLOAD_DOWNLOAD_CD_PATTERN.search(command):
            logger.warning(
                f"🚫 Bash rejected (cd into upload/download) chat_id={self.chat_id}: {command[:200]}"
            )
            return False
        # 拒绝在 upload/ 或 download/ 子树内执行任何命令
        if self._last_cwd and is_inside_upload_or_download(self._last_cwd):
            logger.warning(
                f"🚫 Bash rejected (cwd inside upload/download) chat_id={self.chat_id} cwd={self._last_cwd}"
            )
            return False
        return True

    async def _execute_heredoc_isolated(self, command: str, timeout: int, progress_callback=None) -> str:
        """Execute heredoc-heavy commands in a one-shot bash process.

        A persistent stdin-backed shell can deadlock when a model emits an
        incomplete heredoc: the shell keeps waiting for the terminator, while
        our synthetic end marker is consumed as heredoc input.  A one-shot
        `bash -lc` receives an actual EOF at the end of `command`, so malformed
        heredocs terminate with a shell error instead of hanging the session.
        """
        workspace = self.workspace
        cwd = self._last_cwd or str(self.workdir.absolute())
        env = build_sandbox_env(self.workspace, self.chat_id, self.namespace)
        import functools
        preexec = functools.partial(_preexec_sandbox, str(workspace.absolute()))

        marker = f"__ONE_SHOT_END_{random.randint(100000, 999999)}__"
        full_cmd = command.rstrip() + f"\nprintf '{marker} %s\n' \"$?\"\nprintf '__ONE_SHOT_CWD__ %s\n' \"$PWD\"\n"

        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", full_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            text=False,
            bufsize=0,
            env=env,
            cwd=cwd,
            start_new_session=True,
            preexec_fn=preexec,
        )

        output_parts: list[str] = []
        last_emit = 0.0

        async def emit_progress(force: bool = False):
            nonlocal last_emit
            if progress_callback is None:
                return
            now = time.monotonic()
            if not force and now - last_emit < 1.0:
                return
            preview = "".join(output_parts)[-8000:]
            try:
                result = progress_callback(preview or "正在执行 Bash 命令…")
                if asyncio.iscoroutine(result):
                    await result
                last_emit = now
            except asyncio.CancelledError:
                raise
            except Exception as cb_error:
                logger.debug("bash isolated progress callback failed: %s", cb_error)

        try:
            while True:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=timeout)
                if not chunk:
                    break
                output_parts.append(chunk.decode("utf-8", errors="replace"))
                await emit_progress()
                # Once the first byte arrived, reset the idle read timer to keep
                # long-running commands alive while still detecting a total hang.
                timeout = max(timeout, 1)
        except asyncio.TimeoutError:
            logger.warning("Bash isolated timeout chat_id=%s cmd=%s", self.chat_id, command[:120])
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass
            return f"Error: Command timed out after {timeout} seconds (isolated bash killed)"
        except asyncio.CancelledError:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass
            raise

        await proc.wait()
        await emit_progress(force=True)
        output = "".join(output_parts)
        exit_code = proc.returncode if proc.returncode is not None else "unknown"
        marker_match = re.search(rf"(?m)^{re.escape(marker)}\s+(-?\d+)\s*$", output)
        if marker_match:
            exit_code = marker_match.group(1)
            output = re.sub(rf"(?m)^{re.escape(marker)}\s+-?\d+\s*$\n?", "", output)
        cwd_match = re.search(r"(?m)^__ONE_SHOT_CWD__\s+(.+)$", output)
        actual_cwd = cwd_match.group(1).strip() if cwd_match else cwd
        output = re.sub(r"(?m)^__ONE_SHOT_CWD__\s+.*$\n?", "", output)
        self._last_cwd = actual_cwd
        if len(output) > 20000:
            output = output[:20000] + "\n... (truncated)"
        output = re.sub(r'\x1b\[[0-9;]*m', '', output)
        return (f"Command: {command}\n"
                f"Cwd: {actual_cwd}\n"
                f"Exit code: {exit_code}\n"
                f"Sandbox: landlock\n"
                f"Execution mode: isolated (heredoc-safe)\n"
                f"Output:\n{output}")

    # ===================== 执行命令 =====================
    async def execute(self, command: str, timeout: int = SANDBOX_TIMEOUT_SEC, progress_callback=None) -> str:
        """在沙箱中执行 bash 命令，超时自动终止"""
        # ★ init 在 workspace lock 外面执行：R2 网络同步可能耗时数秒，
        #   不应阻塞其他工具调用获取 workspace lock。init 只需要 init_lock
        #   （在 _ensure_workspace_initialized 内部获取），与 workspace lock 独立。
        #   init 失败不阻断 bash：本地 workspace 可能不全但 bash 仍可运行。
        try:
            await _ensure_runtime_workspace(self.chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"_ensure_workspace_initialized failed (continue): {e}")

        lock = await _get_workspace_lock(self.chat_id)
        async with lock:
            if self.proc is None or self.proc.returncode is not None:
                await self.start()

            if not self._is_safe(command):
                # 给出更可操作的错误信息，让模型知道为什么被拒、该怎么做。
                if self._last_cwd and is_inside_upload_or_download(self._last_cwd):
                    return (
                        f"Error: Command rejected — current shell cwd is inside an "
                        f"upload/ or download/ staging tree ({self._last_cwd}). "
                        f"These directories are read/write data buffers, not execution "
                        f"roots: running commands here (e.g. pip install) would pollute "
                        f"the staging area. Run `cd` to return to your workdir first, "
                        f"then re-issue the command."
                    )
                if self._UPLOAD_DOWNLOAD_CD_PATTERN.search(command):
                    return (
                        f"Error: Command rejected — `cd` into upload/ or download/ is "
                        f"not allowed. These directories are staging buffers: read and "
                        f"write files in them via relative paths (e.g. "
                        f"`cp out.txt ../upload/out.txt`, `cat ../download/doc.pdf`), "
                        f"but never execute commands from inside them. To move a file "
                        f"into upload/ use the stage_upload tool; to pull a file from "
                        f"download/ use the fetch_download tool."
                    )
                return f"Error: Command rejected for security reasons: {command}"

            # Heredoc commands are executed in a one-shot shell. This prevents an
            # incomplete model-generated `<<EOF` from leaving the persistent shell
            # blocked on stdin forever and consuming our end marker as heredoc data.
            if re.search(r"<<-?\s*(?:[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?)", command):
                return await self._execute_heredoc_isolated(
                    command, timeout=timeout, progress_callback=progress_callback
                )

            marker = f"__END_{random.randint(100000, 999999)}__"
            cwd_marker = f"__CWD_{random.randint(100000, 999999)}__"
            # 默认 shell 启动目录为 workspace/workspace root。模型决定使用 skill 后，
            # 可自行 `cd skills/<skill_id>`；persistent bash 会保留该 cwd。
            # ★ 关键：在 echo marker 前先输出一个换行，确保 marker 单独占一行。
            #   如果命令输出不以换行结尾（如 cat 无换行文件、printf 无 \n），
            #   echo 的输出会粘在前一行，readline() 永远读不到以 marker 开头的行，
            #   导致整个会话 hang 死。
            # 同时记录命令结束后的真实 PWD，用于结果显示；不会改变 shell 状态。
            full_cmd = (
                f"{command}; echo; printf '{cwd_marker} %s\n' \"$PWD\"; "
                f"echo '{marker} $?'\n"
            )

            try:
                self.proc.stdin.write(full_cmd.encode('utf-8'))
                await self.proc.stdin.drain()

                output_parts = []
                exit_code = "unknown"
                progress_last_emit = 0.0
                progress_chars_at_emit = 0
                progress_min_interval = 0.20
                progress_min_chars = 256

                async def emit_progress(force: bool = False):
                    nonlocal progress_last_emit, progress_chars_at_emit
                    if progress_callback is None or not output_parts:
                        return
                    output_text = "".join(output_parts)
                    now = time.monotonic()
                    grew = len(output_text) - progress_chars_at_emit
                    if not force and grew < progress_min_chars and (now - progress_last_emit) < progress_min_interval:
                        return
                    # 前端草稿只需要最近一段日志；完整输出仍由最终结果保留。
                    preview_text = output_text[-8000:]
                    try:
                        result = progress_callback(preview_text)
                        if asyncio.iscoroutine(result):
                            await result
                        progress_last_emit = now
                        progress_chars_at_emit = len(output_text)
                    except asyncio.CancelledError:
                        raise
                    except Exception as cb_error:
                        # UI 推送失败绝不能影响 Bash 本身执行。
                        logger.debug(f"bash progress callback failed: {cb_error}")

                async def read_until_marker():
                    nonlocal exit_code
                    # marker 可能跨 chunk 被拆开，因此只保留一个很小的尾部用于跨 chunk 匹配；
                    # 已经确定不可能包含 marker 的前缀立即写入 output_parts，避免每次都 O(n) 拼接。
                    pending = ""
                    keep_tail = len(marker) + 64
                    while True:
                        chunk = await self.proc.stdout.read(4096)
                        if not chunk:
                            if pending:
                                output_parts.append(pending)
                                pending = ""
                            break

                        pending += chunk.decode('utf-8', errors='replace')
                        marker_pos = pending.find(marker)
                        if marker_pos >= 0:
                            # marker 前是命令真实输出；后面紧接着是 echo 的退出码。
                            if marker_pos:
                                output_parts.append(pending[:marker_pos])
                            marker_tail = pending[marker_pos:]
                            match = re.search(rf"{re.escape(marker)}\s+(-?\d+)", marker_tail)
                            if match:
                                exit_code = match.group(1)
                            pending = ""
                            await emit_progress(force=True)
                            break

                        if len(pending) > keep_tail:
                            output_parts.append(pending[:-keep_tail])
                            pending = pending[-keep_tail:]

                        await emit_progress(force=False)

                await asyncio.wait_for(read_until_marker(), timeout=timeout)

                output = "".join(output_parts)
                if len(output) > 20000:
                    output = output[:20000] + "\n... (truncated)"
                output = re.sub(r'\x1b\[[0-9;]*m', '', output)

                # 提取命令结束后的真实 PWD，同时把内部 marker 从用户输出中移除。
                actual_cwd = str(self.workdir.absolute())
                cwd_match = re.search(rf'(?m)^' + re.escape(cwd_marker) + r' (.+)$', output)
                if cwd_match:
                    actual_cwd = cwd_match.group(1).strip()
                    output = re.sub(rf'(?m)^' + re.escape(cwd_marker) + r' .*$\n?', '', output)

                # 记录最新 cwd，下一次 _is_safe 会据此拒绝在 upload/ 或 download/
                # 子树内继续执行命令。即便 cd 进入被拒，模型也可能通过 pushd /
                # 子 shell 等方式间接进入，这里再做一次防御性检查。
                self._last_cwd = actual_cwd
                if is_inside_upload_or_download(actual_cwd):
                    # 不在这里 return error —— 命令已经执行完了，输出仍然有用。
                    # 但在下一次调用时 _is_safe 会拒绝继续执行。
                    output += (
                        "\n[warning] cwd is now inside upload/ or download/. "
                        "The next command will be rejected; run `cd` (back to your "
                        "workdir) first."
                    )

                # 合并后台同步；不会为每次 Bash 创建一个新的全量上传任务。

                return (f"Command: {command}\n"
                        f"Cwd: {actual_cwd}\n"
                        f"Exit code: {exit_code}\n"
                        f"Sandbox: landlock\n"
                        f"Output:\n{output}")

            except asyncio.CancelledError:
                # 外层 asyncio.wait_for 超时、请求取消或应用关闭时，必须同步清理
                # 当前 bash 进程；否则“工具已超时”但实际命令仍在后台继续执行，
                # 下一次 Bash 还可能复用同一个脏 session。
                logger.warning(
                    "Bash execution cancelled; killing session chat_id=%s cmd=%s",
                    self.chat_id,
                    command[:100],
                )
                try:
                    await asyncio.shield(self.close())
                except Exception:
                    logger.exception("Failed to clean up cancelled bash session chat_id=%s", self.chat_id)
                raise

            except asyncio.TimeoutError:
                logger.warning(f"Bash timeout chat_id={self.chat_id} cmd={command[:100]}")
                try:
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except ProcessLookupError:
                    pass
                # 重启会话
                await self.close()
                return f"Error: Command timed out after {timeout} seconds (sandbox killed & session will restart)"

            except Exception as e:
                logger.exception(f"Bash execute error chat_id={self.chat_id}")
                return f"Error: {str(e)}"

    # ===================== 关闭会话 =====================
    async def close(self):
        # 取消看门狗
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("bash session force-close wait timed out chat_id=%s", self.chat_id)
            except Exception:
                logger.exception("bash session close wait failed chat_id=%s", self.chat_id)
            finally:
                self.proc = None
                self._started = False
            return
        self.proc = None
        self._started = False

# =====================================================================
# BashSessionManager —— 多 chat 共享管理
# =====================================================================
class BashSessionManager:
    def __init__(self):
        self._sessions: dict = {}
        self._lock = asyncio.Lock()

    async def get_session(self, chat_id: int, namespace: str | None = None) -> BashSession:
        resolved_namespace = workspace_namespace(chat_id, namespace)
        key = (chat_id, resolved_namespace)
        async with self._lock:
            if key not in self._sessions:
                session = BashSession(chat_id, resolved_namespace)
                await session.start()
                self._sessions[key] = session
            else:
                # 进程已死则重建
                s = self._sessions[key]
                if s.proc is None or s.proc.returncode is not None:
                    await s.start()
            return self._sessions[key]

    async def restart_session(self, chat_id: int, namespace: str | None = None) -> str:
        resolved_namespace = workspace_namespace(chat_id, namespace)
        key = (chat_id, resolved_namespace)
        async with self._lock:
            session = self._sessions.get(key)
            if session:
                await session.close()
                del self._sessions[key]
            new_session = BashSession(chat_id, resolved_namespace)
            await new_session.start()
            self._sessions[key] = new_session
            return f"Bash session restarted (sandbox=landlock)"

    async def cleanup_all(self):
        """优雅关闭所有会话（应用退出时调用）"""
        async with self._lock:
            for s in self._sessions.values():
                try:
                    await s.close()
                except Exception:
                    pass
            self._sessions.clear()

_bash_manager = BashSessionManager()

# =====================================================================
# execute_bash —— 工具调用入口（保持原签名，外部无需修改）
# =====================================================================
async def execute_bash(
    chat_id: int,
    command: str = "",
    restart: bool = False,
    progress_callback=None,
    namespace: str | None = None,
) -> str:
    resolved_namespace = workspace_namespace(chat_id, namespace)
    if restart:
        result = await _bash_manager.restart_session(chat_id, resolved_namespace)
        return result
    if not command:
        return "Error: command is required (or set restart=true)"
    try:
        session = await _bash_manager.get_session(chat_id, resolved_namespace)
    except RuntimeError as e:
        return f"Error: {e}"
    # 执行命令；workspace 本地文件不会自动同步到 R2。
    return await session.execute(command, progress_callback=progress_callback)

# ---------- 工具结果格式化 ----------

# Magic marker emitted by ai_handlers.run_one on asyncio.TimeoutError.
# format_tool_result intercepts this BEFORE any other branch so we can
# surface a user-safe message and avoid leaking the actual timeout value.
# Human-readable label per tool name, used when surfacing timeout messages.
# Falls back to the raw fn_name if not listed here.
_TOOL_TIMEOUT_LABELS = {
    "web_search": "Web search",
    "fetch_url": "Page fetch",
    "wikipedia": "Wikipedia lookup",
    "exchange_rate": "Exchange rate lookup",
    "book_lookup": "Book lookup",
    "weather": "Weather fetch",
    "news": "News fetch",
    "crypto_price": "Crypto price lookup",
    "ip_geo": "IP geolocation",
    "qr_code": "QR code generation",
    "generate_video": "Video generation",
    "geocode": "Geocoding",
    "route": "Route planning",
    "distance": "Distance calculation",
    "poi_keyword_search": "POI keyword search",
    "poi_nearby_search": "Nearby POI search",
    "poi_details": "POI detail lookup",
    "text_editor": "Text editor operation",
    "bash": "Bash command",
    "present_files": "File presentation",
    "fetch_download": "Fetch from download/",
    "stage_upload": "Stage to upload/",
    "list_download": "List download/",
    "list_upload": "List upload/",
}

async def format_tool_result(fn_name: str, fn_args: dict, result_str: str) -> tuple[str, str]:
    def escape_text(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ---- Intercept timeout magic marker BEFORE any other branch ----
    # The raw exception (with TOOL_CALL_TIMEOUT seconds) is kept in
    # logger.error on the backend; the UI only sees the friendly version.
    if result_str == _TOOL_TIMEOUT_MARKER:
        label = _TOOL_TIMEOUT_LABELS.get(fn_name, fn_name)
        summary = f"⏱️ {label} timed out"
        details_html = "Execution exceeded the timeout limit. Please refine your request or try again later."
        return summary, details_html

    if fn_name == "web_search":
        query = fn_args.get('query', '')
        # execute_web_search 返回固定 envelope：成功数/请求数；只在旧格式下
        # 才回退到标题计数，避免把失败误报成 0 results。
        count_match = re.search(r'\[成功:[^\]]+\].*?[（(]\s*(\d+)\s*/\s*(\d+)\s*[）)]', result_str or "", re.S)
        if count_match:
            num_results = int(count_match.group(1))
        else:
            num_results = result_str.count("标题：") if "标题：" in result_str else 0
        if result_str.lstrip().startswith("❌"):
            summary = "Search failed"
        elif query and num_results == 1:
            summary = f"{query} 1 result"
        elif query:
            summary = f"{query} {num_results} results"
        else:
            summary = "Searched the web"
        if "标题：" in result_str and "链接：" in result_str:
            items_html = ""
            current_title = ""
            current_link = ""
            for line in result_str.split('\n'):
                if "标题：" in line:
                    current_title = line.split("标题：")[-1].strip()
                elif "链接：" in line:
                    current_link = line.split("链接：")[-1].strip()
                    if current_title and current_link:
                        if current_link.startswith("http"):
                            domain = current_link.split('/')[2] if '//' in current_link else current_link
                            items_html += f"<li><a href=\"{current_link}\">{current_title}</a> <code>{domain}</code></li>"
                        else:
                            items_html += f"<li>{current_title} <code>{current_link}</code></li>"
                        current_title = ""
                        current_link = ""
            if items_html:
                details_html = f"<ol>{items_html}</ol>"
            else:
                details_html = escape_text(result_str[:60000])
        else:
            details_html = escape_text(result_str[:60000])
        return summary, details_html

    elif fn_name == "fetch_url":
        url = fn_args.get('url', '')
        domain = extract_domain(url)
        if "失败" in result_str or "超时" in result_str or "Failed" in result_str or "Error" in result_str:
            logger.error(f"[fetch_url] Failed to fetch {url}: {result_str[:500]}")
            summary = f"🌐 Failed to fetch {domain}"
            details_html = "Unable to retrieve content. Check the URL or try again later."
        else:
            title = domain
            if "🏷️" in result_str:
                match = re.search(r'🏷️\s+([^\n]+)', result_str)
                if match:
                    title = match.group(1).strip()
            summary = f"🌐 Fetched: {title}"
            safe_url = html.escape(url)
            safe_domain = html.escape(domain)
            safe_title = html.escape(title)
            details_html = f"{safe_title} <a href=\"{safe_url}\">{safe_domain}</a>"
        return summary, details_html

    elif fn_name == "weather":
        try:
            weather_data = json.loads(result_str)
            if "error" in weather_data:
                error_msg = weather_data["error"]
                summary = "🌤️ 天气查询失败"
                details_html = f"<pre><code>{error_msg}</code></pre>"
                return summary, details_html

            city = weather_data.get("city", "未知")
            current = weather_data.get("current", {})
            hourly = weather_data.get("hourly", [])
            daily = weather_data.get("daily", [])
            unit_display = "℃" if weather_data.get("unit") == "C" else "℉"

            temp = current.get("temp", "N/A")
            cond = current.get("condition", "")
            summary = f"🌤️ {city} {temp}{unit_display} {cond}"

            details_html = f"<b>{city} 详细天气</b><br/><br/>"
            details_html += "<h3>📍 当前天气</h3>"
            details_html += f"🌡️ 温度：{temp}{unit_display}（体感 {current.get('feels_like', 'N/A')}{unit_display}）<br/>"
            details_html += f"💧 湿度：{current.get('humidity', 'N/A')}% 💨 风速：{current.get('wind', 'N/A')} km/h"
            if current.get('wind_gust', 'N/A') != 'N/A':
                details_html += f"（阵风 {current['wind_gust']} km/h）"
            details_html += "<br/>"
            details_html += f"☁️ 云量：{current.get('cloudcover', 'N/A')}% 🌡️ 气压：{current.get('pressure', 'N/A')} mb<br/>"
            details_html += f"👁️ 能见度：{current.get('visibility', 'N/A')} km ☀️ 紫外线指数：{current.get('uvIndex', 'N/A')}<br/>"
            details_html += f"🌧️ 降水：{current.get('precip', '0.0')} mm 🧭 风向：{current.get('wind_dir', 'N/A')} ({current.get('wind_deg', 'N/A')}°)<br/>"
            details_html += f"🕒 观测时间：{current.get('obs_time', '')}<br/>"
            details_html += f"🌥️ 天气状况：{cond}<br/><br/>"

            if daily:
                details_html += "<details><summary>📅 未来几天预报（展开）</summary><br/>"
                details_html += "<table bordered striped cellpadding='3'>"
                details_html += "<tr><th>日期</th><th>天气</th><th>最高</th><th>最低</th><th>UV</th><th>日出</th><th>日落</th><th>降水%</th></tr>"
                for day in daily[:5]:
                    date = day.get("date", "")
                    cond_d = day.get("condition", "")
                    max_t = day.get("max", "N/A")
                    min_t = day.get("min", "N/A")
                    max_display = f"{max_t}{unit_display}" if max_t != "N/A" else "--"
                    min_display = f"{min_t}{unit_display}" if min_t != "N/A" else "--"
                    uv = day.get("uvIndex", "N/A")
                    sunrise = day.get("sunrise", "--")
                    sunset = day.get("sunset", "--")
                    rain = day.get("chance_of_rain", "0") + "%"
                    details_html += f"<tr><td>{date}</td><td>{cond_d}</td><td align='right'>{max_display}</td><td align='right'>{min_display}</td><td align='center'>{uv}</td><td>{sunrise}</td><td>{sunset}</td><td align='right'>{rain}</td></tr>"
                details_html += "</table><br/>"
                details_html += "<details><summary>🌙 天文 &amp; 其他概率</summary><br/>"
                for day in daily[:5]:
                    date = day.get("date", "")
                    moon_phase = day.get("moon_phase", "--")
                    moon_illum = day.get("moon_illumination", "0") + "%"
                    snow = day.get("chance_of_snow", "0") + "%"
                    thunder = day.get("chance_of_thunder", "0") + "%"
                    fog = day.get("chance_of_fog", "0") + "%"
                    frost = day.get("chance_of_frost", "0") + "%"
                    details_html += f"<b>{date}</b>：月相 {moon_phase}（{moon_illum}），雪 {snow}，雷暴 {thunder}，雾 {fog}，霜冻 {frost}<br/>"
                details_html += "</details><br/>"
                details_html += "</details><br/>"

            if hourly:
                details_html += "<details><summary>⏰ 逐时预报（展开）</summary><br/>"
                details_html += "<table bordered striped cellpadding='3'>"
                details_html += "<tr><th>时间</th><th>天气</th><th>温度</th><th>降水</th><th>湿度</th><th>风速</th><th>气压</th><th>UV</th></tr>"
                for h in hourly[:24]:
                    time_str = h.get("time", "")
                    cond_h = h.get("condition", "")
                    temp_h = h.get("temp", "N/A")
                    precip_h = h.get("precip", "0")
                    humidity_h = h.get("humidity", "N/A")
                    wind_speed_h = h.get("wind_speed", "N/A")
                    pressure_h = h.get("pressure", "N/A")
                    uv_h = h.get("uvIndex", "N/A")
                    details_html += f"<tr><td>{time_str}</td><td>{cond_h}</td><td align='right'>{temp_h}{unit_display}</td><td align='right'>{precip_h} mm</td><td align='right'>{humidity_h}%</td><td align='right'>{wind_speed_h} km/h</td><td align='right'>{pressure_h} mb</td><td align='center'>{uv_h}</td></tr>"
                details_html += "</table><br/>"
                details_html += "<details><summary>🌪️ 逐时额外数据（阵风、云量、能见度、风向、概率、露点等）</summary><br/>"
                details_html += "<table bordered striped cellpadding='2'>"
                details_html += "<tr><th>时间</th><th>阵风</th><th>云量</th><th>能见度</th><th>风向</th><th>雨%</th><th>雪%</th><th>雷暴%</th><th>雾%</th><th>霜冻%</th><th>露点</th><th>热指数</th><th>风寒</th></tr>"
                for h in hourly[:24]:
                    time_str = h.get("time", "")
                    gust = h.get("wind_gust", "N/A")
                    cloud = h.get("cloudcover", "N/A")
                    vis = h.get("visibility", "N/A")
                    wind_dir = h.get("wind_dir", "N/A")
                    rain = h.get("chance_of_rain", "0") + "%"
                    snow = h.get("chance_of_snow", "0") + "%"
                    thunder = h.get("chance_of_thunder", "0") + "%"
                    fog = h.get("chance_of_fog", "0") + "%"
                    frost = h.get("chance_of_frost", "0") + "%"
                    dew = h.get("DewPointC", "N/A")
                    heat = h.get("HeatIndexC", "N/A")
                    chill = h.get("WindChillC", "N/A")
                    details_html += f"<tr><td>{time_str}</td><td align='right'>{gust} km/h</td><td align='right'>{cloud}%</td><td align='right'>{vis} km</td><td>{wind_dir}</td><td align='right'>{rain}</td><td align='right'>{snow}</td><td align='right'>{thunder}</td><td align='right'>{fog}</td><td align='right'>{frost}</td><td align='right'>{dew}°C</td><td align='right'>{heat}°C</td><td align='right'>{chill}°C</td></tr>"
                details_html += "</table>"
                details_html += "</details><br/>"
                details_html += "</details><br/>"

            tips = []
            cond_lower = cond.lower()
            if "雨" in cond or "rain" in cond_lower:
                tips.append("🌂 今天有降水，出门记得带伞。")
            if "霾" in cond or "haze" in cond_lower or "烟雾" in cond:
                tips.append("😷 空气中有雾霾，建议佩戴口罩或减少户外活动。")
            try:
                if int(temp) > 30:
                    tips.append("☀️ 气温较高，注意防暑降温，多补充水分。")
            except (ValueError, TypeError):
                pass
            try:
                if int(current.get('uvIndex', 0)) >= 8:
                    tips.append("🧴 紫外线指数高，外出请做好防晒。")
            except (ValueError, TypeError):
                pass
            try:
                if int(current.get('visibility', 10)) < 2:
                    tips.append("🌫️ 能见度较低，驾车请减速慢行。")
            except (ValueError, TypeError):
                pass
            try:
                if int(current.get('wind', 0)) > 30:
                    tips.append("💨 风速较大，注意防风。")
            except (ValueError, TypeError):
                pass
            if "雪" in cond or "snow" in cond_lower:
                tips.append("❄️ 有降雪，路面湿滑，注意出行安全。")
            if tips:
                details_html += "<b>💡 温馨提示</b><br/>" + "<br/>".join(tips)

            return summary, details_html

        except json.JSONDecodeError:
            safe_log = escape_text(result_str[:60000])
            summary = "🌤️ 天气数据"
            details_html = f"<pre><code>{safe_log}</code></pre>"
            return summary, details_html

    elif fn_name == "wikipedia":
        query = fn_args.get('query', '')
        lang = fn_args.get('lang', 'zh')
        import urllib.parse
        encoded_query = urllib.parse.quote(query)
        wiki_url = f"https://{lang}.wikipedia.org/wiki/{encoded_query}"
        summary = f"📚 {query}"
        details_html = f'<a href="{wiki_url}">{query}</a>'
        return summary, details_html

    elif fn_name == "exchange_rate":
        base = fn_args.get('base', 'USD')
        summary = f"💱 {base} 汇率"
        details_html = result_str
        return summary, details_html
        details_html = result_str
        return summary, details_html

    elif fn_name == "book_lookup":
        query = fn_args.get('query', '')
        summary = f"📖 {query}"
        details_html = result_str
        return summary, details_html

    elif fn_name == "news":
        source = fn_args.get('source', 'news')
        summary = f"📰 {source.upper()} 新闻"
        details_html = result_str
        return summary, details_html

    elif fn_name == "crypto_price":
        coin = fn_args.get('coin', '')
        summary = f"💰 {coin.upper()} 价格"
        details_html = result_str
        return summary, details_html

    elif fn_name == "qr_code":
        if "✅ 二维码生成成功" in result_str:
            img_match = re.search(r'图片链接：([^\s]+)', result_str)
            content_match = re.search(r'内容：([^\n]+)', result_str)
            if img_match:
                img_url = img_match.group(1)
                content_text = content_match.group(1) if content_match else "已编码内容"
                summary = "📱 二维码已生成"
                # 转义 URL（R2 presigned URL 含大量 & 需转义为 &amp;）
                safe_url = escape_html(img_url)
                details_html = (
                    f'<img src="{safe_url}"/><br/>'
                    f'<b>✅ 二维码生成成功</b><br/>'
                    f'<b>内容：</b>{escape_text(content_text)}<br/>'
                    f'<b>链接：</b><a href="{safe_url}">📷 点击查看 / 下载二维码</a>'
                )
                return summary, details_html
        summary = "📱 二维码"
        details_html = escape_text(result_str)
        return summary, details_html

    elif fn_name == "generate_image_from_text":
        if "✅" in result_str:
            lines = result_str.splitlines()
            urls = [line.strip() for line in lines if line.strip().startswith(("http://", "https://"))]
            if urls:
                count = len(urls)
                summary = f"🎨 Generated {count} image" + ("" if count == 1 else "s")
                img_tags = "".join(f'<img src="{escape_html(u)}"/>' for u in urls)
                # 用简短的"图片 1 / 图片 2"文本链接替代裸 URL，避免长 R2 presigned URL 刷屏
                link_items = "".join(
                    f'<li><a href="{escape_html(u)}">图片 {i + 1}</a></li>'
                    for i, u in enumerate(urls)
                )
                caption = f"已生成 {count} 张图片：<ul>{link_items}</ul>"
                # 单图用 <figure>，多图用 <tg-slideshow> 轮播
                if count == 1:
                    details_html = f'<figure>{img_tags}<figcaption>{caption}</figcaption></figure>'
                else:
                    details_html = f'<tg-slideshow>{img_tags}<figcaption>{caption}</figcaption></tg-slideshow>'
                return summary, details_html
        summary = "🎨 Image generation"
        details_html = escape_text(result_str)
        return summary, details_html

    elif fn_name == "edit_image_with_reference":
        if "✅" in result_str:
            lines = result_str.splitlines()
            urls = [line.strip() for line in lines if line.strip().startswith(("http://", "https://"))]
            if urls:
                count = len(urls)
                summary = f"🎨 Edited {count} image" + ("" if count == 1 else "s")
                img_tags = "".join(f'<img src="{escape_html(u)}"/>' for u in urls)
                link_items = "".join(
                    f'<li><a href="{escape_html(u)}">图片 {i + 1}</a></li>'
                    for i, u in enumerate(urls)
                )
                caption = f"已编辑 {count} 张图片：<ul>{link_items}</ul>"
                # 单图用 <figure>，多图用 <tg-slideshow> 轮播
                if count == 1:
                    details_html = f'<figure>{img_tags}<figcaption>{caption}</figcaption></figure>'
                else:
                    details_html = f'<tg-slideshow>{img_tags}<figcaption>{caption}</figcaption></tg-slideshow>'
                return summary, details_html
        summary = "🎨 Image editing"
        details_html = escape_text(result_str)
        return summary, details_html

    elif fn_name == "generate_video":
        # 视频通过 <figure><video> 内嵌在工具结果卡片里渲染（Telegram Rich Message
        # 支持视频 block 与文本同消息共存，参见 Rich Message Formatting Options）。
        # execute_generate_video 返回的结构：
        #   ✅ 已生成视频。
        #   视频链接：https://...
        if "✅" in result_str:
            url_match = re.search(r'视频链接：(https?://[^\s]+)', result_str)
            if url_match:
                # ⚠️ R2 presigned URL 含大量 & 查询参数（X-Amz-Algorithm、X-Amz-Credential、
                # X-Amz-Signature 等），HTML 属性值中未转义的 & 会被 Telegram HTML
                # 解析器当作实体名起点，导致 URL 被截断 → RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND。
                # 必须用 escape_html 转义（与 _agentic_loop_native_video 老路径一致）。
                video_url = url_match.group(1).strip()
                duration_str = ""
                m = re.search(r'(\d+)\s*秒', fn_args.get("prompt", "") or "")
                if m:
                    duration_str = f" · {m.group(1)}s"
                summary = f"🎬 Video generated{duration_str}"
                # <figure><video> 是一个独立 media block，可以与其他 block 同消息发送；
                # 附带简短文本链接 caption，避免裸 R2 presigned URL 刷屏
                details_html = (
                    f'<figure><video src="{escape_html(video_url)}"></video>'
                    f'<figcaption><a href="{escape_html(video_url)}">下载 / 查看视频</a></figcaption>'
                    f'</figure>'
                )
                return summary, details_html
        summary = "🎬 Video generation"
        details_html = escape_text(result_str)
        return summary, details_html

    # ===================== 地图工具（amap-maps MCP 直通） =====================
    # 所有地理 / 路径 / POI / 距离 / IP 工具现在都委托给 amap-maps MCP 服务，
    # 返回内容是该 MCP 的原生输出（通常是 JSON 文本）。这里不再尝试解析特定
    # schema（旧 amap_integration.py / Overpass / OSRM / TomTom / ORS 的字段
    # 都已废弃），改为：
    #   - 若解析出 JSON 且含 status=error，则显示为失败
    #   - 否则把原始输出转义后直接展示给用户，让 LLM 在后续轮次里自由解读。
    elif fn_name in ("geocode", "route", "distance", "poi_keyword_search",
                     "poi_nearby_search", "poi_details", "ip_geo"):
        label_map = {
            "geocode":            "📍 地理编码",
            "route":              "🚗 路线规划",
            "distance":           "📏 距离测量",
            "poi_keyword_search": "📍 POI 关键词搜索",
            "poi_nearby_search": "📍 POI 周边搜索",
            "poi_details":        "📍 POI 详情",
            "ip_geo":             "🌍 IP 地理位置",
        }
        base_label = label_map.get(fn_name, fn_name)

        # 尝试 JSON 解析；只用于识别明确的 error 状态。
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            data = None

        if isinstance(data, dict) and data.get("status") == "error":
            message = data.get("message") or result_str
            summary = f"❌ {base_label}失败"
            details_html = escape_text(str(message))
            return summary, details_html

        ip = fn_args.get('ip') if fn_name == "ip_geo" else None
        summary = base_label + (f" {ip}" if ip else "")
        # 外部 MCP 常返回 JSON 文本。对聊天界面渲染结构化卡片，而向模型仍保留原始结果。
        details_html = _render_structured_payload(result_str, map_tool=fn_name) or _render_code_panel("服务响应 · 最近 10 行", result_str)
        return summary, details_html

    elif fn_name == "text_editor":
        command = fn_args.get("command", "")
        path = fn_args.get("path", "")
        if any(marker in (result_str or "") for marker in ("Error:", "No match found", "requires ")):
            summary = "❌ 文件操作未完成"
        elif command == "view":
            summary = f"📄 查看 {path}" if path else "📄 查看文件"
        elif command == "create":
            summary = f"📄 已创建 {path}" if path else "📄 已创建文件"
        elif command in ("str_replace", "insert"):
            summary = f"📝 已更新 {path}" if path else "📝 已更新文件"
        elif command == "delete":
            summary = f"🗑️ 已删除 {path}" if path else "🗑️ 已删除文件"
        else:
            summary = "📝 文件操作"
        # 每个编辑结果都优先展示写入后文件的最后十行（含绝对行号）。
        details_html = _render_editor_result(command, path, result_str, fn_args)
        return summary, details_html

    # ===================== Todo 工具格式化 =====================
    # execute_todo 返回 JSON 字符串（给 AI 阅读）。UI 这里把它渲染成富文本卡片：
    #   - 顶部统计：总数 / 已完成 / 待办
    #   - 列表项：状态 emoji + 优先级徽章 + 标题（完成则加删除线）+ 标签 chips
    #   - 长列表自动截断并提示
    # 这里仅渲染工具调用气泡里的折叠预览。
    elif fn_name == "todo":
        try:
            import json as _json
            payload = _json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "📋 待办操作"
            details_html = escape_text(result_str)
            return summary, details_html

        if not payload.get("ok"):
            summary = f"❌ 待办操作失败：{payload.get('code', '')}"
            details_html = f"<p>{escape_text(payload.get('error', '未知错误'))}</p>"
            return summary, details_html

        action = payload.get("action", "list")
        if action == "list":
            todos = payload.get("todos", []) or []
            total = payload.get("total", 0)
            pending = payload.get("pending", 0)
            summary = f"📋 共 {total} 项 · 待办 {pending} 项"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "add":
            t = payload.get("todo", {})
            summary = f"➕ 新增 {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action in ("done", "undone", "toggle"):
            t = payload.get("todo", {})
            icon = "✅" if t.get("done") else "↩️"
            summary = f"{icon} {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "delete":
            t = payload.get("todo", {})
            summary = f"🗑️ 删除 {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "clear":
            summary = f"🧹 清理 {payload.get('removed', 0)} 条"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "edit":
            t = payload.get("todo", {})
            summary = f"📝 编辑 {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        summary = "📋 待办操作"
        details_html = render_todo_card(payload)
        return summary, details_html

    # ===================== Memory 工具格式化 =====================
    # execute_memory 返回 JSON 字符串（给 AI 阅读），这里渲染成富文本卡片。
    elif fn_name == "memory":
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "🧠 记忆操作"
            details_html = escape_text(result_str)
            return summary, details_html
        if not payload.get("ok"):
            summary = f"❌ 记忆操作失败：{payload.get('code', '')}"
            details_html = f"<p>{escape_text(payload.get('error', '未知错误'))}</p>"
            return summary, details_html
        action = payload.get("action", "list")
        if action == "list":
            total = payload.get("total", 0)
            shown = payload.get("shown", 0)
            summary = f"🧠 记忆库：{total} 条 · 显示 {shown} 条"
        elif action == "search":
            summary = f"🔎 记忆搜索：{payload.get('matches', 0)} / {payload.get('total', 0)} 条命中"
        elif action == "add":
            m = payload.get("memory", {})
            summary = f"🧠 保存 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "get":
            m = payload.get("memory", {})
            summary = f"🧠 查看 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "update":
            m = payload.get("memory", {})
            summary = f"📝 更新 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "delete":
            m = payload.get("memory", {})
            summary = f"🗑️ 删除 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "clear":
            summary = f"🧹 清理 {payload.get('removed', 0)} 条记忆"
        else:
            summary = "🧠 记忆操作"
        details_html = render_memory_card(payload)
        return summary, details_html

    # ===================== Subagent 工具格式化 =====================
    # execute_subagent 返回 JSON，含 answer / rounds / tool_calls / elapsed。
    # 父 agent 在工具气泡里看到完整子 agent 答复；用户也能从气泡折叠区阅读。
    elif fn_name == "subagent":
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "🤖 子 agent"
            details_html = escape_text(result_str)
            return summary, details_html
        ok = payload.get("ok", False)
        model_name = payload.get("model_name") or payload.get("model") or "?"
        rounds = payload.get("rounds", 0)
        tool_calls = payload.get("tool_calls", 0)
        elapsed = payload.get("elapsed", 0)
        if ok:
            summary = f"🤖 子 agent 完成 · {rounds} 轮 · {tool_calls} 工具 · {elapsed:.1f}s"
        else:
            err = payload.get("error", "未知错误")
            summary = f"❌ 子 agent 失败 · {rounds} 轮 · {err[:40]}"
        details_html = render_subagent_card(payload)
        return summary, details_html

    # ===================== Bash 工具格式化 =====================
    elif fn_name == "bash":
        if "Error:" in result_str or "Command rejected" in result_str:
            summary = "❌ Bash 执行失败"
        else:
            cmd_line = result_str.split("\n")[0].replace("Command: ", "")
            if len(cmd_line) > 30:
                cmd_line = cmd_line[:30] + "…"
            summary = f"🖥 {cmd_line or '命令已完成'}"
        # 保留命令元信息，并将终端输出固定为带行号的最后十行，避免原始日志撑爆工具卡片。
        details_html = _render_bash_result(result_str)
        return summary, details_html

    elif fn_name == "present_files":
        # ---- Decoupled data abstraction ----
        # execute_present_files returns a JSON payload:
        #   {"sent": [...], "failed": [...], "error": str | null}
        # The model context receives this raw JSON (so it can reply concisely,
        # e.g. "Files sent"), while the UI gets a rich, detailed report built
        # from the parsed structure.
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            data = None

        if not isinstance(data, dict):
            # Legacy fallback: result_str was not JSON (e.g. an error string
            # from dispatch_tool_call's top-level exception handler). Render
            # it as escaped plain text so we never break the UI.
            summary = "📂 Presenting files"
            details_html = escape_text(result_str) or "<i>No files were processed.</i>"
            return summary, details_html

        sent = data.get("sent") or []
        failed = data.get("failed") or []
        error = data.get("error")
        # Be defensive: ensure both lists are actually lists.
        if not isinstance(sent, list):
            sent = []
        if not isinstance(failed, list):
            failed = []

        sent_count = len(sent)
        failed_count = len(failed)

        # ---- Summary with correct pluralization (guards None / 0) ----
        if error and sent_count == 0:
            summary = "📂 No files sent"
        elif sent_count == 0:
            summary = "📂 No files sent"
        elif sent_count == 1:
            summary = "📂 Presented 1 file"
        else:
            summary = f"📂 Presented {sent_count} files"

        # ---- Details: HTML list of successes and failures ----
        details_parts: List[str] = []
        if sent:
            items = "".join(f"<li>{escape_text(str(f))}</li>" for f in sent)
            label = "file" if sent_count == 1 else "files"
            details_parts.append(f"<b>✅ Sent ({sent_count} {label})</b><ul>{items}</ul>")
        if failed:
            items = "".join(f"<li>{escape_text(str(f))}</li>" for f in failed)
            label = "file" if failed_count == 1 else "files"
            details_parts.append(f"<b>❌ Failed ({failed_count} {label})</b><ul>{items}</ul>")
        if error:
            details_parts.append(f"<i>{escape_text(str(error))}</i>")

        if not details_parts:
            details_parts.append("<i>No files were processed.</i>")

        details_html = "<br/>".join(details_parts)
        return summary, details_html
    elif fn_name in ("fetch_download", "stage_upload"):
        # 这些工具返回 {"fetched"|"staged": [...], "failed": [...], "error": ...}
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            data = None
        if not isinstance(data, dict):
            return f"📦 {fn_name}", f"<pre><code>{escape_text(result_str)}</code></pre>"
        ok_key = "fetched" if fn_name == "fetch_download" else "staged"
        ok_items = data.get(ok_key) or []
        failed_items = data.get("failed") or []
        verb = "Fetched" if fn_name == "fetch_download" else "Staged"
        if not ok_items:
            summary = f"📦 No files {verb.lower()}"
        elif len(ok_items) == 1:
            summary = f"📦 {verb} 1 file"
        else:
            summary = f"📦 {verb} {len(ok_items)} files"
        if failed_items:
            summary += f" · {len(failed_items)} failed"
        details_parts: List[str] = []
        if ok_items:
            items = "".join(
                f"<li>{escape_text(str(it.get('path')))}</li>"
                for it in ok_items if isinstance(it, dict)
            )
            label = "file" if len(ok_items) == 1 else "files"
            details_parts.append(f"<b>✅ {verb} ({len(ok_items)} {label})</b><ul>{items}</ul>")
        if failed_items:
            items = "".join(f"<li>{escape_text(str(x))}</li>" for x in failed_items)
            label = "file" if len(failed_items) == 1 else "files"
            details_parts.append(f"<b>❌ Failed ({len(failed_items)} {label})</b><ul>{items}</ul>")
        if not details_parts:
            details_parts.append("<i>No files were processed.</i>")
        return summary, "<br/>".join(details_parts)
    elif fn_name in ("list_download", "list_upload"):
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            data = None
        if not isinstance(data, dict):
            return f"📋 {fn_name}", f"<pre><code>{escape_text(result_str)}</code></pre>"
        files = data.get("files") or []
        count = data.get("count", len(files))
        label = "download/" if fn_name == "list_download" else "upload/"
        summary = f"📋 {count} file(s) in {label}"
        if not files:
            details_html = f"<i>{label} is empty.</i>"
        else:
            items = "".join(
                f"<li>{escape_text(str(f.get('path')))} <i>({f.get('size')} bytes)</i></li>"
                for f in files if isinstance(f, dict)
            )
            details_html = f"<b>{label}</b><ul>{items}</ul>"
        return summary, details_html
    else:
        summary = f"🔧 {fn_name}"
        details_html = escape_text(result_str)
        return summary, details_html


async def execute_present_files(chat_id: int, paths: List[str]) -> str:
    """Send files from the upload/ staging tree to the chat as attachments.

    Files MUST live under upload/ (the dedicated outgoing-artifact buffer).
    The model is responsible for staging artifacts there first — either via
    the `stage_upload` tool or via bash using a relative path such as
    `cp out.txt ../upload/out.txt`. Files left in workspace root/ are not
    directly sendable; this is the persistence/execution boundary.
    """
    if not paths:
        return json.dumps({
            "sent": [],
            "failed": [],
            "error": "No paths provided. Files must be staged under upload/ first.",
        })

    # ★ init 在 workspace lock 外面执行（同 bash / text_editor）。
    await _ensure_runtime_workspace(chat_id)

    lock = await _get_workspace_lock(chat_id)
    async with lock:
        upload_root = workspace_upload_root(chat_id)
        sent = []
        failed = []
        # 文件大小上限：50MB，防止 OOM
        _MAX_PRESENT_FILE_SIZE = 50 * 1024 * 1024
        for path in paths:
            if not isinstance(path, str) or not path:
                failed.append(f"{path} (invalid path)")
                continue
            # 拒绝嵌入的 null 字节
            if "\x00" in path:
                failed.append(f"{path} (invalid path)")
                continue
            safe_path = os.path.normpath(path)
            if safe_path == "." or safe_path.startswith("..") or os.path.isabs(safe_path):
                failed.append(f"{path} (invalid path)")
                continue
            local_path = upload_root / safe_path
            # 关键：使用 resolve() 跟随符号链接，再校验最终路径仍在 upload/ 之下
            try:
                resolved = local_path.resolve()
            except Exception:
                failed.append(f"{path} (invalid path)")
                continue
            try:
                upload_resolved = upload_root.resolve()
            except Exception:
                upload_resolved = upload_root
            if resolved != upload_resolved and upload_resolved not in resolved.parents:
                failed.append(f"{path} (invalid path)")
                continue
            if not resolved.is_file():
                failed.append(
                    f"{path} (file not found in upload/ — use stage_upload to copy "
                    f"it from your workdir first)"
                )
                continue
            try:
                file_size = resolved.stat().st_size
                if file_size > _MAX_PRESENT_FILE_SIZE:
                    failed.append(f"{path} (file too large: {file_size} bytes)")
                    continue
                # 使用 asyncio.to_thread 包装同步 read，避免阻塞事件循环
                file_data = await asyncio.to_thread(resolved.read_bytes)
                # 显式超时，防止 hang 死
                timeout = aiohttp.ClientTimeout(total=60)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(chat_id))
                    form.add_field("document", file_data, filename=resolved.name)
                    async with session.post(f"{BASE_URL}/sendDocument", data=form) as resp:
                        if resp.status == 200:
                            sent.append(resolved.name)
                        else:
                            failed.append(f"{path} (send failed: {resp.status})")
            except Exception as e:
                failed.append(f"{path} (error: {str(e)[:50]})")
        return json.dumps({"sent": sent, "failed": failed, "error": None})


async def execute_fetch_download(chat_id: int, filenames: List[str], overwrite: bool = False) -> str:
    """Copy one or more files from download/ into the runtime workdir.

    User-uploaded documents land in download/ (Telegram → R2 → local). bash
    cannot `cd` into download/, and the model is expected to fetch only the
    files it actually needs rather than hydrate the whole tree. After
    fetch_download, the file is available in the workdir under the same
    relative path and can be opened with text_editor / bash / etc.
    """
    if not isinstance(filenames, list) or not filenames:
        return json.dumps({
            "fetched": [],
            "failed": [],
            "error": "filenames must be a non-empty list.",
        })
    fetched = []
    failed = []
    for raw in filenames:
        name = raw if isinstance(raw, str) else str(raw)
        try:
            result = await fetch_from_download(chat_id, name, overwrite=overwrite)
            fetched.append(result)
        except Exception as exc:
            failed.append(f"{name}: {str(exc)[:160]}")
    return json.dumps(
        {"fetched": fetched, "failed": failed, "error": None if not failed else "Some files were not fetched."},
        ensure_ascii=False,
    )


async def execute_stage_upload(chat_id: int, paths: List[str]) -> str:
    """Copy one or more files from the runtime workdir into upload/.

    upload/ is the sole source for present_files. Staging is explicit so
    that dependency trees, build artifacts, and other runtime material
    never accidentally get sent to the user.
    """
    if not isinstance(paths, list) or not paths:
        return json.dumps({
            "staged": [],
            "failed": [],
            "error": "paths must be a non-empty list.",
        })
    staged = []
    failed = []
    for raw in paths:
        rel = raw if isinstance(raw, str) else str(raw)
        try:
            result = await stage_to_upload(chat_id, rel)
            staged.append(result)
        except Exception as exc:
            failed.append(f"{rel}: {str(exc)[:160]}")
    return json.dumps(
        {"staged": staged, "failed": failed, "error": None if not failed else "Some files were not staged."},
        ensure_ascii=False,
    )


async def execute_list_download(chat_id: int) -> str:
    """List files in download/ (user-uploaded documents)."""
    items = await list_download_files(chat_id)
    return json.dumps({"files": items, "count": len(items)}, ensure_ascii=False)


async def execute_list_upload(chat_id: int) -> str:
    """List files in upload/ (staged outgoing artifacts)."""
    items = await list_upload_files(chat_id)
    return json.dumps({"files": items, "count": len(items)}, ensure_ascii=False)

# ---------- 工具分发 ----------
async def dispatch_tool_call(name: str, arguments: dict, chat_id: int, progress_callback=None) -> str:
    if chat_id is None:
        # 早期失败：避免创建 ./workspace/None 造成跨会话数据泄漏
        return json.dumps({"error": "chat_id is required for tool dispatch"})
    # Resolve workspace identity exactly once for this tool invocation.
    # Every workspace-facing operation below receives this explicit namespace, so
    # async tasks/subtasks cannot accidentally resolve a different ContextVar.
    resolved_namespace = workspace_namespace(chat_id)
    try:
        if name == "web_search":
            return await execute_web_search(
                arguments.get("query", ""),
                arguments.get("num_results"),
                arguments.get("offset"),
            )
        elif name == "fetch_url":
            # 增加重试逻辑：如果超时，重试一次
            url = arguments.get("url", "")
            for attempt in range(2):  # 最多尝试2次
                try:
                    return await execute_fetch_url(url)
                except asyncio.TimeoutError:
                    if attempt == 0:
                        logger.warning(f"fetch_url timeout, retrying (url={url})")
                        await asyncio.sleep(1)
                        continue
                    else:
                        # 重试后仍超时，返回友好消息
                        return "⏱️ 页面抓取超时，该网站可能响应较慢，建议稍后重试或手动访问。"
                except Exception as e:
                    logger.exception(f"fetch_url unexpected error: {e}")
                    return "⚠️ 页面抓取失败，请稍后重试或检查URL。"
            return "⚠️ 页面抓取失败，请稍后重试。"
        elif name == "wikipedia":
            return await execute_wikipedia(arguments.get("query", ""), arguments.get("lang", "zh"))
        elif name == "exchange_rate":
            return await execute_exchange_rate(arguments.get("base", "USD"), arguments.get("target"))
        elif name == "book_lookup":
            return await execute_book_lookup(arguments.get("query", ""))
        elif name == "weather":
            return await execute_weather(arguments.get("city", ""), arguments.get("unit", "c"),
                                         arguments.get("hours", 6))
        elif name == "news":
            return await execute_news(arguments.get("source", "bbc"), arguments.get("limit", 5))
        elif name == "crypto_price":
            return await execute_crypto_price(arguments.get("coin", ""), arguments.get("currency", "usd"))
        elif name == "ip_geo":
            return await execute_ip_geo(arguments.get("ip"))
        elif name == "qr_code":
            return await execute_qr_code(arguments.get("text", ""))
        elif name == "done":
            return await execute_done()
        elif name == "generate_image_from_text":
            return await execute_generate_image(
                prompt=arguments.get("prompt"),
                model=arguments.get("model"),
                aspect_ratio=arguments.get("aspect_ratio", "1:1"),
                image_size=arguments.get("image_size", "1K"),
                num_images=arguments.get("num_images", 1),
                image_url=None  # 强制无参考图
            )
        elif name == "edit_image_with_reference":
            return await execute_generate_image(
                prompt=arguments.get("prompt"),
                model=arguments.get("model"),
                aspect_ratio=arguments.get("aspect_ratio", "1:1"),
                image_size=arguments.get("image_size", "1K"),
                num_images=arguments.get("num_images", 1),
                image_url=arguments.get("image_url")  # 带参考图
            )
        elif name == "generate_video":
            return await execute_generate_video(
                prompt=arguments.get("prompt"),
                model=arguments.get("model"),
                duration=arguments.get("duration", 5),
                chat_id=chat_id,
            )
        # 地图工具
        elif name == "geocode":
            return await execute_geocode(arguments.get("address", ""))
        elif name == "route":
            return await execute_route(
                arguments.get("origin", ""),
                arguments.get("destination", ""),
                arguments.get("mode", "driving"),
                arguments.get("city"),
                arguments.get("cityd"),
            )
        elif name == "distance":
            return await execute_distance(
                arguments.get("origin", ""),
                arguments.get("destination", ""),
            )
        elif name == "poi_keyword_search":
            return await execute_keyword_search(
                arguments.get("keywords", ""),
                arguments.get("city"),
            )
        elif name == "poi_nearby_search":
            return await execute_nearby_search(
                arguments.get("keywords", ""),
                arguments.get("location", ""),
                arguments.get("radius"),
            )
        elif name == "poi_details":
            return await execute_poi_details(arguments.get("id", ""))
        elif name == "text_editor":
            return await execute_text_editor(
                chat_id=chat_id,
                namespace=resolved_namespace,
                command=arguments.get("command", ""),
                path=arguments.get("path", ""),
                view_range=arguments.get("view_range"),
                old_str=arguments.get("old_str"),
                new_str=arguments.get("new_str"),
                insert_line=arguments.get("insert_line"),
                insert_text=arguments.get("insert_text"),
                file_text=arguments.get("file_text"),
            )
        # ========== Bash 工具分支 ==========
        elif name == "bash":
            return await execute_bash(
                chat_id=chat_id,
                namespace=resolved_namespace,
                command=arguments.get("command", ""),
                restart=arguments.get("restart", False),
                progress_callback=progress_callback,
            )
        # ========== Todo 工具分支 ==========
        # 任务 / 待办清单。返回 JSON 字符串给 AI 上下文；UI 渲染由 format_tool_result 处理。
        elif name == "todo":
            result_str = await execute_todo(
                chat_id=chat_id,
                action=arguments.get("action", "list"),
                title=arguments.get("title"),
                todo_id=arguments.get("todo_id"),
                priority=arguments.get("priority"),
                tags=arguments.get("tags"),
                note=arguments.get("note"),
                filter=arguments.get("filter"),
                tag=arguments.get("tag"),
            )
            return result_str
        # ========== Memory 工具分支 ==========
        # 长期记忆库。返回 JSON 给 AI；UI 由 format_tool_result 渲染富文本卡片。
        elif name == "memory":
            return await execute_memory(
                chat_id=chat_id,
                action=arguments.get("action", "list"),
                content=arguments.get("content"),
                memory_id=arguments.get("memory_id"),
                category=arguments.get("category"),
                tags=arguments.get("tags"),
                importance=arguments.get("importance"),
                query=arguments.get("query"),
                scope=arguments.get("scope"),
                limit=arguments.get("limit", 50),
                source=arguments.get("source", "agent"),
            )
        # ========== Subagent 工具分支 ==========
        # 子 agent。返回 JSON（含 answer / rounds / tool_calls）给父 agent 阅读；
        # UI 由 format_tool_result 渲染成「子 agent 已完成」卡片。
        # progress_callback 让子 agent 每轮能向主 agent 的草稿推送进度，避免 90s 黑屏。
        elif name == "subagent":
            return await execute_subagent(
                chat_id=chat_id,
                task=arguments.get("task", ""),
                context=arguments.get("context"),
                model=arguments.get("model"),
                allowed_tools=arguments.get("allowed_tools"),
                timeout=arguments.get("timeout"),
                progress_callback=progress_callback,
            )
        elif name == "present_files":
            paths = arguments.get("paths", [])
            if isinstance(paths, str):
                paths = [paths]
            return await execute_present_files(chat_id, paths)
        elif name == "fetch_download":
            filenames = arguments.get("filenames", [])
            if isinstance(filenames, str):
                filenames = [filenames]
            overwrite = bool(arguments.get("overwrite", False))
            return await execute_fetch_download(chat_id, filenames, overwrite=overwrite)
        elif name == "stage_upload":
            paths = arguments.get("paths", [])
            if isinstance(paths, str):
                paths = [paths]
            return await execute_stage_upload(chat_id, paths)
        elif name == "list_download":
            return await execute_list_download(chat_id)
        elif name == "list_upload":
            return await execute_list_upload(chat_id)
        else:
            return f"失败：未知工具: {name}。"
    except asyncio.CancelledError:
        # 关键：CancelledError 必须向上传播，否则用户发新消息无法取消正在执行的工具调用，
        # agentic 循环会把取消信号当成普通工具失败吞掉，导致旧任务继续跑。
        raise
    except Exception as e:
        # 顶层异常：只记录日志，返回用户友好消息，不暴露内部细节
        logger.exception(f"dispatch_tool_call 顶层异常 [{name}]: {e}")
        return "⚠️ 工具执行出错，请稍后重试或换一种方式。"
