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

流式文本的实时保全（2026-09 新增）
==================================

旧问题：纯文本流式输出中途被打断时，``content_acc`` 是循环局部变量、
从未写入 journal——草稿层（用户可见）保全了内容，历史层（模型记忆）
完全为空。修复后（见 ``ai/bridge_common.py`` 的 ``LiveAssistantSlot``）：
四条 agentic 循环在流式期间把文本/思考增量**实时同步**进 journal 里的
占位 assistant 消息，打断发生在任何时间点都能被本模块保全。配套防御：

- ``_normalize_journal`` 剔除"无正文且无 tool_calls"的空 assistant 占位
  （模型一字未吐即被打断；Codex #22602——空 assistant 消息会让严格网关
  拒绝下一轮请求）。"只吐了思考、没吐正文"的占位同样剔除：思考块按
  官方原则不可部分保全，且 content=null 的出站形状与上述雷同。
- 流式中途未完成的 tool_call 参数 JSON 由循环侧**整体不写入**
  （LiveAssistantSlot 只在 finalize 写 tool_calls），journal 不会出现
  "有 tool_use 却永远没有配对 tool_result"的悬空状态。

五阶段打断规范（2026-09 二期，本次重构）
========================================

按打断发生时的物理阶段执行确定性动作，数据保留基准"文本看前端、
数据看后台"：

- **阶段 1（思考推演中）**：残缺思考**全量丢弃**（
  ``trim_interrupted_stream`` 剥离 ReasoningBlock）——未完成的逻辑推导
  前提已失效，残缺思考会污染模型下一轮隐空间；
- **阶段 2（文本输出中）**：文本按**前端渲染确认游标**物理截断——后端
  超前生成、用户尚未在草稿上看到的文本一律裁掉（"用户没看到的等于
  模型没说过的"，对齐认知现场，防"我明明解释过"幻觉）。游标由
  RichMessageBuilder 维护（最后一帧成功送达时刻的回合累计可见文本
  字符数），经 ``render_cursor_box`` 引用注册进 ``_InFlightEntry``；
  静默回合（/show off）不渲染草稿 → 无游标 → 不裁剪（保全后端全量以
  衔接进度）；
- **阶段 3（工具参数生成中）**：半截 JSON 整体剥离（循环侧已不写入）；
- **阶段 4（工具执行中）**：只读工具——取消传播直接拒断底层网络请求，
  占位回执带 ``aborted`` 状态语义；写操作（bash / text_editor / memory /
  todo，见 ``ai._constants.DETACHED_ON_INTERRUPT_TOOLS``）——
  ``asyncio.shield`` 让执行脱离主进程后台死等确切终态（成功/失败/回滚），
  终态由 ``writeback_detached_tool_result`` 轮询回写进历史（原地替换占位
  tool 消息的 content）；
- **阶段 5（工具结果刚返回）**：调用声明与真实数据结果全量保留（既有
  ``_batch_completed_results`` 回填机制）。

user 消息落位（OpenAI 格式）
============================

- 打断发生在任何 assistant 输出之前：journal 为空，历史末尾仍是上一条
  user 消息 → 新 user 消息由 ``persist_user_message_entry`` **合并**进
  该消息（content 以空行拼接；媒体附件按 kind 归一——同类升级为
  photo_group / video_group / document_group 数组，混合 kind / 多音频
  保留 attachments 列表由解析器逐个解析），避免连续两条 user；
- 上一轮**请求失败**（异常 / IMAGE_ERROR / VIDEO_ERROR / 空响应等，
  历史末尾残留未获回应的 user 消息且被打上 TURN_FAILED_FLAG）：新
  user 消息**整体替换**该失败轮消息而不合并——重试语义下每条文本、
  每张图片只保留新消息的一份，绝不随重试次数叠加（旧实现把重发内容
  反复合并进同一条 user 消息，文本越拼越长、同一张参考图重复出现，
  "越积攒越多"）。新消息不带媒体时，把失败轮的媒体原样搬移一份过来
  （不带旧文本），保证用户上传的图片/文档不因一次请求失败从历史中
  消失，原生图像模型的重试也仍能拿到参考图；
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

静默回复交付标记（按事件源区分默认值）
================================

``/show off``（静默模式）下，模型通过 ``deliver_reply`` 工具的 send 布尔
参数选择是否把最终内容发给用户——send=true 时发送的是 agent 轮次
最后一条助手消息的 content 字段（由 ``ai/tool_call_loop`` 在 journal 里
回溯解析后传入 executor，不含 reasoning 等其他字段）。send 的**缺省值
（不填）按事件源区分**，在每轮 agent 开始时由 ``reset_turn_delivery_state``
重置：

- **USER 回合**（用户主动发消息）：默认 **true**——不填按发送处理；
  模型整轮都不调用 deliver_reply 时，``get_ai_response`` 收尾会按默认
  交付兜底发送最终回复（用户主动提问理应收到回答）——兜底发送的内容
  与工具交付**同源**：同为 agent 轮次最后一条非空 assistant 消息的
  content 本身（同样经 sendRichMessage 直发，不使用整轮草稿累积，
  也不附带中间轮次的过程文本与工具卡片）；只有显式填 ``send=false``
  才会标记 ``_reply_suppressed``，本轮对用户完全静默。
- **TIMER 回合**（后台主动巡检）：默认 **false**——不填 / false / 不调用
  均不发送，与旧行为一致，必须显式填 ``send=true`` 才发送；收尾无
  兜底直发。

