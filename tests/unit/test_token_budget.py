# =====================================================================
# tests/unit/test_token_budget.py — token 计数与截断预算
# =====================================================================
# 被测关键路径：所有模型上下文预算的统一守卫层。
# 覆盖：count_tokens 基础语义、truncate_to_token_budget 严格不超预算、
#       truncate_to_token_budget_head_tail 头尾保留、边界与非法值、JSON 计数。
# =====================================================================
import json

import pytest

from token_budget import (
    count_tokens,
    json_token_count,
    truncate_to_token_budget,
    truncate_to_token_budget_head_tail,
)

LONG_TEXT = (
    "这是一段用于测试截断逻辑的中文长文本。" * 40
    + "The quick brown fox jumps over the lazy dog. " * 40
)


# ---------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------
def test_count_tokens_empty_and_none():
    assert count_tokens("") == 0
    assert count_tokens(None) == 0


def test_count_tokens_positive_and_monotonic():
    short = count_tokens("hello world")
    assert short > 0
    assert count_tokens("hello world, and the rest of the sentence.") > short


def test_count_tokens_non_string_input_uses_str():
    assert count_tokens(12345) == count_tokens(str(12345))
    assert count_tokens(["a", "b"]) == count_tokens(str(["a", "b"]))


def test_count_tokens_chinese_text():
    # 中文在 o200k_base 下压缩率高（常用词 2 字 1 token），确保计数非零且随长度增长
    short = count_tokens("你好世界")
    assert short >= 2
    assert count_tokens("你好世界" * 10) > short


# ---------------------------------------------------------------------
# truncate_to_token_budget
# ---------------------------------------------------------------------
def test_truncate_within_budget_returns_original():
    assert truncate_to_token_budget(LONG_TEXT, 10_000) == LONG_TEXT
    assert truncate_to_token_budget("短文本", 100) == "短文本"


def test_truncate_respects_budget_exactly():
    for budget in (16, 64, 200, 1000):
        out = truncate_to_token_budget(LONG_TEXT, budget)
        assert count_tokens(out) <= budget


def test_truncate_appends_suffix_when_truncated():
    out = truncate_to_token_budget(LONG_TEXT, 64)
    assert out != LONG_TEXT
    assert out.endswith("…[内容已按 token 预算截断]")


def test_truncate_budget_zero_returns_empty():
    assert truncate_to_token_budget(LONG_TEXT, 0) == ""
    assert truncate_to_token_budget("", 100) == ""


def test_truncate_none_returns_empty():
    assert truncate_to_token_budget(None, 100) == ""


def test_truncate_negative_budget_raises():
    with pytest.raises(ValueError):
        truncate_to_token_budget("x", -1)


def test_truncate_custom_suffix_within_budget():
    out = truncate_to_token_budget(LONG_TEXT, 30, suffix="…(更多)")
    assert out.endswith("…(更多)")
    assert count_tokens(out) <= 30


def test_truncate_suffix_exceeds_budget_degrades():
    # 预算小于后缀自身 token 数时，返回被截短的后缀且仍不超预算
    out = truncate_to_token_budget(LONG_TEXT, 2)
    assert count_tokens(out) <= 2
    assert out.startswith("…")


def test_truncate_non_string_value():
    big = {"k": "v" * 500}
    out = truncate_to_token_budget(big, 20)
    assert count_tokens(out) <= 20


# ---------------------------------------------------------------------
# truncate_to_token_budget_head_tail
# ---------------------------------------------------------------------
def test_head_tail_within_budget_returns_original():
    assert truncate_to_token_budget_head_tail(LONG_TEXT, 10_000) == LONG_TEXT


def test_head_tail_keeps_both_ends_and_budget():
    budget = 120
    out = truncate_to_token_budget_head_tail(LONG_TEXT, budget)
    assert count_tokens(out) <= budget
    # 头部内容保留（开头一段文字可在输出中找到）
    assert LONG_TEXT[:30] in out
    # 尾部内容保留（结尾一段文字可在输出中找到）
    assert LONG_TEXT[-30:] in out
    # 中间省略说明存在
    assert "已保留开头" in out and "已省略" in out


def test_head_tail_head_is_prefix_tail_is_suffix():
    budget = 100
    out = truncate_to_token_budget_head_tail(LONG_TEXT, budget)
    # 头部 40 字符是原文前缀，尾部 40 字符是原文后缀（token 对齐回填保真）
    assert LONG_TEXT.startswith(out[:40])
    assert LONG_TEXT.endswith(out[-40:])


def test_head_tail_extreme_ratio_still_within_budget():
    for ratio in (0.0, 0.1, 0.9, 1.0):
        out = truncate_to_token_budget_head_tail(LONG_TEXT, 80, head_ratio=ratio)
        assert count_tokens(out) <= 80


def test_head_tail_boundary_values():
    assert truncate_to_token_budget_head_tail(None, 100) == ""
    assert truncate_to_token_budget_head_tail(LONG_TEXT, 0) == ""
    with pytest.raises(ValueError):
        truncate_to_token_budget_head_tail("x", -5)
    # 极小预算退化路径也不抛异常、不超预算
    tiny = truncate_to_token_budget_head_tail(LONG_TEXT, 3)
    assert count_tokens(tiny) <= 3


# ---------------------------------------------------------------------
# json_token_count
# ---------------------------------------------------------------------
def test_json_token_count_matches_manual_serialization():
    value = {"city": "上海", "temps": [20, 21, 22], "ok": True}
    assert json_token_count(value) == count_tokens(
        json.dumps(value, ensure_ascii=False, default=str)
    )


def test_json_token_count_unserializable_falls_back():
    # set 无法 JSON 序列化，应回退到 str(value) 计数且不抛异常
    assert json_token_count({"s": {1, 2, 3}}) > 0
