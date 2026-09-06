"""serper_api.py — Serper (google.serper.dev) 直接 HTTP 客户端

设计目标
=========
替代原先通过 ModelScope MCP 转发的 google_search 调用，直接对接
Serper 官方 REST API，避开上游 MCP 网关经常出现的"响应体中途被截断 /
SSE 流被立即关闭 / 调用挂死"等不稳定行为。

支持四个端点：
  * search  → https://google.serper.dev/search   普通网页搜索
  * images  → https://google.serper.dev/images   文字搜图
  * videos  → https://google.serper.dev/videos   视频搜索
  * lens    → https://google.serper.dev/lens     以图搜图（reverse image search）

请求与响应格式严格遵循 https://serper.dev 的官方文档；单次请求 body 是
单个 JSON 对象；响应也是单个 JSON 对象（不是数组）。批量请求（数组 body）
在本项目中暂未使用——保持每个 mode × page 一次独立请求，便于失败隔离与
定向重试。

错误分类
========
* SerperAuthError        401/403 — API key 错误 / 配额耗尽
* SerperRateLimitError   429     — 限流
* SerperTimeoutError     网络超时 / 5xx 期间重试耗尽
* SerperRequestError     4xx（除上面三类） / 上游返回非 JSON
* SerperServerError      5xx 重试后仍失败
* SerperUnavailableError SERPER_API_KEY 未配置 / 不可用

调用方（search_engine.execute_web_search 等）只需捕获 SerperError 基类即可。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from config import SERPER_API_KEY

SERPER_BASE_URL = "https://google.serper.dev"
SERPER_DEFAULT_TIMEOUT = 12.0   # 单次请求超时（秒）
SERPER_MAX_RETRIES = 2          # 5xx/网络错误重试次数
SERPER_RETRY_BACKOFF = 0.6      # 重试退避基数（秒）
SERPER_MAX_NUM = 100            # images/videos/lens 的硬上限

ENDPOINTS = {
    "search": "/search",
    "images": "/images",
    "videos": "/videos",
    "lens":   "/lens",
}


class SerperError(RuntimeError):
    """所有 Serper API 错误的基类。"""

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

    def user_message(self, feature_name: str = "网页搜索服务") -> str:
        """返回可安全传递给用户/模型的消息，不暴露 API key 或上游内部异常。"""
        suffix = f"（HTTP {self.status_code}）" if self.status_code is not None else ""
        if self.category == "authentication":
            return (
                f"❌ {feature_name}的上游鉴权失败{suffix}。"
                "请检查 SERPER_API_KEY 是否正确以及是否仍在有效期内。"
            )
        if self.category == "rate_limited":
            return (
                f"❌ {feature_name}受到上游限流或调用额度限制{suffix}。"
                "这不是\"未找到结果\"；请稍后重试，并在 serper.dev 控制台核对配额。"
            )
        if self.category == "request":
            return f"❌ {feature_name}的上游请求被拒绝{suffix}。请检查搜索参数。"
        if self.category == "timeout":
            return f"❌ {feature_name}请求超时。请稍后重试。"
        if self.category == "server":
            return f"❌ {feature_name}的上游服务暂时不可用{suffix}。请稍后重试。"
        if self.category == "unconfigured":
            return (
                f"❌ {feature_name}未配置。"
                "请在环境变量中设置 SERPER_API_KEY（来自 https://serper.dev）。"
            )
        return f"❌ {feature_name}暂时不可用{suffix}。请稍后重试。"


class SerperUnavailableError(SerperError):
    def __init__(self, message: str = "SERPER_API_KEY is not configured") -> None:
        super().__init__(message, category="unconfigured", retryable=False)


class SerperAuthError(SerperError):
    def __init__(self, message: str, status_code: int | None = 401) -> None:
        super().__init__(message, category="authentication", status_code=status_code, retryable=False)


class SerperRateLimitError(SerperError):
    def __init__(self, message: str, status_code: int | None = 429) -> None:
        super().__init__(message, category="rate_limited", status_code=status_code, retryable=False)


class SerperTimeoutError(SerperError):
    def __init__(self, message: str) -> None:
        super().__init__(message, category="timeout", retryable=True)


class SerperRequestError(SerperError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message, category="request", status_code=status_code, retryable=False)


class SerperServerError(SerperError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message, category="server", status_code=status_code, retryable=True)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _is_configured() -> bool:
    return bool(SERPER_API_KEY)


def _headers() -> dict[str, str]:
    return {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json",
    }


def _classify_status(status_code: int, body_text: str) -> tuple[str, bool]:
    """根据 HTTP 状态码与响应体片段，归类错误并给出是否值得重试。"""
    normalized = (body_text or "").lower()
    if status_code == 429 or any(token in normalized for token in (
        "rate limit", "request limit", "quota", "throttl", "too many requests",
    )):
        return "rate_limited", False
    if status_code in {401, 403}:
        return "authentication", False
    if status_code == 404:
        return "request", False
    if 500 <= status_code <= 599:
        return "server", True
    if 400 <= status_code <= 499:
        return "request", False
    return "unknown", True


def _build_payload(
    *,
    mode: str,
    query: str | None,
    image_url: str | None,
    num: int | None,
    page: int | None,
    gl: str | None,
    hl: str | None,
    tbs: str | None,
) -> dict[str, Any]:
    """构造单次请求 payload。

    search/images/videos 端点以 q 为主；lens 端点以 url 为主，可选附 q 做
    "在指定图片基础上加文字约束"的复合查询。每端点的可选字段略有差异，
    此处把所有非空字段都发给上游，由上游决定接受与否。
    """
    payload: dict[str, Any] = {}
    if mode == "lens":
        if not image_url:
            raise SerperRequestError("lens mode requires image_url")
        payload["url"] = image_url
        if query:
            payload["q"] = query
    else:
        if not query:
            raise SerperRequestError(f"{mode} mode requires query")
        payload["q"] = query

    if gl:
        payload["gl"] = gl
    if hl:
        payload["hl"] = hl
    if tbs:
        payload["tbs"] = tbs
    if page is not None and page >= 1:
        payload["page"] = page
    # search 端点不接收 num（serper 单页固定 10；多结果靠多页聚合）。
    # images/videos/lens 接收 num（1-100）。
    if num is not None and mode in {"images", "videos", "lens"}:
        payload["num"] = max(1, min(int(num), SERPER_MAX_NUM))
    return payload


async def _post_with_retry(
    *,
    endpoint: str,
    payload: dict[str, Any],
    timeout: float | None = None,
) -> dict[str, Any]:
    """发 POST 请求并对 5xx / 网络错误做有限次重试。"""
    if not _is_configured():
        raise SerperUnavailableError()
    url = SERPER_BASE_URL + endpoint
    timeout_s = float(timeout) if timeout is not None else SERPER_DEFAULT_TIMEOUT
    last_exc: SerperError | None = None

    # 单次 timeout 用于每次尝试；外层 web_search 工具超时（45s）兜底总预算。
    # 整个重试序列共用一个 ClientSession（复用 TCP/TLS 连接），避免在循环内
    # 每次尝试都新建 session、对同一 host 反复完整握手。
    timeout_cfg = aiohttp.ClientTimeout(total=timeout_s, connect=5, sock_read=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        for attempt in range(SERPER_MAX_RETRIES + 1):
            try:
                async with session.post(
                    url,
                    headers=_headers(),
                    json=payload,
                ) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        try:
                            data = json.loads(text)
                        except (json.JSONDecodeError, ValueError) as exc:
                            raise SerperRequestError(
                                f"Serper returned non-JSON body: {exc}; preview={text[:200]}"
                            ) from exc
                        if not isinstance(data, dict):
                            raise SerperRequestError(
                                f"Serper returned unexpected JSON shape: {type(data).__name__}"
                            )
                        return data
                    category, retryable = _classify_status(resp.status, text)
                    msg = (
                        f"Serper {endpoint} HTTP {resp.status}: "
                        f"{text[:300] if text else '<empty body>'}"
                    )
                    if category == "authentication":
                        raise SerperAuthError(msg, status_code=resp.status)
                    if category == "rate_limited":
                        raise SerperRateLimitError(msg, status_code=resp.status)
                    if category == "server":
                        if attempt < SERPER_MAX_RETRIES:
                            last_exc = SerperServerError(msg, status_code=resp.status)
                            await asyncio.sleep(SERPER_RETRY_BACKOFF * (attempt + 1))
                            continue
                        # 重试耗尽：抛出具体的 SerperServerError 子类，方便调用方
                        # isinstance 区分错误类别。
                        raise SerperServerError(msg, status_code=resp.status)
                    if category == "request":
                        raise SerperRequestError(msg, status_code=resp.status)
                    raise SerperError(msg, category=category, status_code=resp.status,
                                      retryable=retryable)
            except asyncio.TimeoutError as exc:
                # 超时也重试一次，再失败抛 SerperTimeoutError
                last_exc = SerperTimeoutError(f"Serper {endpoint} timed out after {timeout_s}s")
                if attempt < SERPER_MAX_RETRIES:
                    await asyncio.sleep(SERPER_RETRY_BACKOFF * (attempt + 1))
                    continue
                raise last_exc from exc
            except aiohttp.ClientError as exc:
                # 网络错误（DNS、连接、读响应中断）→ 视为可重试
                last_exc = SerperServerError(
                    f"Serper {endpoint} network error: {exc.__class__.__name__}: {exc}"
                )
                if attempt < SERPER_MAX_RETRIES:
                    await asyncio.sleep(SERPER_RETRY_BACKOFF * (attempt + 1))
                    continue
                raise last_exc from exc
            except SerperError:
                raise
            except Exception as exc:  # 兜底，防止未分类异常向上污染
                raise SerperError(
                    f"Serper {endpoint} unexpected error: {exc.__class__.__name__}: {exc}",
                    category="unknown",
                    retryable=False,
                ) from exc

    if last_exc is not None:
        raise last_exc
    raise SerperError(f"Serper {endpoint} exhausted retries")


# ---------------------------------------------------------------------------
# 公共 API：四种搜索模式
# ---------------------------------------------------------------------------

async def search(
    query: str,
    *,
    num: int | None = None,
    page: int | None = None,
    gl: str | None = None,
    hl: str | None = None,
    tbs: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """普通网页搜索：返回 {organic:[{title,link,snippet,date,position}], credits}。"""
    payload = _build_payload(
        mode="search", query=query, image_url=None,
        num=num, page=page, gl=gl, hl=hl, tbs=tbs,
    )
    return await _post_with_retry(endpoint=ENDPOINTS["search"], payload=payload, timeout=timeout)


async def images(
    query: str,
    *,
    num: int | None = None,
    page: int | None = None,
    gl: str | None = None,
    hl: str | None = None,
    tbs: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """图片搜索：返回 {images:[{title,imageUrl,imageWidth,imageHeight,thumbnailUrl,
    thumbnailWidth,thumbnailHeight,source,domain,link,googleUrl,position}], credits}。"""
    payload = _build_payload(
        mode="images", query=query, image_url=None,
        num=num, page=page, gl=gl, hl=hl, tbs=tbs,
    )
    return await _post_with_retry(endpoint=ENDPOINTS["images"], payload=payload, timeout=timeout)


async def videos(
    query: str,
    *,
    num: int | None = None,
    page: int | None = None,
    gl: str | None = None,
    hl: str | None = None,
    tbs: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """视频搜索：返回 {videos:[{title,link,snippet,imageUrl,source,date,position}], credits}。"""
    payload = _build_payload(
        mode="videos", query=query, image_url=None,
        num=num, page=page, gl=gl, hl=hl, tbs=tbs,
    )
    return await _post_with_retry(endpoint=ENDPOINTS["videos"], payload=payload, timeout=timeout)


async def lens(
    image_url: str,
    *,
    query: str | None = None,
    num: int | None = None,
    page: int | None = None,
    gl: str | None = None,
    hl: str | None = None,
    tbs: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """以图搜图：返回 {organic:[{title,source,link,imageUrl,thumbnailUrl}], credits}。

    可选 query 参数：在指定图片的基础上加文字约束（Serper lens 端点支持 q+url
    同时出现，用于精确化搜索）。
    """
    payload = _build_payload(
        mode="lens", query=query, image_url=image_url,
        num=num, page=page, gl=gl, hl=hl, tbs=tbs,
    )
    return await _post_with_retry(endpoint=ENDPOINTS["lens"], payload=payload, timeout=timeout)

