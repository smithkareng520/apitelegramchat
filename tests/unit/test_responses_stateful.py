# =====================================================================
# tests/unit/test_responses_stateful.py — Responses API stateful 增量链
# =====================================================================
# 被测关键路径（Phase 1：stateful + fallback，见 ai/responses_state.py 与
# responses_bridge 模块头注释）：
#
#   1. 首轮 bootstrap：全量发送本地历史，不带 previous_response_id；
#   2. 第二轮（新 user 轮次）：只发送水位之后的增量 input，并携带
#      previous_response_id == 上一响应 id（已入 server 链的 assistant
#      响应绝不重发）；
#   3. 工具续链：function_call 轮结束后，工具结果以 function_call_output
#      增量发送（绝不重发历史）；
#   4. 模型隔离：state 键含 provider/endpoint/model，切模型自动 bootstrap；
#   5. server state 失效：previous_response_id 请求 4xx 时自动丢弃指针、
#      从本地历史 bootstrap 重建重试一次；
#   6. 本地历史是唯一业务真相：stateful 逻辑不改变 history 形状/节奏，
#      reasoning 永不以原生 item 形式写回历史。
#
# 测试策略：FakeAsyncOpenAI 记录每次 responses.create(**kwargs) 并回放
# 预设 SSE 事件序列；FakeBuilder 提供 DraftManager 的最小表面；
# run_tool_batch / chat_actions 在 responses_bridge 命名空间内打桩
# （与生产代码零改动）。state 注册表为进程内存，测试间显式清空。
# =====================================================================
import asyncio
import copy
from types import SimpleNamespace

import pytest

import config
import state as state_module
from ai import responses_bridge
from ai.responses_state import (
    _responses_sessions,
    count_input_visible_messages,
    fingerprint_synced_prefix,
    resolve_synced_prefix,
)
from core.messages import Message

# ---------------------------------------------------------------------
# 测试夹具模型注册（仅测试进程生效，与 conftest 注册媒体模型同一模式）
# ---------------------------------------------------------------------
_TEST_MODEL_A = "test-resp-stateful-a"
_TEST_MODEL_B = "test-resp-stateful-b"
_TEST_MODEL_BG = "test-resp-background"
_TEST_MODEL_PROMPT = "test-resp-prompt"


def _register_models() -> None:
    for model_id in (_TEST_MODEL_A, _TEST_MODEL_B):
        if model_id not in config.SUPPORTED_MODELS:
            config.SUPPORTED_MODELS[model_id] = config.make_model_config(
                model_id=model_id,
                provider="lfree",
                protocol="openai_responses",
                supports_tools=True,
                reasoning_effort="high",
                responses_stateful=True,
            )
    if _TEST_MODEL_BG not in config.SUPPORTED_MODELS:
        # Phase 3 Background Mode：stateful + background 同时开启，
        # 验证两条长任务能力可叠加（增量链 + 轮询）。
        config.SUPPORTED_MODELS[_TEST_MODEL_BG] = config.make_model_config(
            model_id=_TEST_MODEL_BG,
            provider="lfree",
            protocol="openai_responses",
            supports_tools=True,
            responses_stateful=True,
            responses_background=True,
        )
    if _TEST_MODEL_PROMPT not in config.SUPPORTED_MODELS:
        # Phase 3 Prompt Templates：模板对象随请求下发且不再发 instructions。
        config.SUPPORTED_MODELS[_TEST_MODEL_PROMPT] = config.make_model_config(
            model_id=_TEST_MODEL_PROMPT,
            provider="lfree",
            protocol="openai_responses",
            supports_tools=True,
            responses_prompt={
                "id": "pmpt_test_abc",
                "version": "2",
                "variables": {"username": "alice"},
            },
        )


_register_models()


