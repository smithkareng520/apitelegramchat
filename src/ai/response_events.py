"""Typed/dict-neutral helpers for OpenAI Responses stream events."""
from __future__ import annotations

import copy
from typing import Any


def event_field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from SDK models or raw JSON dictionaries."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def event_type(event: Any) -> str:
    value = event_field(event, "type", "")
    return str(value or "")


def plain_response_item(item: Any) -> dict[str, Any]:
    """Convert an SDK output item to a JSON-like snapshot."""
    if isinstance(item, dict):
        return copy.deepcopy(item)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(exclude_none=True, mode="json")
        except TypeError:
            return model_dump(exclude_none=True)
    return {
        key: copy.deepcopy(value)
        for key in (
            "type", "id", "role", "status", "call_id", "name",
            "arguments", "summary", "encrypted_content", "phase",
            "namespace", "caller", "content",
        )
        if (value := getattr(item, key, None)) is not None
    }


def merge_function_call_item(
    states: dict[str, dict[str, str]],
    item: Any,
) -> tuple[str | None, dict[str, str] | None, bool]:
    """Merge a streamed function_call item, preferring final snapshot fields."""
    item_id = str(event_field(item, "id", "") or "")
    if not item_id:
        return None, None, False
    call_id = str(event_field(item, "call_id", "") or item_id)
    name = str(event_field(item, "name", "") or "")
    arguments = str(event_field(item, "arguments", "") or "")
    state = states.get(item_id)
    created = state is None
    if state is None:
        state = {
            "call_id": call_id,
            "name": name,
            "args_json": arguments,
        }
        states[item_id] = state
    else:
        state["call_id"] = call_id
        if name:
            state["name"] = name
        if arguments:
            state["args_json"] = arguments
    return item_id, state, created


def incomplete_reason(response: Any, fallback: str = "") -> str:
    """Extract Responses ``incomplete_details.reason`` with a safe fallback."""
    details = event_field(response, "incomplete_details")
    reason = event_field(details, "reason") if details is not None else None
    return str(reason) if reason else fallback


def response_error_message(event: Any, response: Any = None) -> str:
    """Extract the useful message from error/failed Responses events."""
    for candidate in (
        event_field(event, "message"),
        event_field(event_field(event, "error"), "message"),
        event_field(event_field(response, "error"), "message") if response is not None else None,
    ):
        if candidate:
            return str(candidate)
    error = event_field(event, "error") or (event_field(response, "error") if response is not None else None)
    return str(error) if error else "unknown error"


def response_output_items(response: Any) -> list[Any]:
    output = event_field(response, "output")
    return list(output or []) if isinstance(output, (list, tuple)) else []


__all__ = [
    "event_field",
    "event_type",
    "incomplete_reason",
    "merge_function_call_item",
    "plain_response_item",
    "response_error_message",
    "response_output_items",
]
