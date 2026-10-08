"""工具列表装配辅助函数。"""
from collections.abc import Iterable
from typing import Any

from tool_names import split_mcp_name

def _schema_key(name: str) -> str:
    """完整工具名（mcp__<server>__<tool> 或短名）→ 规范短名。

    MCP 化后模型可见名统一带 mcp__ 前缀；字段排序等 schema 规范化
    规则仍按短名（= 内部 MCP 服务器的工具名）声明。
    """
    split = split_mcp_name(name or "")
    return split[1] if split else (name or "")

def valid_tool_defs(tools: Iterable[Any] | None) -> list[dict]:
    """返回可发送的工具定义，只保留 dict，保持原始顺序。"""
    if not tools:
        return []
    return [normalize_tool_schema(tool) for tool in tools if isinstance(tool, dict)]

def normalize_tool_schema(tool: dict) -> dict:
    """规范化发给模型的工具 schema。

    工具是否声明/必填 ``description`` 完全以 schema 源码声明为准
    （当前只有 bash 声明且必填，其余工具一律不声明该字段）。

    规范化内容：
    - 所有必填字段排在可选字段之前，保持各组内的声明顺序稳定；
    - bash 保留 ``description`` 首位；
    - text_editor 保留 ``command`` 首位（封闭枚举，便于流式推断）。
    """
    import copy

    tool = copy.deepcopy(tool)
    function = tool.get("function")
    if not isinstance(function, dict):
        return tool
    params = function.get("parameters")
    if not isinstance(params, dict):
        return tool
    props = params.get("properties")
    if not isinstance(props, dict):
        return tool

    required = [k for k in (params.get("required") or []) if k in props]
    required_set = set(required)
    optional = [k for k in props if k not in required_set]
    ordered = required + optional

    name = _schema_key(function.get("name"))
    if name == "bash" and "description" in props:
        ordered = ["description"] + [k for k in ordered if k != "description"]
    elif name == "text_editor" and "command" in props:
        ordered = ["command"] + [k for k in ordered if k != "command"]

    params["properties"] = {k: props[k] for k in ordered}
    return tool

def tool_name(tool: dict) -> str:
    """安全读取 OpenAI 风格工具定义的函数名。"""
    function = tool.get("function")
    return function.get("name", "") if isinstance(function, dict) else ""

def prioritize_tool_defs(
    tools: Iterable[Any] | None,
    priority_names: Iterable[str] | None,
) -> list[dict]:
    """稳定地把受限优先工具排列到前部，不移动源代码中的常量定义。

    非优先工具仍会保留并维持相对顺序；调用方若需要安全工具面，
    应在本函数结果上再按允许名称过滤。
    """
    valid = valid_tool_defs(tools)
    priority = {str(name).strip() for name in (priority_names or []) if str(name).strip()}
    if not priority:
        return valid
    return [tool for tool in valid if tool_name(tool) in priority] + [
        tool for tool in valid if tool_name(tool) not in priority
    ]

def restrict_tool_defs(
    tools: Iterable[Any] | None,
    allowed_names: Iterable[str] | None,
) -> list[dict]:
    """过滤为允许工具面，并在过滤前完成非 dict 清理。"""
    allowed = {str(name).strip() for name in (allowed_names or []) if str(name).strip()}
    return [tool for tool in valid_tool_defs(tools) if tool_name(tool) in allowed]
