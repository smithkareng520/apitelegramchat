# =====================================================================
# tests/unit/test_responses_cache_breakpoints.py — Responses 缓存断点策略
# =====================================================================
# 被测关键路径：ai/responses_bridge 的缓存层级注入 + config 的显式断点
# 总开关（explicit_cache_breakpoints_enabled）。
#
# 策略契约（2026-09-09 起）：
#   1) 默认（所有 API）：请求只带 prompt_cache_key +
#      prompt_cache_options.mode="implicit"（保留 1 个网关自动断点），
#      input 上绝不出现 prompt_cache_breakpoint 字段——当前网关不支持
#      手动断点对象，下发即 400；
#   2) 显式断点 = 可选增益：模型字段 explicit_cache_breakpoints 或环境
#      变量 RESPONSES_EXPLICIT_CACHE_BREAKPOINTS（三态强制开关）开启后，
#      才在自动断点之外额外下发最多 3 个显式断点；
#   3) 显式断点位置复刻 anthropic_bridge 的 Claude 策略：前部 1 固定
#      （第一条 user 消息文本块）+ 尾部 2 滚动（最后两个可用文本块），
#      形状必须是 {"mode": "explicit"} 对象（历史 bug 曾序列化为布尔）；
#   4) 非流式一次性调用（subagent 路径）遵循同一开关与默认行为。
# =====================================================================
import asyncio
import os
from types import SimpleNamespace

import config
from ai import responses_bridge as rb
from config import explicit_cache_breakpoints_enabled

_ENV_FLAG = "RESPONSES_EXPLICIT_CACHE_BREAKPOINTS"


# --------------------------------------------------------------------------
# 构造辅助
# --------------------------------------------------------------------------
def _msg_item(role: str, text: str) -> dict:
    """Responses message item（user -> input_text / assistant -> output_text）。"""
    part_type = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": part_type, "text": text}]}


def _function_call_item(call_id: str = "call_1") -> dict:
    return {"type": "function_call", "call_id": call_id, "name": "f", "arguments": "{}"}


def _function_call_output_item(call_id: str = "call_1") -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": "ok"}


def _fake_model(**overrides) -> SimpleNamespace:
    base = {"supports_prompt_cache": True, "explicit_cache_breakpoints": False}
    base.update(overrides)
    return SimpleNamespace(**base)


def _marked_positions(input_items: list) -> list:
    """返回被标记了显式断点的 (item_index, part_index) 列表。"""
    out = []
    for i, item in enumerate(input_items):
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, list):
            continue
        for j, part in enumerate(content):
            if isinstance(part, dict) and "prompt_cache_breakpoint" in part:
                out.append((i, j))
    return out


class _CaptureClient:
    """捕获 responses.create 请求 kwargs 的假客户端（非流式路径用）。"""

    def __init__(self) -> None:
        self.captured: dict = {}
        self.responses = self

    async def create(self, **kwargs):
        self.captured = kwargs
        return SimpleNamespace(output=[], usage=None)


