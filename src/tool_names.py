# tool_names.py — MCP 工具名的单一数据源。
#
# 所有 MCP 工具在模型视角统一采用 Claude Code 的命名约定：
#
#     mcp__<server>__<tool>
#
# server 名与 tool 名都来自 mcp.json 及各 MCP 服务器自身的 list_tools，
# 本模块只把「会出现在策略集合 / UI 渲染 / 模型视图 / 系统提示」里的名字
# 收敛成常量与纯函数，避免同一字符串散落十几处文件（上一代地图工具名的
# 主要维护痛点）。
#
# host 内建工具（message_user / deliver_reply / subagent / generate_image /
# generate_video / present_files）不经 MCP 暴露 —— 它们依赖宿主进程的
# Telegram 会话、草稿流与 LLM 编排能力，属于 Claude Code 里“内建工具”
# 的对应物，名字保持原样，不使用 mcp__ 前缀。
from __future__ import annotations

MCP_PREFIX = "mcp__"


def mcp_name(server: str, tool: str) -> str:
    """按 Claude Code 约定拼出模型视角的完整工具名。"""
    return f"{MCP_PREFIX}{server}__{tool}"


def is_mcp_name(name: str) -> bool:
    return isinstance(name, str) and name.startswith(MCP_PREFIX)


def split_mcp_name(name: str) -> tuple[str, str] | None:
    """拆出 (server, tool)；非 MCP 名返回 None。"""
    if not is_mcp_name(name):
        return None
    rest = name[len(MCP_PREFIX):]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


# =====================================================================
# external: gaode_mcp（高德地图，streamable_http）
# =====================================================================
GAODE_SERVER = "gaode_mcp"

MAPS_GEO = mcp_name(GAODE_SERVER, "maps_geo")
MAPS_REGEOCODE = mcp_name(GAODE_SERVER, "maps_regeocode")
MAPS_IP_LOCATION = mcp_name(GAODE_SERVER, "maps_ip_location")
MAPS_TEXT_SEARCH = mcp_name(GAODE_SERVER, "maps_text_search")
MAPS_AROUND_SEARCH = mcp_name(GAODE_SERVER, "maps_around_search")
MAPS_SEARCH_DETAIL = mcp_name(GAODE_SERVER, "maps_search_detail")
MAPS_DISTANCE = mcp_name(GAODE_SERVER, "maps_distance")
MAPS_DIRECTION_BICYCLING = mcp_name(GAODE_SERVER, "maps_direction_bicycling")
MAPS_DIRECTION_WALKING = mcp_name(GAODE_SERVER, "maps_direction_walking")
MAPS_DIRECTION_DRIVING = mcp_name(GAODE_SERVER, "maps_direction_driving")
MAPS_DIRECTION_TRANSIT = mcp_name(GAODE_SERVER, "maps_direction_transit_integrated")

# 高德地图工具全集（用于 chat action / 超时档位 / 模型视图清洗的命中判断）。
GAODE_TOOLS: frozenset[str] = frozenset({
    MAPS_GEO, MAPS_REGEOCODE, MAPS_IP_LOCATION, MAPS_TEXT_SEARCH, MAPS_AROUND_SEARCH, MAPS_SEARCH_DETAIL,
    MAPS_DISTANCE, MAPS_DIRECTION_BICYCLING, MAPS_DIRECTION_WALKING,
    MAPS_DIRECTION_DRIVING, MAPS_DIRECTION_TRANSIT,
})

# 执行期间向用户显示 find_location 聊天动作的位置类工具。
LOCATION_LOOKUP_TOOLS: frozenset[str] = GAODE_TOOLS


# =====================================================================
# internal stdio servers
# =====================================================================
SEARCH_SERVER = "internal_search"
TODO_SERVER = "internal_todo"
MEMORY_SERVER = "internal_memory"
WORKSPACE_SERVER = "internal_workspace"
BASH_SERVER = "internal_bash"

WEB_SEARCH = mcp_name(SEARCH_SERVER, "web_search")
FETCH_URL = mcp_name(SEARCH_SERVER, "fetch_url")
WIKIPEDIA = mcp_name(SEARCH_SERVER, "wikipedia")
EXCHANGE_RATE = mcp_name(SEARCH_SERVER, "exchange_rate")
WEATHER = mcp_name(SEARCH_SERVER, "weather")
SEARCH_TOOLS_MCP: frozenset[str] = frozenset({WEB_SEARCH, FETCH_URL, WIKIPEDIA, EXCHANGE_RATE, WEATHER})

TODO = mcp_name(TODO_SERVER, "todo")
MEMORY = mcp_name(MEMORY_SERVER, "memory")
TEXT_EDITOR = mcp_name(WORKSPACE_SERVER, "text_editor")
BASH = mcp_name(BASH_SERVER, "bash")


# =====================================================================
# host 内建工具（不经 MCP）
# =====================================================================
MESSAGE_USER = "message_user"
DELIVER_REPLY = "deliver_reply"
SUBAGENT = "subagent"
GENERATE_IMAGE = "generate_image"
GENERATE_VIDEO = "generate_video"
PRESENT_FILES = "present_files"


# =====================================================================
# 工具族（family）：UI 渲染 / 摘要标签按族分发，避免逐名 if/elif。
# =====================================================================
def tool_family(name: str) -> str:
    """返回工具的真实族名；高德 MCP 直接使用原生 ``maps_*`` 名称。"""
    split = split_mcp_name(name)
    if (name in GAODE_TOOLS or name in SEARCH_TOOLS_MCP) and split is not None:
        return split[1]
    if name == TODO:
        return "todo"
    if name == MEMORY:
        return "memory"
    if name == TEXT_EDITOR:
        return "text_editor"
    if name == BASH:
        return "bash"
    if name in {GENERATE_IMAGE, "generate_image_from_text", "edit_image_with_reference"}:
        return "generate_image"
    if name == GENERATE_VIDEO:
        return "generate_video"
    if name in {MESSAGE_USER, "ask_user"}:
        return "message_user"
    if name == DELIVER_REPLY:
        return "deliver_reply"
    if name == SUBAGENT:
        return "subagent"
    if name == PRESENT_FILES:
        return "present_files"
    split = split_mcp_name(name)
    if split:
        return f"mcp:{split[0]}"
    return name
