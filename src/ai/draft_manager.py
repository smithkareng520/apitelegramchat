# -*- coding: utf-8 -*-
"""Agent Stream 与 Draft 显示层解耦：事件流、双状态机与 Draft Manager。

架构位置（改造后的分层）::

                 Agent Runtime          ← agentic_loops / *_bridge / tool_call_loop
              (LLM / Tool Loop)            只生产事件，绝不等待 UI
                     |
                     v
              Agent Event Stream         ← AgentEvent（本模块）
                     |
         +-----------+------------+
         v                        v
  Conversation State        Draft Manager        ← DraftManager（本模块）
  (给 LLM 的上下文)         (纯展示层)              消费事件 → 渲染 Draft
                                                   → 容量检测 → 安全点后台滚动

核心不变量（与需求文档逐条对应）：

1. **Agent 不等待 Draft**（§8）：Agent 侧的一切 UI 动作都收敛为
   ``DraftManager.emit()`` / ``submit()`` —— 同步、非阻塞、永不上网络。
   原先散落在循环里的 ``await builder.rollover_at_turn_boundary()``
   全部替换为安全点通知（``on_stream_block_closed`` / ``on_round_boundary``
   / ``on_tool_batch_end``），真正满足容量阈值的滚动由 DraftManager 以
   ``asyncio.create_task`` 在后台执行；终局（turn.end）例外——回合已经
   结束、不存在"下一轮更快"的问题，且 get_ai_response 收尾要基于构建器
   终态构建最终交付，故 ``finalize_turn()`` 仍同步收束。

2. **两条独立状态机**（§10）：AgentPhase（THINKING / CONTENT /
   TOOL_RUNNING / CONTINUE_GENERATION / DONE）由事件驱动前移；
   DraftPhase（ACTIVE / ROLLOVER_PENDING / WAIT_SAFE_POINT / CLOSED /
   NEW_DRAFT）由容量检测与安全点驱动。二者互不控制。

3. **三种安全切换点**（§7）：reasoning.end / content.end / tool.end。
   滚动绝不发生在 reasoning block / markdown 块 / tool group 中间——
   前两类由 builder 的完整外层块边界扫描（``_pick_rollover_boundary``）
   与 ``_has_pending_tool_group`` 守卫保证，本模块在调度前再做一次
   守卫预检（工具组未收束 → WAIT_SAFE_POINT，等 tool.end 再触发）。

4. **UI Event Buffer**（§9）：后台滚动从"调度"到"换血完成"期间，
   ``_swap_scheduled`` 同步置位，此后到达的全部事件进入
   :class:`DraftEventBuffer`；滚动完成后按序回放进新草稿。置位发生在
   调度瞬间（同步），因此"调度之后、任务首帧之前"的窗口内也没有任何
   直接写构建器的路径，旧段快照永远一致。

兼容层：DraftManager 通过 ``__getattr__`` 把未显式拦截的属性全部透传
给内部 builder，因此可以对所有既有调用方（循环、tool_call_loop、
get_ai_response 收尾）duck-typing 冒充 builder；``get_ai_response`` 在
创建 builder 处包一层 DraftManager 即完成接线，其余代码只把
``await builder.rollover_at_turn_boundary(...)`` 换成本模块的安全点 API。
"""
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

from utils import get_logger

logger = get_logger(__name__)


# =====================================================================
# Agent Event Stream（§4.1）：所有 Agent 输出转换为事件
# =====================================================================
class EventTypes:
    """Agent Event Stream 的标准事件类型（§4.1 事件表）。

    ``reasoning.*`` / ``content.*`` / ``tool.*`` / ``turn.end`` 为文档
    定义的核心事件；``content.block`` 等带 ``x`` 前缀说明的为 UI 内部
    操作事件——它们同样经事件流提交/缓冲/回放，只是不对应模型的原生
    流式输出形态（整块文本、伪工具调用文本撤回等）。
    """

    # 思考流
    REASONING_START = "reasoning.start"
    REASONING_DELTA = "reasoning.delta"
    REASONING_END = "reasoning.end"

    # 正文流
    CONTENT_START = "content.start"
    CONTENT_DELTA = "content.delta"
    CONTENT_END = "content.end"

    # 工具流
    TOOL_START = "tool.start"      # add_tool_item：工具卡片上屏
    TOOL_DELTA = "tool.delta"      # 参数流式更新 / 身份改绑 / 组内附文
    TOOL_RESULT = "tool.result"    # update_tool_item / update_tool_preview
    TOOL_END = "tool.end"          # finish_group：工具组收束（安全点）

    # 回合
    TURN_END = "turn.end"          # 回合终局（安全点，终局路径同步收束）

    # ---- UI 内部操作事件（非模型原生输出，经同一管道缓冲/回放）----
    CONTENT_BLOCK = "content.block"              # add_text：整块文本
    CONTENT_REPLACE_TAIL = "content.replace_tail"  # 伪工具调用文本撤回
    TOOL_GROUP_NEW = "tool.group.new"            # 显式新建工具组
    UI_END_STREAM = "ui.end_stream"              # 关闭当前流式块


