"""回合边界草稿滚动的独立回归测试。"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import apitelegramchat.ai_handlers as handlers  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_oversized_blocks() -> str:
    return (
        "<p>甲" + "a" * 12000 + "</p>"
        "<details><summary>乙</summary><p>" + "b" * 12000 + "</p></details>"
        "<p>丙" + "c" * 10000 + "</p>"
    )


def test_visible_count_and_top_level_blocks() -> None:
    source = (
        "<table><tr><th>标题</th></tr><tr><td>A&amp;B</td></tr></table>"
        "<p>后续文字</p>"
    )
    boundaries, chars, blocks = handlers._scan_rich_html_boundaries(source)
    require(chars == len("标题A&B后续文字"), "实体应按解析后可见字符计数")
    require(blocks == 4, "表格行与段落应纳入嵌套 Rich Block 预算，单元格不单独计数")
    require(source[:boundaries[0][0]].endswith("</table>"), "首个边界必须位于完整表格结束处")


def test_inline_content_is_wrapped_as_rich_block() -> None:
    require(
        handlers._ensure_rich_block_content("仅有普通文本") == "<p>仅有普通文本</p>",
        "裸文本必须包装为段落，避免 Rich Message 拒绝",
    )
    require(
        handlers._ensure_rich_block_content("<i>仅有内联样式</i>") == "<p><i>仅有内联样式</i></p>",
        "仅有内联标签时也必须补齐块级容器",
    )
    table = "<table><tr><td>结构化内容</td></tr></table>"
    require(
        handlers._ensure_rich_block_content(table) == table,
        "已有 Rich 块的内容不得被重复包装",
    )

    builder = handlers.RichMessageBuilder(chat_id=88)
    builder.blocks = ["<tg-thinking>Thinking...</tg-thinking>", "最终回复", "<i>补充说明</i>"]
    builder.block_types = ["html", "text", "text"]
    final_html = builder._build_html_no_thinking()
    require("最终回复" in final_html and "<i>补充说明</i>" in final_html, "顶层文本必须保持原有流式输出格式")

    tool_detail = builder._get_inner_content({
        "summary": "检查文件",
        "details_html": "<i>已完成</i>",
    })
    require(
        "<p><i>已完成</i></p>" in tool_detail,
        "工具详情的内联内容必须成为 details 内的有效 Rich 块",
    )


def test_capacity_warning_is_not_an_immediate_rollover() -> None:
    builder = handlers.RichMessageBuilder(chat_id=1)
    builder.blocks = [make_oversized_blocks()]
    builder.block_types = ["text"]
    old_draft_id = builder.draft_id

    armed = builder._arm_rollover_if_needed(builder._build_html_no_thinking())

    require(armed, "接近容量时必须进入待滚动状态")
    require(builder._rollover_pending, "容量预警必须只置位 pending")
    require(builder.draft_id == old_draft_id, "预警阶段不得分配新 draft_id")
    require(not builder._rollover_in_progress, "预警阶段不得启动后台滚动")


async def test_rollover_occurs_only_at_turn_boundary() -> None:
    permanent_segments = []
    draft_frames = []

    async def fake_permanent(chat_id, html_content, **kwargs):
        permanent_segments.append((chat_id, html_content, kwargs))
        return 4242

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        draft_frames.append((draft_id, html_content, kwargs))
        return 5252

    original_permanent = handlers.send_rich_html_message
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    original_delete_fast = handlers.delete_message_fast
    handlers.send_rich_html_message = fake_permanent
    handlers.send_rich_message_draft = fake_draft
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message_fast = AsyncMock(return_value=True)
    try:
        builder = handlers.RichMessageBuilder(chat_id=99)
        old_draft_id = builder.draft_id
        builder.draft_message_id = 777
        builder._register_active_draft = AsyncMock()
        builder.blocks = [make_oversized_blocks()]
        builder.block_types = ["text"]

        await builder.flush()
        require(builder._rollover_pending, "flush 只能设置容量预警")
        require(builder.draft_id == old_draft_id, "flush 不得在本轮中切换草稿")
        require(not permanent_segments, "没有回合边界时不得永久化")

        rolled = await builder.rollover_at_turn_boundary()
        require(rolled, "完整回合边界必须执行滚动")
        require(len(permanent_segments) == 1, "一个边界只能永久化一个旧段")
        submitted = permanent_segments[0][1]
        require(submitted.endswith("</details>"), "永久化内容必须结束于完整 details 块")
        require("丙" not in submitted, "边界后的尾部不得提前永久化")
        require(builder.draft_id != old_draft_id, "边界滚动后必须生成新的 draft_id")
        require(not builder._rollover_pending, "完成切换后必须清除 pending")
        require(builder._rollover_count == 1 and len(builder._rollover_history) == 1, "必须记录滚动历史")
        require("丙" in builder._build_html(), "新草稿必须携带未提交尾部")
        require("Thinking..." in builder._build_html(), "新草稿首帧必须保留 Thinking 状态")
        require(draft_frames[-1][0] == builder.draft_id, "新首帧必须使用新的 draft_id")
        require(draft_frames[-1][2].get("force") is True, "新草稿首帧必须强制发送")
        handlers.mark_draft_dead.assert_awaited_once_with(old_draft_id)
        builder._register_active_draft.assert_any_await(0)
        builder._register_active_draft.assert_any_await(5252)
        await asyncio.sleep(0)
        # 预警阶段的 flush 会更新旧草稿的 preview message_id；异步清理应删除该最新预览。
        handlers.delete_message_fast.assert_awaited_once_with(99, 5252)
    finally:
        handlers.send_rich_html_message = original_permanent
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead
        handlers.delete_message_fast = original_delete_fast


async def test_handoff_delta_is_preserved() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    permanent_segments = []

    async def slow_permanent(chat_id, html_content, **kwargs):
        permanent_segments.append(html_content)
        started.set()
        await release.wait()
        return 6161

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        return 7171

    original_permanent = handlers.send_rich_html_message
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    original_delete_fast = handlers.delete_message_fast
    handlers.send_rich_html_message = slow_permanent
    handlers.send_rich_message_draft = fake_draft
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message_fast = AsyncMock(return_value=True)
    try:
        builder = handlers.RichMessageBuilder(chat_id=100)
        builder._register_active_draft = AsyncMock()
        builder.blocks = [make_oversized_blocks()]
        builder.block_types = ["text"]
        builder._arm_rollover_if_needed(builder._build_html_no_thinking())

        rollover_task = asyncio.create_task(builder.rollover_at_turn_boundary())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        builder.add_text("<p>LATE_DELTA_DURING_HANDOFF</p>")
        release.set()
        require(await asyncio.wait_for(rollover_task, timeout=1.0), "滚动必须完成")

        resulting_html = builder._build_html()
        require(
            "LATE_DELTA_DURING_HANDOFF" in resulting_html,
            "永久化等待期间新增的 delta 必须进入新草稿，不能丢失",
        )
        require(
            "LATE_DELTA_DURING_HANDOFF" not in "".join(permanent_segments),
            "交接期间的新 delta 不得倒灌进已冻结的永久段",
        )
    finally:
        handlers.send_rich_html_message = original_permanent
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead
        handlers.delete_message_fast = original_delete_fast


async def test_failed_permanent_send_restores_handoff_to_old_draft() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def failing_permanent(chat_id, html_content, **kwargs):
        started.set()
        await release.wait()
        return False

    original_permanent = handlers.send_rich_html_message
    original_dead = handlers.mark_draft_dead
    handlers.send_rich_html_message = failing_permanent
    handlers.mark_draft_dead = AsyncMock()
    try:
        builder = handlers.RichMessageBuilder(chat_id=101)
        old_draft_id = builder.draft_id
        builder.blocks = [make_oversized_blocks()]
        builder.block_types = ["text"]
        builder._arm_rollover_if_needed(builder._build_html_no_thinking())

        rollover_task = asyncio.create_task(builder.rollover_at_turn_boundary())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        builder.add_text("<p>PRESERVE_ON_FAILURE</p>")
        release.set()
        require(not await asyncio.wait_for(rollover_task, timeout=1.0), "永久化失败时不得伪造切换成功")

        require(builder.draft_id == old_draft_id, "永久化失败时不得切换 draft_id")
        require(builder._rollover_pending, "永久化失败后必须保留 pending 以供下轮重试")
        require("PRESERVE_ON_FAILURE" in builder._build_html(), "失败交接期间的 delta 必须恢复到旧草稿")
        handlers.mark_draft_dead.assert_not_awaited()
    finally:
        handlers.send_rich_html_message = original_permanent
        handlers.mark_draft_dead = original_dead


async def test_old_preview_cleanup_does_not_delay_new_draft() -> None:
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()

    async def fake_permanent(chat_id, html_content, **kwargs):
        return 8181

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        return 9191

    async def slow_delete(chat_id, message_id):
        delete_started.set()
        await release_delete.wait()
        return True

    original_permanent = handlers.send_rich_html_message
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    original_delete_fast = handlers.delete_message_fast
    handlers.send_rich_html_message = fake_permanent
    handlers.send_rich_message_draft = fake_draft
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message_fast = slow_delete
    try:
        builder = handlers.RichMessageBuilder(chat_id=102)
        old_draft_id = builder.draft_id
        builder.draft_message_id = 888
        builder._register_active_draft = AsyncMock()
        builder.blocks = [make_oversized_blocks()]
        builder.block_types = ["text"]
        builder._arm_rollover_if_needed(builder._build_html_no_thinking())

        require(await builder.rollover_at_turn_boundary(), "滚动必须完成")
        require(builder.draft_id != old_draft_id, "慢删除前必须已经切到新草稿")
        await asyncio.wait_for(delete_started.wait(), timeout=1.0)
        require(builder.draft_message_id == 9191, "新草稿首帧不得等待旧预览删除")
        release_delete.set()
        await asyncio.sleep(0)
    finally:
        handlers.send_rich_html_message = original_permanent
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead
        handlers.delete_message_fast = original_delete_fast


async def test_turn_boundary_rechecks_capacity_after_rate_limited_flush() -> None:
    permanent_segments = []
    draft_frames = []

    async def fake_permanent(chat_id, html_content, **kwargs):
        permanent_segments.append((chat_id, html_content, kwargs))
        return 7373

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        draft_frames.append((draft_id, html_content, kwargs))
        return 7474

    original_permanent = handlers.send_rich_html_message
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    try:
        handlers.send_rich_html_message = fake_permanent
        handlers.send_rich_message_draft = fake_draft
        handlers.mark_draft_dead = AsyncMock()

        builder = handlers.RichMessageBuilder(chat_id=104)
        old_draft_id = builder.draft_id
        builder._register_active_draft = AsyncMock()
        builder.blocks = ["<p>" + ("甲" * handlers.RICH_DRAFT_ROLLOVER_TEXT_CHARS) + "</p>"]
        builder.block_types = ["text"]

        # 模拟 429 冷却：flush 会提前返回，所以此前实现不会设置 pending。
        builder._rate_limited_until = time.monotonic() + 60
        await builder.flush()
        require(not builder._rollover_pending, "冷却期内的 flush 不应伪造 pending")

        # 修复后，完整工具回合边界自行重新统计 30k 容量并立即切换。
        require(await builder.rollover_at_turn_boundary(), "边界必须独立于 flush/pending 完成滚动")
        require(builder.draft_id != old_draft_id, "达到正式阈值后必须换用新草稿")
        require(len(permanent_segments) == 1, "必须永久化一个旧草稿段")
        require(
            len(handlers._rich_visible_text(permanent_segments[0][1])) == handlers.RICH_DRAFT_ROLLOVER_TEXT_CHARS,
            "30k 的完整段必须在本回合边界立即提交",
        )
        # 新 draft_id 有独立的节流状态；切换后必须立即发送新草稿首帧，不能继续被旧草稿
        # 的冷却窗口拖住，否则用户仍会看到“后端运行但草稿不刷新”。
        require(draft_frames and draft_frames[-1][0] == builder.draft_id, "新草稿首帧必须立即使用新 draft_id 发送")
    finally:
        handlers.send_rich_html_message = original_permanent
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead


async def test_arm_threshold_rolls_at_next_turn_boundary() -> None:
    permanent_segments = []
    draft_frames = []

    async def fake_permanent(chat_id, html_content, **kwargs):
        permanent_segments.append((chat_id, html_content, kwargs))
        return 7574

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        draft_frames.append((draft_id, html_content, kwargs))
        return 7575

    original_permanent = handlers.send_rich_html_message
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    try:
        handlers.send_rich_html_message = fake_permanent
        handlers.send_rich_message_draft = fake_draft
        handlers.mark_draft_dead = AsyncMock()
        builder = handlers.RichMessageBuilder(chat_id=105)
        old_draft_id = builder.draft_id
        builder._register_active_draft = AsyncMock()
        builder.blocks = ["<p>" + ("乙" * handlers.RICH_DRAFT_ARM_TEXT_CHARS) + "</p>"]
        builder.block_types = ["text"]

        await builder.flush()
        require(builder._rollover_pending, "27k 应置位预警")
        require(
            await builder.rollover_at_turn_boundary(),
            "27k 预警后，下一完整工具回合边界必须立即换草稿",
        )
        require(builder.draft_id != old_draft_id, "不得继续复用旧草稿")
        require(builder._rollover_count == 1 and len(permanent_segments) == 1, "必须永久化旧段一次")
        require(
            len(handlers._rich_visible_text(permanent_segments[0][1])) == handlers.RICH_DRAFT_ARM_TEXT_CHARS,
            "27k 阈值内容必须在下一回合前提交",
        )
        require(draft_frames and draft_frames[-1][0] == builder.draft_id, "新草稿首帧必须立即发送")
    finally:
        handlers.send_rich_html_message = original_permanent
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead


async def test_inflight_flush_replays_latest_draft_frame() -> None:
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    sent_frames = []

    async def slow_draft(chat_id, draft_id, html_content, **kwargs):
        sent_frames.append(html_content)
        if len(sent_frames) == 1:
            first_send_started.set()
            await release_first_send.wait()
        return 7777

    original_draft = handlers.send_rich_message_draft
    try:
        handlers.send_rich_message_draft = slow_draft
        builder = handlers.RichMessageBuilder(chat_id=107)
        builder._register_active_draft = AsyncMock()

        builder.add_text("<p>FIRST_FRAME</p>")
        await asyncio.wait_for(first_send_started.wait(), timeout=1.0)

        # 第一次网络发送仍在锁内时，第二次状态变更不能被 request_flush 合并后丢失。
        builder.add_text("<p>LATEST_FRAME</p>")
        release_first_send.set()

        for _ in range(100):
            if len(sent_frames) >= 2:
                break
            await asyncio.sleep(0.01)
        require(len(sent_frames) >= 2, "在途发送期间的新状态必须触发补发")
        require("LATEST_FRAME" in sent_frames[-1], "补发帧必须包含发送期间新增的最新内容")
        require(not builder._flush_dirty, "补发完成后不应遗留未处理的脏状态")
    finally:
        handlers.send_rich_message_draft = original_draft


async def test_done_only_batch_finishes_precreated_tool_group() -> None:
    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        return 7676

    original_draft = handlers.send_rich_message_draft
    try:
        handlers.send_rich_message_draft = fake_draft
        builder = handlers.RichMessageBuilder(chat_id=106)
        builder._register_active_draft = AsyncMock()
        # 真实流式阶段会先创建该条目，然后 _run_tool_calls_and_append 才会看到 done-only 批次。
        builder.add_tool_item("done-1", "done", "Done")
        require(not builder._tool_groups[-1]["finished"], "前置条件：工具组尚未收束")

        status = await handlers._run_tool_calls_and_append(
            tool_calls=[{
                "id": "done-1",
                "type": "function",
                "function": {"name": "done", "arguments": "{}"},
            }],
            loop_messages=[],
            new_history_entries=[],
            tool_call_count_ref=[0],
            api_label="test",
            builder=builder,
            chat_id=106,
        )
        require(status == "continue", "done-only 批次应正常继续")
        require(builder._tool_groups[-1]["finished"], "done-only 批次必须收束已有工具组")
    finally:
        handlers.send_rich_message_draft = original_draft


def test_no_legacy_background_rollover_fields() -> None:
    builder = handlers.RichMessageBuilder(chat_id=103)
    require(not hasattr(builder, "_rollover_task"), "不得残留后台 rollover task")
    require(not hasattr(builder, "_rollover_allowed"), "不得残留旧 rollover 许可标志")
    require(not hasattr(builder, "_maybe_start_rollover"), "不得残留 flush 内后台滚动入口")


def main() -> None:
    test_visible_count_and_top_level_blocks()
    test_inline_content_is_wrapped_as_rich_block()
    test_capacity_warning_is_not_an_immediate_rollover()
    asyncio.run(test_rollover_occurs_only_at_turn_boundary())
    asyncio.run(test_handoff_delta_is_preserved())
    asyncio.run(test_failed_permanent_send_restores_handoff_to_old_draft())
    asyncio.run(test_old_preview_cleanup_does_not_delay_new_draft())
    asyncio.run(test_turn_boundary_rechecks_capacity_after_rate_limited_flush())
    asyncio.run(test_arm_threshold_rolls_at_next_turn_boundary())
    asyncio.run(test_inflight_flush_replays_latest_draft_frame())
    asyncio.run(test_done_only_batch_finishes_precreated_tool_group())
    test_no_legacy_background_rollover_fields()
    print("turn-boundary draft rollover validation: PASS")


if __name__ == "__main__":
    main()
