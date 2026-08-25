"""Small, deterministic context selection for one Agent request.

The store may keep a complete conversation, but a request only receives a
bounded, structurally valid tail.  This keeps slow attachment resolution and
prompt growth from delaying the next visible draft.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from apitelegramchat.token_utils import count_tokens


# 修复：原 DEFAULT_MAX_MESSAGES / DEFAULT_MAX_TOKENS = None 表示"无上限"，
# 实际上让 select_request_context 把整段 history 原样塞进 prompt。一个
# 长会话能轻易达到 100k+ tokens 的请求体，触发 413/上下文超限/费用
# 失控。改成可配置的环境变量，并设默认上限：
#   * DEFAULT_MAX_MESSAGES：保留最近 50 条消息
#   * DEFAULT_MAX_TOKENS：token 上限 60k
# 这些值仍很宽松，留有充足的上下文窗口，但能在意外长会话上踩刹车。
DEFAULT_MAX_MESSAGES = int(os.getenv("CONTEXT_MAX_MESSAGES", "50"))
DEFAULT_MAX_TOKENS = int(os.getenv("CONTEXT_MAX_TOKENS", "60000"))


@dataclass(frozen=True)
class ContextSnapshot:
    messages: list[dict[str, Any]]
    dropped_messages: int
    estimated_tokens: int


def _message_size(message: dict[str, Any]) -> int:
    """Estimate prompt cost without resolving remote attachments."""
    try:
        return count_tokens(json.dumps(message, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return count_tokens(str(message))


def _is_supported(message: object) -> bool:
    return isinstance(message, dict) and message.get("role") in {
        "user", "assistant", "tool", "system"
    }


def select_request_context(
    history: list[dict[str, Any]],
    *,
    max_messages: int | None = None,
    max_tokens: int | None = None,
) -> ContextSnapshot:
    """Return a bounded, structurally valid tail of the conversation.

    A leading tool result is discarded because its matching assistant
    tool-call is outside the selected window. The window is bounded by
    ``max_messages`` (default DEFAULT_MAX_MESSAGES) and ``max_tokens``
    (default DEFAULT_MAX_TOKENS) — both can be configured per-call.
    """
    if max_messages is None:
        max_messages = DEFAULT_MAX_MESSAGES
    if max_tokens is None:
        max_tokens = DEFAULT_MAX_TOKENS

    selected_reversed: list[dict[str, Any]] = []
    used = 0
    for message in reversed(history):
        if not _is_supported(message):
            continue
        size = _message_size(message)
        if max_messages and len(selected_reversed) >= max_messages:
            break
        if max_tokens and used + size > max_tokens and selected_reversed:
            # 这条加进去会超token 上限，且至少已有一条入选，停在这里。
            break
        selected_reversed.append(message.copy())
        used += size

    selected = list(reversed(selected_reversed))
    while selected and selected[0].get("role") == "tool":
        selected.pop(0)

    return ContextSnapshot(
        messages=selected,
        dropped_messages=max(0, len(history) - len(selected)),
        estimated_tokens=sum(_message_size(message) for message in selected),
    )
