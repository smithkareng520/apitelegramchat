# -*- coding: utf-8 -*-
"""多厂商 / 多模型 Conversation 与上下文同步状态机的单元测试。

覆盖需求文档的核心流转（对应 conversation_state.py / server_compaction.py）：
  - 单一事实来源 + vendor_conversations 厂商分区映射（ID 强隔离）
  - 日常态增量优先（水位/序列号账本、写入者台账）
  - 分叉态作废重建（本地压缩、跨厂商写入、传统模型写入、写入者缺口）
  - /clear 全清 + generation fencing
  - 服务端压缩回拉：事件/元数据检测、Adapter 数据清洗、回拉覆盖与
    拉取失败兜底
"""
from __future__ import annotations

import asyncio

import pytest

import conversation_state as cs
from conversation_state import (
    SEQ_META_KEY,
    ConversationState,
    ConversationPhase,
)
from core.messages import Message
from server_compaction import (
    PullOverflowError,
    adapt_items_to_messages,
    detect_compaction_metadata,
    detect_compaction_signal,
)

VENDOR_A = "lfree|https://a.example|openai_responses"
VENDOR_B = "other|https://b.example|openai_responses"
LEGACY = "agnes|https://c.example|openai_chat"


def _user(text: str) -> Message:
    return Message.user_text(text)


def _assistant(text: str) -> Message:
    return Message.assistant_text(text)


class TestMirrorLedger:
    """镜像序列号 / 写入者台账基础语义。"""

    def test_record_append_assigns_monotonic_seqs(self) -> None:
        st = ConversationState()
        m1, m2, m3 = _user("a"), _assistant("b"), _user("c")
        st.record_append([m1, m2], cs.WRITER_USER)
        st.record_append([m3], VENDOR_A)
        seqs = [m.meta[SEQ_META_KEY] for m in (m1, m2, m3)]
        assert seqs == [1, 2, 3]
        assert st.last_seq == 3
        assert st.writer_of(1) == cs.WRITER_USER
        assert st.writer_of(3) == VENDOR_A
        assert st.writer_of(99) is None  # 台账未覆盖

    def test_record_append_skips_non_message(self) -> None:
        st = ConversationState()
        st.record_append([{"role": "user", "content": "dict 形状"}], cs.WRITER_USER)
        assert st.last_seq == 0  # dict 不发号
        assert st.mirror_revision == 0

    def test_append_batch_ledger_bounded(self) -> None:
        st = ConversationState()
        for i in range(200):
            st.record_append([_user(f"m{i}")], cs.WRITER_USER)
        assert len(st.append_batches) <= 64
        # 最早的批次被挤出 → writer 查不到（保守分叉依据）
        assert st.writer_of(1) is None
        assert st.writer_of(200) == cs.WRITER_USER


