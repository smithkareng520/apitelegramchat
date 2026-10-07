"""工具卡片首次上屏强制成帧的回归测试。

背景：content_block_start 建卡时走 request_flush(force=False)，若前一段
正文/思考的 flush 仍在途（卡在 send_rich_message_draft 的 250ms 最小间隔
等待里），建卡请求只置脏即返回；flush() 在拿到 _flush_lock 后才构建 HTML，
等轮到构建时参数往往已流完——空壳占位帧与"参数已完整"帧合并成一帧，用户
看到的第一帧就是完整命令，折叠块"参数打完/开始执行后才出现"。

修复：add_tool_item 新建条目分支改为 request_flush(force=True)，卡片骨架
抢先独立成帧上屏；参数增量仍按非强制节奏填充。

本文件验证：
1. 新建工具卡片触发 force=True 的独立帧；
2. 在途正文 flush 期间建卡，卡片帧仍以 force 抢先发出（不被合并吞掉），
   且占位帧先于参数帧出现；
3. 合并进已有条目（同 id 重复 add）保持非强制，不放大请求量；
4. 同步批量建卡（tool_call_loop 执行路径）合并为一帧 force。
"""

import asyncio
import time

import pytest

import ai.rich_message_builder as rmb
from ai.rich_message_builder import RichMessageBuilder


@pytest.fixture()
def sent_frames(monkeypatch):
    """记录 flush 发出的每一帧（html / force / 时刻）。"""
    frames = []

    async def fake_send(chat_id, draft_id, html_content,
                        message_thread_id=None, force=False):
        frames.append({"html": html_content, "force": bool(force),
                       "t": time.monotonic()})
        return 4321

    monkeypatch.setattr(rmb, "send_rich_message_draft", fake_send)
    return frames


def _make_builder() -> RichMessageBuilder:
    return RichMessageBuilder(chat_id=1)


# =========================================================================
# 1. 新建工具卡片：必须以 force=True 独立成帧
# =========================================================================

def test_new_tool_card_sends_forced_frame_immediately(sent_frames):
    async def scenario():
        b = _make_builder()
        b.add_tool_item("call_1", "bash", "Running command",
                        action_description="running", fn_args={})
        # 让后台 _runner 任务跑完
        await asyncio.sleep(0.05)

        assert sent_frames, "建卡后必须有帧发出"
        assert sent_frames[0]["force"] is True, "卡片首帧必须是强制帧"
        assert "Running command" in sent_frames[0]["html"]
        assert "<details>" in sent_frames[0]["html"]

    asyncio.run(scenario())


# =========================================================================
# 2. 核心竞态：在途正文 flush 期间建卡，占位卡片帧仍抢先独立上屏
# =========================================================================

def test_card_frame_survives_inflight_text_flush(sent_frames):
    async def scenario():
        b = _make_builder()
        b.begin_stream_text()
        b.append_stream_delta("正在准备执行命令……")
        b.request_flush()  # task A：非强制发送，卡进 250ms 最小间隔等待
        await asyncio.sleep(0.05)

        # 在 task A 在途期间：建卡（force 闩上）→ 让出事件循环 → 参数才到达
        b.add_tool_item("call_1", "bash", "Running command", fn_args={})
        await asyncio.sleep(0.30)  # task A 结束后 runner 下一轮以 force 发卡
        b.update_tool_args("call_1", {"command": "echo hello > a.txt"})
        await asyncio.sleep(0.40)  # 参数增量按非强制节奏补发

        assert len(sent_frames) >= 3, (
            f"应至少有 正文帧/占位卡帧/参数帧，实际 {len(sent_frames)}")
        text_frame, card_frame = sent_frames[0], sent_frames[1]
        assert text_frame["force"] is False
        assert card_frame["force"] is True, "卡片帧必须绕过限流以 force 发出"
        # 占位帧先于参数出现：帧内只有通用占位摘要，没有具体命令
        assert "Running command" in card_frame["html"]
        assert "echo hello" not in card_frame["html"]
        # 参数帧随后补发，携带完整命令
        later = "".join(f["html"] for f in sent_frames[2:])
        assert "echo hello" in later

    asyncio.run(scenario())


# =========================================================================
# 3. 合并进已有条目：保持非强制
# =========================================================================

def test_merge_into_existing_item_stays_non_forced(monkeypatch):
    b = _make_builder()
    flags = []
    monkeypatch.setattr(b, "request_flush",
                        lambda force=False: flags.append(bool(force)))
    b.add_tool_item("call_1", "bash", "Running command", fn_args={})
    # 同 id 重复 add（执行批次按真实 id 合并流式占位条目）
    b.add_tool_item("call_1", "bash", "Running command",
                    fn_args={"command": "ls"})

    # 新建：start_new_tool_group 与 _refresh_outer_summary 各发一次非强制，
    # 随后新建分支强制刷新
    assert flags[0] is False
    assert flags[1] is False
    assert flags[2] is True, "新建条目应请求强制刷新"
    # 合并进已有条目（refresh + 显式 flush）：全部保持非强制，绝不出现新的
    # 强制请求（卡片已上屏，更新走正常节流）
    assert len(flags) == 5
    assert not any(flags[3:]), "合并进已有条目不应再触发强制帧"


# =========================================================================
# 4. 同步批量建卡：多个 force 合并为一帧，不放大请求量
# =========================================================================

def test_sync_batch_adds_collapse_into_one_forced_frame(sent_frames):
    async def scenario():
        b = _make_builder()
        for i in range(3):
            b.add_tool_item(f"call_{i}", "bash", "Running command", fn_args={})
        await asyncio.sleep(0.05)

        forced = [f for f in sent_frames if f["force"]]
        assert len(forced) == 1, "同步批量建卡应合并为一帧强制帧"
        # 外层工具组 + 3 张内层卡片
        assert sent_frames[0]["html"].count("<details>") == 4

    asyncio.run(scenario())
