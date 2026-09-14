# -*- coding: utf-8 -*-
"""Responses 桥接层 × 多厂商状态机接入点测试。

验证需求文档的核心修复与语义（ai/responses_bridge.py 的
_begin_turn_sync / commit / 中断作废路径）：
  - 日常态增量跨轮生效（旧实现因 commit 时序缺陷每轮必然自举）；
  - 增量切片的 sent_ids 预置（水位内条目不重发）；
  - 本地压缩 / 跨厂商写入后的分叉自举；
  - 同厂商切换模型保留会话；
  - 会话类 4xx 错误识别与中断作废兜底。
"""
from __future__ import annotations

import asyncio

import pytest

import conversation_state as cs
import ai.responses_bridge as rb
from conversation_state import SEQ_META_KEY, ConversationPhase
from core.messages import Message

MODEL = "claude-opus-5"  # 测试夹具：lfree 厂商，protocol=openai_responses
CHAT = 4242


class _FakeConversations:
    def __init__(self) -> None:
        self._counter = 0

    async def create(self) -> object:
        self._counter += 1
        class _Conv:
            id = f"conv_test_{self._counter}"

        return _Conv()


class _FakeClient:
    def __init__(self) -> None:
        self.conversations = _FakeConversations()


def _setup_state() -> cs.ConversationState:
    cs._conversation_states.clear()
    st = cs.ConversationState()
    cs._conversation_states[CHAT] = st
    return st


def _model_info() -> object:
    from config import SUPPORTED_MODELS

    return SUPPORTED_MODELS[MODEL]


def _mirror_view(st: cs.ConversationState, entries: list[Message]) -> list[Message]:
    """模拟出站视图：system 头 + 镜像副本（meta 携带 seq）。"""
    copies = []
    for m in entries:
        if m.role == "system":
            continue
        copies.append(Message(role=m.role, blocks=list(m.blocks), name=m.name, meta=dict(m.meta)))
    return [Message.system("system head")] + copies


def test_second_turn_is_incremental_not_bootstrap() -> None:
    """核心修复：第一轮自举后，第二轮（仅新增 user 消息）走增量。

    旧实现 commit 时序错误（synced_revision 落后于 append-back 后的
    revision），导致 cursor 每轮必失效、每轮重新自举——增量优化从未
    跨轮生效。本测试锁定新语义。
    """
    st = _setup_state()
    client = _FakeClient()

    # ---- 回合 1：全量自举 ----
    mirror = [_user_msg := Message.user_text("hi"), Message.assistant_text("hello")]
    st.record_append(mirror, "lfree|https://lfree.example|openai_responses" if False else cs.WRITER_USER)
    view1 = _mirror_view(st, mirror)
    turn1 = st.begin_turn("USER")
    ctx1 = asyncio.run(rb._begin_turn_sync(
        client, CHAT, turn1, _model_info(), MODEL, view1,
    ))
    assert ctx1 is not None and ctx1.mode == "bootstrap"
    assert ctx1.conversation_id
    assert ctx1.sent_ids == set()  # 自举：全量发送
    # 提交回合 1（水位 = 已发号最大 seq）
    st.commit_vendor_sync(
        turn1, vendor_key=ctx1.vendor_key,
        conversation_id=ctx1.conversation_id, model=MODEL,
        synced_through_seq=ctx1.max_seq,
    )

    # ---- 回合 2：新增 user 消息 → 日常态增量 ----
    new_user = Message.user_text("next question")
    st.record_append([new_user], cs.WRITER_USER)
    view2 = _mirror_view(st, mirror + [new_user])
    turn2 = st.begin_turn("USER")
    ctx2 = asyncio.run(rb._begin_turn_sync(
        client, CHAT, turn2, _model_info(), MODEL, view2,
    ))
    assert ctx2 is not None
    assert ctx2.mode == "incremental", "第二轮必须复用会话走增量"
    assert ctx2.conversation_id == ctx1.conversation_id
    # 水位内条目全部预置为已发送（含 system 头以外的镜像副本）
    delta = [
        m for m in view2
        if id(m) not in ctx2.sent_ids and id(m) not in ctx2.local_assistant_ids
    ]
    assert [m.text() for m in delta if m.role != "system"] == ["next question"]
    # 清理在途回合登记
    st.unregister_turn(turn1)
    st.unregister_turn(turn2)


