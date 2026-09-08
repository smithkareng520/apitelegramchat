# =====================================================================
# tests/unit/test_responses_cache_degrade.py — Responses 缓存参数降级重试
# =====================================================================
# 被测关键路径（2026-09-09 bugfix）：
#   线上 xxtf 网关对 gpt-5.6-sol 返回
#     "prompt_cache_breakpoint is not supported on this model"
#   导致整轮失败。修复后：网关拒绝某个缓存字段且本轮零输出时，把该字段
#   记入进程级能力表、剥离后原地重试同一轮（流式主循环 + 非流式 subagent
#   路径），后续请求直接按降级后的参数集发送。
#
# 覆盖：
#   - 错误文本 -> 被拒字段识别（含形状类错误必须不误判为降级信号）
#   - 断点打标 / 剥离（_apply_responses_cache_breakpoints enabled=False）
#   - 流式主循环：SSE error 事件路径降级重试、SDK 异常路径降级重试、
#     无关错误不重试、能力表学习后首次请求即降级
#   - 非流式路径：prompt_cache_options 被拒后剥离重试
# =====================================================================
import asyncio
from types import SimpleNamespace

import pytest

import ai.responses_bridge as rb
from ai.responses_bridge import (
    _agentic_loop_openai_responses,
    _apply_responses_cache_breakpoints,
    _cache_field_unsupported_in_error,
    _responses_cache_capabilities,
    openai_responses_chat_completions_create,
)

PROD_ERROR_TEXT = (
    "[xxtf] Responses API error: prompt_cache_breakpoint is not supported on this model"
)


# ---------------------------------------------------------------------
# 测试基建：假 client / 假流 / 假 builder
# ---------------------------------------------------------------------
class _FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        self._iter = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeResponses:
    """按序回放 outcomes：Exception 直接抛出，list 当作 SSE 事件流。"""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, list):  # list -> SSE 事件流；其余视为完整响应对象
            return _FakeStream(outcome)
        return outcome


class _FakeClient:
    def __init__(self, outcomes):
        self.base_url = "https://xxtf.example/v1"
        self.responses = _FakeResponses(outcomes)


class _FakeBuilder:
    """DraftManager 的最小桩：只实现 agentic 循环触到的方法。"""

    def __init__(self):
        self.chat_id = 7162243624
        self.text_chunks: list[str] = []
        self.ended = 0
        self._tool_groups: list = []

    def append_stream_delta(self, text):
        self.text_chunks.append(text)

    def begin_stream_text(self):
        pass

    def begin_stream_reasoning(self):
        pass

    def on_stream_block_closed(self, kind):
        pass

    def end_stream(self):
        self.ended += 1

    def add_tool_item(self, *args, **kwargs):
        pass

    def update_tool_args(self, *args, **kwargs):
        pass

    def request_flush(self, *args, **kwargs):
        pass

    def finalize_reasoning_block(self):
        pass

    def on_round_boundary(self):
        pass

    async def finalize_turn(self):
        return True

    def add_text(self, text):
        self.text_chunks.append(text)


@pytest.fixture(autouse=True)
def _isolate_capability_table(monkeypatch):
    monkeypatch.setattr(rb, "_RESPONSES_CACHE_CAPABILITIES", {})
    monkeypatch.setattr(rb, "start_chat_action", _noop)
    monkeypatch.setattr(rb, "stop_chat_action", _noop)


async def _noop(*args, **kwargs):
    return None


def _completed_event():
    return SimpleNamespace(
        type="response.completed", response=SimpleNamespace(usage=None)
    )


def _text_delta_event(text):
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _error_event(text):
    return SimpleNamespace(type="error", message=text)


def _messages():
    from core.messages import Message

    return [
        Message.from_openai_dict({"role": "system", "content": "you are helpful"}),
        Message.from_openai_dict({"role": "user", "content": "hello"}),
    ]


