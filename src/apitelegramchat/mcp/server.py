from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from .prompts import list_prompts, get_prompt
from .resources import list_resources, read_resource
from .registry import call_tool, list_tools
from workspace_paths import data_root

logger = logging.getLogger("apitelegramchat.mcp")


async def handle_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    req_id = msg.get("id")
    if method is None or req_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "serverInfo": {"name": "apitelegramchat", "version": "2.1.0-mcp-native"},
                "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}, "resources": {"listChanged": False, "subscribe": False}},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": await list_tools()}}
    if method == "tools/call":
        params = msg.get("params", {}) or {}
        result = await call_tool(params.get("name", ""), params.get("arguments", {}) or {})
        if "error" in result:
            return {"jsonrpc": "2.0", "id": req_id, "error": result["error"]}
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": await list_prompts()}}
    if method == "prompts/get":
        params = msg.get("params", {}) or {}
        result = await get_prompt(params.get("name", ""), params.get("arguments", {}) or {})
        if "error" in result:
            return {"jsonrpc": "2.0", "id": req_id, "error": result["error"]}
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": await list_resources()}}
    if method == "resources/read":
        params = msg.get("params", {}) or {}
        result = await read_resource(params.get("uri", ""))
        if "error" in result:
            return {"jsonrpc": "2.0", "id": req_id, "error": result["error"]}
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


async def run_stdio() -> None:
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    transport, _ = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    while True:
        line = await reader.readline()
        if not line:
            break
        raw = line.decode("utf-8", errors="ignore").strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        response = await handle_message(msg)
        if response is not None:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting MCP server data_root=%s", data_root())
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
