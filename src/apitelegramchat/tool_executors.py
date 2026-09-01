# tool_executors.py
import asyncio
import os
import subprocess
import uuid
import aiohttp
import json
import time
from pathlib import Path
from apitelegramchat.workspace_paths import (
    workspace_root, workspace_workdir, runtime_cache_root, workspace_namespace,
    workspace_upload_root, workspace_download_root,
    is_inside_upload_or_download,
    _UPLOAD_DIR_NAME,
)
import re
import html
import logging
from typing import Optional, List
from urllib.parse import urlparse
from apitelegramchat.workspace_utils import (
    _get_workspace_lock,
    _ensure_runtime_workspace,
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


# =====================================================================
# bash 结果信封（终端回放式）
# =====================================================================
def _format_bash_envelope(prompt_cwd: str, command: str, exit_code, output: str) -> str:
    """把一次 bash 执行渲染成终端回放式结果信封。

    形如::

        /abs/cwd$ <command>
        Exit code: <code>
        <output>

    设计（对齐真实终端的使用体验）：
    - 首行是 PS1 风格提示符：模型像人一样直接"看到"命令前面的当前
      目录；``cd`` 之后下一条命令的提示符随之变化。工作区在哪、现在
      在哪，由提示符天然承载，不再需要 Sandbox/Cwd/Command 等元数据
      行（工作区绝对路径与可写范围已在系统提示词和工具描述中声明，
      每轮重复只会浪费 token）。
    - ``Exit code:`` 紧跟命令行、位于输出之前。下游解析（tool_summary
      / tool_call_loop）用首个 ``Exit code: `` 匹配退出码——放在输出
      前面可保证命令输出里即使出现 "Exit code: N" 字样也不会遮蔽
      真实退出码（与旧信封同等安全，且整体更短）。
    - 输出为空时只有两行，与真实终端一致。
    """
    cmd_text = str(command or "").rstrip()
    header = f"{prompt_cwd}$ {cmd_text}\nExit code: {exit_code}"
    body = str(output or "").rstrip("\n")
    if not body:
        return header
    return f"{header}\n{body}"

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
    execute_geocode,
    execute_qr_code,
    execute_generate_image,
    execute_generate_video,
    # 地图工具（全部委托给 amap-maps MCP）
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
from apitelegramchat.token_budget import truncate_to_token_budget, truncate_to_token_budget_head_tail
# chat action 状态指示（白名单与触发位置约定见 chat_actions.py）：
# 地图工具族 → find_location；present_files 发送文件 → upload_document。
from apitelegramchat.chat_actions import chat_action_scope

logger = logging.getLogger(__name__)

# ---------- 信号量控制并发工具调用 ----------
tool_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TOOLS)

# 查找位置类工具（amap 地图族）：模型调用期间显示 find_location。
# 全部经 dispatch_tool_call 分发，含子 agent 内的同名调用。
LOCATION_LOOKUP_TOOLS = frozenset({
    "geocode",
    "route",
    "distance",
    "poi_keyword_search",
    "poi_nearby_search",
    "poi_details",
})

TOOL_RESPONSE_TOKEN_BUDGET = int(os.getenv("TOOL_RESPONSE_TOKEN_BUDGET", "20000"))

# ---------- Bash 输出上限（环境变量可调） ----------
# 单条 Bash 命令返回给模型的内容上限（字符数）。超限时不再像旧版那样
# 只保留开头 20000 字符，而是「保留开头 + 结尾、省略中间」，因为编译错误、
# traceback、日志摘要几乎总是出现在输出末尾，纯头部截断会把最有价值的
# 部分默默丢掉。设为 0 表示不限制（不建议：狂刷输出的命令会撑爆内存与
# 模型上下文）。
SANDBOX_OUTPUT_MAX_CHARS = int(os.getenv("SANDBOX_OUTPUT_MAX_CHARS", "80000"))


def _truncate_tool_result(result: str, fn_name: str | None = None) -> str:
    """Bound every model-facing tool result by an exact 20k-token budget.

    bash 结果改用「头尾保留」策略：命令输出的报错几乎总在结尾，纯头部
    截断会让模型看不到失败原因，进而盲目重试浪费请求。
    """
    if fn_name == "bash":
        return truncate_to_token_budget_head_tail(
            result,
            TOOL_RESPONSE_TOKEN_BUDGET,
        )
    return truncate_to_token_budget(
        result,
        TOOL_RESPONSE_TOKEN_BUDGET,
        suffix="\n…[内容过长，已按 token 预算截断]",
    )


class _BashOutputBuffer:
    """Bounded accumulator for subprocess output: keeps head + rolling tail.

    旧实现把全部输出无限累积进内存，再一刀切只留开头 20000 字符，存在
    两个问题：
      1. 狂刷输出的命令（`yes`、误写的热循环、`find /`）会让应用 OOM；
      2. 头部截断丢掉了几乎必然位于结尾的错误信息。
    本缓冲区用固定字符预算同时解决两者：预算内原样保留；超预算后保留
    开头 head_ratio 比例 + 滚动尾部，中间丢弃并精确计数，最终在结果里
    插入一条可读说明，让模型知道自己看到的是被裁剪过的输出。
    """

    __slots__ = ("keep", "head_ratio", "_head", "_tail", "_kept", "_dropped", "_capped", "_total")

    def __init__(self, keep_chars: int = SANDBOX_OUTPUT_MAX_CHARS, head_ratio: float = 0.7):
        # 下限 200 仅防退化输入（负数/极小值）；0 视为不限制。
        self.keep = max(200, int(keep_chars)) if keep_chars else 10**12
        self.head_ratio = min(max(head_ratio, 0.1), 0.9)
        self._head: list[str] = []
        self._tail: list[str] = []
        self._kept = 0
        self._dropped = 0
        self._total = 0
        self._capped = False

    @property
    def total_seen(self) -> int:
        """到目前为止接收到的全部字符数（含被丢弃的中间部分）。"""
        return self._total

    def add(self, text: str) -> None:
        if not text:
            return
        self._total += len(text)
        if not self._capped:
            self._head.append(text)
            self._kept += len(text)
            if self._kept > self.keep:
                self._enter_capped_mode()
            return
        self._tail.append(text)
        self._kept += len(text)
        self._trim_to_budget()

    def _enter_capped_mode(self) -> None:
        """把已累积内容切成「固定头部 + 滚动尾部」两段。"""
        self._capped = True
        whole = "".join(self._head)
        head_len = max(1, int(self.keep * self.head_ratio))
        tail_len = max(1, self.keep - head_len)
        self._head = [whole[:head_len]]
        self._dropped += len(whole) - head_len - tail_len
        self._tail = [whole[-tail_len:]] if tail_len else []
        self._kept = len(self._head[0]) + len(self._tail[0]) if self._tail else len(self._head[0])

    def _trim_to_budget(self) -> None:
        """超预算时优先消耗头部（挪入 dropped），头部耗尽后滚动丢弃最旧尾部。"""
        while self._kept > self.keep:
            if self._head:
                last = self._head[-1]
                excess = self._kept - self.keep
                if len(last) <= excess:
                    self._head.pop()
                    self._dropped += len(last)
                    self._kept -= len(last)
                else:
                    cut = len(last) - excess
                    self._head[-1] = last[:cut]
                    self._dropped += excess
                    self._kept -= excess
            elif self._tail:
                oldest = self._tail[0]
                excess = self._kept - self.keep
                if len(oldest) <= excess:
                    self._tail.pop(0)
                    self._dropped += len(oldest)
                    self._kept -= len(oldest)
                else:
                    self._tail[0] = oldest[excess:]
                    self._dropped += excess
                    self._kept -= excess
            else:
                break

    def finalize(self) -> str:
        """返回最终文本；中间被省略时插入明确说明。"""
        head = "".join(self._head)
        tail = "".join(self._tail)
        if not self._capped or self._dropped <= 0:
            return head + tail
        note = (
            f"\n... [output truncated: {self._dropped} chars omitted from the middle; "
            f"kept the first {len(head)} and the last {len(tail)} chars. "
            f"Redirect full output to a file (e.g. `cmd > out.log`) and inspect it "
            f"with grep/tail/text_editor if you need the omitted part] ...\n"
        )
        return head + note + tail


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


