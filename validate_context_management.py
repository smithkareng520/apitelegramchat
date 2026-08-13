#!/usr/bin/env python3
"""Regression tests for bounded Agent request context."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from apitelegramchat.context_manager import select_request_context  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_context_is_bounded_and_keeps_newest_message() -> None:
    history = [
        {"role": "user", "content": f"old-{index}-" + "x" * 100}
        for index in range(10)
    ]
    history.append({"role": "user", "content": "newest"})
    snapshot = select_request_context(history, max_messages=4, max_chars=500)

    require(len(snapshot.messages) <= 4, "message budget must be enforced")
    require(snapshot.messages[-1]["content"] == "newest", "newest message must survive")
    require(snapshot.dropped_messages > 0, "older history should be reported as dropped")


def test_context_never_starts_with_orphan_tool_result() -> None:
    history = [
        {"role": "assistant", "content": "old", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        {"role": "user", "content": "latest"},
    ]
    snapshot = select_request_context(history, max_messages=2, max_chars=10_000)

    require(snapshot.messages[0]["role"] != "tool", "context cannot begin with a tool result")
    require(snapshot.messages[-1]["content"] == "latest", "latest user message must remain")


if __name__ == "__main__":
    test_context_is_bounded_and_keeps_newest_message()
    test_context_never_starts_with_orphan_tool_result()
    print("context management validation: PASS")
