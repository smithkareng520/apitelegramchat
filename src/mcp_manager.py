# mcp_manager.py — 统一 MCP 客户端管理器（mcp.json 驱动）。
#
# 职责
# ----
# 1. 解析并校验项目根目录的 mcp.json（对标 Claude Code 的 .mcp.json 语义）：
#    - streamable_http：外部 MCP（url_env / headers_env 从环境变量注入）；
#    - stdio：内部 MCP（python3 -m mcpserver.server --module X），
#      env 条目支持 ``${VAR}`` 形式的宿主环境插值；
#    - policy.expose / policy.disabled_tools：模型视角的暴露策略。
# 2. 工具发现：list_tools 聚合为模型视角统一命名
#       mcp__<server>__<tool>
#    并给出 OpenAI function 格式的工具定义（host 与子 agent 共用）。
# 3. 工具调用：dispatch 只需要 ``call_tool(name, args, chat_id)``——
#    - stdio 服务器按 (chat, server) 懒生成持久子进程（scope=会话命名空间），
#      空闲超时自动回收；进程意外退出自动重启一次；
#    - streamable_http 保持逐调用会话 + 并发信号量 + 故障分类（沿袭上一代
#      mcp_client.py 对 ModelScope 网关的生产加固，不再维护独立抽象层）。
#
# 结果策略：本模块只做「传输 + 发现」，返回 MCP 工具的原始文本结果；
# 模型视图裁剪（condense_for_model）与用户视图渲染（format_tool_result）
# 在 dispatch / tool_call_loop 分层处理，保证“给 AI 的只有有用的”。
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    # config.py 会在自身导入阶段捕获 RUNTIME_ENV，然后再 scrub os.environ。
    from config import RUNTIME_ENV
except ImportError:  # pragma: no cover - standalone/minimal test environment
    RUNTIME_ENV = dict(os.environ)

logger = logging.getLogger(__name__)

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_MCP_FULL_NAME_RE = re.compile(r"^mcp__([A-Za-z0-9_-]{1,64})__([A-Za-z0-9_]{1,64})$")
_ENV_INTERP_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.client.stdio import stdio_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    _MCP_SDK_AVAILABLE = True
except Exception as exc:  # pragma: no cover - optional deployment dependency
    ClientSession = None  # type: ignore[misc,assignment]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    streamablehttp_client = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    create_mcp_http_client = None  # type: ignore[assignment]
    _MCP_SDK_AVAILABLE = False
    logger.warning("MCP SDK unavailable; MCP tool calls are disabled: %s", exc)


class MCPToolError(RuntimeError):
    """MCP 连接或工具调用失败，并保留可安全展示的诊断信息。"""

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

    def model_hint(self) -> str:
        """给模型的一句话行动指引（拼接在错误消息后，保持精简）。

        目标：模型第一轮就做出正确行为 —— 该重试的重试、不该重试的
        立即向用户说明原因，而不是盲目重试浪费轮次。
        """
        return {
            "rate_limited": "上游限流：不要立即原样重试，请告知用户该服务暂时受限。",
            "authentication": "上游鉴权失败：不要重试，请告知用户该服务的访问令牌或部署配置已失效，需要检查服务端环境变量。",
            "gateway": "上游网关暂不可用：可稍后重试一次，仍失败则告知用户服务暂时不可用。",
            "endpoint": "端点不存在或未配置：不要重试，请告知用户该服务尚未正确部署或已下线。",
            "request": "请求被拒绝：请检查并修正参数后重试。",
            "timeout": "请求超时：可重试一次，仍超时请改用其他方式并告知用户。",
            "unknown": "可重试一次，仍失败请告知用户该服务暂时不可用。",
        }.get(self.category, "可重试一次，仍失败请告知用户该服务暂时不可用。")

    def user_message(self, feature_name: str = "外部服务") -> str:
        """返回可安全传递给用户/模型的说明，不暴露令牌或上游内部异常。"""
        suffix = f"（HTTP {self.status_code}）" if self.status_code is not None else ""
        if self.category == "rate_limited":
            return (
                f"❌ {feature_name}受到上游限流或调用额度限制{suffix}。"
                "这不是“未找到结果”；请稍后重试，并核对上游服务的用量、调用日志或配额页面。"
            )
        if self.category == "authentication":
            return f"❌ {feature_name}的上游鉴权失败{suffix}。请检查 MCP 部署地址、访问令牌和授权状态。"
        if self.category == "gateway":
            return f"❌ {feature_name}的上游网关暂时不可用{suffix}。请稍后重试，并检查部署状态及调用日志。"
        if self.category == "endpoint":
            return (
                f"❌ {feature_name}的 MCP 部署地址不存在或不是可用的 Streamable HTTP 端点{suffix}。"
                "请从部署页面重新复制 MCP URL，并确认部署仍处于可用状态。"
            )
        if self.category == "request":
            return f"❌ {feature_name}的上游请求被拒绝{suffix}。请检查工具参数和服务配置。"
        if self.category == "timeout":
            return f"❌ {feature_name}请求超时。请稍后重试。"
        return f"❌ {feature_name}暂时不可用{suffix}。请稍后重试，并检查 MCP 部署调用日志。"