def _truncate_ui_lines(text: str, max_lines: int = _TOOL_UI_MAX_LINES) -> str:
    """Keep only the first max_lines lines; append a truncation note if cut."""
    text = text if isinstance(text, str) else str(text or "")
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = "\n".join(lines[:max_lines])
    return f"{kept}\n…（已截断，共 {len(lines)} 行，仅显示前 {max_lines} 行）"


def _truncate_ui_lines_head_tail(text: str, max_lines: int = _TOOL_UI_MAX_LINES) -> str:
    """Keep the first ~60% and the last ~40% lines; note the omitted middle.

    Bash 输出的报错/摘要几乎总在结尾，纯头部截断会让用户在卡片里看不到
    失败原因；这里保头也保尾，中间以说明行代替。
    """
    text = text if isinstance(text, str) else str(text or "")
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head_lines = max(1, int(max_lines * 0.6))
    tail_lines = max(1, max_lines - head_lines - 1)  # 预留 1 行给省略说明
    omitted = len(lines) - head_lines - tail_lines
    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    return f"{head}\n…（已截断，共 {len(lines)} 行，省略中间 {omitted} 行）\n{tail}"


def _render_editor_quote(label: str, value: str, truncator=_truncate_ui_lines) -> str:
    """Render a tool's input or output as a plain quoted text block, truncated to _TOOL_UI_MAX_LINES lines."""
    text = value if isinstance(value, str) else str(value or "")
    if not text:
        text = "(empty)"
    else:
        text = truncator(text)
    quoted_text = escape_html(text).replace("\n", "<br/>")
    return f"<p><b>{escape_html(label)}</b></p><blockquote>{quoted_text}</blockquote>"


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
            img_tags = "".join(f'<img src="{url}"/>' for url in urls)
            link_items = "".join(
                f'<li><a href="{url}">图片 {index + 1}</a></li>'
                for index, url in enumerate(urls)
            )
            caption = f"{operation_zh} {count} 张图片：<ul>{link_items}</ul>"
            if count == 1:
                details_html = f"<figure>{img_tags}<figcaption>{caption}</figcaption></figure>"
            else:
                details_html = f"<tg-slideshow>{img_tags}<figcaption>{caption}</figcaption></tg-slideshow>"
            return summary, details_html
    return failure_summary, _render_media_failure_result(result_str, failure_fallback)


