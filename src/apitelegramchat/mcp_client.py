"""Hardened client for explicitly trusted external MCP servers."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("apitelegramchat.mcp_client")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    _MCP_SDK_AVAILABLE = True
except Exception as exc:  # pragma: no cover - optional deployment dependency
    ClientSession = None  # type: ignore[assignment]
    streamablehttp_client = None  # type: ignore[assignment]
    _MCP_SDK_AVAILABLE = False
    logger.warning("MCP SDK unavailable; external MCP calls are disabled: %s", exc)


class MCPToolError(RuntimeError):
    """A controlled external MCP connection or execution failure."""


@dataclass(frozen=True)
class MCPServerConfig:
    """A single externally trusted, TLS-protected MCP endpoint."""

    name: str
    url: str
    allowed_hosts: frozenset[str]
    allowed_tools: frozenset[str]
    headers: dict[str, str] = field(default_factory=dict, compare=False, repr=False)
    timeout: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or not parsed.path:
            raise ValueError(f"{self.name}: external MCP endpoint must be an HTTPS URL")
        if host not in {item.lower() for item in self.allowed_hosts}:
            raise ValueError(f"{self.name}: endpoint host is not allowlisted")
        if not self.allowed_tools:
            raise ValueError(f"{self.name}: at least one allowed tool is required")
        if not 1 <= self.timeout <= 60:
            raise ValueError(f"{self.name}: timeout must be between 1 and 60 seconds")


def _configured_hosts(variable: str, defaults: set[str]) -> frozenset[str]:
    raw = (os.getenv(variable) or "").strip()
    if not raw:
        return frozenset(defaults)
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def _build_bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _build_servers() -> dict[str, MCPServerConfig]:
    # Import after config has read environment values. Config deliberately removes
    # secrets from os.environ, but values already held by this module remain usable.
    from apitelegramchat import config

    servers: dict[str, MCPServerConfig] = {}
    if config.BING_CN_MCP_ENABLED and config.BING_CN_MCP_URL:
        try:
            servers["bing-cn-mcp-server"] = MCPServerConfig(
                name="bing-cn-mcp-server",
                url=config.BING_CN_MCP_URL,
                allowed_hosts=_configured_hosts(
                    "BING_CN_MCP_ALLOWED_HOSTS", {"mcp.api-inference.modelscope.net"}
                ),
                allowed_tools=frozenset({"bing_search"}),
                headers=_build_bearer_header(config.BING_CN_MCP_TOKEN),
            )
        except ValueError as exc:
            logger.warning("Bing MCP registration rejected: %s", exc)
    if config.GAODE_MCP_ENABLED and config.GAODE_MCP_URL and config.GAODE_MCP_TOKEN:
        try:
            servers["amap-maps"] = MCPServerConfig(
                name="amap-maps",
                url=config.GAODE_MCP_URL,
                allowed_hosts=_configured_hosts(
                    "GAODE_MCP_ALLOWED_HOSTS", {"mcp.api-inference.modelscope.net"}
                ),
                allowed_tools=frozenset({
                    "maps_ip_location", "maps_geo", "maps_text_search", "maps_around_search",
                    "maps_search_detail", "maps_bicycling", "maps_direction_bicycling",
                    "maps_direction_walking", "maps_direction_driving",
                    "maps_direction_transit_integrated", "maps_distance",
                }),
                headers=_build_bearer_header(config.GAODE_MCP_TOKEN),
            )
        except ValueError as exc:
            logger.warning("AMap MCP registration rejected: %s", exc)
    return servers


EXTERNAL_MCP_SERVERS = _build_servers()


async def call_mcp_tool(server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
    """Call one allowlisted tool on one configured, trusted endpoint."""
    if not _MCP_SDK_AVAILABLE:
        raise MCPToolError("MCP SDK is unavailable")
    if not isinstance(arguments, dict):
        raise MCPToolError("MCP tool arguments must be an object")
    server = EXTERNAL_MCP_SERVERS.get(server_name)
    if server is None:
        raise MCPToolError(f"External MCP server is not configured: {server_name}")
    if not _TOOL_NAME_RE.fullmatch(tool_name) or tool_name not in server.allowed_tools:
        raise MCPToolError(f"External MCP tool is not allowed: {server_name}.{tool_name}")

    async def run_call() -> Any:
        async with streamablehttp_client(server.url, headers=server.headers) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await session.call_tool(tool_name, arguments)

    try:
        result = await asyncio.wait_for(run_call(), timeout=server.timeout)
    except asyncio.TimeoutError as exc:
        raise MCPToolError(f"External MCP tool timed out: {server_name}.{tool_name}") from exc
    except Exception as exc:
        raise MCPToolError(f"External MCP tool failed: {server_name}.{tool_name}") from exc

    text = _extract_text(result)
    if getattr(result, "isError", False):
        raise MCPToolError(f"External MCP tool returned an error: {server_name}.{tool_name}: {text[:500]}")
    return text


def _extract_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()
