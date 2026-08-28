"""tool_result_condense.py — 工具返回内容的「模型视图」精简层。

定位
----
工具的原始返回（result_str）同时服务两个消费者：
  1. UI 草稿渲染（tool_executors.format_tool_result）——用户已确认现状没问题；
  2. LLM 上下文（role=tool 消息 + 会话历史）。

上游 API（wttr.in / amap-maps MCP 等）返回的字段远超模型回答问题所需。
把原始响应一股脑塞进上下文会：
  - 浪费 token（weather 24h×30+ 字段 ≈ 8-10k token，路线 polyline 坐标串
    可达几十 KB）；
  - 挤占 _truncate_tool_result 的 20k token 截断预算，把真正有用的字段
    （POI 名称/地址、导航步骤）挤出模型视野；
  - 噪音字段降低模型定位关键信息的效率。

本模块提供两类能力：

A. condense_for_model(fn_name, fn_args, content) -> str
   按工具把「完整返回」转换成「模型视图」：
     - weather: hourly 按 hours 参数截取（默认 6，1-24），逐时/逐日只保留
       高价值字段（温度/天气/降水/风力等），删月相、露点、热指数、风寒、
       短波辐射及低价值概率字段；
     - subagent: 删 task_preview（父 agent 自己传的任务回声）与 model_name
       （与 model 重复）；
     - 其他工具/解析失败/错误文本: 原样返回（绝不改变错误语义）。

B. condense_amap_payload(raw) -> str
   高德 MCP 原始输出在进入工具返回值之前（源头）清洗：
     - 删除导航渲染专用的大体积字段（polyline / tmcs 等）；
     - 删除所有值为 None / "" / [] / {} 的空字段；
     - POI photos 只保留第一张照片 URL；
     - 顶层删除 infocode 等内部状态码。
   UI 渲染层（_render_poi_cards / _render_map_route_card 等）只依赖保留名单
   内的字段，因此源头清洗对用户可见的展示零影响；同时保证截断预算不被
   坐标串吃光。识别不了的未知结构原样返回（保底，不丢信息）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# =====================================================================
# 通用辅助
# =====================================================================

def _parse_json_stream(text: str) -> list[Any] | None:
    """解析单个 JSON 文档或相邻拼接的多个 JSON 对象/数组。

    部分 MCP 适配器会把多个 text block 直接拼接（``{...}{...}``），
    与 tool_executors._parse_structured_payload 同样的解析策略。
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


def _dump(values: list[Any]) -> str:
    if len(values) == 1:
        return json.dumps(values[0], ensure_ascii=False)
    return "\n".join(json.dumps(v, ensure_ascii=False) for v in values)


# =====================================================================
# A. weather 模型视图
# =====================================================================

# 逐时预报里模型真正用得到的字段。删除的（及其理由）：
#   DewPointC / HeatIndexC / WindChillC — 衍生体感指标，模型几乎从不引用；
#   shortRad / diffRad — 辐射通量，专业气象数据，日常问答用不到；
#   chance_of_fog / frost / overcast / sunshine / windy / hightemp / remdry —
#       wttr.in 的十项概率分类，与 chance_of_rain/snow/thunder 相比引用率极低；
#   wind_gust / cloudcover / visibility / pressure / uvIndex（逐时）—
#       当前实况里有同名字段，逐时粒度下纯属噪音。
_HOURLY_KEEP = (
    "time", "temp", "condition", "precip",
    "chance_of_rain", "humidity", "wind_speed", "wind_dir",
)

# 逐日预报保留字段。删除的：
#   moonrise / moonset / moon_phase / moon_illumination — 月相天文数据；
#   avg — 有 max/min 足够；
#   chance_of_snow / thunder / fog / frost — 低频概率，保留 rain 一项即可
#   （真的被问到时模型可再查）。
_DAILY_KEEP = (
    "date", "max", "min", "condition", "uvIndex",
    "sunrise", "sunset", "chance_of_rain",
)

# 当前实况保留字段（current 打包时本来就贴近需求，只剔除 weather_code
# ——wttr.in 内部编码，condition 文字已可读）。
_CURRENT_DROP = ("weather_code",)


def _filter_keys(mapping: dict, *, keep: tuple[str, ...] | None = None,
                 drop: tuple[str, ...] = ()) -> dict:
    if keep is not None:
        return {k: mapping[k] for k in keep if k in mapping}
    return {k: v for k, v in mapping.items() if k not in drop}


