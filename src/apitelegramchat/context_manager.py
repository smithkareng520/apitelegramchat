"""Small, deterministic context selection for one Agent request.

The store may keep a complete conversation, but a request only receives a
bounded, structurally valid tail.  This keeps slow attachment resolution and
prompt growth from delaying the next visible draft.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


DEFAULT_MAX_MESSAGES = 32
DEFAULT_MAX_CHARS = 48_000


@dataclass(frozen=True)
class ContextSnapshot:
    messages: list[dict[str, Any]]
    dropped_messages: int
    estimated_chars: int


def _message_size(message: dict[str, Any]) -> int:
    """Estimate prompt cost without resolving remote attachments."""
    try:
        return len(json.dumps(message, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(message))


def _is_supported(message: object) -> bool:
    return isinstance(message, dict) and message.get("role") in {
        "user", "assistant", "tool", "system"
    }


def select_request_context(
    history: list[dict[str, Any]],
    *,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> ContextSnapshot:
    """Return the newest bounded history, without dangling tool messages.

    Selection is performed before any multimodal work.  The newest item is
    always retained when valid; older entries are included only while both
    budgets permit.  A leading tool result is discarded because its matching
    assistant tool-call is outside the selected window.
    """
    selected_reversed: list[dict[str, Any]] = []
    used = 0
    for message in reversed(history):
        if not _is_supported(message):
            continue
        size = _message_size(message)
        if selected_reversed and (
            len(selected_reversed) >= max_messages or used + size > max_chars
        ):
            break
        selected_reversed.append(message.copy())
        used += size

    selected = list(reversed(selected_reversed))
    while selected and selected[0].get("role") == "tool":
        selected.pop(0)

    return ContextSnapshot(
        messages=selected,
        dropped_messages=max(0, len(history) - len(selected)),
        estimated_chars=sum(_message_size(message) for message in selected),
    )
