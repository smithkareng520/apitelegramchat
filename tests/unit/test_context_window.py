# =====================================================================
# tests/unit/test_context_window.py — 上下文窗口核心（压缩/淘汰/摘要）
# =====================================================================
# 被测关键路径：会话历史的预算管理核心。
# 覆盖：摘要消息识别、轮块划分、淘汰规划（目标水位 + 受保护轮次 + 摘要槽位
#       永不淘汰）、计划落地、滚动摘要确定性与预算约束、预算解析与水位。
# =====================================================================
import json

import pytest

import context_window as cw
from context_window import (
    DIGEST_MARKER,
    EvictionPlan,
    apply_eviction_plan,
    build_digest_text,
    compact_watermarks,
    count_history_tokens,
    effective_digest_budget,
    is_digest_message,
    make_digest_message,
    plan_turn_eviction,
    resolve_history_budget,
    split_history_blocks,
)


def fixed_token_fn(cost: int):
    """确定性 token 计数：每条消息固定 cost。"""
    return lambda m: cost


def user_block(text: str, replies: int = 1):
    """构造一个用户轮块：1 条 user + replies 条 assistant。"""
    block = [{"role": "user", "content": text}]
    block += [{"role": "assistant", "content": f"回复：{text}"} for _ in range(replies)]
    return block


# ---------------------------------------------------------------------
# 摘要消息识别
# ---------------------------------------------------------------------
def test_is_digest_message_positive_and_negative():
    assert is_digest_message(make_digest_message(DIGEST_MARKER + " 早期 2 轮…"))
    assert not is_digest_message({"role": "user", "content": DIGEST_MARKER + " x"})
    assert not is_digest_message({"role": "system", "content": "普通系统消息"})
    assert not is_digest_message({"role": "system", "content": 123})
    assert not is_digest_message("不是 dict")


# ---------------------------------------------------------------------
# 轮块划分
# ---------------------------------------------------------------------
def test_split_history_blocks_empty():
    digest, blocks = split_history_blocks([])
    assert digest is None
    assert blocks == []


def test_split_history_blocks_extracts_head_digest():
    digest_msg = make_digest_message(DIGEST_MARKER + " 早期 1 轮对话已压缩")
    history = [digest_msg] + user_block("问题一") + user_block("问题二")
    digest, blocks = split_history_blocks(history)
    assert digest is digest_msg
    assert len(blocks) == 2
    assert blocks[0][0]["content"] == "问题一"
    assert blocks[1][0]["content"] == "问题二"


def test_split_history_blocks_non_digest_system_head_not_extracted():
    # 摘要槽位识别要求正文以稳定标记开头，普通 system 消息不占槽位
    history = [{"role": "system", "content": "普通系统提示"}] + user_block("问题")
    digest, blocks = split_history_blocks(history)
    assert digest is None
    assert len(blocks) == 2  # [system] 前导块 + [user+assistant] 块


def test_split_history_blocks_leading_orphan_block():
    # 打断恢复场景：头部孤立 assistant/tool 消息构成无 user 锚点的前导块
    history = [
        {"role": "assistant", "content": "孤立回复"},
        {"role": "tool", "tool_call_id": "t1", "content": "工具结果"},
        {"role": "user", "content": "正式问题"},
    ]
    digest, blocks = split_history_blocks(history)
    assert digest is None
    assert len(blocks) == 2
    assert blocks[0][0]["role"] == "assistant"
    assert blocks[1][0]["role"] == "user"


def test_split_history_blocks_consecutive_users_split():
    history = [
        {"role": "user", "content": "A"},
        {"role": "user", "content": "B"},
    ]
    _, blocks = split_history_blocks(history)
    assert len(blocks) == 2