# 允许触发滚动检查的安全边界（§7 三种安全切换点）。
SAFE_BOUNDARY_EVENTS = frozenset({
    EventTypes.REASONING_END,
    EventTypes.CONTENT_END,
    EventTypes.TOOL_END,
    EventTypes.TURN_END,
})


@dataclass
class AgentEvent:
    """Agent Event Stream 的最小事件信封。

    ``seq`` 由 DraftManager 统一分配，保证事件在观测与回放时可排序；
    ``data`` 的形状按 ``type`` 解释（delta 为 str，tool 为参数 dict 等）。
    """

    type: str
    data: Any = None
    seq: int = 0
    meta: dict = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - 仅诊断用
        data_len = len(self.data) if isinstance(self.data, str) else "-"
        return f"AgentEvent(seq={self.seq}, type={self.type!r}, data_len={data_len})"


# =====================================================================
# 状态机（§10）：两条状态机完全独立
# =====================================================================
class AgentPhase(str, Enum):
    """Agent 执行状态机：由 Agent Event 驱动，与 Draft 状态无关。"""

    THINKING = "THINKING"                  # reasoning.* 期间
    CONTENT = "CONTENT"                    # content.* 期间
    TOOL_RUNNING = "TOOL_RUNNING"          # tool.start..tool.result 期间
    CONTINUE_GENERATION = "CONTINUE_GENERATION"  # tool.end 后待续下一轮
    DONE = "DONE"                          # turn.end


class DraftPhase(str, Enum):
    """Draft 显示层状态机：由容量检测与安全点驱动，与 Agent 状态无关。"""

    ACTIVE = "ACTIVE"                  # 正常接收事件并实时渲染
    ROLLOVER_PENDING = "ROLLOVER_PENDING"  # 容量预警已置位，继续接收事件（§6）
    WAIT_SAFE_POINT = "WAIT_SAFE_POINT"    # 已到安全点：滚动已调度/等待工具组收束
    CLOSED = "CLOSED"                  # 旧草稿已收束（终局或回合结束）
    NEW_DRAFT = "NEW_DRAFT"            # 滚动完成，新草稿已建立


# AgentPhase 迁移表：事件 → 相位（TOOL_END / TURN_END 单独处理）
_PHASE_BY_EVENT = {
    EventTypes.REASONING_START: AgentPhase.THINKING,
    EventTypes.REASONING_DELTA: AgentPhase.THINKING,
    EventTypes.REASONING_END: AgentPhase.THINKING,
    EventTypes.CONTENT_START: AgentPhase.CONTENT,
    EventTypes.CONTENT_DELTA: AgentPhase.CONTENT,
    EventTypes.CONTENT_END: AgentPhase.CONTENT,
    EventTypes.TOOL_START: AgentPhase.TOOL_RUNNING,
    EventTypes.TOOL_DELTA: AgentPhase.TOOL_RUNNING,
    EventTypes.TOOL_RESULT: AgentPhase.TOOL_RUNNING,
}


# =====================================================================
# UI Event Buffer（§9）
# =====================================================================
class DraftEventBuffer:
    """滚动换血期间的事件缓存：push 暂存、flush 按序回放。

    语义（§9）::

        rollover 调度期间 → 事件仍然进入 buffer
        safe boundary 完成 → close current draft → create new draft
                           → flush buffered events（按原序回放）
    """

    def __init__(self) -> None:
        self._events: List[AgentEvent] = []

    def push(self, event: AgentEvent) -> None:
        self._events.append(event)

    def flush(self) -> List[AgentEvent]:
        """取走全部缓存事件（按入队顺序）；缓冲清零。"""
        events = self._events
        self._events = []
        return events

    def __len__(self) -> int:
        return len(self._events)


# 工具批次组句柄：批次开始时滚动已调度、组尚未（也绝不能）在旧草稿创建，
# begin_tool_batch 返回该句柄；finish_tool_batch 据此在回放时收束
# "批次实际落在的那个组"（回放后必为最新未收束组）。
_PENDING_GROUP_TOKEN = -2


