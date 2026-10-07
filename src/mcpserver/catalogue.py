"""MCP 原生工具目录 —— 内部工具的单一 schema 数据源。

设计
----
每个内部模块（search / todo / memory / workspace / bash）在 ``MODULES``
里声明自己的 MCP 工具：名称、描述、JSON Schema 与 handler。host 端不再
维护第二份本地 schema —— 工具定义经 ``list_tools`` 从服务器发现，模型看
到的名字统一是 ``mcp__<server>__<tool>``。

handler 一律在函数体内懒加载执行器模块，保证
``python3 -m mcpserver.server --module todo`` 这类单模块子进程只 import
自己需要的那一小块（mcp SDK + 该模块执行器），不被 host 的重型依赖
（openai / quart / anthropic …）拖累。

所有 handler 在 ``ToolRegistry.call`` 里于 ``context.activate()`` 之下执行：
scope 即命名空间，todo / memory / workspace / bash 的路径解析与 host 进程
完全一致。
"""
from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import mcp.types as types

from mcpserver.context import (
    MCPConfigurationError,
    MCPRequestContext,
    mutations_are_explicitly_enabled,
)

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]
ToolHandler = Callable[[MCPRequestContext, JsonObject], Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    """一个 MCP 工具的完整声明（MCP 原生形态，非 OpenAI 包装）。"""

    name: str
    description: str
    input_schema: JsonObject
    handler: ToolHandler
    input_examples: tuple[JsonObject, ...] = field(default=())

    def as_mcp_tool(self) -> types.Tool:
        tool = types.Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.input_schema,
        )
        if self.input_examples:
            try:
                tool.meta = {"input_examples": list(self.input_examples)}
            except Exception:  # pragma: no cover - meta 非关键路径
                pass
        return tool


@dataclass(frozen=True)
class ModuleSpec:
    """一个内部 MCP 模块 = 一个可独立启动的 stdio 服务器。"""

    name: str
    title: str
    tools: tuple[ToolSpec, ...]
    # mutating 模块需要显式 opt-in（host 自身拉起时会设置对应环境变量）。
    mutating: bool = False


def _text(description: str, min_length: int | None = None) -> JsonObject:
    field_: JsonObject = {"type": "string", "description": description}
    if min_length is not None:
        field_["minLength"] = min_length
    return field_


def _int(description: str, minimum: int | None = None, maximum: int | None = None) -> JsonObject:
    field_: JsonObject = {"type": "integer", "description": description}
    if minimum is not None:
        field_["minimum"] = minimum
    if maximum is not None:
        field_["maximum"] = maximum
    return field_


def _schema(properties: JsonObject, required: tuple[str, ...] = ()) -> JsonObject:
    schema: JsonObject = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