def _lfree_endpoint(model_id: str) -> str:
    return config.get_effective_endpoint(config.SUPPORTED_MODELS[model_id]).endpoint


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """测试间清空 Responses state 注册表 / 会话上下文 / 打桩网络动作。"""
    _responses_sessions.clear()
    state_module.user_contexts.clear()
    state_module.user_models.clear()

    async def _noop_action(chat_id, action):
        return None

    monkeypatch.setattr(responses_bridge, "start_chat_action", _noop_action)
    monkeypatch.setattr(responses_bridge, "stop_chat_action", _noop_action)
    yield
    _responses_sessions.clear()
    state_module.user_contexts.clear()
    state_module.user_models.clear()


# ---------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------
class _FakeUsage:
    def __init__(self) -> None:
        self._d = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}

    def model_dump(self) -> dict:
        return dict(self._d)


class _FakeResponse:
    def __init__(self, response_id: str) -> None:
        self.id = response_id
        self.usage = _FakeUsage()


class _AttrItem:
    """getattr 风格的 output item（function_call 等）。"""

    def __init__(self, d: dict) -> None:
        for key, value in d.items():
            setattr(self, key, value)


def _text_round_events(response_id: str, text: str = "ok") -> list:
    """一轮纯文本响应的最小事件序列。"""
    return [
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.completed", "response": _FakeResponse(response_id)},
    ]


def _function_call_events(response_id: str, call_id: str, item_id: str,
                          name: str, arguments: str) -> list:
    """一轮 function_call 响应的最小事件序列（.added/.done/参数兜底齐全）。"""
    fc = {"id": item_id, "call_id": call_id, "name": name,
          "arguments": arguments, "type": "function_call"}
    return [
        {"type": "response.output_item.added", "item": _AttrItem(fc)},
        {"type": "response.function_call_arguments.delta",
         "item_id": item_id, "delta": ""},
        {"type": "response.function_call_arguments.done",
         "item_id": item_id, "arguments": arguments},
        {"type": "response.output_item.done", "item": _AttrItem(fc)},
        {"type": "response.completed", "response": _FakeResponse(response_id)},
    ]


class _Event:
    """把事件 dict 转成循环按 event.type 分发的 getattr 风格对象。"""

    def __init__(self, raw: dict) -> None:
        self._raw = raw
        for key, value in raw.items():
            if key not in ("type", "response"):
                setattr(self, key, value)

    @property
    def type(self) -> str:
        return self._raw["type"]

    @property
    def response(self):
        return self._raw.get("response")


class _FakeStream:
    def __init__(self, events: list) -> None:
        self._events = events

    def __aiter__(self):
        self._iter = iter(self._events)
        return self

    async def __anext__(self):
        try:
            raw = next(self._iter)
        except StopIteration:
            raise StopAsyncIteration
        return _Event(raw)


class FakeAsyncOpenAI:
    """记录 responses.create(**kwargs) 并按 kwargs 回放事件的假客户端。

    events_provider: 接收本次 kwargs（深拷贝），返回该次调用的事件列表。
    fail_on_previous_response: 模拟网关拒绝 previous_response_id——真实
    SDK 在 create 阶段抛 4xx/404，因此这里在记录后直接 raise。
    """

    def __init__(self, events_provider, fail_on_previous_response: bool = False) -> None:
        self.calls: list[dict] = []
        self._events_provider = events_provider
        self.fail_on_previous_response = fail_on_previous_response
        # 模拟 SDK 形状：client.responses.create(**kwargs)
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self.fail_on_previous_response and kwargs.get("previous_response_id"):
            raise RuntimeError(
                f"404 No response found with id '{kwargs.get('previous_response_id')}'")
        return _FakeStream(list(self._events_provider(copy.deepcopy(kwargs))))


