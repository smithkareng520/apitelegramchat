"""OpenAI Responses API wire-shape helpers.

This module is deliberately independent from the agent/tool orchestration layer.
It owns only the protocol boundary:
  * Chat-style function definitions -> native Responses tool definitions
  * internal Message -> Responses input items
  * native Response output items -> replayable Responses input items
  * native output items -> the small Chat-shaped tool-call envelope required by
    the existing tool executor
  * lossless JSON snapshots for output items that need to survive a local
    re-bootstrap (notably reasoning / future output item variants)
"""
from __future__ import annotations

import copy
import json
from typing import Any, Iterable, Optional

from core.messages import (
    DocumentBlock,
    ImageBlock,
    Message,
    TextBlock,
)


# These keys live only in Message.meta and therefore never leak into other
# provider protocols. They are intentionally stable because Message.meta is
# part of the persisted canonical history.
RESPONSES_OUTPUT_ITEMS_META_KEY = "_openai_responses_output_items"
RESPONSES_OUTPUT_MODEL_META_KEY = "_openai_responses_output_model"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _plain_item(item: Any) -> dict[str, Any]:
    """Return a JSON-like dict without depending on the OpenAI SDK classes."""
    if isinstance(item, dict):
        return copy.deepcopy(item)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(exclude_none=True, mode="json")
        except TypeError:
            return model_dump(exclude_none=True)
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, dict):
            return copy.deepcopy(value)
    # Last-resort projection for OpenAI-compatible proxy objects.
    out: dict[str, Any] = {}
    for key in (
        "type", "id", "role", "content", "status", "call_id", "name",
        "arguments", "summary", "encrypted_content", "phase", "namespace",
        "caller", "async", "output",
    ):
        value = getattr(item, key, None)
        if value is not None:
            out[key] = copy.deepcopy(value)
    return out


def output_items_to_input_items(output_items: Iterable[Any]) -> list[dict[str, Any]]:
    """Preserve OpenAI output items verbatim for replay.

    OpenAI explicitly documents replaying the complete ``response.output``
    sequence as Responses input. Keeping the original item variants avoids
    collapsing reasoning, refusal, annotations, or future output types into
    a Chat Completions approximation.
    """
    out: list[dict[str, Any]] = []
    for item in output_items or []:
        plain = _plain_item(item)
        if plain.get("type"):
            out.append(plain)
    return out


def response_item_text(item: Any) -> str:
    """Extract user-visible text/refusal text from a response output item."""
    if _get(item, "type") != "message":
        return ""
    parts = _get(item, "content") or []
    pieces: list[str] = []
    for part in parts:
        ptype = _get(part, "type")
        if ptype == "output_text":
            text = _get(part, "text", "") or ""
            if text:
                pieces.append(str(text))
        elif ptype == "refusal":
            refusal = _get(part, "refusal", "") or ""
            if refusal:
                pieces.append(str(refusal))
    return "".join(pieces)


def output_text_from_items(output_items: Iterable[Any]) -> str:
    """Aggregate message output text and refusal content in output order."""
    return "".join(response_item_text(item) for item in (output_items or []))


def is_completed_function_call(item: Any) -> bool:
    """A tool call is executable only once its item is complete.

    The status field is optional on some Responses-compatible gateways, so
    absence is treated as completed. Explicit ``in_progress`` / ``incomplete``
    calls are never executed.
    """
    if _get(item, "type") != "function_call":
        return False
    status = _get(item, "status")
    return status in (None, "", "completed")


def response_function_calls_to_chat(output_items: Iterable[Any]) -> list[dict[str, Any]]:
    """Project completed Responses function_call items into the executor shape."""
    calls: list[dict[str, Any]] = []
    for item in output_items or []:
        if not is_completed_function_call(item):
            continue
        call_id = str(_get(item, "call_id") or _get(item, "id") or "")
        name = str(_get(item, "name") or "")
        if not call_id or not name:
            continue
        arguments = _get(item, "arguments")
        if arguments is None:
            arguments = "{}"
        elif not isinstance(arguments, str):
            try:
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                arguments = "{}"
        calls.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments,
            },
        })
    return calls


def _block_to_responses_content_part(block: Any) -> Optional[dict[str, Any]]:
    if isinstance(block, TextBlock):
        return {"type": "input_text", "text": block.text} if block.text else None
    if isinstance(block, ImageBlock):
        if not block.url:
            return None
        return {
            "type": "input_image",
            "image_url": block.url,
            "detail": block.detail or "auto",
        }
    if isinstance(block, DocumentBlock):
        if block.data_url:
            return {
                "type": "input_file",
                "filename": block.filename or "document.pdf",
                "file_data": block.data_url,
            }
        if block.url:
            # Responses input_file has no generic "fetch this URL" source.
            # Keep the URL explicit instead of inventing a non-standard field.
            return {
                "type": "input_text",
                "text": f"[document] {block.filename or block.url}: {block.url}",
            }
    return None