# =====================================================================
# 配置模型
# =====================================================================
@dataclass(frozen=True)
class MCPServerConfig:
    """一个 mcp.json 中注册的 MCP 服务器。"""

    name: str
    type: str                              # "in_process" | "stdio" | "streamable_http"
    # in_process / stdio
    module: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    idle_timeout: float = 900.0
    # streamable_http
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 12.0
    allowed_hosts: frozenset[str] = frozenset()
    # 诊断信息：记录 mcp.json 里声明的环境变量名（不保存值），
    # 便于鉴权/端点失败时在日志中直接指出应检查哪个变量。
    url_env: str = ""
    header_env_names: tuple[str, ...] = ()
    # 策略
    exposed_tools: frozenset[str] | None = None   # None=全部暴露；空集=全部禁用

    def exposes_tool(self, tool_name: str) -> bool:
        if self.exposed_tools is None:
            return True
        return tool_name in self.exposed_tools


def _runtime_env_get(name: str) -> str:
    """读取 config.py 导入时捕获的运行时环境，而非已 scrub 的 os.environ。"""
    return (RUNTIME_ENV.get(name) or "").strip()


def _load_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    override = _runtime_env_get("APITELEGRAMCHAT_MCP_JSON")
    if override:
        return Path(override)
    # mcp.json 与 src/ 同级（项目根）。
    return Path(__file__).resolve().parent.parent / "mcp.json"


def _interpolate(value: str) -> str:
    """解析 ``${VAR}`` 形式的环境变量插值；缺失时替换为空串。"""
    match = _ENV_INTERP_RE.fullmatch(value.strip())
    if match:
        return _runtime_env_get(match.group(1))
    return value


def _resolve_python_command(command: str) -> str:
    """容器镜像里可能只有 python3；显式 python 时优雅降级。"""
    if command in {"python", "python3"}:
        return sys_executable()
    return command


def sys_executable() -> str:
    """优先返回项目可用的 python3 解释器路径。"""
    for candidate in ("python3", "python"):
        path = shutil.which(candidate)
        if path:
            return path
    import sys

    return sys.executable or "python3"


def _resolve_pythonpath(value: str) -> str:
    """把 mcp.json 里的相对 PYTHONPATH（如 ``src``）解析为项目根上的绝对路径。"""
    if not value:
        return value
    parts = []
    project_root = Path(__file__).resolve().parent.parent
    for item in value.split(os.pathsep):
        candidate = Path(item)
        if not candidate.is_absolute():
            candidate = (project_root / item).resolve()
        parts.append(str(candidate))
    return os.pathsep.join(parts)


def _parse_policy(raw: Any, server_name: str) -> frozenset[str] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("expose") is False:
        return frozenset()
    disabled = raw.get("disabled_tools")
    if isinstance(disabled, list):
        disabled_set = {str(item) for item in disabled if isinstance(item, str)}
        if disabled_set:
            logger.info("MCP server %s disables tools: %s", server_name, sorted(disabled_set))
        # disabled_tools 无法在解析期减去远端实际工具列表（未知），
        # 交给 list_tools 时的动态过滤；这里返回哨兵 —— 用 None 表示
        # “动态过滤”，同时把禁用名单存入模块级 _DYNAMIC_DISABLED。
        _DYNAMIC_DISABLED[server_name] = frozenset(disabled_set)
        return None
    return None