# ---------------------------------------------------------------------
# 淘汰规划
# ---------------------------------------------------------------------
def test_plan_eviction_reaches_target():
    history = []
    for i in range(5):
        history += user_block(f"问题{i}")
    # 每条消息 100 token，每块 2 条 → 每块 200，总计 1000
    plan = plan_turn_eviction(
        history, target_tokens=500, protected_turns=0, token_fn=fixed_token_fn(100)
    )
    assert plan.total_tokens == 1000
    # 1000 → 800 → 600 → 400 ≤ 500 停止：淘汰 3 块
    assert len(plan.evicted_blocks) == 3
    assert plan.evicted_tokens == 600
    assert plan.kept_tokens == 400
    assert plan.kept_blocks[0][0]["content"] == "问题3"


def test_plan_eviction_respects_protected_turns():
    history = []
    for i in range(5):
        history += user_block(f"问题{i}")
    # 目标 0 也绝不动最近 2 块
    plan = plan_turn_eviction(
        history, target_tokens=0, protected_turns=2, token_fn=fixed_token_fn(100)
    )
    assert len(plan.evicted_blocks) == 3
    assert plan.kept_blocks[0][0]["content"] == "问题3"
    assert plan.kept_blocks[-1][0]["content"] == "问题4"
    assert plan.protected_turns == 2


def test_plan_eviction_digest_slot_never_evicted():
    digest_msg = make_digest_message(DIGEST_MARKER + " 早期 3 轮")
    history = [digest_msg] + user_block("唯一问题")
    plan = plan_turn_eviction(
        history, target_tokens=0, protected_turns=0, token_fn=fixed_token_fn(100)
    )
    assert plan.digest_message is digest_msg
    # digest 不在淘汰列表中
    flat_evicted = [m for block in plan.evicted_blocks for m in block]
    assert digest_msg not in flat_evicted


def test_plan_eviction_oversized_block_crosses_target():
    # 单块超目标时允许整块淘汰越过 target（块粒度）：可淘汰块全部淘汰
    history = user_block("大块问题") + user_block("小问题")
    plan = plan_turn_eviction(
        history, target_tokens=50, protected_turns=0, token_fn=fixed_token_fn(100)
    )
    assert plan.total_tokens == 400
    assert len(plan.evicted_blocks) == 2
    assert plan.evicted_tokens == 400
    assert plan.kept_blocks == []
    assert plan.kept_tokens == 0


def test_plan_does_not_mutate_input():
    history = user_block("问题一") + user_block("问题二")
    snapshot = json.dumps(history, ensure_ascii=False)
    plan_turn_eviction(history, target_tokens=1, protected_turns=0,
                       token_fn=fixed_token_fn(10))
    assert json.dumps(history, ensure_ascii=False) == snapshot


def test_eviction_plan_properties():
    plan = EvictionPlan(
        evicted_blocks=[[{"role": "user", "content": "a"}],
                        [{"role": "user", "content": "b"}]],
        kept_blocks=[[{"role": "user", "content": "c"}]],
    )
    assert plan.evicted_message_count == 2
    assert plan.kept_messages == [{"role": "user", "content": "c"}]


# ---------------------------------------------------------------------
# 计划落地
# ---------------------------------------------------------------------
def test_apply_eviction_plan_splices_history_in_place():
    digest_old = make_digest_message(DIGEST_MARKER + " 早期 1 轮")
    history = [digest_old] + user_block("旧问题") + user_block("新问题")
    plan = plan_turn_eviction(
        history, target_tokens=0, protected_turns=1, token_fn=fixed_token_fn(100)
    )
    apply_eviction_plan(history, plan, DIGEST_MARKER + " 早期 2 轮对话已压缩")
    # 新历史 = [新摘要] + 受保护的最近一块（user + assistant）
    assert len(history) == 3
    assert is_digest_message(history[0])
    assert history[0]["content"] == DIGEST_MARKER + " 早期 2 轮对话已压缩"
    assert history[1] == {"role": "user", "content": "新问题"}
    assert history[2] == {"role": "assistant", "content": "回复：新问题"}


# ---------------------------------------------------------------------
# 滚动摘要
# ---------------------------------------------------------------------
def test_build_digest_deterministic():
    block = user_block("确定性检查 **重点**")
    a = build_digest_text(None, [block], budget_tokens=1500)
    b = build_digest_text(None, [block], budget_tokens=1500)
    assert a == b
    assert a.startswith(DIGEST_MARKER)
    assert "确定性检查" in a