executor 发送成功后调用 ``mark_reply_delivered`` 记录"本轮已经主动交付过"
（对 USER 回合意味着收尾不再兜底）；``run_one`` 在显式 ``send=false`` 时
调用 ``mark_reply_suppressed`` 记录"本轮显式抑制"。``get_ai_response``
收尾时用 ``pop_reply_delivered`` / ``pop_reply_suppressed`` 读取并清除，
据此决定 USER 静默回合是否需要兜底发送。三类标记都是轮次开始时重置，
上一轮的取值不会泄漏到本轮（也顺带清理了异常/打断路径残留的旧标记）。

锁顺序约定（防 ABBA 死锁）
==========================

- 注册表操作全部是同步原子操作（append / pop / filter，无 await 窗口），
  **不**与 chat 锁交叉持有；
- 持久化路径（finalize / note_turn_persisted 的调用方）先完成注册表
  原子操作，再获取 chat 锁写历史；
- ``update_conversation_and_ledger`` 持 chat 锁期间调用
  ``note_turn_persisted``（只做注册表原子操作），两者不会形成
  跨路径的锁等待。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from state import (
    get_chat_lock,
    get_or_init_context,
)
from utils import get_logger
from core.messages import Message, ReasoningBlock

logger = get_logger(__name__)

__all__ = [
    "INTERRUPTED_TOOL_PLACEHOLDER",
    "DETACHED_TOOL_PLACEHOLDER",
    "LIVE_STREAM_FLAG",
    "SYNTHETIC_ASSISTANT_FLAG",
    "EARLY_PERSIST_FLAG",
    "EARLY_PERSIST_MODE",
    "EARLY_PERSIST_TS",
    "TURN_FAILED_FLAG",
    "register_inflight_turn",
    "attach_render_cursor",
    "trim_interrupted_stream",
    "writeback_detached_tool_result",
    "note_turn_persisted",
    "finalize_interrupted_turn",
    "finalize_pending_turns",
    "drain_completed_turns",
    "persist_salvaged_journal",
    "persist_user_message_entry",
    "undo_early_persist",
    "mark_failed_unanswered_user",
    "reset_turn_delivery_state",
    "default_send_value",
    "mark_reply_delivered",
    "pop_reply_delivered",
    "mark_reply_suppressed",
    "pop_reply_suppressed",
]

# 打断时未执行/被取消的【只读】工具调用的占位回执（tool 消息 content）。
# 五阶段规范·阶段4a：必须带 aborted（已中止）状态语义，保证调用声明与
# 回执绝对成对闭合——网关不会因悬空 tool_use 报 400，模型也不会把
# "没有结果"误读成"执行成功"。
INTERRUPTED_TOOL_PLACEHOLDER = (
    "用户打断，工具调用已中止（aborted）：执行未完成、没有结果返回；"
    "请根据新指令决定是否需要重新发起。"
)

# 打断时仍在执行的【写操作】工具的占位回执（阶段4b）：写操作不可安全
# 中止（中止≠回滚），已脱离主进程后台死等确切终态；终态到达后由
# writeback_detached_tool_result 原地替换本占位——回填前模型不得假定成败。
DETACHED_TOOL_PLACEHOLDER = (
    "用户打断，但该工具是不可中止的写操作：已脱离本轮在后台继续执行，"
    "确切终态（成功/失败/回滚）稍后自动回填本条目；在回填前请勿假定其成败。"
)

# 流式直播中标记：LiveAssistantSlot 占位在流式期间携带，finalize（流
# 正常结束、参数定型）时摘除。打断保全的字段级裁剪（剥残缺思考 + 按
# 渲染游标截断文本）**只作用于仍带此标记的占位**——已定稿消息的思考与
# 文本都已完整，绝不误伤。meta 永不出站（见 core/messages.py）。
LIVE_STREAM_FLAG = "__apitc_live_stream__"

# 合成 assistant 消息标记（媒体生成进度占位等）：文本不是流式渲染产物、
# 从未进入渲染游标计数——trim 计算"前文合计"时排除，避免挤压当前
# 直播占位的截断预算。
SYNTHETIC_ASSISTANT_FLAG = "__apitc_synthetic__"

# user 消息提前持久化标记：update_conversation_and_ledger 见到此标记跳过 append。
EARLY_PERSIST_FLAG = "__apitc_early_persisted__"

# 提前持久化的落库方式（appended=独立追加 / merged=合并进上一条未回应 user）：
# 消费型接管（media_wizard 收素材）与 pre_flight 拒绝分支据此决定是否回滚。
EARLY_PERSIST_MODE = "__apitc_early_persist_mode__"
# 提前持久化的身份标记：与消息一起写入历史 Message.meta，undo 时校验
# 历史末尾那条 user 消息确实是本次写入的那一条（防误删并发写入的消息）。
# meta 永不出站（见 core/messages.py），不会进入请求体。
EARLY_PERSIST_TS = "__apitc_early_persist_ts__"

# 引用回复前缀标记（与 app_turns/media_wizard 同值；此处复制以避免循环导入）。
# 引用前缀只服务当前请求的上下文提示（拼在 user content 开头），持久化历史
# 时必须剥离，否则每轮请求都会把引用全文重发给模型，污染上下文并浪费 token。
REPLY_MARKER = "💡 引用回复:"


def _strip_reply_prefix(content: str) -> str:
    """剥离 user content 开头的引用回复前缀块，返回净文本。

    前缀由 app_turns._get_reply_context 拼接，形态固定为
    f"{REPLY_MARKER}\n> {quote}\n\n" 且位于 content 开头。这里只把开头
    的前缀块整体剥掉；不能用 split(REPLY_MARKER)[-1]——快速连发的
    合并消息里旧文本在标记之前，split[-1] 会把旧文本一并丢弃。
    """
    if not isinstance(content, str) or not content.startswith(REPLY_MARKER):
        return content
    lines = content[len(REPLY_MARKER):].split("\n")
    idx = 0
    # 跳过紧随标记的空行与 "> " 引用行；前缀块以空行与正文分隔。
    while idx < len(lines) and (lines[idx].strip() == "" or lines[idx].startswith(">")):
        idx += 1
    return "\n".join(lines[idx:]).strip()

