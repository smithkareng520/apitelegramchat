"""非法 tool-call JSON 参数不会污染下一轮模型请求的回归测试。"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import apitelegramchat.ai_handlers as handlers  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        self.index = 0
        return self

    async def __anext__(self):
        if self.index >= len(self.chunks):
            raise StopAsyncIteration
        value = self.chunks[self.index]
        self.index += 1
        return value


class Completions:
    def __init__(self):
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            function = type(
                "Function", (), {"name": "web_search", "arguments": '{"query":"unfinished"'}
            )()
            tool_call = type("ToolCall", (), {"index": 0, "id": "bad-json-call", "function": function})()
            delta = type("Delta", (), {"content": "", "reasoning": "", "tool_calls": [tool_call]})()
        else:
            delta = type("Delta", (), {"content": "已安全恢复。", "reasoning": "", "tool_calls": []})()
        choice = type("Choice", (), {"delta": delta})()
        return Stream([type("Chunk", (), {"choices": [choice], "usage": None})()])


async def test_invalid_arguments_are_normalized_before_history_replay() -> None:
    completions = Completions()
    client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    builder = handlers.RichMessageBuilder(chat_id=81)
    builder.flush = AsyncMock()

    original_dispatch = handlers.dispatch_tool_call
    original_format = handlers.format_tool_result
    dispatch_mock = AsyncMock(return_value="unexpected execution")
    handlers.dispatch_tool_call = dispatch_mock
    handlers.format_tool_result = AsyncMock(return_value=("ignored", "<p>ignored</p>"))
    try:
        final_content, _usage, history = await handlers._agentic_loop_openai_compat(
            client,
            "test-model",
            [{"role": "user", "content": "test malformed tool arguments"}],
            "test",
            builder,
            tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            supports_tools=True,
        )
    finally:
        handlers.dispatch_tool_call = original_dispatch
        handlers.format_tool_result = original_format

    require(final_content == "已安全恢复。", "非法参数工具错误后，模型应可继续产生最终回复")
    require(len(completions.requests) == 2, "下一轮请求必须成功发出，而不是被网关 400 拒绝")
    replayed_messages = completions.requests[1]["messages"]
    assistant_with_call = next(
        message for message in replayed_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    arguments = assistant_with_call["tool_calls"][0]["function"]["arguments"]
    parsed = json.loads(arguments)
    require(
        handlers._INVALID_TOOL_ARGUMENTS_KEY in parsed,
        "写入 assistant 历史的 arguments 必须是可解析 JSON 且带恢复标记",
    )
    require(dispatch_mock.await_count == 0, "非法 JSON 调用不得降级为空参数并实际执行工具")
    paired_tool = next(message for message in replayed_messages if message.get("role") == "tool")
    require("malformed JSON" in paired_tool["content"], "模型必须收到可操作的参数错误说明")
    require(any(message.get("role") == "assistant" for message in history), "新历史应包含最终回复")


def test_valid_arguments_are_canonical_json() -> None:
    normalized, corrected = handlers._normalize_tool_arguments('{ "query" : "正常" }')
    require(not corrected, "合法对象 JSON 不应被标为错误")
    require(json.loads(normalized) == {"query": "正常"}, "合法参数应保持语义并规范化")

    normalized, corrected = handlers._normalize_tool_arguments('["not", "object"]')
    require(corrected, "非对象 JSON 不能作为 function.arguments 传回 provider")
    require(handlers._INVALID_TOOL_ARGUMENTS_KEY in json.loads(normalized), "非对象参数必须转为错误对象")


async def main() -> None:
    test_valid_arguments_are_canonical_json()
    await test_invalid_arguments_are_normalized_before_history_replay()
    print("tool argument JSON validation: PASS")


if __name__ == "__main__":
    asyncio.run(main())
