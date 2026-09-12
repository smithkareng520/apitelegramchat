"""连续相同工具错误熔断的回归测试。

背景（代码审查发现的问题）：熔断计数此前借用 DraftManager/
RichMessageBuilder 对象做存储——用 ``setattr(builder, f"_streak:{msg}",
n)`` / ``getattr`` / ``vars(builder)`` 反射遍历清理，把"本轮工具循环"的
临时状态错误地依附在一个只负责 UI 草稿渲染的对象上。修复后改为显式的
``error_streak: dict`` 参数，由调用方（各 bridge 的 ``BridgeLoopState``
或等价的本地字典）在多轮 ``_run_tool_calls_and_append`` 调用之间传递。

本测试覆盖三点：
1. 连续 TOOL_ERROR_STREAK_LIMIT 次相同错误 -> 触发熔断（追加 System
   提示消息，返回值仍是 "continue"，计数清零）；
2. 熔断状态确实存于调用方传入的 error_streak 字典里，而不是 builder
   实例属性上（builder 不应出现任何 "_streak:" 前缀属性）；
3. 中途出现一次成功/不同错误会清空熔断计数（不会被不连续的偶发错误
   误触发）。
"""
import asyncio

import pytest

from ai.rich_message_builder import RichMessageBuilder
from ai._constants import TOOL_ERROR_STREAK_LIMIT
import token_budget


class _FakeEncoding:
    """轻量级 tiktoken.Encoding 替身：按空白粗略切词计数即可满足本测试

    对精确 token 数没有要求（不测截断边界），只需要 encode()/decode()
    可用、不发起任何网络请求——避免测试依赖 tiktoken 编码文件的下载
    （沙箱或离线 CI 环境可能没有出网权限，见 token_budget.py 头部说明）。
    """

    def encode(self, text: str, disallowed_special=()) -> list:
        return list(text.encode("utf-8"))

    def decode(self, tokens: list) -> str:
        return bytes(tokens).decode("utf-8", errors="ignore")


@pytest.fixture(autouse=True)
def _stub_tiktoken_encoding(monkeypatch):
    """本文件全部测试自动生效：避免 _truncate_tool_result 内部的 token
    计数触发真实 tiktoken 下载，与本测试要验证的熔断逻辑无关。"""
    token_budget._get_encoding.cache_clear()
    monkeypatch.setattr(token_budget, "_get_encoding", lambda name: _FakeEncoding())
    yield


def _make_tool_call(call_id: str, name: str = "fetch_url", args: str = '{"url": "https://example.com"}') -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


@pytest.mark.asyncio
async def test_error_streak_uses_explicit_dict_not_builder_attrs(monkeypatch):
    from ai import tool_call_loop

    async def _always_fails(name, arguments, chat_id=None, progress_callback=None):
        return "Exception: tool fetch_url failed - boom"

    monkeypatch.setattr(tool_call_loop, "dispatch_tool_call", _always_fails)

    builder = RichMessageBuilder(chat_id=12345)
    error_streak: dict = {}
    tool_call_count_ref = [0]

    status = None
    for i in range(TOOL_ERROR_STREAK_LIMIT):
        loop_messages: list = []
        new_history_entries: list = []
        status = await tool_call_loop._run_tool_calls_and_append(
            [_make_tool_call(f"call_{i}")],
            loop_messages,
            new_history_entries,
            tool_call_count_ref,
            "test_provider",
            builder,
            chat_id=builder.chat_id,
            tools=None,
            error_streak=error_streak,
        )

    # 熔断应已在最后一轮触发：追加了一条 system/user 提示消息要求模型
    # 换策略，且返回值仍是 "continue"（不是新的状态码）。
    assert status == "continue"
    assert any(
        "STOP retrying the same operation" in (m.text() if hasattr(m, "text") else str(m))
        for m in loop_messages
    )
    # 熔断后计数清零（下一次相同错误需要重新累计到上限才会再次触发）。
    assert error_streak.get("Exception: tool fetch_url failed - boom") == 0

    # 关键回归点：builder 实例不应携带任何 "_streak:" 前缀的动态属性。
    leaked = [attr for attr in vars(builder).keys() if attr.startswith("_streak:")]
    assert leaked == [], f"熔断状态泄漏到了 builder 实例属性上: {leaked}"


@pytest.mark.asyncio
async def test_error_streak_resets_on_success(monkeypatch):
    from ai import tool_call_loop

    call_count = {"n": 0}

    async def _fail_then_succeed(name, arguments, chat_id=None, progress_callback=None):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return "Exception: tool fetch_url failed - boom"
        return "✅ 成功"

    monkeypatch.setattr(tool_call_loop, "dispatch_tool_call", _fail_then_succeed)

    builder = RichMessageBuilder(chat_id=54321)
    error_streak: dict = {}
    tool_call_count_ref = [0]

    # 两次失败（未达上限 3），第三次成功——应清空熔断计数，而不是继续累计。
    for i in range(3):
        await tool_call_loop._run_tool_calls_and_append(
            [_make_tool_call(f"call_{i}")],
            [], [], tool_call_count_ref, "test_provider", builder,
            chat_id=builder.chat_id, tools=None, error_streak=error_streak,
        )

    assert error_streak == {}


@pytest.mark.asyncio
async def test_error_streak_defaults_to_local_dict_when_omitted(monkeypatch):
    """未显式传入 error_streak 时不应报错——退化为函数内局部字典。"""
    from ai import tool_call_loop

    async def _always_fails(name, arguments, chat_id=None, progress_callback=None):
        return "Exception: tool fetch_url failed - boom"

    monkeypatch.setattr(tool_call_loop, "dispatch_tool_call", _always_fails)

    builder = RichMessageBuilder(chat_id=99999)
    tool_call_count_ref = [0]

    status = await tool_call_loop._run_tool_calls_and_append(
        [_make_tool_call("call_0")],
        [], [], tool_call_count_ref, "test_provider", builder,
        chat_id=builder.chat_id, tools=None,
    )
    assert status == "continue"
    assert not any(attr.startswith("_streak:") for attr in vars(builder).keys())