async def _invoke(function: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    result = function(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


# search 模块
async def _web_search(ctx: MCPRequestContext, args: JsonObject) -> str:
    from search.serper import execute_web_search

    return await _invoke(
        execute_web_search,
        args.get("query"),
        args.get("num_results"),
        args.get("offset"),
        mode=args.get("mode", "search"),
        image_url=args.get("image_url"),
        gl=args.get("gl"),
        hl=args.get("hl"),
        tbs=args.get("tbs"),
    )


async def _fetch_url(ctx: MCPRequestContext, args: JsonObject) -> str:
    from search.fetch_url import execute_fetch_url

    return await _invoke(execute_fetch_url, args.get("url", ""))


async def _wikipedia(ctx: MCPRequestContext, args: JsonObject) -> str:
    from search.quick_lookup import execute_wikipedia

    return await _invoke(execute_wikipedia, args.get("query", ""), args.get("lang", "zh"))


async def _exchange_rate(ctx: MCPRequestContext, args: JsonObject) -> str:
    from search.quick_lookup import execute_exchange_rate

    return await _invoke(execute_exchange_rate, args.get("base", "USD"), args.get("target"))


async def _weather(ctx: MCPRequestContext, args: JsonObject) -> str:
    from search.quick_lookup import execute_weather

    return await _invoke(execute_weather, args.get("city", ""), args.get("unit", "c"), args.get("hours", 6))


SEARCH_MODULE = ModuleSpec(
    name="search",
    title="Web search & lookup",
    tools=(
        ToolSpec(
            name="web_search",
            description=(
                "Search the web, images, videos, or reverse-search an image. "
                "Set `mode` to one mode or a list of modes; use `image_url` for lens. "
                "Use `fetch_url` to read a specific result in depth."
            ),
            input_schema=_schema(
                {
                    "query": _text("Search query. Required for search/images/videos; optional for lens.", 1),
                    "mode": {
                        "type": ["string", "array"],
                        "items": {"type": "string", "enum": ["search", "images", "videos", "lens"]},
                        "minItems": 1,
                        "maxItems": 4,
                        "description": "Search mode(s). Default: search. A list runs multiple modes in one call.",
                        "default": "search",
                    },
                    "image_url": _text("Image URL for lens mode. Ignored by other modes.", 1),
                    "num_results": _int("Maximum results per mode. search: 1-20; other modes: 1-100. Default: 8.", 1, 100),
                    "offset": _int("Result offset for search mode; ignored by other modes.", 0),
                    "gl": _text("Region code, e.g. `us` or `cn`. Default: `cn`."),
                    "hl": _text("Interface language, e.g. `en` or `zh-cn`. Default: `zh-cn`."),
                    "tbs": _text("Time filter, e.g. `qdr:h`, `qdr:d`, `qdr:w`, `qdr:m`, or `qdr:y`."),
                },
            ),
            handler=_web_search,
            input_examples=(
                {"query": "2024 诺贝尔物理学奖 获奖者", "num_results": 5},
                {"query": "球球大作战 官网", "mode": "images", "num_results": 8},
                {"query": "特斯拉 model y", "mode": ["search", "images", "videos"], "num_results": 5},
            ),
        ),
        ToolSpec(
            name="fetch_url",
            description=(
                "Fetch and read a specific URL. Use for a user-provided link or a search result in depth. "
                "One URL per call; rich HTML preserves page structure and embedded media."
            ),
            input_schema=_schema({"url": _text("Full URL, including scheme.", 1)}, ("url",)),
            handler=_fetch_url,
        ),
        ToolSpec(
            name="wikipedia",
            description="Look up a topic on Wikipedia by keyword. Use for encyclopedic, factual, or definitional questions.",
            input_schema=_schema(
                {
                    "query": _text("Page title or keyword.", 1),
                    "lang": {"type": "string", "enum": ["zh", "en"], "description": "Wikipedia language. Default: `zh`.", "default": "zh"},
                },
                ("query",),
            ),
            handler=_wikipedia,
        ),
        ToolSpec(
            name="exchange_rate",
            description="Get real-time exchange rates for a base currency. Optionally filter to a single target currency.",
            input_schema=_schema(
                {
                    "base": _text("Base currency code, e.g. `USD`.", 3),
                    "target": _text("Optional target currency code, e.g. `CNY`.", 3),
                },
                ("base",),
            ),
            handler=_exchange_rate,
        ),
        ToolSpec(
            name="weather",
            description=(
                "Get current weather and forecasts for a city. `unit` controls temperature units; "
                "`hours` controls hourly forecast length."
            ),
            input_schema=_schema(
                {
                    "city": _text("City name.", 1),
                    "unit": {"type": "string", "enum": ["c", "f"], "description": "Temperature unit. Default: Celsius (`c`).", "default": "c"},
                    "hours": _int("Hourly forecast entries. Default: 6.", 1, 24),
                },
                ("city",),
            ),
            handler=_weather,
            input_examples=(
                {"city": "Beijing", "unit": "c", "hours": 12},
                {"city": "New York", "unit": "f"},
            ),
        ),
    ),
)


# todo / memory 模块
async def _todo(ctx: MCPRequestContext, args: JsonObject) -> str:
    from todo_tool import execute_todo

    return await _invoke(execute_todo, ctx.chat_id, **args)


async def _memory(ctx: MCPRequestContext, args: JsonObject) -> str:
    from memory_tool import execute_memory

    return await _invoke(execute_memory, ctx.chat_id, **args)


TODO_MODULE = ModuleSpec(
    name="todo",
    title="Persistent todos",
    mutating=True,
    tools=(
        ToolSpec(
            name="todo",
            description=(
                "Persistent per-chat todo list. Actions: add, list, done, undone, toggle, delete, clear, edit. "
                "Use `due_at` for deadlines. After a write action, call `list` to verify the updated state."
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list", "done", "undone", "toggle", "delete", "clear", "edit"],
                        "description": "Action to perform. Default: `list`.",
                        "default": "list",
                    },
                    "title": _text("Todo title. Required for `add`; optional for `edit`.", 1),
                    "todo_id": _text("Target todo id for `done`, `undone`, `toggle`, `delete`, or `edit`.", 1),
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Priority. Default: `medium`.",
                        "default": "medium",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags; up to 8.", "maxItems": 8},
                    "note": _text("Optional note for `add` or `edit`."),
                    "due_at": _text("Optional ISO 8601 deadline for `add` or `edit`."),
                    "filter": {
                        "type": "string",
                        "enum": ["all", "pending", "done"],
                        "description": "Filter for `list`/`clear`. Default: `all`.",
                        "default": "all",
                    },
                    "tag": _text("Filter `list` by tag, or scope `clear` to a tag."),
                },
                (),
            ),
            handler=_todo,
            input_examples=(
                {"action": "add", "title": "归还图书馆书籍", "priority": "high", "due_at": "2026-10-08"},
                {"action": "list", "filter": "pending"},
                {"action": "done", "todo_id": "a1b2c3d4"},
            ),
        ),
    ),
)

