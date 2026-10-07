"""本轮修复的回归测试（2026-10）：

1. streaming._close_quietly：``__aiter__`` 返回 self 且只有 ``close()``
   的 openai SDK 流对象，超时/取消/正常结束后连接必须被释放（修复前
   迭代器只找 aclose、同对象又不追加 stream target，close 永不调用）；
2. agentic_loops 首增量零输出重试：openai SDK 3.x 传输层是 httpx2，
   httpx2.ReadTimeout 必须纳入重试判定（修复前只捕 httpx.ReadTimeout，
   重试从未生效）；
3. gemini_bridge 流中 error / promptFeedback.blockReason 事件不再被
   静默吞掉，转成 AIResponseParseError 上抛；
4. responses_bridge 合成总结流：dict 形状事件经 event_type/event_field
   读取后正常累积（修复前 getattr 全部丢弃，总结静默变空）；
5. media_generation 生成结果图片下载：25MB 体积上限对三处下载路径
   统一生效（Content-Length 预检 + readany 循环限读）。
"""
import asyncio
import base64
import io
import json

import httpx2
import pytest

from ai.streaming import AIStreamTimeoutError, iter_async_stream


# 1) openai SDK 形状（__aiter__ -> self，只有 close()）的流对象清理
class OpenAISelfIteratorStream:
    """模拟 openai SDK 的 AsyncStream：__aiter__ 返回 self、无 aclose、
    只有（同步）close()。旧 _close_quietly 对这种形状什么都不调。"""

    def __init__(self, events, delay=0):
        self.events = list(events)
        self.delay = delay
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.events.pop(0)

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_close_quietly_releases_self_iterator_stream_on_timeout():
    stream = OpenAISelfIteratorStream([1], delay=10)
    with pytest.raises(AIStreamTimeoutError, match="idle timeout"):
        async for _ in iter_async_stream(stream, idle_timeout=0.02, total_timeout=0):
            pass
    assert stream.closed, "超时后 openai SDK 流对象（self-iterator + close()）必须被关闭"


@pytest.mark.asyncio
async def test_close_quietly_releases_self_iterator_stream_on_success():
    stream = OpenAISelfIteratorStream([1, 2])
    assert [x async for x in iter_async_stream(stream, idle_timeout=1, total_timeout=1)] == [1, 2]
    assert stream.closed


@pytest.mark.asyncio
async def test_close_quietly_releases_self_iterator_stream_on_cancel():
    stream = OpenAISelfIteratorStream([1], delay=10)

    async def consume():
        async for _ in iter_async_stream(stream, idle_timeout=0, total_timeout=0):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


# 2) httpx2.ReadTimeout 属于首增量零输出重试判定集合
def test_httpx2_read_timeout_is_retryable_for_zero_output_retry():
    from ai.agentic_loops import _STREAM_READ_TIMEOUT_ERRORS

    # openai SDK 3.x 传输层是 httpx2：其 ReadTimeout 必须在零输出重试集合内
    assert httpx2.ReadTimeout in _STREAM_READ_TIMEOUT_ERRORS
    # 兼容保留：httpx（v1）与应用层闸门异常同样纳入
    import httpx

    assert httpx.ReadTimeout in _STREAM_READ_TIMEOUT_ERRORS
    from ai.streaming import AIStreamTimeoutError as _ASTE

    assert _ASTE in _STREAM_READ_TIMEOUT_ERRORS


# 3) gemini 流中 error / 安全拦截事件上抛
class _FakeAiohttpLikeContent:
    def __init__(self, lines):
        self._lines = lines

    async def __aiter__(self):
        for line in self._lines:
            yield line


class _FakeAiohttpLikeResponse:
    def __init__(self, lines):
        self.content = _FakeAiohttpLikeContent(lines)


@pytest.mark.asyncio
async def test_gemini_stream_error_event_is_surfaced():
    from ai.gemini_bridge import _iter_gemini_stream_events

    chunks = [
        b'data: {"error": {"code": 503, "message": "model overloaded", "status": "UNAVAILABLE"}}\n\n',
    ]
    events = [ev async for ev in _iter_gemini_stream_events(_FakeAiohttpLikeResponse(chunks))]
    assert events and events[0]["kind"] == "error"
    assert "model overloaded" in events[0]["message"]
    assert events[0]["status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_gemini_prompt_feedback_block_is_surfaced():
    from ai.gemini_bridge import _iter_gemini_stream_events

    chunks = [
        b'data: {"promptFeedback": {"blockReason": "SAFETY"}}\n\n',
        b'data: {"candidates": [], "usageMetadata": {"totalTokenCount": 1}}\n\n',
    ]
    events = [ev async for ev in _iter_gemini_stream_events(_FakeAiohttpLikeResponse(chunks))]
    errors = [e for e in events if e["kind"] == "error"]
    assert errors and "SAFETY" in errors[0]["message"]
    assert errors[0].get("safety_block") is True
    # 安全拦截之外的 usage 事件仍然正常产出
    assert any(e["kind"] == "usage" for e in events)


# 4) responses 合成总结流：dict 形状事件必须被读取
def test_response_events_helpers_accept_dict_events():
    from ai.response_events import event_field, event_type

    ev = {"type": "response.output_text.delta", "delta": "你好"}
    assert event_type(ev) == "response.output_text.delta"
    assert event_field(ev, "delta", "") == "你好"
    assert event_type({"type": None}) == ""
    # 键缺失时取默认值；键存在但值为 None 时 get 返回 None（与 getattr 语义一致）
    assert event_field({}, "delta", "") == ""
    assert event_field({"delta": None}, "delta", "") is None


# 5) 生成结果图片下载 25MB 上限
def _png_bytes(width: int = 8, height: int = 8) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeAiohttpContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def readany(self):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeResponse:
    def __init__(self, chunks, headers=None):
        self.content = _FakeAiohttpContent(chunks)
        self.headers = headers or {}


@pytest.mark.asyncio
async def test_read_remote_image_capped_rejects_oversized_content_length():
    from ai.media_generation import _read_remote_image_capped

    resp = _FakeResponse([b"never-read"], headers={"Content-Length": str(26 * 1024 * 1024)})
    assert await _read_remote_image_capped(resp) is None
    # 预检直接拒绝：不应真正读取任何字节
    assert resp.content._chunks == [b"never-read"]


@pytest.mark.asyncio
async def test_read_remote_image_capped_rejects_accumulated_oversize():
    from ai.media_generation import _read_remote_image_capped

    big = b"x" * (1024 * 1024)
    resp = _FakeResponse([big] * 30, headers={})  # 30MB 分块，无 Content-Length
    assert await _read_remote_image_capped(resp) is None


@pytest.mark.asyncio
async def test_read_remote_image_capped_returns_full_bytes_within_limit():
    from ai.media_generation import _read_remote_image_capped

    payload = _png_bytes()
    resp = _FakeResponse([payload[:1000], payload[1000:]], headers={"Content-Length": str(len(payload))})
    assert await _read_remote_image_capped(resp) == payload