class FakeBuilder:
    """DraftManager 最小替身：只实现 responses 循环实际触达的方法。"""

    def __init__(self, chat_id: int = 860001) -> None:
        self.chat_id = chat_id
        self._tool_groups: list = []
        self.draft_text: list[str] = []
        self.thinking_status: str = ""

    def append_stream_delta(self, text: str) -> None:
        self.draft_text.append(text)

    def begin_stream_text(self) -> None:
        pass

    def begin_stream_reasoning(self) -> None:
        pass

    def end_stream(self) -> None:
        pass

    def end_stream_text(self) -> str:
        return ""

    def on_stream_block_closed(self, kind: str) -> None:
        pass

    def finalize_reasoning_block(self) -> None:
        pass

    def on_round_boundary(self) -> None:
        pass

    def on_tool_batch_end(self) -> None:
        pass

    def request_flush(self, force: bool = True) -> None:
        pass

    def add_text(self, text: str) -> None:
        self.draft_text.append(text)

    def add_tool_item(self, call_id, name, summary, **kwargs) -> None:
        pass

    def update_tool_args(self, call_id, args) -> None:
        pass

    def set_thinking_status(self, text: str, *, force: bool = True) -> bool:
        self.thinking_status = text
        return True

    async def finalize_turn(self) -> bool:
        return True


# ---------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------
def _base_messages() -> list:
    return [Message.system("sys prompt"), Message.user_text("hi")]


async def _run_loop(client, model_id: str, messages: list, builder=None, **kwargs):
    builder = builder or FakeBuilder()
    result = await responses_bridge._agentic_loop_openai_responses(
        client, model_id, messages, builder,
        api_label="lfree", tools=[], supports_tools=True, **kwargs,
    )
    return result, builder


async def _get_session(chat_id: int, model_id: str):
    from ai.responses_state import get_responses_session
    return await get_responses_session(chat_id, "lfree", _lfree_endpoint(model_id), model_id)


def _stub_run_tool_batch(monkeypatch):
    """把 run_tool_batch 替换为：给每个 tool_call 追加固定结果并继续。"""

    async def fake_run_tool_batch(builder, tool_calls_list, loop_messages,
                                  new_history_entries, tool_call_count_ref,
                                  api_label, tools, error_streak=None):
        for tc in tool_calls_list:
            result = Message.tool_result(tc["id"], tc["function"]["name"], "tool ok")
            loop_messages.append(result)
            new_history_entries.append(result)
        return "continue"

    monkeypatch.setattr(responses_bridge, "run_tool_batch", fake_run_tool_batch)


# ---------------------------------------------------------------------
# responses_state 纯逻辑：水位 / 指纹
# ---------------------------------------------------------------------
def test_count_input_visible_messages_excludes_system():
    msgs = [Message.system("a"), Message.user_text("u"),
            Message.system("b"), Message.assistant_text("x")]
    assert count_input_visible_messages(msgs) == 2


def test_fingerprint_stable_across_message_dict_roundtrip_and_strip():
    """指纹跨「Message 对象 <-> dict 重建 + assistant 文本 strip」稳定。"""
    msg = Message.assistant_with_tool_calls(
        "trailing text \n", [{"id": "call_1", "type": "function",
                              "function": {"name": "t", "arguments": "{\"x\": 1}"}}],
        reasoning="think")
    rebuilt = Message.from_openai_dict(msg.to_openai_dict())
    # 入历史时 assistant 文本会被 strip（update_conversation_and_ledger）
    rebuilt.set_text(rebuilt.text().strip())
    assert fingerprint_synced_prefix([msg], 1) == fingerprint_synced_prefix([rebuilt], 1)


def test_fingerprint_normalizes_presigned_url_query():
    a = Message.user_text("see https://r2.example.com/file.bin?X-Amz-Signature=aaa&x=1")
    b = Message.user_text("see https://r2.example.com/file.bin?X-Amz-Signature=bbb&x=2")
    assert fingerprint_synced_prefix([a], 1) == fingerprint_synced_prefix([b], 1)


def test_resolve_synced_prefix_detects_rewrite_and_returns_none():
    msgs = [Message.system("s"), Message.user_text("u1"), Message.assistant_text("a1")]
    fp = fingerprint_synced_prefix(msgs, 2)
    # 前缀被改写（压缩/裁剪场景）：对不齐 → bootstrap
    msgs[1] = Message.user_text("u1-rewritten")
    assert resolve_synced_prefix(msgs, 2, fp) is None
    # 消息变少（清空/截断后）：对不齐
    assert resolve_synced_prefix([Message.system("s")], 2, fp) is None
    # 正常追加：返回增量起点（第 3 条非 system 消息的下标）
    msgs2 = [Message.system("s"), Message.user_text("u1"), Message.assistant_text("a1"),
             Message.user_text("u2")]
    assert resolve_synced_prefix(msgs2, 2, fp) == 3