def _parse_bash_envelope(result_str: str):
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
        # start() 可能从两条锁路径被调用（execute 的 workspace 锁内、
        # manager 的全局锁内），两把锁互不互斥；每实例锁串行化 spawn，
        # 防止并发双开 bash 导致先 spawn 的进程泄漏、新进程无看门狗。
        self._start_lock = asyncio.Lock()
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
        # persistent shell 自身的 cwd（隔离/heredoc 执行不回写该值）。
        # 结果信封的提示符用它：保证模型看到的 ``path$`` 永远是命令真正
        # 运行的目录，不会被子 shell 的 cd 污染。
        self._persistent_cwd: Optional[str] = str(self.workdir.absolute())

    async def start(self):
        """启动 bash 进程，套上 Landlock 沙箱 + rlimit + no-new-privs"""
        async with self._start_lock:
            return await self._start_locked()

    async def _start_locked(self):
        """start() 的实际实现；调用方必须已持有 self._start_lock。"""
        if self.proc is not None and self.proc.returncode is None:
            return

        # workspace 目录权限 700，防跨 chat 读取
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workspace, 0o700)
        os.chmod(self.workdir, 0o700)
        # ★ 显式预创建 upload/ 和 download/：bash 进程一启动，cwd 就是
        # workspace root，模型几乎立刻会跑 `cp out.txt upload/out.txt`
        # 或 `cat download/x.pdf`。如果不在这里预创建，bash 进程已经
        # 在跑、第一次 execute() 时才补创建，会出两个问题：
        #   1) 如果 execute() 里 _ensure_runtime_workspace 抛异常被
        #      try/except 吞掉，目录就永远不存在，cp 第一次必然失败，
        #      模型不得不多跑一轮 `mkdir -p upload && cp ...` 才能补救；
        #   2) _ensure_runtime_workspace(self.chat_id) 没传 namespace，
        #      依赖 ContextVar；如果 bash 工具从 background task 里
        #      调用、ContextVar 不可见，upload/ 会被建到错误的 namespace
        #      下，bash 进程实际看到的 cwd 下仍然没有 upload/。
        # 用 self.namespace 直接走 workspace_upload_root /
        # workspace_download_root，确保和 bash 进程的 cwd 完全一致。
        workspace_upload_root(self.chat_id, self.namespace)
        workspace_download_root(self.chat_id, self.namespace)

        # 新进程的 cwd 必然是 workdir；重置 _last_cwd，避免上一次会话
        # 残留的 cwd 状态误拒下一条命令。
        self._last_cwd = str(self.workdir.absolute())
        self._persistent_cwd = str(self.workdir.absolute())

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

        # 启动看门狗（fork bomb 防护）。无条件跟随本次 spawn 的进程：
        # 旧实现"看门狗还活着就不重建"会让重启后的新进程处于无防护状态
        # （旧看门狗盯的是已死的旧进程对象）。
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = asyncio.create_task(
            watchdog(self.proc), name=f"watchdog-{self.chat_id}"
        )


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

    @staticmethod
    async def _is_unterminated(command: str) -> bool:
        """Detect commands bash would keep waiting on (unclosed heredoc,
        quote, backtick, paren, etc.) using `bash -n` as ground truth.

        A persistent stdin-backed shell deadlocks whenever the model emits
        any syntactically incomplete command — not just heredocs. An
        unterminated `"..."` or `'...'` string is just as fatal: the shell
        keeps reading stdin waiting for the closing quote, and our synthetic
        end-marker line is silently swallowed as part of that string instead
        of being executed. `bash -n` performs a pure syntax check (no
        execution) and reports "unexpected EOF while looking for matching"
        for exactly this class of problem, so it is a much more reliable
        signal than trying to enumerate every unterminated-token regex by
        hand (heredocs, quotes, backticks, `$(`, `((`, `{`, ...).
        """
        try:
            # 强制使用 C locale：bash 在 zh_CN.UTF-8 / ja_JP.UTF-8 等环境下
            # 会输出本地化错误信息（"未预期的文件结束符"），从而让下面
            # 的英文子串匹配（"unexpected EOF"）彻底失效，导致本应被路由
            # 到隔离执行的危险命令直接进入持久 shell，触发 300s 卡死。
            import os as _os
            env = {
                "LC_ALL": "C.UTF-8",
                "LANG": "C.UTF-8",
                "PATH": _os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            }
            proc = await asyncio.create_subprocess_exec(
                "bash", "-n", "-c", command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                # Can't prove it's safe; route to isolated execution to be safe.
                return True
            if proc.returncode != 0:
                msg = (stderr or b"").decode("utf-8", errors="replace")
                # 仅 "unexpected EOF" / 未终止 token 错误才说明持久 shell 会
                # 卡住；其他语法错误（如拼写错误）可以由持久 shell 正常报错，
                # 因为它们不会吞掉 end marker。
                if "unexpected EOF" in msg or "unexpected end of file" in msg:
                    return True
            return False
        except Exception:
            # If we can't run the syntax check at all, don't block execution —
            # fall through to the existing heredoc regex as a safety net.
            logger.debug("_is_unterminated 内部忽略的异常", exc_info=True)
            return False

    async def _execute_heredoc_isolated(self, command: str, timeout: int) -> str:
        """Execute heredoc-heavy (or otherwise syntactically risky) commands
        in a one-shot bash process.

        A persistent stdin-backed shell can deadlock when a model emits an
        incomplete heredoc or unterminated quote: the shell keeps waiting for
        the terminator, while our synthetic end marker is consumed as input
        to that still-open construct.  A one-shot `bash -c` receives an
        actual EOF at the end of `command`, so malformed input terminates
        with a shell error instead of hanging the session.
        """
        workspace = self.workspace
        cwd = self._last_cwd or str(self.workdir.absolute())
        env = build_sandbox_env(self.workspace, self.chat_id, self.namespace)
        import functools
        preexec = functools.partial(_preexec_sandbox, str(workspace.absolute()))

        marker = f"__ONE_SHOT_END_{uuid.uuid4().hex[:8]}__"
        full_cmd = command.rstrip() + f"\nprintf '{marker} %s\n' \"$?\"\nprintf '__ONE_SHOT_CWD__ %s\n' \"$PWD\"\n"

        # 与持久会话（sandbox.py 的 /bin/bash --noprofile --norc）保持一致：
        # 登录 shell（-l）会 source /etc/profile 重置 PATH，可能让 runtime_bin
        # 里的 curl/wget shim 失效，且两条执行路径语义分叉。
        proc = await asyncio.create_subprocess_exec(
            "bash", "--noprofile", "--norc", "-c", full_cmd,
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

        output_buffer = _BashOutputBuffer()

        try:
            while True:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=timeout)
                if not chunk:
                    break
                output_buffer.add(chunk.decode("utf-8", errors="replace"))
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
                logger.debug("_execute_heredoc_isolated 内部忽略的异常", exc_info=True)
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
                logger.debug("_execute_heredoc_isolated 内部忽略的异常", exc_info=True)
                pass
            raise

        await proc.wait()
        output = output_buffer.finalize()
        exit_code = proc.returncode if proc.returncode is not None else "unknown"
        marker_match = re.search(rf"(?m)^{re.escape(marker)}\s+(-?\d+)\s*$", output)
        if marker_match:
            exit_code = marker_match.group(1)
            output = re.sub(rf"(?m)^{re.escape(marker)}\s+-?\d+\s*$\n?", "", output)
        cwd_match = re.search(r"(?m)^__ONE_SHOT_CWD__\s+(.+)$", output)
        actual_cwd = cwd_match.group(1).strip() if cwd_match else cwd
        output = re.sub(r"(?m)^__ONE_SHOT_CWD__\s+.*$\n?", "", output)
        self._last_cwd = actual_cwd
        output = re.sub(r'\x1b\[[0-9;]*m', '', output)
        # 终端式信封：提示符 = 本次隔离进程的起始 cwd（命令的真实执行
        # 位置）。隔离命令里的 cd 不会影响 persistent shell，下一条命令
        # 的提示符会如实回到 persistent cwd——和真实终端的子 shell 语义
        # 一致，模型看提示符即可自行推断。
        return _format_bash_envelope(cwd, command, exit_code, output)

    # ===================== 执行命令 =====================
    async def execute(self, command: str, timeout: int = SANDBOX_TIMEOUT_SEC) -> str:
        """在沙箱中执行 bash 命令，超时自动终止

        v2.3：不再接受 ``progress_callback``——bash 工具执行期间不推送
        任何进度预览。原始 stdout 对用户价值有限（多为命令日志），
        频繁刷新草稿只换来视觉抖动 + Telegram API 限流压力。
        卡片摘要由 ``tool_call_loop`` 用 ``_generate_initial_tool_summary``
        生成的命令片段保持不变；最终结果由 ``update_tool_item``
        一次性写入包含 Input/Output 块级结构的完整卡片。
        """
        # ★ init 在 workspace lock 外面执行：R2 网络同步可能耗时数秒，
        #   不应阻塞其他工具调用获取 workspace lock。init 只需要 init_lock
        #   （在 _ensure_workspace_initialized 内部获取），与 workspace lock 独立。
        #   init 失败不阻断 bash：本地 workspace 可能不全但 bash 仍可运行。
        # ★ 显式传 self.namespace：避免依赖 ContextVar 在 background task
        #   里不可见时把 upload/download 建到错误的 namespace 下。
        #   start() 已经预创建过这两棵子树，这里只是兜底——任何路径下
        #   失败都不会让 cp 报 "No such file or directory"，因为 start()
        #   时目录已经存在。
        try:
            await _ensure_runtime_workspace(self.chat_id, self.namespace)
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
                        "Error: Command rejected — `cd` into upload/ or download/ is "
                        "not allowed. These directories are data buffers directly inside "
                        "your workspace root: read and write files in them via relative "
                        "paths from your workdir (e.g. `cp out.txt upload/out.txt`, "
                        "`cat download/doc.pdf`), but never execute commands from "
                        "inside them."
                    )
                return f"Error: Command rejected for security reasons: {command}"

            # Any command containing a heredoc, OR any command bash would
            # consider syntactically unterminated (unclosed quote/backtick/
            # paren — e.g. a truncated `python3 -c "..."` multi-line string),
            # is executed in a one-shot shell instead of the persistent one.
            # A persistent stdin-backed shell blocks forever on unterminated
            # input and silently consumes our synthetic end marker as part of
            # it, which is what previously caused ~300s hangs before the
            # sandbox timeout kicked in and force-restarted the session.
            has_heredoc = bool(re.search(r"<<-?\s*(?:[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?)", command))
            if has_heredoc or await self._is_unterminated(command):
                return await self._execute_heredoc_isolated(
                    command, timeout=timeout
                )

            tag = uuid.uuid4().hex[:8]
            marker = f"__END_{tag}__"
            cwd_marker = f"__CWD_{tag}__"
            # 信封提示符取 persistent shell 当前 cwd（命令的真实执行位置）。
            # 隔离执行（heredoc）不回写 _persistent_cwd，因此提示符不会
            # 被子 shell 的 cd 污染，永远真实。
            prompt_cwd = self._persistent_cwd or str(self.workdir.absolute())
            # 默认 shell 启动目录为 workspace/workspace root。模型决定使用 skill 后，
            # 可自行 `cd skills/<skill_id>`；persistent bash 会保留该 cwd。
            # ★ 关键：在输出 marker 前先输出一个换行，确保 marker 单独占一行。
            #   如果命令输出不以换行结尾（如 cat 无换行文件、printf 无 \n），
            #   echo 的输出会粘在前一行，readline() 永远读不到以 marker 开头的行，
            #   导致整个会话 hang 死。
            # 同时记录命令结束后的真实 PWD，用于结果显示；不会改变 shell 状态。
            # ★ 修复存量 bug ×3：
            #   1) $? 必须放在引号外（旧写法 echo '{marker} $?' 把 $?
            #      包进单引号，bash 不展开，退出码永远是 unknown）；
            #   2) 退出码必须在命令结束的下一刻立刻捕获（__rc=$?），
            #      否则中间的 echo/printf 会把 $? 重置为 0，失败命令
            #      在模型眼里和成功无异；
            #   3) 模型生成的多行命令（如 `python3 -c "..."` 跨行书写）
            #      几乎总带尾随换行。旧写法 f"{command}; __rc=$?..." 直接拼接
            #      会让 "; __rc=$?" 落在新一行的行首——bash 对行首的孤立
            #      分号直接报 syntax error near unexpected token `;'——
            #      marker 永远不会被输出，退出码停留在 unknown。
            #      修复：先 rstrip() 去掉尾随空白/换行，再改用 "\n" 换行拼接
            #      退出码捕获。换行在 bash 里同样是命令分隔符，且能正确
            #      终结行尾注释（`cmd # note` 后直接拼 `;` 会把整段包装
            #      代码吞进注释，导致 marker 丢失、会话卡到超时）。
            #   另：每次执行使用带唯一后缀的退出码变量名（__rc_<tag>）。
            #   持久 shell 里同名变量会在多次 execute() 之间残留，若某次
            #   赋值被跳过（如命令以反斜杠续行符结尾时，`__rc=$?` 会被
            #   join 进上一条命令的参数里），echo 会读到上一次的陈旧退出码，
            #   把失败伪装成成功。唯一变量名保证最坏情况是 "unknown"
            #   而不是错误的旧值。
            rc_var = f"__rc_{tag}"
            cmd_body = command.rstrip()
            full_cmd = (
                f"{cmd_body}\n{rc_var}=$?; echo; printf '{cwd_marker} %s\n' \"$PWD\"; "
                f"echo '{marker}' \"${rc_var}\"\n"
            )

            try:
                self.proc.stdin.write(full_cmd.encode('utf-8'))
                await self.proc.stdin.drain()

                output_buffer = _BashOutputBuffer()
                exit_code = "unknown"

                async def read_until_marker():
                    nonlocal exit_code
                    # marker 可能跨 chunk 被拆开，因此只保留一个很小的尾部用于跨 chunk 匹配；
                    # 已经确定不可能包含 marker 的前缀立即写入输出缓冲，避免每次都 O(n) 拼接。
                    pending = ""
                    keep_tail = len(marker) + 64
                    while True:
                        chunk = await self.proc.stdout.read(4096)
                        if not chunk:
                            if pending:
                                output_buffer.add(pending)
                                pending = ""
                            break

                        pending += chunk.decode('utf-8', errors='replace')
                        marker_pos = pending.find(marker)
                        if marker_pos >= 0:
                            # marker 前是命令真实输出；后面紧接着是 echo 的退出码。
                            if marker_pos:
                                output_buffer.add(pending[:marker_pos])
                            marker_tail = pending[marker_pos:]
                            match = re.search(rf"{re.escape(marker)}\s+(-?\d+)", marker_tail)
                            if match:
                                exit_code = match.group(1)
                            pending = ""
                            break

                        if len(pending) > keep_tail:
                            output_buffer.add(pending[:-keep_tail])
                            pending = pending[-keep_tail:]

                await asyncio.wait_for(read_until_marker(), timeout=timeout)

                # 有界缓冲：预算内完整保留；超预算保留头+尾并省略中间，
                # 上限由 SANDBOX_OUTPUT_MAX_CHARS 控制（默认 80000，远大于旧版 20000）。
                output = output_buffer.finalize()
                output = re.sub(r'\x1b\[[0-9;]*m', '', output)

                # 提取命令结束后的真实 PWD，同时把内部 marker 从用户输出中移除。
                actual_cwd = str(self.workdir.absolute())
                cwd_match = re.search(r'(?m)^' + re.escape(cwd_marker) + r' (.+)$', output)
                if cwd_match:
                    actual_cwd = cwd_match.group(1).strip()
                    output = re.sub(r'(?m)^' + re.escape(cwd_marker) + r' .*$\n?', '', output)

                # 记录最新 cwd，下一次 _is_safe 会据此拒绝在 upload/ 或 download/
                # 子树内继续执行命令。即便 cd 进入被拒，模型也可能通过 pushd /
                # 子 shell 等方式间接进入，这里再做一次防御性检查。
                self._last_cwd = actual_cwd
                self._persistent_cwd = actual_cwd

                # 合并后台同步；不会为每次 Bash 创建一个新的全量上传任务。

                # 终端式信封：模型像人看终端一样，从提示符直接读出命令运行
                # 目录；cd 之后下一条命令的提示符随之变化。
                return _format_bash_envelope(prompt_cwd, command, exit_code, output)

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
            return
        self.proc = None

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
            return "Bash session restarted (sandbox=landlock)"

    async def cleanup_all(self):
        """优雅关闭所有会话（应用退出时调用）"""
        async with self._lock:
            for s in self._sessions.values():
                try:
                    await s.close()
                except Exception:
                    logger.debug("cleanup_all 内部忽略的异常", exc_info=True)
                    pass
            self._sessions.clear()

