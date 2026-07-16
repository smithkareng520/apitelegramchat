
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from typing import Any

from tool_catalog import (
    MCP_PROTOCOL_VERSION,
    PROMPT_NAME_PROJECT_BRIEF,
    RESOURCE_URI_TOOL_CATALOG,
    augment_arguments,
    get_project_brief_prompt,
    get_tool_catalog,
    get_tool_catalog_json,
    is_stateful_tool,
)

logger = logging.getLogger(__name__)

try:
    from tool_executors import dispatch_tool_call
except Exception as exc:  # pragma: no cover - runtime fallback
    dispatch_tool_call = None
    logger.warning("无法导入 dispatch_tool_call: %s", exc)

SERVER_NAME = "apitelegramchat-mcp"
SERVER_VERSION = "1.0.0"


def _jsonrpc_response(req_id: Any, result: Any | None = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {}
    return payload


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return _jsonrpc_response(req_id, error=err)


def _content_text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _stable_workspace_id(workspace_id: str | None) -> int:
    seed = (workspace_id or "default").strip() or "default"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 2_000_000_000


async def _call_project_tool(name: str, arguments: dict[str, Any]) -> str:
    if dispatch_tool_call is None:
        return f"Error: dispatch_tool_call 不可用，无法执行工具 {name}。"

    args = augment_arguments(name, arguments)
    workspace_id = str(args.pop("workspace_id", "default") or "default")
    chat_id = _stable_workspace_id(workspace_id)

    # 兼容现有执行器：它们仍然以 chat_id 作为分区键。
    return await dispatch_tool_call(name, args, chat_id=chat_id)


def _build_tools_payload() -> list[dict[str, Any]]:
    tools = []
    for entry in get_tool_catalog():
        tools.append(entry.to_mcp_tool())
    return tools


def _build_resources_payload() -> list[dict[str, Any]]:
    return [
        {
            "uri": RESOURCE_URI_TOOL_CATALOG,
            "name": "Tool Catalog",
            "description": "apitelegramchat 当前所有 MCP 工具的结构化目录。",
            "mimeType": "application/json",
        }
    ]


def _build_prompts_payload() -> list[dict[str, Any]]:
    return [
        {
            "name": PROMPT_NAME_PROJECT_BRIEF,
            "description": "生成一段项目 / 工具使用提示，适合放进系统提示词。",
            "arguments": [
                {
                    "name": "workspace_id",
                    "description": "可选的 MCP 工作区标识",
                    "required": False,
                }
            ],
        }
    ]


async def handle_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(msg, dict):
        return None

    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # notifications have no id
    is_notification = req_id is None

    try:
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            }
            return None if is_notification else _jsonrpc_response(req_id, result)

        if method == "initialized":
            return None

        if method == "shutdown":
            return None if is_notification else _jsonrpc_response(req_id, {})

        if method in {"exit", "notifications/exit"}:
            return None

        if method == "ping":
            return None if is_notification else _jsonrpc_response(req_id, {"pong": True})

        if method == "tools/list":
            result = {"tools": _build_tools_payload()}
            return None if is_notification else _jsonrpc_response(req_id, result)

        if method == "tools/call":
            tool_name = str(params.get("name", "")).strip()
            arguments = params.get("arguments") or {}
            if not tool_name:
                return _jsonrpc_error(req_id, -32602, "Missing tool name")
            if not isinstance(arguments, dict):
                return _jsonrpc_error(req_id, -32602, "arguments must be an object")

            text = await _call_project_tool(tool_name, arguments)
            result = {
                "content": [_content_text(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False))],
                "isError": False,
            }
            return None if is_notification else _jsonrpc_response(req_id, result)

        if method == "resources/list":
            result = {"resources": _build_resources_payload()}
            return None if is_notification else _jsonrpc_response(req_id, result)

        if method == "resources/read":
            uri = str(params.get("uri", "")).strip()
            if uri == RESOURCE_URI_TOOL_CATALOG:
                payload = get_tool_catalog_json()
                result = {
                    "contents": [
                        {
                            "uri": RESOURCE_URI_TOOL_CATALOG,
                            "mimeType": "application/json",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2),
                        }
                    ]
                }
                return None if is_notification else _jsonrpc_response(req_id, result)
            return _jsonrpc_error(req_id, -32602, f"Unknown resource: {uri}")

        if method == "prompts/list":
            result = {"prompts": _build_prompts_payload()}
            return None if is_notification else _jsonrpc_response(req_id, result)

        if method == "prompts/get":
            prompt_name = str(params.get("name", "")).strip()
            if prompt_name != PROMPT_NAME_PROJECT_BRIEF:
                return _jsonrpc_error(req_id, -32602, f"Unknown prompt: {prompt_name}")
            workspace_id = params.get("arguments", {}).get("workspace_id") if isinstance(params.get("arguments"), dict) else None
            text = get_project_brief_prompt(workspace_id)
            result = {
                "messages": [
                    {
                        "role": "system",
                        "content": {"type": "text", "text": text},
                    }
                ]
            }
            return None if is_notification else _jsonrpc_response(req_id, result)

        if method in {"notifications/initialized"}:
            return None

        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("处理 MCP 消息失败: %s", exc)
        return _jsonrpc_error(req_id, -32603, "Internal error", data=str(exc))


async def serve_stdio() -> None:
    logger.info("Starting MCP stdio server: %s", SERVER_NAME)
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            break
        try:
            msg = json.loads(line.decode("utf-8"))
        except Exception as exc:
            logger.exception("无法解析 MCP 消息: %s", exc)
            continue

        response = await handle_message(msg)
        if response is None:
            continue

        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
        sys.stdout.write(payload)
        sys.stdout.flush()


def main() -> None:
    try:
        asyncio.run(serve_stdio())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
