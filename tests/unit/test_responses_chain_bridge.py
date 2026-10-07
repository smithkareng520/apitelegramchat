# tests/unit/test_responses_chain_bridge.py
'''Responses bridge 链路回归：官方 server-managed state（previous_response_id）。'''

from __future__ import annotations


import pytest

from core.messages import Message


# ---------------------------------------------------------------------------
# 测试用模型注册（config 驱动架构：厂商 + 模型）
# ---------------------------------------------------------------------------
from config import PROVIDERS, SUPPORTED_MODELS, ModelConfig, ProviderConfig

_TEST_PROVIDER = "unit-test-responses-provider"
_TEST_MODEL = "unit-test-responses-model"

if _TEST_PROVIDER not in PROVIDERS:
    PROVIDERS[_TEST_PROVIDER] = ProviderConfig(
        name=_TEST_PROVIDER,
        endpoint="https://unit-test.example/v1",
        api_key_env="UNIT_TESTResponsesKey",
        protocol="openai_responses",
    )
if _TEST_MODEL not in SUPPORTED_MODELS:
    SUPPORTED_MODELS[_TEST_MODEL] = ModelConfig(
        model_id=_TEST_MODEL,
        provider=_TEST_PROVIDER,
        supports_tools=True,
        max_output_tokens=512,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeResponsesClient:
    """脚本化 AsyncOpenAI.responses 替身：记录 kwargs、回放事件流。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.responses = self  # client.responses.create(**kwargs)

    async def create(self, **kwargs):
        import json as _json

        self.calls.append(_json.loads(_json.dumps(kwargs, default=str)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step

        async def _stream():
            for event in step:
                yield event

        return _stream()


class _FakeBuilder:
    """DraftManager 的最小替身（只需 responses bridge 实际触达的接口）。"""

    def __init__(self, chat_id=42):
        self.chat_id = chat_id
        self._tool_groups: list = []
        self.texts: list[str] = []
        self.tool_items: list[tuple] = []

    async def start_chat_action(self, *a, **k):
        return None

    def append_stream_delta(self, text):
        self.texts.append(text)

    def add_tool_item(self, call_id, name, summary, **kwargs):
        self.tool_items.append((call_id, name))

    def update_tool_args(self, *a, **k):
        return None

    def request_flush(self, force=False):
        return None

    def end_stream(self):
        return None

    def begin_stream_text(self):
        return None

    def end_stream_text(self):
        return None

    def begin_stream_reasoning(self):
        return None

    def on_stream_block_closed(self, *a):
        return None

    def on_round_boundary(self):
        return None

    def on_tool_batch_end(self):
        return None

    def finalize_reasoning_block(self):
        return None

    def add_text(self, text):
        self.texts.append(text)

    async def finalize_turn(self):
        return True


def _completed(response_id: str, output: list, text_deltas: list[str] | None = None):
    """构造一条完整事件流：可选文本 delta + response.completed 终态。"""
    events: list[dict] = [
        {"type": "response.output_text.delta", "delta": d} for d in (text_deltas or [])
    ]
    events.append({
        "type": "response.completed",
        "response": {"id": response_id, "status": "completed", "output": output},
    })
    return events


def _message_item(text: str) -> dict:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _function_call_item(call_id: str, name: str, arguments: str) -> dict:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


@pytest.fixture()
def _bridge_env(monkeypatch):
    """隔离外部副作用：技能目录刷新 / chat action / 缓存日志。"""
    import ai.responses_bridge as bridge
    import chat_actions
    import skills_runtime

    monkeypatch.setattr(skills_runtime, "refresh_skill_catalog", lambda *a, **k: None)
    monkeypatch.setattr(chat_actions, "start_chat_action", async_noop)
    monkeypatch.setattr(chat_actions, "stop_chat_action", async_noop)
    monkeypatch.setattr(bridge, "_log_cache_usage", lambda *a, **k: None)
    return bridge


async def async_noop(*a, **k):
    return None


def _run_tool_batch_factory(executed: list):
    """替身工具执行器：追加与 function_call 配对的 tool 结果消息。"""

    async def _run_tool_batch(builder, tool_calls_list, loop_messages,
                              new_history_entries, tool_call_count_ref,
                              api_label, tools, *args, **kwargs):
        for call in tool_calls_list:
            executed.append(call)
            result = Message.tool_result(call["id"], call["function"]["name"], "tool-ok")
            loop_messages.append(result)
            new_history_entries.append(result)
        return "continue"

    return _run_tool_batch


def _fresh_state(chat_id: int):
    import responses_state as rs
    rs._response_states.pop(chat_id, None)
    rs._tool_chain_unsupported.clear()
    return rs, rs.get_response_state_sync(chat_id)


# ---------------------------------------------------------------------------
# 1. 普通多轮：bootstrap → commit → 只发新增 user item
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multi_turn_chain_sends_only_new_user_item(_bridge_env):
    bridge = _bridge_env
    rs, st = _fresh_state(424001)

    # ---- 回合 1：bootstrap（无链头），全量历史作为 input ----
    turn1 = st.begin_turn("USER")
    client = _FakeResponsesClient([
        _completed("resp_1", [_message_item("hello")], text_deltas=["hel", "lo"]),
    ])
    builder = _FakeBuilder(chat_id=424001)
    messages1 = [
        Message.system("sys prompt"),
        Message.user_text("hi"),
    ]
    content, usage, new_entries = await bridge._agentic_loop_openai_responses(
        client, _TEST_MODEL, messages1, builder,
        api_label="unit-test", tools=[], journal=[],
        workspace_namespace=None, turn=turn1,
    )
    assert content == "hello"
    assert len(client.calls) == 1
    first = client.calls[0]
    assert "previous_response_id" not in first  # bootstrap 不带链头
    assert first["input"] == [{
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }]
    assert first["instructions"] == "sys prompt"
    # response 成功返回 → 链头原子推进
    assert st.chain.response_id == "resp_1"
    assert st.chain.model == _TEST_MODEL

    # ---- 回合 2：链式续接，input 只带新增 user item ----
    turn2 = st.begin_turn("USER")
    client2 = _FakeResponsesClient([
        _completed("resp_2", [_message_item("answer 2")]),
    ])
    messages2 = [
        Message.system("sys prompt"),
        Message.user_text("hi"),
        Message.assistant_text("hello"),
        Message.user_text("second question"),
    ]
    content2, _, _ = await bridge._agentic_loop_openai_responses(
        client2, _TEST_MODEL, messages2, _FakeBuilder(chat_id=424001),
        api_label="unit-test", tools=[], journal=[],
        workspace_namespace=None, turn=turn2,
    )
    assert content2 == "answer 2"
    second = client2.calls[0]
    assert second["previous_response_id"] == "resp_1"
    assert second["input"] == [{
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "second question"}],
    }]
    assert st.chain.response_id == "resp_2"


# ---------------------------------------------------------------------------
# 2. 工具轮次：function_call（无文本）→ function_call_output 续链
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tool_round_continuation_with_previous_response_id(_bridge_env, monkeypatch):
    bridge = _bridge_env
    rs, st = _fresh_state(424002)
    executed: list = []
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory(executed))

    turn = st.begin_turn("USER")
    # 第一条 response 只有 function_call、没有任何文本——官方语义下这是
    # 完全合法的工具调用 response，绝不是"空 response"。
    client = _FakeResponsesClient([
        _completed("resp_call", [_function_call_item("call_1", "echo", '{"x": 1}')]),
        _completed("resp_final", [_message_item("all done")]),
    ])
    messages = [
        Message.system("sys prompt"),
        Message.user_text("run the tool"),
    ]
    content, _, new_entries = await bridge._agentic_loop_openai_responses(
        client, _TEST_MODEL, messages, _FakeBuilder(chat_id=424002),
        api_label="unit-test", tools=[], journal=[],
        workspace_namespace=None, turn=turn,
    )

    assert content == "all done"
    assert [c["id"] for c in executed] == ["call_1"]
    assert len(client.calls) == 2

    # 续链请求：previous_response_id = 产生 function_call 的 response.id；
    # input = 与 call_id 配对的 function_call_output（来自真实工具执行）。
    continuation = client.calls[1]
    assert continuation["previous_response_id"] == "resp_call"
    assert continuation["input"] == [{
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "tool-ok",
    }]
    assert continuation["instructions"] == "sys prompt"
    # 回合收尾：最终 response.id 提交为链头。
    assert st.chain.response_id == "resp_final"


@pytest.mark.asyncio
async def test_tool_rounds_across_multi_round_chain(_bridge_env, monkeypatch):
    """多轮工具链：每次续轮都沿最新 completed response 继续。"""
    bridge = _bridge_env
    rs, st = _fresh_state(424003)

    round_counter = {"n": 0}

    async def _two_round_executor(builder, tool_calls_list, loop_messages,
                                  new_history_entries, tool_call_count_ref,
                                  api_label, tools, *args, **kwargs):
        round_counter["n"] += 1
        for call in tool_calls_list:
            result = Message.tool_result(
                call["id"], call["function"]["name"], f"result-{round_counter['n']}")
            loop_messages.append(result)
            new_history_entries.append(result)
        return "continue"

    monkeypatch.setattr(bridge, "run_tool_batch", _two_round_executor)

    turn = st.begin_turn("USER")
    client = _FakeResponsesClient([
        _completed("resp_c1", [_function_call_item("call_1", "echo", "{}")]),
        _completed("resp_c2", [_function_call_item("call_2", "echo", "{}")]),
        _completed("resp_final", [_message_item("finished")]),
    ])
    await bridge._agentic_loop_openai_responses(
        client, _TEST_MODEL, [Message.system("s"), Message.user_text("go")],
        _FakeBuilder(chat_id=424003), api_label="unit-test", tools=[], journal=[],
        workspace_namespace=None, turn=turn,
    )
    assert [c.get("previous_response_id") for c in client.calls] == [
        None,           # bootstrap
        "resp_c1",      # 第 1 次工具续轮
        "resp_c2",      # 第 2 次工具续轮
    ]
    assert client.calls[1]["input"][0]["call_id"] == "call_1"
    assert client.calls[1]["input"][0]["output"] == "result-1"
    assert client.calls[2]["input"][0]["call_id"] == "call_2"
    assert client.calls[2]["input"][0]["output"] == "result-2"
    assert st.chain.response_id == "resp_final"


@pytest.mark.asyncio
async def test_tool_continuation_provider_empty_input_bootstraps_once(_bridge_env, monkeypatch):
    """兼容不正确处理 native function_call_output 的 Responses 网关。"""
    bridge = _bridge_env
    rs, st = _fresh_state(424010)
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory([]))

    turn = st.begin_turn("USER")

    class _GatewayEmptyInputError(Exception):
        status_code = 400

        def __init__(self):
            super().__init__("Error from provider (Console): `input` must be non-empty")

    client = _FakeResponsesClient([
        _completed("resp_call", [_function_call_item("call_1", "echo", '{"x": 1}')]),
        _GatewayEmptyInputError(),
        _completed("resp_boot", [_message_item("recovered")]),
    ])
    content, _, _ = await bridge._agentic_loop_openai_responses(
        client, _TEST_MODEL, [Message.system("s"), Message.user_text("go")],
        _FakeBuilder(chat_id=424010), api_label="unit-test", tools=[], journal=[],
        workspace_namespace=None, turn=turn,
    )

    assert content == "recovered"
    assert len(client.calls) == 3
    # First request is bootstrap, second is the native tool continuation.
    assert client.calls[1]["previous_response_id"] == "resp_call"
    assert client.calls[1]["input"] == [{
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "tool-ok",
    }]
    # Compatibility recovery must stop using previous_response_id and replay
    # canonical assistant + tool history, not send another empty continuation.
    retry = client.calls[2]
    assert "previous_response_id" not in retry
    assert any(
        item.get("type") == "function_call" and item.get("call_id") == "call_1"
        for item in retry["input"]
    )
    assert any(
        item.get("type") == "function_call_output" and item.get("call_id") == "call_1"
        for item in retry["input"]
    )
    assert st.chain.response_id == "resp_boot"



@pytest.mark.asyncio
async def test_gateway_empty_input_is_learned_once_then_tool_rounds_skip_chain(_bridge_env, monkeypatch):
    """网关拒绝 previous_response_id + 纯 function_call_output 是确定性行为。

    第一次被拒：bootstrap 重试一次并记住该 (端点, 模型)；之后同一回合/
    后续回合的工具续轮直接 bootstrap，不再白发一个必然 400 的请求。
    bootstrap 成功的 response 仍然提升为正常链头（普通用户回合继续链式）。
    """
    bridge = _bridge_env
    rs, st = _fresh_state(424011)
    executed: list = []
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory(executed))

    class _GatewayEmptyInputError(Exception):
        status_code = 400
        def __init__(self):
            super().__init__("Error from provider (Console): `input` must be non-empty")

    turn = st.begin_turn("USER")
    client = _FakeResponsesClient([
        _completed("resp_c1", [_function_call_item("call_1", "echo", "{}")]),
        _GatewayEmptyInputError(),                                              # 仅此一次被拒
        _completed("resp_c2", [_function_call_item("call_2", "echo", "{}")]),  # bootstrap 成功
        _completed("resp_final", [_message_item("finished")]),                 # 第二个工具续轮：直接 bootstrap
    ])

    content, _, _ = await bridge._agentic_loop_openai_responses(
        client, _TEST_MODEL, [Message.system("s"), Message.user_text("go")],
        _FakeBuilder(chat_id=424011), api_label="unit-test", tools=[],
        journal=[], workspace_namespace=None, turn=turn,
    )

    assert content == "finished"
    assert [c["id"] for c in executed] == ["call_1", "call_2"]
    assert len(client.calls) == 4  # 旧行为是 5：每个工具续轮都白发一次 400

    assert client.calls[1]["previous_response_id"] == "resp_c1"
    assert client.calls[1]["input"][0]["type"] == "function_call_output"

    for idx, call_ids in ((2, {"call_1"}), (3, {"call_1", "call_2"})):
        assert "previous_response_id" not in client.calls[idx]
        items = client.calls[idx]["input"]
        assert {i.get("call_id") for i in items if i.get("type") == "function_call"} >= call_ids
        assert {i.get("call_id") for i in items if i.get("type") == "function_call_output"} >= call_ids

    assert rs.is_tool_continuation_chain_unsupported(
        rs.derive_vendor_key(SUPPORTED_MODELS[_TEST_MODEL]), _TEST_MODEL
    )
    assert st.chain.response_id == "resp_final"


@pytest.mark.asyncio
async def test_truncated_stream_invalidates_chain_and_raises_protocol_error(_bridge_env):
    """流缺终态事件：必须作废链并抛协议错误（此前误传 3 个参数会先抛 TypeError）。"""
    from ai.errors import AIResponseProtocolError
    bridge = _bridge_env
    rs, st = _fresh_state(424012)
    st.commit_response(
        st.begin_turn("USER"),
        vendor_key=rs.derive_vendor_key(SUPPORTED_MODELS[_TEST_MODEL]),
        response_id="resp_prev", model=_TEST_MODEL,
    )
    turn = st.begin_turn("USER")
    client = _FakeResponsesClient([[{"type": "response.output_text.delta", "delta": "par"}]])

    with pytest.raises(AIResponseProtocolError):
        await bridge._agentic_loop_openai_responses(
            client, _TEST_MODEL, [Message.system("s"), Message.user_text("go")],
            _FakeBuilder(chat_id=424012), api_label="unit-test", tools=[],
            journal=[], workspace_namespace=None, turn=turn,
        )
    assert st.chain.response_id is None

# ---------------------------------------------------------------------------
# 3. 异常状态：失败不推进链 / stale-ID 只 bootstrap 一次 / 中断断链
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failed_create_keeps_chain_head(_bridge_env):
    bridge = _bridge_env
    rs, st = _fresh_state(424004)
    t0 = st.begin_turn("USER")
    st.commit_response(t0, vendor_key=rs.derive_vendor_key(
        SUPPORTED_MODELS[_TEST_MODEL]), response_id="resp_prev", model=_TEST_MODEL)

    turn = st.begin_turn("USER")
    client = _FakeResponsesClient([RuntimeError("gateway exploded")])
    with pytest.raises(RuntimeError):
        await bridge._agentic_loop_openai_responses(
            client, _TEST_MODEL, [Message.system("s"), Message.user_text("hi")],
            _FakeBuilder(chat_id=424004), api_label="unit-test", tools=[], journal=[],
            workspace_namespace=None, turn=turn,
        )
    # 请求失败 → 链头保持旧值，绝不推进到不存在的 response。
    assert st.chain.response_id == "resp_prev"


class _StaleResponseError(Exception):
    status_code = 404

    def __init__(self, response_id: str):
        super().__init__(
            f"Previous response with id '{response_id}' not found."
        )


@pytest.mark.asyncio
async def test_stale_previous_response_id_bootstraps_once(_bridge_env):
    bridge = _bridge_env
    rs, st = _fresh_state(424005)
    t0 = st.begin_turn("USER")
    st.commit_response(t0, vendor_key=rs.derive_vendor_key(
        SUPPORTED_MODELS[_TEST_MODEL]), response_id="resp_gone", model=_TEST_MODEL)

    turn = st.begin_turn("USER")
    client = _FakeResponsesClient([
        _StaleResponseError("resp_gone"),
        _completed("resp_boot", [_message_item("bootstrapped")]),
    ])
    content, _, _ = await bridge._agentic_loop_openai_responses(
        client, _TEST_MODEL, [Message.system("s"), Message.user_text("hi")],
        _FakeBuilder(chat_id=424005), api_label="unit-test", tools=[], journal=[],
        workspace_namespace=None, turn=turn,
    )
    assert content == "bootstrapped"
    assert client.calls[0]["previous_response_id"] == "resp_gone"
    # 重试请求：无 previous_response_id（canonical history 全量 bootstrap）。
    assert "previous_response_id" not in client.calls[1]
    assert [
        item for item in client.calls[1]["input"] if item.get("type") == "message"
    ] == [{
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }]
    assert st.chain.response_id == "resp_boot"


@pytest.mark.asyncio
async def test_stale_id_retry_only_once(_bridge_env):
    bridge = _bridge_env
    rs, st = _fresh_state(424006)
    t0 = st.begin_turn("USER")
    st.commit_response(t0, vendor_key=rs.derive_vendor_key(
        SUPPORTED_MODELS[_TEST_MODEL]), response_id="resp_gone", model=_TEST_MODEL)

    turn = st.begin_turn("USER")
    client = _FakeResponsesClient([
        _StaleResponseError("resp_gone"),
        _StaleResponseError("resp_gone"),  # bootstrap 后再次失败 → 不再重试
    ])
    with pytest.raises(_StaleResponseError):
        await bridge._agentic_loop_openai_responses(
            client, _TEST_MODEL, [Message.system("s"), Message.user_text("hi")],
            _FakeBuilder(chat_id=424006), api_label="unit-test", tools=[], journal=[],
            workspace_namespace=None, turn=turn,
        )
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_midstream_failure_after_dispatch_breaks_chain(_bridge_env):
    bridge = _bridge_env
    rs, st = _fresh_state(424007)
    vendor_key = rs.derive_vendor_key(SUPPORTED_MODELS[_TEST_MODEL])
    t0 = st.begin_turn("USER")
    st.commit_response(t0, vendor_key=vendor_key, response_id="resp_prev", model=_TEST_MODEL)

    turn = st.begin_turn("USER")

    class _BrokenStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("connection reset mid-stream")

    class _Client:
        def __init__(self):
            self.calls = 0
            self.responses = self

        async def create(self, **kwargs):
            self.calls += 1
            return _BrokenStream()

    client = _Client()
    with pytest.raises(RuntimeError):
        await bridge._agentic_loop_openai_responses(
            client, _TEST_MODEL, [Message.system("s"), Message.user_text("hi")],
            _FakeBuilder(chat_id=424007), api_label="unit-test", tools=[], journal=[],
            workspace_namespace=None, turn=turn,
        )
    # 请求已出网后异常：显式断链，下一轮必须 bootstrap。
    assert st.chain.response_id is None


@pytest.mark.asyncio
async def test_generic_4xx_is_not_treated_as_stale_chain(_bridge_env):
    bridge = _bridge_env
    rs, st = _fresh_state(424008)
    t0 = st.begin_turn("USER")
    st.commit_response(t0, vendor_key=rs.derive_vendor_key(
        SUPPORTED_MODELS[_TEST_MODEL]), response_id="resp_prev", model=_TEST_MODEL)

    class _InputTooLong(Exception):
        status_code = 400

        def __init__(self):
            super().__init__("input must be non-empty")

    turn = st.begin_turn("USER")
    client = _FakeResponsesClient([_InputTooLong()])
    with pytest.raises(_InputTooLong):
        await bridge._agentic_loop_openai_responses(
            client, _TEST_MODEL, [Message.system("s"), Message.user_text("hi")],
            _FakeBuilder(chat_id=424008), api_label="unit-test", tools=[], journal=[],
            workspace_namespace=None, turn=turn,
        )
    # 普通 4xx 不能证明 previous_response_id 失效：不 bootstrap、不断链。
    assert len(client.script) == 0  # 没有发生第二次请求
    assert st.chain.response_id == "resp_prev"


# ---------------------------------------------------------------------------
# 4. 空 input invariant：调用 SDK 前失败
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_input_raises_protocol_error_before_sdk_call(_bridge_env):
    bridge = _bridge_env
    from ai.errors import ResponsesProtocolError

    rs, st = _fresh_state(424009)
    t0 = st.begin_turn("USER")
    st.commit_response(t0, vendor_key=rs.derive_vendor_key(
        SUPPORTED_MODELS[_TEST_MODEL]), response_id="resp_prev", model=_TEST_MODEL)

    turn = st.begin_turn("USER")
    # 历史以 assistant 结尾且没有新增 user 内容 → 链式轮次推导不出 input。
    client = _FakeResponsesClient([])
    with pytest.raises(ResponsesProtocolError):
        await bridge._agentic_loop_openai_responses(
            client, _TEST_MODEL,
            [Message.system("s"), Message.assistant_text("nothing new")],
            _FakeBuilder(chat_id=424009), api_label="unit-test", tools=[], journal=[],
            workspace_namespace=None, turn=turn,
        )
    assert client.calls == []  # 绝不向供应商发出 input: []


# ---------------------------------------------------------------------------
# 5. _turn_input_messages 纯函数
# ---------------------------------------------------------------------------
def test_turn_input_messages_tail_after_last_assistant():
    from ai.responses_bridge import _turn_input_messages

    sys_m = Message.system("s")
    u1 = Message.user_text("u1")
    a1 = Message.assistant_text("a1")
    u2 = Message.user_text("u2")
    tail = _turn_input_messages([sys_m, u1, a1, u2])
    assert [m.text() for m in tail] == ["u2"]
    # 没有 assistant 时尾部从头开始（system 项在协议层汇入 instructions，
    # 不产生 input item）。
    assert [m.text() for m in _turn_input_messages([sys_m, u1])] == ["s", "u1"]
    assert _turn_input_messages([sys_m, u1, a1]) == []
