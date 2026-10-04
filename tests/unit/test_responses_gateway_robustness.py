# tests/unit/test_responses_gateway_robustness.py
"""生产事故回归：lfree 网关上的两类 Responses 失败。

1. 工具续轮（previous_response_id + 纯 function_call_output）被上游以
   ``Upstream request failed: invalid request`` 拒绝——措辞不含
   ``input must be non-empty``，旧代码的恢复路径靠文本匹配，直接整回合
   失败并丢掉已执行的工具结果。
2. 模型输出了 function_call，但网关的 ``response.completed.output`` 不含
   它（或 item.status 停在 in_progress）——旧代码静默丢掉调用，回合以
   “AI 响应为空”收场，同时还把带着悬空 function_call 的 response 提交成
   了链头。
"""
from __future__ import annotations

import pytest

from core.messages import Message

from tests.unit.test_responses_chain_bridge import (  # noqa: F401  (fixtures/helpers)
    _TEST_MODEL,
    _FakeBuilder,
    _FakeResponsesClient,
    _bridge_env,
    _completed,
    _fresh_state,
    _function_call_item,
    _message_item,
    _run_tool_batch_factory,
)


class _HttpError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


async def _run(bridge, client, chat_id, turn):
    return await bridge._agentic_loop_openai_responses(
        client, _TEST_MODEL, [Message.system("s"), Message.user_text("go")],
        _FakeBuilder(chat_id=chat_id), api_label="unit-test", tools=[], journal=[],
        workspace_namespace=None, turn=turn,
    )


# ---------------------------------------------------------------------------
# 1. 续轮被拒：与措辞无关，按 4xx 状态恢复
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 409, 422])
async def test_tool_continuation_rejected_with_any_wording_bootstraps(
    _bridge_env, monkeypatch, status,
):
    bridge = _bridge_env
    rs, st = _fresh_state(425000 + status)
    executed: list = []
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory(executed))
    turn = st.begin_turn("USER")

    client = _FakeResponsesClient([
        _completed("0710e51b65acd9d7680f43a8eb1e8493",
                   [_function_call_item("call_1", "bash", '{"cmd":"ls"}')]),
        _HttpError(status, "Error from provider (Console): Upstream request failed: "
                           "[invalid_request_error] invalid request"),
        _completed("resp_boot", [_message_item("recovered")]),
    ])
    content, _, _ = await _run(bridge, client, 425000 + status, turn)

    assert content == "recovered"
    assert [c["id"] for c in executed] == ["call_1"]  # 工具只执行一次，结果没丢
    assert len(client.calls) == 3
    assert client.calls[1]["previous_response_id"] == "0710e51b65acd9d7680f43a8eb1e8493"
    retry = client.calls[2]
    assert "previous_response_id" not in retry
    types = [i.get("type") for i in retry["input"]]
    assert "function_call" in types and "function_call_output" in types
    assert st.chain.response_id == "resp_boot"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
async def test_non_chain_errors_are_not_retried_on_tool_continuation(
    _bridge_env, monkeypatch, status,
):
    bridge = _bridge_env
    rs, st = _fresh_state(425100 + status)
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory([]))
    turn = st.begin_turn("USER")

    client = _FakeResponsesClient([
        _completed("resp_call", [_function_call_item("call_1", "bash", "{}")]),
        _HttpError(status, "boom"),
    ])
    with pytest.raises(_HttpError):
        await _run(bridge, client, 425100 + status, turn)
    assert len(client.calls) == 2  # 没有额外的 bootstrap 重试


@pytest.mark.asyncio
async def test_bootstrap_also_rejected_is_raised_not_looped(_bridge_env, monkeypatch):
    bridge = _bridge_env
    rs, st = _fresh_state(425200)
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory([]))
    turn = st.begin_turn("USER")

    client = _FakeResponsesClient([
        _completed("resp_call", [_function_call_item("call_1", "bash", "{}")]),
        _HttpError(400, "invalid request"),   # 链式续轮被拒
        _HttpError(400, "invalid request"),   # bootstrap 也被拒：请求本身有问题
    ])
    with pytest.raises(_HttpError):
        await _run(bridge, client, 425200, turn)
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_stale_id_during_tool_continuation_drops_pending_output(_bridge_env, monkeypatch):
    """既有缺陷：stale previous_response_id 恢复若发生在工具续轮，
    旧代码会在 bootstrap 重试里仍只发孤立的 function_call_output。"""
    bridge = _bridge_env
    rs, st = _fresh_state(425300)
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory([]))
    turn = st.begin_turn("USER")

    client = _FakeResponsesClient([
        _completed("resp_call", [_function_call_item("call_1", "bash", "{}")]),
        _HttpError(404, "previous response id resp_call not found"),
        _completed("resp_boot", [_message_item("ok")]),
    ])
    content, _, _ = await _run(bridge, client, 425300, turn)
    assert content == "ok"
    retry = client.calls[2]
    assert "previous_response_id" not in retry
    types = [i.get("type") for i in retry["input"]]
    assert "function_call" in types, "bootstrap 重放必须带上 function_call，不能只剩孤立 output"


