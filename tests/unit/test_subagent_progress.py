"""ai/subagent_progress.py 的单测。

这段正则解析逻辑此前内嵌在 tool_call_loop.py 里、完全没有测试覆盖——
子 agent 一旦改了状态文案的措辞（如"第 X/Y 轮"变成别的格式），解析会
静默退化成"unknown"阶段兜底展示，不会报错，只会让用户看到的进度卡片
变得不完整。这里把几种真实会出现的 status_text 样式钉住，防止之后
改动正则时无声破坏其中某一种。
"""
from ai.subagent_progress import (
    subagent_progress_phase,
    format_subagent_progress_html,
)


def test_phase_start():
    assert subagent_progress_phase("启动子 agent：模型 gpt-5，任务：抓取网页") == "start"


def test_phase_thinking():
    assert subagent_progress_phase("第 2/5 轮：LLM 思考中…（已耗时 3.2s）") == "thinking"


def test_phase_tools():
    assert subagent_progress_phase("第 2/5 轮：执行工具 fetch_url + str_replace…（已耗时 4.1s）") == "tools"


def test_phase_done_beats_tools_pattern():
    # "完成" 开头的行本身也可能包含"次工具调用"字样，必须优先匹配 done。
    assert subagent_progress_phase("完成：5 轮，3 次工具调用，12.4s") == "done"


def test_phase_timeout():
    assert subagent_progress_phase("整体超时，已终止子 agent") == "timeout"


def test_phase_error():
    assert subagent_progress_phase("解析失败：返回内容不是合法 JSON") == "error"


def test_phase_empty_text_is_unknown():
    assert subagent_progress_phase("") == "unknown"
    assert subagent_progress_phase(None) == "unknown"


def test_format_thinking_extracts_round_and_elapsed():
    html = format_subagent_progress_html("第 2/5 轮：LLM 思考中…（已耗时 3.2s）")
    assert "LLM 思考中" in html
    assert "2" in html and "5" in html
    assert "3.2" in html


def test_format_tools_extracts_tool_names():
    html = format_subagent_progress_html("第 2/5 轮：执行工具 fetch_url + str_replace…（已耗时 4.1s）")
    assert "fetch_url" in html
    assert "str_replace" in html


def test_format_tool_names_collapse_when_many():
    names = " + ".join(f"tool_{i}" for i in range(8))
    html = format_subagent_progress_html(f"第 1/3 轮：执行工具 {names}…（已耗时 1.0s）")
    # 超过 6 个工具名时应折叠为"等 N 个"，而不是把全部 8 个原样堆出来。
    assert "等 8 个" in html
    assert "tool_7" not in html  # 第 7、8 个已被折叠，不应逐个出现


def test_format_done_shows_total_rounds_and_calls():
    html = format_subagent_progress_html("完成：5 轮，3 次工具调用，12.4s")
    assert "完成" in html
    assert "5" in html and "3" in html and "12.4" in html


def test_format_unstructured_text_falls_back_to_raw_preview():
    # 完全不匹配已知模式的文本：不应抛异常，应原样截断展示。
    html = format_subagent_progress_html("一些无法识别的自由文本状态")
    assert "一些无法识别的自由文本状态" in html


def test_format_escapes_model_controlled_text():
    # 工具名/模型名等字段最终来自子 agent（间接受模型输出影响），必须经
    # HTML 转义，不能让 "<script>" 之类内容破坏 Rich Message 结构。
    html = format_subagent_progress_html(
        "第 1/2 轮：执行工具 <script>alert(1)</script>…（已耗时 1.0s）"
    )
    assert "<script>" not in html
