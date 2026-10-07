# tests/unit/test_early_persist_and_input_count.py
"""回归测试：多照片/连发消息静默丢失（2026-09-12 生产事故）。

事故链路（用户视角："发两张图，模型只收到一张"）：

  1. 用户快速连发两条带图消息（或相册分片间隔超过聚合窗口）；
  2. 第二条的 spawn_turn_task 打断并取消第一条的回合任务；
  3. 第一条的 user 消息原先进持久化发生在回合任务内部的
     get_ai_response——被打断时可能尚未落库 → 消息静默消失；
  4. 预检日志的输入组合计数又把 file_ids 与 attachments 双表示
     各数一遍（1 张照片显示 photo×2），日志"证明"两张图都到了，
     把排查方向带偏（前几轮补丁都在修下游解析，真正丢失在上游）。

修复后的两个不变量（本文件逐条断言）：

  A. 计数不变量：resolve_input_combination 对 (模态, file_id) 去重，
     信封同时携带 file_ids 与 attachments 时每个物理附件恰好计 1 次。
  B. 持久化不变量：spawn_turn_task 在派发前把 user 消息落库；即便
     回合任务在 get_ai_response 之前被取消，消息也已在历史中，会被
     下一条消息合并（两张图都进入模型请求）；消费型接管（媒体参数
     卡片）与 pre_flight 拒绝可经 undo_early_persist 回滚。
"""
from __future__ import annotations

import asyncio

import pytest

from protocols.pipeline import resolve_input_combination
import turn_recovery
from turn_recovery import (
    EARLY_PERSIST_FLAG,
    EARLY_PERSIST_MODE,
    EARLY_PERSIST_TS,
    persist_user_message_entry,
    undo_early_persist,
)
from state import get_or_init_context
from core.messages import Message


def _photo_envelope(fids: list[str], text: str = "回答") -> dict:
    """模拟 app.py 单图/图片组生产者的信封形状（file_ids + attachments 双表示）。"""
    return {
        "role": "user",
        "content": f"📎 用户上传了图片组（共 {len(fids)} 张）\n\n{text}",
        "file_ids": list(fids),
        "type": "photo_group",
        "attachments": [{"kind": "photo", "file_id": f} for f in fids],
    }


# ---------------------------------------------------------------------------
# A. 输入组合计数：去重联合计数
# ---------------------------------------------------------------------------
def test_input_count_dual_representation_counts_once():
    """信封同时携带 file_ids 与 attachments（同一附件两种表示）：计 1 次。

    修复前该形状计 2 —— 生产日志 photo×2 实为 1 张图，直接误导排查。
    """
    msg = _photo_envelope(["AAA111", "BBB222"])
    combo = resolve_input_combination(msg)
    assert combo.photo_count == 2


def test_input_count_single_photo_dual_representation():
    """单图信封（file_ids=[X] + attachments=[X]）：photo×1，而不是 photo×2。"""
    msg = _photo_envelope(["CCCC3333"])
    combo = resolve_input_combination(msg)
    assert combo.photo_count == 1


def test_input_count_attachments_only():
    """混合类型信封（无 type/file_ids，仅 attachments）：按 attachments 计数。"""
    msg = {
        "role": "user",
        "content": "📎 用户引用了媒体（图片、视频）",
        "attachments": [
            {"kind": "photo", "file_id": "P1"},
            {"kind": "video", "file_id": "V1"},
        ],
    }
    combo = resolve_input_combination(msg)
    assert combo.photo_count == 1
    assert combo.video_count == 1


def test_input_count_file_ids_only():
    """仅 file_ids 无 attachments（历史遗留形状）：按 file_ids 计数。"""
    msg = {"role": "user", "content": "x", "type": "photo_group",
           "file_ids": ["F1", "F2", "F3"]}
    combo = resolve_input_combination(msg)
    assert combo.photo_count == 3


def test_input_count_singular_file_id_with_attachments():
    """单数 file_id + attachments 同一附件：计 1 次。"""
    msg = {
        "role": "user", "content": "x", "type": "document",
        "file_id": "D1", "file_name": "a.pdf",
        "attachments": [{"kind": "document", "file_id": "D1"}],
    }
    combo = resolve_input_combination(msg)
    assert combo.document_count == 1


def test_input_count_audio_mixed_kinds():
    """多音频 + 附件去重：voice/file_id 与 attachments 双表示不叠加。"""
    msg = {
        "role": "user", "content": "x",
        "attachments": [
            {"kind": "audio", "file_id": "A1"},
            {"kind": "voice", "file_id": "A2"},
        ],
    }
    combo = resolve_input_combination(msg)
    assert combo.audio_count == 2


# ---------------------------------------------------------------------------
# B. 提前持久化 + 回滚
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_spawn_early_persist_appended_then_undo():
    """appended 路径：入库 → meta 带 TS → undo 精确弹出该条。"""
    chat_id = 910000001
    ctx = get_or_init_context(chat_id)
    history = ctx.setdefault("conversation_history", [])
    before = len(history)

    env = _photo_envelope(["EARLYFID1"], text="第一张")
    ok = await persist_user_message_entry(chat_id, env)
    assert ok is True
    assert env[EARLY_PERSIST_FLAG] is True
    assert env[EARLY_PERSIST_MODE] == "appended"
    assert len(history) == before + 1
    assert history[-1].role == "user"
    # TS 随 _wrap_envelope 进入存储 meta（meta 永不出站）。
    assert history[-1].meta.get(EARLY_PERSIST_TS) == env[EARLY_PERSIST_TS]

    await undo_early_persist(chat_id, env)
    assert len(history) == before
    # 标记已清理：重复 undo 幂等无操作。
    assert EARLY_PERSIST_FLAG not in env
    await undo_early_persist(chat_id, env)
    assert len(history) == before


