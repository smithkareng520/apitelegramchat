# -*- coding: utf-8 -*-
"""对话状态层（Conversation State）：Canonical History 与 Provider Cursor 分离。

背景
====
项目现有的 ``ctx["conversation_history"]``（见 state.py）是所有厂商共享
的"事实历史"（canonical history）——每个协议适配器在请求前把它渲染成
自己的线上形状（Chat messages / Responses input items / Anthropic
messages / Gemini contents），这一层不变。

但 OpenAI Responses API 原生支持"服务端会话状态"（``conversation`` /
``previous_response_id``）：只要请求携带同一个 ``conversation`` id，
服务端就记得此前的全部 input/output item，本轮只需要发送**最新一轮**
的增量，不必每次重新序列化整份历史。要吃到这个能力，必须把"我们聊过
什么"（canonical history，跨厂商共享、任何时候可切换模型）和"某个
provider 服务端目前记得到哪里"（provider cursor，只在连续使用同一
provider 且没有中途切换模型/清空历史时才继续有效）分成两层：

- **Canonical History**：不变，仍是 ``ctx["conversation_history"]``。
- **Provider Cursor**（本模块新增）：记录"OpenAI Responses 服务端会话
  当前已经同步到 canonical history 的第几条消息"，以及它的
  ``conversation_id``。下一轮请求前，调用方比较 cursor 的
  ``synced_revision`` 与当前 ``canonical_revision``：
    * 相等 -> cursor 仍然有效，只发送 revision 之后新增的消息
      （通常就是本轮新的 user 消息 + 上一轮的 assistant/tool 消息，
      若上一轮就是靠同一个 cursor 产生的，见 responses_bridge 的
      "自举"注释）；
    * 不等（模型切换到别的协议又切回来 / 历史被压缩 / 从未建立过
      cursor）-> cursor 失效，必须重新"自举"：把当前 canonical
      history 全量转换一次性发送，建立新的服务端会话。

模型切换语义（务必记住）：
    模型切换 != 清空聊天。切换模型只会让**当前协议**的 provider cursor
    失效（下次切回来时自举一次），canonical history 与其它协议的
    cursor 完全不受影响。只有 ``/clear`` 才清空 canonical history 并
    让全部 cursor 失效。

TIMER / USER 并发保护（generation + base_revision fencing）：
    本项目的调度模型（proactive.py）保证同一 chat 任意时刻最多一个
    in-flight 的 agent 回合——TIMER 回合被 USER 消息打断时走
    ``asyncio.CancelledError`` 协作取消，不存在两个回合真正并发跑
    模型请求的情形。但取消存在"竞态窗口"：TIMER 的请求已经发出、
    正在等待网络返回时被取消，若取消传播不够快（例如 finally 块里
    残留的收尾代码），理论上可能出现"取消已经发生，但回调仍尝试
    commit 服务端 cursor"的情况。为了不依赖"取消一定足够快"这个
    脆弱假设，commit 前额外做一次乐观并发校验（generation +
    base_revision fencing）：每个回合开始时快照当前 generation 与
    canonical_revision，commit 时只有这两个值仍然匹配"发起请求那一刻"
    才允许写入 cursor；不匹配（期间发生了 /clear 或另一个回合已经
    抢先 commit 过更新的 revision）则丢弃本次 commit，不倒退状态。
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# 数据结构
# =============================================================================
@dataclass
class ProviderCursor:
    """单个 provider（目前只有 openai_responses）的服务端会话游标。

    synced_revision：该 cursor 建立/续接时对应的 canonical_revision——
        即"服务端目前记得 canonical history 的前 synced_revision 条"。
        下一轮请求前与当前 canonical_revision 比较，判断 cursor 是否
        仍然可以增量续接。
    conversation_id：Responses API 的 ``conversation`` 对象 id
        （conv_xxx）。选择 conversation 而不是 previous_response_id
        的原因见模块级 README 任务指南：conversation 的 item 保留期
        不受单次 response 30 天 TTL 限制，且语义上更贴近"一个
        Telegram chat = 一个长期会话"。
    last_response_id：最近一次成功响应的 response id，仅用于诊断
        日志，不参与续接判断（Responses 的 conversation 模式下续接
        只需要 conversation_id，不需要 previous_response_id）。
    model：建立该 cursor 时使用的模型名，仅用于诊断日志（同协议
        换模型不强制失效 cursor——Responses 的 conversation 是模型
        无关的容器，但记录下来方便排查"换模型后语气突变"之类问题）。
    """

    conversation_id: Optional[str] = None
    last_response_id: Optional[str] = None
    synced_revision: int = 0
    model: Optional[str] = None

    def is_valid_for(self, canonical_revision: int) -> bool:
        """cursor 是否可以在当前 canonical_revision 下继续增量续接。"""
        return bool(self.conversation_id) and self.synced_revision == canonical_revision

    def invalidate(self) -> None:
        self.conversation_id = None
        self.last_response_id = None
        self.synced_revision = 0
        self.model = None


@dataclass
class TurnState:
    """一次 agent 回合的事务快照（在回合开始时创建，用于 commit 前的
    乐观并发校验——见模块 docstring 的 "TIMER / USER 并发保护"）。
    """

    turn_id: str
    event_source: str  # "USER" | "TIMER"
    generation: int
    base_revision: int


@dataclass
class ConversationState:
    """单个 chat 的对话状态：generation + revision + 各 provider 的 cursor。

    generation：语义边界计数器。只有 ``/clear`` 会递增它——递增后，
        所有此前基于旧 generation 快照的 TurnState 在 commit 时都会
        被 fencing 拒绝（即使它们的 base_revision 恰好数值相同，
        generation 不同也判定为"过期"）。
    canonical_revision：canonical history 的写入版本号，每次
        ``update_conversation_and_ledger`` 成功追加消息后递增
        （见 bump_revision 调用点：app_turns.py）。
    responses：openai_responses 协议的 provider cursor。预留其它协议
        字段（anthropic / gemini）供未来扩展，当前不使用。
    """

    generation: int = 0
    canonical_revision: int = 0
    responses: ProviderCursor = field(default_factory=ProviderCursor)

    def begin_turn(self, event_source: str) -> TurnState:
        return TurnState(
            turn_id=uuid.uuid4().hex[:16],
            event_source=event_source,
            generation=self.generation,
            base_revision=self.canonical_revision,
        )

    def bump_revision(self, n: int = 1) -> int:
        self.canonical_revision += n
        return self.canonical_revision

    def reset(self) -> None:
        """``/clear`` 语义：generation += 1，revision 归零，全部 cursor 失效。

        注意：canonical history 本身（ctx["conversation_history"]）不由
        本类持有，调用方（state.safe_clear_history）负责清空；这里只
        重置"状态账本"部分，两者必须在同一把 chat 锁内一起调用，保证
        原子性（safe_clear_history 已持锁，见该函数）。
        """
        self.generation += 1
        self.canonical_revision = 0
        self.responses.invalidate()

    def is_turn_current(self, turn: TurnState) -> bool:
        """commit 前的乐观并发校验：generation 必须仍是发起请求时的那个。

        注意：不校验 base_revision——收到响应时 canonical_revision 通常
        已经因为本轮自己的 user 消息提前持久化而前进了，这是预期之内的
        正常推进，不代表竞态。真正需要拒绝的只有"回合发起后又发生了
        /clear（generation 变了）"这一种情况；至于"哪个 provider cursor
        写入的 synced_revision 更新"，由 commit_responses_cursor 按写入
        时刻的 canonical_revision 重新赋值，天然是最后写入者生效，不会
        因为 TIMER 晚到而把 revision 往回写（因为它写入的是"当前"
        revision 而不是"发起时"的 base_revision）。
        """
        return turn.generation == self.generation


# =============================================================================
# 每 chat 一份状态 + 异步锁保护（与 state.py 的 _chat_locks 分工一致：
# 复用同一把 chat 锁会让本模块的读写与历史读写天然互斥，不再单独加锁）。
# =============================================================================
_conversation_states: dict[int, ConversationState] = {}
_states_lock = asyncio.Lock()


async def get_conversation_state(chat_id: int) -> ConversationState:
    """获取（惰性创建）指定 chat 的 ConversationState。

    调用方应在持有 ``state.get_chat_lock(chat_id)`` 的前提下调用本函数
    并修改返回对象的字段——本模块自身的 ``_states_lock`` 只保护"惰性
    创建"这一步，不代替 chat 锁；这与 state.py 里 ``user_contexts`` 的
    读写模型完全一致（dict 本体的结构性变更受保护，字段的读写由调用方
    的业务锁保证串行）。
    """
    async with _states_lock:
        st = _conversation_states.get(chat_id)
        if st is None:
            st = ConversationState()
            _conversation_states[chat_id] = st
        return st


def get_conversation_state_sync(chat_id: int) -> ConversationState:
    """同步读路径：仅供已经确定状态必然存在、或可以接受"读到默认值"的
    只读诊断场景使用（例如日志）。写路径一律走异步版本。
    """
    st = _conversation_states.get(chat_id)
    if st is None:
        st = ConversationState()
        _conversation_states[chat_id] = st
    return st


async def reset_conversation_state(chat_id: int) -> None:
    """``/clear`` 专用：必须在调用方已持有 chat 锁时调用（与
    state.safe_clear_history 同一临界区，见该函数的调用点 app_commands.py）。
    """
    st = await get_conversation_state(chat_id)
    st.reset()


async def bump_canonical_revision(chat_id: int, n: int = 1) -> int:
    """canonical history 追加消息后递增 revision；同样要求调用方已持锁
    （见 app_turns.update_conversation_and_ledger 的调用点，与历史写入
    共享同一把 chat 锁，天然保证"写入"与"计数"在同一原子区间）。
    """
    st = await get_conversation_state(chat_id)
    return st.bump_revision(n)


def commit_responses_cursor(
    chat_id: int,
    turn: TurnState,
    *,
    conversation_id: Optional[str],
    response_id: Optional[str],
    model: Optional[str],
) -> bool:
    """回合成功结束后尝试把新的 provider cursor 写回状态。

    返回 True 表示写入生效；False 表示被 fencing 拒绝（generation 已经
    因 /clear 前进，本次结果对当前对话已经过期，绝不能用一个过期结果
    覆盖/复活已经失效的 cursor——典型场景：TIMER 回合的响应在
    ``/clear`` 之后才姗姗来迟）。

    conversation_id 为空（本轮请求没有使用 Responses 服务端会话，或者
    该次请求走的是 bootstrap 之外的路径）时不写入、直接返回 False，
    调用方按"未生效"处理，不影响下一轮的自举判断。

    本函数是同步纯函数（不 await），因此不存在"读取-判断-写入"之间
    再次被打断的窗口——调用方在拿到响应后、还未释放事件循环控制权的
    同一段代码里调用即可安全生效，无需额外加锁。
    """
    if not conversation_id:
        return False
    st = get_conversation_state_sync(chat_id)
    if not st.is_turn_current(turn):
        return False
    st.responses.conversation_id = conversation_id
    st.responses.last_response_id = response_id
    st.responses.model = model
    # 关键：写入"当前" canonical_revision（这一刻的值），而不是
    # turn.base_revision——历史通常已经因为本轮自己的消息追加而前进，
    # cursor 的"有效性基准"必须是追加之后的最新值，否则下一轮的
    # is_valid_for 比较永远为假，自举逻辑会被误触发。
    st.responses.synced_revision = st.canonical_revision
    return True


def invalidate_responses_cursor(chat_id: int) -> None:
    """协议切走 openai_responses（例如切到 Claude）时使当前 cursor 失效。

    不清空 canonical history、不递增 generation——只是让下次切回
    Responses 协议时走一次自举（bootstrap），语义与模块 docstring
    "模型切换 != 清空聊天" 一致。
    """
    st = get_conversation_state_sync(chat_id)
    st.responses.invalidate()


__all__ = [
    "ProviderCursor",
    "TurnState",
    "ConversationState",
    "get_conversation_state",
    "get_conversation_state_sync",
    "reset_conversation_state",
    "bump_canonical_revision",
    "commit_responses_cursor",
    "invalidate_responses_cursor",
]