# ---------------------------------------------------------------------
# 循环级：stateful 增量链
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_responses_state_first_turn_bootstraps_full_history():
    """首轮：全量发送本地历史，不带 previous_response_id；指针落库。"""
    client = FakeAsyncOpenAI(lambda kw: _text_round_events("resp_t1"))
    messages = _base_messages()
    (content, usage, entries), builder = await _run_loop(client, _TEST_MODEL_A, messages)

    assert content == "ok"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert "previous_response_id" not in call
    # 全量 = user 消息 item；system 消息进 instructions
    assert call["input"] == [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi"}]},
    ]
    assert call["instructions"] == "sys prompt"

    session = await _get_session(builder.chat_id, _TEST_MODEL_A)
    assert session is not None and session.response_id == "resp_t1"
    # 水位含本轮 assistant 响应（server 链已包含它）
    assert session.synced_message_count == 2  # user + assistant
    assert session.status == "completed"
    assert session.created_at > 0 and session.last_used_at >= session.created_at


@pytest.mark.asyncio
async def test_responses_state_second_turn_only_sends_delta():
    """第二轮：只发送水位之后的新 user 消息（已入链的 assistant 不重发）。"""
    seq = {"n": 0}

    def provider(kw):
        seq["n"] += 1
        return _text_round_events(f"resp_t{seq['n']}")

    client = FakeAsyncOpenAI(provider)
    messages = _base_messages()
    await _run_loop(client, _TEST_MODEL_A, messages)

    # 第二轮：本地历史追加第一轮的 assistant 响应 + 新 user 消息
    messages.extend([Message.assistant_text("ok"), Message.user_text("again")])
    await _run_loop(client, _TEST_MODEL_A, messages)

    assert len(client.calls) == 2
    second = client.calls[1]
    assert second["previous_response_id"] == "resp_t1"
    # 增量 = 仅新 user 消息；第一轮的 assistant 响应已在 server 链里
    assert second["input"] == [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "again"}]},
    ]
    # instructions 每轮全量重发（不进入 server 链）
    assert second["instructions"] == "sys prompt"


@pytest.mark.asyncio
async def test_responses_tool_output_uses_previous_response_id(monkeypatch):
    """工具续链：function_call_output 增量发送，绝不重发历史。"""
    def provider(kw):
        if "previous_response_id" in kw:
            return _text_round_events("resp_tool_final", "done")
        return _function_call_events("resp_tool_1", "call_1", "fc_1",
                                     "do_thing", '{"x": 1}')

    client = FakeAsyncOpenAI(provider)
    _stub_run_tool_batch(monkeypatch)

    (content, usage, entries), builder = await _run_loop(
        client, _TEST_MODEL_A, _base_messages())

    assert content == "done"
    assert len(client.calls) == 2
    first, second = client.calls
    assert "previous_response_id" not in first
    assert second["previous_response_id"] == "resp_tool_1"
    # 增量 = 仅 function_call_output；function_call item 已在 server 链里
    assert second["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "tool ok"},
    ]

    session = await _get_session(builder.chat_id, _TEST_MODEL_A)
    assert session.response_id == "resp_tool_final"
    assert session.synced_message_count == 4
    # user + assistant(call) + tool_out + assistant(final)