def test_turn_local_assistant_excluded_from_delta() -> None:
    """本回合由服务端响应产生的 assistant 消息不作为增量重发。"""
    st = _setup_state()
    client = _FakeClient()
    mirror = [Message.user_text("hi")]
    st.record_append(mirror, cs.WRITER_USER)
    view = _mirror_view(st, mirror)
    turn = st.begin_turn("USER")
    ctx = asyncio.run(rb._begin_turn_sync(
        client, CHAT, turn, _model_info(), MODEL, view,
    ))
    assert ctx is not None and ctx.mode == "bootstrap"
    # 模拟自举首轮已把全部视图条目发出（含 system 头→instructions）
    ctx.sent_ids.update(id(m) for m in view)
    # 模拟流式结束后 finalize 出的 assistant 消息进入 loop_messages
    assistant = Message.assistant_text("I am the reply")
    view.append(assistant)
    ctx.local_assistant_ids.add(id(assistant))
    # 工具结果随后进入 loop_messages
    tool_msg = Message.tool_result("call_1", "weather", "sunny")
    view.append(tool_msg)
    delta = [
        m for m in view
        if id(m) not in ctx.sent_ids and id(m) not in ctx.local_assistant_ids
    ]
    assert [id(m) for m in delta] == [id(tool_msg)]
    st.unregister_turn(turn)


def test_local_compaction_forces_bootstrap() -> None:
    """本地压缩（结构分叉）后：作废旧会话，重新自举（需求文档 二.2）。"""
    st = _setup_state()
    client = _FakeClient()
    mirror = [Message.user_text("hi"), Message.assistant_text("hello")]
    st.record_append(mirror, cs.WRITER_USER)
    turn1 = st.begin_turn("USER")
    ctx1 = asyncio.run(rb._begin_turn_sync(
        client, CHAT, turn1, _model_info(), MODEL, _mirror_view(st, mirror),
    ))
    st.commit_vendor_sync(
        turn1, vendor_key=ctx1.vendor_key,
        conversation_id=ctx1.conversation_id, model=MODEL,
        synced_through_seq=ctx1.max_seq,
    )
    st.unregister_turn(turn1)

    # 本地压缩：结构纪元推进 + 全部厂商会话作废（app_turns 接线语义）
    st.mark_structural_fork("local_compaction_eviction")
    turn2 = st.begin_turn("USER")
    ctx2 = asyncio.run(rb._begin_turn_sync(
        client, CHAT, turn2, _model_info(), MODEL, _mirror_view(st, mirror),
    ))
    assert ctx2 is not None
    assert ctx2.mode == "bootstrap"
    assert ctx2.conversation_id != ctx1.conversation_id
    st.unregister_turn(turn2)


def test_cross_vendor_write_then_switch_back_bootstraps() -> None:
    """切到其他厂商产出内容后切回：作废重建（需求文档 二.3）。"""
    st = _setup_state()
    client = _FakeClient()
    mirror = [Message.user_text("hi")]
    st.record_append(mirror, cs.WRITER_USER)
    turn1 = st.begin_turn("USER")
    ctx1 = asyncio.run(rb._begin_turn_sync(
        client, CHAT, turn1, _model_info(), MODEL, _mirror_view(st, mirror),
    ))
    st.commit_vendor_sync(
        turn1, vendor_key=ctx1.vendor_key,
        conversation_id=ctx1.conversation_id, model=MODEL,
        synced_through_seq=ctx1.max_seq,
    )
    st.unregister_turn(turn1)

    # 传统模型（其它端点）产出内容进入镜像
    legacy_out = Message.assistant_text("legacy answer")
    st.record_append([legacy_out], "agnes|https://c.example|openai_chat")

    turn2 = st.begin_turn("USER")
    ctx2 = asyncio.run(rb._begin_turn_sync(
        client, CHAT, turn2, _model_info(), MODEL,
        _mirror_view(st, mirror + [legacy_out]),
    ))
    assert ctx2 is not None
    assert ctx2.mode == "bootstrap", "传统模型写入后必须作废重建"
    assert ctx2.conversation_id != ctx1.conversation_id
    st.unregister_turn(turn2)


