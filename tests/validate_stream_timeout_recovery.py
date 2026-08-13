"""OpenAI 兼容流读取超时的回归测试。"""
import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import apitelegramchat.ai_handlers as handlers  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_chunk(content: str):
    delta = type("Delta", (), {"content": content, "reasoning": "", "tool_calls": []})()
    choice = type("Choice", (), {"delta": delta})()
    return type("Chunk", (), {"choices": [choice], "usage": None})()


class EmptyReadTimeoutStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise httpx.ReadTimeout("no first SSE event")


class OneChunkStream:
    def __init__(self, content: str):
        self.content = content
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return make_chunk(self.content)


class PartialReadTimeoutStream:
    def __init__(self):
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index == 0:
            self.index += 1
            return make_chunk("partial")
        raise httpx.ReadTimeout("timed out after visible content")


class Completions:
    def __init__(self, streams):
        self.streams = list(streams)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.streams.pop(0)


def fake_client(streams):
    completions = Completions(streams)
    client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    return client, completions


async def test_first_event_timeout_retries_once() -> None:
    client, completions = fake_client([EmptyReadTimeoutStream(), OneChunkStream("recovered")])
    builder = handlers.RichMessageBuilder(chat_id=71)

    original_sleep = handlers.asyncio.sleep

    async def no_wait(_delay):
        return None

    handlers.asyncio.sleep = no_wait
    try:
        final_content, _usage, _history = await handlers._agentic_loop_openai_compat(
            client,
            "test-model",
            [{"role": "user", "content": "test"}],
            "test",
            builder,
            tools=[],
            supports_tools=False,
        )
    finally:
        handlers.asyncio.sleep = original_sleep

    require(final_content == "recovered", "首包超时后的重试应交付第二次流的最终内容")
    require(len(completions.requests) == 2, "首个增量前读取超时时必须且只能重试一次")
    require("recovered" in builder._build_html(), "恢复后的增量必须写入草稿构建器")


async def test_partial_stream_timeout_is_not_replayed() -> None:
    client, completions = fake_client([PartialReadTimeoutStream(), OneChunkStream("must-not-run")])
    builder = handlers.RichMessageBuilder(chat_id=72)

    try:
        await handlers._agentic_loop_openai_compat(
            client,
            "test-model",
            [{"role": "user", "content": "test"}],
            "test",
            builder,
            tools=[],
            supports_tools=False,
        )
    except httpx.ReadTimeout:
        pass
    else:
        raise AssertionError("已有可见增量后的 ReadTimeout 必须向上抛出，禁止重放半个模型回合")

    require(len(completions.requests) == 1, "已有增量后不得重试相同模型请求")
    require("partial" in builder._build_html(), "已收到的增量必须保留在草稿状态中")


async def main() -> None:
    await test_first_event_timeout_retries_once()
    await test_partial_stream_timeout_is_not_replayed()
    print("stream timeout recovery validation: PASS")


if __name__ == "__main__":
    asyncio.run(main())