def test_unsupported_mark_expires(monkeypatch):
    import responses_state as rs
    rs._tool_chain_unsupported.clear()
    rs.mark_tool_continuation_chain_unsupported("v", "m")
    assert rs.is_tool_continuation_chain_unsupported("v", "m")
    base = rs.time.monotonic()
    monkeypatch.setattr(rs.time, "monotonic", lambda: base + rs._TOOL_CHAIN_UNSUPPORTED_TTL + 1)
    assert not rs.is_tool_continuation_chain_unsupported("v", "m")
    assert ("v", "m") not in rs._tool_chain_unsupported


# ---------------------------------------------------------------------------
# 2. function_call 不在权威 output 里
# ---------------------------------------------------------------------------
def _stream_only_call_events(response_id: str, call_id: str, name: str, args: str):
    return [
        {"type": "response.output_item.added",
         "item": {"type": "function_call", "id": f"fc_{call_id}",
                  "call_id": call_id, "name": name, "arguments": ""}},
        {"type": "response.function_call_arguments.delta",
         "item_id": f"fc_{call_id}", "delta": args},
        {"type": "response.function_call_arguments.done",
         "item_id": f"fc_{call_id}", "arguments": args},
        {"type": "response.completed",
         "response": {"id": response_id, "status": "completed", "output": []}},
    ]


@pytest.mark.asyncio
async def test_function_call_only_in_stream_events_is_executed(_bridge_env, monkeypatch):
    bridge = _bridge_env
    rs, st = _fresh_state(425400)
    executed: list = []
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory(executed))
    turn = st.begin_turn("USER")

    client = _FakeResponsesClient([
        _stream_only_call_events("resp_s", "call_s", "bash", '{"cmd":"unzip x"}'),
        _completed("resp_final", [_message_item("done")]),
    ])
    content, _, _ = await _run(bridge, client, 425400, turn)

    assert content == "done"
    assert [c["id"] for c in executed] == ["call_s"]
    assert executed[0]["function"]["arguments"] == '{"cmd":"unzip x"}'
    # 服务端存的 response 不一定认得这个调用：续轮直接 bootstrap，不发链式请求。
    assert len(client.calls) == 2
    nxt = client.calls[1]
    assert "previous_response_id" not in nxt
    types = [i.get("type") for i in nxt["input"]]
    assert "function_call" in types and "function_call_output" in types
    assert st.chain.response_id == "resp_final"


@pytest.mark.asyncio
async def test_function_call_stuck_in_progress_in_completed_response_is_executed(
    _bridge_env, monkeypatch,
):
    bridge = _bridge_env
    rs, st = _fresh_state(425500)
    executed: list = []
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory(executed))
    turn = st.begin_turn("USER")

    stuck = dict(_function_call_item("call_1", "bash", "{}"), status="in_progress")
    client = _FakeResponsesClient([
        _completed("resp_call", [stuck]),
        _completed("resp_final", [_message_item("done")]),
    ])
    content, _, _ = await _run(bridge, client, 425500, turn)
    assert content == "done"
    assert [c["id"] for c in executed] == ["call_1"]


@pytest.mark.asyncio
async def test_explicitly_incomplete_function_call_is_still_not_executed(_bridge_env, monkeypatch):
    bridge = _bridge_env
    rs, st = _fresh_state(425600)
    executed: list = []
    monkeypatch.setattr(bridge, "run_tool_batch", _run_tool_batch_factory(executed))
    turn = st.begin_turn("USER")

    bad = dict(_function_call_item("call_1", "bash", "{}"), status="incomplete")
    client = _FakeResponsesClient([_completed("resp_x", [bad])])
    await _run(bridge, client, 425600, turn)
    assert executed == []


# ---------------------------------------------------------------------------
# 3. 空终局不能提交链头
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_final_response_does_not_commit_chain(_bridge_env):
    bridge = _bridge_env
    rs, st = _fresh_state(425700)
    turn = st.begin_turn("USER")

    client = _FakeResponsesClient([_completed("resp_empty", [])])
    content, _, _ = await _run(bridge, client, 425700, turn)

    assert (content or "").strip() == ""
    assert st.chain is None or st.chain.response_id is None
    # 下一轮应当从 canonical history bootstrap，而不是接在空 response 后面。
    _, mode = st.resolve_chain(rs.derive_vendor_key(
        __import__("config").SUPPORTED_MODELS[_TEST_MODEL]), _TEST_MODEL)
    assert mode.startswith("bootstrap")