@pytest.mark.asyncio
async def test_responses_state_isolated_by_model():
    """同 chat 两个模型各自持有独立指针，互不串链。"""
    seq = {"n": 0}

    def provider(kw):
        seq["n"] += 1
        return _text_round_events(f"resp_{seq['n']}")

    client = FakeAsyncOpenAI(provider)
    builder = FakeBuilder(chat_id=860002)
    messages = _base_messages()
    await _run_loop(client, _TEST_MODEL_A, messages, builder=builder)
    await _run_loop(client, _TEST_MODEL_B, messages, builder=builder)

    s_a = await _get_session(builder.chat_id, _TEST_MODEL_A)
    s_b = await _get_session(builder.chat_id, _TEST_MODEL_B)
    assert s_a.response_id == "resp_1"
    assert s_b.response_id == "resp_2"

    # 模型 B 的第二轮只续 B 自己的链，绝不引用模型 A 的响应 id
    messages.extend([Message.assistant_text("ok"), Message.user_text("again")])
    await _run_loop(client, _TEST_MODEL_B, messages, builder=builder)
    assert client.calls[-1]["previous_response_id"] == "resp_2"
    assert client.calls[-1]["previous_response_id"] != s_a.response_id


@pytest.mark.asyncio
async def test_model_switch_bootstraps_from_local_history():
    """切到没有指针的模型：bootstrap 全量发送，不携带任何旧模型指针。"""
    client = FakeAsyncOpenAI(lambda kw: _text_round_events("resp_m1"))
    builder = FakeBuilder(chat_id=860003)
    messages = _base_messages()
    await _run_loop(client, _TEST_MODEL_A, messages, builder=builder)

    # 切到模型 B（同 provider/endpoint，不同 model）：无 B 的指针 → bootstrap
    messages.extend([Message.assistant_text("ok"), Message.user_text("again")])
    await _run_loop(client, _TEST_MODEL_B, messages, builder=builder)

    second = client.calls[1]
    assert "previous_response_id" not in second
    assert [item["role"] for item in second["input"]] == ["user", "assistant", "user"]


@pytest.mark.asyncio
async def test_invalid_previous_response_id_rebuilds_state():
    """server state 失效：4xx 后丢弃指针、bootstrap 重试一次、指针重建。"""
    seq = {"n": 0}

    def provider(kw):
        seq["n"] += 1
        return _text_round_events(f"resp_fb_{seq['n']}")

    client = FakeAsyncOpenAI(provider, fail_on_previous_response=True)
    builder = FakeBuilder(chat_id=860004)
    messages = _base_messages()
    await _run_loop(client, _TEST_MODEL_A, messages, builder=builder)

    # 篡改指针为失效 id（模拟上游 state 过期 / 网关重启丢 state）
    session = await _get_session(builder.chat_id, _TEST_MODEL_A)
    session.response_id = "resp_expired"

    messages.extend([Message.assistant_text("ok"), Message.user_text("again")])
    (content, usage, entries), builder = await _run_loop(
        client, _TEST_MODEL_A, messages, builder=builder)

    assert content == "ok"
    assert len(client.calls) == 3
    first, second, fallback = client.calls
    # 第一轮 bootstrap：无指针
    assert "previous_response_id" not in first
    # 第二轮增量尝试携带失效指针
    assert second["previous_response_id"] == "resp_expired"
    # fallback：全量 bootstrap、无指针（user + assistant + 新 user）
    assert "previous_response_id" not in fallback
    assert [item["role"] for item in fallback["input"]] == ["user", "assistant", "user"]
    assert fallback["instructions"] == "sys prompt"

    # 指针以 fallback 响应重建，水位与本地历史一致
    session = await _get_session(builder.chat_id, _TEST_MODEL_A)
    assert session.response_id == "resp_fb_2"
    # u1 + a1(首轮) + u2 + a2(fallback 轮)
    assert session.synced_message_count == 4


@pytest.mark.asyncio
async def test_stateless_model_keeps_full_history_behavior():
    """responses_stateful 未开启的模型：行为与旧版完全一致（每轮全量）。"""
    config.SUPPORTED_MODELS["test-resp-stateless"] = config.make_model_config(
        model_id="test-resp-stateless", provider="lfree",
        protocol="openai_responses", supports_tools=True,
    )
    client = FakeAsyncOpenAI(lambda kw: _text_round_events("resp_s1"))
    builder = FakeBuilder(chat_id=860005)
    messages = _base_messages()
    await _run_loop(client, "test-resp-stateless", messages, builder=builder)
    messages.extend([Message.assistant_text("ok"), Message.user_text("again")])
    await _run_loop(client, "test-resp-stateless", messages, builder=builder)

    assert "previous_response_id" not in client.calls[1]
    # 第二轮仍然全量发送（含首轮历史）
    assert [item["role"] for item in client.calls[1]["input"]] == ["user", "assistant", "user"]
    assert (await _get_session(builder.chat_id, "test-resp-stateless")) is None