# --------------------------------------------------------------------------
# 1) 默认：只走自动断点，请求零显式断点字段
# --------------------------------------------------------------------------
def test_default_request_has_no_breakpoint_fields():
    """_add_responses_cache_options 默认只注入 key + implicit 自动断点。"""
    items = [_msg_item("user", "hello"), _msg_item("assistant", "hi")]
    kwargs: dict = {}
    rb._add_responses_cache_options(kwargs, api_label="t", model="m", chat_id=1, enabled=True)
    assert kwargs["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert kwargs["prompt_cache_key"]
    # input 未被本函数触碰，绝无断点字段
    for item in items:
        for part in item["content"]:
            assert "prompt_cache_breakpoint" not in part


def test_switch_disabled_by_default_for_model_and_none():
    """模型字段缺省 / None（继承厂商默认 False）时开关为关。"""
    assert explicit_cache_breakpoints_enabled(None) is False
    assert explicit_cache_breakpoints_enabled(_fake_model()) is False
    assert explicit_cache_breakpoints_enabled(
        _fake_model(explicit_cache_breakpoints=None)) is False


def test_real_gpt56_sol_model_defaults_to_auto_only():
    """真实模型 gpt-5.6-sol 默认不下发显式断点（网关未就绪）。"""
    model_info = config.SUPPORTED_MODELS["gpt-5.6-sol"]
    assert model_info.supports_prompt_cache is True
    assert explicit_cache_breakpoints_enabled(model_info) is False


def test_cache_options_respect_enabled_gate():
    """enabled=False 时连 key/options 都不注入（非缓存模型零副作用）。"""
    kwargs: dict = {}
    rb._add_responses_cache_options(kwargs, api_label="t", model="m", chat_id=1, enabled=False)
    assert "prompt_cache_key" not in kwargs
    assert "prompt_cache_options" not in kwargs


# --------------------------------------------------------------------------
# 2) 显式断点位置 / 数量 / 形状（复刻 Claude 策略）
# --------------------------------------------------------------------------
def test_apply_breakpoints_front_fixed_plus_tail_two():
    """前部 1 固定 + 尾部 2 滚动：3 个断点全落在文本块上，形状为对象。"""
    items = [
        _msg_item("user", "u1"),        # 候选 0（最前 -> 固定断点）
        _msg_item("assistant", "a1"),
        _msg_item("user", "u2"),        # 候选 1（尾部滚动 2）
        _msg_item("assistant", "a2"),
        _msg_item("user", "u3"),        # 候选 2（尾部滚动 1）
    ]
    applied = rb._apply_responses_cache_breakpoints(items)
    assert applied == 3
    positions = _marked_positions(items)
    assert positions == [(0, 0), (2, 0), (4, 0)]
    for i, j in positions:
        assert items[i]["content"][j]["prompt_cache_breakpoint"] == {"mode": "explicit"}


def test_apply_breakpoints_skips_non_message_items():
    """function_call / function_call_output 无文本块，不参与也不报错。"""
    items = [
        _msg_item("user", "u1"),
        _function_call_item(),
        _function_call_output_item(),
    ]
    applied = rb._apply_responses_cache_breakpoints(items)
    assert applied == 1
    assert _marked_positions(items) == [(0, 0)]


def test_apply_breakpoints_fewer_candidates_fewer_marks():
    """候选不足时按实际数量降级，前部与尾部重叠时不重复标记。"""
    single = [_msg_item("user", "only")]
    assert rb._apply_responses_cache_breakpoints(single) == 1
    assert _marked_positions(single) == [(0, 0)]

    empty: list = []
    assert rb._apply_responses_cache_breakpoints(empty) == 0


def test_apply_breakpoints_never_exceeds_three():
    """候选再多也只标 3 个（合计 3 显式 + 1 自动 <= 每请求 4 个上限）。"""
    items = [_msg_item("user", f"u{i}") for i in range(10)]
    assert rb._apply_responses_cache_breakpoints(items) == 3
    assert len(_marked_positions(items)) == 3


def test_apply_breakpoints_marks_are_objects_not_booleans():
    """回归：断点字段必须是对象（历史 bug 曾写成 true 布尔导致 400）。"""
    items = [_msg_item("user", "u1"), _msg_item("user", "u2")]
    rb._apply_responses_cache_breakpoints(items)
    for i, j in _marked_positions(items):
        mark = items[i]["content"][j]["prompt_cache_breakpoint"]
        assert isinstance(mark, dict) and mark.get("mode") == "explicit"


# --------------------------------------------------------------------------
# 3) 开关优先级：环境变量三态 > 模型字段 > 默认关
# --------------------------------------------------------------------------
def test_env_tri_state_overrides_model_field(monkeypatch):
    monkeypatch.delenv(_ENV_FLAG, raising=False)
    # 未设置环境变量 -> 跟随模型字段
    assert explicit_cache_breakpoints_enabled(_fake_model()) is False
    assert explicit_cache_breakpoints_enabled(
        _fake_model(explicit_cache_breakpoints=True)) is True

    # =1 强制开：即使模型字段为 False
    monkeypatch.setenv(_ENV_FLAG, "1")
    assert explicit_cache_breakpoints_enabled(_fake_model()) is True
    for truthy in ("true", "YES", "on"):
        monkeypatch.setenv(_ENV_FLAG, truthy)
        assert explicit_cache_breakpoints_enabled(_fake_model()) is True

    # =0 强制关：即使模型字段为 True
    monkeypatch.setenv(_ENV_FLAG, "0")
    assert explicit_cache_breakpoints_enabled(
        _fake_model(explicit_cache_breakpoints=True)) is False
    for falsy in ("false", "NO", "off"):
        monkeypatch.setenv(_ENV_FLAG, falsy)
        assert explicit_cache_breakpoints_enabled(
            _fake_model(explicit_cache_breakpoints=True)) is False

    # 无法识别的值 -> 不强制，回退模型字段
    monkeypatch.setenv(_ENV_FLAG, "maybe")
    assert explicit_cache_breakpoints_enabled(_fake_model()) is False


def test_switch_requires_prompt_cache_support_in_loop_decision():
    """主循环决策：prompt_cache_enabled=False 时即使开关开也不叠断点。"""
    model = _fake_model(supports_prompt_cache=False, explicit_cache_breakpoints=True)
    prompt_cache_enabled = bool(model and getattr(model, "supports_prompt_cache", False))
    assert (prompt_cache_enabled and explicit_cache_breakpoints_enabled(model)) is False


# --------------------------------------------------------------------------
# 4) 非流式一次性调用（subagent 路径）遵循同一开关
# --------------------------------------------------------------------------
def _run_nonstream(monkeypatch, model_info) -> dict:
    monkeypatch.setattr(rb, "SUPPORTED_MODELS", {"test-model": model_info})
    client = _CaptureClient()
    # input 候选：user 消息文本块（input_text）x3；assistant 文本块
    # （output_text）与 system（并入 instructions）不参与断点标记。
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]
    asyncio.run(rb.openai_responses_chat_completions_create(
        client, model="test-model", messages=messages, max_tokens=64))
    return client.captured


