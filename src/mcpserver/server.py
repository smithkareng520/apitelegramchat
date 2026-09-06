"""Official-SDK MCP stdio server adapter."""
from __future__ import annotations

import asyncio
import logging
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server

from mcpserver.context import MCPConfigurationError, MCPRequestContext
from mcpserver.registry import ToolRegistry
from mcpserver.resources import ResourceService
from workspace_paths import data_root

logger = logging.getLogger(__name__)
SERVER_NAME = "apitelegramchat"
# 引用包 __version__ 作为唯一来源，避免与 __init__.py 不同步。
from version import __version__ as SERVER_VERSION


def create_server(context: MCPRequestContext) -> Server:
    """Create a legacy-compatible SDK server for one trusted local scope."""
    tools = ToolRegistry(context)
    resources = ResourceService(context)
    server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def list_tools():
        return await tools.list_tools()

    @server.call_tool(validate_input=True)
    async def call_tool(name: str, arguments: dict):
        return await tools.call(name, arguments)

    @server.list_resources()
    async def list_resources():
        return await resources.list_resources()

    @server.read_resource()
    async def read_resource(uri):
        return await resources.read_resource(str(uri))

    return server


async def run_stdio() -> None:
    """Run a single local MCP connection over SDK-managed stdio transport."""
    context = MCPRequestContext.from_environment()
    server = create_server(context)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=False,
        )


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        context = MCPRequestContext.from_environment()
    except MCPConfigurationError as exc:
        logger.error("MCP server refused to start: %s", exc)
        raise SystemExit(2) from exc
    logger.info("Starting MCP server scope_fingerprint=%s data_root=%s", context.scope[:8], data_root())
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
