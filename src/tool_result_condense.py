"""tool_result_condense.py — 工具返回内容的「模型视图」精简层。

定位
----
工具的原始返回（result_str）同时服务两个消费者：
  1. UI 草稿渲染（tool_executors.format_tool_result）——结构化卡片依赖
     原始 JSON 载荷，保持不动；
  2. LLM 上下文（role=tool 消息 + 会话历史）。

模型视图原则（v3：去 JSON 化）
------------------------------
JSON 是给程序读的，不是给模型读的。大段 JSON 让模型把 token 花在
解析结构与跳过无关字段上，真正回答问题所需的信息反而被稀释。本模块
把「完整 JSON」转换成「模型视图」——按工具逐一判断模型回答用户问题
真正需要什么，只把这些内容用紧凑的纯文本给模型：

  - weather: 当前实况一行 + 逐时（hours 参数控制条数，默认 6）+ 逐日；
    只保留温度/天气/降水/风力/湿度等高价值字段，月相、露点、辐射等
    从源头就不进载荷（见 search/quick_lookup.py 的载荷瘦身）；
  - todo / memory: 逐条「动作 + 对象 + id」纯文本——id 是模型后续
    done/delete/edit 必需的句柄，其余内部字段（created_at /
    completed_at / changed / ok 等）对模型零信息量，全部丢弃；
  - subagent: 终态 + 统计一行 + 最终答复正文；任务回声（task_preview）、
    展示名（model_name）、内部码（ok/code）不再重复给模型；
  - message_user: 用户的回答转成一句话（选了什么/自定义回答/取消/离开），
    不再回 JSON 信封；
  - present_files: 成功/失败清单一行化；
  - gaode maps_*: POI 列表 / 路线步骤 / 距离表转成可读文本。polyline、
    tmcs、行政区划内部编码、photos 等渲染与遥测专用字段先按原清洗规则
    剔除，再转文本 —— 模型永远看不到坐标串与格网号；
  - 其他工具（web_search / fetch_url / wikipedia / bash / text_editor …）
    本来就是文本形态，原样返回。

安全性约定
----------
  - 错误与超时文本逐字保留（错误连击熔断靠前缀匹配）；JSON 信封里的
    ``{"error": ...}`` 转写成「失败：…」文本，语义不变且仍可被失败判定
    识别（失败判定在原始 safe_content 上进行，双保险）；
  - 任何解析失败都原样返回（绝不能改变错误语义）；
  - 精简失败时退回完整内容 —— 宁多勿缺。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import tool_names as tn
from tool_names import tool_family

logger = logging.getLogger(__name__)


# =====================================================================
# 通用辅助
# =====================================================================

def _parse_json_stream(text: str) -> list[Any] | None:
    """解析单个 JSON 文档或相邻拼接的多个 JSON 对象/数组。

    部分 MCP 适配器会把多个 text block 直接拼接（``{...}{...}``）。
    返回 None 表示内容不是 JSON（调用方原样透传）。
    """
    raw = (text or "").strip()
    if not raw or raw[0] not in "[{":
        return None
    if raw.startswith("```"):
        return None  # 代码围栏包裹的内容不是本层职责
    decoder = json.JSONDecoder()
    values: list[Any] = []
    cursor = 0
    length = len(raw)
    while cursor < length:
        while cursor < length and raw[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break
        try:
            value, next_cursor = decoder.raw_decode(raw, cursor)
        except (json.JSONDecodeError, ValueError):
            return None if not values else values
        values.append(value)
        cursor = next_cursor
    return values or None


def _parse_single_object(text: str) -> dict | None:
    """尽力把工具结果解析成单个 JSON object；否则 None。"""
    values = _parse_json_stream(text)
    if values and isinstance(values[0], dict) and len(values) == 1:
        return values[0]
    return None


def _error_text(payload: dict) -> str | None:
    """JSON 错误信封 → 「失败：…」文本（保持可被失败判定识别的语义）。"""
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return f"失败：{error.strip()}"
    return None


def _clean(value: Any) -> str:
    """字段值 → 单行短文本：压空白、去空。"""
    return " ".join(str(value if value is not None else "").split())


def _no_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _compact_location(value: Any) -> str:
    """经纬度串压缩到 4 位小数（模型定位足够，省 token）。"""
    text = _clean(value)
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        return text
    try:
        lng = float(parts[0])
        lat = float(parts[1])
    except (TypeError, ValueError):
        return text
    def _fmt(v: float) -> str:
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s
    return f"{_fmt(lng)},{_fmt(lat)}"


# =====================================================================
# A. weather 模型视图（纯文本）
# =====================================================================

def _weather_line_current(current: dict, unit: str) -> str:
    temp = _clean(current.get("temp", "N/A"))
    cond = _clean(current.get("condition", "")) or "未知"
    parts = [f"{temp}{unit} {cond}"]
    feels = current.get("feels_like")
    if not _no_value(feels) and str(feels) != "N/A":
        parts.append(f"体感{feels}{unit}")
    humidity = current.get("humidity")
    if not _no_value(humidity) and str(humidity) != "N/A":
        parts.append(f"湿度{humidity}%")
    wind_dir = current.get("wind_dir")
    wind = current.get("wind")
    if not _no_value(wind) and str(wind) != "N/A":
        parts.append(f"{wind_dir}风{wind}km/h" if not _no_value(wind_dir) else f"风速{wind}km/h")
    precip = current.get("precip")
    if not _no_value(precip):
        parts.append(f"降水{precip}mm")
    vis = current.get("visibility")
    if not _no_value(vis) and str(vis) != "N/A":
        parts.append(f"能见度{vis}km")
    uv = current.get("uvIndex")
    if not _no_value(uv) and str(uv) != "N/A":
        parts.append(f"UV{uv}")
    return "，".join(parts)


def _weather_line_hourly(h: dict, unit: str) -> str:
    bits = [
        _clean(h.get("time", "")),
        f"{_clean(h.get('temp', 'N/A'))}{unit}",
        _clean(h.get("condition", "")) or "未知",
    ]
    precip = _clean(h.get("precip", ""))
    rain = _clean(h.get("chance_of_rain", ""))
    if precip and precip not in ("0", "0.0"):
        bits.append(f"降水{precip}mm")
    if rain and rain != "0":
        bits.append(f"雨概率{rain}%")
    humidity = _clean(h.get("humidity", ""))
    if humidity and humidity != "N/A":
        bits.append(f"湿度{humidity}%")
    wind_speed = _clean(h.get("wind_speed", ""))
    if wind_speed and wind_speed != "N/A":
        bits.append(f"风速{wind_speed}km/h")
    return " ".join(bits)


def _weather_line_daily(d: dict, unit: str) -> str:
    bits = [
        _clean(d.get("date", "")),
        _clean(d.get("condition", "")) or "未知",
        f"{_clean(d.get('max', 'N/A'))}~{_clean(d.get('min', 'N/A'))}{unit}",
    ]
    rain = _clean(d.get("chance_of_rain", ""))
    if rain and rain != "0":
        bits.append(f"降水概率{rain}%")
    uv = _clean(d.get("uvIndex", ""))
    if uv and uv != "N/A":
        bits.append(f"UV{uv}")
    sunrise = _clean(d.get("sunrise", ""))
    sunset = _clean(d.get("sunset", ""))
    if sunrise and sunset:
        bits.append(f"日出{sunrise} 日落{sunset}")
    return " ".join(bits)


def _weather_model_view(payload: dict, hours_arg: Any) -> str:
    error = _error_text(payload)
    if error:
        return error
    try:
        hours = max(1, min(int(hours_arg), 24))
    except (TypeError, ValueError):
        hours = 6

    city = _clean(payload.get("city", "")) or "未知城市"
    unit = "°F" if str(payload.get("unit", "C")).upper() == "F" else "°C"

    current = payload.get("current")
    hourly = payload.get("hourly")
    daily = payload.get("daily")
    if not isinstance(current, dict) and not isinstance(hourly, list) and not isinstance(daily, list):
        # 上游 schema 变化且三类数据都没识别出来：宁可多给也不能让模型拿不到数据。
        return json.dumps(payload, ensure_ascii=False)

    lines: list[str] = [f"{city} 天气："]
    if isinstance(current, dict):
        lines.append(f"当前：{_weather_line_current(current, unit)}")
    if isinstance(hourly, list) and hourly:
        shown = hourly[:hours]
        scope = f"（未来 {len(shown)} 小时）"
        lines.append(f"逐时{scope}：")
        lines.extend("  " + _weather_line_hourly(h, unit) for h in shown if isinstance(h, dict))
        if len(hourly) > len(shown):
            lines.append(f"  （其余 {len(hourly) - len(shown)} 条逐时数据已省略）")
    if isinstance(daily, list) and daily:
        lines.append("逐日预报：")
        lines.extend("  " + _weather_line_daily(d, unit) for d in daily if isinstance(d, dict))
    return "\n".join(lines)


# =====================================================================
# B. todo 模型视图（纯文本）
# =====================================================================

_TODO_DUE_STATUS_LABELS = {
    "overdue": "已逾期",
    "due_soon": "24小时内到期",
}


def _todo_item_line(todo: dict) -> str:
    todo = todo if isinstance(todo, dict) else {}
    title = _clean(todo.get("title", "")) or "（无标题）"
    tid = _clean(todo.get("id", ""))
    mark = "x" if todo.get("done") else " "
    parts = [f"[{mark}]"]
    priority = _clean(todo.get("priority", ""))
    if priority and priority != "medium":
        parts.append(f"[{priority}]")
    extras: list[str] = []
    if tid:
        extras.append(f"id={tid}")
    due_at = _clean(todo.get("due_at", ""))
    if due_at:
        status = _clean(todo.get("due_status", ""))
        label = _TODO_DUE_STATUS_LABELS.get(status, "")
        extras.append(f"截止 {due_at}" + (f"，{label}" if label else ""))
    tags = todo.get("tags")
    if isinstance(tags, list) and tags:
        cleaned = [_clean(t) for t in tags if _clean(t)]
        if cleaned:
            extras.append(" ".join(f"#{t}" for t in cleaned))
    parts.append(title)
    if extras:
        parts.append("（" + "，".join(extras) + "）")
    return " ".join(parts)


def _todo_note_line(todo: dict) -> str:
    note = _clean((todo or {}).get("note", ""))
    return f"    备注：{note}" if note else ""


def _todo_model_view(payload: dict, fn_args: dict) -> str:
    error = _error_text(payload)
    if error:
        return error
    action = _clean(payload.get("action", "")).lower() or _clean(fn_args.get("action", "")).lower() or "list"
    todo = payload.get("todo") if isinstance(payload.get("todo"), dict) else {}
    total = payload.get("total")
    pending = payload.get("pending")
    stats = ""
    if total is not None:
        stats = f"当前共 {total} 项"
        if pending is not None:
            stats += f"，未完成 {pending} 项"
        stats += "。"
    label = _clean(todo.get("title", ""))
    tid = _clean(todo.get("id", ""))
    who = f"「{label}」" if label else "待办"
    if tid:
        who += f"（id={tid}）"

    if action == "list" or (action not in {"add", "done", "undone", "toggle", "delete", "clear", "edit"} and payload.get("todos") is not None):
        todos = payload.get("todos")
        if not isinstance(todos, list):
            return json.dumps(payload, ensure_ascii=False)
        header = f"待办清单：{stats}" if stats else "待办清单："
        if not todos:
            return f"{header}\n（当前筛选下没有待办项）"
        lines = [header]
        for t in todos:
            if isinstance(t, dict):
                lines.append(f"- {_todo_item_line(t)}")
                note_line = _todo_note_line(t)
                if note_line:
                    lines.append(note_line)
        return "\n".join(lines)
    if action == "add":
        extra = ""
        priority = _clean(todo.get("priority", ""))
        if priority and priority != "medium":
            extra += f"，优先级 {priority}"
        due_at = _clean(todo.get("due_at", ""))
        if due_at:
            extra += f"，截止 {due_at}"
        return f"已添加待办 {who}{extra}。{stats}"
    if action in ("done", "undone", "toggle"):
        done_flag = todo.get("done")
        if done_flag is True:
            verb = "已完成待办"
        elif done_flag is False:
            verb = "已重开待办"
        else:
            verb = "已更新待办"
        if payload.get("changed") is False:
            verb = f"待办状态未变化（仍为{'已完成' if done_flag else '未完成'}）"
        return f"{verb} {who}。{stats}"
    if action == "delete":
        return f"已删除待办 {who}。剩余 {total if total is not None else '0'} 项。"
    if action == "clear":
        message = _clean(payload.get("message", ""))
        return f"{message}。剩余 {total if total is not None else '0'} 项，未完成 {pending if pending is not None else '0'} 项。"
    if action == "edit":
        changed = payload.get("changed")
        changed_txt = ""
        if isinstance(changed, list) and changed:
            changed_txt = f"，修改字段：{'、'.join(_clean(c) for c in changed)}"
        return f"已更新待办 {who}{changed_txt}。{stats}"
    # 未知 action（防御）：退回原 JSON
    return json.dumps(payload, ensure_ascii=False)


# =====================================================================
# C. memory 模型视图（纯文本）
# =====================================================================

def _memory_line(mem: dict) -> str:
    mem = mem if isinstance(mem, dict) else {}
    content = _clean(mem.get("content", ""))
    tid = _clean(mem.get("id", ""))
    category = _clean(mem.get("category", ""))
    importance = _clean(mem.get("importance", ""))
    prefixes = []
    if importance and importance != "medium":
        prefixes.append(importance)
    if category and category != "note":
        prefixes.append(category)
    prefix = "".join(f"[{p}]" for p in prefixes)
    extras: list[str] = []
    if tid:
        extras.append(f"id={tid}")
    tags = mem.get("tags")
    if isinstance(tags, list) and tags:
        cleaned = [_clean(t) for t in tags if _clean(t)]
        if cleaned:
            extras.append("标签 " + " ".join(f"#{t}" for t in cleaned))
    suffix = f"（{'，'.join(extras)}）" if extras else ""
    return f"- {prefix}{content}{suffix}"


def _memory_model_view(payload: dict, fn_args: dict) -> str:
    error = _error_text(payload)
    if error:
        return error
    action = _clean(payload.get("action", "")).lower() or _clean(fn_args.get("action", "")).lower() or "list"
    mem = payload.get("memory") if isinstance(payload.get("memory"), dict) else {}
    total = payload.get("total")
    stats = f"当前共 {total} 条记忆。" if total is not None else ""

    def _mem_desc(m: dict) -> str:
        content = _clean(m.get("content", ""))
        category = _clean(m.get("category", ""))
        importance = _clean(m.get("importance", ""))
        bits = []
        if importance and importance != "medium":
            bits.append(f"重要度 {importance}")
        if category and category != "note":
            bits.append(f"分类 {category}")
        suffix = f"（{'，'.join(bits)}）" if bits else ""
        return f"「{content}」{suffix}"

    if action in ("list", "search") or payload.get("memories") is not None:
        memories = payload.get("memories")
        if not isinstance(memories, list):
            return json.dumps(payload, ensure_ascii=False)
        header = f"记忆检索结果：{stats}" if stats else "记忆检索结果："
        if not memories:
            return f"{header}\n（没有匹配的记忆）"
        lines = [header]
        lines.extend(_memory_line(m) for m in memories if isinstance(m, dict))
        return "\n".join(lines)
    if action == "add":
        return f"已保存记忆 {_mem_desc(mem)}（id={_clean(mem.get('id', ''))}）。{stats}"
    if action == "get":
        return f"记忆详情（id={_clean(mem.get('id', ''))}）：{_clean(mem.get('content', ''))}"
    if action == "update":
        changed = payload.get("changed")
        changed_txt = ""
        if isinstance(changed, list) and changed:
            changed_txt = f"，修改字段：{'、'.join(_clean(c) for c in changed)}"
        return f"已更新记忆（id={_clean(mem.get('id', ''))}{changed_txt}）：{_clean(mem.get('content', ''))}。{stats}"
    if action == "delete":
        return f"已删除记忆（id={_clean(mem.get('id', ''))}）：{_clean(mem.get('content', ''))}。剩余 {total if total is not None else '0'} 条。"
    if action == "clear":
        message = _clean(payload.get("message", ""))
        return f"{message}。{stats}"
    return json.dumps(payload, ensure_ascii=False)


# =====================================================================
# D. subagent 模型视图（纯文本）
# =====================================================================

def _subagent_model_view(payload: dict) -> str:
    error = _error_text(payload)
    rounds = _clean(payload.get("rounds", ""))
    tool_calls = _clean(payload.get("tool_calls", ""))
    stats_bits = []
    if rounds and rounds not in ("0",):
        stats_bits.append(f"{rounds} 轮")
    if tool_calls and tool_calls != "0":
        stats_bits.append(f"{tool_calls} 次工具调用")
    stats = "（" + " · ".join(stats_bits) + "）" if stats_bits else ""
    if error:
        model = _clean(payload.get("model", ""))
        model_txt = f"，模型 {model}" if model else ""
        return f"{error}{stats}{model_txt}"
    answer = str(payload.get("answer", "") or "").strip()
    model = _clean(payload.get("model", ""))
    head_bits = []
    if model:
        head_bits.append(f"模型 {model}")
    head_bits.extend(stats_bits)
    elapsed = payload.get("elapsed")
    if isinstance(elapsed, (int, float)) and elapsed > 0:
        head_bits.append(f"用时 {elapsed:.0f}s")
    head = f"子 agent 完成：{' · '.join(head_bits)}。" if head_bits else "子 agent 完成。"
    if not answer:
        return head + "\n（最终答复为空）"
    return f"{head}\n最终答复：\n{answer}"


# =====================================================================
# E. message_user / ask_user 回答模型视图（纯文本）
# =====================================================================

def _message_user_answer_view(payload: dict) -> str:
    atype = _clean(payload.get("type", "")).lower() or "unknown"
    if atype == "choice":
        selected = payload.get("selected")
        labels = []
        if isinstance(selected, list):
            for item in selected:
                if isinstance(item, dict):
                    label = _clean(item.get("label", ""))
                    if label:
                        labels.append(label)
                elif isinstance(item, str) and item.strip():
                    labels.append(item.strip())
        if labels:
            return f"用户选择了：{'、'.join(labels)}"
        return "用户已提交选择，但未包含可读选项。"
    if atype == "custom":
        value = _clean(payload.get("value", "") or payload.get("text", ""))
        return f"用户的回答：{value}" if value else "用户提供了自定义回答，但内容为空。"
    if atype == "cancelled":
        return "用户取消了本次交互（未回答）。不要等待；若信息必需请换一种方式继续。"
    if atype == "expired":
        note = _clean(payload.get("note", ""))
        return note or "用户在超时时间内没有回复（用户可能不在）。可结束本回合，用户回来后会再联系。"
    return json.dumps(payload, ensure_ascii=False)


# =====================================================================
# F. present_files 模型视图（纯文本）
# =====================================================================

def _present_files_view(payload: dict) -> str:
    error = _error_text(payload)
    if error:
        return error
    sent = payload.get("sent")
    failed = payload.get("failed")
    sent_names = [ _clean(x) for x in sent ] if isinstance(sent, list) else []
    failed_items = [ _clean(x) for x in failed ] if isinstance(failed, list) else []
    lines: list[str] = []
    if sent_names:
        listing = "、".join(sent_names[:3]) + ("等" if len(sent_names) > 3 else "")
        lines.append(f"已发送 {len(sent_names)} 个文件：{listing}。")
    elif not failed_items:
        return "失败：没有发送任何文件（无有效路径）。"
    if failed_items:
        lines.append(f"发送失败 {len(failed_items)} 个：")
        lines.extend(f"  - {item}" for item in failed_items[:5])
        if len(failed_items) > 5:
            lines.append(f"  （其余 {len(failed_items) - 5} 个失败已省略）")
    return "\n".join(lines)


# =====================================================================
# G. gaode maps 模型视图（纯文本）
# =====================================================================
# 先按原清洗规则剔除渲染/遥测专用字段（polyline、内部编码、空值…），
# 再把剩下的业务字段转成可读文本。UI 视图仍拿完整原始载荷。

_AMAP_DROP_KEYS = frozenset({
    "polyline",       # 路线坐标串（lng,lat;lng,lat;…），单条可达几十 KB
    "tmcs",           # 每一步内部再细分路段的实时路况数组
    "navi_poiid",     # 导航专用 POI 关联 ID
    "poi_tag",
    "biz_type",
    "parent",         # 父 POI ID 数组（几乎总是空数组）
    "children",       # 子 POI 列表（加油站分枪等场景，本项目用不到）
    "indoor_map",     # 室内地图标识
    "entrance",       # 出入口坐标串
    "exit",
    "infocode",       # 高德内部状态码（"10000"）
    "scode",          # 部分网关返回的二级状态码
    # 高德内部行政区划/网格/运营编码——模型不该在自然语言回答里引用它们：
    "gridcode", "pcode", "adcode", "citycode", "cpid",
    "entr_location", "timestamp",
    "discount_num", "shopinfo", "recommend", "groupbuy_num", "importance", "poiweight",
    # 模型视图不需要 photos（不帮助文本选点，体积大）；typecode 是内部
    # 分类编码，type 文字已可读。
    "photos", "typecode",
})

_AMAP_BIZ_EXT_DROP_KEYS = frozenset({"meal_ordering"})


def _amap_is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _condense_amap_node(node: Any, *, in_biz_ext: bool = False) -> Any:
    """递归清洗高德结构。未知形状原样返回，绝不抛异常。"""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _AMAP_DROP_KEYS:
                continue
            if in_biz_ext and key in _AMAP_BIZ_EXT_DROP_KEYS:
                continue
            cleaned = _condense_amap_node(value, in_biz_ext=(key == "biz_ext"))
            if _amap_is_empty(cleaned):
                continue
            out[key] = cleaned
        return out
    if isinstance(node, list):
        cleaned_list = [_condense_amap_node(item) for item in node]
        return [item for item in cleaned_list if not _amap_is_empty(item)]
    return node


_AMAP_BIZ_PROMOTE_KEYS = (
    "rating", "cost", "tag", "atag", "business_area", "website", "email",
    "parking_type", "opentime2", "open_time", "opentime", "business_hours",
    "opening_hours", "hours",
)


def _normalize_amap_poi(poi: dict[str, Any]) -> dict[str, Any]:
    """把 biz_ext 里的高价值字段提升到 POI 顶层（模型直读）。"""
    out = dict(poi)
    biz_ext = out.get("biz_ext")
    if isinstance(biz_ext, dict):
        for key in _AMAP_BIZ_PROMOTE_KEYS:
            if out.get(key) in (None, "", [], {}) and biz_ext.get(key) not in (None, "", [], {}):
                out[key] = biz_ext[key]
        out.pop("biz_ext", None)
    return out


def _normalize_amap_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        out = {key: _normalize_amap_payload(value) for key, value in payload.items()}
        pois = out.get("pois")
        if isinstance(pois, list):
            out["pois"] = [
                _normalize_amap_poi(item) if isinstance(item, dict) else item
                for item in pois
            ]
        if (
            "name" in out
            and isinstance(out.get("name"), str)
            and any(key in out for key in ("address", "type", "location"))
        ):
            out = _normalize_amap_poi(out)
        return out
    if isinstance(payload, list):
        return [_normalize_amap_payload(item) for item in payload]
    return payload


_POI_TEXT_FIELDS = (
    "name", "alias", "address", "location", "tel", "website", "distance",
    "rating", "cost", "opentime2", "open_time", "opentime", "business_hours",
    "opening_hours", "hours", "business_area", "tag", "atag", "parking_type",
    "type", "email",
)


def _poi_text_line(poi: dict, index: int | None = None) -> str:
    poi = _normalize_amap_poi(_condense_amap_node(poi) if poi else {})
    if not poi:
        return ""
    bits = []
    for key in _POI_TEXT_FIELDS:
        value = poi.get(key)
        if _no_value(value):
            continue
        if key == "location":
            value = _compact_location(value)
        text = _clean(value)
        if not text or text == "[]":
            continue
        bits.append(text)
    # id 单独收尾：模型后续 maps_search_detail 需要它。
    pid = _clean(poi.get("id", ""))
    id_txt = f" id={pid}" if pid else ""
    prefix = f"{index}. " if index is not None else ""
    return f"{prefix}" + "｜".join(bits) + id_txt


def _amap_geo_view(payload: dict) -> str:
    geocodes = payload.get("geocodes")
    if not isinstance(geocodes, list) or not geocodes:
        return json.dumps(payload, ensure_ascii=False)
    lines = [f"地理编码结果（{len(geocodes)} 条）："]
    for geo in geocodes[:5]:
        geo = geo if isinstance(geo, dict) else {}
        bits = []
        for key in ("formatted_address", "country", "province", "city", "district",
                    "street", "number", "location", "level"):
            value = geo.get(key)
            if _no_value(value):
                continue
            if key == "location":
                value = _compact_location(value)
            bits.append(_clean(value))
        lines.append(f"- {'｜'.join(bits)}" if bits else "- （空结果）")
    if len(geocodes) > 5:
        lines.append(f"（其余 {len(geocodes) - 5} 条已省略）")
    return "\n".join(lines)


def _amap_regeocode_view(payload: dict) -> str:
    regeo = payload.get("regeocode")
    if not isinstance(regeo, dict):
        return json.dumps(payload, ensure_ascii=False)
    address = _clean(regeo.get("formatted_address", ""))
    comp = regeo.get("addressComponent")
    area_bits: list[str] = []
    if isinstance(comp, dict):
        for key in ("country", "province", "city", "district", "street", "number",
                    "township", "neighborhood"):
            value = comp.get(key)
            if isinstance(value, dict):
                value = value.get("name")
            if _no_value(value):
                continue
            area_bits.append(_clean(value))
    area = "，".join(dict.fromkeys(area_bits))
    return f"逆地理编码结果：{address}" + (f"（{area}）" if area and area != address else "")


def _amap_poi_list_view(payload: dict, keyword: str = "") -> str:
    pois = payload.get("pois")
    if not isinstance(pois, list):
        return json.dumps(payload, ensure_ascii=False)
    head = f"POI 搜索结果{f'「{keyword}」' if keyword else ''}（共 {len(pois)} 条）："
    if not pois:
        return head + "\n（没有匹配的地点）"
    lines = [head]
    idx = 0
    for poi in pois:
        if isinstance(poi, dict):
            idx += 1
            line = _poi_text_line(poi, idx)
            if line:
                lines.append(line)
        if idx >= 10:
            break
    if len(pois) > 10:
        lines.append(f"（其余 {len(pois) - 10} 条已省略；可加 city/缩窄关键词再查）")
    return "\n".join(lines)


def _amap_detail_view(payload: dict) -> str:
    if not isinstance(payload, dict) or "name" not in payload:
        return json.dumps(payload, ensure_ascii=False)
    line = _poi_text_line(payload)
    return f"POI 详情：{line}" if line else json.dumps(payload, ensure_ascii=False)


def _format_distance(value: Any) -> str:
    text = _clean(value)
    try:
        meters = float(text)
    except (TypeError, ValueError):
        return text
    if meters >= 1000:
        return f"{meters / 1000:.1f}km"
    return f"{meters:g}m" if meters else text


def _format_duration(value: Any) -> str:
    text = _clean(value)
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        return text
    minutes = seconds / 60
    if minutes >= 60:
        hours = int(minutes // 60)
        return f"{hours}小时{int(round(minutes % 60))}分钟"
    return f"{minutes:.0f}分钟" if minutes >= 1 else f"{seconds:g}秒"


def _amap_direction_view(payload: dict, mode_label: str) -> str:
    route = payload.get("route")
    if not isinstance(route, dict):
        return json.dumps(payload, ensure_ascii=False)
    origin = _compact_location(route.get("origin", ""))
    destination = _compact_location(route.get("destination", ""))
    header = f"{mode_label}（{origin} → {destination}）："
    paths = route.get("paths")
    transits = route.get("transits")
    lines = [header]
    if isinstance(paths, list) and paths:
        for i, path in enumerate(paths[:3], 1):
            path = path if isinstance(path, dict) else {}
            distance = _format_distance(path.get("distance", ""))
            duration = _format_duration(path.get("duration", ""))
            lines.append(f"方案{i}：全程 {distance}，{duration}")
            steps = path.get("steps")
            if isinstance(steps, list) and steps:
                step_items = []
                for s in steps:
                    if not isinstance(s, dict):
                        continue
                    instruction = _clean(s.get("instruction", ""))
                    if not instruction:
                        continue
                    step_items.append((instruction, _clean(s.get("road", "")),
                                       _format_distance(s.get("distance", ""))))
                for shown, (instruction, road, step_distance) in enumerate(step_items[:15], 1):
                    road_txt = f"（{road}，{step_distance}）" if road else f"（{step_distance}）"
                    lines.append(f"  {shown}. {instruction}{road_txt}")
                if len(step_items) > 15:
                    lines.append(f"  （其余 {len(step_items) - 15} 步已省略，完整共 {len(step_items)} 步）")
    elif isinstance(transits, list) and transits:
        for i, transit in enumerate(transits[:3], 1):
            transit = transit if isinstance(transit, dict) else {}
            duration = _format_duration(transit.get("duration", ""))
            walking = _format_distance(transit.get("walking_distance", ""))
            lines.append(f"方案{i}：{duration}，步行 {walking}")
            segments = transit.get("segments")
            if isinstance(segments, list):
                for seg in segments:
                    if not isinstance(seg, dict):
                        continue
                    bus = seg.get("bus")
                    bus_lines = bus.get("buslines") if isinstance(bus, dict) else None
                    if isinstance(bus_lines, list):
                        for bl in bus_lines[:2]:
                            if isinstance(bl, dict):
                                name = _clean(bl.get("name", ""))
                                departure = _clean(bl.get("departure_stop", {}).get("name", "") if isinstance(bl.get("departure_stop"), dict) else "")
                                arrival = _clean(bl.get("arrival_stop", {}).get("name", "") if isinstance(bl.get("arrival_stop"), dict) else "")
                                via = f"（{departure} → {arrival}）" if departure and arrival else ""
                                if name:
                                    lines.append(f"  乘 {name}{via}")
    else:
        return json.dumps(payload, ensure_ascii=False)
    return "\n".join(lines)


def _amap_distance_view(payload: dict) -> str:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return json.dumps(payload, ensure_ascii=False)
    lines = [f"距离测量结果（{len(results)} 条）："]
    for row in results[:10]:
        row = row if isinstance(row, dict) else {}
        origin = _compact_location(row.get("origin_id", "") or row.get("origin", ""))
        dest = _compact_location(row.get("dest_id", "") or row.get("destination", ""))
        distance = _format_distance(row.get("distance", ""))
        duration = _format_duration(row.get("duration", ""))
        pair = f"{origin} → {dest}" if origin and dest else ""
        lines.append(f"- {pair}：{distance}，{duration}" if pair else f"- {distance}，{duration}")
    if len(results) > 10:
        lines.append(f"（其余 {len(results) - 10} 条已省略）")
    return "\n".join(lines)


def _amap_ip_location_view(payload: dict) -> str:
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)
    province = _clean(payload.get("province", ""))
    city = _clean(payload.get("city", ""))
    if not province and not city:
        return json.dumps(payload, ensure_ascii=False)
    area = " ".join(x for x in (province, city) if x and x != "[]")
    return f"IP 归属地：{area}"


def _amap_model_view(fn_name: str, content: str) -> str:
    values = _parse_json_stream(content)
    if not values:
        return content
    try:
        cleaned_docs = [_normalize_amap_payload(_condense_amap_node(v)) for v in values]
    except Exception:
        logger.exception("amap 模型视图清洗失败，返回原始内容")
        return content
    short_name = tn.split_mcp_name(fn_name)[1] if tn.split_mcp_name(fn_name) else fn_name
    views: list[str] = []
    for doc in cleaned_docs:
        if not isinstance(doc, dict):
            views.append(json.dumps(doc, ensure_ascii=False))
            continue
        if doc.get("status") not in (None, "1", 1, "0", 0) and _error_text(doc):
            views.append(_error_text(doc))
            continue
        if str(doc.get("status")) in ("0", 0) and not doc.get("geocodes"):
            # 高德 status=0：查询失败。info 携带原因。
            info = _clean(doc.get("info", "") or doc.get("message", ""))
            views.append(f"失败：地图查询未成功（{info}）" if info else "失败：地图查询未成功。")
            continue
        try:
            if short_name == "maps_geo":
                views.append(_amap_geo_view(doc))
            elif short_name == "maps_regeocode":
                views.append(_amap_regeocode_view(doc))
            elif short_name in ("maps_text_search", "maps_around_search"):
                views.append(_amap_poi_list_view(doc))
            elif short_name == "maps_search_detail":
                views.append(_amap_detail_view(doc))
            elif short_name == "maps_direction_driving":
                views.append(_amap_direction_view(doc, "驾车路线"))
            elif short_name == "maps_direction_walking":
                views.append(_amap_direction_view(doc, "步行路线"))
            elif short_name == "maps_direction_bicycling":
                views.append(_amap_direction_view(doc, "骑行路线"))
            elif short_name == "maps_direction_transit_integrated":
                views.append(_amap_direction_view(doc, "公交路线"))
            elif short_name == "maps_distance":
                views.append(_amap_distance_view(doc))
            elif short_name == "maps_ip_location":
                views.append(_amap_ip_location_view(doc))
            else:
                # 未知高德工具：保守起见返回清洗后的 JSON（不是原始大载荷）。
                views.append(json.dumps(doc, ensure_ascii=False))
        except Exception:
            logger.exception("amap 模型视图渲染失败（%s），退回清洗 JSON", short_name)
            views.append(json.dumps(doc, ensure_ascii=False))
    return "\n".join(v for v in views if v)


# =====================================================================
# 对外主入口
# =====================================================================

def condense_for_model(fn_name: str, fn_args: dict | None, content: str) -> str:
    """把工具的完整返回转换成发给 LLM 的精简视图。

    原则：
      - 模型视图一律是紧凑的纯文本（去 JSON 化）——JSON 只属于 UI 卡片；
      - 只对有专属视图的工具改写；其余工具（本就是文本形态）原样返回；
      - 任何解析失败都原样返回（错误文本、非 JSON 文本绝不能被改写，
        否则 tool_call_loop 的错误连击熔断 / 失败判定会失效）；
      - 精简失败时退回完整内容 —— 宁多勿缺。
    """
    if not isinstance(content, str) or not content:
        return content
    stripped = content.lstrip()
    # 错误与超时语义必须逐字保留（错误连击熔断靠前缀匹配）。
    if stripped.startswith(("Error:", "Exception:", "❌", "失败：", "失败:", "⚠️")):
        return content
    fn_args = fn_args or {}
    family = tool_family(fn_name)

    if family == "weather":
        payload = _parse_single_object(content)
        if payload is None:
            return content
        try:
            return _weather_model_view(payload, fn_args.get("hours"))
        except Exception:
            logger.exception("weather 模型视图精简失败，返回完整内容")
            return content

    if family == "subagent":
        payload = _parse_single_object(content)
        if payload is None:
            return content
        try:
            return _subagent_model_view(payload)
        except Exception:
            logger.exception("subagent 模型视图精简失败，返回完整内容")
            return content

    if family == "todo":
        payload = _parse_single_object(content)
        if payload is None:
            return content
        try:
            return _todo_model_view(payload, fn_args)
        except Exception:
            logger.exception("todo 模型视图精简失败，返回完整内容")
            return content

    if family == "memory":
        payload = _parse_single_object(content)
        if payload is None:
            return content
        try:
            return _memory_model_view(payload, fn_args)
        except Exception:
            logger.exception("memory 模型视图精简失败，返回完整内容")
            return content

    if family == "message_user":
        payload = _parse_single_object(content)
        if payload is None:
            return content
        try:
            return _message_user_answer_view(payload)
        except Exception:
            logger.exception("message_user 模型视图精简失败，返回完整内容")
            return content

    if family == "present_files":
        payload = _parse_single_object(content)
        if payload is None:
            return content
        try:
            return _present_files_view(payload)
        except Exception:
            logger.exception("present_files 模型视图精简失败，返回完整内容")
            return content

    if fn_name in tn.GAODE_TOOLS:
        # 高德 MCP 原生工具：清洗 + 纯文本化（UI 视图仍拿完整载荷渲染卡片）。
        try:
            return _amap_model_view(fn_name, content)
        except Exception:
            logger.exception("amap 模型视图转换失败，返回原始内容")
            return content

    return content