def test_nonstream_default_auto_only(monkeypatch):
    """默认：非流式请求同样零显式断点字段，仅 implicit 自动断点。"""
    captured = _run_nonstream(monkeypatch, _fake_model())
    assert captured["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert _marked_positions(captured["input"]) == []


def test_nonstream_explicit_opt_in(monkeypatch):
    """开关开启：非流式请求额外叠加 3 个显式断点（位置与主循环一致）。"""
    captured = _run_nonstream(
        monkeypatch, _fake_model(explicit_cache_breakpoints=True))
    # options 保持 implicit（自动断点仍在），显式断点叠加在 input 上
    assert captured["prompt_cache_options"]["mode"] == "implicit"
    positions = _marked_positions(captured['input'])
    assert len(positions) == 3
    # input: [u1, a1, u2, a2, u3] -> 前部固定 0 + 尾部滚动 4、2（仅
    # input_text 候选；assistant/output_text 不参与标记）
    assert positions == [(0, 0), (2, 0), (4, 0)]
    for i, j in positions:
        assert captured["input"][i]["content"][j]["prompt_cache_breakpoint"] == {
            "mode": "explicit",
        }


def test_nonstream_env_force_on(monkeypatch):
    """环境变量强制开启同样作用于非流式路径（模型字段仍为 False）。"""
    monkeypatch.setenv(_ENV_FLAG, "1")
    try:
        captured = _run_nonstream(monkeypatch, _fake_model())
        assert len(_marked_positions(captured["input"])) == 3
    finally:
        monkeypatch.delenv(_ENV_FLAG, raising=False)


def test_nonstream_unknown_model_never_marks_breakpoints(monkeypatch):
    """未知模型：保守保留缓存 key/options，但绝不下发显式断点。"""
    monkeypatch.setattr(rb, "SUPPORTED_MODELS", {})
    client = _CaptureClient()
    asyncio.run(rb.openai_responses_chat_completions_create(
        client, model="unknown-model",
        messages=[{"role": "user", "content": "u1"}], max_tokens=64))
    assert client.captured["prompt_cache_options"]["mode"] == "implicit"
    assert _marked_positions(client.captured["input"]) == []