_bash_manager = BashSessionManager()

# =====================================================================
# execute_bash —— 工具调用入口
# v2.3：移除 ``progress_callback`` 参数。bash 执行期间不再推送任何进度
# 预览（卡片摘要保持命令片段，最终结果由 update_tool_item 一次性写入
# 包含 Input/Output 块级结构的完整卡片）。
# =====================================================================
async def execute_bash(
    chat_id: int,
    command: str = "",
    restart: bool = False,
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
    return await session.execute(command)

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
}

# ---------- web_search 结果解析与渲染 ----------
# 实现拆到 apitelegramchat.ai.web_search_render，避免在 tool_executors
# 里维护大段正则与渲染函数；这里只暴露 _format_web_search_result 给
# format_tool_result 调用，保持调用点零改动。
from apitelegramchat.ai.web_search_render import (
    format_web_search_result as _format_web_search_result,
)


async def format_tool_result(fn_name: str, fn_args: dict, result_str: str) -> tuple[str, str]:
    # 历史上这里曾重复定义一个本地 escape_text（与 import 的 escape_html
    # 行为不完全一致：本地版会对已经合法的实体再做一次 `&` -> `&amp;` 转换，
    # 导致双重转义），是容易飘移的脏代码。现已删除本地定义，全部走
    # utils.escape_html（它做了智能 ampersand 处理，不会重复转义）。
    # ---- Intercept timeout magic marker BEFORE any other branch ----
    # The raw exception (with TOOL_CALL_TIMEOUT seconds) is kept in
    # logger.error on the backend; the UI only sees the friendly version.
    if result_str == _TOOL_TIMEOUT_MARKER:
        label = _TOOL_TIMEOUT_LABELS.get(fn_name, fn_name)
        summary = f"⏱️ {label} timed out"
        timeout_message = "Execution exceeded the timeout limit. Please refine your request or try again later."
        if fn_name in {"generate_image_from_text", "edit_image_with_reference", "generate_video"}:
            details_html = _render_media_failure_result(timeout_message, timeout_message)
        else:
            details_html = timeout_message
        return summary, details_html

    if fn_name == "web_search":
        return _format_web_search_result(fn_args, result_str)

    elif fn_name == "fetch_url":
        url = fn_args.get('url', '')
        domain = extract_domain(url)
        text = str(result_str or "")
        stripped = text.lstrip()
        # 新版 fetch_url 成功结果本身就是面向模型的 Telegram Rich HTML；正文
        # 文本里也可能出现"失败"字样，因此失败判断只看前缀，避免把谈论"失败"
        # 的新闻正文误判为抓取失败。
        if (stripped.startswith(("失败", "❌"))
                or stripped.lower().startswith(("error", "failed", "timeout", "exception"))
                or "超时" in stripped[:30]):
            logger.error(f"[fetch_url] Failed to fetch {url}: {text[:500]}")
            summary = f"🌐 Failed to fetch {domain}"
            details_html = "Unable to retrieve content. Check the URL or try again later."
        else:
            # 展示保持历史样式：仅标题 + 来源域名链接。富 HTML 是给模型看的，
            # 不在 Telegram 工具折叠面板中渲染（避免长消息 + 重复内容）。
            title = domain
            m = re.search(r'<h3[^>]*>(.*?)</h3>', text, re.S | re.I)
            if m:
                # <h3> 内容是已转义的 HTML 文本（&amp; 等），原样嵌入合法。
                title = re.sub(r'<[^>]+>', '', m.group(1)).strip() or domain
            else:
                m = re.search(r'🏷️\s+([^\n]+)', text)
                if m:
                    title = m.group(1).strip()
            summary = f"🌐 Fetched: {title}"
            details_html = f"{title} <a href=\"{url}\">{domain}</a>"
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
            safe_log = escape_html(result_str[:60000])
            summary = "🌤️ 天气数据"
            details_html = f"<pre><code>{safe_log}</code></pre>"
            return summary, details_html

    elif fn_name == "wikipedia":
        query = fn_args.get('query', '')
        lang = fn_args.get('lang', 'zh')
        import urllib.parse
        text = result_str.strip()
        # 标题：富 HTML 结果取首个 <h3>；退化（纯文本摘要）取 <b>Wikipedia — 标题</b>。
        title = None
        m = re.search(r"<h3[^>]*>(.*?)</h3>", text, re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if not title:
            m = re.search(r"<b>Wikipedia\s*[—-]\s*(.+?)</b>", text, re.S)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()
        if not title:
            title = query
        # 来源链接：优先结果中的真实 URL——关键词解析出的页面标题
        # 可能与 query 不同（如搜"可塑性记忆"命中"可塑性記憶"），
        # 猜测 URL 会 404。富 HTML 里是 <a href>；退化格式里是纯文本。
        m = re.search(r'<a href="(https://[^"]*wikipedia\.org[^"]*)"', text)
        if m:
            wiki_url = m.group(1)
        else:
            m = re.search(r"https://[^\s<>\"']+wikipedia\.org[^\s<>\"']*", text)
            wiki_url = m.group(0) if m else f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(query)}"
        summary = f"📚 {escape_html(title)}"
        details_html = f'<a href="{wiki_url}">{escape_html(title)}</a>'
        return summary, details_html

    elif fn_name == "exchange_rate":
        base = fn_args.get('base', 'USD')
        summary = f"💱 {escape_html(base)} 汇率"
        # result_str 可能是成功 HTML，也可能是以 "失败：" 开头的错误文本。
        # 后者含上游错误消息，需要 escape 以免打坏 Telegram 渲染。
        details_html = result_str if not result_str.startswith("失败：") else escape_html(result_str)
        return summary, details_html

    elif fn_name == "book_lookup":
        query = fn_args.get('query', '')
        summary = f"📖 {escape_html(query)}"
        details_html = result_str if not result_str.startswith("失败：") else escape_html(result_str)
        return summary, details_html

    elif fn_name == "news":
        source = fn_args.get('source', 'news')
        summary = f"📰 {escape_html(source.upper())} 新闻"
        details_html = result_str if not result_str.startswith("失败：") else escape_html(result_str)
        return summary, details_html

    elif fn_name == "crypto_price":
        coin = fn_args.get('coin', '')
        summary = f"💰 {escape_html(coin.upper())} 价格"
        details_html = result_str if not result_str.startswith("失败：") else escape_html(result_str)
        return summary, details_html

    elif fn_name == "qr_code":
        if "✅ 二维码生成成功" in result_str:
            img_match = re.search(r'图片链接：([^\s]+)', result_str)
            content_match = re.search(r'内容：([^\n]+)', result_str)
            if img_match:
                img_url = img_match.group(1)
                content_text = content_match.group(1) if content_match else "已编码内容"
                summary = "📱 二维码已生成"
                details_html = (
                    f'<img src="{img_url}"/><br/>'
                    f'<b>✅ 二维码生成成功</b><br/>'
                    f'<b>内容：</b>{escape_html(content_text)}<br/>'
                    f'<b>链接：</b><a href="{img_url}">📷 点击查看 / 下载二维码</a>'
                )
                return summary, details_html
        summary = "📱 二维码"
        details_html = escape_html(result_str)
        return summary, details_html

    elif fn_name == "generate_image_from_text":
        return _format_image_generation_result(
            result_str,
            operation_en="Generated",
            operation_zh="已生成",
            failure_summary="🎨 图片生成失败",
            failure_fallback="图片生成未完成，请稍后重试。",
        )

    elif fn_name == "edit_image_with_reference":
        return _format_image_generation_result(
            result_str,
            operation_en="Edited",
            operation_zh="已编辑",
            failure_summary="🎨 图片编辑失败",
            failure_fallback="图片编辑未完成，请稍后重试。",
        )

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
                    f'<figure><video src="{video_url}"></video>'
                    f'<figcaption><a href="{video_url}">下载 / 查看视频</a></figcaption>'
                    f'</figure>'
                )
                return summary, details_html
        summary = "🎬 视频生成失败"
        details_html = _render_media_failure_result(result_str, "视频生成未完成，请稍后重试。")
        return summary, details_html

    # ===================== 地图工具（amap-maps MCP 直通） =====================
    # 所有地理 / 路径 / POI / 距离 / IP 工具现在都委托给 amap-maps MCP 服务，
    # 返回内容是该 MCP 的原生输出（通常是 JSON 文本）。这里不再尝试解析特定
    # schema（旧 amap_integration.py / Overpass / OSRM / TomTom / ORS 的字段
    # 都已废弃），改为：
    #   - 若解析出 JSON 且含 status=error，则显示为失败
    #   - 否则把原始输出转义后直接展示给用户，让 LLM 在后续轮次里自由解读。
    elif fn_name in ("geocode", "route", "distance", "poi_keyword_search",
                     "poi_nearby_search", "poi_details"):
        label_map = {
            "geocode":            "📍 地理编码",
            "route":              "🚗 路线规划",
            "distance":           "📏 距离测量",
            "poi_keyword_search": "📍 POI 关键词搜索",
            "poi_nearby_search": "📍 POI 周边搜索",
            "poi_details":        "📍 POI 详情",
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
            details_html = escape_html(str(message))
            return summary, details_html

        summary = base_label
        # 外部 MCP 常返回 JSON 文本。对聊天界面渲染结构化卡片，而向模型仍保留原始结果。
        details_html = _render_structured_payload(result_str, map_tool=fn_name) or _render_code_panel("服务响应 · 最近 10 行", result_str)
        return summary, details_html

    elif fn_name == "text_editor":
        command = fn_args.get("command", "")
        path = fn_args.get("path", "")
        # 工具自身错误总是以 "Error" 开头；view 返回的是文件内容，
        # 内容里出现 "Error:"（如查看日志文件）不代表操作失败。
        if (result_str or "").startswith("Error"):
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
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "📋 待办操作"
            details_html = escape_html(result_str)
            return summary, details_html

        if not payload.get("ok"):
            summary = f"❌ 待办操作失败：{payload.get('code', '')}"
            details_html = f"<p>{escape_html(payload.get('error', '未知错误'))}</p>"
            return summary, details_html

        action = payload.get("action", "list")
        if action == "list":
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
            details_html = escape_html(result_str)
            return summary, details_html
        if not payload.get("ok"):
            summary = f"❌ 记忆操作失败：{payload.get('code', '')}"
            details_html = f"<p>{escape_html(payload.get('error', '未知错误'))}</p>"
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
            details_html = escape_html(result_str)
            return summary, details_html
        ok = payload.get("ok", False)
        model_name = payload.get("model_name") or payload.get("model") or "?"
        rounds = payload.get("rounds", 0)
        tool_calls = payload.get("tool_calls", 0)
        elapsed = payload.get("elapsed", 0)
        if ok:
            summary = f"🤖 {model_name} · {rounds} 轮 · {tool_calls} 工具 · {elapsed:.1f}s"
        else:
            err = payload.get("error", "未知错误")
            summary = f"❌ {model_name} 失败 · {rounds} 轮 · {err[:40]}"
        details_html = render_subagent_card(payload)
        return summary, details_html

    # ===================== Bash 工具格式化 =====================
    elif fn_name == "bash":
        # 优先展示模型提供的意图描述（_description/_summary），让用户一眼
        # 看到命令目的；未提供时退化为命令首行摘要。意图文本直接原样展示、
        # 不加符号，与进行时摘要（tool_summary._generate_initial_tool_summary、
        # rich_message_builder._refresh_outer_summary 的 custom_desc 规范）一致，
        # 保证执行中与完成后摘要一致、不闪烁变化。
        # 延迟导入：tool_summary 模块级导入了 tool_executors，顶层导入会循环。
        from apitelegramchat.ai.tool_summary import _get_tool_description_from_args
        intent = _get_tool_description_from_args(fn_args) or ""
        # 用信封里的退出码判定失败。旧实现对输出内容做 "Error:" 子串匹配,
        # `grep Error: app.log` 这类命令（exit 0）会被误标为执行失败。
        parsed_env = _parse_bash_envelope(result_str)
        bash_failed = (
            (parsed_env is not None and parsed_env[1] not in ("", "0"))
            or (parsed_env is None and ("Error:" in result_str or "Command rejected" in result_str))
        )
        if bash_failed:
            summary = "❌ Bash 执行失败"
        elif intent:
            summary = intent
        else:
            # 优先从工具调用参数取原始命令：多行命令从结果信封逐行解析
            # 只能拿到第一行，摘要会退化成 `python3 -c "` 这样的残句。
            args_command = ""
            if isinstance(fn_args, dict):
                raw = fn_args.get("command")
                if isinstance(raw, str):
                    args_command = raw
            cmd_line = args_command.strip() or _extract_bash_command_from_envelope(result_str)
            cmd_line = cmd_line.splitlines()[0].strip() if cmd_line else ""
            if len(cmd_line) > 30:
                cmd_line = cmd_line[:30] + "…"
            summary = f"🖥 {cmd_line or '命令已完成'}"
        # 保留命令元信息（Input 只展示原始命令，意图由上方摘要行单独呈现；
        # Output 保头保尾），避免长输出撑爆工具卡片的同时让用户始终能看到结尾的报错。
        details_html = _render_bash_result(result_str, fn_args=fn_args)
        return summary, details_html

    elif fn_name == "present_files":
        # ---- Decoupled data abstraction ----
        # execute_present_files returns a JSON payload:
        #   {"sent": [...], "failed": [...]}   (+ "error": str only on early failure)
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
            details_html = escape_html(result_str) or "<i>No files were processed.</i>"
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
        if sent_count == 0:
            summary = "📂 No files sent"
        elif sent_count == 1:
            summary = "📂 Presented 1 file"
        else:
            summary = f"📂 Presented {sent_count} files"

        # ---- Details: HTML list of successes and failures ----
        details_parts: List[str] = []
        if sent:
            items = "".join(f"<li>{escape_html(str(f))}</li>" for f in sent)
            label = "file" if sent_count == 1 else "files"
            details_parts.append(f"<b>✅ Sent ({sent_count} {label})</b><ul>{items}</ul>")
        if failed:
            items = "".join(f"<li>{escape_html(str(f))}</li>" for f in failed)
            label = "file" if failed_count == 1 else "files"
            details_parts.append(f"<b>❌ Failed ({failed_count} {label})</b><ul>{items}</ul>")
        if error:
            details_parts.append(f"<i>{escape_html(str(error))}</i>")

        if not details_parts:
            details_parts.append("<i>No files were processed.</i>")

        details_html = "<br/>".join(details_parts)
        return summary, details_html
    else:
        summary = f"🔧 {fn_name}"
        details_html = escape_html(result_str)
        return summary, details_html


async def execute_present_files(chat_id: int, paths: List[str], namespace: str | None = None) -> str:
    """Send staged files from the workspace to the chat as attachments.

    Paths are workspace-relative. Files MUST be staged under ``upload/``
    first, for example ``cp out.txt upload/out.txt``; the corresponding call
    is ``present_files([\"upload/out.txt\"])``. Absolute paths are accepted
    only when they resolve inside this chat's workspace. The final resolved
    file must remain inside ``upload/``. This keeps Bash, file tools, and file
    presentation in one path namespace.
    """
    if not paths:
        return json.dumps({
            "sent": [],
            "failed": [],
            "error": "No paths provided. Files must be staged under upload/ first.",
        })
    # ★ init 在 workspace lock 外面执行（同 bash / text_editor）。
    # 显式接收 namespace：与 bash/text_editor 一致，避免依赖 ContextVar
    # 在 background task 里解析到错误的 namespace。
    await _ensure_runtime_workspace(chat_id, namespace)

    lock = await _get_workspace_lock(chat_id)
    async with lock:
        upload_root = workspace_upload_root(chat_id, namespace)
        sent = []
        failed = []
        # 文件大小上限：50MB，防止 OOM
        _MAX_PRESENT_FILE_SIZE = 50 * 1024 * 1024
        # 提升：把 aiohttp session 提升到循环外层，避免每个文件都做一次
        # TLS 握手。同时若异常 str(e) 里包含了带 TELEGRAM_BOT_TOKEN 的 URL
        # （BASE_URL 里嵌了 bot token），截断 + 脱敏后再写入 failed 列表，
        # 否则这个 list 会被 LLM 看到从而泄露 token。
        timeout = aiohttp.ClientTimeout(total=60)
        # 在循环外解析一次 upload_root，避免每个文件都重新 resolve。
        try:
            upload_resolved = upload_root.resolve()
        except Exception:
            logger.debug("execute_present_files 内部忽略的异常", exc_info=True)
            upload_resolved = upload_root
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for path in paths:
                if not isinstance(path, str) or not path:
                    failed.append(f"{path} (invalid path)")
                    continue
                # 拒绝嵌入的 null 字节
                if "\x00" in path:
                    failed.append(f"{path} (invalid path)")
                    continue

                # ----- 统一 workspace-relative 路径解析 -----
                # 所有相对路径都相对于唯一 workspace 根目录解析；不再把
                # present_files 的参数解释成相对于 upload/ 的第二套命名空间。
                raw_path = path.strip()
                while raw_path.startswith("./"):
                    raw_path = raw_path[2:]
                workspace = workspace_workdir(chat_id, namespace).resolve()
                try:
                    if os.path.isabs(raw_path):
                        candidate = Path(raw_path).expanduser()
                        display_path = str(candidate.resolve().relative_to(workspace))
                    else:
                        norm = os.path.normpath(raw_path)
                        if norm in ("", ".") or norm == ".." or norm.startswith(".." + os.sep):
                            raise ValueError("path escapes workspace")
                        display_path = norm
                        candidate = workspace / norm
                    resolved = candidate.resolve()
                except (OSError, ValueError):
                    failed.append(f"{path} (invalid workspace-relative path)")
                    continue
                if resolved != upload_resolved and upload_resolved not in resolved.parents:
                    failed.append(
                        f"{path} (not staged: workspace-relative path must be under "
                        f"upload/, for example upload/{Path(display_path).name})"
                    )
                    continue

                if not resolved.is_file():
                    failed.append(
                        f"{path} (file not found at workspace path {display_path!r}; "
                        f"stage it from workspace root with `cp {display_path} "
                        f"upload/{Path(display_path).name}` and call present_files with "
                        f"the workspace-relative path `upload/{Path(display_path).name}`)"
                    )
                    continue
                try:
                    file_size = resolved.stat().st_size
                    if file_size > _MAX_PRESENT_FILE_SIZE:
                        failed.append(f"{path} (file too large: {file_size} bytes)")
                        continue
                    # 使用 asyncio.to_thread 包装同步 read，避免阻塞事件循环
                    file_data = await asyncio.to_thread(resolved.read_bytes)
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(chat_id))
                    form.add_field("document", file_data, filename=resolved.name)
                    # chat action：bot 正在发送文件（sendDocument）。每个文件的
                    # 上传期间显示 upload_document；多文件连续发送时引用计数
                    # 叠加，指示无断档；上传超 5 秒由 4 秒循环重发保活。
                    async with chat_action_scope(chat_id, "upload_document"):
                        async with session.post(f"{BASE_URL}/sendDocument", data=form) as resp:
                            if resp.status == 200:
                                sent.append(resolved.name)
                            else:
                                failed.append(f"{path} (send failed: HTTP {resp.status})")
                except aiohttp.ClientError as e:
                    # 网络层错误：str(e) 可能含 URL（带 bot token），脱敏后再写。
                    safe_msg = str(e)
                    if BASE_URL and BASE_URL in safe_msg:
                        safe_msg = "[redacted url]"
                    failed.append(f"{path} (network error: {safe_msg[:80]})")
                except Exception as e:
                    # 通用兜底：同样脱敏 URL，避免 token 泄露给 LLM 上下文。
                    logger.debug("execute_present_files 内部忽略的异常", exc_info=True)
                    safe_msg = str(e)
                    if BASE_URL and BASE_URL in safe_msg:
                        safe_msg = "[redacted url]"
                    failed.append(f"{path} (error: {safe_msg[:50]})")
        # 返回结构：{"sent": [...], "failed": [...]}；仅当有真实错误时才附带
        # "error" 键。成功路径不再输出 "error": null —— 对模型而言是零信息
        # 字段，且会诱使模型在回复里重复说明“没有错误”。
        return json.dumps({"sent": sent, "failed": failed})


