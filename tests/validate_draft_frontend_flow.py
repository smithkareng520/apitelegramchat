#!/usr/bin/env python3
"""Telegram Rich Message 草稿前端响应与生命周期的独立回归测试。"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import apitelegramchat.ai_handlers as handlers  # noqa: E402
import apitelegramchat.subagent_tool as subagent_tool  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_thinking_status_is_escaped_and_requests_refresh() -> None:
    builder = handlers.RichMessageBuilder(chat_id=801)
    refresh_requests: list[bool] = []
    builder.request_flush = lambda force=False: refresh_requests.append(force)
    builder.add_initial_thinking("Thinking...")

    changed = builder.set_thinking_status("Thinking <attachment> & context...")

    require(changed, "应能更新尚未移除的思考占位")
    require(
        builder.blocks[0] == "<tg-thinking>Thinking &lt;attachment&gt; &amp; context...</tg-thinking>",
        "思考状态必须按富消息 HTML 规则转义",
    )
    require(refresh_requests == [True], "状态变更后必须触发一次强制可见刷新请求")


def test_long_subagent_card_preserves_rich_structure() -> None:
    """长子 agent 答复必须保留卡片、引用块和已展示部分的来源链接。"""
    answer = (
        '<p>结论：详见 <a href="https://example.com/source">来源</a>。</p>'
        + "<p>" + ("长内容 " * 2500) + "</p>"
    )
    card = subagent_tool.render_subagent_card({
        "ok": True,
        "model": "test-model",
        "rounds": 2,
        "tool_calls": 1,
        "elapsed": 1.2,
        "task_preview": "验证长卡片",
        "answer": answer,
    })

    require("答复较长，卡片仅展示前半部分" in card, "长答复必须明确提示卡片预览已截断")
    require('<a href="https://example.com/source">来源</a>' in card, "截断前的来源链接必须保留")
    require(card.count("<blockquote>") == card.count("</blockquote>"), "卡片中的引用块必须完整闭合")
    require("<p>任务：</p><blockquote>验证长卡片</blockquote>" in card, "任务引用块不得嵌套在段落内")

    builder = handlers.RichMessageBuilder(chat_id=804)
    builder.request_flush = lambda force=False: None
    builder.start_new_tool_group()
    builder.add_tool_item("subagent-1", "subagent", "子 agent 完成", fn_args={})
    builder.update_tool_item("subagent-1", "子 agent 完成", card)
    builder.finish_group()
    rendered = builder._build_html()

    require("子 agent 已完成" in rendered, "长答复不得导致子 agent 卡片消失")
    require('<a href="https://example.com/source">来源</a>' in rendered, "专用详情预算不得扁平化来源链接")
    require("答复较长，卡片仅展示前半部分" in rendered, "卡片截断提示必须持续可见")


def test_tool_group_details_are_bounded_for_ui() -> None:
    builder = handlers.RichMessageBuilder(chat_id=802)
    builder.request_flush = lambda force=False: None
    builder.start_new_tool_group()
    for index in range(6):
        tool_id = f"search-{index}"
        builder.add_tool_item(
            tool_id,
            "web_search",
            f"搜索任务 {index}",
            fn_args={"query": f"query-{index}"},
        )
        builder.update_tool_item(
            tool_id,
            f"搜索任务 {index}",
            f"<p>{'x' * 5000}</p>",
        )
    builder.finish_group()

    rendered = builder._build_html()
    visible = handlers._rich_visible_text(rendered)

    require("搜索任务 0" in visible and "搜索任务 5" in visible, "所有工具标题必须保留")
    require("工具输出已截断" in visible, "超出组展示预算的详情必须显式提示截断")
    require(
        len(visible) < handlers.RichMessageBuilder.MAX_TOOL_GROUP_UI_DETAIL_CHARS + 800,
        "草稿工具详情总量必须受组展示预算约束",
    )


async def test_silent_active_draft_is_force_refreshed_globally() -> None:
    """静默保活应由构建器统一发起，而不是由具体工具轮询代码承担。"""
    builder = handlers.RichMessageBuilder(chat_id=803)
    builder._last_flush_time = time.monotonic() - handlers.STREAM_SILENT_FORCE_FLUSH
    flushes: list[bool] = []

    async def fake_flush(force: bool = False) -> None:
        flushes.append(force)
        builder._stop_flush = True

    builder.flush = fake_flush
    await builder._stream_flush_loop()

    require(flushes == [True], "静默超时后必须对当前活跃草稿执行 force flush")


async def test_initial_draft_precedes_expensive_request_preparation() -> None:
    chat_id = 803
    frames: list[str] = []
    events: list[str] = []

    async def fake_draft(_chat_id, _draft_id, html_content, **_kwargs):
        events.append("draft")
        frames.append(html_content)
        return 981

    async def fake_prompt(*_args, **_kwargs):
        events.append("prompt")
        require(bool(frames), "构建系统提示词前必须先让用户看到草稿首帧")
        return "system prompt"

    async def fake_append_history(*_args, **_kwargs):
        return None

    async def fake_resolve(*_args, **_kwargs):
        events.append("resolve")
        return "user input"

    async def fake_call_api(*_args, **_kwargs):
        events.append("model")
        return "最终答复", None, [{"role": "assistant", "content": "最终答复"}]

    async def fake_permanent(*_args, **_kwargs):
        events.append("permanent")
        return 982

    original_draft = handlers.send_rich_message_draft
    original_prompt = handlers.build_system_prompt
    original_append = handlers._append_history_async
    original_resolve = handlers._resolve_multimodal_content
    original_call_api = handlers._call_api
    original_permanent = handlers.send_rich_html_message
    original_dead = handlers.mark_draft_dead
    original_delete = handlers.delete_message
    handlers.send_rich_message_draft = fake_draft
    handlers.build_system_prompt = fake_prompt
    handlers._append_history_async = fake_append_history
    handlers._resolve_multimodal_content = fake_resolve
    handlers._call_api = fake_call_api
    handlers.send_rich_html_message = fake_permanent
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message = AsyncMock()
    try:
        result = await handlers.get_ai_response(
            chat_id,
            {chat_id: handlers.DEFAULT_MODEL},
            {chat_id: {"conversation_history": []}},
            "tester",
            user_message={"text": "hello"},
        )
        require(result[0] == "最终答复", "模拟主流程应返回模型最终内容")
        require(events.index("draft") < events.index("prompt"), "首帧必须早于提示词准备")
        require(events.index("prompt") < events.index("resolve"), "准备态应覆盖多模态输入解析前阶段")
        require("Thinking..." in frames[0], "首帧应展示稳定的 Thinking 状态")
        require(all("正在思考" not in frame for frame in frames), "草稿状态不应回退为中文文案")
    finally:
        handlers.send_rich_message_draft = original_draft
        handlers.build_system_prompt = original_prompt
        handlers._append_history_async = original_append
        handlers._resolve_multimodal_content = original_resolve
        handlers._call_api = original_call_api
        handlers.send_rich_html_message = original_permanent
        handlers.mark_draft_dead = original_dead
        handlers.delete_message = original_delete


def main() -> None:
    test_thinking_status_is_escaped_and_requests_refresh()
    test_long_subagent_card_preserves_rich_structure()
    test_tool_group_details_are_bounded_for_ui()
    asyncio.run(test_silent_active_draft_is_force_refreshed_globally())
    asyncio.run(test_initial_draft_precedes_expensive_request_preparation())
    print("draft frontend flow validation: PASS")


if __name__ == "__main__":
    main()
