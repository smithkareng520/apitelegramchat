"""请求侧上下文守卫（request-time guard）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from context_window import resolve_history_budget, split_history_blocks
from token_budget import json_token_count, truncate_to_token_budget
from core.messages import Message, TextBlock, as_message

@dataclass(frozen=True)
class ContextSnapshot:
    messages: list[Any]
    dropped_messages: int
    estimated_tokens: int

def _message_token_count(message: Any) -> int:
    # token 估算与出站载荷同源：Message 按其 OpenAI 投影计数
    # （meta 附件元数据不进请求体，也就不计入请求 token）。
    if isinstance(message, Message):
        return json_token_count(message.to_openai_dict())
    return json_token_count(message)

def _is_supported(message: object) -> bool:
    if isinstance(message, Message):
        return message.role in {"user", "assistant", "tool", "system"}
    return isinstance(message, dict) and message.get("role") in {
        "user", "assistant", "tool", "system"
    }

def _fit_message_to_token_budget(message: Any, token_budget: int) -> Any:
    """Trim an oversized plain-text message so the selected context stays bounded."""
    if token_budget <= 0:
        return None
    if _message_token_count(message) <= token_budget:
        return message

    m = as_message(message)
    # 仅纯文本（单 TextBlock）消息可无损截断；多模态/结构化消息无法
    # 在块语义内安全裁剪，放弃该消息。
    text_blocks = [b for b in m.blocks if isinstance(b, TextBlock)]
    if len(m.blocks) != 1 or len(text_blocks) != 1:
        return None

    original = text_blocks[0].text
    empty = Message(role=m.role, blocks=[], name=m.name, meta=dict(m.meta))
    available = token_budget - _message_token_count(empty)
    if available <= 0:
        return None

    # 原实现逐 token 递减 available：一条超大消息在预算边界附近会触发
    # O(token_budget) 次完整 JSON 序列化 + token 编码。这里利用
    # ``truncate_to_token_budget`` 的单调性二分查找最大可用文本预算，把
    # 最坏复杂度降到 O(log token_budget)，同时保持最终结果严格不超预算。
    def _candidate(budget: int) -> Message:
        text = truncate_to_token_budget(original, budget, suffix="…")
        return Message(role=m.role, blocks=[TextBlock(text)], name=m.name, meta=dict(m.meta))

    low, high = 0, available
    while low < high:
        mid = (low + high + 1) // 2
        candidate = _candidate(mid)
        if _message_token_count(candidate) <= token_budget:
            low = mid
        else:
            high = mid - 1

    candidate_text = truncate_to_token_budget(original, low, suffix="…")

    # meta 随拷贝保留（Responses 原生 output 快照、早持久化标记等都
    # 存在 meta 里；meta 永不进出站请求体）。
    fitted = Message(role=m.role, blocks=[TextBlock(candidate_text)], name=m.name, meta=dict(m.meta))
    return fitted if _message_token_count(fitted) <= token_budget else None

def _tail_fit_messages(messages: list, max_tokens: int) -> tuple[list, int]:
    """尾部装配兜底：从末尾回退累积，塞不下时按预算截断首条入选消息。

    仅在"单块消息自身超预算"的退化场景被调用（守卫的最后一道防线）。
    """
    selected_reversed: list = []
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

        selected_reversed.append(message)
        used_tokens += message_tokens

    return list(reversed(selected_reversed)), used_tokens

def select_request_context(
    history: list,
    *,
    max_tokens: int | None = None,
    model_max_context: Optional[int] = None,
    model_max_output: Optional[int] = None,
) -> ContextSnapshot:
    """返回合法且受 token 预算约束的请求上下文（守卫语义）。

    - 预算内：返回全部历史（引用透传，Message 不可变约定由调用方保证）。
      前缀字节稳定是本函数的第一目标（prompt cache 命中的前提），因此
      不做任何"顺手"修剪；
    - 超预算：按用户轮块从最老开始淘汰出站视图（摘要保留在头部稳定槽位），
      持久历史不受影响；单条消息超预算时退化为尾部装配 + 截断。
    """
    if max_tokens is not None and max_tokens > 0:
        budget = int(max_tokens)
    else:
        budget = resolve_history_budget(model_max_context, model_max_output)

    supported = [message for message in history if _is_supported(message)]
    total_tokens = sum(_message_token_count(message) for message in supported)

    if total_tokens <= budget:
        # 快路径：全量透传。Message 是不可变使用约定（历史追加-only），
        # 出站装饰（cache_control）只落在渲染产物上，持久对象不被触碰。
        selected = list(supported)
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
    while selected:
        first = selected[0]
        first_role = first.role if isinstance(first, Message) else first.get("role")
        if first_role == "tool":
            selected.pop(0)
        else:
            break

    return ContextSnapshot(
        messages=selected,
        dropped_messages=max(0, len(history) - len(selected)),
        estimated_tokens=used_tokens,
    )
