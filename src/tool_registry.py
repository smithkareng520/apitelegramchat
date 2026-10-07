# tool_registry.py — 模型视角工具清单的统一装配。
# 工具分两类：
# 1. host 内建工具（不经 MCP）：message_user / deliver_reply / subagent /
# generate_image / generate_video / present_files —— 依赖宿主进程的
# Telegram 会话、草稿流与 LLM 编排，等价于 Claude Code 的内建工具；
# 2. MCP 工具：来自 mcp.json 注册的服务器，模型视角名
# ``mcp__<server>__<tool>``，schema 经 list_tools 协议发现（内部 in_process/stdio 服务器的目录与本进程
# 共享同一份 mcpserver.catalogue，外部 streamable_http 服务器走真实协议发现 + TTL 缓存）。
# 本模块是 SEARCH_TOOLS 时代之后的单一工具面入口：主 agent、TIMER 工具面、
# 子 agent 白名单、各协议桥接层都从这里取工具定义。
from __future__ import annotations

import logging
from typing import Any

from mcp_manager import mcp_manager

logger = logging.getLogger(__name__)

_tools_cache: list[dict] | None = None

def _media_tool_defs() -> list[dict]:
    """图像 / 视频生成工具（依赖模型目录，按可用性裁剪）。"""
    from search.tool_schemas import build_media_tool_defs

    return build_media_tool_defs()

def builtin_tool_defs() -> list[dict]:
    """host 内建工具定义（顺序稳定）。"""
    from file_delivery import PRESENT_FILES_TOOL
    from message_user_tool import MESSAGE_USER_TOOL
    from search.tool_schemas import build_deliver_reply_tool
    from subagent_tool import SUBAGENT_TOOL

    defs: list[dict] = [
        MESSAGE_USER_TOOL,
        SUBAGENT_TOOL,
        *_media_tool_defs(),
        PRESENT_FILES_TOOL,
    ]
    # deliver_reply 按回合类型追加（见 ai_handlers）；这里不进入默认面。
    del build_deliver_reply_tool
    return defs

def _catalogue_defs(server_name: str) -> list[dict] | None:
    """受信内部 MCP：直接从共享目录读取工具定义，免 transport 探测。"""

    import mcpserver.catalogue as catalogue

    server = mcp_manager.servers.get(server_name)
    if server is None or server.type not in {"stdio", "in_process"}:
        return None
    module_names = [server.module] if server.type == "in_process" and server.module else _modules_of(server)
    disabled = mcp_manager._dynamic_disabled(server_name)
    defs: list[dict] = []
    for module_name in module_names:
        spec = catalogue.MODULES.get(module_name)
        if spec is None:
            logger.warning("mcp.json internal server %s references unknown module %s", server_name, module_name)
            continue
        for tool_spec in spec.tools:
            if tool_spec.name in disabled or not server.exposes_tool(tool_spec.name):
                continue
            function: dict = {
                "name": mcp_manager._full_name(server_name, tool_spec.name),
                "description": tool_spec.description,
                "parameters": tool_spec.input_schema,
            }
            if tool_spec.input_examples:
                function["input_examples"] = list(tool_spec.input_examples)
            defs.append({"type": "function", "function": function})
    return defs

def _modules_of(server: Any) -> list[str]:
    """从 stdio 启动参数中解析 --module（支持重复与多值）。"""
    modules: list[str] = []
    args = list(server.args)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--module" or arg == "--modules":
            if index + 1 < len(args):
                modules.append(str(args[index + 1]))
                index += 2
                continue
        if arg.startswith("--module="):
            modules.append(arg.split("=", 1)[1])
        index += 1
    if not modules or "all" in modules:
        from mcpserver.catalogue import MODULES

        return sorted(MODULES)
    return modules

async def mcp_tool_defs() -> list[dict]:
    """全部已暴露 MCP 工具的模型视角定义（顺序 = mcp.json 声明序）。"""
    defs: list[dict] = []
    for server_name in mcp_manager.servers:
        catalogue_defs = _catalogue_defs(server_name)
        if catalogue_defs is not None:
            defs.extend(catalogue_defs)
        else:
            defs.extend(await mcp_manager.list_server_tools(server_name))
    return defs

async def get_model_tools() -> list[dict]:
    """默认（USER / 草稿模式）回合的完整模型工具面。"""
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache
    defs = [*builtin_tool_defs(), *await mcp_tool_defs()]
    _tools_cache = defs
    return defs