def _condense_weather(payload: dict, hours_arg: Any) -> dict:
    out: dict[str, Any] = {}
    for key in ("city", "unit"):
        if key in payload:
            out[key] = payload[key]
    if "error" in payload:
        out["error"] = payload["error"]

    current = payload.get("current")
    if isinstance(current, dict):
        out["current"] = _filter_keys(current, drop=_CURRENT_DROP)

    # hours 参数在工具描述中承诺控制逐时条数（默认 6）；此前实现从未生效，
    # 固定返回 24 条。这里让它真正起作用，模型需要更长展望时可以显式传大值。
    try:
        hours = int(hours_arg)
    except (TypeError, ValueError):
        hours = 6
    hours = max(1, min(hours, 24))

    hourly = payload.get("hourly")
    if isinstance(hourly, list):
        trimmed = hourly[:hours]
        if len(hourly) > hours:
            out["hourly_omitted"] = len(hourly) - hours
        out["hourly"] = [
            _filter_keys(h, keep=_HOURLY_KEEP) for h in trimmed
            if isinstance(h, dict)
        ]

    daily = payload.get("daily")
    if isinstance(daily, list):
        out["daily"] = [
            _filter_keys(d, keep=_DAILY_KEEP) for d in daily
            if isinstance(d, dict)
        ]

    # 保底：三类数据一个都没识别出来（上游 schema 变了），退回原始 payload，
    # 宁可多给 token 也不能让模型拿不到数据。
    if not any(k in out for k in ("current", "hourly", "daily")):
        return payload
    return out


# =====================================================================
# B. subagent 模型视图
# =====================================================================

# 删除理由：
#   task_preview — 父 agent 自己发出的 task 的前 80 字回声，对模型零信息量
#                  （UI 卡片仍从完整返回中读取）；
#   model_name  — 展示名，model（模型 ID）已在同一对象里。
_SUBAGENT_DROP = ("task_preview", "model_name")


def _condense_subagent(payload: dict) -> dict:
    condensed = {k: v for k, v in payload.items() if k not in _SUBAGENT_DROP}
    return condensed or payload


# =====================================================================
# C. amap-maps MCP 输出源头清洗
# =====================================================================

# 导航渲染专用字段：坐标串与分段路况对「文字回答」毫无价值，却是体积大头。
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
})

# photos 数组清洗后每条保留的字段。
_AMAP_PHOTO_KEEP = ("url",)

# 顶层高德状态字段中保留的（status 用于错误判定，info 携带错误原因，
# count 表明结果规模），其余（如 cost 之外的空 biz_ext 子键）按空值规则删除。


def _amap_is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _condense_amap_node(node: Any) -> Any:
    """递归清洗高德结构。未知形状原样返回，绝不抛异常。"""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _AMAP_DROP_KEYS:
                continue
            cleaned = _condense_amap_node(value)
            if _amap_is_empty(cleaned):
                continue
            out[key] = cleaned
        return out
    if isinstance(node, list):
        cleaned_list = [_condense_amap_node(item) for item in node]
        return [item for item in cleaned_list if not _amap_is_empty(item)]
    return node


def _condense_amap_photos(payload: Any) -> Any:
    """POI photos 列表只保留第一张可用 URL（UI 与模型都只用第一张）。"""
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if key == "photos" and isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, dict):
                    url = first.get("url") or first.get("photo_url")
                    if isinstance(url, str) and url.strip():
                        out[key] = [{"url": url.strip()}]
                        continue
                out[key] = value  # 结构不符合预期时保守保留
            else:
                out[key] = _condense_amap_photos(value)
        return out
    if isinstance(payload, list):
        return [_condense_amap_photos(item) for item in payload]
    return payload


def condense_amap_payload(raw: str) -> str:
    """在工具返回值进入上下文/截断预算之前清洗高德 MCP 原始输出。

    解析失败（非 JSON / 截断的 JSON）时原样返回 —— 这类内容通常本身就是
    错误文本，调用方的错误判定逻辑依赖它的原文。
    """
    if not raw or len(raw) < 2:
        return raw
    values = _parse_json_stream(raw)
    if values is None:
        return raw
    try:
        cleaned = [
            _condense_amap_photos(_condense_amap_node(v)) for v in values
        ]
        return _dump(cleaned)
    except Exception:  # 防御：清洗逻辑本身绝不能吞掉工具结果
        logger.exception("condense_amap_payload 清洗失败，返回原始内容")
        return raw


# =====================================================================
# 对外主入口
# =====================================================================

def condense_for_model(fn_name: str, fn_args: dict | None, content: str) -> str:
    """把工具的完整返回转换成发给 LLM 的精简视图。

    原则：
      - 只对「确认存在无价值字段」的工具做改写；其余工具原样返回；
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
    if fn_name == "weather":
        values = _parse_json_stream(content)
        if values and isinstance(values[0], dict) and len(values) == 1:
            try:
                return json.dumps(
                    _condense_weather(values[0], (fn_args or {}).get("hours")),
                    ensure_ascii=False,
                )
            except Exception:
                logger.exception("weather 模型视图精简失败，返回完整内容")
                return content
        return content
    if fn_name == "subagent":
        values = _parse_json_stream(content)
        if values and isinstance(values[0], dict) and len(values) == 1:
            try:
                return json.dumps(_condense_subagent(values[0]), ensure_ascii=False)
            except Exception:
                logger.exception("subagent 模型视图精简失败，返回完整内容")
                return content
        return content
    return content