# =====================================================================
# Draft Manager（§5）：纯展示层
# =====================================================================
class DraftManager:
    """消费 Agent Event Stream、渲染 Draft 并在安全点后台滚动。

    职责（§5）：接收 Event / 渲染当前 Draft / 判断 rollover / 管理 Draft 生命周期。
    不负责（§5）：Agent 调度、Tool 执行、LLM 调用——本模块对传入的任何
    事件都只做同步的 UI 状态变更或缓冲，从不发起模型请求，也从不等待
    工具执行。

    ``builder`` 是既有 :class:`~ai.rich_message_builder.RichMessageBuilder`
    （或 SilentMessageBuilder）。本类显式拦截"需要事件语义 / 滚动期缓冲"
    的方法，其余属性经 ``__getattr__`` 透传（draft_id、_tool_groups、
    stop_flush_loop 之外的读侧方法等），保证对全部既有调用方
    duck-typing 兼容。
    """

    def __init__(self, builder: Any) -> None:
        self._builder = builder
        # ---- 两条独立状态机（§10）----
        self.agent_phase: AgentPhase = AgentPhase.THINKING
        self.draft_phase: DraftPhase = DraftPhase.ACTIVE
        # ---- 事件流与缓冲 ----
        self._buffer = DraftEventBuffer()
        self._event_seq = 0
        # 当前开启的流式块类别（仅用于给 delta 事件标注类型，渲染不依赖）。
        self._open_stream_kind: Optional[str] = None
        # ---- 后台滚动状态 ----
        # 同步置位：调度瞬间即生效，杜绝"调度后、任务首帧前"的直写窗口。
        self._swap_scheduled = False
        self._rollover_task: Optional[asyncio.Task] = None
        # ---- 思考折叠提前收束续写标记 ----
        # 草稿预警后思考流仍在输出时，提前收束当前思考折叠块并调度滚动；
        # 本标记表示"滚动换血后首个思考增量需先在新草稿重开折叠块"，
        # 由 _apply 的 reasoning.delta 分支消费（见
        # _split_reasoning_fold_for_rollover）。
        self._reasoning_split_pending = False

    # ------------------------------------------------------------------
    # duck-typing 兼容层：未拦截的属性一律透传内部 builder
    # ------------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        # 仅在常规查找失败时调用；_builder 在 __init__ 已入实例字典，
        # 不存在递归风险（__init__ 未完成时 _builder 缺失会 AttributeError）。
        return getattr(self._builder, name)

    @property
    def pending_ui_events(self) -> int:
        """当前缓冲区中的事件数（观测用）。"""
        return len(self._buffer)

    # ------------------------------------------------------------------
    # Agent Event Stream 入口（§4 / §9）——同步、非阻塞
    # ------------------------------------------------------------------
    def emit(self, event_type: str, data: Any = None, **meta: Any) -> AgentEvent:
        """构造并提交一个 Agent Event；立即返回，绝不等待 UI。"""
        self._event_seq += 1
        event = AgentEvent(type=event_type, data=data, seq=self._event_seq, meta=meta)
        self.submit(event)
        return event

    def submit(self, event: AgentEvent) -> None:
        """Agent Event Stream 唯一消费入口。

        - 同步推进 Agent 状态机（纯观测，不影响渲染）；
        - 滚动换血期间（``_swap_scheduled``）：事件进入 :class:`DraftEventBuffer`（§9）；
        - 其余情况：立即应用到 builder（同步方法调用，不上网络）。
        """
        self._track_agent_phase(event.type)
        self._sync_draft_phase()
        if self._swap_scheduled:
            self._buffer.push(event)
            return
        self._apply(event)

    def _track_agent_phase(self, event_type: str) -> None:
        phase = _PHASE_BY_EVENT.get(event_type)
        if phase is not None:
            self.agent_phase = phase
        elif event_type == EventTypes.TOOL_END:
            self.agent_phase = AgentPhase.CONTINUE_GENERATION
        elif event_type == EventTypes.TURN_END:
            self.agent_phase = AgentPhase.DONE

    def _sync_draft_phase(self) -> None:
        """镜像 builder 的容量预警位（§6：只置 pending、不立即切换）。"""
        if (
            self.draft_phase == DraftPhase.ACTIVE
            and not self._swap_scheduled
            and getattr(self._builder, "_rollover_pending", False)
        ):
            self.draft_phase = DraftPhase.ROLLOVER_PENDING

    async def flush(self, force: bool = False) -> None:
        """透传刷新并在完成后同步 Draft 状态机镜像。

        容量预警（``_arm_rollover_if_needed``）在 flush 内部发生，此处是
        DraftPhase.ROLLOVER_PENDING 的最近观测点——镜像即时生效，调用方
        （如状态观测/测试）无需等到下一个事件提交。
        """
        await self._builder.flush(force=force)
        self._sync_draft_phase()

    # ------------------------------------------------------------------
    # 事件 → builder 渲染
    # ------------------------------------------------------------------
    def _apply(self, event: AgentEvent) -> None:
        """把单个事件应用到 builder（同步渲染；永不阻塞）。"""
        etype, data = event.type, event.data
        builder = self._builder
        if etype != EventTypes.REASONING_DELTA:
            # 任何非思考增量事件都终结"思考折叠续写"窗口（见
            # _split_reasoning_fold_for_rollover）：滚动后真正继续思考时，
            # 首个 reasoning.delta 会先重开折叠块；思考流正常结束或 Agent
            # 转入其他事件（content/tool/turn…）时窗口作废，避免之后误开
            # 空的续写折叠块。滚动换血期间事件只入缓冲、不经本方法，
            # 窗口在换血期间自然保持。
            self._reasoning_split_pending = False
        if etype == EventTypes.REASONING_START:
            self._open_stream_kind = "reasoning"
            builder.begin_stream_reasoning()
        elif etype == EventTypes.REASONING_DELTA:
            if self._reasoning_split_pending:
                # 思考折叠已在前一草稿提前收束且滚动换血完成：在新草稿
                # 重开思考折叠块，后续思考增量继续写入（用户看到的是
                # 新草稿中接续的折叠块，而不是散落的正文文本）。
                self._reasoning_split_pending = False
                self._open_stream_kind = "reasoning"
                builder.begin_stream_reasoning()
            builder.append_stream_delta(data)
            self._maybe_split_reasoning_fold_for_rollover()
        elif etype == EventTypes.REASONING_END:
            self._open_stream_kind = None
            builder.finalize_reasoning_block()
            self._handle_safe_boundary()
        elif etype == EventTypes.CONTENT_START:
            self._open_stream_kind = "content"
            builder.begin_stream_text()
        elif etype == EventTypes.CONTENT_DELTA:
            builder.append_stream_delta(data)
        elif etype == EventTypes.CONTENT_END:
            self._open_stream_kind = None
            self._handle_safe_boundary()
        elif etype == EventTypes.CONTENT_BLOCK:
            builder.add_text(data)
        elif etype == EventTypes.CONTENT_REPLACE_TAIL:
            builder.replace_trailing_text(
                data.get("original", ""), data.get("replacement", ""))
        elif etype == EventTypes.TOOL_START:
            builder.add_tool_item(**data)
        elif etype == EventTypes.TOOL_DELTA:
            kind = data.get("_kind", "args")
            if kind == "args":
                builder.update_tool_args(data["tool_id"], data.get("fn_args") or {})
            elif kind == "identity":
                builder.attach_stream_tool_identity(
                    data["item_id"], new_id=data.get("new_id"),
                    tool_type=data.get("tool_type"),
                )
            elif kind == "group_text":
                builder.append_to_current_tool_group_text(data.get("text", ""))
        elif etype == EventTypes.TOOL_RESULT:
            if data.get("_kind") == "preview":
                builder.update_tool_preview(
                    data["tool_id"], data.get("preview_html") or "",
                    summary=data.get("summary"),
                )
            else:
                builder.update_tool_item(
                    data["tool_id"], data.get("summary") or "",
                    data.get("details_html") or "",
                    status=data.get("status") or "done",
                )
        elif etype == EventTypes.TOOL_END:
            self._dispatch_finish_group(data)
            self._handle_safe_boundary()
        elif etype == EventTypes.TURN_END:
            # 终局边界不在事件路径里调度后台滚动：turn.end 由
            # finalize_turn() 同步收束（rollover(start_next_draft=False)，
            # 只永久化旧段、不创建新草稿——与终局轮 will_request_again
            # 语义一致）。若在此调度 start_next_draft=True 的后台滚动，
            # 终局会闪现只含尾段/占位的"幽灵草稿"（历史问题2）。
            pass
        elif etype == EventTypes.TOOL_GROUP_NEW:
            builder.start_new_tool_group()
        elif etype == EventTypes.UI_END_STREAM:
            builder.end_stream()
        else:  # pragma: no cover - 未知事件只记日志，绝不影响 Agent
            logger.debug("DraftManager 忽略未知事件: %s", event)

    def _dispatch_finish_group(self, group_idx: Any) -> None:
        """收束工具组：旧草稿换血导致的失效索引在此归位。

        批次组句柄跨滚动失效（滚动清空 _tool_groups 后批次组由回放重建，
        且必然是最新组）时，退化为收束最新未收束组（finish_group(None)）。
        """
        builder = self._builder
        groups = builder._tool_groups
        if isinstance(group_idx, int) and 0 <= group_idx < len(groups):
            builder.finish_group(group_idx)
        else:
            builder.finish_group(None)

    # ------------------------------------------------------------------
    # 便捷发射端：与 RichMessageBuilder 同签名，循环零成本切换
    #（滚动期间自动转缓冲，§9）
    # ------------------------------------------------------------------
    def begin_stream_reasoning(self) -> None:
        self.emit(EventTypes.REASONING_START)

    def begin_stream_text(self) -> None:
        self.emit(EventTypes.CONTENT_START)

    def append_stream_delta(self, delta: str) -> None:
        if not delta:
            return
        # 事件类型按当前开启的流标注（仅观测语义；渲染层不区分两者）。
        etype = (
            EventTypes.REASONING_DELTA
            if self._open_stream_kind == "reasoning"
            else EventTypes.CONTENT_DELTA
        )
        self.emit(etype, delta)

    def end_stream(self) -> str:
        """关闭当前流式块。返回值语义与 builder 一致；滚动期返回 ""。"""
        if self._swap_scheduled:
            self._buffer.push(AgentEvent(
                type=EventTypes.UI_END_STREAM, seq=self._next_seq()))
            return ""
        self._open_stream_kind = None
        return self._builder.end_stream()

    def end_stream_text(self) -> str:
        return self.end_stream()

    def add_text(self, text: str) -> None:
        if not text or not text.strip():
            return
        self.emit(EventTypes.CONTENT_BLOCK, text)

    def replace_trailing_text(self, original: str, replacement: str = "") -> bool:
        """撤回尾段文本（伪工具调用清理）。滚动期入缓冲并乐观返回 True。"""
        if not original:
            return False
        if self._swap_scheduled:
            self._buffer.push(AgentEvent(
                type=EventTypes.CONTENT_REPLACE_TAIL,
                data={"original": original, "replacement": replacement},
                seq=self._next_seq(),
            ))
            return True
        return self._builder.replace_trailing_text(original, replacement)

    def add_tool_item(self, tool_id: str, tool_type: str, summary: str,
                      action_description: Optional[str] = None,
                      search_query: Optional[str] = None,
                      domain: Optional[str] = None,
                      fn_args: Optional[dict] = None) -> None:
        self.emit(EventTypes.TOOL_START, {
            "tool_id": tool_id, "tool_type": tool_type, "summary": summary,
            "action_description": action_description,
            "search_query": search_query, "domain": domain,
            "fn_args": fn_args,
        })

    def update_tool_args(self, tool_id: str, fn_args: dict) -> None:
        self.emit(EventTypes.TOOL_DELTA, {
            "_kind": "args", "tool_id": tool_id, "fn_args": fn_args,
        })

    def attach_stream_tool_identity(self, item_id: str,
                                    new_id: Optional[str] = None,
                                    tool_type: Optional[str] = None) -> bool:
        self.emit(EventTypes.TOOL_DELTA, {
            "_kind": "identity", "item_id": item_id,
            "new_id": new_id, "tool_type": tool_type,
        })
        return True

    def update_tool_item(self, tool_id: str, summary: str,
                         details_html: str, status: str = "done") -> None:
        self.emit(EventTypes.TOOL_RESULT, {
            "tool_id": tool_id, "summary": summary,
            "details_html": details_html, "status": status,
        })

    def update_tool_preview(self, tool_id: str, preview_html: str,
                            summary: Optional[str] = None) -> None:
        self.emit(EventTypes.TOOL_RESULT, {
            "_kind": "preview", "tool_id": tool_id,
            "preview_html": preview_html, "summary": summary,
        })

    def append_to_current_tool_group_text(self, text: str) -> None:
        if not text:
            return
        self.emit(EventTypes.TOOL_DELTA, {
            "_kind": "group_text", "text": text,
        })

    def finish_group(self, group_idx: Optional[int] = None) -> None:
        """tool.end：收束工具组并触发安全点检查（§7 场景三）。"""
        self.emit(EventTypes.TOOL_END, group_idx)

    def begin_tool_batch(self) -> int:
        """工具批次开始：确保存在当前组并返回批次组句柄（滚动安全）。

        滚动已调度时**绝不**在旧草稿里创建组（否则换血即丢失，工具卡片
        永远停在 Running...），返回 :data:`_PENDING_GROUP_TOKEN`；组由
        缓冲回放中的 add_tool_item 在新草稿里惰性创建，
        :meth:`finish_tool_batch` 据句柄收束"批次实际落在的那个组"。
        """
        if self._swap_scheduled:
            return _PENDING_GROUP_TOKEN
        return self._builder._get_current_group()

    def finish_tool_batch(self, token: int) -> None:
        """工具批次结束：收束批次组（等价于原 finish_group(group_idx)）。"""
        if token == -1:
            return  # 批次没有对应组（空批次），与原 group_idx >= 0 守卫一致
        if token == _PENDING_GROUP_TOKEN:
            self.finish_group(None)
        else:
            self.finish_group(token)

    def start_new_tool_group(self) -> int:
        """显式新建工具组。返回值仅在非滚动期有意义（调用方不依赖）。"""
        self.emit(EventTypes.TOOL_GROUP_NEW)
        return getattr(self._builder, "_current_group_idx", -1)

    def _next_seq(self) -> int:
        self._event_seq += 1
        return self._event_seq

    # ------------------------------------------------------------------
    # 安全点 API（§7 / §8）：替代原先 await rollover_at_turn_boundary
    # ------------------------------------------------------------------
    def on_stream_block_closed(self, kind: str) -> None:
        """流式块闭合安全点（switch_stream 块边界检查点①②的非阻塞替代）。

        ``kind`` 为刚闭合的块类别（reasoning / content）。发射对应
        ``*.end`` 事件——若满容量且无守卫阻塞，后台调度滚动；Agent 立即
        继续，不做任何等待。
        """
        if kind == "reasoning":
            self.emit(EventTypes.REASONING_END)
        elif kind == "content":
            self.emit(EventTypes.CONTENT_END)

    def on_round_boundary(self) -> None:
        """回合级安全检查点（原 post-stream rollover 检查点的非阻塞替代）。

        先补发仍开启的流式块的 ``*.end`` 事件（事件流完整性——最后一个
        块不会再经历 switch_stream 转换），再执行安全点检查。
        """
        kind = self._open_stream_kind
        if kind == "reasoning":
            self.emit(EventTypes.REASONING_END)
        elif kind == "content":
            self.emit(EventTypes.CONTENT_END)
        self._handle_safe_boundary()

    def on_tool_batch_end(self) -> None:
        """tool.end 安全检查点（原"工具批次后 rollover"的非阻塞替代）。

        工具结果此刻已全部进入 conversation context，下一轮 LLM 请求
        随即发出；满容量时滚动在后台进行，后续轮次的事件经缓冲衔接。
        """
        self._handle_safe_boundary()

    async def finalize_turn(self) -> bool:
        """turn.end 终局安全点：收束在途滚动与缓冲，再永久化旧段。

        与中途安全点不同，终局必须**同步等待**：回合已结束，不存在
        "下一轮更快"的收益；且 get_ai_response 随后将基于构建器终态
        构建最终交付 HTML，任何未完成的换血与缓冲事件都必须先落地。

        返回值与 builder.rollover_at_turn_boundary(start_next_draft=False)
        一致（是否发生了旧段永久化）。
        """
        self.emit(EventTypes.TURN_END)
        await self._await_pending_rollover()
        self._replay_buffer()
        ok = await self._builder.rollover_at_turn_boundary(start_next_draft=False)
        self.draft_phase = DraftPhase.CLOSED
        self.agent_phase = AgentPhase.DONE
        return ok

    # ------------------------------------------------------------------
    # 思考折叠提前收束（草稿预警后的超长思考续写）
    # ------------------------------------------------------------------
    def _maybe_split_reasoning_fold_for_rollover(self) -> None:
        """草稿预警后思考流仍在输出：提前收束思考折叠并调度滚动。

        原行为：滚动只在安全边界（reasoning.end / content.end / tool.end）
        调度——超长思考必须整段输出完毕才有机会换草稿，预警后思考继续
        原地写入同一折叠块，草稿一路膨胀，极端时超出草稿上限、整帧被拒。

        现行为：思考流期间一旦容量预警置位（``_rollover_pending``），在
        最近的思考增量处提前收束当前折叠块（等价于一个合成 reasoning.end
        安全点），由既有安全点路径调度后台滚动；滚动换血后首个思考增量
        在新草稿重开折叠块继续写入。Agent 侧思考流本身不受影响——后续
        增量仍按 reasoning.delta 记账，真实 reasoning.end 的语义与时序
        保持不变。

        守卫与 ``_handle_safe_boundary`` 的调度条件保持一致：静默构建器、
        已有在途滚动、容量未预警、存在未收束工具组时都不触发——这些情形
        下提前收束不会带来滚动，只会把思考拆成同草稿内的两个折叠块。
        "滚动绝不发生在 reasoning block 中间"的不变量依旧成立：先提交并
        复位流指针使折叠块定格，再经合成 reasoning.end 走安全点。
        """
        if self._open_stream_kind != "reasoning":
            return
        builder = self._builder
        if getattr(builder, "silent", False):
            # 静默构建器无可见草稿、永不滚动（与 _handle_safe_boundary 一致）。
            return
        if self._swap_scheduled or (
            self._rollover_task is not None and not self._rollover_task.done()
        ):
            return  # 幂等：已有滚动在排队/执行
        if not getattr(builder, "_rollover_pending", False) or builder._stop_flush:
            return  # 未预警或草稿已停止刷新：保持原行为，等真实安全边界
        if builder._has_pending_tool_group():
            return  # 工具组未收束：滚动被推迟到 tool.end，提前收束无收益
        self._split_reasoning_fold_for_rollover()

    def _split_reasoning_fold_for_rollover(self) -> None:
        """提前收束思考折叠并经安全点调度滚动（合成 reasoning.end）。"""
        builder = self._builder
        logger.info(
            "草稿预警后提前收束思考折叠，调度滚动续写: chat=%s draft=%s",
            builder.chat_id, builder.draft_id,
        )
        # ① 提前结束思考折叠：提交未落块的思考增量并复位流指针——当前
        #    草稿中的思考折叠块至此定格，后续思考增量不再写入。
        builder.end_stream()
        # ② 事件流完整性：补发 reasoning.end 安全点事件。折叠块在旧草稿
        #    以完整结构定格；安全点检查随即调度后台滚动，此后到达的事件
        #    进入缓冲（§9）。
        self.emit(EventTypes.REASONING_END, fold_split=True)
        # ③ Agent 侧思考流并未结束：恢复流类别标注（reasoning.end 的应用
        #    路径会把它置空），后续增量仍按 reasoning.delta 记账；并标记
        #    滚动换血后首个思考增量在新草稿重开折叠块续写思考内容。
        self._open_stream_kind = "reasoning"
        self._reasoning_split_pending = True

    # ------------------------------------------------------------------
    # 安全点判定与后台滚动调度
    # ------------------------------------------------------------------
    def _handle_safe_boundary(self) -> None:
        """安全切换点统一入口（非阻塞）。

        设计为“预警状态 + 最近安全边界”：

        1. 预警通常由异步 flush 发现并置位；
        2. 到达任一安全边界时，再同步补做一次容量扫描，避免“内容已经
           超阈值，但异步 flush 尚未来得及 arm”而错过最近退出点；
        3. 一旦预警成立，就在当前安全边界兑现。

        对工具批次尤其重要：最后一个 tool result 已写入 builder 后，
        ``tool.end`` 本身就是这一轮完整工具批次的最近安全退出点。
        此处必须先补做预警扫描，再判断是否需要 rollover，否则会把切换
        错过到下一轮 reasoning.end / content.end。
        """
        builder = self._builder
        if getattr(builder, "silent", False):
            # 静默构建器无可见草稿；其 _rollover_pending 恒 False，此处双保险。
            return
        if self._swap_scheduled or (
            self._rollover_task is not None and not self._rollover_task.done()
        ):
            return  # 幂等：已有滚动在排队/执行

        # 关键修复：不要只读取旧的 _rollover_pending。
        # 容量预警通常在异步 flush() 中 arm，但 tool.end / text.end /
        # reasoning.end 是“最近退出点”，如果这里不立即补扫，就会错过
        # 这个安全边界，直到下一次 flush 才把 pending 置上，最终只能等
        # 更晚的 reasoning.end / content.end。
        try:
            builder._arm_rollover_if_needed()
        except Exception:
            # 安全点不应因为诊断性的容量扫描异常而阻断 Agent；保留原有
            # pending 状态，下一次 flush / 安全点仍可继续尝试。
            logger.debug(
                "安全边界容量预警扫描失败: chat=%s draft=%s",
                getattr(builder, "chat_id", None),
                getattr(builder, "draft_id", None),
                exc_info=True,
            )

        if not getattr(builder, "_rollover_pending", False):
            return
        if builder._has_pending_tool_group():
            # 工具组未收束：绝不把 tool call / tool result 拆进两个草稿（§7）。
            self.draft_phase = DraftPhase.WAIT_SAFE_POINT
            logger.debug(
                "滚动等待工具组收束（WAIT_SAFE_POINT）: chat=%s draft=%s",
                builder.chat_id, builder.draft_id,
            )
            return
        self._schedule_rollover()

    def _schedule_rollover(self) -> None:
        """调度后台滚动。同步置位 _swap_scheduled：此后事件一律入缓冲。"""
        self._swap_scheduled = True
        self.draft_phase = DraftPhase.WAIT_SAFE_POINT
        self._rollover_task = asyncio.create_task(self._execute_rollover())
        logger.info(
            "草稿滚动已调度至后台安全点（Agent 不等待）: chat=%s draft=%s buffer=0",
            self._builder.chat_id, self._builder.draft_id,
        )

    async def _execute_rollover(self) -> None:
        """后台执行草稿滚动（唯一允许等待网络的上游任务）。"""
        builder = self._builder
        try:
            # 双重检查：任务真正获得调度时状态可能已变化（例如工具批次
            # 在任务启动前同步创建了新的未收束工具组）。被守卫拦下时
            # 放弃本次滚动，缓冲回放至当前草稿，等待下一个安全点重试。
            if (
                builder._stop_flush
                or not builder._rollover_pending
                or builder._has_pending_tool_group()
            ):
                return
            try:
                swapped = await builder.rollover_at_turn_boundary(
                    start_next_draft=True)
            except asyncio.CancelledError:
                raise
            if swapped:
                self.draft_phase = DraftPhase.NEW_DRAFT
                logger.info(
                    "后台草稿滚动完成: chat=%s new_draft=%s",
                    builder.chat_id, builder.draft_id,
                )
            else:
                # 永久化失败等：保留 pending，由下一个安全点重试。
                logger.warning(
                    "后台草稿滚动未完成，保留预警等待下一安全点: chat=%s draft=%s",
                    builder.chat_id, builder.draft_id,
                )
        except asyncio.CancelledError:
            # 打断路径：builder 的取消分支已恢复交接缓冲；缓冲事件由
            # stop_flush_loop 回放到（恢复后的）当前草稿。
            raise
        except Exception:
            logger.exception("后台草稿滚动异常（不影响 Agent 执行）")
        finally:
            self._swap_scheduled = False
            self._replay_buffer()
            if self.draft_phase in (DraftPhase.NEW_DRAFT, DraftPhase.WAIT_SAFE_POINT):
                self.draft_phase = (
                    DraftPhase.ACTIVE
                    if not getattr(builder, "_rollover_pending", False)
                    else DraftPhase.ROLLOVER_PENDING
                )

    def _replay_buffer(self) -> None:
        """把缓冲事件按原序回放到当前（新）草稿（§9 flush）。"""
        events = self._buffer.flush()
        if not events:
            return
        logger.debug("回放 UI 事件缓冲: chat=%s events=%s", self._builder.chat_id, len(events))
        for event in events:
            # 经 submit 回放：若回放中的边界事件再次调度滚动（级联滚动），
            # 后续事件会被重新缓冲，顺序与归属始终正确。
            self.submit(event)

    async def _await_pending_rollover(self) -> None:
        """等待在途后台滚动结束（终局收束 / 打断清理用）。"""
        task = self._rollover_task
        if task is None or task.done():
            return
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("等待后台滚动结束时出现异常（已按失败处理）", exc_info=True)

    # ------------------------------------------------------------------
    # 生命周期覆盖：stop_flush_loop 同时取消后台滚动
    # ------------------------------------------------------------------
    async def stop_flush_loop(self) -> None:
        """停止刷新循环（扩展：取消在途滚动 → 回放缓冲 → 委托 builder）。

        打断路径（get_ai_response 的 CancelledError 分支）经此收束：
        滚动任务被取消后 builder 会恢复交接缓冲，缓冲事件回放至恢复后
        的当前草稿，随后 finalize_interrupted_draft 固定的可见进度不缺页。
        """
        await self._cancel_rollover_task()
        self._replay_buffer()
        await self._builder.stop_flush_loop()
        self.draft_phase = DraftPhase.CLOSED

    async def _cancel_rollover_task(self) -> None:
        task = self._rollover_task
        self._rollover_task = None
        self._swap_scheduled = False
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("后台滚动任务停止时出现异常（可忽略）", exc_info=True)
