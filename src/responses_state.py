"""Responses API 对话状态：官方 server-managed state（previous_response_id 链）。

设计（对齐官方 Responses API 的状态管理建议）：

1. **单一状态源**：每个 chat 只保存一个 ``previous_response_id`` 链头
   （连同产生它的 model / vendor_key）。不再维护"水位 + 增量历史"镜像
   账本，也不再从本地 transcript 重新拼装 Responses 上下文——服务端
   response chain 就是多轮上下文的唯一载体。
2. **原子推进**：只有 response 真正成功返回并拿到有效 ``response.id``
   后才推进链头；请求失败 / 流中断 / 缺终态事件时保持旧链头，绝不出现
   "请求失败了，但本地已把链推进到不存在的 response"的脏状态。
3. **显式失效**：/clear（generation fencing）、模型或端点切换、本地历史
   结构变化（压缩淘汰）、跨协议写入、回合中断（请求已出网）都会使链
   失效。失效后的下一轮从本地 canonical history 全量 bootstrap——这是
   官方文档定义的异常恢复路径，不是常规工作模式。
4. **恢复的是 ID，不是伪造历史**：进程重启后可按
   ``(chat_id, model, previous_response_id)`` 恢复链头继续官方 chain
   （见 :func:`export_chain_state` / :func:`restore_chain_state`）；
   服务端明确返回 previous response 不存在时，走一次性 bootstrap 重试。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from config import ModelConfig

# 链头进入 bootstrap 的原因只用于日志/诊断，不参与任何状态机判断。
RESPONSE_ID_MISSING = "no_response_chain"
MODEL_CHANGED = "model_or_endpoint_changed"


@dataclass
class ResponseChainRef:
    """一个 chat 的最近成功 Response 链头。

    ``response_id`` 为 ``None`` 表示当前没有可用链（下一轮 bootstrap）。
    """

    vendor_key: str
    response_id: Optional[str] = None
    model: Optional[str] = None
    updated_at: float = 0.0
    invalid_reason: Optional[str] = None

    def invalidate(self, reason: str) -> None:
        self.response_id = None
        self.invalid_reason = reason
        self.updated_at = time.time()


@dataclass
class TurnState:
    """一次 agent 回合的 generation fencing 快照。

    ``generation`` 是 /clear 边界：/clear 后迟到的回合提交会被
    :meth:`ResponseState.commit_response` 拒绝，不能复活旧 response chain。
    """

    turn_id: str
    event_source: str
    generation: int
    sync_ctx: Any = None


@dataclass
class ResponseState:
    """每个 chat 的 Responses 链头 + 回合 fencing 状态。"""

    generation: int = 0
    chain: Optional[ResponseChainRef] = None
    active_turn_ids: set[str] = field(default_factory=set)

    # 回合登记（generation fencing）
    def begin_turn(self, event_source: str) -> TurnState:
        turn = TurnState(
            turn_id=uuid.uuid4().hex[:16],
            event_source=event_source,
            generation=self.generation,
        )
        self.active_turn_ids.add(turn.turn_id)
        return turn

    def unregister_turn(self, turn: Optional[TurnState]) -> None:
        if turn is not None:
            self.active_turn_ids.discard(turn.turn_id)

    def is_turn_current(self, turn: TurnState) -> bool:
        return turn.generation == self.generation

    # Responses response-chain 状态
    def invalidate_chain(self, reason: str) -> None:
        """作废当前链头（幂等）。

        下一轮 Responses 请求将从本地 canonical history 全量 bootstrap。
        """
        if self.chain is not None:
            self.chain.invalidate(reason)
            return
        self.chain = ResponseChainRef(vendor_key="", response_id=None)
        self.chain.invalidate(reason)

    def resolve_chain(self, vendor_key: str, model: str) -> tuple[Optional[str], str]:
        """决定本回合是否沿 previous_response_id 续链。

        返回 ``(previous_response_id, mode)``；``mode`` 为 ``"chain"``
        （续链）或 ``"bootstrap:<reason>"``（从 canonical history 全量
        重建）。模型或端点分区与链头不一致时主动断链——防止把 A 模型的
        response id 续给 B 模型，也防止 A→B→A 后把 B 期间的本地历史
        遗漏在 A 的旧链之外。
        """
        ref = self.chain
        if ref is None or not ref.response_id:
            return None, f"bootstrap:{RESPONSE_ID_MISSING}"
        if ref.model != model or ref.vendor_key != vendor_key:
            ref.invalidate(MODEL_CHANGED)
            return None, f"bootstrap:{MODEL_CHANGED}"
        return ref.response_id, "chain"

    def commit_response(
        self,
        turn: TurnState,
        *,
        vendor_key: str,
        response_id: Optional[str],
        model: Optional[str],
    ) -> bool:
        """回合收尾提交链头（仅在 response 成功返回后调用）。

        - ``response_id`` 为空 → 拒绝（失败响应不能成为续接点）；
        - generation 已过期（/clear 竞态）→ 拒绝；
        - 成功时原子覆盖链头。
        """
        if not response_id or not self.is_turn_current(turn):
            return False
        self.chain = ResponseChainRef(
            vendor_key=vendor_key,
            response_id=response_id,
            model=model,
            updated_at=time.time(),
        )
        return True

    def reset(self) -> None:
        """/clear：清空链头并建立 generation fencing 边界。"""
        self.generation += 1
        self.chain = None


_response_states: dict[int, ResponseState] = {}
_states_lock: Optional[asyncio.Lock] = None


def _get_states_lock() -> asyncio.Lock:
    global _states_lock
    if _states_lock is None:
        _states_lock = asyncio.Lock()
    return _states_lock


async def get_response_state(chat_id: int) -> ResponseState:
    async with _get_states_lock():
        st = _response_states.get(chat_id)
        if st is None:
            st = ResponseState()
            _response_states[chat_id] = st
        return st


def get_response_state_sync(chat_id: int) -> ResponseState:
    st = _response_states.get(chat_id)
    if st is None:
        st = ResponseState()
        _response_states[chat_id] = st
    return st


async def reset_response_state(chat_id: int) -> None:
    st = await get_response_state(chat_id)
    st.reset()


def invalidate_response_chain(chat_id: int, reason: str) -> None:
    """作废该 chat 的 Responses 链（下一轮 bootstrap）。"""
    get_response_state_sync(chat_id).invalidate_chain(reason)


def mark_legacy_divergence(chat_id: int) -> None:
    """非 Responses 协议（chat-completions / anthropic / gemini）写入历史后，
    作废 Responses 链：这些写入不在服务端 response chain 里，续链会静默
    丢失这段上下文。"""
    invalidate_response_chain(chat_id, "legacy_protocol_turn")


def resolve_response_chain(chat_id: int, vendor_key: str, model: str) -> tuple[Optional[str], str]:
    """读取该 chat 的链头（bridge 每回合开始时调用）。"""
    return get_response_state_sync(chat_id).resolve_chain(vendor_key, model)


def commit_response_sync(
    chat_id: int,
    turn: TurnState,
    *,
    vendor_key: str,
    response_id: Optional[str],
    model: Optional[str],
) -> bool:
    """回合收尾提交链头（bridge 仅在 response 成功返回后调用）。"""
    return get_response_state_sync(chat_id).commit_response(
        turn, vendor_key=vendor_key, response_id=response_id, model=model,
    )


def register_active_turn(chat_id: int, turn: TurnState) -> None:
    get_response_state_sync(chat_id).active_turn_ids.add(turn.turn_id)


def unregister_active_turn(chat_id: int, turn: Optional[TurnState]) -> None:
    get_response_state_sync(chat_id).unregister_turn(turn)


def has_active_turns(chat_id: int) -> bool:
    return bool(get_response_state_sync(chat_id).active_turn_ids)


# 网关能力记忆：previous_response_id + 纯 function_call_output 续轮
# 官方语义下工具续轮就是 ``previous_response_id`` + ``input=[function_call_output]``。
# 个别 OpenAI-compatible 网关（尤其是自行保存 response 状态、再翻译给上游
# 的中转）会拒绝这种续轮：400 的措辞随上游而变（``input must be
# non-empty`` / ``invalid request`` / ...），但同一 (端点, 模型) 上会反复
# 复现。记住一次，后续工具续轮直接走 bootstrap 重放（full replay 靠
# prompt_cache_key 的隐式前缀缓存，成本可控），不再白发一个注定 400 的请求。
#
# 记忆带 TTL：网关/上游会升级或切换路由，永久标记会让一次偶发失败把链式
# 续轮永远关掉。TTL 到期后重新探测一次链式续轮。
_TOOL_CHAIN_UNSUPPORTED_TTL = 30 * 60  # 秒，与 prompt cache ttl 同量级
_tool_chain_unsupported: dict[tuple[str, str], float] = {}


def mark_tool_continuation_chain_unsupported(vendor_key: str, model: str) -> None:
    _tool_chain_unsupported[(vendor_key, model)] = time.monotonic()


def is_tool_continuation_chain_unsupported(vendor_key: str, model: str) -> bool:
    key = (vendor_key, model)
    marked_at = _tool_chain_unsupported.get(key)
    if marked_at is None:
        return False
    if time.monotonic() - marked_at > _TOOL_CHAIN_UNSUPPORTED_TTL:
        _tool_chain_unsupported.pop(key, None)
        return False
    return True


def derive_vendor_key(model_info: "ModelConfig") -> str:
    from config import get_effective_endpoint
    endpoint = get_effective_endpoint(model_info)
    return f"{endpoint.provider}|{endpoint.endpoint}|{endpoint.protocol}"


# 重启恢复（规则：只恢复 ID，不伪造历史）
def export_chain_state() -> dict[str, dict[str, Any]]:
    """导出全部 chat 的链头快照：``{chat_id: {vendor_key, model,
    previous_response_id, updated_at}}``。

    供宿主进程在重启前持久化、重启后调用 :func:`restore_chain_state`
    继续官方 chain。没有任何本地 transcript 参与恢复——服务端持有
    全部上下文，本地历史为空时模型侧记忆仍由 response chain 承载。
    """
    out: dict[str, dict[str, Any]] = {}
    for chat_id, st in _response_states.items():
        ref = st.chain
        if ref is None or not ref.response_id:
            continue
        out[str(chat_id)] = {
            "vendor_key": ref.vendor_key,
            "model": ref.model,
            "previous_response_id": ref.response_id,
            "updated_at": ref.updated_at,
        }
    return out


def restore_chain_state(
    chat_id: int,
    *,
    model: str,
    previous_response_id: str,
    vendor_key: str = "",
) -> bool:
    """重启后恢复链头（chat_id / model / previous_response_id）。

    只恢复 ID：不重建、不推测任何本地历史。恢复的链头第一次使用时若
    服务端明确返回该 response 不存在/不可用，bridge 会走一次明确的
    bootstrap（全量 canonical history），然后回到正常链式模式。
    """
    if not previous_response_id:
        return False
    st = get_response_state_sync(chat_id)
    st.chain = ResponseChainRef(
        vendor_key=vendor_key,
        response_id=previous_response_id,
        model=model,
        updated_at=time.time(),
    )
    return True


__all__ = [
    "ResponseChainRef", "TurnState", "ResponseState",
    "RESPONSE_ID_MISSING", "MODEL_CHANGED",
    "get_response_state", "get_response_state_sync", "reset_response_state",
    "invalidate_response_chain", "mark_legacy_divergence",
    "resolve_response_chain", "commit_response_sync",
    "register_active_turn", "unregister_active_turn", "has_active_turns",
    "derive_vendor_key", "export_chain_state", "restore_chain_state",
    "mark_tool_continuation_chain_unsupported",
    "is_tool_continuation_chain_unsupported",
]