# ---------- 已移除工具的迁移提示 ----------
# stage_upload / fetch_download / list_download / list_upload 已删除：upload/
# 与 download/ 本就是工作区根目录的子目录，bash 可直接读写；所有文件工具
# 的相对路径也以 workspace 根目录解析（`cat download/x.pdf`、
# `cp out.txt upload/out.txt`、`present_files(["upload/out.txt"])`）。
# 若模型（尤其是带着旧对话历史）仍调用旧工具，返回可操作的迁移指引
# 而不是干巴巴的“未知工具”。
_REMOVED_TOOL_HINTS = {
    "fetch_download": (
        "fetch_download 已移除：download/ 就在工作区根目录下，可直接访问。"
        "用 bash（如 `ls download/`、`cat download/<文件名>`）或 text_editor"
        "（path 填 `download/<文件名>`）直接读取即可。"
    ),
    "stage_upload": (
        "stage_upload 已移除：用 bash 把文件复制到 upload/ 子目录即可，"
        "例如 `cp <文件> upload/<文件名>`，然后调用 present_files([\"upload/<文件名>\"]) 发送给用户。"
    ),
    "list_download": (
        "list_download 已移除：用 bash 执行 `ls -la download/` 查看用户上传的文件。"
    ),
    "list_upload": (
        "list_upload 已移除：用 bash 执行 `ls -la upload/` 查看发送暂存区里的文件。"
    ),
    "ip_geo": (
        "ip_geo 已移除：不再提供 IP 归属地查询能力，无需重试。"
    ),
    "send_message_to_user": (
        "send_message_to_user 已移除：请改用 message_user（提问/留言，超时即用户不在）"
        "或 deliver_reply（静默模式下交付最终回复）。无需重试本工具。"
    ),
    "ask_user": (
        "ask_user 已更名为 message_user：参数与行为兼容（question 必填，options 可选），"
        "请改用 message_user。无需重试本工具。"
    ),
}

