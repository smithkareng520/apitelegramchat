"""LLM 供应商余额查询（DeepSeek / OpenRouter）（自 utils.py 拆出）。"""

import asyncio
from typing import Any

import aiohttp

from config import DEEPSEEK_API_KEY, OPENROUTER_API_KEY

import logging

logger = logging.getLogger(__name__)


class BalanceResult(dict):
    """统一的余额查询结果，兼容字典访问。"""


async def _fetch_json(session: aiohttp.ClientSession, url: str, api_key: str) -> tuple[int, Any, str | None]:
    """发送一次余额请求并返回 HTTP 状态码、JSON 和错误信息。"""
    if not api_key:
        return 0, None, "未配置 API Key"
    try:
        async with session.get(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
        ) as response:
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                data = None
            if response.status != 200:
                return response.status, data, f"HTTP {response.status}"
            return response.status, data, None
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.debug("余额查询请求失败: %s", url, exc_info=True)
        return 0, None, str(exc)[:100]


async def _query_deepseek_balance(session: aiohttp.ClientSession) -> BalanceResult:
    status, data, error = await _fetch_json(
        session,
        "https://api.deepseek.com/user/balance",
        DEEPSEEK_API_KEY,
    )
    if error:
        return BalanceResult(provider="DeepSeek", ok=False, error=error)
    try:
        info = data["balance_infos"][0]
        return BalanceResult(
            provider="DeepSeek",
            ok=True,
            available=data.get("is_available"),
            remaining=info["total_balance"],
            currency=info["currency"],
            granted_balance=info.get("granted_balance"),
            topped_up_balance=info.get("topped_up_balance"),
        )
    except (KeyError, IndexError, TypeError):
        return BalanceResult(provider="DeepSeek", ok=False, error="响应格式异常")


async def _query_openrouter_balance(session: aiohttp.ClientSession) -> BalanceResult:
    status, data, error = await _fetch_json(
        session,
        "https://openrouter.ai/api/v1/key",
        OPENROUTER_API_KEY,
    )
    if error:
        return BalanceResult(provider="OpenRouter", ok=False, error=error)
    try:
        info = data["data"]
        remaining = info.get("limit_remaining")
        return BalanceResult(
            provider="OpenRouter",
            ok=True,
            available=True,
            remaining=remaining,
            currency="USD",
            limit=info.get("limit"),
            usage=info.get("usage"),
            usage_daily=info.get("usage_daily"),
            usage_monthly=info.get("usage_monthly"),
            unlimited=remaining is None,
        )
    except (KeyError, TypeError):
        return BalanceResult(provider="OpenRouter", ok=False, error="响应格式异常")


_BALANCE_QUERYERS = {
    "deepseek": _query_deepseek_balance,
    "ds": _query_deepseek_balance,
    "openrouter": _query_openrouter_balance,
    "or": _query_openrouter_balance,
}


async def query_provider_balances(provider: str | None = None) -> list[BalanceResult]:
    """并发查询已适配厂商的余额；不传 provider 时查询全部已适配厂商。"""
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if provider:
            queryer = _BALANCE_QUERYERS.get(provider.lower())
            if queryer is None:
                return [BalanceResult(provider=provider, ok=False, error="暂未适配公开余额接口")]
            return [await queryer(session)]
        return list(await asyncio.gather(
            _query_deepseek_balance(session),
            _query_openrouter_balance(session),
        ))