def test_build_digest_accumulates_turns_and_older_lines_first():
    first = build_digest_text(None, [user_block("最早的问题")], budget_tokens=1500)
    assert "最早的问题" in first
    second = build_digest_text(first, [user_block("较新的问题")], budget_tokens=1500)
    assert "最早的问题" in second
    assert "较新的问题" in second
    # 轮数累计：头部标注 2 轮
    assert "早期 2 轮" in second
    # 旧摘要在前，新淘汰轮在后
    assert second.index("最早的问题") < second.index("较新的问题")


def test_build_digest_respects_token_budget():
    blocks = [user_block(f"问题{'很长' * 30}{i}", replies=2) for i in range(8)]
    text = build_digest_text(None, blocks, budget_tokens=300)
    assert cw.count_tokens(text) <= 300 + 32  # 与实现中的兜底余量一致


def test_build_digest_tiny_budget_still_valid():
    text = build_digest_text(None, [user_block("x" * 500)], budget_tokens=10)
    assert isinstance(text, str) and text


def test_build_digest_tool_lines_with_locator_and_archive():
    block = [
        {"role": "user", "content": "查天气"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "weather",
                    "arguments": json.dumps({"city": "上海", "hours": 6}, ensure_ascii=False),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call_1",
         "content": "tool payload archived at .context-archive/tool_call_1.json"},
    ]
    text = build_digest_text(None, [block], budget_tokens=1500)
    assert "T: weather(" in text
    assert 'city="上海"' in text
    assert "已归档 .context-archive/tool_call_1.json" in text


def test_build_digest_locator_truncated_to_char_budget():
    block = [
        {"role": "user", "content": "抓取"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "c2",
                "type": "function",
                "function": {"name": "fetch_url", "arguments": json.dumps(
                    {"url": "https://example.com/" + "x" * 300})},
            }],
        },
    ]
    text = build_digest_text(None, [block], budget_tokens=1500)
    # locator 上限 96 字符 + 省略号
    assert 'url="' in text
    assert "…" in text


# ---------------------------------------------------------------------
# 预算解析与水位
# ---------------------------------------------------------------------
def test_resolve_history_budget_fallback():
    assert resolve_history_budget(None, None) == cw.FALLBACK_BUDGET_TOKENS == 50000


def test_resolve_history_budget_model_derivation():
    # ratio=0.8 默认：8000 与 (10000-2000) 取更紧者
    assert resolve_history_budget(10000, 2000) == 8000
    # 小窗口触底 1024
    assert resolve_history_budget(1000, 200) == 1024


def test_resolve_history_budget_absolute_override_wins(monkeypatch):
    monkeypatch.setattr(cw, "CONTEXT_MAX_TOKENS_ENV", 0)
    assert resolve_history_budget(10000, 2000, absolute_override=1234) == 1234


def test_resolve_history_budget_env_override(monkeypatch):
    monkeypatch.setattr(cw, "CONTEXT_MAX_TOKENS_ENV", 777)
    assert resolve_history_budget(10000, 2000) == 777


def test_compact_watermarks_use_configured_ratios(monkeypatch):
    monkeypatch.setattr(cw, "CONTEXT_COMPACT_TRIGGER_RATIO", 0.90)
    monkeypatch.setattr(cw, "CONTEXT_COMPACT_TARGET_RATIO", 0.50)
    trigger, target = compact_watermarks(10000)
    assert (trigger, target) == (9000, 5000)
    # 滞后语义：触发水位必须高于目标水位
    assert trigger > target


def test_effective_digest_budget_cap(monkeypatch):
    monkeypatch.setattr(cw, "CONTEXT_DIGEST_TOKEN_BUDGET", 1500)
    assert effective_digest_budget(8000) == 1500   # 8000//4 = 2000 → 取 1500
    assert effective_digest_budget(400) == 200     # 小窗口保护下限


def test_count_history_tokens():
    history = [{"a": "x"}, {"b": "yy"}]
    assert count_history_tokens(history, token_fn=lambda m: 7) == 14
