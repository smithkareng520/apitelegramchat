"""AI 响应流的应用层生命周期：idle / total deadline 与确定性清理。

底层 HTTP read timeout 只约束"两次读之间的字节间隔"，网关发送 SSE 心跳
注释行即可让它永不触发。这里按"两个真实事件之间的间隔"和"整条流的总时长"
再加一层闸门，所有 bridge 共用。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from ai.errors import AIStreamTimeoutError

DEFAULT_STREAM_IDLE_TIMEOUT = 300.0
DEFAULT_STREAM_TOTAL_TIMEOUT = 1800.0


def _env_seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ[name]))
    except (KeyError, ValueError):
        return default


def get_stream_timeouts() -> tuple[float, float]:
    """返回 (idle, total) 秒数；0 表示关闭对应闸门。"""
    return (
        _env_seconds("AI_STREAM_IDLE_TIMEOUT_SECONDS", DEFAULT_STREAM_IDLE_TIMEOUT),
        _env_seconds("AI_STREAM_TOTAL_TIMEOUT_SECONDS", DEFAULT_STREAM_TOTAL_TIMEOUT),
    )


async def _close_quietly(stream: Any, iterator: Any) -> None:
    """释放迭代器与底层连接。清理失败不能覆盖原始异常，但取消信号必须放行。

    关键：OpenAI SDK 的流对象 ``__aiter__`` 返回 ``self``，且只有同步
    ``close()``、没有 ``aclose()``。若按"迭代器只试 aclose"的旧逻辑，
    同一对象找不到 ``aclose`` 就什么都不调，连接在超时后滞留连接池。
    因此迭代器也要依次尝试 ``close()``（awaitable 结果会被等待）；
    底层 stream 与迭代器不是同一对象时，两边各自清理，同一对象不重复。
    """
    targets = [(iterator, ("aclose", "close"))]
    if stream is not iterator:
        targets.append((stream, ("aclose", "close")))
    for target, names in targets:
        for name in names:
            close = getattr(target, name, None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 —— best-effort 清理
                pass
            break


async def iter_async_stream(
    stream: Any,
    *,
    idle_timeout: float | None = None,
    total_timeout: float | None = None,
) -> AsyncIterator[Any]:
    """逐个产出事件；超过 idle/total 期限抛 :class:`AIStreamTimeoutError`。

    退出时（正常结束 / 超时 / 异常 / 取消）一律关闭迭代器和底层流，
    避免连接滞留在连接池里。
    """
    default_idle, default_total = get_stream_timeouts()
    idle = default_idle if idle_timeout is None else max(0.0, idle_timeout)
    total = default_total if total_timeout is None else max(0.0, total_timeout)

    iterator = stream.__aiter__()
    started = time.monotonic()
    try:
        while True:
            wait: float | None = idle or None
            limit_kind = "idle"
            if total:
                remaining = total - (time.monotonic() - started)
                if remaining <= 0:
                    raise AIStreamTimeoutError(f"AI stream exceeded total timeout ({total:.0f}s)")
                if wait is None or remaining < wait:
                    wait, limit_kind = remaining, "total"

            waited_from = time.monotonic()
            try:
                event = await (
                    iterator.__anext__()
                    if wait is None
                    else asyncio.wait_for(iterator.__anext__(), timeout=wait)
                )
            except StopAsyncIteration:
                return
            except (asyncio.TimeoutError, TimeoutError) as exc:  # 3.10 中二者不是同一个类
                # 流自身抛出的 TimeoutError（如 aiohttp sock_read）不是我们的闸门，原样上抛。
                if wait is None or time.monotonic() - waited_from < wait - 0.05:
                    raise
                limit = idle if limit_kind == "idle" else total
                raise AIStreamTimeoutError(
                    f"AI stream {limit_kind} timeout ({limit:.0f}s)"
                ) from exc
            yield event
    finally:
        await _close_quietly(stream, iterator)
