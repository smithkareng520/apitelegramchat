
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-06-18"

STATEFUL_TOOL_NAMES = {
    "bash",
    "text_editor",
    "todo",
    "memory",
    "skill",
    "subagent",
    "present_files",
}

RESOURCE_URI_TOOL_CATALOG = "project://tool-catalog"
PROMPT_NAME_PROJECT_BRIEF = "project-brief"


@dataclass(frozen=True)
class ToolCatalogEntry:
    name: str
    description: str
    input_schema: dict[str, Any]
    category: str
    stateful: bool = False
    annotations: dict[str, Any] | None = None

    def to_mcp_tool(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "description": self.description,
            "inputSchema": _with_workspace_id(self.input_schema, self.stateful),
        }
        if self.annotations:
            payload["annotations"] = self.annotations
        return payload


def _safe_copy(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False))


def _normalize_tool_def(tool_def: dict[str, Any]) -> ToolCatalogEntry:
    function = tool_def.get("function", {}) if isinstance(tool_def, dict) else {}
    name = str(function.get("name", "")).strip()
    description = str(function.get("description", "")).strip()
    params = _safe_copy(function.get("parameters") or {"type": "object", "properties": {}})
    category = _guess_category(name)
    return ToolCatalogEntry(
        name=name,
        description=description or name,
        input_schema=params,
        category=category,
        stateful=name in STATEFUL_TOOL_NAMES,
        annotations=_default_annotations(name, category),
    )


def _guess_category(name: str) -> str:
    if name in {"todo"}:
        return "task"
    if name in {"memory", "skill", "subagent"}:
        return "agent"
    if name in {"bash", "text_editor", "present_files"}:
        return "workspace"
    return "search"


def _default_annotations(name: str, category: str) -> dict[str, Any]:
    read_only = name in {
        "web_search",
        "fetch_url",
        "wikipedia",
        "exchange_rate",
        "hacker_news",
        "book_lookup",
        "weather",
        "news",
        "crypto_price",
        "ip_geo",
        "qr_code",
        "image_search",
        "geocode",
        "search_poi",
        "route",
        "distance",
        "place_details",
        "elevation",
        "traffic",
        "isochrone",
    }
    destructive = name in {"bash", "text_editor", "todo", "memory", "skill", "subagent"}
    open_world = name in {
        "web_search",
        "fetch_url",
        "wikipedia",
        "exchange_rate",
        "hacker_news",
        "book_lookup",
        "weather",
        "news",
        "crypto_price",
        "ip_geo",
        "image_search",
        "geocode",
        "search_poi",
        "route",
        "distance",
        "place_details",
        "elevation",
        "traffic",
        "isochrone",
        "bash",
        "text_editor",
        "present_files",
        "subagent",
    }
    return {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": read_only and not destructive,
        "openWorldHint": open_world,
        "title": name,
        "category": category,
    }


def _with_workspace_id(schema: dict[str, Any], stateful: bool) -> dict[str, Any]:
    out = _safe_copy(schema)
    if not stateful:
        return out
    if out.get("type") != "object":
        out = {"type": "object", "properties": {}}
    properties = dict(out.get("properties") or {})
    properties["workspace_id"] = {
        "type": "string",
        "description": (
            "MCP 工作区标识。留空时使用默认工作区；"
            "同一个 workspace_id 会复用同一份 todos / memories / skills / 文件编辑状态。"
        ),
    }
    out["properties"] = properties
    required = [item for item in out.get("required", []) if item != "workspace_id"]
    out["required"] = required
    return out


def _load_tool_defs() -> list[dict[str, Any]]:
    tool_defs: list[dict[str, Any]] = []

    try:
        from search_engine import SEARCH_TOOLS
        tool_defs.extend([_safe_copy(item) for item in SEARCH_TOOLS])
    except Exception as exc:
        logger.warning("无法加载 SEARCH_TOOLS: %s", exc)

    for module_name, attr_name in [
        ("todo_tool", "TODO_TOOL"),
        ("memory_tool", "MEMORY_TOOL"),
        ("skill_tool", "SKILL_TOOL"),
        ("subagent_tool", "SUBAGENT_TOOL"),
    ]:
        try:
            module = __import__(module_name, fromlist=[attr_name])
            tool_defs.append(_safe_copy(getattr(module, attr_name)))
        except Exception as exc:
            logger.warning("无法加载 %s.%s: %s", module_name, attr_name, exc)

    return tool_defs


def get_tool_catalog() -> list[ToolCatalogEntry]:
    return [_normalize_tool_def(item) for item in _load_tool_defs()]


def get_tool_catalog_json() -> dict[str, Any]:
    catalog = get_tool_catalog()
    return {
        "protocol": "MCP",
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "name": "apitelegramchat",
        "description": "Telegram AI Assistant 工具集的 MCP 暴露层。",
        "statefulToolNames": sorted(STATEFUL_TOOL_NAMES),
        "tools": [entry.to_mcp_tool() for entry in catalog],
        "notes": [
            "州性工具可以传 workspace_id 来隔离会话；不传则使用默认工作区。",
            "当前实现采用 stdio 传输，适合 Claude Desktop、MCP Inspector 等本地客户端。",
            "所有原有工具仍由项目内的现有实现执行，MCP 仅作为标准化入口。",
        ],
    }


def get_project_brief_prompt(workspace_id: str | None = None) -> str:
    workspace_clause = f"当前工作区标识：{workspace_id}。" if workspace_id else "未指定 workspace_id，使用默认工作区。"
    return (
        "你正在与 apitelegramchat 的 MCP 服务器交互。\n"
        "可用工具包括搜索、抓取、待办、记忆、技能、子 agent、bash 和 text_editor。\n"
        "遇到会改变状态的工具（todo / memory / skill / subagent / bash / text_editor / present_files），"
        "请尽量携带 workspace_id，以免与其他会话混淆。\n"
        f"{workspace_clause}\n"
        "建议先调用 tools/list 了解最新工具定义，再按需要调用 tools/call。"
    )


def is_stateful_tool(name: str) -> bool:
    return name in STATEFUL_TOOL_NAMES


def augment_arguments(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    args = dict(arguments or {})
    if is_stateful_tool(tool_name) and not args.get("workspace_id"):
        args["workspace_id"] = "default"
    return args
