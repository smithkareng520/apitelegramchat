"""Official-SDK MCP stdio server —— 按模块启动的内部工具服务器。

用法（mcp.json 中注册的正是这些命令）::

    python3 -m mcpserver.server --module search
    python3 -m mcpserver.server --module todo
    python3 -m mcpserver.server --module memory
    python3 -m mcpserver.server --module workspace
    python3 -m mcpserver.server --module bash
    python3 -m mcpserver.server --module all      # 全部模块（外部客户端调试用）

启动前必须通过 ``APITELEGRAMCHAT_MCP_SCOPE`` 提供受信 scope（即该 chat 的
状态命名空间）；host 逐 chat 拉起本服务器时自动注入。 ``--module all`` 额外
暴露 workspace 资源（skills / todos / memories 元数据），供 Claude Desktop
这类外部 MCP 客户端浏览；按模块启动的精简进程不带资源服务，保持轻量。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server

from mcpserver.catalogue import MODULES, ToolRegistry
from mcpserver.context import MCPConfigurationError, MCPRequestContext
from workspace_paths import data_root

logger = logging.getLogger(__name__)
SERVER_NAME = "apitelegramchat"
from version import __version__ as SERVER_VERSION  # noqa: E402  包元数据唯一来源

def create_server(context: MCPRequestContext, module_names: list[str], *, with_resources: bool) -> Server:
    """Create a stdio SDK server for one trusted local scope."""
    tools = ToolRegistry(module_names)
    server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def list_tools() -> Any:
        return await tools.list_tools()

    @server.call_tool(validate_input=True)
    async def call_tool(name: str, arguments: dict) -> Any:
        return await tools.call(name, arguments, context)

    if with_resources:
        from mcpserver.resources import ResourceService

        resources = ResourceService(context)

        @server.list_resources()
        async def list_resources() -> Any:
            return await resources.list_resources()

        @server.read_resource()
        async def read_resource(uri: Any) -> Any:
            return await resources.read_resource(str(uri))

    return server

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mcpserver.server", description="apitelegramchat internal MCP tools")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--module",
        action="append",
        dest="modules",
        choices=sorted(MODULES) + ["all"],
        help="Tool module(s) to expose; repeatable. Default: all.",
    )
    group.add_argument("--modules", dest="modules", nargs="+", help="Alias of --module (accepts multiple values).")
    return parser.parse_args(argv)

async def run_stdio(module_names: list[str], *, with_resources: bool) -> None:
    """Run a single local MCP connection over SDK-managed stdio transport."""
    context = MCPRequestContext.from_environment()
    server = create_server(context, module_names, with_resources=with_resources)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=False,
        )

def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    args = _parse_args(argv)
    raw_modules = args.modules or ["all"]
    module_names = sorted(MODULES) if "all" in raw_modules else sorted({m for m in raw_modules})
    with_resources = "all" in raw_modules
    try:
        context_probe = MCPRequestContext.from_environment()
    except MCPConfigurationError as exc:
        logger.error("MCP server refused to start: %s", exc)
        raise SystemExit(2) from exc
    logger.info(
        "Starting MCP server modules=%s scope=%s data_root=%s",
        ",".join(module_names),
        context_probe.scope[:8],
        data_root(),
    )
    try:
        asyncio.run(run_stdio(module_names, with_resources=with_resources))
    except KeyboardInterrupt:
        logger.info("MCP server stopped by signal")

if __name__ == "__main__":
    main()