# 请求失败标记：轮次以异常/媒体错误/空响应告终（无任何 assistant 输出）时，
# 由 get_ai_response 的失败路径打在历史末尾那条未获回应的 user 消息上。
# 下一条 user 消息到来时 persist_user_message_entry 看到该标记走
# "替换"而不是"合并"——重试不与上一轮的文本/图片叠加。
# 标记在读取时被 pop 消费；历史中部的陈旧标记永远不会被读到（只在
# 历史末尾 role=user 时才检查），无副作用。
TURN_FAILED_FLAG = "__apitc_turn_failed__"

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
    # 渲染确认游标引用（builder 的 render_cursor_box，单元素列表）。
    # register 时 builder 尚未创建，由 get_ai_response 在 builder 就绪后
    # 经 attach_render_cursor 回填；打断保全时零拷贝读取最新值（builder
    # 每帧送达后原地更新 box[0]）。None / 缺失 = 无渲染基准（静默回合），
    # trim 不裁剪文本。
    render_cursor_ref: Optional[list] = None


# chat_id -> 该 chat 进行中（或尚未注销）的轮次登记，按注册顺序排列。
# 注册表只做同步原子操作（append / pop / filter），asyncio 单线程模型下
# 不存在中途让出，故无需 asyncio.Lock——这也让"写历史 + 注销登记"
# 可以在 chat 锁内连成一个无取消窗口的原子区间（见 note_turn_persisted）。
_inflight: dict[int, list[_InFlightEntry]] = {}

# chat_id -> 本轮是否已通过 deliver_reply 主动交付过回复。
_reply_delivered: set[int] = set()

# chat_id -> 本轮模型是否显式抑制过交付（deliver_reply send=false）。
# 仅对静默 USER 回合有意义：显式抑制后收尾不再兜底发送。
_reply_suppressed: set[int] = set()

# chat_id -> 本轮 deliver_reply 的 send 缺省值（在集合中 = 缺省 true）。
# agent 轮次开始时由 reset_turn_delivery_state 重置：静默 USER 回合
# 重置为 true（默认交付，收尾有兜底），静默 TIMER 回合 / 非静默回合
# 重置为 false（保持旧行为）。
_default_send: set[int] = set()


# =====================================================================
# 登记与注销
# =====================================================================
def _current_task() -> Optional[asyncio.Task]:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _read_render_cursor(ref: Optional[list]) -> Optional[int]:
    """读取渲染确认游标的当前值（异常客错 → None = 不裁剪）。"""
    if not ref:
        return None
    try:
        value = ref[0]
    except Exception:
        return None
    return value if isinstance(value, int) and value >= 0 else None


def attach_render_cursor(chat_id: int, journal: list, cursor_ref: Optional[list]) -> None:
    """把 builder 的渲染确认游标绑定到该轮次的登记条目（同步原子）。

    register_inflight_turn 时 builder 尚未创建（登记在最前、builder 在
    预处理后）；get_ai_response 在 builder 就绪后调用本函数按 journal
    对象身份回填。静默回合传 None（无渲染基准，保全时不裁剪文本）。
    """
    if chat_id is None or journal is None:
        return
    entries = _inflight.get(chat_id)
    if not entries:
        return
    for entry in entries:
        if entry.journal is journal:
            entry.render_cursor_ref = cursor_ref


def trim_interrupted_stream(journal: list, render_cursor: Optional[int]) -> bool:
    """打断保全的字段级裁剪（五阶段规范：阶段1/2/3 的落地）。

    只处理 journal 末尾**仍带 LIVE_STREAM_FLAG** 的直播占位（正在流式的
    assistant 消息）；已定稿消息（流正常结束、含 tool_calls）的思考与
    文本都是完整的，绝不触碰。判断依据是标记而非形状——纯文本轮的
    定稿消息同样无 tool_calls，形状上与直播占位无法区分。

    - 阶段 1（思考推演中打断）：ReasoningBlock 全量丢弃——未完成的
      逻辑推导前提已失效，残缺思考会污染模型下一轮的隐空间；
    - 阶段 2（文本输出中打断）：TextBlock 按前端渲染确认游标物理截断
      ——后端超前生成、用户尚未在草稿上看到的文本一律裁掉（"用户没
      看到的等于模型没说过的"，对齐认知现场，防"我明明解释过"幻觉）。
      render_cursor 为 None（静默回合，无渲染基准）时不裁剪，保全后端
      全量以衔接进度；
    - 阶段 3（工具参数生成中打断）：半截 JSON 本就未写入占位（循环侧
      整体丢弃），此处只剩文本与思考的处置。

    游标是**回合累计口径**（含此前各轮已渲染文本与非流式 add_text），
    当前占位的保留预算 = render_cursor - 之前所有流式 assistant 消息
    文本之和；SYNTHETIC_ASSISTANT_FLAG 标记的合成占位（媒体生成进度等
    非流式渲染文本）从未进入游标计数，不计入合计——否则会挤压当前
    占位的预算、把用户已看到的流式文本多裁掉。

    原地修改（打断时刻的固化语义）；裁剪/剥离后消息变空由
    ``_normalize_journal`` 的 _is_persistable_assistant 过滤兑底。
    返回是否实际触到了直播占位。
    """
    target_idx: Optional[int] = None
    for i in range(len(journal) - 1, -1, -1):
        msg = journal[i]
        if isinstance(msg, Message) and msg.role == "assistant":
            if bool(msg.meta.get(LIVE_STREAM_FLAG)):
                target_idx = i
            break  # 只看最后一条 assistant：非直播占位（已定稿）不动
    if target_idx is None:
        return False

    msg = journal[target_idx]

    # 阶段 2：文本按渲染游标物理截断（无基准 → 不裁）。
    if render_cursor is not None:
        prior_chars = 0
        for j in range(target_idx):
            prev = journal[j]
            if (
                isinstance(prev, Message)
                and prev.role == "assistant"
                and not prev.meta.get(SYNTHETIC_ASSISTANT_FLAG)
            ):
                prior_chars += len(prev.text())
        budget = render_cursor - prior_chars
        text = msg.text()
        if len(text) > budget:
            msg.set_text(text[:max(0, budget)])
            logger.info(
                "[turn-recovery] 打断裁剪：直播文本 %s 字符 → 保留 %s 字符"
                "（渲染游标=%s，前文合计=%s）——用户未看到的文本不进历史",
                len(text), max(0, budget), render_cursor, prior_chars,
            )

    # 阶段 1：残缺思考全量丢弃（放在截断之后：set_text 保留非文本块）。
    reasoning = msg.reasoning()
    if reasoning:
        msg.blocks = [b for b in msg.blocks if not isinstance(b, ReasoningBlock)]
        logger.info(
            "[turn-recovery] 打断裁剪：丢弃残缺思考 %s 字符（推演前提已失效）",
            len(reasoning),
        )
    # 直播标记随之摘除：裁剪已落地，防后续重复处理。
    msg.meta.pop(LIVE_STREAM_FLAG, None)
    return True


