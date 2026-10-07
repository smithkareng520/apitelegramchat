'''输出截断的分类与用户提示'''


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
