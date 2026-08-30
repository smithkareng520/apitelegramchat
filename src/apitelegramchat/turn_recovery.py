# turn_recovery.py
"""Agent 轮次打断保全（turn journal + graceful close）。

背景与问题
==========

旧架构里，用户主动发消息（或 TIMER 唤醒）会**取消并丢弃**当前进行中的
agent 轮次：轮次内已经完成的 assistant 消息、工具调用与工具结果只存在于
任务局部变量 ``new_history_entries`` 里，任务被取消后全部丢失——已花的
token、已执行的工具进度全部作废，新轮次只能从零开始。

本模块把"进行中轮次的消息流水"登记为**轮次日志（turn journal）**：

- ``get_ai_response`` 开始时 ``register_inflight_turn`` 登记一个空 journal
  （列表引用），agentic 循环照常往里追加 assistant / tool 消息；
- 轮次**正常**结束：``update_conversation_and_ledger`` 在把 new_msgs 写入
  持久历史的同一把 chat 锁内调用 ``note_turn_persisted`` 注销登记
  （注销点必须在"消息已 append"之后，保证取消竞态下不会双写也不会漏写）；
- 轮次被**打断**：打断方（``proactive.interrupt_proactive_flow`` /
  ``app._interrupt_active_generation``）在旧任务完全停止后调用
  ``finalize_interrupted_turn`` / ``finalize_pending_turns``，把 journal
  里的已完成消息沉淀进持久历史；
- 轮次**异常**（额度不足 / 网关错误等）：``ai_handlers.get_ai_response``
  的异常路径调用 ``persist_salvaged_journal``，进度不因错误而丢失。

补齐结构（placeholder tool_result）
==================================

OpenAI 兼容协议要求 assistant 消息里的每个 ``tool_calls[i].id`` 都必须有
配对的 ``role=tool`` 消息。打断可能发生在工具执行前 / 执行中 / 批次结果
回写前，此时 journal 末尾会残留未配对的 tool_use。``_normalize_journal``
为所有未配对的 tool_call 追加占位结果：

    {"role": "tool", "tool_call_id": <id>, "name": <name>,
     "content": "用户打断，未执行"}

（``ai/tool_call_loop.py`` 在取消路径上会先把**已经执行完**的工具结果回填
真实 tool 消息，只剩真正未完成的才落到占位——最大化保留进度。）

user 消息落位（OpenAI 格式）
============================

- 打断发生在任何 assistant 输出之前：journal 为空，历史末尾仍是上一条
  user 消息 → 新 user 消息由 ``persist_user_message_entry`` **合并**进
  该消息（content 以空行拼接，附件数组拼接），避免连续两条 user；
- 打断发生在 tool_call 之后：占位 tool 消息补齐配对，新 user 消息直接
  追加在 tool 消息之后（OpenAI 允许 tool 后跟 user，tool 角色不破坏
  user 交替）；
- 打断发生在工具结果已回填、模型生成最终文本时：无需补结构，新 user
  消息直接追加。

新 user 消息由 ``persist_user_message_entry`` 在轮次开始时（get_ai_response
内）**提前持久化**并打上 early-persisted 标记，旧
``update_conversation_and_ledger`` 见到标记后跳过重复 append。提前持久化
让"快速连发多条消息"的合并链天然成立：msg2 打断 msg1 → msg2 并入 user1；
msg3 再打断 → 若 msg2 的轮次仍无任何输出，msg3 继续并入同一条 user 消息。

静默回复交付标记
================

``/show off``（静默模式）下，模型通过 ``deliver_reply`` 工具自主选择是否
把最终内容发给用户——工具无需参数，发送的是 agent 轮次的最后一条助手
消息正文（由 ``ai/tool_call_loop`` 在 journal 里回溯解析后传入 executor）。
executor 调用 ``mark_reply_delivered`` 记录"本轮已经
主动交付过"，``get_ai_response`` 收尾时据此判断用户主动回合是否需要兜底
直发（防止模型忘记调用导致用户提问石沉大海）。

锁顺序约定（防 ABBA 死锁）
==========================

- 注册表操作只短暂持有自己的 ``_registry_lock``，**不**在持锁期间获取
  chat 锁；
- 持久化路径（finalize / note_turn_persisted 的调用方）先释放注册表锁，
  再获取 chat 锁写历史；
- ``update_conversation_and_ledger`` 持 chat 锁期间调用
  ``note_turn_persisted``（只碰注册表锁），两个锁不同时跨路径持有。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from apitelegramchat.state import (
    get_chat_lock,
    get_or_init_context,
)
from apitelegramchat.utils import get_logger

logger = get_logger(__name__)

__all__ = [
    "INTERRUPTED_TOOL_PLACEHOLDER",
    "EARLY_PERSIST_FLAG",
    "register_inflight_turn",
    "note_turn_persisted",
    "finalize_interrupted_turn",
    "finalize_pending_turns",
    "drain_completed_turns",
    "persist_salvaged_journal",
    "persist_user_message_entry",
    "mark_reply_delivered",
    "pop_reply_delivered",
]

# 打断时未执行/被取消的工具调用的占位结果（OpenAI 格式 tool 消息 content）。
INTERRUPTED_TOOL_PLACEHOLDER = "用户打断，未执行"

# user 消息提前持久化标记：update_conversation_and_ledger 见到此标记跳过 append。
EARLY_PERSIST_FLAG = "__apitc_early_persisted__"

# 打断后等待旧任务收尾的上限（旧任务在 _cancel_old_task 里已等过 2~3s，
# 这里只是兜底；超时则按当前 journal 快照保全）。
_FINALIZE_TASK_WAIT_SECONDS = 1.5


@dataclass
class _InFlightEntry:
    chat_id: int
    journal: list
    task: Optional[asyncio.Task]
    event_source: str
    registered_at: float = field(default_factory=time.monotonic)


# chat_id -> 该 chat 进行中（或尚未注销）的轮次登记，按注册顺序排列。
# 注册表只做同步原子操作（append / pop / filter），asyncio 单线程模型下
# 不存在中途让出，故无需 asyncio.Lock——这也让"写历史 + 注销登记"
# 可以在 chat 锁内连成一个无取消窗口的原子区间（见 note_turn_persisted）。
_inflight: dict[int, list[_InFlightEntry]] = {}

# chat_id -> 本轮是否已通过 deliver_reply 主动交付过回复。
_reply_delivered: set[int] = set()


# =====================================================================
# 登记与注销
# =====================================================================
def _current_task() -> Optional[asyncio.Task]:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _pop_registry_entries(chat_id: int, *, expect_task=None, only_done: bool = False) -> list:
    """同步弹出符合条件的登记条目（无 await，天然原子，取消安全）。

    - expect_task 给定：只弹该任务对应的条目；
    - only_done=True：只弹任务已结束的条目（陈旧清扫）；
    - 其余情况弹出全部。
    """
    entries = _inflight.get(chat_id)
    if not entries:
        return []
    if expect_task is not None:
        targets = [e for e in entries if e.task is expect_task]
        remaining = [e for e in entries if e.task is not expect_task]
    elif only_done:
        targets = [e for e in entries if e.task is None or e.task.done()]
        remaining = [e for e in entries if not (e.task is None or e.task.done())]
    else:
        targets = list(entries)
        remaining = []
    if remaining:
        _inflight[chat_id] = remaining
    else:
        _inflight.pop(chat_id, None)
    return targets


async def register_inflight_turn(chat_id: int, journal: list, event_source: str = "USER") -> None:
    """登记一个进行中的 agent 轮次（journal 由 agentic 循环持续追加）。

    顺手把此前"已完成但仍未注销"的陈旧登记（例如空回复轮次从未走到
    update_conversation_and_ledger）先保全进历史，避免进度无限期滞留。
    """
    if chat_id is None:
        return
    try:
        await drain_completed_turns(chat_id)
    except Exception:
        logger.debug("drain_completed_turns 失败（可忽略）", exc_info=True)
    entry = _InFlightEntry(
        chat_id=chat_id,
        journal=journal,
        task=_current_task(),
        event_source=event_source or "USER",
    )
    # 同步 append：无 await 窗口，注册原子完成。
    _inflight.setdefault(chat_id, []).append(entry)
    logger.debug(
        "[turn-recovery] chat=%s 登记进行中轮次 source=%s journal_id=%s",
        chat_id, entry.event_source, id(journal),
    )


def note_turn_persisted(chat_id: int, new_msgs: list) -> None:
    """轮次消息已写入持久历史（update_conversation_and_ledger 内调用）。

    按列表对象身份匹配注销，绝不误删其他轮次的登记。
    【关键】本函数是同步的：update_conversation_and_ledger 在 chat 锁内
    的"append 消息 → 注销登记"因此连成一个原子区间——取消竞态下
    既不会出现"已写入却未注销"（打断方二次持久化导致双写），也不会
    出现"已注销却未写入"（进度丢失）。注册表只有同步原子操作，无需锁。
    """
    if chat_id is None or new_msgs is None:
        return
    entries = _inflight.get(chat_id)
    if not entries:
        return
    remaining = [e for e in entries if e.journal is not new_msgs]
    if len(remaining) != len(entries):
        _inflight[chat_id] = remaining
        logger.debug(
            "[turn-recovery] chat=%s 轮次已随历史持久化注销 journal_id=%s",
            chat_id, id(new_msgs),
        )


def deregister_turn(chat_id: int, journal: list) -> None:
    """显式注销（异常路径内部持久化后使用）。"""
    if chat_id is None or journal is None:
        return
    entries = _inflight.get(chat_id)
    if not entries:
        return
    remaining = [e for e in entries if e.journal is not journal]
    _inflight[chat_id] = remaining


async def drain_completed_turns(chat_id: int) -> None:
    """把"任务已结束但尚未持久化"的陈旧登记保全进历史（如空回复轮次）。"""
    if chat_id is None:
        return
    stale = _pop_registry_entries(chat_id, only_done=True)
    for entry in stale:
        try:
            await _persist_one_entry(entry, reason="stale-completed")
        except Exception:
            logger.debug("陈旧登记保全失败（可忽略）", exc_info=True)


# =====================================================================
# 补齐结构与持久化
# =====================================================================
def _unpaired_tool_calls(journal: list) -> list[tuple[str, str]]:
    """找出 journal 中没有配对 tool 消息的 (tool_call_id, name) 列表。"""
    paired: set[str] = set()
    for msg in journal:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id")
            if isinstance(tc_id, str) and tc_id:
                paired.add(tc_id)
    unpaired: list[tuple[str, str]] = []
    seen: set[str] = set()
    for msg in journal:
        if not (isinstance(msg, dict) and msg.get("role") == "assistant"):
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id")
            if not (isinstance(tc_id, str) and tc_id):
                continue
            if tc_id in paired or tc_id in seen:
                continue
            seen.add(tc_id)
            fn = tc.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            unpaired.append((tc_id, str(name or "unknown")))
    return unpaired


def _normalize_journal(journal: list) -> list:
    """返回补齐占位 tool 消息后的 journal 副本（原列表不被修改）。"""
    normalized = list(journal)
    placeholders = [
        {
            "role": "tool",
            "tool_call_id": tc_id,
            "name": name,
            "content": INTERRUPTED_TOOL_PLACEHOLDER,
        }
        for tc_id, name in _unpaired_tool_calls(normalized)
    ]
    if placeholders:
        normalized.extend(placeholders)
    return normalized


async def _append_journal_to_history(chat_id: int, journal: list) -> None:
    """把（已补齐结构的）journal 消息追加进持久历史。"""
    if not journal:
        return
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        history = ctx.setdefault("conversation_history", [])
        history.extend(journal)
    logger.info(
        "[turn-recovery] chat=%s 已保全轮次进度 %s 条消息（历史总长=%s）",
        chat_id, len(journal), len(get_or_init_context(chat_id).get("conversation_history") or []),
    )


async def _persist_one_entry(entry: _InFlightEntry, *, reason: str) -> list:
    """等待任务收尾（若仍在跑）并把该轮 journal 保全进历史。"""
    task = entry.task
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_FINALIZE_TASK_WAIT_SECONDS)
        except asyncio.CancelledError:
            # finalize 本身被取消（极端：两条用户消息背靠背到达）：
            # 把登记放回去，让下一次 finalize 继续处理。
            async with _registry_lock:
                _inflight.setdefault(entry.chat_id, []).append(entry)
            raise
        except Exception:
            logger.debug("[turn-recovery] 等待旧轮次收尾失败（按当前快照保全）", exc_info=True)
    journal = _normalize_journal(entry.journal)
    if not journal:
        return []
    await _append_journal_to_history(entry.chat_id, journal)
    logger.info(
        "[turn-recovery] chat=%s 轮次已保全并关闭 source=%s reason=%s msgs=%s",
        entry.chat_id, entry.event_source, reason, len(journal),
    )
    return journal


async def finalize_interrupted_turn(
    chat_id: int, *, expect_task: Optional[asyncio.Task] = None, reason: str = "interrupt"
) -> int:
    """打断后保全：持久化 journal（含占位补齐）。

    - ``expect_task`` 给定时只处理该任务对应的登记（proactive TIMER 打断）；
    - 不给时处理全部登记（用户回合打断的兜底路径）。
    返回本次实际保全的消息总条数（int；0 表示无可保全内容）。
    """
    if chat_id is None:
        return 0
    # 同步弹出（原子）：之后再做可能 await 的等待与持久化。
    targets = _pop_registry_entries(chat_id, expect_task=expect_task)
    if not targets:
        return 0

    salvaged = 0
    for entry in targets:
        journal = await _persist_one_entry(entry, reason=reason)
        salvaged += len(journal)
    return salvaged


async def finalize_pending_turns(chat_id: int, *, reason: str = "interrupt") -> int:
    """``app._interrupt_active_generation`` 在旧任务取消完成后调用。"""
    return await finalize_interrupted_turn(chat_id, reason=reason)


async def persist_salvaged_journal(chat_id: int, journal: list, *, reason: str) -> list:
    """异常路径（额度不足/网关错误等）保全进度：补齐结构并写历史。"""
    if chat_id is None:
        return []
    deregister_turn(chat_id, journal)
    normalized = _normalize_journal(journal)
    if not normalized:
        return []
    await _append_journal_to_history(chat_id, normalized)
    logger.info(
        "[turn-recovery] chat=%s 异常轮次进度已保全 reason=%s msgs=%s",
        chat_id, reason, len(normalized),
    )
    return normalized


# =====================================================================
# 新 user 消息的提前持久化与合并
# =====================================================================
_ARRAY_KEYS = ("file_ids", "file_names", "mime_types", "attachments")


def _merge_user_message(old: dict, new: dict) -> None:
    """把新 user 消息合并进历史末尾的旧 user 消息（原地修改 old）。

    - content：字符串以空行拼接（保持出站管线对 str content 的假设）；
    - 附件数组（file_ids / attachments 等）：顺序拼接；
    - type / file_id：旧消息缺失时采纳新消息的（保证多模态解析生效）。
    """
    old_text = old.get("content")
    new_text = new.get("content")
    parts = [str(t).strip() for t in (old_text, new_text) if isinstance(t, str) and str(t).strip()]
    if parts:
        old["content"] = "\n\n".join(parts)
    for key in _ARRAY_KEYS:
        new_vals = new.get(key)
        if isinstance(new_vals, list) and new_vals:
            existing = old.get(key)
            if not isinstance(existing, list):
                existing = []
            old[key] = existing + list(new_vals)
    if not old.get("type") and new.get("type"):
        old["type"] = new["type"]
    if not old.get("file_id") and new.get("file_id"):
        old["file_id"] = new["file_id"]
    if not old.get("file_name") and new.get("file_name"):
        old["file_name"] = new["file_name"]
    if not old.get("mime_type") and new.get("mime_type"):
        old["mime_type"] = new["mime_type"]


async def persist_user_message_entry(chat_id: int, user_message: dict) -> bool:
    """轮次开始时把新 user 消息写入持久历史（必要时合并进上一条 user）。

    返回 True 表示已持久化（get_ai_response 无需再单独 append 到请求，
    update_conversation_and_ledger 也会跳过重复写入）。TIMER 合成唤醒
    消息不经过本函数（不写历史）。
    """
    if chat_id is None or not isinstance(user_message, dict):
        return False
    lock = await get_chat_lock(chat_id)
    merged = False
    async with lock:
        ctx = get_or_init_context(chat_id)
        history = ctx.setdefault("conversation_history", [])
        last = history[-1] if history else None
        if isinstance(last, dict) and last.get("role") == "user":
            # 打断发生在任何 assistant 输出之前：合并，避免连续两条 user。
            _merge_user_message(last, user_message)
            merged = True
            logger.info(
                "[turn-recovery] chat=%s 新 user 消息合并进上一条未回应的 user 消息",
                chat_id,
            )
        else:
            history.append(user_message)
    user_message[EARLY_PERSIST_FLAG] = True
    return True


# =====================================================================
# 静默模式 deliver_reply 交付标记
# =====================================================================
def mark_reply_delivered(chat_id: int) -> None:
    """deliver_reply executor 成功发送后调用。"""
    if chat_id is not None:
        _reply_delivered.add(chat_id)


def pop_reply_delivered(chat_id: int) -> bool:
    """get_ai_response 收尾时读取并清除本轮交付标记。"""
    if chat_id is None:
        return False
    if chat_id in _reply_delivered:
        _reply_delivered.discard(chat_id)
        return True
    return False