# list_tools 时按服务器动态剔除的禁用工具（policy.disabled_tools）。
_DYNAMIC_DISABLED: dict[str, frozenset[str]] = {}


def _build_server(name: str, raw: Any) -> MCPServerConfig:
    if not isinstance(raw, dict):
        raise ValueError(f"{name}: server entry must be an object")
    server_type = str(raw.get("type") or "").strip()
    if server_type == "in_process":
        module = str(raw.get("module") or "").strip()
        allowed_modules = {"search", "todo", "memory", "workspace", "bash"}
        if module not in allowed_modules:
            raise ValueError(f"{name}: in_process module must be one of {sorted(allowed_modules)}")
        return MCPServerConfig(
            name=name,
            type="in_process",
            module=module,
            exposed_tools=_parse_policy(raw.get("policy"), name),
        )
    if server_type == "stdio":
        command = str(raw.get("command") or "").strip()
        if not command:
            raise ValueError(f"{name}: stdio server requires command")
        args = tuple(str(item) for item in (raw.get("args") or []) if isinstance(item, (str, int, float)))
        env = {}
        raw_env = raw.get("env") or {}
        if isinstance(raw_env, dict):
            for key, value in raw_env.items():
                if isinstance(key, str) and isinstance(value, (str, int, float)):
                    env[str(key)] = _interpolate(str(value))
        try:
            idle_timeout = float(raw.get("idle_timeout") or 900)
        except (TypeError, ValueError):
            idle_timeout = 900.0
        return MCPServerConfig(
            name=name,
            type="stdio",
            command=_resolve_python_command(command),
            args=args,
            env=env,
            idle_timeout=max(60.0, idle_timeout),
            exposed_tools=_parse_policy(raw.get("policy"), name),
        )
    if server_type == "streamable_http":
        url_env = str(raw.get("url_env") or "").strip()
        url = _runtime_env_get(url_env) if url_env else str(raw.get("url") or "").strip()
        headers: dict[str, str] = {}
        header_env_names: list[str] = []
        raw_headers = raw.get("headers_env") or {}
        if isinstance(raw_headers, dict):
            for header, env_name in raw_headers.items():
                header_env_names.append(str(env_name))
                token = _runtime_env_get(str(env_name))
                if token:
                    headers[str(header)] = f"Bearer {token}" if str(header).lower() == "authorization" else token
                    # 与下方"缺失 WARNING"对称的确认行：值本身绝不入日志。
                    logger.info(
                        "MCP server %s: header %s sourced from env %s [set]",
                        name, header, env_name,
                    )
                else:
                    # 配置声明了鉴权头但环境变量为空：启动期就把问题
                    # 摆到台面上，而不是等第一次调用 401 后再排查。
                    logger.warning(
                        "MCP server %s: env %s (for header %s) is not set; header omitted — expect upstream failures",
                        name, env_name, header,
                    )
        if not url:
            logger.info("MCP server %s skipped: %s is not set", name, url_env or "url")
            raise ValueError(f"{name}: url not configured")
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            raise ValueError(f"{name}: external MCP endpoint must be an HTTPS URL")
        allowed_hosts_raw = raw.get("allowed_hosts")
        allowed_hosts = frozenset(
            str(item).lower() for item in allowed_hosts_raw if isinstance(item, str)
        ) if isinstance(allowed_hosts_raw, list) else frozenset()
        if allowed_hosts and host not in allowed_hosts:
            raise ValueError(f"{name}: endpoint host is not allowlisted")
        try:
            timeout = float(raw.get("timeout") or 12)
        except (TypeError, ValueError):
            timeout = 12.0
        timeout = min(max(timeout, 1.0), 60.0)
        return MCPServerConfig(
            name=name,
            type="streamable_http",
            url=url,
            headers=headers,
            timeout=timeout,
            allowed_hosts=allowed_hosts,
            exposed_tools=_parse_policy(raw.get("policy"), name),
            url_env=url_env,
            header_env_names=tuple(header_env_names),
        )
    raise ValueError(f"{name}: unknown MCP server type {server_type!r}")


