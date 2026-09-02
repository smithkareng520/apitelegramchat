"""Small, deterministic context selection for one Agent request.

The store may keep a complete conversation, but a request only receives a
bounded, structurally valid tail.  Bounds are measured with the shared
``tiktoken`` tokenizer, so multilingual text consumes the same budget units
used by model APIs and tool outputs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from apitelegramchat.token_budget import json_token_count, truncate_to_token_budget


# A long conversation can easily exceed a model's useful context window.  Keep
# the latest 50 messages and, by default, no more than 50k exact input tokens.
DEFAULT_MAX_MESSAGES = int(os.getenv("CONTEXT_MAX_MESSAGES", "50"))

# 默认 token 上限：优先使用环境变量，其次在调用时根据模型动态计算
DEFAULT_MAX_TOKENS_ENV = int(os.getenv("CONTEXT_MAX_TOKENS", "0"))


# =====================================================================
# 量化淘汰步长（prompt cache 关键路径）
# ---------------------------------------------------------------------
# 普通滑动窗口每轮多丢 1 条最旧消息，窗口起点逐轮变化：对 DeepSeek / GLM /
# Gemini / OpenAI 这类靠"前缀完全匹配"命中的隐式缓存，起点一变，从窗口
# 头部到结尾的整段历史全部 miss，只剩 system+tools 前缀还能命中。
# 把淘汰量向上取整到本步长的整数倍后，淘汰量成为历史长度的阶梯函数：
# 历史每增长 step 条，窗口起点才前进 step 条，中间若干轮保持不变，
# 历史前缀得以跨轮命中缓存。步长同时作用于消息数上限与 token 上限
# 两种触顶场景。设为 1 可恢复旧版"逐轮滑动"的行为。
# 例：max=50、step=10 时，历史 51..60 条的起点都是第 10 条（窗口 41..50
# 条），历史 61..70 条的起点都是第 20 条，以此类推。
# =====================================================================
def _non_negative_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return max(0, int(str(raw).strip())) if raw is not None and str(raw).strip() else default
    except (TypeError, ValueError):
        return default


EVICT_HEADROOM_MESSAGES = _non_negative_int_env("CONTEXT_EVICT_HEADROOM_MESSAGES", 10)


@dataclass(frozen=True)
class ContextSnapshot:
    messages: list[dict[str, Any]]
    dropped_messages: int
    estimated_tokens: int


def _message_token_count(message: dict[str, Any]) -> int:
    """Estimate serialized prompt cost without resolving remote attachments."""
    return json_token_count(message)


def _is_supported(message: object) -> bool:
    return isinstance(message, dict) and message.get("role") in {
        "user", "assistant", "tool", "system"
    }


def _fit_message_to_token_budget(message: dict[str, Any], token_budget: int) -> dict[str, Any] | None:
    """Trim an oversized plain-text message so the selected context stays hard-bounded."""
    candidate = message.copy()
    if _message_token_count(candidate) <= token_budget:
        return candidate
    content = candidate.get("content")
    if not isinstance(content, str) or not content or token_budget <= 0:
        return None

    empty_content = candidate.copy()
    empty_content["content"] = ""
    available_content_tokens = token_budget - _message_token_count(empty_content)
    if available_content_tokens <= 0:
        return None

    candidate["content"] = truncate_to_token_budget(content, available_content_tokens, suffix="…")
    # JSON serialization can add a small tokenizer-boundary difference. Tighten
    # the content allocation until the full serialized message fits exactly.
    while available_content_tokens > 0 and _message_token_count(candidate) > token_budget:
        available_content_tokens -= 1
        candidate["content"] = truncate_to_token_budget(content, available_content_tokens, suffix="…")
    return candidate if _message_token_count(candidate) <= token_budget else None


def _quantized_drop(natural_drop: int, supported_len: int, keep_min: int) -> int:
    """把自然淘汰量向上取整到步长的整数倍（缓存友好的淘汰核心）。

    natural_drop 是普通滑窗规则（消息数 / token 上限，从最新端往旧选取）
    算出的淘汰量；量化后淘汰量成为历史长度的阶梯函数，窗口起点在连续
    若干轮内保持字节级一致，隐式前缀缓存（DeepSeek/GLM/Gemini/OpenAI）
    的历史段得以持续命中。
    """
    step = max(1, EVICT_HEADROOM_MESSAGES)
    if natural_drop <= 0:
        return 0
    quantized = ((natural_drop + step - 1) // step) * step
    # 不越过"至少保留 keep_min 条"的底线（如 max_messages=0 时允许清空）。
    max_drop = max(0, supported_len - keep_min)
    return min(quantized, max_drop) if max_drop < supported_len else quantized


def select_request_context(
    history: list[dict[str, Any]],
    *,
    max_messages: int | None = None,
    max_tokens: int | None = None,
    model_max_context: Optional[int] = None,
) -> ContextSnapshot:
    """Return a bounded, structurally valid tail of the conversation.

    A leading tool result is discarded because its matching assistant tool-call
    is outside the selected window.  The window is bounded by ``max_messages``
    and an exact tokenizer ``max_tokens`` budget, both configurable per call.

    缓存友好的淘汰：先用原始规则（消息数 / token 上限，从最新端往旧选取）
    得出自然淘汰量，再把淘汰量向上量化到 EVICT_HEADROOM_MESSAGES 的整数
    倍（见 _quantized_drop）。窗口起点因此按阶梯前进而非逐轮滑动，两次
    跳跃之间的若干轮里请求前缀字节级一致，隐式前缀缓存可跨轮命中。
    设 CONTEXT_EVICT_HEADROOM_MESSAGES=1 可恢复旧版逐轮滑动行为。
    
    Args:
        history: 完整的历史消息列表
        max_messages: 最大消息数量（可选，默认 DEFAULT_MAX_MESSAGES）
        max_tokens: 最大 token 数（可选，优先使用此值，其次根据模型动态计算）
        model_max_context: 模型的最大上下文窗口（用于动态计算 max_tokens）
    """
    if max_messages is None:
        max_messages = DEFAULT_MAX_MESSAGES
    
    # 动态上下文策略：
    # 1. 如果显式传入 max_tokens，直接使用
    # 2. 否则如果提供了 model_max_context，使用模型上下文的 80% 作为软上限
    # 3. 否则使用环境变量 DEFAULT_MAX_TOKENS_ENV（如果 > 0）
    # 4. 最后退回到 50000 的硬编码默认值
    if max_tokens is None:
        if model_max_context is not None:
            # 使用模型上下文的 80% 作为动态 token 上限，留出余量给响应和工具输出
            max_tokens = int(model_max_context * 0.8)
        elif DEFAULT_MAX_TOKENS_ENV > 0:
            max_tokens = DEFAULT_MAX_TOKENS_ENV
        else:
            max_tokens = 50000  # 硬编码默认值

    supported = [message for message in history if _is_supported(message)]
    total = len(supported)

    selected_reversed: list[dict[str, Any]] = []
    used_tokens = 0
    natural_drop = 0
    for idx in range(total - 1, -1, -1):
        message = supported[idx]
        message_tokens = _message_token_count(message)
        # 用 `is not None` 而非 falsy 检查，避免 max_messages=0/max_tokens=0
        # 被当作"无限制"而非"零消息/零 token"——语义颠倒。
        if max_messages is not None and len(selected_reversed) >= max_messages:
            natural_drop = idx + 1
            break
        if max_tokens is not None and used_tokens + message_tokens > max_tokens:
            if selected_reversed:
                natural_drop = idx + 1
                break
            fitted = _fit_message_to_token_budget(message, max_tokens)
            if fitted is None:
                natural_drop = idx + 1
                break
            selected_reversed.append(fitted)
            used_tokens += _message_token_count(fitted)
            continue
        selected_reversed.append(message.copy())
        used_tokens += message_tokens

    # ---- 量化淘汰：一次性多丢 (step - natural % step) 条，换取后续轮次稳定 ----
    keep_min = 0 if (max_messages is not None and max_messages <= 0) else 1
    target_drop = _quantized_drop(natural_drop, total, keep_min)
    while len(selected_reversed) > keep_min and (total - len(selected_reversed)) < target_drop:
        selected_reversed.pop()  # 列表末尾 = 最旧消息

    selected = list(reversed(selected_reversed))
    while selected and selected[0].get("role") == "tool":
        selected.pop(0)

    # 复用循环中累计的 used_tokens（拟合消息用的是拟合后计数），
    # 避免对全部选中消息再做一次完整的序列化+编码。
    return ContextSnapshot(
        messages=selected,
        dropped_messages=max(0, len(history) - len(selected)),
        estimated_tokens=used_tokens,
    )
