"""Token accounting and token-budget truncation helpers.

All internal text budgets should be expressed in tokens rather than Python
string length.  The project talks to multiple OpenAI-compatible providers, so
the tokenizer is configurable.  ``tiktoken`` model mappings are used when a
model is supplied; otherwise the conservative ``cl100k_base`` encoding is
used.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Iterable

import tiktoken


@lru_cache(maxsize=32)
def _encoding_for(model: str | None = None):
    configured = os.getenv("TOKEN_ENCODING", "").strip()
    if configured:
        try:
            return tiktoken.get_encoding(configured)
        except Exception:
            pass

    if model:
        try:
            return tiktoken.encoding_for_model(model)
        except Exception:
            pass

    # OpenRouter can expose model IDs unknown to tiktoken. cl100k_base is a
    # stable fallback and, importantly, keeps every budget in token units.
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: object, model: str | None = None) -> int:
    """Return the tiktoken token count for arbitrary text-like input."""
    if text is None:
        return 0
    value = text if isinstance(text, str) else str(text)
    return len(_encoding_for(model).encode(value, disallowed_special=()))


def truncate_to_tokens(
    text: object,
    max_tokens: int,
    *,
    suffix: str = "…[内容过长已截断]",
    model: str | None = None,
) -> str:
    """Truncate text to a token budget, preferring a complete suffix.

    The returned value is guaranteed to be within ``max_tokens`` tokens
    (unless a non-positive budget is supplied, in which case it is empty).
    """
    value = text if isinstance(text, str) else str(text or "")
    budget = max(0, int(max_tokens))
    if not value or budget == 0:
        return ""
    enc = _encoding_for(model)
    tokens = enc.encode(value, disallowed_special=())
    if len(tokens) <= budget:
        return value

    suffix_tokens = enc.encode(suffix, disallowed_special=())
    keep = max(0, budget - len(suffix_tokens))
    if keep == 0:
        return enc.decode(tokens[:budget])
    return enc.decode(tokens[:keep]) + enc.decode(suffix_tokens[:budget - keep])


def truncate_blocks_to_tokens(
    blocks: Iterable[str],
    max_tokens: int,
    *,
    separator: str = "\n",
    model: str | None = None,
) -> tuple[list[str], bool]:
    """Keep whole blocks until the combined token budget is exhausted."""
    budget = max(0, int(max_tokens))
    kept: list[str] = []
    used = 0
    separator_tokens = count_tokens(separator, model=model) if kept else 0

    for block in blocks:
        block = str(block)
        cost = count_tokens(block, model=model)
        extra = separator_tokens if kept else 0
        if used + extra + cost > budget:
            return kept, True
        kept.append(block)
        used += extra + cost
    return kept, False