@pytest.mark.asyncio
async def test_rapid_two_photos_merge_both_survive_interrupt():
    """核心场景：连发两张图，第一张已提前持久化 → 第二张合并 → 两张都进入历史。

    修复前：第一张在回合任务内尚未落库即被打断取消，静默丢失；
    第二张单独成回合（预检 photo×2 = 双表示把 1 张图数成 2）。
    """
    chat_id = 910000002
    ctx = get_or_init_context(chat_id)
    history = ctx.setdefault("conversation_history", [])
    before = len(history)

    first = _photo_envelope(["PHOTO_AAAA"], text="")
    await persist_user_message_entry(chat_id, first)          # 派发时提前持久化

    second = _photo_envelope(["PHOTO_BBBB"], text="回答")
    await persist_user_message_entry(chat_id, second)         # 第二条派发时合并

    assert len(history) == before + 1, "两条应合并为一条，不产生连续 user"
    merged = history[-1]
    assert merged.role == "user"
    # 两张图的 file_ids 都在合并后的信封里 —— 模型请求将包含两张图。
    assert merged.meta.get("file_ids") == ["PHOTO_AAAA", "PHOTO_BBBB"]
    assert {a["file_id"] for a in merged.meta.get("attachments", [])} == {
        "PHOTO_AAAA", "PHOTO_BBBB",
    }
    # 第二条信封标记为 merged：undo 不回滚（旧消息已被改写，保守保留）。
    assert second[EARLY_PERSIST_MODE] == "merged"
    history_len = len(history)
    await undo_early_persist(chat_id, second)
    assert len(history) == history_len


@pytest.mark.asyncio
async def test_undo_skips_when_not_early_persisted():
    """未提前持久化的信封：undo 无操作（幂等安全）。"""
    chat_id = 910000003
    ctx = get_or_init_context(chat_id)
    history = ctx.setdefault("conversation_history", [])
    before = len(history)
    env = _photo_envelope(["NOFLAG1"])
    await undo_early_persist(chat_id, env)  # 无 FLAG → 直接返回
    assert len(history) == before


@pytest.mark.asyncio
async def test_undo_conservative_when_tail_changed():
    """appended 后历史末尾被其它写入顶掉：undo 校验 TS 不命中，保守不删。"""
    chat_id = 910000004
    ctx = get_or_init_context(chat_id)
    history = ctx.setdefault("conversation_history", [])

    env = _photo_envelope(["RACEFID1"])
    await persist_user_message_entry(chat_id, env)
    # 模拟并发写入顶掉末尾（例如旧回合 journal 保全先落了一条 assistant）。
    history.append(Message(role="assistant", blocks=[]))
    snapshot = list(history)
    await undo_early_persist(chat_id, env)
    assert history == snapshot, "TS 不命中时不得误删历史消息"


# ---------------------------------------------------------------------------
# C. spawn_turn_task 提前持久化接线（打断旧回合之后、创建任务之前）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_spawn_turn_task_persists_before_task_starts(monkeypatch):
    """spawn_turn_task(user_message=...)：落库发生在派发前（任务尚未运行）。"""
    import app_turns as at

    async def _no_interrupt(chat_id):  # 隔离：不打真实旧任务
        return None

    async def _no_cleanup(chat_id, task):  # 隔离：不触发 proactive
        return None

    monkeypatch.setattr(at, "_interrupt_active_generation", _no_interrupt)
    monkeypatch.setattr(at, "_cleanup_task", _no_cleanup)

    chat_id = 910000005
    ctx = get_or_init_context(chat_id)
    history = ctx.setdefault("conversation_history", [])
    before = len(history)

    env = _photo_envelope(["SPAWNFID1"], text="看这张")
    persisted_flag = {"value": False}

    async def _probe_turn():
        # 回合任务首行执行时，消息必须已经落库（这正是修复的目标不变量：
        # 任务从诞生起就不存在"消息未落库"的窗口）。
        persisted_flag["value"] = any(
            isinstance(m, Message) and m.meta.get(EARLY_PERSIST_TS) == env[EARLY_PERSIST_TS]
            for m in history
        )

    task = await at.spawn_turn_task(chat_id, _probe_turn(), user_message=env)
    await task
    assert persisted_flag["value"] is True
    assert len(history) == before + 1
    assert env.get(EARLY_PERSIST_FLAG) is True


@pytest.mark.asyncio
async def test_spawn_turn_task_without_user_message_no_persist(monkeypatch):
    """不传 user_message（TIMER / 媒体生成任务等）：不落库，行为不变。"""
    import app_turns as at

    async def _no_interrupt(chat_id):
        return None

    async def _no_cleanup(chat_id, task):
        return None

    monkeypatch.setattr(at, "_interrupt_active_generation", _no_interrupt)
    monkeypatch.setattr(at, "_cleanup_task", _no_cleanup)

    chat_id = 910000006
    ctx = get_or_init_context(chat_id)
    history = ctx.setdefault("conversation_history", [])
    before = len(history)

    async def _noop_turn():
        return None

    task = await at.spawn_turn_task(chat_id, _noop_turn())
    await task
    assert len(history) == before
