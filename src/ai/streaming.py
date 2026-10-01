"""AI 响应流的应用层生命周期：idle / total deadline 与确定性清理。

底层 HTTP read timeout 只约束"两次读之间的字节间隔"，网关发送 SSE 心跳
注释行即可让它永不触发。这里按"两个真实事件之间的间隔"和"整条流的总时长"
再加一层闸门，所有 bridge 共用。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from ai.errors import AIStreamTimeoutError

logger = logging.getLogger(__name__)

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
            except asyncio.CancelledError:
                raise
            except Exception:
                # best-effort 清理：记录但不覆盖原始异常。
                logger.debug("流清理失败（%s）", type(target).__name__, exc_info=True)
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

    超时归因：闸门等待用 ``asyncio.wait`` 对 ``__anext__()`` 与
    ``sleep(wait)`` 显式竞争——``__anext__`` 先完成时事件/异常原样采用
    （流自身抛出的 TimeoutError 不会被误判为闸门触发），仅当 sleep
    先完成才判定为闸门超时；两任务同一轮完成时优先消费事件，避免
    临界到达的最后一个 chunk 丢失。
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
                    raise AIStreamTimeoutError(
                        f"AI stream exceeded total timeout ({total:.0f}s)", kind="total",
                    )
                if wait is None or remaining < wait:
                    wait, limit_kind = remaining, "total"

            if wait is None:
                # 无闸门：直接等待，流自身的异常（含 TimeoutError）原样上抛。
                try:
                    event = await iterator.__anext__()
                except StopAsyncIteration:
                    return
                yield event
                continue

            # 有闸门：显式竞争区分"应用层闸门触发"与"流自身抛出的超时"。
            next_task = asyncio.ensure_future(iterator.__anext__())
            gate_task = asyncio.ensure_future(asyncio.sleep(wait))
            try:
                done, _ = await asyncio.wait(
                    {next_task, gate_task}, return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                next_task.cancel()
                gate_task.cancel()
                await asyncio.gather(next_task, gate_task, return_exceptions=True)
                raise
            gate_task.cancel()
            await asyncio.gather(gate_task, return_exceptions=True)

            if next_task not in done:
                # 闸门先触发：回收挂起的 __anext__ 再抛闸门异常。
                next_task.cancel()
                await asyncio.gather(next_task, return_exceptions=True)
                raise AIStreamTimeoutError(
                    f"AI stream {limit_kind} timeout ({wait:.0f}s)", kind=limit_kind,
                )
            try:
                event = next_task.result()
            except StopAsyncIteration:
                return
            yield event
    finally:
        await _close_quietly(stream, iterator)