# ---------- 工具分发 ----------
async def execute_deliver_reply(chat_id: int, content) -> str:
    """deliver_reply：静默模式（/show off）下交付最终回复给用户。

    语义：发送的是 agent 轮次最后一条助手消息的 content 字段本身——由
    ai/tool_call_loop.run_one 在 send 解析为 true 时从轮次日志里回溯得到后
    传入（通常就是当前这条含 deliver_reply 调用的消息的 content），也不会
    附带 reasoning 等其他字段。send 的缺省值按事件源区分（run_one 内经
    turn_recovery.default_send_value 解析）：静默 USER 回合（用户主动发
    消息）不填按 true 处理，静默 TIMER 回合（后台巡检）不填按 false
    处理（必须显式 true）。本函数只负责发送与交付标记：通过
    sendRichMessage 发送永久富文本消息（不经过草稿）；发送成功后在
    turn_recovery 里标记"本轮已主动交付"，get_ai_response 收尾时据此决定
    静默 USER 回合是否还需要按默认 true 兜底发送（已交付则不再兜底，
    避免双发；兜底路径发送的也是同一段最后一条非空 assistant 正文——
    同样复用 _last_assistant_text 回溯，两条路径交付内容完全同源）；
    TIMER 回合没有兜底直发，不调用（或不显式填 true）本轮
    就不会有任何内容送达用户。

    工具结果刻意不携带 message_id 与正文预览：旧版结果里的
    "已发送给用户（message_id=…）：正文预览"会诱导模型在后续轮次把
    "已确认：deliver_reply 工具已成功调用"之类的回执当成新正文再次交付，
    造成冗余消息链。message_id 只写入服务端日志。
    """
    if not isinstance(content, str) or not content.strip():
        return (
            "失败：deliver_reply 没有可发送的正文。请把完整、自包含的最终回复直接写成"
            "当前消息的正文（Telegram Rich HTML），并在同一条消息中再次调用本工具"
            "（send=true，系统会发送该正文）。"
        )
    from apitelegramchat.utils import send_rich_html_message
    from apitelegramchat import turn_recovery
    try:
        result = await send_rich_html_message(chat_id, content, reassert_draft=False)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"[deliver_reply] 发送失败: {e}")
        return "失败：消息发送异常，可稍后重试。"
    if isinstance(result, int) and not isinstance(result, bool) and result > 0:
        turn_recovery.mark_reply_delivered(chat_id)
        logger.info("[deliver_reply] chat=%s 已交付最终回复 message_id=%s chars=%s", chat_id, result, len(content))
        return (
            "已发送：本轮最后一条消息正文已永久发送给用户，交付完成。"
            "不要再调用 deliver_reply，也不要输出\"已发送/已确认\"之类的确认正文——"
            "用户已经收到，重复确认只会造成冗余消息。"
        )
    if result is True:
        # HTTP 200 但未解析到 message_id：按成功处理。
        turn_recovery.mark_reply_delivered(chat_id)
        return (
            "已发送：本轮最后一条消息正文已永久发送给用户，交付完成。"
            "不要再调用 deliver_reply，也不要输出\"已发送/已确认\"之类的确认正文——"
            "用户已经收到，重复确认只会造成冗余消息。"
        )
    return "失败：消息发送失败（网络或 Telegram 错误），可稍后重试。"


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
                arguments.get("query"),
                arguments.get("num_results"),
                arguments.get("offset"),
                mode=arguments.get("mode", "search"),
                image_url=arguments.get("image_url"),
                gl=arguments.get("gl"),
                hl=arguments.get("hl"),
                tbs=arguments.get("tbs"),
            )
        elif name == "fetch_url":
            # execute_fetch_url 内部已自带逐次重试循环，并自行把
            # TimeoutError 转成失败文案返回——外层的重试包装永远捕获不到
            # 异常，属于死逻辑（最多让同一 URL 被抓 2x2 次），直接透传。
            return await execute_fetch_url(arguments.get("url", ""))
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
        elif name == "qr_code":
            return await execute_qr_code(arguments.get("text", ""))
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
        # 地图工具：模型调用查找位置类方法期间显示 find_location。
        # 同批次并发的多个地图工具共享同一条指示（引用计数），全部结束
        # 才熄灭；单次查询通常数秒内完成，长查询由 4 秒循环保活。
        elif name in LOCATION_LOOKUP_TOOLS:
            async with chat_action_scope(chat_id, "find_location"):
                if name == "geocode":
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
                else:
                    return f"失败：未知工具: {name}。"
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
        # v2.3：bash 执行期间不再推送任何进度预览（progress_callback
        # 不再传递给 execute_bash；卡片摘要保持命令片段，最终结果由
        # update_tool_item 一次性写入）。
        elif name == "bash":
            return await execute_bash(
                chat_id=chat_id,
                namespace=resolved_namespace,
                command=arguments.get("command", ""),
                restart=arguments.get("restart", False),
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
                due_at=arguments.get("due_at"),
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
            return await execute_present_files(chat_id, paths, namespace=resolved_namespace)
        elif name == "deliver_reply":
            # 防御路径：正常情况下 deliver_reply 由 tool_call_loop.run_one 的
            # 专用分支处理（send 解析为 true 时自动携带「本轮最后一条助手
            # 消息正文」；send 缺省值按事件源区分——静默 USER 回合 true、
            # 静默 TIMER 回合 false）。仅当其他路径（如子 agent 误用）直达
            # dispatch 时才走到这里——此时没有轮次日志可回溯，统一按未发送
            # 处理，避免误发。
            return (
                "未发送：deliver_reply 只能在主对话的静默回合中生效"
                "（send=true 时由系统发送本轮最后一条助手消息正文），"
                "当前路径无法执行交付。"
            )
        elif name in _REMOVED_TOOL_HINTS:
            # stage_upload / fetch_download / list_download / list_upload / ip_geo 已移除；
            # 迁移提示让模型立即改用 bash 直访，避免无意义的重试。
            return f"失败：{_REMOVED_TOOL_HINTS[name]}"
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
