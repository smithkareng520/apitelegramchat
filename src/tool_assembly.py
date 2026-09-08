"""工具列表装配辅助函数。

这里不改写 SEARCH_TOOLS 的源码声明顺序，而是在请求装配时生成稳定的新列表：
优先集合会被放到列表前部，其他工具保持原有相对顺序。所有发送给网关的
工具定义都会先过滤掉非 dict 元素，避免严格端点因字面 [] 等非法元素返回 400。
"""
from collections.abc import Iterable
from typing import Any


def valid_tool_defs(tools: Iterable[Any] | None) -> list[dict]:
    """返回可发送的工具定义，只保留 dict，保持原始顺序。"""
    if not tools:
        return []
    return [normalize_tool_schema(tool) for tool in tools if isinstance(tool, dict)]



def normalize_tool_schema(tool: dict) -> dict:
    """规范化发给模型的工具 schema。

    历史行为（已移除）：曾把 ``description`` 强制注入每个声明了该字段的
    工具的 ``required``——这会让 web_search 等用不到该字段的工具因「缺
    description」被参数校验整单拒绝。现在工具是否声明/必填
    ``description`` 完全以 schema 源码声明为准（当前只有 bash 声明且
    必填，其余工具一律不声明该字段）。

    保留的规范化：
    - 声明了 ``description`` 的工具（即 bash）把该字段排到 properties
      首位，让模型在长参数（command 等）之前先看到意图描述；
    - text_editor 把 ``command`` 排到首位（封闭枚举，便于流式推断）。
    """
    import copy
    tool = copy.deepcopy(tool)
    try:
        params = tool["function"]["parameters"]
        props = params.get("properties")
        if isinstance(props, dict):
            if "description" in props:
                params["properties"] = {
                    "description": props["description"],
                    **{k: v for k, v in props.items() if k != "description"},
                }
            elif tool.get("function", {}).get("name") == "text_editor" and "command" in props:
                params["properties"] = {
                    "command": props["command"],
                    **{k: v for k, v in props.items() if k != "command"},
                }
    except Exception:
        pass
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
