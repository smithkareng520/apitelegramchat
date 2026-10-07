'''第 2 轮审查修复的回归测试（2026-10）：'''

import asyncio

import pytest

import responses_state as rs
from protocols.base import invalidate_responses_chain_for


# ---------------------------------------------------------------------------
# 1) 非 Responses 协议路由后的链头作废
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invalidate_responses_chain_for_invalidates_existing_chain():
    chat_id = 911001
    state = await rs.get_response_state(chat_id)
    state.reset()
    turn = state.begin_turn("USER")
    assert rs.commit_response_sync(
        chat_id, turn, vendor_key="openrouter|https://x|openai_chat",
        response_id="resp_abc", model="gpt-test",
    )
    ref = state.chain
    assert ref is not None and ref.response_id == "resp_abc"

    cid = chat_id

    class _Builder:
        chat_id = cid

    invalidate_responses_chain_for(_Builder())
    assert state.chain.response_id is None
    assert state.chain.invalid_reason == "legacy_protocol_turn"


@pytest.mark.asyncio
async def test_invalidate_responses_chain_for_ignores_missing_chat_id():
    # builder 无 chat_id（异常形状）时不作废任何状态、也不抛错
    state = await rs.get_response_state(911002)
    state.reset()

    class _NoChat:
        pass

    invalidate_responses_chain_for(_NoChat())
    # 其他 chat 状态不受影响（这里只验证调用本身不抛）
    assert state.generation == 1


@pytest.mark.asyncio
async def test_invalidate_responses_chain_for_propagates_errors(monkeypatch):
    # 修复点核心语义：链头失效失败不再被静默吞掉——异常必须向上传播
    def _boom(chat_id):
        raise RuntimeError("state machine broken")

    monkeypatch.setattr(rs, "mark_legacy_divergence", _boom, raising=True)

    class _Builder:
        chat_id = 911003

    with pytest.raises(RuntimeError, match="state machine broken"):
        invalidate_responses_chain_for(_Builder())


# ---------------------------------------------------------------------------
# 2) subagent 工具超时归因（内部超时 vs 外层预算）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_subagent_tool_internal_timeout_is_attributed_to_tool(monkeypatch):
    from subagent_tool import _execute_tool_for_subagent

    async def _fake_dispatch(name, args, chat_id=None, progress_callback=None):
        # 工具内部（如 aiohttp / SDK）抛出的 TimeoutError
        raise asyncio.TimeoutError("internal sdk read timeout")

    monkeypatch.setattr("ai.tool_call_loop.dispatch_tool_call", _fake_dispatch)
    result = await _execute_tool_for_subagent("web_search", {"query": "x"}, chat_id=1)
    assert "timed out internally" in result
    assert "internal sdk read timeout" in result
    # 绝不能再出现外层预算文案
    assert "subagent context" not in result


@pytest.mark.asyncio
async def test_subagent_tool_outer_budget_timeout_keeps_context_message(monkeypatch):
    import subagent_tool
    from subagent_tool import _execute_tool_for_subagent

    # 直接把模块级预算改小（绕过 _env_int 的最小值校验）：外层预算 0.1s，
    # 工具睡 1s —— 只有外层预算耗尽这一种可能。
    monkeypatch.setattr(subagent_tool, "SUBAGENT_TOOL_TIMEOUT", 0.1)

    async def _fake_dispatch(name, args, chat_id=None, progress_callback=None):
        await asyncio.sleep(1)
        return "should not reach"

    monkeypatch.setattr("ai.tool_call_loop.dispatch_tool_call", _fake_dispatch)
    result = await _execute_tool_for_subagent("web_search", {"query": "x"}, chat_id=1)
    assert "timed out in subagent context after" in result


@pytest.mark.asyncio
async def test_subagent_forbidden_tool_is_rejected():
    from subagent_tool import _execute_tool_for_subagent

    result = await _execute_tool_for_subagent("subagent", {}, chat_id=1)
    assert "forbidden" in result


# ---------------------------------------------------------------------------
# 3) _cancel_old_task：调用方自身取消向上传播
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_old_task_propagates_caller_cancellation():
    from app_turns import _cancel_old_task, active_tasks, active_tasks_lock

    # 旧任务吞掉第一次取消后仍在收尾（模拟长 finally 排空）——
    # _cancel_old_task 会一直停在 asyncio.wait 上，此时调用方
    # （runner）自身被取消：修复后必须向上传播。
    async def _stubborn_exit():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(5)  # 收尾排空窗口

    old_task = asyncio.create_task(_stubborn_exit())
    async with active_tasks_lock:
        active_tasks[1] = old_task
    await asyncio.sleep(0)

    async def _interruptor():
        await _cancel_old_task(1)

    runner = asyncio.create_task(_interruptor())
    await asyncio.sleep(0.05)  # runner 已进入 wait、旧任务已进入收尾窗口
    assert not runner.done()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    # 清理：再取消一次让收尾窗口立即结束
    old_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old_task


@pytest.mark.asyncio
async def test_cancel_old_task_absorbs_expected_child_cancellation():
    from app_turns import _cancel_old_task, active_tasks, active_tasks_lock

    # 旧任务吞掉取消后改抛真实异常：_cancel_old_task 不得向上抛
    # （该异常已被取回并记录），且调用正常返回。
    async def _swallow_and_raise():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup exploded")

    old_task = asyncio.create_task(_swallow_and_raise())
    async with active_tasks_lock:
        active_tasks[2] = old_task
    await asyncio.sleep(0)
    await _cancel_old_task(2)  # 不应抛出 RuntimeError
    assert old_task.done()


# ---------------------------------------------------------------------------
# 4) cache_usage.usage_num 共享实现
# ---------------------------------------------------------------------------
def test_usage_num_semantics():
    from ai.cache_usage import usage_num

    assert usage_num(5) == 5
    assert usage_num(5.9) == 5
    assert usage_num(True) == 0          # bool 必须排除（True 不能当 1 token）
    assert usage_num("12") == 0          # 字符串不算数值
    assert usage_num(None) == 0
    assert usage_num(None, default=None) is None   # cache_usage._num 的 None 兜底语义
    assert usage_num(True, default=None) is None


# ---------------------------------------------------------------------------
# 5) MediaProgressSlot.complete 返回 journal 本体（身份匹配注销 in-flight）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_media_complete_returns_journal_identity_for_persisted_note():
    from ai.bridge_common import MediaProgressSlot
    import turn_recovery as tr

    chat_id = 911004
    journal: list = []
    await tr.register_inflight_turn(chat_id, journal, event_source="USER")

    slot = MediaProgressSlot(journal, "[图片生成中] 指令: 猫")
    new_msgs = slot.complete("[图片已生成] 指令: 猫")
    # 修复点：complete 必须返回 journal 本体（对象身份），否则
    # note_turn_persisted 永远无法注销该轮登记 → drain 时同一条
    # assistant 消息二次持久化进历史。
    assert new_msgs is journal

    # 模拟 update_conversation_and_ledger 的注销调用
    tr.note_turn_persisted(chat_id, new_msgs)
    assert tr._inflight.get(chat_id) in (None, []), \
        "journal 身份匹配后该轮 in-flight 登记必须被注销"


@pytest.mark.asyncio
async def test_media_complete_without_journal_still_returns_entries():
    from ai.bridge_common import MediaProgressSlot

    # journal=None（media_wizard 独立媒体轮）：退回新建列表，行为同旧版
    slot = MediaProgressSlot(None, "[图片生成中]")
    new_msgs = slot.complete("done")
    assert new_msgs == [slot.message]
