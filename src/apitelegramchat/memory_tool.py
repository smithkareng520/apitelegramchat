"""Persistent, file-based memory tool.

This module implements the same client-side memory protocol used by Claude's
memory tool while preserving this project's per-chat isolation and R2-backed
persistence.  The model sees a virtual ``/memories`` tree.  The application
maps that tree into a private state directory and performs every operation only
after canonical-path validation.

Supported commands are ``view``, ``create``, ``str_replace``, ``insert``,
``delete`` and ``rename``.  Files are retrieved just in time rather than being
preloaded into the conversation context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from apitelegramchat.s3_utils import delete_r2_object, list_r2_objects, upload_bytes_to_r2
from apitelegramchat.workspace_paths import chat_state_root, workspace_namespace
from apitelegramchat.workspace_utils import _get_workspace_lock, _sync_generic_tree_from_r2

logger = logging.getLogger(__name__)

MEMORY_ROOT = "/memories"
MEMORY_DIR_NAME = "memories"
MAX_VIEW_CHARS = 16_000
MAX_FILE_CHARS = 1_000_000
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_DIRECTORY_DEPTH = 2
MAX_FILE_LINES = 999_999
MAX_PATH_LENGTH = 240
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


class MemoryToolError(Exception):
    """An expected, model-actionable memory operation error."""

    def __init__(self, message: str, code: str = "memory_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _memory_root(chat_id: int, namespace: object | None = None) -> Path:
    """Return the private, non-symlinked backing directory for ``/memories``."""
    parent = chat_state_root(chat_id, namespace)
    root = parent / MEMORY_DIR_NAME
    if root.exists() and root.is_symlink():
        raise MemoryToolError("Memory storage is unavailable: symlinked memory directory was rejected.", "unsafe_storage")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    resolved = root.resolve()
    if not resolved.is_dir() or resolved.is_symlink():
        raise MemoryToolError("Memory storage directory is invalid.", "unsafe_storage")
    return resolved


def _remote_prefix(chat_id: int, namespace: object | None = None) -> str:
    return f"state/{workspace_namespace(chat_id, namespace)}/{MEMORY_DIR_NAME}/"


def _human_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "K", "M", "G"):
        if value < 1024.0 or unit == "G":
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{int(value)}B"


def _logical_path(root: Path, real_path: Path) -> str:
    relative = real_path.resolve().relative_to(root.resolve())
    return MEMORY_ROOT if str(relative) == "." else f"{MEMORY_ROOT}/{relative.as_posix()}"


def _decoded_path(value: Any) -> str:
    if not isinstance(value, str):
        raise MemoryToolError("A memory path must be a string.", "bad_path")
    candidate = value.strip()
    # Decode a small, bounded number of times so encoded traversal cannot sneak
    # through a prefix-only check, without accepting unbounded work from input.
    for _ in range(3):
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    if not candidate:
        raise MemoryToolError("A memory path is required.", "bad_path")
    # Treat the conventional root spelling `/memories/` as `/memories` while
    # retaining rejection of empty components in the middle of a path.
    if candidate != "/":
        candidate = candidate.rstrip("/") or "/"
    if "\x00" in candidate or "\\" in candidate:
        raise MemoryToolError("Invalid memory path.", "bad_path")
    if len(candidate) > MAX_PATH_LENGTH:
        raise MemoryToolError(f"Memory path exceeds the {MAX_PATH_LENGTH}-character limit.", "bad_path")
    return candidate


def _resolve_path(root: Path, logical_path: Any, *, allow_root: bool = True) -> Path:
    """Safely map a logical ``/memories`` path to an on-disk path.

    Traversal is rejected before and after resolution.  The second check also
    protects against a malicious symlink that may exist inside the memory tree.
    """
    candidate = _decoded_path(logical_path)
    if candidate == MEMORY_ROOT:
        relative = Path()
    else:
        prefix = MEMORY_ROOT + "/"
        if not candidate.startswith(prefix):
            raise MemoryToolError("Memory paths must stay inside /memories.", "path_outside_memory")
        relative_text = candidate[len(prefix):]
        if not relative_text:
            relative = Path()
        else:
            raw_parts = relative_text.split("/")
            if any(part in {"", ".", ".."} for part in raw_parts):
                raise MemoryToolError("Invalid memory path traversal sequence.", "path_traversal")
            if any(part.startswith(".") for part in raw_parts):
                raise MemoryToolError("Hidden memory paths are not allowed.", "bad_path")
            relative = Path(*raw_parts)
    if not allow_root and not relative.parts:
        raise MemoryToolError("The /memories root cannot be modified or deleted.", "root_protected")
    target = root / relative
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise MemoryToolError("Memory path resolves outside /memories.", "path_traversal") from exc
    return resolved


def _validate_text(value: Any, field_name: str, *, allow_empty: bool = True) -> str:
    if value is None:
        return "" if allow_empty else _raise_missing(field_name)
    if not isinstance(value, str):
        raise MemoryToolError(f"{field_name} must be text.", "bad_input")
    if len(value) > MAX_FILE_CHARS:
        raise MemoryToolError(f"{field_name} exceeds the {MAX_FILE_CHARS}-character limit.", "file_too_large")
    if not value and not allow_empty:
        raise MemoryToolError(f"{field_name} cannot be empty.", "bad_input")
    return value


def _raise_missing(field_name: str) -> str:
    raise MemoryToolError(f"{field_name} is required.", "missing_input")


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        try:
            if path.is_symlink():
                continue
            if path.is_file():
                files.append(path)
        except OSError:
            continue
    return files


def _total_size(root: Path, *, excluding: Path | None = None) -> int:
    total = 0
    excluded = excluding.resolve() if excluding else None
    for path in _walk_files(root):
        try:
            if excluded is not None and path.resolve() == excluded:
                continue
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _assert_storage_budget(root: Path, additional_bytes: int, *, replacing: Path | None = None) -> None:
    used = _total_size(root, excluding=replacing)
    if used + additional_bytes > MAX_TOTAL_BYTES:
        raise MemoryToolError(
            f"Memory storage limit exceeded: {used + additional_bytes} bytes requested, maximum is {MAX_TOTAL_BYTES} bytes.",
            "storage_limit",
        )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _numbered_content(logical_path: str, text: str, view_range: Any = None) -> str:
    lines = text.splitlines()
    # Preserve a final empty line when it is meaningful to the model's exact
    # replacement/insertion logic.
    if text.endswith("\n"):
        lines.append("")
    if len(lines) > MAX_FILE_LINES:
        raise MemoryToolError(
            f"File {logical_path} exceeds maximum line limit of {MAX_FILE_LINES:,} lines.",
            "too_many_lines",
        )
    start, end = 1, len(lines)
    if view_range is not None:
        if not isinstance(view_range, (list, tuple)) or len(view_range) != 2:
            raise MemoryToolError("view_range must be [start_line, end_line].", "bad_view_range")
        try:
            start, end = int(view_range[0]), int(view_range[1])
        except (TypeError, ValueError) as exc:
            raise MemoryToolError("view_range values must be integers.", "bad_view_range") from exc
        if start < 1 or (end != -1 and end < start):
            raise MemoryToolError("Invalid view_range. Use [start_line, end_line] or [start_line, -1].", "bad_view_range")
        if start > len(lines) and lines:
            raise MemoryToolError(f"view_range starts after the end of {logical_path}.", "bad_view_range")
        if end == -1:
            end = len(lines)
        else:
            end = min(end, len(lines))
    selected = lines[start - 1:end] if lines else []
    body = "\n".join(f"{index:>6}\t{line}" for index, line in enumerate(selected, start=start))
    result = f"Here's the content of {logical_path} with line numbers:\n{body}"
    if len(result) > MAX_VIEW_CHARS:
        available = max(0, MAX_VIEW_CHARS - len(f"Here's the content of {logical_path} with line numbers:\n") - 72)
        result = result[:available].rsplit("\n", 1)[0]
        result += "\n…[text view truncated at 16,000 characters; use view_range to continue]"
    return result


def _directory_listing(root: Path, target: Path) -> str:
    logical_target = _logical_path(root, target)
    entries: list[tuple[str, Path]] = []
    try:
        for path in target.rglob("*"):
            relative = path.relative_to(target)
            if len(relative.parts) > MAX_DIRECTORY_DEPTH:
                continue
            if any(part.startswith(".") or part == "node_modules" for part in relative.parts):
                continue
            if path.is_symlink():
                continue
            entries.append((relative.as_posix(), path))
    except OSError as exc:
        raise MemoryToolError(f"Unable to list {logical_target}: {exc}", "view_failed") from exc

    lines = [
        f"Here're the files and directories up to {MAX_DIRECTORY_DEPTH} levels deep in {logical_target}, excluding hidden items and node_modules:"
    ]
    root_size = _total_size(target)
    lines.append(f"{_human_size(root_size)}\t{logical_target}")
    for _, path in sorted(entries, key=lambda pair: (pair[0].lower(), pair[0])):
        try:
            size = _total_size(path) if path.is_dir() else path.stat().st_size
            lines.append(f"{_human_size(size)}\t{_logical_path(root, path)}")
        except OSError:
            continue
    return "\n".join(lines)


def _view(root: Path, path: Any, view_range: Any = None) -> tuple[str, dict[str, Any]]:
    target = _resolve_path(root, path)
    logical = _logical_path(root, target)
    if not target.exists():
        raise MemoryToolError(f"The path {logical} does not exist. Please provide a valid path.", "not_found")
    if target.is_dir():
        return _directory_listing(root, target), {"path": logical, "kind": "directory"}
    if not target.is_file() or target.is_symlink():
        raise MemoryToolError(f"The path {logical} does not exist. Please provide a valid path.", "not_found")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise MemoryToolError(f"Unable to read {logical}: {exc}", "view_failed") from exc
    if target.suffix.lower() in IMAGE_SUFFIXES:
        return f"Image memory file at {logical} ({_human_size(size)}).", {"path": logical, "kind": "image", "size": size}
    if size > MAX_FILE_CHARS * 4:
        raise MemoryToolError(f"File {logical} is too large to view safely.", "file_too_large")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MemoryToolError(f"File {logical} is not a UTF-8 text file.", "non_text_file") from exc
    except OSError as exc:
        raise MemoryToolError(f"Unable to read {logical}: {exc}", "view_failed") from exc
    return _numbered_content(logical, text, view_range), {"path": logical, "kind": "file", "size": size}


def _create(root: Path, path: Any, file_text: Any) -> tuple[str, dict[str, Any]]:
    target = _resolve_path(root, path, allow_root=False)
    logical = _logical_path(root, target)
    if target.exists():
        raise MemoryToolError(f"Error: File {logical} already exists", "already_exists")
    text = _validate_text(file_text, "file_text")
    encoded = text.encode("utf-8")
    _assert_storage_budget(root, len(encoded))
    _atomic_write(target, text)
    return f"File created successfully at: {logical}", {"path": logical, "bytes": len(encoded)}


def _str_replace(root: Path, path: Any, old_str: Any, new_str: Any) -> tuple[str, dict[str, Any]]:
    target = _resolve_path(root, path, allow_root=False)
    logical = _logical_path(root, target)
    if not target.is_file() or target.is_symlink():
        raise MemoryToolError(f"Error: The path {logical} does not exist. Please provide a valid path.", "not_found")
    old = _validate_text(old_str, "old_str", allow_empty=False)
    new = _validate_text(new_str, "new_str")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MemoryToolError(f"Error: The path {logical} is not an editable UTF-8 text file.", "non_text_file") from exc
    count = text.count(old)
    if count == 0:
        raise MemoryToolError(f"No replacement was performed, old_str `{old}` did not appear verbatim in {logical}.", "no_match")
    if count > 1:
        line_numbers: list[str] = []
        start = 0
        while True:
            start = text.find(old, start)
            if start < 0:
                break
            line_numbers.append(str(text.count("\n", 0, start) + 1))
            start += len(old)
        raise MemoryToolError(
            f"No replacement was performed. Multiple occurrences of old_str `{old}` in lines: {', '.join(line_numbers)}. Please ensure it is unique",
            "multiple_matches",
        )
    updated = text.replace(old, new, 1)
    _assert_storage_budget(root, len(updated.encode("utf-8")), replacing=target)
    _atomic_write(target, updated)
    line = text.count("\n", 0, text.find(old)) + 1
    preview_range = [max(1, line - 2), line + max(3, new.count("\n") + 2)]
    preview = _numbered_content(logical, updated, preview_range)
    return f"The memory file has been edited.\n{preview}", {"path": logical, "changed_lines_from": line}


def _insert(root: Path, path: Any, insert_line: Any, insert_text: Any) -> tuple[str, dict[str, Any]]:
    target = _resolve_path(root, path, allow_root=False)
    logical = _logical_path(root, target)
    if not target.is_file() or target.is_symlink():
        raise MemoryToolError(f"Error: The path {logical} does not exist", "not_found")
    try:
        position = int(insert_line)
    except (TypeError, ValueError) as exc:
        raise MemoryToolError("Error: Invalid `insert_line` parameter.", "bad_insert_line") from exc
    insertion = _validate_text(insert_text, "insert_text", allow_empty=False)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MemoryToolError(f"Error: The path {logical} is not an editable UTF-8 text file.", "non_text_file") from exc
    lines = text.splitlines(keepends=True)
    n_lines = len(lines)
    if not 0 <= position <= n_lines:
        raise MemoryToolError(
            f"Error: Invalid `insert_line` parameter: {position}. It should be within the range of lines of the file: [0, {n_lines}]",
            "bad_insert_line",
        )
    updated = "".join(lines[:position]) + insertion + "".join(lines[position:])
    _assert_storage_budget(root, len(updated.encode("utf-8")), replacing=target)
    _atomic_write(target, updated)
    return f"The file {logical} has been edited.", {"path": logical, "inserted_after_line": position}


def _delete(root: Path, path: Any) -> tuple[str, dict[str, Any]]:
    target = _resolve_path(root, path, allow_root=False)
    logical = _logical_path(root, target)
    if not target.exists() and not target.is_symlink():
        raise MemoryToolError(f"Error: The path {logical} does not exist", "not_found")
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        parent = target.parent
        while parent != root and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    except OSError as exc:
        raise MemoryToolError(f"Unable to delete {logical}: {exc}", "delete_failed") from exc
    return f"Successfully deleted {logical}", {"path": logical}


def _rename(root: Path, old_path: Any, new_path: Any) -> tuple[str, dict[str, Any]]:
    source = _resolve_path(root, old_path, allow_root=False)
    destination = _resolve_path(root, new_path, allow_root=False)
    old_logical = _logical_path(root, source)
    new_logical = _logical_path(root, destination)
    if not source.exists() and not source.is_symlink():
        raise MemoryToolError(f"Error: The path {old_logical} does not exist", "not_found")
    if destination.exists() or destination.is_symlink():
        raise MemoryToolError(f"Error: The destination {new_logical} already exists", "destination_exists")
    # A directory cannot be moved into itself (``a`` -> ``a/b``).
    try:
        destination.relative_to(source.resolve())
        raise MemoryToolError("Error: A memory directory cannot be moved into itself.", "invalid_rename")
    except ValueError:
        pass
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(source, destination)
    except OSError as exc:
        raise MemoryToolError(f"Unable to rename {old_logical}: {exc}", "rename_failed") from exc
    return f"Successfully renamed {old_logical} to {new_logical}", {"old_path": old_logical, "new_path": new_logical}


async def _sync_from_remote(chat_id: int, root: Path, namespace: object | None = None) -> None:
    try:
        prefix = _remote_prefix(chat_id, namespace)
        # Never let an unavailable/empty remote listing erase a local write.  A
        # non-empty listing is safe to mirror; an empty one is ambiguous during
        # first use, R2 outages, and immediately after a failed upload.
        remote_keys = await list_r2_objects(prefix)
        if remote_keys:
            await _sync_generic_tree_from_r2(chat_id, root, prefix, namespace=namespace)
    except Exception as exc:
        logger.warning("memory: R2→local directory sync failed (chat=%s): %s", chat_id, exc)


async def _sync_to_remote(chat_id: int, root: Path, namespace: object | None = None) -> None:
    prefix = _remote_prefix(chat_id, namespace)
    try:
        remote_keys = set(await list_r2_objects(prefix))
        current_keys: set[str] = set()
        for local_path in _walk_files(root):
            try:
                resolved = local_path.resolve()
                relative = resolved.relative_to(root.resolve()).as_posix()
                if not relative or relative.startswith(".") or "/." in relative:
                    continue
                key = prefix + relative
                current_keys.add(key)
                data = await asyncio.to_thread(resolved.read_bytes)
                await upload_bytes_to_r2(data, key, "text/plain; charset=utf-8")
            except (OSError, ValueError) as exc:
                logger.warning("memory: skipped unsafe local file during sync: %s", exc)
        for key in remote_keys - current_keys:
            await delete_r2_object(key)
    except Exception as exc:
        logger.warning("memory: local→R2 directory sync failed (chat=%s): %s", chat_id, exc)


def _payload(ok: bool, command: str, result: str, *, code: str | None = None, meta: dict[str, Any] | None = None) -> str:
    data: dict[str, Any] = {"ok": ok, "action": command, "command": command}
    if ok:
        data["result"] = result
    else:
        data["error"] = result
        data["code"] = code or "memory_error"
    if meta:
        data.update(meta)
    return json.dumps(data, ensure_ascii=False)


async def execute_memory(
    chat_id: int,
    command: str = "view",
    path: str = MEMORY_ROOT,
    view_range: Any = None,
    old_str: Optional[str] = None,
    new_str: Optional[str] = None,
    insert_line: Optional[int] = None,
    insert_text: Optional[str] = None,
    file_text: Optional[str] = None,
    old_path: Optional[str] = None,
    new_path: Optional[str] = None,
    namespace: object | None = None,
    **legacy: Any,
) -> str:
    """Execute one memory protocol command and return a model-readable payload.

    ``legacy`` is intentionally accepted so older callers fail gracefully with a
    migration hint instead of producing a Python ``TypeError``.
    """
    if legacy.get("action") is not None:
        return _payload(
            False,
            "legacy",
            "The structured memory API has been replaced by the file-based protocol. Use command=view/create/str_replace/insert/delete/rename under /memories.",
            code="legacy_api_removed",
        )
    command = (command or "view").strip().lower()
    if command not in {"view", "create", "str_replace", "insert", "delete", "rename"}:
        return _payload(False, command or "unknown", f"Unknown memory command: {command}", code="bad_command")

    try:
        lock = await _get_workspace_lock(chat_id)
        async with lock:
            root = _memory_root(chat_id, namespace)
            await _sync_from_remote(chat_id, root, namespace)
            if command == "view":
                result, meta = _view(root, path, view_range)
            elif command == "create":
                result, meta = _create(root, path, file_text)
                await _sync_to_remote(chat_id, root, namespace)
            elif command == "str_replace":
                result, meta = _str_replace(root, path, old_str, new_str)
                await _sync_to_remote(chat_id, root, namespace)
            elif command == "insert":
                result, meta = _insert(root, path, insert_line, insert_text)
                await _sync_to_remote(chat_id, root, namespace)
            elif command == "delete":
                result, meta = _delete(root, path)
                await _sync_to_remote(chat_id, root, namespace)
            else:
                result, meta = _rename(root, old_path, new_path)
                await _sync_to_remote(chat_id, root, namespace)
            return _payload(True, command, result, meta=meta)
    except MemoryToolError as exc:
        return _payload(False, command, exc.message, code=exc.code)
    except Exception as exc:  # Do not leak filesystem paths or tracebacks to the model/UI.
        logger.exception("memory operation failed (chat=%s command=%s)", chat_id, command)
        return _payload(False, command, "Memory operation could not be completed safely.", code="memory_error")


def _escape(value: Any) -> str:
    return str(value if value is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _preview_lines(text: str, max_lines: int = 12, max_chars: int = 1800) -> str:
    lines = text.splitlines()
    visible = lines[:max_lines]
    preview = "\n".join(visible)
    if len(lines) > max_lines:
        preview += "\n…"
    if len(preview) > max_chars:
        preview = preview[:max_chars] + "…"
    return _escape(preview)


def render_memory_card(payload: dict[str, Any], max_items: int = 30) -> str:
    """Render clear Telegram HTML without exposing raw JSON to end users."""
    if not isinstance(payload, dict):
        return f"<p><b>记忆工具</b></p><blockquote>{_escape(payload)}</blockquote>"
    if not payload.get("ok"):
        code = _escape(payload.get("code", "memory_error"))
        message = _escape(payload.get("error", "未知错误"))
        return f"<p><b>记忆操作未完成</b> · <code>{code}</code></p><blockquote>{message}</blockquote>"

    command = str(payload.get("command") or payload.get("action") or "view")
    result = str(payload.get("result", ""))
    path = _escape(payload.get("path", ""))
    if command == "view":
        kind = payload.get("kind")
        if kind == "directory":
            title = "记忆目录"
        elif kind == "image":
            title = "记忆图像"
        else:
            title = "记忆文件"
        return f"<p><b>{title}</b>{(' · <code>' + path + '</code>') if path else ''}</p><pre>{_preview_lines(result)}</pre>"
    if command == "create":
        return f"<p><b>已创建记忆文件</b> · <code>{path}</code></p><blockquote>{_escape(result)}</blockquote>"
    if command == "str_replace":
        return f"<p><b>已编辑记忆文件</b> · <code>{path}</code></p><pre>{_preview_lines(result)}</pre>"
    if command == "insert":
        return f"<p><b>已插入记忆内容</b> · <code>{path}</code></p><blockquote>{_escape(result)}</blockquote>"
    if command == "delete":
        return f"<p><b>已删除记忆项目</b> · <code>{path}</code></p><blockquote>{_escape(result)}</blockquote>"
    if command == "rename":
        return (
            "<p><b>已重命名记忆项目</b></p>"
            f"<p><code>{_escape(payload.get('old_path', ''))}</code> → <code>{_escape(payload.get('new_path', ''))}</code></p>"
        )
    return f"<p><b>记忆操作已完成</b></p><pre>{_preview_lines(result)}</pre>"


MEMORY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Persistent per-chat file memory. ALWAYS begin a task by calling view on /memories, then read only relevant files. "
            "Use create, str_replace, insert, delete, or rename to maintain concise, organized long-term memory. "
            "All paths must be inside /memories. Do not store secrets, credentials, or unnecessary conversation transcripts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "_description": {"type": "string", "description": "简述本次记忆操作目的（≤60字）。"},
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace", "insert", "delete", "rename"],
                    "description": "记忆文件操作命令。"
                },
                "path": {"type": "string", "description": "目标路径，必须为 /memories 或其内部路径。"},
                "view_range": {
                    "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
                    "description": "仅 view：行范围 [起始行, 结束行]；结束行 -1 表示到文件末尾。"
                },
                "file_text": {"type": "string", "description": "仅 create：新文件的完整 UTF-8 文本。"},
                "old_str": {"type": "string", "description": "仅 str_replace：必须在文件中唯一且完全匹配的旧文本。"},
                "new_str": {"type": "string", "description": "仅 str_replace：替换文本；省略时删除 old_str。"},
                "insert_line": {"type": "integer", "description": "仅 insert：在该 1-based 行之后插入；0 表示文件开头。"},
                "insert_text": {"type": "string", "description": "仅 insert：要插入的文本。"},
                "old_path": {"type": "string", "description": "仅 rename：原路径，必须在 /memories 内。"},
                "new_path": {"type": "string", "description": "仅 rename：目标路径，必须在 /memories 内且不存在。"}
            },
            "required": ["command"]
        },
        "input_examples": [
            {"command": "view", "path": "/memories"},
            {"command": "create", "path": "/memories/project-status.md", "file_text": "# Project status\n\n- Pending: inspect requirements\n"},
            {"command": "str_replace", "path": "/memories/project-status.md", "old_str": "Pending: inspect requirements", "new_str": "Done: requirements inspected"},
            {"command": "insert", "path": "/memories/project-status.md", "insert_line": 3, "insert_text": "- Next: implement changes\n"},
            {"command": "rename", "old_path": "/memories/draft.md", "new_path": "/memories/final.md"}
        ]
    }
}
