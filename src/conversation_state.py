# -*- coding: utf-8 -*-
"""多厂商 / 多模型下的 Conversation 与上下文同步状态机。

本模块是"本地上下文镜像 ↔ 各厂商服务端会话"同步的唯一权威实现，按
需求文档的架构原则与状态机流转规则整体重写（即使旧实现已覆盖其中大部分
语义，也以本文件为准，旧的 ProviderCursor 单游标模型已废弃）。

一、核心架构原则
================
1. **单一事实来源（Single Source of Truth）**：本地维护一份标准、权威的
   对话上下文镜像——``ctx["conversation_history"]``（``messages[]``，
   内部 ``core.messages.Message`` 形状，所有厂商共享）。任何协议适配器
   都只是把它渲染成各自的线上形状；服务端会话（Responses
   ``conversation`` 对象）只是它的**投影副本**，绝不是事实来源。

2. **日常态（增量优先）**：在同一厂商 / 支持 Responses API 的体系内，
   最大化利用服务端托管的 ``conversation_id``——日常交互只发送增量
   ``input``（通常就是本轮新增的 user 消息），不再每轮全量重发历史。

3. **分叉态（作废重建）**：凡是遇到跨厂商历史分叉、本地主动压缩或清空
   对话等场景，坚决避免脆弱的"双向增量差分追加"，一律**作废**当前
   ``conversation_id``，下一轮以本地最新全量上下文初始化一个全新会话，
   借由服务端的前缀匹配机制自动命中底层 Prompt Cache。

二、状态机（每个 chat × 每个厂商一个 VendorConversationRef）
==========================================================
::

                     ┌─────────────────────────────────────────────┐
                     │                                             │
   [IDLE 无会话] ──首次 Responses 请求──▶ [DAILY 日常态]             │
                     │   （全量自举建立 conversation）  │             │
                     │                    │增量请求（仅发 input 增量）│
                     │                    ▼                           │
                     │                 [DAILY] ───┐                   │
                     │                            │ 本地压缩(结构分叉) │
                     │                            │ 跨厂商/传统模型写入│
                     │                            │ /clear            │
                     │                            ▼                   │
                     │                 [FORK 分叉态] ─────────────────┘
                     │                            │ 下一次 Responses 请求
                     │                            ▼
                     │                 作废旧 id + 全量自举 → 新 [DAILY]
                     │
                     └──[DAILY] ──检测到服务端压缩事件──▶ [SYNCING 回拉同步中]
                                       （持 chat 锁：回拉期间下一轮输入
                                         不能抢占写入本地镜像）
                                            │ 回拉成功：Adapter 清洗覆盖
                                            │ 本地镜像 → 重新对齐 [DAILY]
                                            │ 回拉失败：兜底作废 → [FORK]

三、同步账本（为什么不是 "revision == 消息条数"）
================================================
旧实现把 cursor 的同步位置与 canonical_revision（每次 append 递增一次的
批次计数）混为一谈，且未扣除 system 消息、未考虑"append 批次 ≠ 消息条
数"，导致增量判定极脆弱。本版本改为三个正交账本：

- **mirror_seq（消息序列号）**：每条进入镜像的消息在 ``Message.meta
  ["mirror_seq"]`` 携带一个 chat 内单调递增的序列号（``next_seq()`` 分配）。
  跨回合的增量判定 = "镜像中 seq > ref.synced_through_seq 的条目"，与
  列表下标、system 头、出站视图裁剪（select_request_context）全部解耦。
- **append_batches（写入者台账）**：每次镜像追加记录 ``[seq..., writer]``。
  writer ∈ {"user"（用户输入，厂商无关）| "<vendor_key>"（该厂商模型产出）
  | "recovery"（打断/异常保全）| "server_sync"（服务端压缩回拉覆盖）}。
  增量合法性检查：seq > watermark 的镜像条目的写入者必须全部属于
  {``"user"``, 本厂商}——出现任何其他厂商 / 传统模型 / 保全 / 覆盖写入
  即判定"结构分叉"，作废重建（对应需求文档 二.3 的跨厂商切换规则）。
- **structural_epoch（结构纪元）**：本地压缩淘汰（滑动窗口 / 摘要合并）、
  服务端回拉覆盖等"镜像结构被替换"的事件递增。每个 ref 记录自己同步时
  的纪元，不匹配即作废——这是"本地压缩 → 作废重建"（需求文档 二.2）的
  硬性闸门。

四、厂商强隔离
==============
``vendor_conversations: Record<VendorKey, VendorConversationRef>`` 按厂商
分区维护服务端会话 id，严禁把厂商 A 的 id 传给厂商 B。VendorKey 由
``derive_vendor_key(model_info)`` 生成：``provider|endpoint|protocol``——
同厂商同端点内切换模型（均支持 Responses API）共享同一个 ref（保留
conversation_id、更新请求 model 参数、仅发增量）；不同厂商 / 不同端点 /
不同协议天然隔离。

五、并发模型
============
- 与旧版一致：同一 chat 任意时刻最多一个 in-flight 回合（spawn_turn_task
  取消语义 + chat 锁），所有镜像写入与状态变更都要求持有
  ``state.get_chat_lock(chat_id)``。
- **回拉加锁**（需求文档 二.1）：服务端压缩回拉覆盖执行期间持有 chat 锁，
  同步完成前下一轮用户输入不能抢占写入本地镜像（server_compaction 模块
  负责，本模块提供相位与 pending 登记）。
- **fencing**：commit 前校验 ``generation``（/clear 边界）。TIMER 回合被
  /clear 打断后迟到的结果会被拒绝，绝不复活已作废的会话。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:  # 仅供类型标注；运行时惰性导入避免循环依赖
    from config import ModelConfig

# 镜像消息序列号在 Message.meta 中的键名。
# meta 永不进出站请求体（core.messages.Message 的结构性保证），因此把
# 同步账本元数据放在 meta 是安全的。
SEQ_META_KEY = "mirror_seq"

# 厂商无关的写入者标签：用户输入（user 消息提前持久化 / 兜底追加）。
WRITER_USER = "user"
# 打断/异常保全路径的写入者标签：journal 保全的内容可能是任何厂商在途
# 产出的片段，一律视为"非本厂商写入"→ 触发分叉（保守且符合需求文档
# "分叉态作废重建"原则）。
WRITER_RECOVERY = "recovery"
# 服务端压缩回拉覆盖的写入者标签（server_compaction.Adapter 覆盖镜像）。
WRITER_SERVER_SYNC = "server_sync"
# 未知写入者（例如媒体向导卡片提交路径绕过了 note_turn_writer 登记）：
# 保守处理，触发分叉。
WRITER_UNKNOWN = "unknown"

# append_batches 台账容量：保留最近 N 个追加批次。超出后被查询的 seq
# 视为"台账未覆盖"→ 保守分叉（宁可重建，不做脆弱的差分追加）。
_APPEND_BATCH_LEDGER_MAX = 64


# =============================================================================
# 相位
# =============================================================================
class ConversationPhase:
    """VendorConversationRef 的生命周期相位（需求文档"状态机流转"）。

    用字符串常量而非 Enum：ref 会被日志序列化，保持值可直接阅读。
    """

    # 无服务端会话（尚未建立 / 从未使用过 Responses API）。
    IDLE = "idle"
    # 日常态：conversation_id 有效、同步账本对齐，增量优先。
    DAILY = "daily"
    # 分叉态：本地镜像与服务端会话已结构分叉，旧 id 已作废，
    # 下一次 Responses 请求将以本地全量上下文重建新会话。
    FORK = "fork"
    # 回拉同步中：检测到服务端压缩事件，正在 GET 拉取 + Adapter 覆盖
    # 本地镜像（持 chat 锁，下一轮输入不能抢占写入）。
    SYNCING = "syncing"


# =============================================================================
# 数据结构
# =============================================================================
@dataclass
class VendorConversationRef:
    """单个 (chat, vendor) 分区的服务端会话引用与同步账本。

    vendor_key：厂商分区键（``provider|endpoint|protocol``），厂商间 ID
        强隔离（需求文档 二.3：严禁将厂商 A 的 ID 传递给厂商 B）。
    conversation_id：Responses API 的 ``conversation`` 对象 id（conv_xxx）。
        选择 conversation 而非 previous_response_id：item 保留期不受单次
        response 30 天 TTL 限制，且语义贴近"一个 Telegram chat = 一个
        长期会话"。
    model：该会话最近一次使用的模型名。同厂商内切换模型**不**失效会话
        （Responses conversation 是模型无关容器，请求按需更新 model 参数）；
        记录模型名仅供诊断与回拉时定位客户端。
    instructions_hash：自举时登记的服务端会话 instructions 指纹（头部
        system 段文本的 SHA-256）。增量轮不重发头部 system 段——若本地
        系统提示发生变化（技能激活等），指纹不匹配 ⇒ 结构分叉，作废
        重建，保证模型始终拿到当前系统提示。
    phase：``ConversationPhase`` 四相位之一。
    synced_through_seq：服务端会话已确认登记的镜像序列号水位——镜像中
        seq ≤ 该值的（非 system 同步单元）都已存在于服务端会话中。
    synced_structural_epoch：同步完成时的镜像结构纪元（与当前
        structural_epoch 不等 ⇒ 镜像结构已被本地替换，必须作废重建）。
    fork_reason：进入分叉态的原因（诊断用）。
    """

    vendor_key: str
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    instructions_hash: Optional[str] = None
    phase: str = ConversationPhase.IDLE
    synced_through_seq: int = 0
    synced_structural_epoch: int = 0
    fork_reason: Optional[str] = None
    updated_at: float = 0.0

    def invalidate(self, reason: str) -> None:
        """作废当前服务端会话（分叉态）：清 id、标记相位与原因。

        不清 synced_through_seq——保留水位仅供诊断；有效性判定以
        conversation_id / structural_epoch / 写入者台账为准。
        """
        self.conversation_id = None
        self.phase = ConversationPhase.FORK
        self.fork_reason = reason
        self.updated_at = time.time()


@dataclass
class VendorSyncPlan:
    """``plan_vendor_request`` 的决策结果（增量 vs 自举）。

    mode："incremental"（日常态：复用 conversation_id，仅发增量 input）
          | "bootstrap"（自举：以本地全量上下文初始化新会话）。
    conversation_id：incremental 模式下为可复用的会话 id；bootstrap 模式
        下为 None（由调用方新建 conversation 对象后回填 commit）。
    synced_through_seq：incremental 模式的已同步水位（发号器基准）。
    reason：决策原因（日志 / 诊断）。
    """

    mode: str
    conversation_id: Optional[str]
    synced_through_seq: int
    reason: str


@dataclass
class TurnState:
    """一次 agent 回合的事务快照（commit 前的 generation fencing）。

    sync_ctx：Responses 桥接层（ai/responses_bridge.py）私有的回合内同步
        上下文挂载点——记录本回合使用了哪个厂商会话，供"回合中途被打断 /
        异常"时作废对应厂商会话（分叉态兜底，避免服务端残留半轮内容与
        本地镜像产生静默错位）。类型为桥接层对象，这里以 Any 承载避免
        循环导入。
    """

    turn_id: str
    event_source: str  # "USER" | "TIMER"
    generation: int
    base_revision: int
    sync_ctx: Any = None


@dataclass
class _AppendBatch:
    """一次镜像追加批次的写入者台账记录。"""

    seqs: tuple[int, ...]
    writer: str
    at: float


@dataclass
class ConversationState:
    """单个 chat 的对话同步状态机（全部字段变更要求持有 chat 锁）。

    generation：语义边界计数器。只有 ``/clear`` 递增——递增后所有旧
        generation 快照的 TurnState 在 commit 时被 fencing 拒绝。
    mirror_revision：镜像版本号（诊断用单调计数），每次追加批次 / 覆盖
        事件递增。不再与"消息条数"挂钩（旧实现的脆弱假设，已废弃）。
    structural_epoch：镜像结构纪元——本地压缩淘汰、回拉覆盖等"镜像结构
        被替换"的事件递增；各 ref 记录同步时的纪元，不匹配即作废重建。
    last_seq：镜像消息序列号发号器当前值（chat 内单调递增，不回退）。
    vendor_conversations：**厂商分区的服务端会话映射**
        （需求文档：``vendor_conversations: Record<VendorKey, string>``）。
    append_batches：写入者台账（有界队列，超出保守分叉）。
    active_turn_ids：在途回合登记（server_compaction 据此延迟回拉，
        避免覆盖一个正在流式中的回合的工作集）。
    pending_writer：当前回合的产出写入者标签槽（_call_api 登记、
        update_conversation_and_ledger 消费；chat 内回合串行，单槽即可）。
    pending_server_sync：待执行的服务端压缩回拉 {vendor_key: reason}。
    """

    generation: int = 0
    mirror_revision: int = 0
    structural_epoch: int = 0
    last_seq: int = 0
    vendor_conversations: dict[str, VendorConversationRef] = field(default_factory=dict)
    append_batches: "deque[_AppendBatch]" = field(default_factory=deque)
    active_turn_ids: set[str] = field(default_factory=set)
    pending_writer: Optional[str] = None
    pending_server_sync: dict[str, str] = field(default_factory=dict)

    # ---------------- 回合事务 ----------------

    def begin_turn(self, event_source: str) -> TurnState:
        turn = TurnState(
            turn_id=uuid.uuid4().hex[:16],
            event_source=event_source,
            generation=self.generation,
            base_revision=self.mirror_revision,
        )
        self.active_turn_ids.add(turn.turn_id)
        return turn

    def unregister_turn(self, turn: Optional[TurnState]) -> None:
        """注销在途回合登记（幂等；所有退出路径都必须调用）。"""
        if turn is not None:
            self.active_turn_ids.discard(turn.turn_id)

    def is_turn_current(self, turn: TurnState) -> bool:
        """commit 前的乐观并发校验：generation 必须仍是发起回合时的那个。

        不校验 base_revision——回合发起后镜像因本轮自身消息追加而前进是
        预期内的正常推进。真正要拒绝的只有"回合发起后发生了 /clear"
        （generation 变化）这一种情况。
        """
        return turn.generation == self.generation

    # ---------------- 序列号 / 镜像账本 ----------------

    def next_seq(self) -> int:
        self.last_seq += 1
        return self.last_seq

    def ensure_sequenced(self, messages: Iterable[Any]) -> int:
        """为缺少序列号的 Message 补发 seq（就地写 meta），返回 last_seq。

        供两处调用：
          1) 追加入库（record_mirror_append）时为新消息发号；
          2) Responses 桥接在回合开始 / 每轮工具循环后对请求视图调用——
             视图内的对象要么是镜像对象的副本（meta 已随拷贝携带 seq），
             要么是本回合私有的新消息（发号后随 append-back 进入镜像）。
        非 Message 形状（旧 dict 兼容路径）跳过——由 plan 阶段判定为
        "未发号条目"并保守分叉。
        """
        from core.messages import Message  # 惰性导入，避免环

        for msg in messages:
            if isinstance(msg, Message) and SEQ_META_KEY not in msg.meta:
                msg.meta[SEQ_META_KEY] = self.next_seq()
        return self.last_seq

    def record_append(self, messages: Iterable[Any], writer: str) -> None:
        """镜像追加入库：发号 + 台账 + 版本推进（必须在 chat 锁内调用）。

        messages 为本批追加进 ``ctx["conversation_history"]`` 的对象列表；
        已带 seq 的对象（回合内桥接层预发号）不重复发号。
        """
        from core.messages import Message  # 惰性导入，避免环

        seqs: list[int] = []
        for msg in messages:
            if isinstance(msg, Message):
                seq = msg.meta.get(SEQ_META_KEY)
                if not isinstance(seq, int):
                    seq = self.next_seq()
                    msg.meta[SEQ_META_KEY] = seq
                seqs.append(seq)
        if not seqs:
            return
        self.append_batches.append(_AppendBatch(tuple(seqs), writer, time.time()))
        while len(self.append_batches) > _APPEND_BATCH_LEDGER_MAX:
            self.append_batches.popleft()
        self.mirror_revision += 1

    def writer_of(self, seq: int) -> Optional[str]:
        """查询某序列号的写入者；台账未覆盖返回 None（保守分叉依据）。"""
        for batch in reversed(self.append_batches):
            if seq in batch.seqs:
                return batch.writer
        return None

    def note_entry_rewritten(self, old_seq: Optional[int], new_msg: Any) -> None:
        """镜像末尾条目被"合并/替换"改写（turn_recovery 路径）。

        - 旧 seq 已被某个厂商会话同步过 ⇒ 该会话可能已在服务端登记了
          改写前的旧文本：作废这些会话（分叉重建，绝不静默错位）。
        - 改写后的条目按新条目发号（成为正常的增量候选）。
        """
        if old_seq is not None:
            for ref in self.vendor_conversations.values():
                if ref.conversation_id and ref.synced_through_seq >= old_seq:
                    ref.invalidate("mirror_entry_rewritten")
        from core.messages import Message  # 惰性导入

        if isinstance(new_msg, Message):
            new_msg.meta[SEQ_META_KEY] = self.next_seq()
            self.append_batches.append(
                _AppendBatch((new_msg.meta[SEQ_META_KEY],), WRITER_USER, time.time())
            )
            while len(self.append_batches) > _APPEND_BATCH_LEDGER_MAX:
                self.append_batches.popleft()
            self.mirror_revision += 1

    def note_entry_retracted(self, msg: Any) -> None:
        """镜像条目被回滚撤销（undo_early_persist 的 append 撤回路径）。

        撤回通常发生在请求派发前（服务端不可能见过该条），此处仅防御性
        作废"水位已覆盖该 seq"的会话，保证任何时序下都不静默错位。
        """
        from core.messages import Message  # 惰性导入

        seq = msg.meta.get(SEQ_META_KEY) if isinstance(msg, Message) else None
        if seq is None:
            return
        for ref in self.vendor_conversations.values():
            if ref.conversation_id and ref.synced_through_seq >= seq:
                ref.invalidate("mirror_entry_retracted")

    def mark_structural_fork(self, reason: str) -> None:
        """结构分叉（需求文档 二.2 / 一.3）：作废**全部**厂商会话。

        触发场景：本地压缩结构性淘汰（滑动窗口 / 摘要合并）等导致镜像
        结构被替换的事件。本地镜像是唯一事实来源——所有厂商的服务端
        会话一律作废，下一次请求以本地最新全量上下文重建（服务端前缀
        匹配自动命中 Prompt Cache）。
        """
        self.structural_epoch += 1
        self.mirror_revision += 1
        for ref in self.vendor_conversations.values():
            ref.invalidate(reason)

    # ---------------- 厂商会话：计划 / 提交 / 作废 ----------------

    def get_vendor_ref(self, vendor_key: str) -> Optional[VendorConversationRef]:
        return self.vendor_conversations.get(vendor_key)

    def invalidate_vendor(self, vendor_key: str, reason: str) -> None:
        """作废指定厂商的会话（分叉态）；无会话时安静返回。"""
        ref = self.vendor_conversations.get(vendor_key)
        if ref is not None:
            ref.invalidate(reason)

    def invalidate_all_vendors(self, reason: str) -> None:
        """作废全部厂商会话（例如路由落到传统协议适配器：传统模型的
        问答尚未在云端登记，切回 Responses 时必须重建——提前在此统一
        作废，等价于需求文档 二.3 的"切回即作废"）。"""
        for ref in self.vendor_conversations.values():
            ref.invalidate(reason)

    def plan_vendor_request(
        self,
        vendor_key: str,
        model: str,
        candidate_seqs: Iterable[Optional[int]],
        instructions_hash: Optional[str] = None,
    ) -> VendorSyncPlan:
        """判定本次 Responses 请求走"日常态增量"还是"分叉态自举"。

        candidate_seqs：本次请求视图中全部同步单元（按出站顺序）的镜像
        序列号；无法发号的条目（旧 dict 兼容形状）以 None 占位。
        instructions_hash：本次请求头部 system 段的指纹（增量轮不重发
        头部 system，指纹变化 = 系统提示已变 ⇒ 分叉重建）。

        增量合法性（全部满足才走 incremental）：
          1) ref 存在且 conversation_id 有效、相位为 DAILY；
          2) ref.synced_structural_epoch == 当前 structural_epoch；
          3) instructions_hash 与自举时登记的一致（系统提示未变）；
          4) 所有 seq > watermark 的候选条目写入者 ∈ {"user", 本厂商}。
        任一不满足 ⇒ 作废（如尚未作废）并返回 bootstrap——下一次请求以
        本地全量上下文重建新会话（前缀匹配命中 Prompt Cache）。
        """
        ref = self.vendor_conversations.get(vendor_key)
        if ref is None or not ref.conversation_id:
            return VendorSyncPlan("bootstrap", None, 0, "no_server_session")
        if ref.phase == ConversationPhase.SYNCING:
            # 防御：回拉同步进行中不应有新请求到达（chat 锁串行保证）。
            return VendorSyncPlan("bootstrap", None, 0, "server_sync_in_progress")
        if ref.phase == ConversationPhase.FORK:
            return VendorSyncPlan(
                "bootstrap", None, 0, f"fork_pending:{ref.fork_reason or 'unknown'}"
            )
        if ref.synced_structural_epoch != self.structural_epoch:
            self.invalidate_vendor(vendor_key, "structural_epoch_mismatch")
            return VendorSyncPlan("bootstrap", None, 0, "structural_fork")
        if (
            instructions_hash is not None
            and ref.instructions_hash is not None
            and instructions_hash != ref.instructions_hash
        ):
            self.invalidate_vendor(vendor_key, "instructions_changed")
            return VendorSyncPlan("bootstrap", None, 0, "instructions_changed")

        watermark = ref.synced_through_seq
        for seq in candidate_seqs:
            if seq is None:
                self.invalidate_vendor(vendor_key, "unsequenced_mirror_entry")
                return VendorSyncPlan("bootstrap", None, 0, "unsequenced_entry")
            if seq <= watermark:
                continue
            writer = self.writer_of(seq)
            if writer is None:
                self.invalidate_vendor(vendor_key, "writer_ledger_gap")
                return VendorSyncPlan("bootstrap", None, 0, "writer_ledger_gap")
            if writer != WRITER_USER and writer != vendor_key:
                # 跨厂商 / 传统模型 / 保全 / 覆盖写入：结构分叉（需求文档
                # 二.3——传统模型生成的问答尚未在云端登记，作废重建）。
                self.invalidate_vendor(vendor_key, f"foreign_writer:{writer}")
                return VendorSyncPlan("bootstrap", None, 0, f"foreign_writer:{writer}")
        return VendorSyncPlan(
            "incremental", ref.conversation_id, watermark, "daily_incremental"
        )

    def commit_vendor_sync(
        self,
        turn: TurnState,
        *,
        vendor_key: str,
        conversation_id: Optional[str],
        model: Optional[str],
        synced_through_seq: int,
        instructions_hash: Optional[str] = None,
    ) -> bool:
        """回合成功结束后提交（建立 / 续接）厂商会话同步账本。

        - fencing：generation 已因 /clear 前进 ⇒ 拒绝（过期回合的迟到
          结果绝不复活已作废的会话）。
        - bootstrap 场景在此绑定新 conversation_id；incremental 场景在此
          推进水位。watermark 以调用方传入的"本回合已发号最大 seq"为准
          （桥接层逐轮追踪，天然覆盖 user 提前持久化 + 回合内新增单元）。
        """
        if not conversation_id:
            return False
        if not self.is_turn_current(turn):
            return False
        ref = self.vendor_conversations.get(vendor_key)
        if ref is None:
            ref = VendorConversationRef(vendor_key=vendor_key)
            self.vendor_conversations[vendor_key] = ref
        ref.conversation_id = conversation_id
        ref.model = model
        ref.phase = ConversationPhase.DAILY
        ref.synced_through_seq = max(0, int(synced_through_seq))
        ref.synced_structural_epoch = self.structural_epoch
        if instructions_hash is not None:
            ref.instructions_hash = instructions_hash
        ref.fork_reason = None
        ref.updated_at = time.time()
        return True

    # ---------------- 服务端压缩回拉（需求文档 二.1） ----------------

    def request_server_sync(self, vendor_key: str, reason: str) -> None:
        """登记一次待执行的服务端压缩回拉（每厂商去重）。"""
        self.pending_server_sync.setdefault(vendor_key, reason)

    def take_pending_server_syncs(self) -> dict[str, str]:
        """取走全部待执行回拉登记（执行方独占消费）。"""
        if not self.pending_server_sync:
            return {}
        pendings = dict(self.pending_server_sync)
        self.pending_server_sync.clear()
        return pendings

    # ---------------- /clear（需求文档 二.4） ----------------

    def reset(self) -> None:
        """``/clear`` 语义：重置为初始空白状态。

        - generation += 1（fencing 边界：在途回合的迟到 commit 被拒绝）；
        - 清空全部厂商的 conversation_id 绑定（vendor_conversations 清空）；
        - 镜像账本（seq / 台账 / 纪元 / 版本）全部归零。
        canonical history 本身（ctx["conversation_history"]）不由本类持有，
        调用方（state.safe_clear_history）在同一 chat 锁临界区内清空。
        active_turn_ids 保留：/clear 先打断在途回合，其 finally 仍需注销
        登记（unregister 幂等）。
        """
        self.generation += 1
        self.mirror_revision = 0
        self.structural_epoch = 0
        self.last_seq = 0
        self.vendor_conversations.clear()
        self.append_batches.clear()
        self.pending_writer = None
        self.pending_server_sync.clear()

    # ---------------- 回合写入者标签槽 ----------------

    def note_turn_writer(self, vendor_key: str) -> None:
        """登记"当前回合产出消息的写入者"（_call_api 派发前调用）。

        chat 内回合串行（spawn_turn_task 取消语义），单槽即可；若上一
        回合异常退出未消费，本回合登记自然覆盖，不会串味。
        """
        self.pending_writer = vendor_key

    def consume_turn_writer(self) -> str:
        """取走写入者标签（update_conversation_and_ledger 追加时调用）。

        未登记（媒体向导等绕过 _call_api 的路径）返回 WRITER_UNKNOWN——
        写入者不明的追加一律保守触发分叉。
        """
        writer = self.pending_writer or WRITER_UNKNOWN
        self.pending_writer = None
        return writer


# =============================================================================
# 每 chat 一份状态 + 异步锁保护（与 state.py 的 _chat_locks 分工一致：
# 复用同一把 chat 锁让本模块的读写与历史读写天然互斥；本模块自身的
# _states_lock 只保护"惰性创建"这一步）。
# =============================================================================
_conversation_states: dict[int, ConversationState] = {}
_states_lock: Optional[asyncio.Lock] = None


def _get_states_lock() -> asyncio.Lock:
    """惰性创建 asyncio.Lock（与项目其他模块的惰性风格一致）。"""
    global _states_lock
    if _states_lock is None:
        _states_lock = asyncio.Lock()
    return _states_lock


async def get_conversation_state(chat_id: int) -> ConversationState:
    """获取（惰性创建）指定 chat 的 ConversationState。

    调用方应在持有 ``state.get_chat_lock(chat_id)`` 的前提下调用本函数
    并修改返回对象的字段——本模块自身的锁只保护"惰性创建"，不代替
    chat 锁；这与 state.py 里 ``user_contexts`` 的读写模型完全一致。
    """
    async with _get_states_lock():
        st = _conversation_states.get(chat_id)
        if st is None:
            st = ConversationState()
            _conversation_states[chat_id] = st
        return st


def get_conversation_state_sync(chat_id: int) -> ConversationState:
    """同步读路径：仅供已经确定状态必然存在、或可以接受"读到默认值"的
    场景（桥接层回合内的同步账本操作 / 诊断日志）使用。写路径一律走
    异步版本 + chat 锁。
    """
    st = _conversation_states.get(chat_id)
    if st is None:
        st = ConversationState()
        _conversation_states[chat_id] = st
    return st


async def reset_conversation_state(chat_id: int) -> None:
    """``/clear`` 专用：必须在调用方已持有 chat 锁时调用（与
    state.safe_clear_history 同一临界区）。
    """
    st = await get_conversation_state(chat_id)
    st.reset()


# =============================================================================
# 模块级 API：镜像追加 / 改写 / 撤回（要求调用方已持有 chat 锁）
# =============================================================================
def record_mirror_append(chat_id: int, messages: Iterable[Any], writer: str) -> None:
    """镜像追加入库（发号 + 写入者台账 + 版本推进）。

    调用点（全部处于 chat 锁内、与历史写入同一原子区间）：
      - app_turns.update_conversation_and_ledger（回合产出 / user 兜底追加）
      - turn_recovery.persist_user_message_entry（user 消息提前持久化）
      - turn_recovery._append_journal_to_history（打断/异常保全，writer=recovery）
      - server_compaction（回拉覆盖，writer=server_sync）
    """
    get_conversation_state_sync(chat_id).record_append(messages, writer)


def ensure_mirror_sequenced(chat_id: int, messages: Iterable[Any]) -> int:
    """为视图 / 镜像中缺号的 Message 补发 seq，返回当前 last_seq。"""
    return get_conversation_state_sync(chat_id).ensure_sequenced(messages)


def note_mirror_entry_rewritten(chat_id: int, old_seq: Optional[int], new_msg: Any) -> None:
    """镜像末尾 user 条目被合并/替换改写（见 ConversationState 同名方法）。"""
    get_conversation_state_sync(chat_id).note_entry_rewritten(old_seq, new_msg)


def note_mirror_entry_retracted(chat_id: int, msg: Any) -> None:
    """镜像末尾 user 条目被回滚撤销（见 ConversationState 同名方法）。"""
    get_conversation_state_sync(chat_id).note_entry_retracted(msg)


def mark_structural_fork(chat_id: int, reason: str) -> None:
    """结构分叉：本地压缩淘汰后调用，作废全部厂商会话（二.2）。"""
    get_conversation_state_sync(chat_id).mark_structural_fork(reason)


def mark_legacy_divergence(chat_id: int) -> None:
    """路由落到传统（非 Responses）协议适配器时调用：作废全部厂商会话。

    传统模型的问答尚未在云端登记；本调用让"切回 Responses API"天然
    命中需求文档 二.3 的"作废 + 全量自举重建"路径。不递增
    structural_epoch、不动镜像——canonical history 不受影响。
    """
    get_conversation_state_sync(chat_id).invalidate_all_vendors("legacy_protocol_turn")


def invalidate_vendor_session(chat_id: int, vendor_key: str, reason: str) -> None:
    """作废指定厂商会话（桥接层增量请求失败 / 回拉失败兜底等）。"""
    get_conversation_state_sync(chat_id).invalidate_vendor(vendor_key, reason)


# =============================================================================
# 模块级 API：厂商会话计划 / 提交
# =============================================================================
def plan_vendor_request(
    chat_id: int,
    vendor_key: str,
    model: str,
    candidate_seqs: Iterable[Optional[int]],
    instructions_hash: Optional[str] = None,
) -> VendorSyncPlan:
    """判定增量 vs 自举（见 ConversationState.plan_vendor_request）。"""
    return get_conversation_state_sync(chat_id).plan_vendor_request(
        vendor_key, model, candidate_seqs, instructions_hash
    )


def commit_vendor_sync(
    chat_id: int,
    turn: TurnState,
    *,
    vendor_key: str,
    conversation_id: Optional[str],
    model: Optional[str],
    synced_through_seq: int,
    instructions_hash: Optional[str] = None,
) -> bool:
    """回合结束提交厂商会话账本（fencing 内置；见 ConversationState）。"""
    return get_conversation_state_sync(chat_id).commit_vendor_sync(
        turn,
        vendor_key=vendor_key,
        conversation_id=conversation_id,
        model=model,
        synced_through_seq=synced_through_seq,
        instructions_hash=instructions_hash,
    )


def has_pending_server_sync(chat_id: int) -> bool:
    """是否存在待执行的服务端压缩回拉。"""
    return bool(get_conversation_state_sync(chat_id).pending_server_sync)


def request_server_sync(chat_id: int, vendor_key: str, reason: str) -> None:
    """登记待执行的服务端压缩回拉（流式事件 / 响应元数据检测到压缩时）。"""
    get_conversation_state_sync(chat_id).request_server_sync(vendor_key, reason)


# =============================================================================
# 模块级 API：回合写入者标签 / 在途登记
# =============================================================================
def note_turn_writer(chat_id: int, vendor_key: str) -> None:
    """登记当前回合的产出写入者（_call_api 派发前调用）。"""
    get_conversation_state_sync(chat_id).note_turn_writer(vendor_key)


def consume_turn_writer(chat_id: int) -> str:
    """取走当前回合的产出写入者（update_conversation_and_ledger 调用）。"""
    return get_conversation_state_sync(chat_id).consume_turn_writer()


def register_active_turn(chat_id: int, turn: TurnState) -> None:
    """登记在途回合（get_ai_response 回合开始时调用）。"""
    get_conversation_state_sync(chat_id).active_turn_ids.add(turn.turn_id)


def unregister_active_turn(chat_id: int, turn: Optional[TurnState]) -> None:
    """注销在途回合（get_ai_response 的 finally；幂等）。"""
    get_conversation_state_sync(chat_id).unregister_turn(turn)


def has_active_turns(chat_id: int) -> bool:
    """是否存在在途回合（server_compaction 回拉前的让路检查）。"""
    return bool(get_conversation_state_sync(chat_id).active_turn_ids)


# =============================================================================
# 厂商分区键
# =============================================================================
def derive_vendor_key(model_info: "ModelConfig") -> str:
    """从模型配置推导厂商分区键：``provider|endpoint|protocol``。

    - 同厂商同端点内切换模型 ⇒ 同一键 ⇒ 共享 conversation_id（日常态
      增量优先，请求按需更新 model 参数——需求文档 二.3）。
    - 不同厂商 / 不同端点 / 不同协议 ⇒ 不同键 ⇒ 服务端会话 ID 强隔离。
    """
    from config import get_effective_endpoint  # 惰性导入，避免环

    endpoint = get_effective_endpoint(model_info)
    return f"{endpoint.provider}|{endpoint.endpoint}|{endpoint.protocol}"


# =============================================================================
# 兼容垫片（deprecated）：旧 canonical_revision 计数语义
# =============================================================================
async def bump_canonical_revision(chat_id: int, n: int = 1) -> int:
    """.. deprecated:: 旧"批次计数"语义，仅供尚未迁移的调用方过渡。

    新代码一律使用 ``record_mirror_append``（携带写入者标签的镜像追加）。
    本函数只推进诊断版本号，不记写入者台账——因此经由此函数的追加会被
    plan 阶段判定为"台账缺口"并保守分叉（安全但低效）。
    """
    st = get_conversation_state_sync(chat_id)
    st.mirror_revision += n
    return st.mirror_revision


__all__ = [
    # 相位与数据结构
    "ConversationPhase",
    "VendorConversationRef",
    "VendorSyncPlan",
    "TurnState",
    "ConversationState",
    "SEQ_META_KEY",
    "WRITER_USER",
    "WRITER_RECOVERY",
    "WRITER_SERVER_SYNC",
    "WRITER_UNKNOWN",
    # 每 chat 状态访问
    "get_conversation_state",
    "get_conversation_state_sync",
    "reset_conversation_state",
    # 镜像账本
    "record_mirror_append",
    "ensure_mirror_sequenced",
    "note_mirror_entry_rewritten",
    "note_mirror_entry_retracted",
    "mark_structural_fork",
    "mark_legacy_divergence",
    "invalidate_vendor_session",
    # 厂商会话
    "derive_vendor_key",
    "plan_vendor_request",
    "commit_vendor_sync",
    # 服务端压缩回拉登记
    "has_pending_server_sync",
    "request_server_sync",
    # 回合写入者 / 在途登记
    "note_turn_writer",
    "consume_turn_writer",
    "register_active_turn",
    "unregister_active_turn",
    "has_active_turns",
    # 兼容垫片
    "bump_canonical_revision",
]
