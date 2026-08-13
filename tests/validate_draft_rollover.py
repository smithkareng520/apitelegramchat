#!/usr/bin/env python3
"""Telegram Rich Message 草稿滚动的独立回归测试。"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
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
    """
    ★ _rollover_draft_if_needed 现在会在状态切换完成后自己
    `await self.flush(force=True)` 发新草稿首帧（因为调用方 flush() 不再
    等待滚动，不能假设"调用方会在这之后接着发"，见其 docstring）。因此这里
    还需要 mock send_rich_message_draft，并且 _register_active_draft 会被
    调用两次：一次是滚动内部的占位登记（draft_id, 0），一次是
    flush(force=True) 里首帧发出后的真实 message_id 登记。
    """
    completed_segments = []

    async def fake_send(chat_id, html_content, **kwargs):
        completed_segments.append((chat_id, html_content, kwargs))
        return 4242

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        return 5252

    original_send = handlers.send_rich_html_message_unserialized
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    original_delete = handlers.delete_message
    handlers.send_rich_html_message_unserialized = fake_send
    handlers.send_rich_message_draft = fake_draft
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message = AsyncMock()
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
        await asyncio.sleep(0)
        require(len(completed_segments) == 1, "一个滚动周期只能永久化一次")
        submitted = completed_segments[0][1]
        require(submitted.endswith("</details>"), "永久化内容必须结束于完整 details 块")
        require("三" not in submitted, "边界后的尾部内容不得被提前永久化")
        require(builder.draft_id != old_draft_id, "滚动后必须生成新的 draft_id")
        require(builder._rollover_count == 1 and len(builder._rollover_history) == 1, "必须记录滚动历史")
        require("三" in builder._build_html(), "新草稿必须携带未提交的尾部内容")
        require("Thinking..." in builder._build_html(), "新草稿首帧必须保留 Thinking 状态")
        handlers.mark_draft_dead.assert_awaited_once_with(old_draft_id)
        handlers.delete_message.assert_awaited_once_with(99, 777)
        require(builder._register_active_draft.await_count == 2, "需先登记新草稿占位，再登记 flush 首帧的真实 message_id")
        builder._register_active_draft.assert_any_await(0)
        builder._register_active_draft.assert_any_await(5252)
    finally:
        handlers.send_rich_html_message_unserialized = original_send
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead
        handlers.delete_message = original_delete


async def test_flush_restarts_preview_with_new_draft_id() -> None:
    """
    ★ 行为变更（草稿冻结 bug 修复的一部分）：滚动（含 send_rich_html_message
    这条网络 I/O）现在由 flush() 内部通过 _maybe_start_rollover 触发为独立
    后台任务，flush() 本身不再 await 它完成——这是为了避免一次慢速的永久
    消息发送把 _flush_lock 或整条 flush 调用链卡住，导致草稿在前端冻结
    数分钟（详见 _rollover_draft_if_needed 的 docstring）。
    因此：
      - `await builder.flush()` 返回时，滚动可能还没完成；旧断言"flush 一次
        性完成滚动 + 发新首帧"不再成立，测试改为显式等待
        `builder._rollover_task` 完成。
      - `flush()` 触发滚动的这一次调用，会先用滚动前的旧状态发一帧草稿
        （锁内快照），滚动任务完成后再自己 `flush(force=True)` 发新草稿
        首帧——所以 draft_frames 会有 2 帧，不是旧版的 1 帧；测试改为断言
        "最后一帧"才是滚动后的新草稿首帧。
    """
    permanent_segments = []
    draft_frames = []

    async def fake_permanent(chat_id, html_content, **kwargs):
        permanent_segments.append(html_content)
        return 6161

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        draft_frames.append((draft_id, html_content, kwargs))
        return 7171

    original_permanent = handlers.send_rich_html_message_unserialized
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    original_delete = handlers.delete_message
    handlers.send_rich_html_message_unserialized = fake_permanent
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
        # 滚动此时是后台任务，可能仍在进行；等它跑完再断言最终状态。
        if builder._rollover_task is not None:
            await builder._rollover_task
        await asyncio.sleep(0)
        require(len(permanent_segments) == 1, "旧段应在后台永久化")
        require(len(draft_frames) >= 1, "flush 必须至少发送一帧草稿")
        new_draft_id, tail_html, kwargs = draft_frames[-1]
        require(new_draft_id != old_draft_id, "最终帧必须使用新 draft_id 续写")
        require("丙" in tail_html and "甲" not in tail_html, "新草稿首帧只能包含未提交尾部")
        require("Thinking..." in tail_html, "滚动后的首帧必须带 Thinking 状态")
        require(kwargs.get("force") is True, "滚动后的新草稿首帧必须强制发送")
        builder._register_active_draft.assert_any_await(0)
        builder._register_active_draft.assert_any_await(7171)
        require(builder._register_active_draft.await_count >= 2, "需登记新草稿占位并登记新首帧 message_id")
    finally:
        handlers.send_rich_html_message_unserialized = original_permanent
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead
        handlers.delete_message = original_delete


async def test_rollover_switches_draft_before_slow_permanent_send() -> None:
    """永久消息慢 0.5s 时，新 draft 仍应在该等待之前建立并发送首帧。"""
    started = asyncio.Event()
    release = asyncio.Event()
    draft_frames = []

    async def slow_permanent(chat_id, html_content, **kwargs):
        started.set()
        await release.wait()
        return 8282

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        draft_frames.append((draft_id, html_content, kwargs))
        return 9292

    original_permanent = handlers.send_rich_html_message_unserialized
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    original_delete = handlers.delete_message
    handlers.send_rich_html_message_unserialized = slow_permanent
    handlers.send_rich_message_draft = fake_draft
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message = AsyncMock()
    try:
        builder = handlers.RichMessageBuilder(chat_id=103)
        old_draft_id = builder.draft_id
        builder._register_active_draft = AsyncMock()
        builder.blocks = [
            "<p>甲" + "a" * 12000 + "</p>"
            "<details><summary>乙</summary><p>" + "b" * 12000 + "</p></details>"
            "<p>丙" + "c" * 10000 + "</p>"
        ]
        builder.block_types = ["text"]

        rollover_task = asyncio.create_task(builder._rollover_draft_if_needed(builder._build_html()))
        await started.wait()
        require(builder.draft_id != old_draft_id, "永久消息开始等待后，draft_id 必须已经切换")
        require(draft_frames, "切换后必须立即发送新草稿首帧，而不能等永久消息完成")
        require(draft_frames[-1][0] == builder.draft_id, "首帧必须使用新 draft_id")
        require("丙" in draft_frames[-1][1] and "甲" not in draft_frames[-1][1], "新首帧只能包含尾部内容")

        release.set()
        await rollover_task
        await asyncio.sleep(0)
        require(
            any(call[0] == builder.draft_id for call in draft_frames),
            "永久消息完成后也不能把新草稿切回旧 ID",
        )
    finally:
        release.set()
        handlers.send_rich_html_message_unserialized = original_permanent
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead
        handlers.delete_message = original_delete


async def test_rollover_network_io_does_not_block_flush() -> None:
    """
    ★ 新增回归测试：这是本次修复要保证的核心属性——滚动触发的永久消息
    网络 I/O（此处 mock 为耗时 0.3s）不应阻塞 flush() 调用本身返回，也不
    应阻塞其他并发的 flush() 调用（模拟 _stream_flush_loop / refresh_loop
    在滚动期间继续按自己的节奏刷新）。
    """
    import time

    async def slow_send(chat_id, html_content, **kwargs):
        await asyncio.sleep(0.3)
        return 8181

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        return 9191

    original_permanent = handlers.send_rich_html_message_unserialized
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    original_delete = handlers.delete_message
    handlers.send_rich_html_message_unserialized = slow_send
    handlers.send_rich_message_draft = fake_draft
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message = AsyncMock()
    try:
        builder = handlers.RichMessageBuilder(chat_id=102)
        builder._register_active_draft = AsyncMock()
        builder.blocks = [
            "<p>甲" + "a" * 12000 + "</p>"
            "<details><summary>乙</summary><p>" + "b" * 12000 + "</p></details>"
            "<p>丙" + "c" * 10000 + "</p>"
        ]
        builder.block_types = ["text"]

        t0 = time.monotonic()
        await builder.flush()
        elapsed = time.monotonic() - t0
        require(
            elapsed < 0.2,
            f"flush() 不应等待滚动的网络 I/O 完成，实际耗时 {elapsed:.3f}s（模拟网络延迟 0.3s）",
        )

        # 触发滚动的同时，另一次独立 flush() 调用（模拟并发的刷新循环）也不应被卡住。
        t1 = time.monotonic()
        await builder.flush()
        elapsed2 = time.monotonic() - t1
        require(
            elapsed2 < 0.2,
            f"滚动进行中时，并发的 flush() 调用不应被阻塞，实际耗时 {elapsed2:.3f}s",
        )

        if builder._rollover_task is not None:
            await builder._rollover_task
        await asyncio.sleep(0)
    finally:
        handlers.send_rich_html_message_unserialized = original_permanent
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead
        handlers.delete_message = original_delete


async def test_oversized_single_block_falls_back_without_loss() -> None:
    completed_segments = []

    async def fake_send(chat_id, html_content, **kwargs):
        completed_segments.append(html_content)
        return 5252

    async def fake_draft(chat_id, draft_id, html_content, **kwargs):
        return 6363

    original_send = handlers.send_rich_html_message_unserialized
    original_draft = handlers.send_rich_message_draft
    original_dead = handlers.mark_draft_dead
    original_delete = handlers.delete_message
    handlers.send_rich_html_message_unserialized = fake_send
    handlers.send_rich_message_draft = fake_draft
    handlers.mark_draft_dead = AsyncMock()
    handlers.delete_message = AsyncMock()
    try:
        payload = "X" * (handlers.RICH_DRAFT_HARD_GUARD_CHARS + 500)
        builder = handlers.RichMessageBuilder(chat_id=100)
        builder._register_active_draft = AsyncMock()
        builder.blocks = [f"<table><tr><td>{payload}</td></tr></table>"]
        builder.block_types = ["text"]
        rolled = await builder._rollover_draft_if_needed(builder._build_html())
        await asyncio.sleep(0)
        require(rolled, "接近真实上限的未闭合单块必须触发兜底滚动")
        require(builder._rollover_history[-1]["mode"] == "plain_text_fallback", "超长单块应标记为降级模式")
        submitted_text = handlers._rich_visible_text(completed_segments[0])
        remainder_text = handlers._rich_visible_text(builder._build_html())
        # 新 draft 会带一个短暂的 Thinking 状态，占位文本不属于原始内容。
        remainder_text = remainder_text.replace("Thinking...", "", 1)
        require(submitted_text + remainder_text == payload, "降级分段必须保持全部可见文本，不得丢失")
        require(len(submitted_text) <= handlers.RICH_DRAFT_ROLLOVER_TEXT_CHARS, "兜底永久段仍须低于主动阈值")
    finally:
        handlers.send_rich_html_message_unserialized = original_send
        handlers.send_rich_message_draft = original_draft
        handlers.mark_draft_dead = original_dead
        handlers.delete_message = original_delete


def main() -> None:
    test_visible_count_and_top_level_blocks()
    test_prefers_complete_structural_boundary()
    test_character_and_block_budget_boundaries()
    asyncio.run(test_rollover_tracks_drafts_and_keeps_remainder())
    asyncio.run(test_flush_restarts_preview_with_new_draft_id())
    asyncio.run(test_rollover_network_io_does_not_block_flush())
    asyncio.run(test_oversized_single_block_falls_back_without_loss())
    print("draft rollover validation: PASS")


if __name__ == "__main__":
    main()
