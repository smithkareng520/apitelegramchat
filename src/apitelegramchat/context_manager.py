"""Deterministic context selection for one Agent request.

The store may keep a complete conversation, but a request only receives a
valid tail bounded by tokenizer based token budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
import os

from apitelegramchat.token_budget import json_token_count, truncate_to_token_budget


DEFAULT_MAX_TOKENS_ENV = int(os.getenv("CONTEXT_MAX_TOKENS", "0"))


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


def select_request_context(
    history: list[dict[str, Any]],
    *,
    max_tokens: int | None = None,
    model_max_context: Optional[int] = None,
) -> ContextSnapshot:
    """Return a structurally valid context tail bounded only by token budget.

    Message-count based eviction has intentionally been removed. Context
    trimming is controlled by token budget only, which avoids artificial
    message-count boundaries and keeps the prompt prefix more stable for
    provider-side prompt/KV caching.
    """
    if max_tokens is None:
        if model_max_context is not None:
            max_tokens = int(model_max_context * 0.8)
        elif DEFAULT_MAX_TOKENS_ENV > 0:
            max_tokens = DEFAULT_MAX_TOKENS_ENV
        else:
            max_tokens = 50000

    supported = [message for message in history if _is_supported(message)]

    selected_reversed: list[dict[str, Any]] = []
    used_tokens = 0

    for idx in range(len(supported) - 1, -1, -1):
        message = supported[idx]
        message_tokens = _message_token_count(message)

        if max_tokens is not None and used_tokens + message_tokens > max_tokens:
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

    # Never start a request with an orphaned tool result.
    while selected and selected[0].get("role") == "tool":
        selected.pop(0)

    return ContextSnapshot(
        messages=selected,
        dropped_messages=max(0, len(history) - len(selected)),
        estimated_tokens=used_tokens,
    )
