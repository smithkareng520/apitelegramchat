"""Small, deterministic context selection for one Agent request.

The store may keep a complete conversation, but a request only receives a
bounded, structurally valid tail.  Bounds are measured with the shared
``tiktoken`` tokenizer, so multilingual text consumes the same budget units
used by model APIs and tool outputs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from apitelegramchat.token_budget import json_token_count, truncate_to_token_budget


# A long conversation can easily exceed a model's useful context window.  Keep
# the latest 50 messages and, by default, no more than 50k exact input tokens.
DEFAULT_MAX_MESSAGES = int(os.getenv("CONTEXT_MAX_MESSAGES", "50"))
DEFAULT_MAX_TOKENS = int(os.getenv("CONTEXT_MAX_TOKENS", "50000"))


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


def select_request_context(
    history: list[dict[str, Any]],
    *,
    max_messages: int | None = None,
    max_tokens: int | None = None,
) -> ContextSnapshot:
    """Return a bounded, structurally valid tail of the conversation.

    A leading tool result is discarded because its matching assistant tool-call
    is outside the selected window.  The window is bounded by ``max_messages``
    and an exact tokenizer ``max_tokens`` budget, both configurable per call.
    """
    if max_messages is None:
        max_messages = DEFAULT_MAX_MESSAGES
    if max_tokens is None:
        max_tokens = DEFAULT_MAX_TOKENS

    selected_reversed: list[dict[str, Any]] = []
    used_tokens = 0
    for message in reversed(history):
        if not _is_supported(message):
            continue
        message_tokens = _message_token_count(message)
        if max_messages and len(selected_reversed) >= max_messages:
            break
        if max_tokens and used_tokens + message_tokens > max_tokens:
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

    selected = list(reversed(selected_reversed))
    while selected and selected[0].get("role") == "tool":
        selected.pop(0)

    return ContextSnapshot(
        messages=selected,
        dropped_messages=max(0, len(history) - len(selected)),
        estimated_tokens=sum(_message_token_count(message) for message in selected),
    )