def load_servers(config_path: str | None = None) -> dict[str, MCPServerConfig]:
    path = _load_config_path(config_path)
    if not path.is_file():
        logger.warning("mcp.json not found at %s; no MCP tools will be available", path)
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("mcp.json parse failed (%s): %s", path, exc)
        return {}
    entries = raw.get("mcpServers")
    if not isinstance(entries, dict) or not entries:
        logger.warning("mcp.json has no mcpServers entry")
        return {}
    servers: dict[str, MCPServerConfig] = {}
    for name, raw_server in entries.items():
        if not _MCP_FULL_NAME_RE.match(f"mcp__{name}__x"):
            logger.warning("mcp.json server name %r is not usable in mcp__<server>__<tool>; skipped", name)
            continue
        try:
            servers[name] = _build_server(name, raw_server)
        except ValueError as exc:
            logger.warning("MCP server registration rejected: %s", exc)
    return servers


# =====================================================================
# HTTP 诊断（沿袭 mcp_client.py 的生产加固）
# =====================================================================
class _MCPHTTPTrace:
    """记录 MCP SDK 自行吞掉前的最后一个 HTTP 响应状态。"""

    status_code: int | None = None

    async def observe_response(self, response: Any) -> None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            self.status_code = status_code


def _tracing_http_client_factory(trace: _MCPHTTPTrace) -> Callable[..., Any]:
    def factory(*args: Any, **kwargs: Any) -> Any:
        if create_mcp_http_client is None:  # pragma: no cover
            raise RuntimeError("MCP HTTP client factory is unavailable")
        client = create_mcp_http_client(*args, **kwargs)
        hooks = getattr(client, "event_hooks", None)
        if isinstance(hooks, dict):
            hooks.setdefault("response", []).append(trace.observe_response)
        return client

    return factory


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
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
    text = str(value or "").strip().replace("\n", " ")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1***", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+", r"\1***", text)
    return text[:limit]


def _env_hint(server: MCPServerConfig, category: str) -> str:
    """鉴权/端点类失败时，在日志里直接指出应检查的环境变量名。"""
    if category == "authentication":
        names = [n for n in server.header_env_names if n]
        if names:
            return f"check env: {', '.join(names)}"
        return "check headers config in mcp.json"
    if category == "endpoint" and server.url_env:
        return f"check env: {server.url_env}"
    return "-"


def _classify_failure(status_code: int | None, detail: str) -> tuple[str, bool]:
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
                logger.debug("_diagnose_mcp_exception 内部忽略的异常", exc_info=True)
                response_text = ""
            if response_text:
                response_details.append(_truncate_safe_detail(response_text))
        text = _truncate_safe_detail(current)
        if text:
            exception_details.append(text)
    detail = next((item for item in response_details if item), "")
    if not detail and exception_details:
        # 取异常链最内层（叶节点）的消息：最外层往往是 TaskGroup/
        # ExitStack 等包装（“unhandled errors in a TaskGroup…”），
        # 对排障没有价值；根因（如 “Client error '401 Unauthorized'
        # for url …”）在链的末端。
        detail = exception_details[-1]
    category, retryable = _classify_failure(status_code, detail)
    return status_code, category, detail, retryable


# 同一时刻允许并发打开的外部 HTTP MCP 会话总数。
# ModelScope 的 streamable HTTP 网关在并发会话数升高时表现不稳定
# （响应体截断 / SSE GET 流被立即关闭），限流到 2 降低上游抖动概率。
def _max_http_concurrency() -> int:
    raw = (os.getenv("EXTERNAL_MCP_MAX_CONCURRENCY") or "").strip()
    try:
        return max(1, min(int(raw), 8))
    except (TypeError, ValueError):
        return 2


_HTTP_CALL_SEMAPHORE = asyncio.Semaphore(_max_http_concurrency())


def _extract_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


