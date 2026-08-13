#!/usr/bin/env python3
"""Regression tests for durable compaction of selected tool history entries."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path


TEMP_DATA_DIR = tempfile.TemporaryDirectory(prefix="apitelegramchat-context-test-")
os.environ["APITELEGRAMCHAT_DATA_DIR"] = TEMP_DATA_DIR.name
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apitelegramchat.tool_context_compaction import (  # noqa: E402
    ARCHIVE_DIR,
    compact_older_tool_rounds,
)
from apitelegramchat.workspace_paths import workspace_workdir  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def build_history() -> list[dict]:
    target_specs = [
        ("wikipedia", {"query": "人工智能", "lang": "zh", "_description": "x" * 300}),
        ("fetch_url", {"url": "https://example.com/article", "timeout": 30, "unused": "x" * 300}),
        ("text_editor", {"command": "create", "path": "notes/item.txt", "file_text": "x" * 300}),
    ]
    history: list[dict] = []
    for index in range(6):
        name, arguments = target_specs[index % len(target_specs)]
        target_id = f"target-{index}"
        other_id = f"other-{index}"
        history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    call(target_id, name, arguments),
                    call(other_id, "weather", {"city": "Beijing", "verbose": "x" * 300}),
                ],
            }
        )
        history.append({"role": "tool", "tool_call_id": target_id, "content": f"original result {index}: " + "r" * 1_000})
        history.append({"role": "tool", "tool_call_id": other_id, "content": f"weather result {index}: " + "w" * 1_000})
    return history


async def test_compacts_only_older_half_and_archives_payloads() -> None:
    chat_id = 17
    history = build_history()
    original_target_args = [
        message["tool_calls"][0]["function"]["arguments"]
        for message in history
        if message.get("role") == "assistant"
    ]
    original_target_results = [
        message["content"]
        for message in history
        if message.get("role") == "tool" and str(message.get("tool_call_id", "")).startswith("target-")
    ]
    original_weather_args = [
        message["tool_calls"][1]["function"]["arguments"]
        for message in history
        if message.get("role") == "assistant"
    ]

    stats = await compact_older_tool_rounds(chat_id, history)
    require(stats.eligible_rounds == 6, "all six target rounds should be eligible")
    require(stats.compacted_rounds == 3, "only the older half of target rounds should compact")
    require(stats.compacted_calls == 3, "exactly three target calls should compact")
    require(stats.archived_bytes > 0, "archive should contain the original payloads")

    assistants = [message for message in history if message.get("role") == "assistant"]
    target_results = [
        message for message in history
        if message.get("role") == "tool" and str(message.get("tool_call_id", "")).startswith("target-")
    ]
    for index in range(3):
        target_call = assistants[index]["tool_calls"][0]
        minimal = json.loads(target_call["function"]["arguments"])
        name = target_call["function"]["name"]
        expected_keys = {"wikipedia": {"query", "lang"}, "fetch_url": {"url"}, "text_editor": {"command", "path"}}[name]
        require(set(minimal) == expected_keys, f"round {index} should retain only stable locator fields")
        pointer = target_results[index]["content"]
        require(pointer.startswith("Tool result archived at "), f"round {index} should have a retrieval pointer")
        rel_path = pointer.split("Tool result archived at ", 1)[1].split(". Use text_editor", 1)[0]
        require(rel_path.startswith(ARCHIVE_DIR + "/"), "archive must live in the private workspace")
        archive_path = workspace_workdir(chat_id) / rel_path
        require(archive_path.is_file(), "pointer must resolve to a readable archive file")
        payload = json.loads(archive_path.read_text(encoding="utf-8"))
        require(payload["tool_call"]["function"]["arguments"] == original_target_args[index], "full original arguments must survive in archive")
        require(payload["tool_result"]["content"] == original_target_results[index], "full original result must survive in archive")

    for index in range(3, 6):
        require(
            assistants[index]["tool_calls"][0]["function"]["arguments"] == original_target_args[index],
            "newer half must remain verbatim",
        )
        require(target_results[index]["content"] == original_target_results[index], "newer result must remain verbatim")

    for index, assistant in enumerate(assistants):
        require(
            assistant["tool_calls"][1]["function"]["arguments"] == original_weather_args[index],
            "non-target tool calls must not be compacted",
        )


async def test_no_compaction_when_fewer_than_two_target_rounds() -> None:
    history = [
        {"role": "assistant", "content": "", "tool_calls": [call("single", "wikipedia", {"query": "测试", "extra": "x" * 200})]},
        {"role": "tool", "tool_call_id": "single", "content": "full result " + "x" * 1_000},
    ]
    before_call = history[0]["tool_calls"][0]["function"]["arguments"]
    before_result = history[1]["content"]
    stats = await compact_older_tool_rounds(18, history)
    require(stats.compacted_calls == 0, "a lone round must stay uncompressed")
    require(history[0]["tool_calls"][0]["function"]["arguments"] == before_call, "single call must remain unchanged")
    require(history[1]["content"] == before_result, "single result must remain unchanged")


async def main() -> None:
    await test_compacts_only_older_half_and_archives_payloads()
    await test_no_compaction_when_fewer_than_two_target_rounds()
    print("tool context compaction validation: PASS")


if __name__ == "__main__":
    asyncio.run(main())
