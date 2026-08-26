"""Hardened client for explicitly trusted external MCP servers."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

# 与项目其它模块保持一致：使用 __name__ 而非硬编码字符串，
# 这样 reload / 重命名模块时 logger 命名空间会自动跟随。
logger = logging.getLogger(__name__)
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    _MCP_SDK_AVAILABLE = True
except Exception as exc:  # pragma: no cover - optional deployment dependency
    ClientSession = None  # type: ignore[assignment]
    streamablehttp_client = None  # type: ignore[assignment]
    create_mcp_http_client = None  # type: ignore[assignment]
    _MCP_SDK_AVAILABLE = False
    logger.warning("MCP SDK unavailable; external MCP calls are disabled: %s", exc)


class MCPToolError(RuntimeError):
    """外部 MCP 连接或工具调用失败，并保留可安全展示的诊断信息。"""

    def __init__(
        self,
        message: str,
        *,
        category: str = "unknown",
        status_code: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retryable = retryable

    def user_message(self, feature_name: str = "外部服务") -> str:
        """返回可安全传递给用户/模型的说明，不暴露令牌或上游内部异常。"""
        suffix = f"（HTTP {self.status_code}）" if self.status_code is not None else ""
        if self.category == "rate_limited":
            return (
                f"❌ {feature_name}受到上游限流或调用额度限制{suffix}。"
                "这不是“未找到结果”；请稍后重试，并在 ModelScope MCP 部署的用量、调用日志或配额页面核对限制。"
            )
        if self.category == "authentication":
            return (
                f"❌ {feature_name}的上游鉴权失败{suffix}。"
                "请检查 MCP 部署地址、访问令牌和授权状态。"
            )
        if self.category == "gateway":
            return (
                f"❌ {feature_name}的上游网关暂时不可用{suffix}。"
                "该状态不能证明调用额度已用完；请稍后重试，并检查 ModelScope MCP 部署状态及调用日志。"
            )
        if self.category == "endpoint":
            return (
                f"❌ {feature_name}的 MCP 部署地址不存在或不是可用的 Streamable HTTP 端点{suffix}。"
                "这不是额度耗尽；请从 ModelScope 部署页面重新复制 MCP URL，并确认部署仍处于可用状态。"
            )
        if self.category == "request":
            return f"❌ {feature_name}的上游请求被拒绝{suffix}。请检查搜索参数和 MCP 服务配置。"
        if self.category == "timeout":
            return f"❌ {feature_name}请求超时。请稍后重试。"
        return f"❌ {feature_name}暂时不可用{suffix}。请稍后重试，并检查 MCP 部署调用日志。"


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
    if config.SERPER_MCP_URL and config.SERPER_MCP_TOKEN:
        try:
            servers["serper-search"] = MCPServerConfig(
                name="serper-search",
                url=config.SERPER_MCP_URL,
                allowed_hosts=frozenset({"mcp.api-inference.modelscope.net"}),
                allowed_tools=frozenset({"google_search"}),
                headers=_build_bearer_header(config.SERPER_MCP_TOKEN),
            )
        except ValueError as exc:
            logger.warning("Serper MCP registration rejected: %s", exc)
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


@dataclass
class _MCPHTTPTrace:
    """记录 MCP SDK 自行吞掉前的最后一个 HTTP 响应状态。"""

    status_code: int | None = None

    async def observe_response(self, response: Any) -> None:
        """httpx 事件钩子：当前运行时会 await 该回调，因此必须是协程函数。"""
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            self.status_code = status_code


def _tracing_http_client_factory(trace: _MCPHTTPTrace):
    """在保留 SDK 推荐 HTTP 客户端配置的前提下附加响应观察钩子。"""
    def factory(*args: Any, **kwargs: Any) -> Any:
        if create_mcp_http_client is None:  # pragma: no cover - SDK 可用时必然存在
            raise RuntimeError("MCP HTTP client factory is unavailable")
        client = create_mcp_http_client(*args, **kwargs)
        hooks = getattr(client, "event_hooks", None)
        if isinstance(hooks, dict):
            hooks.setdefault("response", []).append(trace.observe_response)
        return client

    return factory


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    """递归展开因果链和 ExceptionGroup，寻找 SDK 包装前的 HTTP 异常。"""
    chain: list[BaseException] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending and len(chain) < 32:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)

        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, (tuple, list)):
            pending.extend(item for item in nested if isinstance(item, BaseException))
    return tuple(chain)


def _truncate_safe_detail(value: Any, limit: int = 500) -> str:
    """清理上游错误摘要，避免日志中意外留下授权头或过长响应体。"""
    text = str(value or "").strip().replace("\n", " ")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1***", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+", r"\1***", text)
    return text[:limit]


def _classify_failure(status_code: int | None, detail: str) -> tuple[str, bool]:
    """将 HTTP/MCP 失败归类，并返回是否值得短时间内自动重试。"""
    normalized = detail.lower()
    if status_code == 429 or any(token in normalized for token in (
        "rate limit", "request limit", "quota", "throttl", "too many requests",
    )):
        return "rate_limited", False
    if status_code in {401, 403}:
        return "authentication", False
    if status_code == 404:
        return "endpoint", False
    if status_code is not None and 500 <= status_code <= 599:
        return "gateway", True
    if status_code is not None and 400 <= status_code <= 499:
        return "request", False
    return "unknown", True


def _diagnose_mcp_exception(
    exc: BaseException,
    observed_status_code: int | None = None,
) -> tuple[int | None, str, str, bool]:
    """从 SDK 包装异常和 HTTP 观察器中提取状态与简短响应摘要。"""
    status_code: int | None = observed_status_code
    response_details: list[str] = []
    exception_details: list[str] = []
    for current in _exception_chain(exc):
        response = getattr(current, "response", None)
        raw_status = getattr(response, "status_code", None)
        if raw_status is None:
            raw_status = getattr(current, "status_code", None)
        if status_code is None and isinstance(raw_status, int):
            status_code = raw_status

        if response is not None:
            try:
                response_text = getattr(response, "text", "")
            except Exception:
                response_text = ""
            if response_text:
                response_details.append(_truncate_safe_detail(response_text))
        text = _truncate_safe_detail(current)
        if text:
            exception_details.append(text)

    # HTTP 响应体比 ExceptionGroup 的包装文字更接近根因；无响应体时再退回异常文本。
    detail = next((item for item in response_details if item), "")
    if not detail:
        detail = next((item for item in exception_details if item), "")
    category, retryable = _classify_failure(status_code, detail)
    return status_code, category, detail, retryable


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

    http_trace = _MCPHTTPTrace()

    async def run_call() -> Any:
        async with streamablehttp_client(
            server.url,
            headers=server.headers,
            httpx_client_factory=_tracing_http_client_factory(http_trace),
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await session.call_tool(tool_name, arguments)

    try:
        result = await asyncio.wait_for(run_call(), timeout=server.timeout)
    except asyncio.TimeoutError as exc:
        raise MCPToolError(
            f"External MCP tool timed out: {server_name}.{tool_name}",
            category="timeout",
            retryable=True,
        ) from exc
    except MCPToolError:
        # 已经是 MCPToolError，避免再包一层导致 chain 不清晰。
        raise
    except Exception as exc:
        status_code, category, detail, retryable = _diagnose_mcp_exception(
            exc,
            observed_status_code=http_trace.status_code,
        )
        logger.warning(
            "External MCP call failed server=%s tool=%s status=%s category=%s retryable=%s detail=%s",
            server_name,
            tool_name,
            status_code if status_code is not None else "unknown",
            category,
            retryable,
            detail or "<empty>",
            exc_info=True,
        )
        status_fragment = f" HTTP {status_code}" if status_code is not None else ""
        raise MCPToolError(
            f"External MCP tool failed:{status_fragment} {server_name}.{tool_name}",
            category=category,
            status_code=status_code,
            retryable=retryable,
        ) from exc

    text = _extract_text(result)
    if getattr(result, "isError", False):
        category, retryable = _classify_failure(None, text)
        raise MCPToolError(
            f"External MCP tool returned an error: {server_name}.{tool_name}: {text[:500]}",
            category=category,
            retryable=retryable,
        )
    return text


def _extract_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()