def _pop_registry_entries(chat_id: int, *, expect_task: Optional[asyncio.Task] = None, only_done: bool = False) -> list[_InFlightEntry]:
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
    """找出 journal 中没有配对 tool 消息的 (tool_call_id, name) 列表。

    重构说明（Internal Message）：journal 统一为 Message 列表（兼容旧
    dict 直通），assistant 消息经 tool_calls() 读取结构化 ToolCallBlock。
    """
    def _role(m):
        return m.role if isinstance(m, Message) else m.get("role")

    paired: set[str] = set()
    for msg in journal:
        if _role(msg) != "tool":
            continue
        if isinstance(msg, Message):
            tr = msg.tool_result_block()
            if tr is not None and tr.tool_call_id:
                paired.add(tr.tool_call_id)
        elif isinstance(msg, dict):
            tc_id = msg.get("tool_call_id")
            if isinstance(tc_id, str) and tc_id:
                paired.add(tc_id)
    unpaired: list[tuple[str, str]] = []
    seen: set[str] = set()
    for msg in journal:
        if _role(msg) != "assistant":
            continue
        if isinstance(msg, Message):
            for tc in msg.tool_calls():
                if not tc.id or tc.id in paired or tc.id in seen:
                    continue
                seen.add(tc.id)
                unpaired.append((tc.id, str(tc.name or "unknown")))
            continue
        if not isinstance(msg, dict):
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


def _is_persistable_assistant(msg: Any) -> bool:
    """判断 assistant 消息是否携带可出站的实质内容（打断保全过滤）。

    Codex #22602 教训（问题排查文档引用）：把 "content=null 且
    tool_calls=null" 的半成品 assistant 消息存进历史，下一轮请求会被
    严格网关直接拒绝（Invalid assistant message: content or tool_calls
    must be set），整个对话线程报废。

    本过滤器在保全阶段（_normalize_journal）把这类消息剔除，覆盖两类
    来源：

    - LiveAssistantSlot 的空占位：打断发生在模型输出任何内容之前
      （含"只吐了思考、没吐正文"的半成品——思考块按官方原则不可部分
      保全，无续写价值，剔除后 content 仍为 null 的形状一并消失）；
    - 其它路径意外产生的空 assistant 消息（防御性兜底）。

    正常路径不受影响：四条循环的终局兜底（"（模型未返回任何内容）" /
    _tool_limit_summary）保证正常写入的 assistant 消息必有正文。
    """
    if isinstance(msg, Message):
        if msg.role != "assistant":
            return True
        return bool(msg.text().strip() or msg.tool_calls())
    if isinstance(msg, dict):
        if msg.get("role") != "assistant":
            return True
        content = msg.get("content")
        has_text = (
            (isinstance(content, str) and bool(content.strip()))
            or (isinstance(content, list) and bool(content))
        )
        return has_text or bool(msg.get("tool_calls"))
    return True


