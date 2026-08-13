#!/usr/bin/env python3
"""Regression tests for two-pass compaction and structural budget fallback."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

TEMP_DATA_DIR = tempfile.TemporaryDirectory(prefix="apitelegramchat-budget-test-")
os.environ["APITELEGRAMCHAT_DATA_DIR"] = TEMP_DATA_DIR.name
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import apitelegramchat.app as app  # noqa: E402
from apitelegramchat.tool_context_compaction import compact_older_tool_rounds  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def build_target_history(rounds: int) -> list[dict]:
    history: list[dict] = []
    for index in range(rounds):
        call_id = f"call-{index}"
        history.extend(
            [
                {"role": "assistant", "content": "", "tool_calls": [call(call_id, "fetch_url", {"url": f"https://example.com/{index}", "metadata": "x" * 200})]},
                {"role": "tool", "tool_call_id": call_id, "content": "result " + "y" * 1_000},
            ]
        )
    return history


async def test_two_passes_compact_three_quarters_for_eight_rounds() -> None:
    history = build_target_history(8)
    first = await compact_older_tool_rounds(71, history)
    remaining = first.eligible_rounds - first.compacted_rounds
    second = await compact_older_tool_rounds(71, history, rounds_to_compact=max(1, remaining // 2))
    require(first.compacted_rounds == 4, "first pass must compact the older 50%")
    require(second.compacted_rounds == 2, "second pass must compact half of the remaining rounds")
    results = [message for message in history if message.get("role") == "tool"]
    compacted = [message for message in results if str(message.get("content", "")).startswith("Tool result archived at ")]
    require(len(compacted) == 6, "two passes must compact 6/8 = 75% of target rounds")
    require(not str(results[6]["content"]).startswith("Tool result archived at "), "newer retained quarter must stay verbose")
    require(not str(results[7]["content"]).startswith("Tool result archived at "), "newest retained round must stay verbose")


async def test_preflight_drops_oldest_non_system_blocks_after_two_passes() -> None:
    chat_id = 72
    model_name = "context-budget-test-model"
    original_model = app.SUPPORTED_MODELS.get(model_name)
    app.SUPPORTED_MODELS[model_name] = SimpleNamespace(max_context=1_800, max_output_tokens=200)
    app.user_models[chat_id] = model_name
    app.user_contexts[chat_id] = {
        "conversation_history": [
            {"role": "system", "content": "system instructions must remain"},
            {"role": "user", "content": "old user block " + "oldtoken " * 5_000},
            {"role": "assistant", "content": "old assistant block " + "oldreply " * 1_000},
            {"role": "user", "content": "recent user block " + "recenttoken " * 200},
            {"role": "assistant", "content": "recent assistant block " + "recentreply " * 100},
        ],
        "token_ledger": [{"input_tokens": 1, "output_tokens": 1}],
        "last_prompt_tokens": 10,
        "last_completion_tokens": 10,
    }
    try:
        accepted = await app.pre_flight_context_check(chat_id, {"role": "user", "content": "next request"})
        history = app.user_contexts[chat_id]["conversation_history"]
        require(accepted, "structural trim should make the request fit")
        require(history[0].get("role") == "system", "system message must never be removed")
        require(all("old user block" not in str(message.get("content", "")) for message in history), "oldest user block must be removed")
        require(any("recent user block" in str(message.get("content", "")) for message in history), "newest block must remain")
        require(app.user_contexts[chat_id]["token_ledger"] == [], "ledger must reset after structural trim")
    finally:
        app.user_contexts.pop(chat_id, None)
        app.user_models.pop(chat_id, None)
        if original_model is None:
            app.SUPPORTED_MODELS.pop(model_name, None)
        else:
            app.SUPPORTED_MODELS[model_name] = original_model


async def main() -> None:
    await test_two_passes_compact_three_quarters_for_eight_rounds()
    await test_preflight_drops_oldest_non_system_blocks_after_two_passes()
    print("context budget fallback validation: PASS")


if __name__ == "__main__":
    asyncio.run(main())