class TestPlanAndCommit:
    """日常态增量 / 分叉态自举的判定与提交。"""

    def _bootstrap(self, st: ConversationState, turn) -> None:
        u = _user("hi")
        st.record_append([u], cs.WRITER_USER)
        assert st.commit_vendor_sync(
            turn, vendor_key=VENDOR_A, conversation_id="conv_1",
            model="ma", synced_through_seq=st.last_seq,
        )

    def test_first_request_bootstraps(self) -> None:
        st = ConversationState()
        plan = st.plan_vendor_request(VENDOR_A, "ma", [1, 2])
        assert plan.mode == "bootstrap"
        assert plan.reason == "no_server_session"

    def test_daily_incremental_within_watermark(self) -> None:
        st = ConversationState()
        turn = st.begin_turn("USER")
        self._bootstrap(st, turn)
        # 全部条目都在水位内 → 日常态增量
        plan = st.plan_vendor_request(VENDOR_A, "ma", [1])
        assert plan.mode == "incremental"
        assert plan.conversation_id == "conv_1"

    def test_user_written_delta_is_incremental(self) -> None:
        """新 user 输入（厂商无关写入者）→ 仍走增量（需求文档 日常态）。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        self._bootstrap(st, turn)
        new_user = _user("next question")
        st.record_append([new_user], cs.WRITER_USER)
        seq = new_user.meta[SEQ_META_KEY]
        plan = st.plan_vendor_request(VENDOR_A, "ma", [1, seq])
        assert plan.mode == "incremental"

    def test_same_vendor_model_switch_keeps_conversation(self) -> None:
        """同厂商内切换模型：保留 conversation_id，仅更新 model 参数。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        self._bootstrap(st, turn)
        new_user = _user("hello")
        st.record_append([new_user], cs.WRITER_USER)
        plan = st.plan_vendor_request(
            VENDOR_A, "mb", [1, new_user.meta[SEQ_META_KEY]]  # 模型换成 mb
        )
        assert plan.mode == "incremental"
        assert plan.conversation_id == "conv_1"
        # commit 更新 model，会话不换
        st.commit_vendor_sync(
            turn, vendor_key=VENDOR_A, conversation_id="conv_1",
            model="mb", synced_through_seq=st.last_seq,
        )
        assert st.get_vendor_ref(VENDOR_A).model == "mb"

    def test_foreign_vendor_writer_forks(self) -> None:
        """跨厂商写入 → 分叉作废（需求文档 二.3 厂商间隔离）。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        self._bootstrap(st, turn)
        foreign = _assistant("from vendor B")
        st.record_append([foreign], VENDOR_B)
        plan = st.plan_vendor_request(
            VENDOR_A, "ma", [1, foreign.meta[SEQ_META_KEY]]
        )
        assert plan.mode == "bootstrap"
        assert "foreign_writer" in plan.reason
        ref = st.get_vendor_ref(VENDOR_A)
        assert ref.conversation_id is None
        assert ref.phase == ConversationPhase.FORK

    def test_legacy_writer_forks(self) -> None:
        """传统模型写入 → 切回 Responses 时作废重建（需求文档 二.3）。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        self._bootstrap(st, turn)
        legacy_out = _assistant("legacy answer")
        st.record_append([legacy_out], LEGACY)
        plan = st.plan_vendor_request(
            VENDOR_A, "ma", [1, legacy_out.meta[SEQ_META_KEY]]
        )
        assert plan.mode == "bootstrap"

    def test_recovery_writer_forks(self) -> None:
        """打断保全写入（recovery）→ 保守分叉。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        self._bootstrap(st, turn)
        salvaged = _assistant("partial output")
        st.record_append([salvaged], cs.WRITER_RECOVERY)
        plan = st.plan_vendor_request(
            VENDOR_A, "ma", [1, salvaged.meta[SEQ_META_KEY]]
        )
        assert plan.mode == "bootstrap"

    def test_writer_ledger_gap_forks(self) -> None:
        """台账缺口（seq 被占但无批次记录）→ 保守分叉。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        self._bootstrap(st, turn)
        orphan = _user("gap")
        orphan.meta[SEQ_META_KEY] = st.next_seq()  # 跳过台账直接占号
        plan = st.plan_vendor_request(
            VENDOR_A, "ma", [1, orphan.meta[SEQ_META_KEY]]
        )
        assert plan.mode == "bootstrap"
        assert plan.reason == "writer_ledger_gap"

    def test_unsequenced_entry_forks(self) -> None:
        """无法发号的条目（dict 形状）→ 保守分叉。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        self._bootstrap(st, turn)
        plan = st.plan_vendor_request(VENDOR_A, "ma", [1, None])
        assert plan.mode == "bootstrap"
        assert plan.reason == "unsequenced_entry"

    def test_structural_epoch_mismatch_forks(self) -> None:
        """结构纪元不匹配（本地压缩后）→ 分叉。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        self._bootstrap(st, turn)
        st.mark_structural_fork("local_compaction_eviction")
        plan = st.plan_vendor_request(VENDOR_A, "ma", [1])
        assert plan.mode == "bootstrap"

    def test_commit_fencing_rejects_stale_generation(self) -> None:
        """/clear 后旧回合的迟到 commit 被拒绝（不复活已作废会话）。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        st.reset()
        committed = st.commit_vendor_sync(
            turn, vendor_key=VENDOR_A, conversation_id="conv_late",
            model="ma", synced_through_seq=5,
        )
        assert committed is False
        assert st.get_vendor_ref(VENDOR_A) is None

    def test_commit_binds_new_conversation(self) -> None:
        st = ConversationState()
        turn = st.begin_turn("USER")
        assert st.commit_vendor_sync(
            turn, vendor_key=VENDOR_A, conversation_id="conv_new",
            model="ma", synced_through_seq=3,
        )
        ref = st.get_vendor_ref(VENDOR_A)
        assert ref.conversation_id == "conv_new"
        assert ref.phase == ConversationPhase.DAILY
        assert ref.synced_through_seq == 3
        assert ref.synced_structural_epoch == st.structural_epoch


class TestStructuralForkAndRewrite:
    """本地压缩 / 条目改写 / 撤回的结构分叉语义。"""

    def test_mark_structural_fork_invalidates_all_vendors(self) -> None:
        st = ConversationState()
        turn = st.begin_turn("USER")
        for vendor in (VENDOR_A, VENDOR_B):
            st.commit_vendor_sync(
                turn, vendor_key=vendor, conversation_id=f"conv_{vendor[:5]}",
                model="m", synced_through_seq=1,
            )
        st.mark_structural_fork("local_compaction_eviction")
        for vendor in (VENDOR_A, VENDOR_B):
            ref = st.get_vendor_ref(vendor)
            assert ref.conversation_id is None
            assert ref.phase == ConversationPhase.FORK
            assert ref.fork_reason == "local_compaction_eviction"
        assert st.structural_epoch == 1

    def test_note_entry_rewritten_invalidates_covering_refs_only(self) -> None:
        """合并/替换改写：仅作废水位已覆盖旧 seq 的会话。"""
        st = ConversationState()
        turn_a = st.begin_turn("USER")
        u1 = _user("first")
        st.record_append([u1], cs.WRITER_USER)  # seq=1
        st.commit_vendor_sync(
            turn_a, vendor_key=VENDOR_A, conversation_id="conv_a",
            model="ma", synced_through_seq=1,  # A 已同步 seq 1
        )
        # 厂商 B 从未见过任何条目（watermark=0 但有会话）
        turn_b = st.begin_turn("USER")
        st.commit_vendor_sync(
            turn_b, vendor_key=VENDOR_B, conversation_id="conv_b",
            model="mb", synced_through_seq=1,  # B 也同步到了 seq 1
        )
        rewritten = _user("first + followup")
        st.note_entry_rewritten(1, rewritten)  # 改写 seq=1 的条目
        # 两个会话都覆盖 seq=1 → 都作废
        assert st.get_vendor_ref(VENDOR_A).conversation_id is None
        assert st.get_vendor_ref(VENDOR_B).conversation_id is None
        # 改写后的条目获得新 seq，且以 user 写入者入账
        assert rewritten.meta[SEQ_META_KEY] == 2
        assert st.writer_of(2) == cs.WRITER_USER

    def test_note_entry_retracted_invalidates_covering_refs(self) -> None:
        st = ConversationState()
        turn = st.begin_turn("USER")
        u = _user("temp")
        st.record_append([u], cs.WRITER_USER)
        st.commit_vendor_sync(
            turn, vendor_key=VENDOR_A, conversation_id="conv_a",
            model="ma", synced_through_seq=1,
        )
        st.note_entry_retracted(u)
        assert st.get_vendor_ref(VENDOR_A).conversation_id is None


class TestClear:
    """/clear 语义（需求文档 二.4）。"""

    def test_reset_clears_all_vendor_bindings(self) -> None:
        st = ConversationState()
        turn = st.begin_turn("USER")
        for vendor in (VENDOR_A, VENDOR_B):
            st.commit_vendor_sync(
                turn, vendor_key=vendor, conversation_id=f"conv_{vendor[:5]}",
                model="m", synced_through_seq=2,
            )
        st.record_append([_user("x")], cs.WRITER_USER)
        st.request_server_sync(VENDOR_A, "stream:compaction")
        st.reset()
        assert st.vendor_conversations == {}
        assert st.append_batches.maxlen != 0 and len(st.append_batches) == 0
        assert st.last_seq == 0
        assert st.structural_epoch == 0
        assert st.mirror_revision == 0
        assert st.pending_server_sync == {}
        assert st.generation == 1

    def test_reset_keeps_active_turn_registry(self) -> None:
        """/clear 先打断在途回合：登记保留给其 finally 注销（幂等）。"""
        st = ConversationState()
        turn = st.begin_turn("USER")
        st.reset()
        assert turn.turn_id in st.active_turn_ids
        st.unregister_turn(turn)
        assert turn.turn_id not in st.active_turn_ids


class TestCompactionDetection:
    """服务端压缩事件 / 元数据检测（需求文档 二.1 监听）。"""

    class _Evt:
        def __init__(self, etype: str) -> None:
            self.type = etype

    def test_stream_event_detection(self) -> None:
        assert detect_compaction_signal(self._Evt("response.conversation.compacted"))
        assert detect_compaction_signal(self._Evt("conversation.compaction.completed"))
        assert detect_compaction_signal(self._Evt("response.history.truncated"))
        # 普通事件不误判
        assert detect_compaction_signal(self._Evt("response.output_text.delta")) is None
        assert detect_compaction_signal(self._Evt("response.output_item.added")) is None
        # 输出截断（max_output_tokens）不是历史压缩
        assert detect_compaction_signal(self._Evt("response.incomplete")) is None
        assert detect_compaction_signal(self._Evt("response.failed")) is None

    def test_metadata_detection(self) -> None:
        class Resp1:
            compaction = {"summary": "x"}
            metadata = None

        class Resp2:
            compaction = None
            metadata = {"openai.compaction.applied": True}

        class Resp3:
            compaction = None
            metadata = {"trace_id": "abc"}

        assert detect_compaction_metadata(Resp1()) == "response.compaction"
        assert detect_compaction_metadata(Resp2()) == "metadata.openai.compaction.applied"
        assert detect_compaction_metadata(Resp3()) is None
        assert detect_compaction_metadata(None) is None


class TestAdapter:
    """服务端 items -> 标准 messages 的数据清洗 Adapter（需求文档 二.1）。"""

    def test_sequential_tool_flow(self) -> None:
        items = [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "查天气"}]},
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "我来查一下"}]},
            {"type": "function_call", "call_id": "call_A",
             "name": "weather", "arguments": '{"city": "北京"}'},
            {"type": "function_call_output", "call_id": "call_A", "output": "晴 25度"},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "今天晴，25度"}]},
        ]
        msgs, stats = adapt_items_to_messages(items)
        assert [m.role for m in msgs] == ["user", "assistant", "assistant", "tool", "assistant"]
        assert msgs[0].text() == "查天气"
        assert msgs[1].text() == "我来查一下"
        calls = msgs[2].tool_calls()
        assert len(calls) == 1 and calls[0].id == "call_A" and calls[0].name == "weather"
        assert msgs[3].tool_result_block().tool_call_id == "call_A"
        assert msgs[3].text() == "晴 25度"
        assert stats["skipped_reasoning"] == 1

    def test_parallel_calls_out_of_order_outputs(self) -> None:
        items = [
            {"type": "function_call", "call_id": "c1", "name": "f1", "arguments": "{}"},
            {"type": "function_call", "call_id": "c2", "name": "f2", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c2", "output": "r2"},
            {"type": "function_call_output", "call_id": "c1", "output": "r1"},
        ]
        msgs, _stats = adapt_items_to_messages(items)
        assert len(msgs) == 3
        assert [tc.id for tc in msgs[0].tool_calls()] == ["c1", "c2"]
        assert msgs[1].tool_result_block().tool_call_id == "c2"
        assert msgs[2].tool_result_block().tool_call_id == "c1"

    def test_orphans_and_unknown_items(self) -> None:
        items = [
            {"type": "function_call_output", "call_id": "ghost", "output": "x"},
            {"type": "function_call", "call_id": "tail", "name": "g", "arguments": "{}"},
            {"type": "weird_item", "foo": 1},
        ]
        msgs, stats = adapt_items_to_messages(items)
        assert msgs == []
        assert stats["skipped_orphan_output"] == 1
        assert stats["skipped_orphan_call"] == 1
        assert stats["skipped_unknown"] == 1

    def test_empty_content_skipped(self) -> None:
        msgs, stats = adapt_items_to_messages(
            [{"type": "message", "role": "user", "content": []}]
        )
        assert msgs == []
        assert stats["skipped_empty"] == 1


class _FakeItemsPage:
    def __init__(self, data: list, has_more: bool = False, last_id: str | None = None) -> None:
        self.data = data
        self.has_more = has_more
        self.last_id = last_id


class _FakeConversationsItems:
    """模拟分页 items.list（含超限 / 失败注入）。"""

    def __init__(self, pages: list[_FakeItemsPage], fail: bool = False) -> None:
        self._pages = pages
        self._fail = fail
        self.calls = 0

    async def list(self, conversation_id: str, **kwargs: object) -> _FakeItemsPage:
        self.calls += 1
        if self._fail:
            raise RuntimeError("gateway 500")
        page_idx = kwargs.get("after") and 1 or 0
        if page_idx >= len(self._pages):
            return _FakeItemsPage([])
        return self._pages[page_idx]


class _FakeConversations:
    def __init__(self, items: _FakeConversationsItems) -> None:
        self.items = items


class _FakeClient:
    def __init__(self, items: _FakeConversationsItems) -> None:
        self.conversations = _FakeConversations(items)


class TestPullBackOverride:
    """回拉覆盖执行器：持锁覆盖镜像 / 其他厂商作废 / 拉取失败兜底。"""

    def _setup_chat(self, monkeypatch: pytest.MonkeyPatch) -> ConversationState:
        cs._conversation_states.clear()
        st = ConversationState()
        cs._conversation_states[9001] = st
        turn = st.begin_turn("USER")
        u = _user("hi")
        st.record_append([u], cs.WRITER_USER)
        st.commit_vendor_sync(
            turn, vendor_key=VENDOR_A, conversation_id="conv_sync",
            model="ma", synced_through_seq=st.last_seq,
        )
        st.unregister_turn(turn)  # setup 回合已结束，不阻塞回拉
        return st

    def _run(self, coro: asyncio.futures.Future) -> None:
        asyncio.run(coro)

    def test_override_replaces_mirror_and_rebaselines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        st = self._setup_chat(monkeypatch)
        # 预置本地镜像（含 system 摘要头 + 对话主体）
        import state as state_mod

        state_mod.user_contexts[9001] = {
            "conversation_history": [
                Message.system("[conversation digest] earlier stuff"),
                _user("hi"),
                _assistant("old reply"),
            ],
        }
        pages = [
            _FakeItemsPage([
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "compacted reply"}]},
            ]),
        ]
        fake_items = _FakeConversationsItems(pages)
        monkeypatch.setattr(
            "server_compaction._client_for_model_name",
            lambda model_name: _FakeClient(fake_items),
        )
        # 登记回拉 + 一个其他厂商的旧会话（应被覆盖事件作废）
        st.request_server_sync(VENDOR_A, "stream:response.conversation.compacted")
        turn_b = st.begin_turn("USER")
        st.commit_vendor_sync(
            turn_b, vendor_key=VENDOR_B, conversation_id="conv_b",
            model="mb", synced_through_seq=1,
        )
        st.unregister_turn(turn_b)  # 模拟回合已结束（否则回拉让路）
        from server_compaction import run_pending_server_sync

        self._run(run_pending_server_sync(9001))

        history = state_mod.user_contexts[9001]["conversation_history"]
        # system 摘要头保留，对话主体被服务端真实列表覆盖
        assert history[0].role == "system"
        assert "[conversation digest]" in history[0].text()
        assert [m.role for m in history[1:]] == ["user", "assistant"]
        assert history[1].text() == "hi"
        assert history[2].text() == "compacted reply"
        # 本厂商 ref 重新对齐（DAILY、新水位）；其他厂商作废
        ref_a = st.get_vendor_ref(VENDOR_A)
        assert ref_a.phase == ConversationPhase.DAILY
        assert ref_a.conversation_id == "conv_sync"
        assert ref_a.synced_through_seq == st.last_seq
        ref_b = st.get_vendor_ref(VENDOR_B)
        assert ref_b.conversation_id is None
        assert ref_b.phase == ConversationPhase.FORK
        # 清洗后的条目已重新发号并记入 server_sync 写入者台账
        assert st.writer_of(history[1].meta[SEQ_META_KEY]) == cs.WRITER_SERVER_SYNC

    def test_pull_failure_falls_back_to_fork(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """拉取失败兜底：本地镜像原样保留，会话作废进入分叉态。"""
        st = self._setup_chat(monkeypatch)
        import state as state_mod

        mirror = [
            Message.system("[conversation digest] d"),
            _user("hi"),
            _assistant("keep me"),
        ]
        state_mod.user_contexts[9001] = {"conversation_history": list(mirror)}
        fake_items = _FakeConversationsItems([], fail=True)
        monkeypatch.setattr(
            "server_compaction._client_for_model_name",
            lambda model_name: _FakeClient(fake_items),
        )
        st.request_server_sync(VENDOR_A, "stream:compaction")
        from server_compaction import run_pending_server_sync

        self._run(run_pending_server_sync(9001))

        # 镜像未被破坏（单一事实来源）
        history = state_mod.user_contexts[9001]["conversation_history"]
        assert [m.text() for m in history] == [m.text() for m in mirror]
        # 会话作废 → 下一轮自举重建
        ref = st.get_vendor_ref(VENDOR_A)
        assert ref.conversation_id is None
        assert ref.phase == ConversationPhase.FORK
        assert "server_sync_failed" in (ref.fork_reason or "")

    def test_defers_when_active_turn_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """有在途回合时回拉让路并重新登记（不抢占镜像工作集）。"""
        st = self._setup_chat(monkeypatch)
        import state as state_mod

        state_mod.user_contexts[9001] = {"conversation_history": []}
        fake_items = _FakeConversationsItems([])
        monkeypatch.setattr(
            "server_compaction._client_for_model_name",
            lambda model_name: _FakeClient(fake_items),
        )
        st.begin_turn("USER")  # 模拟在途回合未退出
        st.request_server_sync(VENDOR_A, "stream:compaction")
        from server_compaction import run_pending_server_sync

        self._run(run_pending_server_sync(9001))
        # 重新登记，未执行覆盖
        assert st.pending_server_sync == {VENDOR_A: "stream:compaction"}
        assert fake_items.calls == 0

    def test_pull_overflow_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超限捕获：分页超上限抛 PullOverflowError（不重试）。"""
        pages = [_FakeItemsPage([], has_more=True, last_id="p1")] * 100
        fake_items = _FakeConversationsItems(pages)
        client = _FakeClient(fake_items)
        from server_compaction import pull_conversation_items

        with pytest.raises(PullOverflowError):
            self._run(pull_conversation_items(client, "conv_x"))


class TestVendorKeyDerivation:
    """厂商分区键推导（provider|endpoint|protocol）。"""

    def test_derive_from_model_config(self) -> None:
        from config import SUPPORTED_MODELS
        from conversation_state import derive_vendor_key

        # 任意已注册模型均可推导出稳定的分区键
        name = next(iter(SUPPORTED_MODELS))
        key = derive_vendor_key(SUPPORTED_MODELS[name])
        parts = key.split("|")
        assert len(parts) == 3
        assert parts[2]  # protocol 非空
        # 同一模型重复推导结果一致
        assert derive_vendor_key(SUPPORTED_MODELS[name]) == key