# =====================================================================
# stdio 连接（按 chat 持久子进程）
# =====================================================================
class _StdioConnection:
    """一个 (server, chat) 对应的持久 stdio 子进程 + MCP 会话。

    生命周期由专属 keeper task 持有：stdio_client / ClientSession 的
    async context 全部在 keeper task 内进入与退出。anyio 的 cancel
    scope 必须在进入它的同一个 task 中退出 —— 而 reaper / aclose /
    失败重试都可能从别的 task 触发关闭；若让调用方 task 直接持有
    ExitStack，跨 task 关闭会把 CancelledError 注入仍在等待结果的
    其他调用方。keeper 模式把进/出收敛在同一 task，任意 task 都能
    安全触发关闭（close 只向 keeper 发信号并等待其退出）。
    """

    _INITIALIZE_TIMEOUT = 30.0
    _CLOSE_GRACE = 15.0

    def __init__(self, params: Any, scope: str, display_name: str) -> None:
        self._params = params
        self._scope = scope
        self.display_name = display_name
        self.session: Any = None
        self.last_used: float = time.monotonic()
        self.lock = asyncio.Lock()
        self._keeper: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._close_requested = asyncio.Event()
        self._error: BaseException | None = None

    async def _run_keeper(self) -> None:
        """keeper：持有全部 async context，直到 close_requested。"""
        try:
            async with AsyncExitStack() as stack:
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(self._params)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await asyncio.wait_for(session.initialize(), timeout=self._INITIALIZE_TIMEOUT)
                self.session = session
                self._ready.set()
                logger.info("[mcp] stdio server %s started (scope=%s)", self.display_name, self._scope)
                await self._close_requested.wait()
        except asyncio.CancelledError:
            if self._error is None:
                self._error = asyncio.CancelledError()
            raise
        except BaseException as exc:  # noqa: BLE001 —— 必须唤醒 ensure() 等待者
            self._error = exc
        finally:
            self.session = None
            self._ready.set()  # 任何结束路径都唤醒等待者，绝不悬挂

    async def ensure(self) -> Any:
        if self.session is not None:
            return self.session
        async with self.lock:
            if self.session is not None:
                return self.session
            if not _MCP_SDK_AVAILABLE:
                raise MCPToolError("MCP SDK is unavailable", category="endpoint", retryable=False)
            if self._keeper is None or self._keeper.done():
                self._error = None
                self._ready = asyncio.Event()
                self._close_requested = asyncio.Event()
                self._keeper = asyncio.create_task(self._run_keeper())
            await self._ready.wait()
            if self.session is None:
                error, self._error = self._error, None
                self._keeper = None
                if isinstance(error, asyncio.CancelledError):
                    # keeper 被取消（进程退出/循环关闭）：按可重试失败处理，
                    # 不把取消信号注入调用方。
                    raise MCPToolError(
                        "Internal MCP server initialize was cancelled",
                        category="endpoint", retryable=True,
                    )
                raise error if error is not None else RuntimeError(
                    "stdio server exited before initialization completed"
                )
            return self.session

    async def close(self) -> None:
        """请求 keeper 退出并等待；幂等，任意 task 调用都安全。"""
        async with self.lock:
            self.session = None
            keeper, self._keeper = self._keeper, None
            if keeper is not None and not keeper.done():
                self._close_requested.set()
            else:
                keeper = None
        if keeper is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(keeper), timeout=self._CLOSE_GRACE)
        except asyncio.TimeoutError:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await keeper
        except asyncio.CancelledError:
            if not keeper.done():
                # close() 自身被取消：终止 keeper 后继续传播外层取消。
                keeper.cancel()
                raise
            # keeper 被取消导致的 CancelledError：关闭已达成，吞掉。
        except Exception:
            logger.debug("[mcp] stdio close ignored error", exc_info=True)
        logger.info("[mcp] stdio server %s stopped (scope=%s)", self.display_name, self._scope)

    async def call(self, tool: str, arguments: dict) -> str:
        session = await self.ensure()
        self.last_used = time.monotonic()
        result = await session.call_tool(tool, arguments)
        self.last_used = time.monotonic()
        return _extract_text(result)

    @property
    def idle_for(self) -> float:
        return time.monotonic() - self.last_used


