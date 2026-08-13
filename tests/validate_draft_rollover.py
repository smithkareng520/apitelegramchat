"""回合边界草稿滚动的独立回归测试。"""
import asyncio
import os
import sys
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


def test_no_legacy_background_rollover_fields() -> None:
    builder = handlers.RichMessageBuilder(chat_id=103)
    require(not hasattr(builder, "_rollover_task"), "不得残留后台 rollover task")
    require(not hasattr(builder, "_rollover_allowed"), "不得残留旧 rollover 许可标志")
    require(not hasattr(builder, "_maybe_start_rollover"), "不得残留 flush 内后台滚动入口")


def main() -> None:
    test_visible_count_and_top_level_blocks()
    test_capacity_warning_is_not_an_immediate_rollover()
    asyncio.run(test_rollover_occurs_only_at_turn_boundary())
    asyncio.run(test_handoff_delta_is_preserved())
    asyncio.run(test_failed_permanent_send_restores_handoff_to_old_draft())
    asyncio.run(test_old_preview_cleanup_does_not_delay_new_draft())
    test_no_legacy_background_rollover_fields()
    print("turn-boundary draft rollover validation: PASS")


if __name__ == "__main__":
    main()
