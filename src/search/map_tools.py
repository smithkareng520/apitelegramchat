"""地图工具族：经 amap-maps MCP 委托的 geocode / POI / route / distance（自 search_engine.py 拆出）。"""

import json
from typing import Any

from mcp_client import call_mcp_tool, MCPToolError
from tool_result_condense import condense_amap_payload

import logging

logger = logging.getLogger(__name__)


# ===================== 地图工具实现（amap-maps MCP 委托） =====================
# ---------------------------------------------------------------------------
# 内部：调用 amap-maps MCP 工具的统一封装
# ---------------------------------------------------------------------------
async def _call_amap_mcp(tool_name: str, arguments: dict[str, Any]) -> str:
    """调用 amap-maps MCP 服务的某个工具，返回清洗后的纯文本。

    出错时返回 JSON 错误信息（{"status": "error", "message": ...}），让上层
    工具的调用方（LLM / format_tool_result）能直接看到失败原因。

    成功输出在返回前经过 condense_amap_payload 清洗：删除 polyline / tmcs
    等导航渲染专用的大体积字段与全部空值字段。这是“源头清洗”而非仅在
    发给 LLM 前过滤——polyline 坐标串可达几十 KB，若不先清洗，
    _truncate_tool_result 的 20k token 预算会被坐标串吃光，把 POI
    名称/地址/导航步骤等真正有用的字段挤出模型视野。UI 渲染层
    （_render_poi_cards / _render_map_route_card 等）只依赖保留名单内
    的字段，清洗对用户可见的展示零影响。
    """
    try:
        raw = await call_mcp_tool("amap-maps", tool_name, arguments)
    except MCPToolError as e:
        return json.dumps(
            {"status": "error", "message": f"amap-maps MCP 调用失败（{tool_name}）：{e}"},
            ensure_ascii=False,
        )
    if not raw:
        return json.dumps(
            {"status": "error", "message": f"amap-maps MCP 返回空（{tool_name}）"},
            ensure_ascii=False,
        )
    return condense_amap_payload(raw)


def _empty_mcp_error(tool_name: str) -> str:
    return json.dumps(
        {"status": "error", "message": f"amap-maps MCP 返回空（{tool_name}）"},
        ensure_ascii=False,
    )


def _amap_error(message: str, *, code: str = "invalid_request") -> str:
    """返回统一、可被 MCP 调用方解析的地图工具错误。"""
    return json.dumps({"status": "error", "code": code, "message": message}, ensure_ascii=False)


def _is_unknown_mcp_tool_error(error: Exception) -> bool:
    """仅在服务端明确提示工具不存在时启用别名回退，避免吞掉真实业务错误。"""
    text = str(error).lower()
    markers = ("unknown tool", "tool not found", "method not found", "不存在", "未找到工具")
    return any(marker in text for marker in markers)


async def _call_amap_mcp_candidates(tool_names: list[str], arguments: dict[str, Any]) -> str:
    """按优先级调用高德 MCP 工具，并仅对未知工具名启用兼容别名。"""
    if not tool_names:
        return _amap_error("未配置高德 MCP 工具名", code="configuration_error")
    last_error: Exception | None = None
    for index, tool_name in enumerate(tool_names):
        try:
            raw = await call_mcp_tool("amap-maps", tool_name, arguments)
            if raw:
                # 与 _call_amap_mcp 同源的清洗策略（见其 docstring）。
                return condense_amap_payload(raw)
            return _empty_mcp_error(tool_name)
        except MCPToolError as exc:
            last_error = exc
            if index < len(tool_names) - 1 and _is_unknown_mcp_tool_error(exc):
                logger.info("高德 MCP 工具 %s 不可用，尝试兼容别名", tool_name)
                continue
            return _amap_error(f"amap-maps MCP 调用失败（{tool_name}）：{exc}", code="upstream_error")
    return _amap_error(f"amap-maps MCP 调用失败：{last_error}", code="upstream_error")


def _normalize_amap_coordinate(value: Any, field_name: str) -> str:
    """校验并规范化高德坐标为 ``经度,纬度``（WGS/GCJ 坐标系由上游约定）。"""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        raw_lng, raw_lat = value
    elif isinstance(value, str):
        parts = [item.strip() for item in value.split(",")]
        if len(parts) != 2:
            raise ValueError(f"{field_name} 必须是“经度,纬度”格式，例如 116.397128,39.916527")
        raw_lng, raw_lat = parts
    else:
        raise ValueError(f"{field_name} 必须是“经度,纬度”字符串")
    try:
        lng = float(raw_lng)
        lat = float(raw_lat)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须包含合法数字经纬度") from exc
    if not (-180 <= lng <= 180):
        raise ValueError(f"{field_name} 的经度必须在 -180 到 180 之间")
    if not (-90 <= lat <= 90):
        raise ValueError(f"{field_name} 的纬度必须在 -90 到 90 之间")
    return f"{lng:.6f},{lat:.6f}"


# ---------------------------------------------------------------------------
# 1. 地理编码
# ---------------------------------------------------------------------------
async def execute_geocode(address: str) -> str:
    """地理编码：地址 -> 经纬度。委托给 amap-maps MCP 的 maps_geo 工具。"""
    if not address or not address.strip():
        return json.dumps({"status": "error", "message": "地址为空"}, ensure_ascii=False)
    return await _call_amap_mcp("maps_geo", {"address": address.strip()})


