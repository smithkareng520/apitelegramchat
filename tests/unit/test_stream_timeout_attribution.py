"""第一轮健壮性修复回归：超时归因、闸门竞争、SSE 多行 data、后台任务跟踪。

覆盖 2026-10 代码审查发现并修复的四类问题：
1. ``iter_async_stream`` 的闸门与上游超时误判（旧实现依赖 50ms 时间差启发式）；
2. ``AIStreamTimeoutError.kind``：total 硬期限绝不参与零输出重试；
3. ``tool_call_loop`` 外层等待预算与工具内部超时混同（超时文案张冠李戴）；
4. Gemini SSE 解析不依赖行交付粒度（多行 data / \\r\\n / 无尾空行 / 非法载荷可见）。
"""
from __future__ import annotations

import asyncio

import pytest

from ai.errors import AIStreamTimeoutError
from ai.streaming import AIStreamTimeoutError as _AIStreamTimeoutErrorAlias
import ai.tool_call_loop as tcl
from ai.tool_call_loop import _dispatch_capturing_internal_timeout, _ToolInternalTimeout
from ai.streaming import iter_async_stream


assert _AIStreamTimeoutErrorAlias is AIStreamTimeoutError  # 单一来源


# ---------------------------------------------------------------------------
# 1) iter_async_stream：闸门竞争与超时归因
# ---------------------------------------------------------------------------
class _FakeStream:
    """按脚本交付事件的最小流对象；记录 aclose 调用。"""

    def __init__(self, script):
        # script: 列表，元素为 ("event", value) / ("sleep", seconds) / ("raise", exc)
        self._script = list(script)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._script:
            raise StopAsyncIteration
        action, value = self._script.pop(0)
        if action == "event":
            return value
        if action == "sleep":
            await asyncio.sleep(value)
            return await self.__anext__()
        raise value

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_upstream_timeouterror_passes_through_unchanged():
    """流自身抛出的 TimeoutError 必须原样上抛，不得包装成应用层闸门异常。"""
    stream = _FakeStream([("raise", TimeoutError("sock_read expired"))])
    with pytest.raises(TimeoutError) as exc_info:
        async for _ in iter_async_stream(stream, idle_timeout=5.0, total_timeout=60.0):
            pass
    assert not isinstance(exc_info.value, AIStreamTimeoutError)
    assert "sock_read" in str(exc_info.value)
    assert stream.closed


@pytest.mark.asyncio
async def test_idle_gate_raises_ai_stream_timeout_with_kind():
    stream = _FakeStream([("sleep", 1.0)])
    with pytest.raises(AIStreamTimeoutError) as exc_info:
        async for _ in iter_async_stream(stream, idle_timeout=0.05, total_timeout=0.0):
            pass
    assert exc_info.value.kind == "idle"


@pytest.mark.asyncio
async def test_total_gate_raises_ai_stream_timeout_with_kind():
    """idle 关闭（0）时 total 闸门独立生效，且 kind 标记为 total。"""
    stream = _FakeStream([("sleep", 1.0)])
    with pytest.raises(AIStreamTimeoutError) as exc_info:
        async for _ in iter_async_stream(stream, idle_timeout=0.0, total_timeout=0.1):
            pass
    assert exc_info.value.kind == "total"


@pytest.mark.asyncio
async def test_events_before_gate_are_all_delivered():
    """闸门触发前已产出的事件一个都不能丢。"""
    stream = _FakeStream([
        ("event", "a"), ("event", "b"), ("event", "c"), ("sleep", 1.0),
    ])
    received: list = []
    with pytest.raises(AIStreamTimeoutError):
        async for event in iter_async_stream(stream, idle_timeout=0.05, total_timeout=0.0):
            received.append(event)
    assert received == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_stream_closed_after_normal_completion_and_cancel():
    stream = _FakeStream([("event", "a")])
    events = [e async for e in iter_async_stream(stream, idle_timeout=1.0)]
    assert events == ["a"]
    assert stream.closed

    cancelled_stream = _FakeStream([("event", "x"), ("sleep", 5.0)])
    async def consume():
        async for _ in iter_async_stream(cancelled_stream, idle_timeout=10.0):
            await asyncio.sleep(0.01)
    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled_stream.closed


def test_ai_stream_timeout_error_kind_metadata():
    assert AIStreamTimeoutError("x").kind == "idle"
    assert AIStreamTimeoutError("y", kind="total").kind == "total"


