# =====================================================================
# tests/unit/test_truncation_notice.py — 输出截断的分类与用户提示
# =====================================================================
# 被测关键路径：四条 agentic 循环（openai_compat / anthropic / gemini /
# responses）共用的「纯文本终局是否被输出长度上限截断」判定与提示追加。
#
# 回归背景（Code Review 发现，2026-09）：
#   json_repair._finish_reason_cut_info 此前只被 build_invalid_arguments_
#   envelope 消费——只在「本轮解析出了工具调用但参数 JSON 非法」时才会
#   被查阅，用于诊断参数是否被输出上限截断。当模型本轮没有调用任何
#   工具、只是输出了一段被 max_tokens/length 提前切断的纯文本终局回答
#   时，finish_reason 同样带着这个信息，却从未被任何调用方读取：截断的
#   回答会被当成完整回答直接展示给用户、写入历史，用户和模型自己都
#   无法得知回答其实没说完。
#
#   同时发现 OpenAI Responses API 桥接（responses_bridge.py）此前把
#   response.incomplete 事件名当作 finish_reason 记录，但真正的截断
#   原因在 response.incomplete_details.reason（字面量是
#   "max_output_tokens"，不是 "incomplete"，也不是 Chat Completions /
#   Anthropic 使用的 "length"/"max_tokens"）——旧代码既没有提取这个
#   字段，_finish_reason_cut_info 本身也不认识这个取值，导致该协议的
#   截断诊断/提示永远判定为"未截断"。
#
# 本文件锁定修复后的行为：分类逻辑（_finish_reason_cut_info）与提示
# 追加逻辑（append_truncation_notice_if_needed）分别覆盖。
# =====================================================================
import pytest

from ai.bridge_common import append_truncation_notice_if_needed, _TRUNCATION_NOTICE
from ai.json_repair import _finish_reason_cut_info


class _FakeBuilder:
    """只需要 add_text 的最小 DraftManager 替身。"""

    def __init__(self) -> None:
        self.added: list[str] = []

    def add_text(self, text: str) -> None:
        self.added.append(text)


# ---------------------------------------------------------------------
# _finish_reason_cut_info：三种协议的截断拼写必须被同等识别
# ---------------------------------------------------------------------
@pytest.mark.parametrize("finish_reason", [
    "length",             # OpenAI Chat Completions
    "max_tokens",         # Anthropic Messages API（stop_reason）
    "max_output_tokens",  # OpenAI Responses API（incomplete_details.reason）
    "MAX_TOKENS",         # Gemini streamGenerateContent（finishReason，大写）
    "Length",             # 大小写不敏感
])
def test_finish_reason_cut_info_recognizes_all_truncation_spellings(finish_reason):
    is_cut, cause = _finish_reason_cut_info(finish_reason)
    assert is_cut is True
    assert "output token limit" in cause


@pytest.mark.parametrize("finish_reason", ["stop", "tool_calls", "end_turn", "tool_use"])
def test_finish_reason_cut_info_normal_stop_is_not_cut(finish_reason):
    is_cut, cause = _finish_reason_cut_info(finish_reason)
    assert is_cut is False
    assert cause == ""


def test_finish_reason_cut_info_empty_string_is_dropped_stream_evidence():
    """空字符串 = 流被完整消费但从未见到终止事件，视为断流证据。"""
    is_cut, cause = _finish_reason_cut_info("")
    assert is_cut is True
    assert "without a finish_reason" in cause


def test_finish_reason_cut_info_content_filter():
    is_cut, cause = _finish_reason_cut_info("content_filter")
    assert is_cut is True
    assert "content filter" in cause


def test_finish_reason_cut_info_none_means_no_information():
    """None = 调用方没有该信息（旧行为），不应下"被截断"的结论。"""
    is_cut, cause = _finish_reason_cut_info(None)
    assert is_cut is False
    assert cause == ""


# ---------------------------------------------------------------------
# append_truncation_notice_if_needed：只在"输出长度上限"时追加提示
# ---------------------------------------------------------------------
@pytest.mark.parametrize("finish_reason", [
    "length", "max_tokens", "max_output_tokens", "MAX_TOKENS",
])
def test_append_truncation_notice_triggers_on_length_limit(finish_reason):
    builder = _FakeBuilder()
    result = append_truncation_notice_if_needed(builder, "部分回答", finish_reason)
    assert result == "部分回答" + _TRUNCATION_NOTICE
    assert builder.added == [_TRUNCATION_NOTICE]


@pytest.mark.parametrize("finish_reason", ["stop", "tool_calls", "end_turn", None])
def test_append_truncation_notice_silent_on_normal_finish(finish_reason):
    builder = _FakeBuilder()
    result = append_truncation_notice_if_needed(builder, "完整回答。", finish_reason)
    assert result == "完整回答。"
    assert builder.added == []


def test_append_truncation_notice_silent_on_content_filter():
    """content_filter 走既有空响应兜底更合适，不叠加"输出被截断"的误导提示。"""
    builder = _FakeBuilder()
    result = append_truncation_notice_if_needed(builder, "某些内容", "content_filter")
    assert result == "某些内容"
    assert builder.added == []


def test_append_truncation_notice_silent_on_dropped_stream():
    """空字符串是连接层断流证据，不是"内容太长被截断"，不应追加提示。"""
    builder = _FakeBuilder()
    result = append_truncation_notice_if_needed(builder, "某些内容", "")
    assert result == "某些内容"
    assert builder.added == []


def test_append_truncation_notice_never_fires_on_empty_content():
    """本就没有正文（空响应兜底场景）不应叠加截断提示。"""
    builder = _FakeBuilder()
    result = append_truncation_notice_if_needed(builder, "", "length")
    assert result == ""
    assert builder.added == []