def _normalize_journal(journal: list) -> list:
    """返回补齐占位 tool 消息后的 journal 副本（原列表不被修改）。

    打断保全过滤（改动点1第4项）：先剔除"无正文且无 tool_calls"的空
    assistant 占位——模型一字未吐就被打断时，journal 里的实时占位不落
    历史（Codex #22602 防御，见 _is_persistable_assistant）。剔除只发生在
    保全副本上，内存中的 journal 原列表不动（LiveAssistantSlot 仍持有
    同一对象，轮次若继续正常推进不受影响）。
    """
    normalized = [
        m for m in journal
        if _is_persistable_assistant(m)
    ]
    if len(normalized) != len(journal):
        logger.debug(
            "[turn-recovery] 打断保全剔除空 assistant 占位 %s 条（模型无任何输出）",
            len(journal) - len(normalized),
        )
    placeholders = [
        Message.tool_result(tc_id, name, INTERRUPTED_TOOL_PLACEHOLDER)
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
            # 把登记放回去，让下一次 finalize 继续处理。append 是同步
            # 原子操作（无 await 窗口），与 register_inflight_turn 同一
            # 取消安全模式。
            _inflight.setdefault(entry.chat_id, []).append(entry)
            raise
        except Exception:
            logger.debug("[turn-recovery] 等待旧轮次收尾失败（按当前快照保全）", exc_info=True)
    # 五阶段规范·阶段1/2：等待旧任务收尾后（builder 渲染游标已定格）
    # 先做字段级裁剪（剥残缺思考 + 按前端渲染游标截断直播文本），
    # 再补齐结构与落库。无渲染基准（静默回合 / 未 attach）时只剥思考。
    trim_interrupted_stream(entry.journal, _read_render_cursor(entry.render_cursor_ref))
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
    # 异常路径同样做字段级裁剪：流断了 → 末尾直播占位的思考是残缺的、
    # 文本只该保留到用户已渲染的部分（与打断同语义）。渲染游标按
    # journal 身份从注册表回查（builder 侧 attach 过）。
    trim_interrupted_stream(journal, _render_cursor_of_journal(journal))
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


def _render_cursor_of_journal(journal: list) -> Optional[int]:
    """按 journal 对象身份回查渲染游标（异常路径用，找不到返回 None）。"""
    for entries in _inflight.values():
        for entry in entries:
            if entry.journal is journal:
                return _read_render_cursor(entry.render_cursor_ref)
    return None


# =====================================================================
# 脱离工具（写操作）终态回写
# =====================================================================
# 回写轮询间隔：历史中该 tool_call_id 的 tool 消息可能尚未写入（打断方
# 保全 / 正常收尾都在异步进行），等待其出现后原地替换。
_DETACHED_WRITEBACK_POLL = 2.0
# 回写等待上限：超过则放弃（该轮次历史本身未能落库，无处可写）。
_DETACHED_WRITEBACK_WAIT = 600.0


async def writeback_detached_tool_result(
    chat_id: int, tool_call_id: str, tool_name: str, content: str,
) -> bool:
    """把脱离工具（写操作）的最终结果回写进持久历史（阶段4b）。

    写操作（转账/写库等）不可安全中止：主进程取消后由后台任务死等
    确切终态（成功/失败/回滚），拿到后经本函数写入上下文——历史中
    该 tool_call_id 的 tool 消息（打断时的 DETACHED 占位 / 超时的
    "仍在执行"标记）被**原地替换**为真实终态，调用声明与回执的配对
    结构不变，只有 content 换血。

    时序：该 tool 消息此刻可能尚未写入历史（打断方还在等旧任务收尾 /
    正常轮次还在跑）——轮询等待其出现后替换，最多等
    ``_DETACHED_WRITEBACK_WAIT`` 秒（超时放弃并告警；那种场景下
    历史本身就没落库，回写无处可去）。

    替换的是历史 Message 的 ToolResultBlock.content（对象原地改写，
    journal/loop_messages 与持久历史持有的都是同一对象引用）。chat 锁
    内完成查找+替换，与任何写入方互斥。
    """
    if chat_id is None or not tool_call_id:
        return False
    deadline = time.monotonic() + _DETACHED_WRITEBACK_WAIT
    while True:
        lock = await get_chat_lock(chat_id)
        async with lock:
            history = get_or_init_context(chat_id).get("conversation_history") or []
            for msg in reversed(history):
                if isinstance(msg, Message) and msg.role == "tool":
                    tr = msg.tool_result_block()
                    if tr is not None and tr.tool_call_id == tool_call_id:
                        if tr.content != content:
                            tr.content = str(content or "")
                            logger.info(
                                "[turn-recovery] chat=%s 脱离工具 %s(%s) 终态已回写历史（%s 字符）",
                                chat_id, tool_name, tool_call_id, len(tr.content),
                            )
                        return True
        if time.monotonic() >= deadline:
            logger.warning(
                "[turn-recovery] chat=%s 脱离工具终态回写等待超时：历史始终未出现 tool_call_id=%s",
                chat_id, tool_call_id,
            )
            return False
        try:
            await asyncio.sleep(_DETACHED_WRITEBACK_POLL)
        except asyncio.CancelledError:
            # 进程关闭等场景：回写中止（下次启动后无从追补，但绝不向上炸日志）
            return False


# =====================================================================
# 新 user 消息的提前持久化与合并
# =====================================================================
_ARRAY_KEYS = ("file_ids", "file_names", "mime_types", "attachments")

# 同类多附件可归一的组形态（_resolve_multimodal_content 原生支持多附件）。
_KIND_GROUP_TYPE = {
    "photo": "photo_group",
    "video": "video_group",
    "document": "document_group",
}
# 允许从单数字段重建附件条目的消息类型（历史遗留消息可能没有 attachments）。
_SINGLE_ATTACHMENT_TYPES = ("photo", "video", "document", "audio", "voice")
# 解析器可按 type 路由的类型（用于判断旧消息的 type 是否仍然可路由）。
_RESOLVER_TYPES = {
    "photo", "photo_group", "video", "video_group",
    "document", "document_group", "audio", "voice",
}


def _attachment_entries(msg: dict) -> list[dict]:
    """提取消息携带的附件条目（attachments 优先，缺失时从单数字段重建）。

    打断合并需要统一的附件视图：组消息（photo_group 等）、单发消息
    （file_id 单数字段）以及历史遗留的无 attachments 消息，都要能归到
    同一个 (kind, file_id) 列表上，后续才能按 kind 判断合并形态。
    """
    atts = msg.get("attachments")
    entries: list[dict] = []
    if isinstance(atts, list):
        entries = [dict(a) for a in atts if isinstance(a, dict) and a.get("file_id")]
    if entries:
        return entries
    kind = str(msg.get("type") or "").strip().lower()
    fid = msg.get("file_id")
    if fid and kind in _SINGLE_ATTACHMENT_TYPES:
        entry = {"kind": kind, "file_id": fid}
        if msg.get("file_name"):
            entry["file_name"] = msg["file_name"]
        if msg.get("mime_type"):
            entry["mime_type"] = msg["mime_type"]
        return [entry]
    return []


def _merge_user_message(old: dict, new: dict) -> None:
    """把新 user 消息合并进历史末尾的旧 user 消息（原地修改 old）。

    - content：字符串以空行拼接（保持出站管线对 str content 的假设）；
    - 媒体附件：合并双方全部附件并按 kind 归一——
      * 同类多附件（图/视频/文档，含旧消息 type 不可路由的场景）升级为
        photo_group / video_group / document_group 数组形态，解析器原生
        支持多附件；
      * 混合 kind（如图片+视频）或多音频：保留合并后的 attachments
        列表，由 _resolve_multimodal_content 的混合附件分支逐个解析
        （旧实现只拼 attachments 而解析器从不读它，第二条视频/音频/
        混合媒体会被静默丢弃）；
      * 单一音频并入纯文本/贴纸消息：沿用单数字段补位（audio 无组形态）；
    - type / file_id：旧消息缺失或不可路由时采纳新消息的（保证多模态解析生效）。
    """
    old_text = old.get("content")
    new_text = new.get("content")
    # 新文本先剥掉引用回复前缀再拼接：否则前缀会落在合并文本中部，
    # 既污染历史，也会让后续基于前缀的剥离逻辑（只认开头）失效。
    if isinstance(new_text, str):
        new_text = _strip_reply_prefix(new_text)
    parts = [str(t).strip() for t in (old_text, new_text) if isinstance(t, str) and str(t).strip()]
    if parts:
        old["content"] = "\n\n".join(parts)

    combined = _attachment_entries(old) + _attachment_entries(new)
    if combined:
        kinds = {str(a.get("kind") or "").strip().lower() for a in combined}
        if len(kinds) == 1:
            kind = kinds.pop()
            group_type = _KIND_GROUP_TYPE.get(kind)
            old_type = str(old.get("type") or "").strip().lower()
            if group_type and (len(combined) >= 2 or old_type not in _RESOLVER_TYPES):
                # 同类多附件（或旧消息 type 不可路由，如纯文本/贴纸）→
                # 归一为解析器原生支持的组形态。
                old["type"] = group_type
                old["file_ids"] = [a["file_id"] for a in combined]
                if kind == "photo":
                    old["file_names"] = [
                        a.get("file_name") or f"photo_{str(a['file_id'])[:8]}.jpg"
                        for a in combined
                    ]
                else:
                    old["file_names"] = [a.get("file_name") or "" for a in combined]
                    old["mime_types"] = [a.get("mime_type") or "" for a in combined]
                old["attachments"] = combined
                for key in ("file_id", "file_name", "mime_type"):
                    old.pop(key, None)
                return
        if len(combined) >= 2:
            # 混合 kind 或多音频：保留 attachments 列表交给解析器混合分支。
            old["attachments"] = combined
            for key in ("file_id", "file_ids", "file_name", "file_names",
                        "mime_type", "mime_types"):
                old.pop(key, None)
            return
        # 单一附件且无组形态（音频并入纯文本/贴纸消息）：落到下方单数
        # 字段补位，保持与未合并消息一致的可路由形态。

    for key in _ARRAY_KEYS:
        new_vals = new.get(key)
        if isinstance(new_vals, list) and new_vals:
            existing = old.get(key)
            if not isinstance(existing, list):
                existing = []
            old[key] = existing + list(new_vals)
    if new.get("type"):
        old_type = str(old.get("type") or "").strip().lower()
        if not old.get("type") or old_type not in _RESOLVER_TYPES:
            old["type"] = new["type"]
    if not old.get("file_id") and new.get("file_id"):
        old["file_id"] = new["file_id"]
    if not old.get("file_name") and new.get("file_name"):
        old["file_name"] = new["file_name"]
    if not old.get("mime_type") and new.get("mime_type"):
        old["mime_type"] = new["mime_type"]


def _apply_attachment_entries(msg: dict, entries: list[dict]) -> None:
    """把附件条目写到消息上（归一化规则与 _merge_user_message 保持一致）。

    失败轮替换时搬运旧媒体用：形状对齐 _resolve_multimodal_content 的
    各路由分支——同类多图/多视频/多文档写组形态，单视频/文档/音频写
    单数字段，混合 kind / 多音频保留 attachments 列表交给解析器混合分支。
    """
    if not entries:
        return
    kinds = {str(a.get("kind") or "").strip().lower() for a in entries}
    mixed = len(kinds) > 1 or (bool(kinds) and kinds <= {"audio", "voice"})
    if mixed and len(entries) >= 2:
        # 混合 kind 或多音频：attachments 列表形态（解析器混合分支）。
        msg["attachments"] = entries
        for key in ("file_id", "file_ids", "file_name", "file_names",
                    "mime_type", "mime_types"):
            msg.pop(key, None)
        return
    if len(kinds) == 1:
        kind = kinds.pop()
        group_type = _KIND_GROUP_TYPE.get(kind)
        if group_type and len(entries) >= 2:
            msg["type"] = group_type
            msg["file_ids"] = [a["file_id"] for a in entries]
            if kind == "photo":
                msg["file_names"] = [
                    a.get("file_name") or f"photo_{str(a['file_id'])[:8]}.jpg"
                    for a in entries
                ]
            else:
                msg["file_names"] = [a.get("file_name") or "" for a in entries]
                msg["mime_types"] = [a.get("mime_type") or "" for a in entries]
            msg["attachments"] = entries
            return
        # 单附件：写单数字段（photo 用组形态，与 app 生产者一致）。
        entry = entries[0]
        if group_type and kind == "photo":
            msg["type"] = "photo_group"
            msg["file_ids"] = [entry["file_id"]]
            msg["file_names"] = [entry.get("file_name") or f"photo_{str(entry['file_id'])[:8]}.jpg"]
        else:
            msg["type"] = kind
            msg["file_id"] = entry["file_id"]
            if entry.get("file_name"):
                msg["file_name"] = entry["file_name"]
            if entry.get("mime_type"):
                msg["mime_type"] = entry["mime_type"]
        msg["attachments"] = entries
        return


def _replace_failed_user_message(old: dict, new: dict) -> None:
    """用新 user 消息整体替换失败轮的 user 消息（原地修改 old，保持槽位身份）。

    重试语义：新消息的文本与媒体完全接管该历史槽位，不与旧内容拼接——
    每次重试后请求里每段文本、每张图片恰好一份，绝不随重试次数叠加。

    唯一例外：新消息**不带任何媒体**而失败轮带了（用户只回了一句
    "再试一次"）：把失败轮的媒体原样搬移一份过来（不搬旧文本），
    否则用户上传的图片会因一次请求失败从历史中彻底消失，原生图像
    模型的重试也会退化成不带参考图的文生图。
    """
    carried_media = _attachment_entries(old) if not _attachment_entries(new) else []
    if len(carried_media) == 1:
        # 兼容历史遗留消息：attachments 条目缺 file_name/mime_type 但单数
        # 字段上有的，回填进条目再搬运（仅单附件时无歧义）。
        entry = carried_media[0]
        if not entry.get("file_name") and old.get("file_name"):
            entry["file_name"] = old["file_name"]
        if (not entry.get("mime_type") and old.get("mime_type")
                and str(entry.get("kind") or "").lower() == str(old.get("type") or "").lower()):
            entry["mime_type"] = old["mime_type"]
    old.clear()
    for key, value in new.items():
        if key != EARLY_PERSIST_FLAG:
            old[key] = value
    if carried_media:
        _apply_attachment_entries(old, carried_media)


async def persist_user_message_entry(chat_id: int, user_message: dict) -> bool:
    """轮次开始时把新 user 消息写入持久历史（必要时合并/替换上一条 user）。

    返回 True 表示已持久化（get_ai_response 无需再单独 append 到请求，
    update_conversation_and_ledger 也会跳过重复写入）。TIMER 合成唤醒
    消息不经过本函数（不写历史）。

    历史末尾是上一条未获回应的 user 消息时分两种情况：
    - 带 TURN_FAILED_FLAG（上一轮请求失败）：整体替换，不合并（重试不
      叠加旧文本/旧图片；新消息无媒体时搬移旧媒体一份）；
    - 无标记（上一轮被新消息打断、尚无任何输出）：合并（快速连发的
      合并链，保持原设计）。
    """
    if chat_id is None or not isinstance(user_message, dict):
        return False

    def _envelope_of(msg: Message) -> dict:
        """把存储的 user Message 投影回信封 dict（meta + content），复用
        旧版纯 dict 的合并/替换算法。"""
        env = dict(msg.meta)
        env["content"] = msg.text()
        return env

    def _wrap_envelope(env: dict) -> Message:
        """信封 dict -> 存储 Message（文本进 blocks，其余进 meta）。

        统一在此剥离引用回复前缀：追加/合并/替换三条路径进入持久历史
        的消息都可能带 "💡 引用回复:\n> ...\n\n" 前缀。前缀只对当前请求
        有意义，进历史后会在后续每轮请求中重复重发给模型，污染上下文。
        """
        env = dict(env)
        content = env.pop("content", "")
        content = _strip_reply_prefix(content if isinstance(content, str) else str(content or ""))
        return Message.user_text(str(content or ""), **env)

    # 身份标记必须 BEFORE 落库写入：它会随 _wrap_envelope 进入存储消息的
    # meta，undo_early_persist 据此校验"历史末尾那条就是我写的这条"。
    user_message[EARLY_PERSIST_TS] = uuid.uuid4().hex
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        history = ctx.setdefault("conversation_history", [])
        last = history[-1] if history else None
        last_is_user = isinstance(last, Message) and last.role == "user"
        if last_is_user:
            last_env = _envelope_of(last)
            if last_env.pop(TURN_FAILED_FLAG, None):
                # 上一轮请求失败：替换而非合并（重试语义，见函数 docstring）。
                _replace_failed_user_message(last_env, user_message)
                history[-1] = _wrap_envelope(last_env)
                user_message[EARLY_PERSIST_MODE] = "replaced"
                logger.info(
                    "[turn-recovery] chat=%s 上一轮请求失败：新 user 消息替换失败轮消息"
                    "（不合并旧文本/图片，媒体仅在新消息为空时搬移一份）",
                    chat_id,
                )
            else:
                # 打断发生在任何 assistant 输出之前：合并，避免连续两条 user。
                # 防御性断言（改动点5）：合并的前提是"旧轮次无任何 assistant
                # 输出"——此刻注册表里不应再有带未保全 journal 的旧登记
                # （顺序上 _interrupt_active_generation → finalize_pending_
                # turns 已跑完；本轮自己的登记 journal 为空，不会误报）。
                # 若仍有，说明前提被打破（旧轮实际有输出却走了合并），
                # ERROR 落日志即可从日志直接定位，不必再靠用户截图反推。
                residual = [
                    e for e in _inflight.get(chat_id, [])
                    if e.journal  # 非空 journal = 旧轮有进度未被保全
                ]
                if residual:
                    logger.error(
                        "[turn-recovery] chat=%s 合并分支触发，但仍有 %s 条旧轮次登记"
                        "带未保全的 journal（合并前提『无 assistant 输出』被破坏——"
                        "检查打断/保全时序，本轮进度可能被错误合并）",
                        chat_id, len(residual),
                    )
                _merge_user_message(last_env, user_message)
                history[-1] = _wrap_envelope(last_env)
                user_message[EARLY_PERSIST_MODE] = "merged"
                logger.info(
                    "[turn-recovery] chat=%s 新 user 消息合并进上一条未回应的 user 消息",
                    chat_id,
                )
        else:
            history.append(_wrap_envelope(user_message))
            user_message[EARLY_PERSIST_MODE] = "appended"
    user_message[EARLY_PERSIST_FLAG] = True
    return True


async def undo_early_persist(chat_id: int, user_message: dict) -> None:
    """回滚一次提前持久化（消费型接管 / pre_flight 拒绝时恢复原状）。

    背景（2026-09-12 生产事故修复）：spawn_turn_task 在派发回合任务前
    先把 user 消息持久化，消除"回合在 persist 前被下一条消息打断、
    消息静默丢失"的窗口。代价是：消息入库后，媒体参数卡片接管
    （try_consume_media_message / try_consume_text_message）或
    pre_flight_context_check 拒绝时需要撤回这条入库——本函数就是那个
    撤回入口。

    语义：
      - mode=appended：历史末尾就是本次 append 的那条 → 校验 TS 一致后
        pop，恢复到未入库状态；
      - mode=merged / replaced：旧消息已被改写（合并/替换），无法也不应
        机械回滚——保持现状（消息确实是用户发的，留在历史不产生错误
        信息；被卡片消费的素材下次回合作为上下文出现属可接受的边界）。
      - 未持久化（无标记）：无操作。幂等，可安全重复调用。
    """
    if chat_id is None or not isinstance(user_message, dict):
        return
    if not user_message.get(EARLY_PERSIST_FLAG):
        return
    mode = user_message.get(EARLY_PERSIST_MODE)
    if mode != "appended":
        logger.debug(
            "[turn-recovery] chat=%s undo_early_persist 跳过（mode=%s，不回滚）",
            chat_id, mode,
        )
        return
    ts = user_message.get(EARLY_PERSIST_TS)
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        history = ctx.get("conversation_history") or []
        last = history[-1] if history else None
        if (
            isinstance(last, Message)
            and last.role == "user"
            and ts
            and last.meta.get(EARLY_PERSIST_TS) == ts
        ):
            history.pop()
            logger.info(
                "[turn-recovery] chat=%s 已回滚提前持久化的 user 消息（消费/拒绝路径）",
                chat_id,
            )
        else:
            # 历史末尾不是本次写入的那条（并发写入/已被改写）：保守不删。
            logger.debug(
                "[turn-recovery] chat=%s undo_early_persist 未命中末尾消息，保持不动",
                chat_id,
            )
    # 清理标记，避免同一次派发内重复回滚。
    user_message.pop(EARLY_PERSIST_FLAG, None)
    user_message.pop(EARLY_PERSIST_MODE, None)
    user_message.pop(EARLY_PERSIST_TS, None)


async def mark_failed_unanswered_user(chat_id: int) -> None:
    """轮次失败收尾时调用：给历史末尾未获回应的 user 消息打失败标记。

    调用时机：get_ai_response 的各失败路径（顶层异常 / IMAGE_ERROR /
    VIDEO_ERROR / 空响应 / ⚠️ 前缀的 IMAGE_SENT 拒绝等）——这些路径
    都不会写入任何 assistant 消息，历史末尾保持为本轮的 user 消息。
    打上标记后，下一次 persist_user_message_entry 会走"替换"而不是
    "合并"，请求失败后的重试不再叠加上一轮的文本与图片。

    - 历史末尾不是 user 消息（轮次已有部分进度被 salvage 落历史）时
      无操作——此时下一条 user 消息本来就按新消息追加，不存在合并。
    - 只改末尾一条，绝不扫描/改写更早的历史。
    """
    if chat_id is None:
        return
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        history = ctx.get("conversation_history") or []
        last = history[-1] if history else None
        if isinstance(last, Message) and last.role == "user":
            last.meta[TURN_FAILED_FLAG] = True
            logger.info(
                "[turn-recovery] chat=%s 轮次失败：已标记末尾未回应的 user 消息"
                "（下一条消息将替换而非合并）",
                chat_id,
            )


# =====================================================================
# 静默模式 deliver_reply 交付标记（轮次开始时重置，收尾时读取清除）
# =====================================================================
def reset_turn_delivery_state(chat_id: int, *, default_send: bool) -> None:
    """agent 轮次开始时重置本轮交付状态，并设定 send 的缺省值。

    - ``default_send=True``（/show off + USER 回合）：deliver_reply 的 send
      不填按 true 处理；整轮未调用时收尾由 get_ai_response 兜底发送最后
      一条非空 assistant 消息正文（与工具交付同源）；
    - ``default_send=False``（/show off + TIMER 回合，或非静默回合）：
      send 不填 / false / 不调用均不发送，无兜底（旧行为）。

    顺带清理上一轮（含异常 / 打断路径）残留的 delivered / suppressed 标记，
    保证"agent 开始时重置"语义成立：上一轮交付或抑制与否绝不影响本轮。
    """
    if chat_id is None:
        return
    _reply_delivered.discard(chat_id)
    _reply_suppressed.discard(chat_id)
    if default_send:
        _default_send.add(chat_id)
    else:
        _default_send.discard(chat_id)


def default_send_value(chat_id: int) -> bool:
    """读取本轮 deliver_reply 的 send 缺省值（tool_call_loop.run_one 用）。"""
    return chat_id is not None and chat_id in _default_send


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


def mark_reply_suppressed(chat_id: int) -> None:
    """模型显式调用 deliver_reply(send=false) 后调用：本轮抑制兜底交付。"""
    if chat_id is not None:
        _reply_suppressed.add(chat_id)


def pop_reply_suppressed(chat_id: int) -> bool:
    """get_ai_response 收尾时读取并清除本轮显式抑制标记。"""
    if chat_id is None:
        return False
    if chat_id in _reply_suppressed:
        _reply_suppressed.discard(chat_id)
        return True
    return False
