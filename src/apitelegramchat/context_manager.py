"""请求侧上下文守卫（request-time guard）。

策略（2026-09 重构，详见 CACHE_OPTIMIZATION.md 与 context_window.py）：

**存储历史即请求上下文。** 历史的有界性由 app.pre_flight_context_check
的自动压缩事件（高/低水位 + 滞后）维护，本模块不再做逐轮滑动截尾——
旧版"从尾部回退装配"会让窗口起点每轮后移，隐式前缀缓存整段 miss。

select_request_context 退化为守卫，只在两种情况下工作：

1. **快路径（常态）**：历史在预算内 → 原样返回全部消息（浅拷贝），
   一字节不改 → 请求前缀与上一轮完全一致，provider 端 prompt/KV
   缓存全量命中；
2. **兜底路径（罕见）**：持久历史超出预算（压缩事件失败、会话中途
   切换到小窗口模型、异常路径）→ 从最老的用户轮块开始**按块**淘汰
   出站视图（不改写摘要、不触碰持久历史），保证发出去的请求永远
   合法；单条消息自身超预算时按 token 预算截断该消息。
   下一次压缩事件会把持久历史收敛回预算内，兜底路径随之消失。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from apitelegramchat.context_window import resolve_history_budget, split_history_blocks
from apitelegramchat.token_budget import json_token_count, truncate_to_token_budget


@dataclass(frozen=True)
class ContextSnapshot:
    messages: list[dict[str, Any]]
    dropped_messages: int
    estimated_tokens: int


def _message_token_count(message: dict[str, Any]) -> int:
    return json_token_count(message)


def _is_supported(message: object) -> bool:
    return isinstance(message, dict) and message.get("role") in {
        "user", "assistant", "tool", "system"
    }


def _fit_message_to_token_budget(message: dict[str, Any], token_budget: int) -> dict[str, Any] | None:
    """Trim an oversized plain-text message so the selected context stays bounded."""
    candidate = message.copy()
    if _message_token_count(candidate) <= token_budget:
        return candidate

    content = candidate.get("content")
    if not isinstance(content, str) or not content or token_budget <= 0:
        return None

    empty_content = candidate.copy()
    empty_content["content"] = ""
    available = token_budget - _message_token_count(empty_content)
    if available <= 0:
        return None

    candidate["content"] = truncate_to_token_budget(content, available, suffix="…")
    while available > 0 and _message_token_count(candidate) > token_budget:
        available -= 1
        candidate["content"] = truncate_to_token_budget(content, available, suffix="…")

    return candidate if _message_token_count(candidate) <= token_budget else None


def _tail_fit_messages(
    messages: list[dict[str, Any]], max_tokens: int
) -> tuple[list[dict[str, Any]], int]:
    """尾部装配兜底：从末尾回退累积，塞不下时按预算截断首条入选消息。

    仅在"单块消息自身超预算"的退化场景被调用（守卫的最后一道防线）。
    """
    selected_reversed: list[dict[str, Any]] = []
    used_tokens = 0

    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        message_tokens = _message_token_count(message)

        if used_tokens + message_tokens > max_tokens:
            if selected_reversed:
                break

            fitted = _fit_message_to_token_budget(message, max_tokens)
            if fitted is None:
                break

            selected_reversed.append(fitted)
            used_tokens += _message_token_count(fitted)
            continue

        selected_reversed.append(message.copy())
        used_tokens += message_tokens

    return list(reversed(selected_reversed)), used_tokens


def select_request_context(
    history: list[dict[str, Any]],
    *,
    max_tokens: int | None = None,
    model_max_context: Optional[int] = None,
    model_max_output: Optional[int] = None,
) -> ContextSnapshot:
    """返回合法且受 token 预算约束的请求上下文（守卫语义）。

    - 预算内：返回全部历史（浅拷贝）。前缀字节稳定是本函数的第一目标
      （prompt cache 命中的前提），因此不做任何"顺手"修剪；
    - 超预算：按用户轮块从最老开始淘汰出站视图（摘要槽位保留），
      持久历史不受影响；单条消息超预算时退化为尾部装配 + 截断。
    """
    if max_tokens is not None and max_tokens > 0:
        budget = int(max_tokens)
    else:
        budget = resolve_history_budget(model_max_context, model_max_output)

    supported = [message for message in history if _is_supported(message)]
    total_tokens = sum(_message_token_count(message) for message in supported)

    if total_tokens <= budget:
        # 快路径：全量透传（浅拷贝，防止出站改写污染持久历史）。
        selected = [message.copy() for message in supported]
        used_tokens = total_tokens
    else:
        # 兜底路径：先按块淘汰出站视图（摘要保留在头部稳定槽位）。
        # 至少保留最后一个块（当前活跃轮），由尾部装配做单消息级
        # 截断——否则一条超大的新消息会被整块丢掉而不是被截断。
        digest_msg, blocks = split_history_blocks(supported)
        block_tokens = [sum(_message_token_count(m) for m in block) for block in blocks]

        idx = 0
        dropped = 0
        while idx < len(blocks) - 1 and total_tokens - dropped > budget:
            dropped += block_tokens[idx]
            idx += 1

        remaining = ([digest_msg] if digest_msg is not None else []) + [
            message for block in blocks[idx:] for message in block
        ]
        # 单块自身超预算的退化场景：尾部装配 + 单消息截断。
        selected, used_tokens = _tail_fit_messages(remaining, budget)

    # Never start a request with an orphaned tool result.
    while selected and selected[0].get("role") == "tool":
        selected.pop(0)

    return ContextSnapshot(
        messages=selected,
        dropped_messages=max(0, len(history) - len(selected)),
        estimated_tokens=used_tokens,
    )
