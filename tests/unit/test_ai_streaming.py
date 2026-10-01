import asyncio

import pytest

from ai.streaming import AIStreamTimeoutError, iter_async_stream


class FakeStream:
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

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_yields_events_and_closes():
    stream = FakeStream([1, 2])
    assert [x async for x in iter_async_stream(stream, idle_timeout=1, total_timeout=2)] == [1, 2]
    assert stream.closed


@pytest.mark.asyncio
async def test_stream_idle_timeout_is_bounded():
    stream = FakeStream([1], delay=0.05)
    with pytest.raises(AIStreamTimeoutError, match="idle timeout"):
        async for _ in iter_async_stream(stream, idle_timeout=0.01, total_timeout=1):
            pass
    assert stream.closed


@pytest.mark.asyncio
async def test_stream_total_timeout_is_bounded():
    stream = FakeStream([1, 2, 3], delay=0.03)
    with pytest.raises(AIStreamTimeoutError, match="total timeout"):
        async for _ in iter_async_stream(stream, idle_timeout=1, total_timeout=0.06):
            pass
    assert stream.closed


@pytest.mark.asyncio
async def test_stream_zero_disables_deadlines():
    stream = FakeStream([1], delay=0.02)
    assert [x async for x in iter_async_stream(stream, idle_timeout=0, total_timeout=0)] == [1]


class HangingStream:
    """首个事件永远不来；用 close()（同步/异步各一种）暴露底层连接是否被释放。"""

    def __init__(self, sync_close: bool):
        self.closed = False
        self._sync_close = sync_close

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        await asyncio.sleep(3600)
        yield 1

    def close(self):
        self.closed = True
        if self._sync_close:
            return None

        async def _noop():
            return None
        return _noop()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_close", [True, False])
async def test_timeout_closes_underlying_stream(sync_close):
    stream = HangingStream(sync_close)
    with pytest.raises(AIStreamTimeoutError, match="idle timeout"):
        async for _ in iter_async_stream(stream, idle_timeout=0.02, total_timeout=0):
            pass
    assert stream.closed


class RaisingStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise TimeoutError("sock_read")


@pytest.mark.asyncio
async def test_inner_timeout_is_not_relabelled_as_deadline():
    with pytest.raises(TimeoutError) as info:
        async for _ in iter_async_stream(RaisingStream(), idle_timeout=5, total_timeout=10):
            pass
    assert not isinstance(info.value, AIStreamTimeoutError)


@pytest.mark.asyncio
async def test_cancellation_propagates():
    stream = FakeStream([1], delay=10)

    async def consume():
        async for _ in iter_async_stream(stream, idle_timeout=0, total_timeout=0):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


def test_env_seconds_falls_back_on_garbage(monkeypatch):
    from ai.streaming import get_stream_timeouts
    monkeypatch.setenv("AI_STREAM_IDLE_TIMEOUT_SECONDS", "abc")
    monkeypatch.setenv("AI_STREAM_TOTAL_TIMEOUT_SECONDS", "-5")
    assert get_stream_timeouts() == (300.0, 0.0)