class MCPManager:
    """mcp.json 驱动的统一 MCP 客户端管理器。"""

    def __init__(self, config_path: str | None = None) -> None:
        self.servers: dict[str, MCPServerConfig] = load_servers(config_path)
        self._stdio: dict[tuple[str, str], _StdioConnection] = {}
        # Trusted built-in MCP modules execute in-process to avoid one resident
        # Python interpreter per chat/server under a tight cgroup memory limit.
        self._inprocess_registry: dict[str, Any] = {}
        self._reaper_task: asyncio.Task | None = None
        self._tools_cache: dict[str, tuple[float, list[dict]]] = {}
        self._tools_cache_ttl = 300.0
        self._closed = False

    # ---------- 工具发现 ----------
    def _full_name(self, server_name: str, tool_name: str) -> str:
        return f"mcp__{server_name}__{tool_name}"

    def _dynamic_disabled(self, server_name: str) -> frozenset[str]:
        return _DYNAMIC_DISABLED.get(server_name, frozenset())

    async def _get_inprocess_registry(self, server: MCPServerConfig) -> Any:
        registry = self._inprocess_registry.get(server.name)
        if registry is not None:
            return registry
        from mcpserver.catalogue import ToolRegistry

        # The standalone MCP server enables mutations through its trusted
        # environment. In-process execution is equally trusted, so pass the
        # capability explicitly instead of mutating os.environ globally.
        registry = ToolRegistry([server.module], allow_mutations=True)
        self._inprocess_registry[server.name] = registry
        return registry

    async def _list_raw_tools(self, server: MCPServerConfig) -> list[Any]:
        if server.type == "in_process":
            registry = await self._get_inprocess_registry(server)
            return await registry.list_tools()
        if server.type == "stdio":
            # 任一连接都能给出同样的目录；用临时连接避免污染 per-chat 池。
            conn = self._make_connection(server, scope="list_tools")
            try:
                session = await conn.ensure()
                response = await session.list_tools()
                return list(response.tools or [])
            finally:
                await conn.close()
        trace = _MCPHTTPTrace()

        async def _run() -> Any:
            async with streamablehttp_client(
                server.url,
                headers=server.headers,
                httpx_client_factory=_tracing_http_client_factory(trace),
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    response = await session.list_tools()
                    return list(response.tools or [])

        async with _HTTP_CALL_SEMAPHORE:
            return await asyncio.wait_for(_run(), timeout=server.timeout)

    async def list_server_tools(self, server_name: str, *, force_refresh: bool = False) -> list[dict]:
        """返回某服务器的 OpenAI function 工具定义（带 TTL 缓存）。"""
        server = self.servers.get(server_name)
        if server is None or not _MCP_SDK_AVAILABLE:
            return []
        cached = self._tools_cache.get(server_name)
        now = time.monotonic()
        if cached and not force_refresh and now - cached[0] < self._tools_cache_ttl:
            return cached[1]
        try:
            raw_tools = await self._list_raw_tools(server)
        except Exception as exc:
            status_code, category, detail, _retryable = _diagnose_mcp_exception(exc)
            logger.warning(
                "MCP list_tools failed server=%s status=%s category=%s detail=%s hint=%s",
                server_name, status_code if status_code is not None else "unknown", category, detail or "<empty>",
                _env_hint(server, category),
                exc_info=True,
            )
            # 保留旧缓存（可能过期但聊胜于无）。
            return cached[1] if cached else []
        disabled = self._dynamic_disabled(server_name)
        defs: list[dict] = []
        for tool in raw_tools:
            tool_name = getattr(tool, "name", "")
            if not isinstance(tool_name, str) or not _TOOL_NAME_RE.fullmatch(tool_name):
                continue
            if tool_name in disabled or not server.exposes_tool(tool_name):
                continue
            defs.append(self._tool_def(server_name, tool))
        self._tools_cache[server_name] = (now, defs)
        return defs

    def _tool_def(self, server_name: str, tool: Any) -> dict:
        parameters = getattr(tool, "inputSchema", None)
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}, "additionalProperties": False}
        function: dict[str, Any] = {
            "name": self._full_name(server_name, getattr(tool, "name", "")),
            "description": str(getattr(tool, "description", "") or ""),
            "parameters": parameters,
        }
        meta = getattr(tool, "meta", None)
        examples: list = []
        if isinstance(meta, dict):
            raw_examples = meta.get("input_examples")
            if isinstance(raw_examples, list):
                examples = [item for item in raw_examples if isinstance(item, dict)]
        definition: dict[str, Any] = {"type": "function", "function": function}
        if examples:
            function["input_examples"] = examples
        return definition

    async def list_all_tools(self) -> list[dict]:
        """聚合全部已暴露 MCP 工具的模型视角定义（顺序稳定）。"""
        defs: list[dict] = []
        for server_name in self.servers:
            defs.extend(await self.list_server_tools(server_name))
        return defs

    # ---------- 工具调用 ----------
    def _make_connection(self, server: MCPServerConfig, *, scope: str) -> _StdioConnection:
        params = self._stdio_params(server, scope)
        return _StdioConnection(params, scope, display_name=server.name)

    def _stdio_params(self, server: MCPServerConfig, scope: str) -> Any:
        # os.environ 已完成 secret scrub；stdio 子进程使用启动时快照，
        # 再叠加 mcp.json 显式声明的 env。
        env = dict(RUNTIME_ENV)
        env["APITELEGRAMCHAT_MCP_SCOPE"] = scope
        env["APITELEGRAMCHAT_MCP_ENABLE_MUTATIONS"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        for key, value in server.env.items():
            if key.upper() == "PYTHONPATH":
                env[key] = _resolve_pythonpath(value)
            else:
                env[key] = value
        return StdioServerParameters(command=server.command, args=list(server.args), env=env)

    async def _get_stdio_connection(self, server: MCPServerConfig, scope: str) -> _StdioConnection:
        key = (server.name, scope)
        conn = self._stdio.get(key)
        if conn is None:
            conn = self._make_connection(server, scope=scope)
            self._stdio[key] = conn
            self._ensure_reaper()
        return conn

    async def call_tool(self, name: str, arguments: dict[str, Any], chat_id: int) -> str:
        """按完整名 mcp__<server>__<tool> 调用工具，返回原始文本结果。"""
        if not _MCP_SDK_AVAILABLE:
            raise MCPToolError("MCP SDK is unavailable", category="endpoint", retryable=False)
        if not isinstance(arguments, dict):
            raise MCPToolError("MCP tool arguments must be an object", retryable=False)
        match = _MCP_FULL_NAME_RE.fullmatch(name or "")
        if not match:
            raise MCPToolError(f"Invalid MCP tool name: {name}", category="request", retryable=False)
        server_name, tool_name = match.group(1), match.group(2)
        server = self.servers.get(server_name)
        if server is None:
            raise MCPToolError(f"External MCP server is not configured: {server_name}", category="endpoint", retryable=False)
        if not server.exposes_tool(tool_name) or tool_name in self._dynamic_disabled(server_name):
            raise MCPToolError(f"External MCP tool is not allowed: {name}", category="request", retryable=False)
        if server.type == "in_process":
            return await self._call_inprocess(server, tool_name, arguments, chat_id)
        if server.type == "stdio":
            return await self._call_stdio(server, tool_name, arguments, chat_id)
        return await self._call_http(server, tool_name, arguments)

    async def _call_inprocess(
        self,
        server: MCPServerConfig,
        tool: str,
        arguments: dict,
        chat_id: int,
    ) -> str:
        """Execute a trusted built-in MCP module without a resident child process."""
        from mcpserver.context import MCPRequestContext
        from workspace_paths import workspace_namespace

        registry = await self._get_inprocess_registry(server)
        scope = workspace_namespace(chat_id)
        context = MCPRequestContext(scope=scope, chat_id=chat_id)
        result = await registry.call(tool, arguments, context)
        text = _extract_text(result)
        if getattr(result, "isError", False):
            raise MCPToolError(
                f"Internal MCP tool returned an error: {server.name}.{tool}: {text[:500]}",
                category="request",
                retryable=False,
            )
        return text

    async def _call_stdio(self, server: MCPServerConfig, tool: str, arguments: dict, chat_id: int) -> str:
        from workspace_paths import workspace_namespace

        scope = workspace_namespace(chat_id)
        conn = await self._get_stdio_connection(server, scope)
        try:
            return await conn.call(tool, arguments)
        except MCPToolError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 子进程死亡 / 会话失效：重启一次并重试一次。
            logger.warning("[mcp] stdio call failed server=%s tool=%s (%s); restarting once",
                           server.name, tool, type(exc).__name__, exc_info=True)
            await conn.close()
            fresh = await self._get_stdio_connection(server, scope)
            try:
                return await fresh.call(tool, arguments)
            except MCPToolError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as retry_exc:
                status_code, category, detail, retryable = _diagnose_mcp_exception(retry_exc)
                raise MCPToolError(
                    f"Internal MCP tool failed: {server.name}.{tool}: {detail or type(retry_exc).__name__}",
                    category=category,
                    status_code=status_code,
                    retryable=retryable,
                ) from retry_exc

    async def _call_http(self, server: MCPServerConfig, tool: str, arguments: dict) -> str:
        trace = _MCPHTTPTrace()

        async def run_call() -> Any:
            async with streamablehttp_client(
                server.url,
                headers=server.headers,
                httpx_client_factory=_tracing_http_client_factory(trace),
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await session.call_tool(tool, arguments)

        try:
            # 信号量在超时预算之外获取：排队等待不应消耗单次调用的超时预算。
            async with _HTTP_CALL_SEMAPHORE:
                result = await asyncio.wait_for(run_call(), timeout=server.timeout)
        except asyncio.TimeoutError as exc:
            raise MCPToolError(
                f"External MCP tool timed out: {server.name}.{tool}",
                category="timeout",
                retryable=True,
            ) from exc
        except Exception as exc:
            status_code, category, detail, retryable = _diagnose_mcp_exception(
                exc, observed_status_code=trace.status_code,
            )
            logger.warning(
                "External MCP call failed server=%s tool=%s status=%s category=%s retryable=%s detail=%s hint=%s",
                server.name, tool,
                status_code if status_code is not None else "unknown",
                category, retryable, detail or "<empty>",
                _env_hint(server, category),
                exc_info=True,
            )
            status_fragment = f" HTTP {status_code}" if status_code is not None else ""
            raise MCPToolError(
                f"External MCP tool failed:{status_fragment} {server.name}.{tool}",
                category=category,
                status_code=status_code,
                retryable=retryable,
            ) from exc

        text = _extract_text(result)
        if getattr(result, "isError", False):
            category, retryable = _classify_failure(None, text)
            raise MCPToolError(
                f"External MCP tool returned an error: {server.name}.{tool}: {text[:500]}",
                category=category,
                retryable=retryable,
            )
        return text

    # ---------- 生命周期 ----------
    def _ensure_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            try:
                self._reaper_task = asyncio.create_task(self._reap_idle())
            except RuntimeError:
                # 事件循环尚未运行（导入期）；首次真正使用时再建。
                self._reaper_task = None

    async def _reap_idle(self) -> None:
        interval = 60.0
        while not self._closed:
            await asyncio.sleep(interval)
            now_servers = [s for s in self.servers.values() if s.type == "stdio"]
            for (server_name, scope), conn in list(self._stdio.items()):
                server = next((s for s in now_servers if s.name == server_name), None)
                if server is None:
                    continue
                if conn.idle_for >= server.idle_timeout:
                    logger.info("[mcp] reaping idle stdio server %s scope=%s (idle %.0fs)",
                                server_name, scope, conn.idle_for)
                    self._stdio.pop((server_name, scope), None)
                    await conn.close()

    async def aclose(self) -> None:
        self._closed = True
        tasks = [conn.close() for conn in self._stdio.values()]
        self._stdio.clear()
        self._inprocess_registry.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None


# 进程级单例：host 与子 agent 共用同一个管理器（同一个 per-chat 连接池）。
mcp_manager = MCPManager()