def _blocks_to_responses_content(blocks: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        part = _block_to_responses_content_part(block)
        if part is not None:
            out.append(part)
    return out


def _native_items_match_message(message: Message, native_items: list[dict[str, Any]]) -> bool:
    """Reject stale raw output snapshots after local history rewrites."""
    native_text = output_text_from_items(native_items)
    if native_text != message.text():
        return False

    native_calls = response_function_calls_to_chat(native_items)
    message_calls = []
    for tc in message.tool_calls():
        message_calls.append({
            "id": tc.id,
            "name": tc.name,
            "arguments": copy.deepcopy(tc.arguments or {}),
        })

    normalized_native: list[dict[str, Any]] = []
    for call in native_calls:
        raw_args = call["function"].get("arguments") or "{}"
        try:
            parsed_args = json.loads(raw_args)
        except (TypeError, json.JSONDecodeError):
            parsed_args = raw_args
        normalized_native.append({
            "id": call.get("id") or "",
            "name": call["function"].get("name") or "",
            "arguments": parsed_args,
        })
    return normalized_native == message_calls


def message_to_responses_input_items(
    message: Message,
    *,
    model: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Render one canonical Message into native Responses input items."""
    if message.role == "tool":
        tr = message.tool_result_block()
        if tr is None:
            return []
        output = tr.content
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        return [{
            "type": "function_call_output",
            "call_id": tr.tool_call_id,
            "output": output,
        }]

    if message.role == "user":
        content = _blocks_to_responses_content(message.blocks)
        return (
            [{"type": "message", "role": "user", "content": content}]
            if content else []
        )

    if message.role == "assistant":
        native_items = message.meta.get(RESPONSES_OUTPUT_ITEMS_META_KEY)
        native_model = message.meta.get(RESPONSES_OUTPUT_MODEL_META_KEY)
        if (
            isinstance(native_items, list)
            and native_items
            and all(isinstance(item, dict) for item in native_items)
            and _native_items_match_message(message, native_items)
        ):
            # OpenAI's documented manual-history flow replays the complete
            # response.output sequence. Do not collapse it merely because the
            # next request uses a different model; native items can carry
            # phase, encrypted reasoning, annotations, and other state that
            # ordinary assistant text cannot represent.
            return copy.deepcopy(native_items)

        items: list[dict[str, Any]] = []
        text = message.text()
        if text:
            # The documented manual-history shape for an assistant message is
            # the ordinary role/content message. Keep the wire shape simple.
            items.append({
                "role": "assistant",
                "content": text,
            })
        for tc in message.tool_calls():
            try:
                args_json = json.dumps(tc.arguments or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                args_json = "{}"
            item: dict[str, Any] = {
                "type": "function_call",
                "call_id": tc.id,
                "name": tc.name,
                "arguments": args_json,
            }
            if tc.id:
                item["id"] = tc.id
            if tc.extra:
                for key, value in tc.extra.items():
                    if key not in item:
                        item[key] = copy.deepcopy(value)
            items.append(item)
        return items

    return []


def messages_to_responses_request(
    messages: Iterable[Any],
    *,
    model: Optional[str] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(instructions, input_items)`` for a Responses request."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []

    for raw in messages:
        msg = raw if isinstance(raw, Message) else Message.from_openai_dict(raw)
        if msg.role == "system":
            text = msg.text()
            if text:
                instructions.append(text)
            continue
        items.extend(message_to_responses_input_items(msg, model=model))

    return "\n\n".join(instructions), items


def attach_output_items_to_message(
    message: Message,
    output_items: Iterable[Any],
    *,
    model: str,
) -> None:
    """Persist a replayable native output snapshot without polluting Message blocks."""
    snapshot = output_items_to_input_items(output_items)
    if not snapshot:
        return
    message.meta[RESPONSES_OUTPUT_ITEMS_META_KEY] = snapshot
    message.meta[RESPONSES_OUTPUT_MODEL_META_KEY] = model


def tools_to_responses(tools: Optional[list[Any]]) -> Optional[list[dict[str, Any]]]:
    """Convert Chat Completions function definitions to native Responses tools."""
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type and tool_type != "function":
            # Responses has first-class hosted/native tools (for example web
            # search, file search, MCP, and future tool kinds). They are already
            # in their native wire shape and must not be dropped by a Chat-style
            # function converter.
            converted.append(copy.deepcopy(tool))
            continue
        if tool_type == "function" and "function" not in tool and tool.get("name"):
            converted.append(copy.deepcopy(tool))
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = fn.get("name")
        if not name:
            continue
        flat: dict[str, Any] = {
            "type": "function",
            "name": str(name),
            "description": fn.get("description", "") or "",
            "parameters": copy.deepcopy(
                fn.get("parameters") or {"type": "object", "properties": {}}
            ),
        }
        if fn.get("strict") is not None:
            flat["strict"] = bool(fn.get("strict"))
        # Keep Responses-native function options when a caller already supplied
        # them through the Chat-shaped compatibility envelope.
        for key in ("namespace", "description"):
            if key in fn and key not in flat and fn[key] is not None:
                flat[key] = copy.deepcopy(fn[key])
        converted.append(flat)
    return converted or None


__all__ = [
    "RESPONSES_OUTPUT_ITEMS_META_KEY",
    "RESPONSES_OUTPUT_MODEL_META_KEY",
    "attach_output_items_to_message",
    "is_completed_function_call",
    "message_to_responses_input_items",
    "messages_to_responses_request",
    "output_items_to_input_items",
    "output_text_from_items",
    "response_function_calls_to_chat",
    "tools_to_responses",
]