def test_system_prompt_change_forks() -> None:
    """系统提示变化（技能激活等）：指纹不匹配 ⇒ 作废重建。

    增量轮不重发头部 system 段，因此系统提示的变化必须通过指纹守卫
    触发分叉，保证模型始终拿到当前系统提示。
    """
    st = _setup_state()
    client = _FakeClient()
    mirror = [Message.user_text("hi"), Message.assistant_text("hello")]
    st.record_append(mirror, cs.WRITER_USER)
    turn1 = st.begin_turn("USER")
    ctx1 = asyncio.run(rb._begin_turn_sync(
        client, CHAT, turn1, _model_info(), MODEL, _mirror_view(st, mirror),
    ))
    # 与真实桥接层一致：commit 携带头部 system 段指纹
    head_view = _mirror_view(st, mirror)
    st.commit_vendor_sync(
        turn1, vendor_key=ctx1.vendor_key,
        conversation_id=ctx1.conversation_id, model=MODEL,
        synced_through_seq=ctx1.max_seq,
        instructions_hash=rb._head_instructions_key(head_view),
    )
    st.unregister_turn(turn1)

    # 系统提示变化：换一个头部 system 文本重建视图
    view2 = _mirror_view(st, mirror)
    view2[0] = Message.system("system head WITH SKILL ACTIVE")
    turn2 = st.begin_turn("USER")
    ctx2 = asyncio.run(rb._begin_turn_sync(
        client, CHAT, turn2, _model_info(), MODEL, view2,
    ))
    assert ctx2 is not None
    assert ctx2.mode == "bootstrap"
    ref = st.get_vendor_ref(ctx1.vendor_key)
    assert ref.fork_reason == "instructions_changed"
    st.unregister_turn(turn2)


def test_interrupt_after_dispatch_invalidates_vendor() -> None:
    """回合中断（已发过请求）：作废该厂商会话（分叉态兜底）。"""
    st = _setup_state()
    vendor_key = cs.derive_vendor_key(_model_info())
    mirror = [Message.user_text("hi")]
    st.record_append(mirror, cs.WRITER_USER)
    turn = st.begin_turn("USER")
    # 预置一个 DAILY 会话（模拟此前已同步，本回合正在增量续接）
    st.commit_vendor_sync(
        turn, vendor_key=vendor_key,
        conversation_id="conv_live", model=MODEL,
        synced_through_seq=st.last_seq,
    )
    ctx = rb._TurnSyncContext(
        vendor_key=vendor_key,
        mode="incremental",
        conversation_id="conv_live",
        max_seq=st.last_seq,
        dispatched=True,
    )
    turn.sync_ctx = ctx

    class _Builder:
        chat_id = CHAT

    try:
        raise asyncio.CancelledError()
    except BaseException:
        rb._invalidate_on_interrupt(_Builder(), turn)
    ref = st.get_vendor_ref(vendor_key)
    assert ref is not None and ref.conversation_id is None
    assert ref.phase == ConversationPhase.FORK
    st.unregister_turn(turn)


def test_interrupt_before_dispatch_keeps_vendor() -> None:
    """请求未出网时被打断：服务端会话未被污染，不作废。"""
    st = _setup_state()
    vendor_key = cs.derive_vendor_key(_model_info())
    turn = st.begin_turn("USER")
    ctx = rb._TurnSyncContext(
        vendor_key=vendor_key,
        mode="incremental",
        conversation_id="conv_live",
        max_seq=1,
        dispatched=False,
    )
    turn.sync_ctx = ctx
    # 预置一个 DAILY 会话（模拟此前已同步）
    mirror = [Message.user_text("hi")]
    st.record_append(mirror, cs.WRITER_USER)
    st.commit_vendor_sync(
        turn, vendor_key=ctx.vendor_key,
        conversation_id="conv_live", model=MODEL,
        synced_through_seq=st.last_seq,
    )

    class _Builder:
        chat_id = CHAT

    try:
        raise RuntimeError("pre-dispatch failure")
    except BaseException:
        rb._invalidate_on_interrupt(_Builder(), turn)
    assert st.get_vendor_ref(ctx.vendor_key).conversation_id == "conv_live"
    st.unregister_turn(turn)


def test_conversation_error_classification() -> None:
    """会话类 4xx 判定：400/404/422 命中；401/403/429 与网络错误不命中。"""
    class _ApiErr(Exception):
        def __init__(self, status: int) -> None:
            self.status_code = status

    assert rb._is_conversation_error(_ApiErr(400))
    assert rb._is_conversation_error(_ApiErr(404))
    assert rb._is_conversation_error(_ApiErr(422))
    assert not rb._is_conversation_error(_ApiErr(401))
    assert not rb._is_conversation_error(_ApiErr(403))
    assert not rb._is_conversation_error(_ApiErr(429))
    assert not rb._is_conversation_error(RuntimeError("connection reset"))