@pytest.mark.asyncio
async def test_reasoning_not_written_as_native_item(monkeypatch):
    """reasoning 只进内部 ReasoningBlock；历史里绝无 Responses 原生 item。"""
    def provider(kw):
        if "previous_response_id" in kw:
            return [
                {"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
                {"type": "response.output_text.delta", "delta": "final"},
                {"type": "response.completed", "response": _FakeResponse("resp_final")},
            ]
        return _function_call_events("resp_r1", "call_1", "fc_1",
                                     "do_thing", '{"x": 1}')

    client = FakeAsyncOpenAI(provider)
    _stub_run_tool_batch(monkeypatch)

    messages = _base_messages()
    (content, usage, entries), builder = await _run_loop(
        client, _TEST_MODEL_A, messages)

    assert content == "final"
    # 历史条目全部是内部 Message（role/content 形状），绝无 dict 形状的
    # 原生 item（function_call / function_call_output / reasoning item）
    for entry in entries:
        assert isinstance(entry, Message), f"history 被写入了非 Message 条目: {entry!r}"
        dumped = entry.to_openai_dict()
        assert dumped.get("role") in ("user", "assistant", "tool", "system")
        assert dumped.get("type") != "function_call"
        assert dumped.get("type") != "function_call_output"
        assert dumped.get("type") != "reasoning"
    # reasoning 文本保存在内部 ReasoningBlock，可跨协议展示
    assert any(m.reasoning() == "thinking" for m in entries)
    # blocks 全部是结构化 Block 对象（非原生 item dict）
    assert not any(isinstance(b, dict) for m in entries for b in m.blocks)


@pytest.mark.asyncio
async def test_clear_history_drops_responses_state():
    """safe_clear_history 轮换会话纪元并显式清扫 Responses 指针。"""
    client = FakeAsyncOpenAI(lambda kw: _text_round_events("resp_c1"))
    builder = FakeBuilder(chat_id=860006)
    await _run_loop(client, _TEST_MODEL_A, _base_messages(), builder=builder)
    session = await _get_session(builder.chat_id, _TEST_MODEL_A)
    assert session is not None

    from ai.responses_state import drop_responses_sessions_for_chat, get_responses_session
    dropped = drop_responses_sessions_for_chat(builder.chat_id)
    assert dropped >= 1
    assert await get_responses_session(
        builder.chat_id, "lfree", _lfree_endpoint(_TEST_MODEL_A),
        _TEST_MODEL_A) is None

    # state.safe_clear_history 的联动路径（延迟导入）不抛错
    await state_module.safe_clear_history(builder.chat_id)


# =====================================================================
# Phase 3：Background Mode / Prompt Templates / Compaction 联动
# =====================================================================
class _FakeBgResponse:
    """background create/retrieve 返回的 Response 最小替身。"""

    def __init__(self, id: str, status: str, output: list | None = None,
                 usage=None, error=None, incomplete_reason: str | None = None) -> None:
        self.id = id
        self.status = status
        self.output = output or []
        self.usage = usage
        self.error = error
        self.incomplete_details = (
            SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None)


def _bg_message_item(text: str):
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _bg_function_call_item(call_id: str = "call_1", item_id: str = "fc_1",
                           name: str = "do_thing", arguments: str = '{"x": 1}'):
    return SimpleNamespace(type="function_call", id=item_id, call_id=call_id,
                           name=name, arguments=arguments)


class FakeBackgroundOpenAI:
    """background 模式假客户端：create 立即返回 queued，retrieve 按脚本回放。

    retrieve_sequence 逐项消费：
      - "in_progress" / "queued" 字符串 → 对应状态的中间响应；
      - _FakeBgResponse 实例 → 原样返回（终态）；
      - Exception 实例 → 抛出（瞬时失败 / CancelledError 脚本）。
    """

    def __init__(self, retrieve_sequence: list) -> None:
        self.calls: list[dict] = []
        self.retrieves: list[str] = []
        self.cancelled: list[str] = []
        self._retrieve_sequence = list(retrieve_sequence)
        self.responses = SimpleNamespace(
            create=self._create, retrieve=self._retrieve, cancel=self._cancel)

    async def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return _FakeBgResponse(id=f"resp_bg_{len(self.calls)}", status="queued")

    async def _retrieve(self, response_id, **kwargs):
        self.retrieves.append(response_id)
        nxt = self._retrieve_sequence.pop(0)
        if isinstance(nxt, BaseException):
            # CancelledError 在 Py3.8+ 继承 BaseException，不能用 Exception 判断
            raise nxt
        if isinstance(nxt, str):
            return _FakeBgResponse(id=response_id, status=nxt)
        return nxt

    async def _cancel(self, response_id, **kwargs):
        self.cancelled.append(response_id)


# ---------------------------------------------------------------------
# Background Mode
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_background_round_completes_and_chains(monkeypatch):
    """background 轮询至终态：文本/工具卡片/增量续链全部走通。"""
    # 第 1 轮：轮询一次 in_progress 后终态，输出 function_call；
    # 第 2 轮（工具续链，仍 background + previous_response_id）：输出纯文本。
    retrieve_sequence = [
        "in_progress",
        _FakeBgResponse("resp_bg_1", "completed", output=[_bg_function_call_item()],
                        usage=_FakeUsage()),
        "in_progress",
        _FakeBgResponse("resp_bg_2", "completed", output=[_bg_message_item("done")],
                        usage=_FakeUsage()),
    ]
    client = FakeBackgroundOpenAI(retrieve_sequence)
    _stub_run_tool_batch(monkeypatch)

    (content, usage, entries), builder = await _run_loop(
        client, _TEST_MODEL_BG, _base_messages())

    assert content == "done"
    assert len(client.calls) == 2
    first, second = client.calls
    # background 提交形态：background=True、无 stream 键
    assert first["background"] is True
    assert "stream" not in first
    assert "previous_response_id" not in first
    # 轮询：每轮 create(queued) -> retrieve(in_progress) -> retrieve(completed)，
    # 两轮共 4 次 retrieve
    assert client.retrieves == ["resp_bg_1", "resp_bg_1", "resp_bg_2", "resp_bg_2"]
    # 第二轮：background + 增量链叠加（function_call_output 增量）
    assert second["background"] is True
    assert second["previous_response_id"] == "resp_bg_1"
    assert second["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "tool ok"},
    ]
    # 水位照常推进（background 轮与流式轮共享同一状态管线）
    session = await _get_session(builder.chat_id, _TEST_MODEL_BG)
    assert session.response_id == "resp_bg_2"
    assert session.synced_message_count == 4
    # 轮询心跳推送过 thinking 状态
    assert "Background task" in builder.thinking_status


@pytest.mark.asyncio
async def test_background_timeout_cancels_task(monkeypatch):
    """轮询超时：cancel 服务端任务并抛 RuntimeError。"""
    retrieve_sequence = ["in_progress"] * 1000  # 永不终态
    client = FakeBackgroundOpenAI(retrieve_sequence)
    monkeypatch.setattr(responses_bridge, "_RESPONSES_BG_POLL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(responses_bridge, "_RESPONSES_BG_POLL_INTERVAL_SECONDS", 0.01)

    with pytest.raises(RuntimeError, match="超时"):
        await _run_loop(client, _TEST_MODEL_BG, _base_messages())

    assert client.cancelled == ["resp_bg_1"]


@pytest.mark.asyncio
async def test_background_interrupt_cancels_and_propagates():
    """被打断（CancelledError）：尽力 cancel 后原样传播。"""
    client = FakeBackgroundOpenAI([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await _run_loop(client, _TEST_MODEL_BG, _base_messages())

    assert client.cancelled == ["resp_bg_1"]


@pytest.mark.asyncio
async def test_background_transient_poll_failures_tolerated(monkeypatch):
    """瞬时轮询失败（< 连续上限）自动重试，不中断长任务。"""
    retrieve_sequence = [
        RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom"),
        _FakeBgResponse("resp_bg_1", "completed", output=[_bg_message_item("ok")],
                        usage=_FakeUsage()),
    ]
    client = FakeBackgroundOpenAI(retrieve_sequence)
    monkeypatch.setattr(responses_bridge, "_RESPONSES_BG_POLL_INTERVAL_SECONDS", 0.01)

    (content, usage, entries), builder = await _run_loop(
        client, _TEST_MODEL_BG, _base_messages())

    assert content == "ok"
    assert len(client.retrieves) == 4
    assert client.cancelled == []


# ---------------------------------------------------------------------
# Prompt Templates
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prompt_template_sent_without_instructions():
    """responses_prompt 配置后：请求携带 prompt 且不再发送 instructions。"""
    client = FakeAsyncOpenAI(lambda kw: _text_round_events("resp_p1"))
    (content, usage, entries), builder = await _run_loop(
        client, _TEST_MODEL_PROMPT, _base_messages())

    assert content == "ok"
    call = client.calls[0]
    assert call["prompt"] == {
        "id": "pmpt_test_abc",
        "version": "2",
        "variables": {"username": "alice"},
    }
    assert "instructions" not in call


def test_responses_prompt_config_validation():
    """responses_prompt 必须是带 id 的 dict；拼错在配置期报错。"""
    with pytest.raises(ValueError, match="responses_prompt"):
        config.make_model_config(
            model_id="test-resp-bad-prompt", provider="lfree",
            protocol="openai_responses", responses_prompt={"version": "1"},
        )


# ---------------------------------------------------------------------
# Compaction 联动
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_compaction_resets_responses_state():
    """本地历史压缩事件 → 该模型组合的 server state 指针显式丢弃。"""
    from ai.responses_state import reset_responses_session_for_model

    client = FakeAsyncOpenAI(lambda kw: _text_round_events("resp_c1"))
    builder = FakeBuilder(chat_id=860007)
    await _run_loop(client, _TEST_MODEL_A, _base_messages(), builder=builder)
    await _run_loop(client, _TEST_MODEL_B, _base_messages(), builder=builder)
    assert (await _get_session(builder.chat_id, _TEST_MODEL_A)) is not None
    assert (await _get_session(builder.chat_id, _TEST_MODEL_B)) is not None

    # 压缩事件：只 reset 当前（被压缩的）模型组合
    reset_a = await reset_responses_session_for_model(
        builder.chat_id, config.SUPPORTED_MODELS[_TEST_MODEL_A])
    assert reset_a is True
    assert (await _get_session(builder.chat_id, _TEST_MODEL_A)) is None
    assert (await _get_session(builder.chat_id, _TEST_MODEL_B)) is not None

    # 再次 reset（已无指针）：返回 False（幂等）
    assert await reset_responses_session_for_model(
        builder.chat_id, config.SUPPORTED_MODELS[_TEST_MODEL_A]) is False

    # 非 stateful 模型：no-op
    if "test-resp-stateless" not in config.SUPPORTED_MODELS:
        config.SUPPORTED_MODELS["test-resp-stateless"] = config.make_model_config(
            model_id="test-resp-stateless", provider="lfree",
            protocol="openai_responses", supports_tools=True,
        )
    assert await reset_responses_session_for_model(
        builder.chat_id, config.SUPPORTED_MODELS["test-resp-stateless"]) is False


def test_pre_flight_wires_responses_state_reset():
    """pre_flight_context_check 的压缩事件路径已接线 state reset（防回归守卫）。"""
    import inspect
    from app_turns import pre_flight_context_check
    src = inspect.getsource(pre_flight_context_check)
    assert "reset_responses_session_for_model" in src
