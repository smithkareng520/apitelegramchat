"""Archive large tool payloads and replace old history entries with durable pointers.

The archive is deliberately kept inside the private workspace so the model can
retrieve a prior payload with the existing ``text_editor`` tool.  Only selected
read-mostly tools are compacted; tool-call/result pairing and provider-required
IDs are always retained.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from apitelegramchat.workspace_paths import workspace_workdir
from apitelegramchat.workspace_utils import _ensure_runtime_workspace, _get_workspace_lock

logger = logging.getLogger(__name__)

ARCHIVE_DIR = ".context-archive/tool-results"
TARGET_TOOLS = frozenset({"wikipedia", "fetch_url", "text_editor"})
_POINTER_PREFIX = "Tool result archived at "


@dataclass(frozen=True)
class ToolCompactionStats:
    """Counts produced by one idempotent history compaction pass."""

    eligible_calls: int = 0
    compacted_calls_count: int = 0
    compacted_calls: int = 0
    archived_bytes: int = 0


def _is_archived_pointer(content: object) -> bool:
    return isinstance(content, str) and content.startswith(_POINTER_PREFIX)


def _tool_name(tool_call: object) -> str:
    if not isinstance(tool_call, dict):
        return ""
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    return name if isinstance(name, str) else ""


def _tool_call_id(tool_call: object) -> str:
    if not isinstance(tool_call, dict):
        return ""
    value = tool_call.get("id")
    return value if isinstance(value, str) else ""


def _parse_arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _minimal_arguments(name: str, raw: object) -> str:
    """Keep only the stable locator fields needed to repeat a compacted call."""
    parsed = _parse_arguments(raw)
    if name == "wikipedia":
        compact = {key: parsed[key] for key in ("query", "lang") if key in parsed}
    elif name == "fetch_url":
        compact = {key: parsed[key] for key in ("url",) if key in parsed}
    elif name == "text_editor":
        compact = {key: parsed[key] for key in ("command", "path") if key in parsed}
    else:  # Defensive fallback; callers only invoke this for TARGET_TOOLS.
        compact = parsed
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _archive_relative_path(round_index: int, call_id: str) -> str:
    """生成归档文件的相对路径。

    修复：原 digest 包含 round_index（history 数组下标），一旦之前的
    消息被裁剪或压缩（例如 select_request_context 截尾之后），同一
    次 tool_call 的 round_index 就变了，重新归档会生成不同的 digest，
    让旧的 archive 文件成为孤儿（无法被引用、永远占据 workspace）。
    改用 call_id 一个稳定标识：call_id 由 LLM 在生成 tool_call 时给出，
    在整个会话内不变。
    """
    safe_call_id = re.sub(r'[^A-Za-z0-9_-]', '_', str(call_id or ""))[:32] or "anon"
    digest = hashlib.sha256(f"{call_id}".encode("utf-8")).hexdigest()[:16]
    return f"{ARCHIVE_DIR}/call-{safe_call_id}-{digest}.json"


def _pointer_text(name: str, relative_path: str) -> str:
    return (
        f"{_POINTER_PREFIX}{relative_path}. "
        f"Use text_editor view with path {json.dumps(relative_path, ensure_ascii=False)} "
        f"to retrieve the original {name} call and result if needed."
    )


def _archive_payload(
    *,
    tool_call: dict[str, Any],
    tool_result: dict[str, Any],
    name: str,
    relative_path: str,
) -> bytes:
    payload = {
        "schema_version": 1,
        "tool_name": name,
        "tool_call": {
            "id": tool_call.get("id", ""),
            "type": tool_call.get("type", "function"),
            "function": dict(tool_call.get("function") or {}),
        },
        "tool_result": {
            "tool_call_id": tool_result.get("tool_call_id", ""),
            "content": tool_result.get("content", ""),
        },
        "retrieval": {
            "path": relative_path,
            "instruction": "Use text_editor with command=view and this relative path.",
        },
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")


def _eligible_calls(history: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    """Return unarchived target tool-call/result pairs in chronological order."""
    results_by_id: dict[str, dict[str, Any]] = {}
    for message in history:
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        if isinstance(call_id, str) and call_id and not _is_archived_pointer(message.get("content")):
            results_by_id[call_id] = message

    calls: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, message in enumerate(history):
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            name = _tool_name(tool_call)
            result = results_by_id.get(_tool_call_id(tool_call))
            if name in TARGET_TOOLS and result is not None:
                calls.append((index, tool_call, result))
    return calls


async def compact_older_tool_calls(
    chat_id: int,
    history: list[dict[str, Any]],
    *,
    calls_to_compact: int | None = None,
) -> ToolCompactionStats:
    """Archive a selected prefix of eligible tool-call rounds.

    With the default, the function archives the older half of still-unarchived
    target tool calls.  Callers may provide ``calls_to_compact`` for a later
    compaction pass; this makes it possible to compact half of the remaining
    rounds without revisiting already archived payloads.  Full payloads are
    written before the in-memory history is changed, so an archive write failure
    leaves the conversation untouched for that call.
    """
    calls = _eligible_calls(history)
    if calls_to_compact is None:
        compact_call_count = len(calls) // 2
    else:
        compact_call_count = min(len(calls), max(0, int(calls_to_compact)))
    if compact_call_count == 0:
        return ToolCompactionStats(eligible_calls=len(calls))

    await _ensure_runtime_workspace(chat_id)
    workspace_lock = await _get_workspace_lock(chat_id)
    compacted_calls = 0
    archived_bytes = 0

    async with workspace_lock:
        workspace = workspace_workdir(chat_id)
        for round_index, tool_call, tool_result in calls[:compact_call_count]:
                name = _tool_name(tool_call)
                call_id = _tool_call_id(tool_call)
                relative_path = _archive_relative_path(round_index, call_id)
                archive_path = (workspace / relative_path).resolve()
                if workspace not in archive_path.parents:
                    raise ValueError("tool archive path escapes workspace")
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                payload = _archive_payload(
                    tool_call=tool_call,
                    tool_result=tool_result,
                    name=name,
                    relative_path=relative_path,
                )
                # 体积上限：fetch_url 等工具可能返回 10MB+ 的 payload，
                # 每次压缩都生成一份归档会让 workspace 持续膨胀。
                # 超过 1MB 的 payload 跳过归档，仅保留 in-memory 历史。
                _MAX_ARCHIVE_PAYLOAD = 1 * 1024 * 1024
                if len(payload) > _MAX_ARCHIVE_PAYLOAD:
                    logger.info(
                        "skip archiving tool payload (size=%s, max=%s) for call_id=%s",
                        len(payload), _MAX_ARCHIVE_PAYLOAD, str(call_id)[:32],
                    )
                    # 不写盘，也不替换 content 为 pointer —— 保留原 result，
                    # 让 select_request_context 的截尾处理自然把它推出去。
                    continue
                await asyncio.to_thread(archive_path.write_bytes, payload)

                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                function["arguments"] = _minimal_arguments(name, function.get("arguments"))
                tool_result["content"] = _pointer_text(name, relative_path)
                compacted_calls += 1
                archived_bytes += len(payload)

    stats = ToolCompactionStats(
        eligible_calls=len(calls),
        compacted_calls_count=compacted_calls,
        compacted_calls=compacted_calls,
        archived_bytes=archived_bytes,
    )
    logger.info(
        "Tool context compacted: chat=%s eligible_calls=%s compacted_calls_count=%s compacted_calls=%s archived_bytes=%s",
        chat_id,
        stats.eligible_calls,
        stats.compacted_calls_count,
        stats.compacted_calls,
        stats.archived_bytes,
    )
    return stats
