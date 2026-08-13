#!/usr/bin/env python3
"""Telegram Rich Message 草稿滚动的独立回归测试。"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import apitelegramchat.ai_handlers as handlers  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_visible_count_and_top_level_blocks() -> None:
    source = (
        "<table><tr><th>标题</th></tr><tr><td>A&amp;B</td></tr></table>"
        "<p>后续文字</p>"
    )
    boundaries, chars, blocks = handlers._scan_rich_html_boundaries(source)
    require(chars == len("标题A&B后续文字"), "实体应按解析后可见字符计数")
    require(blocks == 4, "表格行与段落应纳入嵌套 Rich Block 预算，单元格不单独计数")
    require(source[:boundaries[0][0]].endswith("</table>"), "首个边界必须位于完整表格结束处")


def test_prefers_complete_structural_boundary() -> None:
    parts = [
        "<p>甲" + "a" * 11000 + "</p>",
        "<details><summary>折叠</summary><p>乙" + "b" * 11000 + "</p></details>",
        "<table><tr><th>列</th></tr><tr><td>丙" + "c" * 11000 + "</td></tr></table>",
    ]
    source = "".join(parts)
    builder = handlers.RichMessageBuilder(chat_id=1)
    cut_at, chars, blocks = builder._pick_rollover_boundary(source)
    require(chars > handlers.RICH_DRAFT_ROLLOVER_TEXT_CHARS, "测试输入必须触发滚动阈值")
    require(cut_at is not None, "应找到安全边界")
    prefix = source[:cut_at]
    require(prefix.endswith("</details>"), "应选择表格前的完整折叠块结束点")
    require("<table>" not in prefix, "不得截断或半提交表格")
    require(blocks == 6, "应正确统计段落、details 内段落和表格行等嵌套结构块")


def test_character_and_block_budget_boundaries() -> None:
    builder = handlers.RichMessageBuilder(chat_id=2)
    many_blocks = "".join(f"<p>{index}</p>" for index in range(450))
    cut_at, _chars, blocks = builder._pick_rollover_boundary(many_blocks)
    require(blocks == 450, "完整输入应统计 450 个段落块")
    require(cut_at is not None, "块数接近限制时应找到完整段落边界")
    require(many_blocks[:cut_at].count("</p>") == handlers.RICH_DRAFT_ROLLOVER_BLOCKS, "应在第 440 个完整块后滚动")

    oversized_but_complete = "<details><summary>说明</summary><p>" + "x" * 30050 + "</p></details>"
    cut_at, chars, _blocks = builder._pick_rollover_boundary(oversized_but_complete)
    require(chars > handlers.RICH_DRAFT_ROLLOVER_TEXT_CHARS, "测试块应略超过主动字符阈值")
    require(cut_at == len(oversized_but_complete), "完整块略超主动阈值时应在闭合处安全提交")


async def test_rollover_tracks_drafts_and_keeps_remainder() -> None:
    completed_segments = []

    async def fake_send(chat_id, html_content, **kwargs):
        completed_segments.append((chat_id, html_content, kwargs))
        return 4242

    original_send = handlers.send_rich_html_message
    original_dead = handlers.mark_draft_dead
    original_delete_fast = handlers.delete_message_fast
    handlers.send_rich_html_message = fake_send
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message_fast = AsyncMock(return_value=True)
    try:
        builder = handlers.RichMessageBuilder(chat_id=99)
        old_draft_id = builder.draft_id
        builder.draft_message_id = 777
        builder._register_active_draft = AsyncMock()
        builder.blocks = [
            "<p>一" + "a" * 12000 + "</p>"
            "<details><summary>二</summary><p>" + "b" * 12000 + "</p></details>"
            "<p>三" + "c" * 10000 + "</p>"
        ]
        builder.block_types = ["text"]
        rolled = await builder._rollover_draft_if_needed(builder._build_html())
        require(rolled, "超过阈值时必须发生滚动")
        require(len(completed_segments) == 1, "一个滚动周期只能永久化一次")
        submitted = completed_segments[0][1]
        require(submitted.endswith("</details>"), "永久化内容必须结束于完整 details 块")
        require("三" not in submitted, "边界后的尾部内容不得被提前永久化")
        require(builder.draft_id != old_draft_id, "滚动后必须生成新的 draft_id")
        require(builder._rollover_count == 1 and len(builder._rollover_history) == 1, "必须记录滚动历史")
        new_draft_html = builder._build_html()
        require("三" in new_draft_html, "新草稿必须携带未提交的尾部内容")
        require("正在继续生成" in new_draft_html, "新草稿首帧必须显示继续生成状态")
        handlers.mark_draft_dead.assert_awaited_once_with(old_draft_id)
        await asyncio.sleep(0)
        handlers.delete_message_fast.assert_awaited_once_with(99, 777)
        builder._register_active_draft.assert_awaited_once_with(0)
    finally:
        handlers.send_rich_html_message = original_send
        handlers.mark_draft_dead = original_dead
        handlers.delete_message_fast = original_delete_fast


async def test_flush_restarts_preview_with_new_draft_id() -> None:
    permanent_segments = []
    draft_frames = []

    async def fake_permanent(chat_id, html_content, **kwargs):
        permanent_segments.append(html_content)
        return 6161

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        draft_frames.append((draft_id, html_content, kwargs))
        return 7171

    original_permanent = handlers.send_rich_html_message
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    original_delete = handlers.delete_message
    handlers.send_rich_html_message = fake_permanent
    handlers.send_rich_message_draft = fake_draft
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message = AsyncMock()
    try:
        builder = handlers.RichMessageBuilder(chat_id=101)
        old_draft_id = builder.draft_id
        builder._register_active_draft = AsyncMock()
        builder.blocks = [
            "<p>甲" + "a" * 12000 + "</p>"
            "<details><summary>乙</summary><p>" + "b" * 12000 + "</p></details>"
            "<p>丙" + "c" * 10000 + "</p>"
        ]
        builder.block_types = ["text"]
        await builder.flush()
        require(len(permanent_segments) == 1, "flush 必须先永久化完整旧段")
        require(len(draft_frames) == 1, "flush 必须立即发送新草稿首帧")
        new_draft_id, tail_html, kwargs = draft_frames[0]
        require(new_draft_id != old_draft_id, "尾部必须使用新 draft_id 续写")
        require("丙" in tail_html and "甲" not in tail_html, "新草稿首帧只能包含未提交尾部")
        require("正在继续生成" in tail_html, "滚动后的新草稿首帧必须包含 Thinking 状态")
        require(kwargs.get("force") is True, "滚动后的新草稿首帧必须强制发送")
        require(builder._register_active_draft.await_count == 2, "需先登记新草稿占位，再登记首帧 message_id")
        builder._register_active_draft.assert_any_await(0)
        builder._register_active_draft.assert_any_await(7171)
    finally:
        handlers.send_rich_html_message = original_permanent
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead
        handlers.delete_message = original_delete


async def test_oversized_single_block_falls_back_without_loss() -> None:
    completed_segments = []

    async def fake_send(chat_id, html_content, **kwargs):
        completed_segments.append(html_content)
        return 5252

    original_send = handlers.send_rich_html_message
    original_dead = handlers.mark_draft_dead
    original_delete = handlers.delete_message
    handlers.send_rich_html_message = fake_send
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message = AsyncMock()
    try:
        payload = "X" * (handlers.RICH_DRAFT_HARD_GUARD_CHARS + 500)
        builder = handlers.RichMessageBuilder(chat_id=100)
        builder._register_active_draft = AsyncMock()
        builder.blocks = [f"<table><tr><td>{payload}</td></tr></table>"]
        builder.block_types = ["text"]
        rolled = await builder._rollover_draft_if_needed(builder._build_html())
        require(rolled, "接近真实上限的未闭合单块必须触发兜底滚动")
        require(builder._rollover_history[-1]["mode"] == "plain_text_fallback", "超长单块应标记为降级模式")
        submitted_text = handlers._rich_visible_text(completed_segments[0])
        remainder_text = handlers._rich_visible_text(builder._build_html())
        require("正在继续生成" in remainder_text, "fallback 后的新草稿也必须显示 Thinking 状态")
        remainder_payload = remainder_text.replace("正在继续生成…", "", 1)
        require(submitted_text + remainder_payload == payload, "降级分段必须保持全部可见文本，不得丢失")
        require(len(submitted_text) <= handlers.RICH_DRAFT_ROLLOVER_TEXT_CHARS, "兜底永久段仍须低于主动阈值")
    finally:
        handlers.send_rich_html_message = original_send
        handlers.mark_draft_dead = original_dead
        handlers.delete_message = original_delete


def main() -> None:
    test_visible_count_and_top_level_blocks()
    test_prefers_complete_structural_boundary()
    test_character_and_block_budget_boundaries()
    asyncio.run(test_rollover_tracks_drafts_and_keeps_remainder())
    asyncio.run(test_flush_restarts_preview_with_new_draft_id())
    asyncio.run(test_oversized_single_block_falls_back_without_loss())
    print("draft rollover validation: PASS")


if __name__ == "__main__":
    main()
