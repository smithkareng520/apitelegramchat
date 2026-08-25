"""Shared, exact token counting and truncation helpers.

All model-facing content budgets in the application are expressed in tokens.  The
module uses ``tiktoken``'s ``o200k_base`` encoding by default, which is the
current OpenAI-family encoding suitable for multilingual (including Chinese)
content.  Deployments can override it with ``TOKEN_BUDGET_ENCODING``.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import tiktoken


DEFAULT_ENCODING_NAME = os.getenv("TOKEN_BUDGET_ENCODING", "o200k_base")


@lru_cache(maxsize=4)
def _get_encoding(name: str):
    """Return a configured tokenizer, falling back to a broadly supported one."""
    try:
        return tiktoken.get_encoding(name)
    except ValueError:
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(value: Any, *, encoding_name: str = DEFAULT_ENCODING_NAME) -> int:
    """Return the exact token count of a value after converting it to text."""
    if value is None:
        return 0
    text = value if isinstance(value, str) else str(value)
    if not text:
        return 0
    return len(_get_encoding(encoding_name).encode(text, disallowed_special=()))


def truncate_to_token_budget(
    value: Any,
    token_budget: int,
    *,
    suffix: str = "…[内容已按 token 预算截断]",
    encoding_name: str = DEFAULT_ENCODING_NAME,
) -> str:
    """Return text within ``token_budget`` while preserving valid Unicode.

    The suffix is included in the total budget when truncation is necessary.
    If the suffix alone exceeds the budget, it is itself shortened.  The helper
    intentionally operates on plain text; callers producing structured HTML
    should truncate at complete-block boundaries before using this final guard.
    """
    if token_budget < 0:
        raise ValueError("token_budget must be non-negative")
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not text or token_budget == 0:
        return ""

    encoding = _get_encoding(encoding_name)
    encoded = encoding.encode(text, disallowed_special=())
    if len(encoded) <= token_budget:
        return text

    encoded_suffix = encoding.encode(suffix, disallowed_special=()) if suffix else []
    if len(encoded_suffix) >= token_budget:
        return encoding.decode(encoded_suffix[:token_budget])

    keep = token_budget - len(encoded_suffix)
    return encoding.decode(encoded[:keep]) + suffix


def fits_token_budget(value: Any, token_budget: int) -> bool:
    """Return whether ``value`` fits within a non-negative token budget."""
    if token_budget < 0:
        raise ValueError("token_budget must be non-negative")
    return count_tokens(value) <= token_budget


def json_token_count(value: Any) -> int:
    """Count a JSON-like value without requiring callers to duplicate serialization."""
    import json

    try:
        return count_tokens(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return count_tokens(value)


__all__ = [
    "DEFAULT_ENCODING_NAME",
    "count_tokens",
    "fits_token_budget",
    "json_token_count",
    "truncate_to_token_budget",
]