MEMORY_MODULE = ModuleSpec(
    name="memory",
    title="Long-term memory",
    mutating=True,
    tools=(
        ToolSpec(
            name="memory",
            description=(
                "Persistent per-chat long-term memory. Actions: add, get, list, search, update, delete, clear. "
                "Use for facts, preferences, people, events, or notes that should survive across sessions."
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": ["add", "get", "list", "search", "update", "delete", "clear"],
                        "description": "Action to perform. Default: `list`.",
                        "default": "list",
                    },
                    "content": _text("Memory content. Required for `add`/`update`.", 1),
                    "memory_id": _text("Memory id for `get`, `update`, or `delete`.", 1),
                    "category": _text("Category, such as `fact`, `preference`, `person`, `event`, or `note`.", 1),
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags; up to 8.", "maxItems": 8},
                    "importance": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Importance. Default: `medium`.",
                        "default": "medium",
                    },
                    "query": _text("Search query for `search`; matches content, tags, and category.", 1),
                    "scope": _text("Clear scope: `all`, `category:<name>`, or `tag:<name>`. Default: `all`."),
                    "limit": _int("Maximum results for `list`/`search`. Default: 50.", 1, 500),
                    "source": _text("Memory source. Default: `agent`."),
                },
                ("action",),
            ),
            handler=_memory,
            input_examples=(
                {"action": "add", "content": "用户对花生过敏", "category": "fact", "importance": "high", "tags": ["健康", "过敏"]},
                {"action": "search", "query": "过敏"},
                {"action": "update", "memory_id": "a1b2c3d4", "content": "用户对花生和海鲜过敏", "importance": "high"},
            ),
        ),
    ),
)


# workspace / bash 模块
async def _text_editor(ctx: MCPRequestContext, args: JsonObject) -> str:
    from search.text_editor import execute_text_editor

    return await _invoke(
        execute_text_editor,
        chat_id=ctx.chat_id,
        namespace=ctx.scope,
        command=args.get("command", ""),
        path=args.get("path", ""),
        view_range=args.get("view_range"),
        old_str=args.get("old_str"),
        new_str=args.get("new_str"),
        insert_line=args.get("insert_line"),
        insert_text=args.get("insert_text"),
        file_text=args.get("file_text"),
    )


TEXT_EDITOR_MODULE = ModuleSpec(
    name="workspace",
    title="Workspace text editor",
    mutating=True,
    tools=(
        ToolSpec(
            name="text_editor",
            description=(
                "View or edit UTF-8 text files in the workspace. "
                "Commands: `view`, `str_replace`, `create`, `insert`. "
                "View immediately before editing; `str_replace` requires exactly one match."
            ),
            input_schema=_schema(
                {
                    "command": {
                        "type": "string",
                        "enum": ["view", "str_replace", "create", "insert"],
                        "description": "The text-editor operation to perform: view, str_replace, create, or insert.",
                    },
                    "path": _text("Workspace path, or `.` for the workspace root.", 1),
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "For `view`: `[start_line, end_line]`; lines start at 1, and `-1` means end.",
                    },
                    "old_str": _text("For `str_replace`: exact existing text; it must occur exactly once."),
                    "new_str": _text("Replacement text for `str_replace`, or alternate insert text for `insert`."),
                    "file_text": _text("For `create`: complete initial file content; may be empty."),
                    "insert_line": _int("For `insert`: insert after this line; `0` inserts at the beginning.", 0),
                    "insert_text": _text("For `insert`: text to add after `insert_line`; use `new_str` instead if preferred."),
                },
                ("command", "path"),
            ),
            handler=_text_editor,
        ),
    ),
)


