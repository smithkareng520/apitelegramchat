#!/usr/bin/env python3
"""工具调用预算与文本伪工具调用清理的独立回归测试。"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import apitelegramchat.ai_handlers as handlers  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeBuilder:
    def __init__(self) -> None:
        self.chat_id = 123
        self._tool_groups = []
        self.items = []
        self.updated = []
        self.flushes = []

    def _get_current_group(self) -> int:
        return 0

    def add_tool_item(self, *args, **kwargs) -> None:
        self.items.append((args, kwargs))

    def update_tool_item(self, *args, **kwargs) -> None:
        self.updated.append((args, kwargs))

    async def flush(self, force: bool = False) -> None:
        self.flushes.append(force)


def tool_call(identifier: str, name: str = "bash") -> dict:
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": '{"command":"echo ok"}'},
    }


def test_textual_tool_call_cleaning() -> None:
    raw = (
        "完成了前置检查。<tool_call><function=bash><parameter=command>echo bad"
        "</parameter></function></tool_call>后续说明。"
    )
    cleaned = handlers._strip_textual_tool_calls(raw)
    require(handlers._contains_textual_tool_call(raw), "必须检测到文本伪工具调用")
    require("<tool_call>" not in cleaned and "echo bad" not in cleaned, "伪工具 XML 不得残留")
    require(cleaned == "完成了前置检查。后续说明。", "应保留伪调用前后的正常文字")
    require(handlers.MAX_TOOL_CALLS == 100, "默认真实工具调用预算必须为 100")


async def test_batch_respects_remaining_budget() -> None:
    builder = FakeBuilder()
    loop_messages = []
    history = []
    counter = [98]

    original_dispatch = handlers.dispatch_tool_call
    original_format = handlers.format_tool_result
    dispatch_mock = AsyncMock(return_value="Exit code: 0\nOK")
    format_mock = AsyncMock(return_value=("done", "<p>OK</p>"))
    handlers.dispatch_tool_call = dispatch_mock
    handlers.format_tool_result = format_mock
    try:
        status = await handlers._run_tool_calls_and_append(
            [tool_call("one"), tool_call("two"), tool_call("three")],
            loop_messages,
            history,
            counter,
            "test",
            builder,
            chat_id=builder.chat_id,
        )
    finally:
        handlers.dispatch_tool_call = original_dispatch
        handlers.format_tool_result = original_format

    require(status == "over_limit", "达到第 100 次真实调用后必须进入无工具总结路径")
    require(counter == [100], "计数只能增加至 100，不得超额执行")
    require(len(builder.items) == 2, "剩余预算为 2 时只能注册并执行两个工具")
    # 前两条为实际结果，第三条为跳过说明；每一个模型请求 ID 都必须得到 tool 消息。
    require([message["tool_call_id"] for message in loop_messages] == ["one", "two", "three"], "所有调用 ID 必须获得配对 tool 消息")
    require("Not executed" in loop_messages[-1]["content"], "超出预算的调用必须明确标记为未执行")
    require(dispatch_mock.await_count == 2, "不得执行第 101 次工具调用")


async def test_over_limit_synthesis_never_leaks_tool_xml() -> None:
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
                calls = []
                for index in range(handlers.MAX_TOOL_CALLS):
                    function = type("Function", (), {"name": "bash", "arguments": '{"command":"echo ok"}'})()
                    calls.append(type("ToolCall", (), {"index": index, "id": f"limit-{index}", "function": function})())
                delta = type("Delta", (), {"content": "", "reasoning": "", "tool_calls": calls})()
            else:
                delta = type("Delta", (), {"content": "<tool_call><function=bash/></tool_call>", "reasoning": "", "tool_calls": []})()
            choice = type("Choice", (), {"delta": delta})()
            return Stream([type("Chunk", (), {"choices": [choice], "usage": None})()])

    completions = Completions()
    client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    builder = handlers.RichMessageBuilder(chat_id=2)
    builder.flush = AsyncMock()

    original_dispatch = handlers.dispatch_tool_call
    original_format = handlers.format_tool_result
    dispatch_mock = AsyncMock(return_value="Exit code: 0\nOK")
    handlers.dispatch_tool_call = dispatch_mock
    handlers.format_tool_result = AsyncMock(return_value=("done", "<p>OK</p>"))
    try:
        final_content, _usage, _history = await handlers._agentic_loop_openai_compat(
            client,
            "test-model",
            [{"role": "user", "content": "test"}],
            "test",
            builder,
            tools=[{"type": "function", "function": {"name": "bash", "parameters": {}}}],
            supports_tools=True,
        )
    finally:
        handlers.dispatch_tool_call = original_dispatch
        handlers.format_tool_result = original_format

    require(dispatch_mock.await_count == handlers.MAX_TOOL_CALLS, "第 100 次工具调用应被执行，但不得执行第 101 次")
    require(len(completions.requests) == 2, "预算耗尽后应只发起一次无工具总结请求")
    require("tools" not in completions.requests[1], "超限总结请求必须禁用工具接口")
    require(final_content == handlers._tool_limit_summary(), "仅含伪工具 XML 的总结必须替换为安全状态说明")
    require("<tool_call>" not in builder._build_html(), "草稿中不能遗留伪工具调用 XML")


async def test_builder_retracts_raw_tool_xml() -> None:
    builder = handlers.RichMessageBuilder(chat_id=1)
    raw = "<tool_call><function=bash/></tool_call>"
    builder.blocks = ["准备执行。" + raw]
    builder.block_types = ["text"]
    changed = builder.replace_trailing_text(raw, "")
    require(changed, "构建器应能撤回已流入草稿的 XML")
    require(builder._build_html() == "准备执行。", "撤回后草稿中不能残留工具 XML")


def main() -> None:
    test_textual_tool_call_cleaning()
    asyncio.run(test_batch_respects_remaining_budget())
    asyncio.run(test_over_limit_synthesis_never_leaks_tool_xml())
    asyncio.run(test_builder_retracts_raw_tool_xml())
    print("tool loop validation: PASS")


if __name__ == "__main__":
    main()
