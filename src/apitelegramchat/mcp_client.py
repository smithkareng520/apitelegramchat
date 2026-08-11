# mcp_client.py — 通用外部 MCP 客户端
#
# 用于连接外部 MCP server（当前通过 streamable_http 传输），发现其工具，
# 并调用其工具。设计上支持同时配置多个外部 MCP server（不局限于单一
# 搜索服务），后续新增服务只需在 EXTERNAL_MCP_SERVERS 中追加一项配置。
#
# 每次工具调用都新建一个短生命周期的 streamable_http 连接 + ClientSession，
# 调用完成后立即关闭。这样实现简单、不需要维护长连接/重连状态机，
# 代价是每次调用多一次握手往返，对于搜索这种非高频路径是可接受的取舍。

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("apitelegramchat.mcp_client")

try:
    from mcp import ClientSession  # type: ignore
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore
    _MCP_SDK_AVAILABLE = True
except Exception as e:  # pragma: no cover - optional dependency fallback
    ClientSession = None  # type: ignore
    streamablehttp_client = None  # type: ignore
    _MCP_SDK_AVAILABLE = False
    logger.warning(f"mcp SDK 不可用，外部 MCP 工具调用将被禁用: {e}")


@dataclass
class MCPServerConfig:
    """单个外部 MCP server 的连接配置。"""

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    # 单次工具调用（含连接握手）的超时时间，单位秒
    timeout: float = 30.0


from apitelegramchat import config as _config


def _build_bing_cn_server() -> Optional[MCPServerConfig]:
    """必应中文搜索 MCP 服务，替代原先的 Google CSE / DuckDuckGo。

    连接地址与鉴权 Token 完全从 config.py（即环境变量 BING_CN_MCP_URL /
    BING_CN_MCP_TOKEN）读取，代码里不含任何明文密钥或地址默认值。
    未配置 URL 时直接不注册该服务。
    """
    if not _config.BING_CN_MCP_ENABLED:
        return None
    url = _config.BING_CN_MCP_URL
    if not url:
        logger.warning("BING_CN_MCP_URL 未配置，bing-cn-mcp-server 搜索服务不可用")
        return None
    headers: dict[str, str] = {}
    if _config.BING_CN_MCP_TOKEN:
        headers["Authorization"] = f"Bearer {_config.BING_CN_MCP_TOKEN}"
    return MCPServerConfig(name="bing-cn-mcp-server", url=url, headers=headers, timeout=30.0)


def _build_amap_maps_server() -> Optional[MCPServerConfig]:
    """高德地图 MCP 服务（@amap/amap-maps on ModelScope）。

    通过 streamable_http 调用 https://mcp.api-inference.modelscope.net/.../mcp，
    使用 GAODE_MCP_TOKEN 作为 Bearer 鉴权。这是替代旧的
    amap_integration.py 直接调用高德 Web 服务 API 的新实现：所有地理 /
    路径 / POI / IP 定位 / 静态地图能力都由该 MCP 服务提供。

    连接地址与 Token 完全从 config.py（即环境变量 GAODE_MCP_URL /
    GAODE_MCP_TOKEN）读取；未配置 Token 时直接不注册该服务。
    """
    if not _config.GAODE_MCP_ENABLED:
        return None
    url = _config.GAODE_MCP_URL
    if not url:
        logger.warning("GAODE_MCP_URL 未配置，amap-maps MCP 服务不可用")
        return None
    headers: dict[str, str] = {}
    if _config.GAODE_MCP_TOKEN:
        headers["Authorization"] = f"Bearer {_config.GAODE_MCP_TOKEN}"
    else:
        logger.warning("GAODE_MCP_TOKEN 未配置，amap-maps MCP 服务不可用")
        return None
    return MCPServerConfig(name="amap-maps", url=url, headers=headers, timeout=30.0)


# 已配置的外部 MCP server 注册表。新增服务时在此追加一个 _build_xxx_server()
# 并加入下面的字典即可，其余调用方代码无需改动。
def _load_servers() -> dict[str, MCPServerConfig]:
    servers: dict[str, MCPServerConfig] = {}
    for cfg in (_build_bing_cn_server(), _build_amap_maps_server()):
        if cfg is not None:
            servers[cfg.name] = cfg
    return servers


EXTERNAL_MCP_SERVERS: dict[str, MCPServerConfig] = _load_servers()


class MCPToolError(Exception):
    """调用外部 MCP 工具失败（连接失败 / 工具报错 / SDK 缺失等）。"""


async def call_mcp_tool(server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
    """连接指定的外部 MCP server 并调用其上的一个工具，返回文本结果。

    每次调用都是一次独立的 streamable_http 会话：connect -> initialize ->
    call_tool -> close。不缓存/复用连接。
    """
    if not _MCP_SDK_AVAILABLE:
        raise MCPToolError(
            "mcp SDK 未安装，无法调用外部 MCP 工具（请安装 `mcp` 包）。"
        )

    server = EXTERNAL_MCP_SERVERS.get(server_name)
    if server is None:
        raise MCPToolError(f"未配置的外部 MCP server: {server_name}")

    async def _run() -> Any:
        async with streamablehttp_client(server.url, headers=server.headers) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await session.call_tool(tool_name, arguments)

    try:
        # asyncio.wait_for 而非 asyncio.timeout：项目最低支持 Python 3.10，
        # asyncio.timeout 是 3.11+ 才有的 API。
        result = await asyncio.wait_for(_run(), timeout=server.timeout)
    except asyncio.TimeoutError as e:
        raise MCPToolError(f"调用外部 MCP 工具超时: {server_name}.{tool_name}") from e
    except MCPToolError:
        raise
    except Exception as e:
        raise MCPToolError(f"调用外部 MCP 工具失败: {server_name}.{tool_name}: {e}") from e

    if getattr(result, "isError", False):
        text = _extract_text(result)
        raise MCPToolError(f"外部 MCP 工具返回错误: {server_name}.{tool_name}: {text}")

    return _extract_text(result)


def _extract_text(result: Any) -> str:
    """把 MCP CallToolResult.content（可能含多个 block）拼接成纯文本。"""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()