async def _bash(ctx: MCPRequestContext, args: JsonObject) -> str:
    from bash_session import execute_bash

    return await _invoke(
        execute_bash,
        chat_id=ctx.chat_id,
        namespace=ctx.scope,
        command=args.get("command") or "",
        restart=bool(args.get("restart", False)),
        timeout=args.get("timeout"),
        run_in_background=bool(args.get("run_in_background", False)),
        task_action=args.get("task_action"),
        task_id=args.get("task_id"),
        description=str(args.get("description") or ""),
    )


BASH_MODULE = ModuleSpec(
    name="bash",
    title="Sandboxed bash session",
    mutating=True,
    tools=(
        ToolSpec(
            name="bash",
            description=(
                "Run non-interactive bash commands in the per-session workspace. "
                "CWD starts at `$HOME` / `$WORKSPACE`; do not leave the workspace or use `/tmp`. "
                "Use `download/` for user uploads, `upload/` for files to present, and `.runtime/` only for temporary files. "
                "Avoid interactive programs and daemons; use `restart=true` for a stuck session."
            ),
            input_schema=_schema(
                {
                    "description": _text("Required progress label: one short sentence explaining the command's purpose.", 1),
                    "command": _text("Bash command. Required unless restarting or using `task_action`."),
                    "restart": {"type": "boolean", "description": "Reset the bash session before doing anything else.", "default": False},
                    "timeout": _int("Foreground timeout in seconds. Default: 300; use for expected long quiet commands.", 5, 600),
                    "run_in_background": {"type": "boolean", "description": "Run as a background task and return its task id immediately.", "default": False},
                    "task_action": {
                        "type": "string",
                        "enum": ["status", "output", "stop", "list"],
                        "description": "`status`/`output`/`stop` need `task_id`; `list` does not. Do not combine with `command` or `run_in_background`.",
                    },
                    "task_id": _text("Background task id for `status`, `output`, or `stop`."),
                },
                ("description",),
            ),
            handler=_bash,
            input_examples=(
                {"description": "查看项目文件列表", "command": "ls -la"},
                {"description": "安装依赖并运行测试", "command": "pip install --user pytest && python3 -m pytest -q"},
                {"description": "重启卡死的会话", "restart": True},
            ),
        ),
    ),
)


# 模块注册表
MODULES: dict[str, ModuleSpec] = {
    spec.name: spec
    for spec in (SEARCH_MODULE, TODO_MODULE, MEMORY_MODULE, TEXT_EDITOR_MODULE, BASH_MODULE)
}


def tools_for_modules(module_names: list[str], *, allow_mutations: bool | None = None) -> list[ToolSpec]:
    """收集模块工具；mutating 模块需显式 opt-in.

    ``allow_mutations`` is reserved for the trusted host in-process MCP path;
    standalone stdio servers continue to use the environment capability.
    """
    specs: list[ToolSpec] = []
    mutations_enabled = (
        mutations_are_explicitly_enabled()
        if allow_mutations is None
        else bool(allow_mutations)
    )
    for name in module_names:
        spec = MODULES.get(name)
        if spec is None:
            raise MCPConfigurationError(f"Unknown MCP tool module: {name}")
        if spec.mutating and not mutations_enabled:
            logger.warning("Module %s is mutating but mutations are not enabled; skipped", name)
            continue
        specs.extend(spec.tools)
    return specs


class ToolRegistry:
    """为一个受信 scope 暴露确定性、最小权限的 MCP 工具集。"""

    def __init__(self, module_names: list[str], *, allow_mutations: bool | None = None) -> None:
        self._specs = tuple(tools_for_modules(module_names, allow_mutations=allow_mutations))
        self._by_name = {spec.name: spec for spec in self._specs}

    async def list_tools(self) -> list[types.Tool]:
        return [spec.as_mcp_tool() for spec in self._specs]

    async def call(self, name: str, arguments: JsonObject, context: MCPRequestContext) -> types.CallToolResult:
        spec = self._by_name.get(name)
        if spec is None:
            return _error_result(f"Unknown or disabled tool: {name}")
        if not isinstance(arguments, dict):
            return _error_result("Tool arguments must be an object")
        try:
            with context.activate():
                text = await spec.handler(context, arguments)
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=False)
        except Exception as exc:
            logger.exception("MCP tool execution failed: %s", name)
            # 异常类型 + 简短 message 放进 error text，便于调用方定位；
            # 不放完整 traceback（含敏感字段）。
            return _error_result(f"Tool execution failed: {type(exc).__name__}: {exc}")


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)