# ---------------------------------------------------------------------------
# 2) total 硬期限绝不重试（anthropic 分类器）
# ---------------------------------------------------------------------------
def test_anthropic_never_retries_total_gate():
    from ai.anthropic_bridge import _is_retryable_stream_error

    assert _is_retryable_stream_error(AIStreamTimeoutError("idle hit"))
    assert not _is_retryable_stream_error(AIStreamTimeoutError("total hit", kind="total"))


# ---------------------------------------------------------------------------
# 3) 工具调用：外层等待预算 vs 工具内部超时
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tool_internal_timeout_is_retagged(monkeypatch):
    async def fake_dispatch(fn_name, fn_args, chat_id=None, progress_callback=None):
        raise TimeoutError("fetch_url timed out after 30s")

    monkeypatch.setattr(tcl, "dispatch_tool_call", fake_dispatch)
    with pytest.raises(_ToolInternalTimeout, match="fetch_url"):
        await _dispatch_capturing_internal_timeout("fetch_url", {}, chat_id=1)


@pytest.mark.asyncio
async def test_tool_success_passes_through_wrapper(monkeypatch):
    async def fake_dispatch(fn_name, fn_args, chat_id=None, progress_callback=None):
        return "done"

    monkeypatch.setattr(tcl, "dispatch_tool_call", fake_dispatch)
    result = await asyncio.wait_for(
        _dispatch_capturing_internal_timeout("bash", {}, chat_id=1), timeout=1.0)
    assert result == "done"


@pytest.mark.asyncio
async def test_outer_budget_expiry_still_raises_timeout(monkeypatch):
    async def fake_dispatch(fn_name, fn_args, chat_id=None, progress_callback=None):
        await asyncio.sleep(1.0)
        return "late"

    monkeypatch.setattr(tcl, "dispatch_tool_call", fake_dispatch)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _dispatch_capturing_internal_timeout("bash", {}, chat_id=1), timeout=0.05)


@pytest.mark.asyncio
async def test_tool_cancellation_is_not_retagged(monkeypatch):
    async def fake_dispatch(fn_name, fn_args, chat_id=None, progress_callback=None):
        await asyncio.sleep(5.0)
        return "never"

    monkeypatch.setattr(tcl, "dispatch_tool_call", fake_dispatch)
    inner = asyncio.ensure_future(
        _dispatch_capturing_internal_timeout("bash", {}, chat_id=1))
    await asyncio.sleep(0.02)
    inner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await inner


# ---------------------------------------------------------------------------
# 4) Gemini SSE：不依赖行交付粒度的事件切分
# ---------------------------------------------------------------------------
class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeResp:
    def __init__(self, chunks):
        self.content = _FakeContent(chunks)


async def _collect(chunks):
    from ai.gemini_bridge import _iter_gemini_stream_events

    return [ev async for ev in _iter_gemini_stream_events(_FakeResp(chunks))]


@pytest.mark.asyncio
async def test_gemini_sse_multi_line_data_event_is_joined():
    """同一事件的多行 data 字段按 SSE 规范以 \\n 连接后整体解析。"""
    events = await _collect([
        b'data: {"candidates": [{"content": {"parts": [',
        b'data: {"text": "hi"}]}}]}\n\n',
    ])
    assert [e["kind"] for e in events] == ["text"]
    assert events[0]["text"] == "hi"


@pytest.mark.asyncio
async def test_gemini_sse_event_without_trailing_blank_line_is_flushed():
    """流结束时缓冲中的最后一个事件不能丢。"""
    events = await _collect([
        b'data: {"candidates": [{"content": {"parts": [{"text": "tail"}]}}]}\n',
    ])
    assert events and events[0]["kind"] == "text" and events[0]["text"] == "tail"


@pytest.mark.asyncio
async def test_gemini_sse_crlf_and_multiline_chunk():
    chunks = [
        b'data: {"candidates": [{"content": {"parts": [{"text": "a"}]}}]}\r\n\r\n',
        b'data: {"candidates": [{"content": {"parts": [{"text": "b"}]}}]}\r\n\r\n',
    ]
    events = await _collect(chunks)
    assert [e.get("text") for e in events if e["kind"] == "text"] == ["a", "b"]


@pytest.mark.asyncio
async def test_gemini_sse_invalid_payload_is_visible(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="ai.gemini_bridge"):
        events = await _collect([b'data: {oops\n\n'])
    assert events == []
    assert any("无法解析的 SSE 载荷" in rec.message for rec in caplog.records)