def _breakpoint_count(request_kwargs) -> int:
    """统计请求 input 里 content block 上挂的显式断点数。"""
    count = 0
    for item in request_kwargs.get("input") or []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and "prompt_cache_breakpoint" in part:
                count += 1
    return count


# ---------------------------------------------------------------------
# 错误文本识别
# ---------------------------------------------------------------------
def test_detect_breakpoint_unsupported_error():
    assert _cache_field_unsupported_in_error(PROD_ERROR_TEXT) == "prompt_cache_breakpoint"


def test_detect_options_and_key_errors():
    assert _cache_field_unsupported_in_error(
        "prompt_cache_options is not supported on this model"
    ) == "prompt_cache_options"
    assert _cache_field_unsupported_in_error(
        "Unknown parameter: 'prompt_cache_key'."
    ) == "prompt_cache_key"
    assert _cache_field_unsupported_in_error(
        "Unrecognized request argument supplied: prompt_cache_options"
    ) == "prompt_cache_options"


def test_shape_error_is_not_a_degrade_signal():
    # 上一轮修过的形状 bug（boolean 而非对象）必须保持为代码 bug 抛出，
    # 不能被降级逻辑静默吞掉。
    assert _cache_field_unsupported_in_error(
        "Invalid type for 'input[0].content[0].prompt_cache_breakpoint': "
        "expected an object, but got a boolean instead."
    ) is None


def test_unrelated_error_returns_none():
    assert _cache_field_unsupported_in_error("Internal server error (error_id=x)") is None
    assert _cache_field_unsupported_in_error("") is None
    assert _cache_field_unsupported_in_error(None) is None


# ---------------------------------------------------------------------
# 断点打标 / 剥离
# ---------------------------------------------------------------------
def _sample_items():
    return [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "first"},
            {"type": "input_image", "image_url": "https://x/y.png"},
        ]},
        {"type": "function_call", "call_id": "c1", "name": "search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "tail"},
        ]},
    ]


def test_apply_and_strip_breakpoints():
    items = _sample_items()
    assert _apply_responses_cache_breakpoints(items) == 2  # first + tail
    assert _breakpoint_count({"input": items}) == 2

    # enabled=False：剥离已打标（降级重试路径），返回 0
    assert _apply_responses_cache_breakpoints(items, enabled=False) == 0
    assert _breakpoint_count({"input": items}) == 0

    # 剥离后可安全重新打标（幂等）
    assert _apply_responses_cache_breakpoints(items) == 2
    assert _apply_responses_cache_breakpoints(items) == 2  # 重复打标不新增位置


def test_capability_table_isolation_by_base_url():
    c1, c2 = _FakeClient([]), _FakeClient([])
    c2.base_url = "https://other.example/v1"
    caps1 = _responses_cache_capabilities(c1, "gpt-5.6-sol")
    caps2 = _responses_cache_capabilities(c2, "gpt-5.6-sol")
    assert caps1 is not caps2
    assert all(caps1.values()) and all(caps2.values())
    caps1["prompt_cache_breakpoint"] = False
    assert _responses_cache_capabilities(c1, "gpt-5.6-sol")["prompt_cache_breakpoint"] is False
    assert _responses_cache_capabilities(c2, "gpt-5.6-sol")["prompt_cache_breakpoint"] is True


# ---------------------------------------------------------------------
# 流式主循环：SSE error 事件路径
# ---------------------------------------------------------------------
def _run_loop(client, builder):
    return asyncio.run(_agentic_loop_openai_responses(
        client, "gpt-5.6-sol", _messages(), builder,
        api_label="xxtf", tools=None, supports_tools=True, journal=None,
    ))