# ---------------------------------------------------------------------------
# 2. POI 关键词、周边与详情搜索
# ---------------------------------------------------------------------------
async def execute_keyword_search(keywords: str, city: str | None = None) -> str:
    """按关键词搜索 POI；可用 city 将查询限定在指定城市。"""
    keyword_text = str(keywords or "").strip()
    if not keyword_text:
        return _amap_error("keywords 不能为空")
    arguments: dict[str, Any] = {"keywords": keyword_text}
    if city is not None:
        city_text = str(city).strip()
        if not city_text:
            return _amap_error("city 如提供则不能为空")
        arguments["city"] = city_text
    return await _call_amap_mcp("maps_text_search", arguments)


async def execute_nearby_search(
    keywords: str,
    location: str,
    radius: int | None = None,
) -> str:
    """在中心点周边检索 POI；location 必须为 ``经度,纬度``。"""
    keyword_text = str(keywords or "").strip()
    if not keyword_text:
        return _amap_error("keywords 不能为空")
    try:
        normalized_location = _normalize_amap_coordinate(location, "location")
    except ValueError as exc:
        return _amap_error(str(exc))
    if radius is None:
        radius_value = 1000
    else:
        if isinstance(radius, bool):
            return _amap_error("radius 必须是 1 到 50000 之间的整数（米）")
        try:
            radius_value = int(radius)
        except (TypeError, ValueError):
            return _amap_error("radius 必须是 1 到 50000 之间的整数（米）")
        if radius_value < 1 or radius_value > 50000:
            return _amap_error("radius 必须在 1 到 50000 米之间")
    return await _call_amap_mcp(
        "maps_around_search",
        {"keywords": keyword_text, "location": normalized_location, "radius": str(radius_value)},
    )


async def execute_poi_details(id: str) -> str:
    """按关键词或周边搜索结果中的 POI ID 查询地点详情。"""
    poi_id = str(id or "").strip()
    if not poi_id:
        return _amap_error("id 不能为空；请传入关键词搜索或周边搜索返回的 POI ID")
    return await _call_amap_mcp_candidates(["maps_search_detail"], {"id": poi_id})


# ---------------------------------------------------------------------------
# 3. 统一路线规划：用 mode 合并骑行、步行、驾车和公交四类能力
# ---------------------------------------------------------------------------
_ROUTE_TOOL_CANDIDATES: dict[str, list[str]] = {
    # 官方不同发布版本对骑行工具名存在差异，未知工具名时才按顺序降级。
    "cycling": ["maps_bicycling", "maps_direction_bicycling"],
    "walking": ["maps_direction_walking"],
    "driving": ["maps_direction_driving"],
    "transit": ["maps_direction_transit_integrated"],
}

_ROUTE_MODE_ALIASES = {
    "bicycle": "cycling",
    "bicycling": "cycling",
    "bike": "cycling",
    "cycling": "cycling",
    "walk": "walking",
    "walking": "walking",
    "drive": "driving",
    "driving": "driving",
    "car": "driving",
    "transit": "transit",
    "public_transit": "transit",
    "public-transport": "transit",
}


async def execute_route(
    origin: str,
    destination: str,
    mode: str = "driving",
    city: str | None = None,
    cityd: str | None = None,
) -> str:
    """规划骑行、步行、驾车或公交路线；坐标统一使用 ``经度,纬度``。"""
    try:
        normalized_origin = _normalize_amap_coordinate(origin, "origin")
        normalized_destination = _normalize_amap_coordinate(destination, "destination")
    except ValueError as exc:
        return _amap_error(str(exc))

    mode_key = _ROUTE_MODE_ALIASES.get(str(mode or "").strip().lower())
    if mode_key is None:
        return _amap_error("mode 必须是 cycling、walking、driving 或 transit")

    arguments: dict[str, Any] = {
        "origin": normalized_origin,
        "destination": normalized_destination,
    }
    city_text = str(city).strip() if city is not None else ""
    cityd_text = str(cityd).strip() if cityd is not None else ""
    if city is not None and not city_text:
        return _amap_error("city 如提供则不能为空")
    if cityd is not None and not cityd_text:
        return _amap_error("cityd 如提供则不能为空")
    if mode_key != "transit" and (city_text or cityd_text):
        return _amap_error("city 和 cityd 仅适用于 transit 公交路径规划")
    if cityd_text and not city_text:
        return _amap_error("跨城 transit 路线必须同时提供 city 和 cityd")
    if city_text:
        arguments["city"] = city_text
    if cityd_text:
        arguments["cityd"] = cityd_text

    return await _call_amap_mcp_candidates(_ROUTE_TOOL_CANDIDATES[mode_key], arguments)


# ---------------------------------------------------------------------------
# 4. 两点距离
# ---------------------------------------------------------------------------
async def execute_distance(origin: str, destination: str) -> str:
    """测量两点直线距离；origin 与 destination 均为 ``经度,纬度``。"""
    try:
        normalized_origin = _normalize_amap_coordinate(origin, "origin")
        normalized_destination = _normalize_amap_coordinate(destination, "destination")
    except ValueError as exc:
        return _amap_error(str(exc))
    return await _call_amap_mcp(
        "maps_distance",
        {"origins": normalized_origin, "destination": normalized_destination, "type": "1"},
    )