def test_stream_loop_degrades_on_sse_cache_error_and_retries():
    client = _FakeClient([
        [_error_event(PROD_ERROR_TEXT)],
        [_text_delta_event("hello final"), _completed_event()],
    ])
    builder = _FakeBuilder()

    content, usage, entries = _run_loop(client, builder)

    assert content == "hello final"
    assert len(client.responses.calls) == 2

    first, second = client.responses.calls
    # 第一次尝试：带显式断点（3 个文本 block 中取首+尾 2 个）
    assert _breakpoint_count(first) >= 1
    assert "prompt_cache_options" in first
    assert "prompt_cache_key" in first
    # 第二次尝试：断点被剥离，其余缓存字段保留
    assert _breakpoint_count(second) == 0
    assert "prompt_cache_options" in second
    assert "prompt_cache_key" in second

    caps = _responses_cache_capabilities(client, "gpt-5.6-sol")
    assert caps["prompt_cache_breakpoint"] is False
    assert caps["prompt_cache_options"] is True

    # 第二轮（同一进程内新循环）：首次请求即不再携带断点
    client2 = _FakeClient([
        [_text_delta_event("ok"), _completed_event()],
    ])
    content2, _, _ = _run_loop(client2, _FakeBuilder())
    assert content2 == "ok"
    assert len(client2.responses.calls) == 1
    assert _breakpoint_count(client2.responses.calls[0]) == 0


def test_stream_loop_degrades_on_sdk_exception_path():
    exc = Exception(PROD_ERROR_TEXT)  # 模拟网关直接回 4xx 的 SDK 异常
    client = _FakeClient([
        exc,
        [_text_delta_event("recovered"), _completed_event()],
    ])
    builder = _FakeBuilder()

    content, _, _ = _run_loop(client, builder)
    assert content == "recovered"
    assert len(client.responses.calls) == 2
    assert _breakpoint_count(client.responses.calls[1]) == 0


def test_stream_loop_raises_on_unrelated_error_without_retry():
    client = _FakeClient([
        [_error_event("boom: internal gateway failure")],
    ])
    builder = _FakeBuilder()

    with pytest.raises(RuntimeError) as excinfo:
        _run_loop(client, builder)
    assert "boom" in str(excinfo.value)
    assert len(client.responses.calls) == 1
    # 能力表未被污染
    caps = _responses_cache_capabilities(client, "gpt-5.6-sol")
    assert all(caps.values())


def test_stream_loop_partial_output_error_keeps_best_effort_content():
    # 已有部分输出（流中途收到 error 事件）：与旧行为一致，不视为零输出
    # 失败——尽力保留已产出内容正常收尾，不重试、不降级。
    client = _FakeClient([
        [_text_delta_event("partial"), _error_event(PROD_ERROR_TEXT)],
    ])
    builder = _FakeBuilder()
    content, _, _ = _run_loop(client, builder)
    assert content == "partial"
    assert len(client.responses.calls) == 1
    caps = _responses_cache_capabilities(client, "gpt-5.6-sol")
    assert caps["prompt_cache_breakpoint"] is True  # 未降级


# ---------------------------------------------------------------------
# 非流式路径（subagent）
# ---------------------------------------------------------------------
def test_non_stream_degrades_on_options_rejection():
    client = _FakeClient([
        Exception("prompt_cache_options is not supported on this model"),
        SimpleNamespace(output=[], usage=None),
    ])

    resp = asyncio.run(openai_responses_chat_completions_create(
        client, model="gpt-5.6-sol", messages=_messages(), max_tokens=128,
    ))

    assert resp is not None
    assert len(client.responses.calls) == 2
    first, second = client.responses.calls
    assert "prompt_cache_options" in first
    assert "prompt_cache_options" not in second
    assert "prompt_cache_key" in second  # 其它缓存字段保留

    caps = _responses_cache_capabilities(client, "gpt-5.6-sol")
    assert caps["prompt_cache_options"] is False
    assert caps["prompt_cache_breakpoint"] is True


def test_non_stream_raises_on_unrelated_error():
    client = _FakeClient([Exception("boom")])
    with pytest.raises(Exception, match="boom"):
        asyncio.run(openai_responses_chat_completions_create(
            client, model="gpt-5.6-sol", messages=_messages(), max_tokens=128,
        ))
    assert len(client.responses.calls) == 1
