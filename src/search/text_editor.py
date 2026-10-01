"""text_editor 工具：view/str_replace/create/insert/list 与 R2 持久化（自 search_engine.py 拆出）。

行尾保真：所有读写按原始字节进行（CRLF 不被 universal newlines
静默翻译成 LF）；纯 CRLF 文件在匹配/写入时整体按 CRLF 空间处理。
"""

import os
import asyncio
import tempfile
import mimetypes
from pathlib import Path
from typing import cast

from workspace_paths import workspace_workdir, workspace_namespace
from s3_utils import upload_bytes_to_r2, delete_r2_object
from workspace_utils import _get_workspace_lock, _ensure_runtime_workspace
from token_budget import count_tokens

import logging

logger = logging.getLogger(__name__)


# ===================== 显式持久化单个编辑文件 =====================
# 后台持久化任务的强引用集合：asyncio.create_task 返回的 Task 若不保存
# 引用，事件循环只持弱引用，任务可能在执行中途被垃圾回收（Python 官方
# 文档明确要求保存引用）。任务完成后经 done 回调从集合移除，避免泄漏。
_editor_persist_tasks: set = set()


async def _persist_edited_file(
    chat_id: int,
    rel_path: str,
    *,
    delete: bool = False,
    namespace: str | None = None,
    content_bytes: bytes | None = None,
) -> None:
    """Persist only the file explicitly changed through text_editor.

    ``content_bytes``：调用方传入本次编辑**实际写入**的字节。后台任务
    若重新从磁盘读取，可能读到后续并发编辑的新内容（或读到写入前的
    旧内容，取决于时序），让 R2 镜像与本次结果不一致；直传字节同时
    消除这个竞态和一次额外 IO。
    """
    try:
        result = await persist_workspace_file(
            chat_id, rel_path, delete=delete, namespace=namespace,
            content_bytes=content_bytes,
        )
        logger.debug("显式持久化成功：%s", result.get("key", rel_path))
    except Exception as e:
        logger.error("显式持久化失败 %s: %s", rel_path, e)


def _spawn_persist_task(
    chat_id: int,
    safe_path: str,
    *,
    namespace: str,
    content_bytes: bytes | None = None,
) -> None:
    """调度后台持久化任务，并保存 Task 引用防止被 GC 中途回收。"""
    try:
        task = asyncio.create_task(
            _persist_edited_file(
                chat_id, safe_path, namespace=namespace,
                content_bytes=content_bytes,
            )
        )
    except RuntimeError:
        # 没有正在运行的事件循环（同步测试上下文等）：跳过后台持久化。
        logger.debug("无运行事件循环，跳过后台持久化 %s", safe_path)
        return
    _editor_persist_tasks.add(task)
    task.add_done_callback(_editor_persist_tasks.discard)


def _normalize_editor_text(text: str) -> str:
    if text is None:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_editor_content(path: Path) -> str:
    """Read a UTF-8 text file preserving its exact line-ending bytes.

    旧实现用 ``Path.read_text``（universal newlines）：读取时把 CRLF/CR
    全部翻译成 LF，任何一次 str_replace/insert 都会把 CRLF 文件整体
    静默改写成 LF 风格（编辑一行 = 重写全文件行尾）。这里按原始字节
    读回并 decode，行尾原样保留，由各命令自行决定是否（以及在哪个
    空间里）做行尾归一化。
    """
    return path.read_bytes().decode("utf-8")


def _is_pure_crlf(content: str) -> bool:
    """True 当文件行尾全部是 CRLF（每个 ``\n`` 都属于某个 ``\r\n``）。"""
    return content.count("\r\n") > 0 and content.count("\n") == content.count("\r\n")


def _is_plain_int(value: object) -> bool:
    """int 且不是 bool（bool 是 int 的子类，True 会被当成 1 放行）。"""
    return isinstance(value, int) and not isinstance(value, bool)


# 文本编辑器输出与读入体积上限（可用环境变量覆盖）。
# - 单行截断：超长行（minified JS / base64 单行）截到 2000 字符，
#   否则一行就能吃光整个 view 预算；
# - view 预算：与全局 TOOL_RESPONSE_TOKEN_BUDGET 同口径（20k token），
#   在工具内部按【完整行】截断并给出 view_range 续读指引，比外层
#   一刀切截断（截半个词/半行）对模型自纠友好得多；
# - 体积上限：防止把超大文件整个读进内存（会 OOM 整个进程，影响所有
#   用户的会话），超限时返回可操作错误并建议 bash head/tail/grep。
_EDITOR_MAX_LINE_CHARS = int(os.getenv("TEXT_EDITOR_MAX_LINE_CHARS", "2000"))
_EDITOR_VIEW_TOKEN_BUDGET = int(os.getenv("TEXT_EDITOR_VIEW_TOKEN_BUDGET", "20000"))
_EDITOR_MAX_VIEW_BYTES = int(os.getenv("TEXT_EDITOR_MAX_VIEW_BYTES", str(16 * 1024 * 1024)))
_EDITOR_MAX_EDIT_BYTES = int(os.getenv("TEXT_EDITOR_MAX_EDIT_BYTES", str(64 * 1024 * 1024)))


def _format_editor_line(line_no: int, text: str, width: int) -> str:
    """Format a text-editor view line with an absolute 1-based line number."""
    text = text.rstrip("\r\n")
    if len(text) > _EDITOR_MAX_LINE_CHARS:
        text = text[:_EDITOR_MAX_LINE_CHARS] + "…[line truncated]"
    return f"{line_no:>{width}}: {text}"


def _render_view_output(
    lines: list[str], start: int, end: int, total_lines: int, width: int,
) -> str:
    """Join numbered view lines, truncating at line boundaries within budget.

    小输出直接返回（避免每次 view 都跑 tokenizer）；超过预算时按完整行
    截断并附上总行数与 ``view_range`` 续读指引，让模型一轮就能精确
    继续读取，而不是被外层截断器截在半行后盲目重试。
    """
    rendered = "\n".join(
        _format_editor_line(line_number, lines[line_number - 1], width)
        for line_number in range(start, end + 1)
    )
    if len(rendered) <= _EDITOR_VIEW_TOKEN_BUDGET:
        # 字符数不超预算时 token 数也不可能超（1 token 至少 1 字符）。
        return rendered
    if count_tokens(rendered) <= _EDITOR_VIEW_TOKEN_BUDGET:
        return rendered

    kept: list[str] = []
    used = 0
    reserve = 64  # 预留给尾注行
    for line_number in range(start, end + 1):
        line = _format_editor_line(line_number, lines[line_number - 1], width)
        cost = count_tokens(line) + 1  # +1 for the joining newline
        if used + cost > _EDITOR_VIEW_TOKEN_BUDGET - reserve:
            break
        kept.append(line)
        used += cost
    shown_end = start + len(kept) - 1  # 最后一个已展示的绝对行号
    resume_from = shown_end + 1
    note = (
        f"…[file has {total_lines} lines; output truncated at line "
        f"{shown_end} to fit the token budget; continue reading with "
        f"view_range=[{resume_from}, -1]]"
    )
    if not kept:
        return note
    return "\n".join(kept) + "\n" + note


def _latest_editor_snapshot(content: str, max_lines: int = 10) -> str:
    """Return the tail of a file with absolute line numbers for the chat UI."""
    lines = _normalize_editor_text(content).splitlines()
    if not lines:
        return "(empty file)"
    start = max(1, len(lines) - max_lines + 1)
    width = len(str(len(lines)))
    return "\n".join(_format_editor_line(index, lines[index - 1], width) for index in range(start, len(lines) + 1))


def _with_latest_editor_snapshot(message: str, content: str) -> str:
    return f"{message}\n\nLatest file snapshot (tail 10):\n{_latest_editor_snapshot(content)}"


SNIPPET_LINES = 4


def _format_editor_snippet(
    content: str,
    target_line: int,
    snippet_lines: int = SNIPPET_LINES,
    total_new_lines: int = 0,
) -> str:
    """Generate a snippet around the modified section with 1-based line numbers.
    
    This matches official Claude text_editor behavior: providing immediate context
    around the edited section so the model can verify changes without needing an extra view.
    """
    lines = _normalize_editor_text(content).splitlines()
    if not lines:
        return "(empty file)"
    total_lines = len(lines)
    start_line = max(1, target_line - snippet_lines)
    end_line = min(total_lines, target_line + snippet_lines + max(0, total_new_lines))
    width = max(len(str(total_lines)), 4)
    snippet = "\n".join(
        _format_editor_line(idx, lines[idx - 1], width)
        for idx in range(start_line, end_line + 1)
    )
    return (
        f"Here's the result of running `cat -n` on a snippet of the file (lines {start_line}-{end_line}):\n"
        f"{snippet}\n"
        "Review the changes and make sure they are as expected. Edit the file again if necessary."
    )


def _with_editor_snippet_or_tail(message: str, content: str, target_line: int | None = None, new_lines: int = 0) -> str:
    if target_line is not None and target_line >= 1:
        snippet = _format_editor_snippet(content, target_line, total_new_lines=new_lines)
        return f"{message}\n\n{snippet}"
    return _with_latest_editor_snapshot(message, content)


def _list_directory_contents(dir_path: Path, display_name: str, max_depth: int = 2) -> str:
    """List directory contents up to max_depth levels deep, excluding hidden items."""
    entries = []
    root_level = len(dir_path.parts)
    try:
        for current_root, dirs, files in os.walk(dir_path):
            current_path = Path(current_root)
            depth = len(current_path.parts) - root_level
            # 过滤隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if depth >= max_depth:
                dirs.clear()

            for f in sorted(files):
                if f.startswith("."):
                    continue
                try:
                    rel_file = (current_path / f).relative_to(dir_path)
                    entries.append(str(rel_file))
                except ValueError:
                    entries.append(f)
            for d in sorted(dirs):
                try:
                    rel_dir = (current_path / d).relative_to(dir_path)
                    entries.append(str(rel_dir) + "/")
                except ValueError:
                    entries.append(d + "/")
    except Exception as exc:
        return f"Error listing directory: {exc}"

    entries.sort()
    header_name = display_name if display_name and display_name != "." else "the workspace root"
    if not entries:
        return f"Directory {header_name} is empty (excluding hidden items)."
    listing = "\n".join(entries)
    return (
        f"Here's the files and directories up to 2 levels deep in {header_name}, "
        f"excluding hidden items:\n{listing}\n"
    )


def _write_text_editor_file(local_path: Path, new_content: str) -> None:
    """Atomically replace an existing UTF-8 text file while preserving its mode.

    以字节写入：绕过文本模式的平台换行翻译（``newline=None`` 会把 ``\n``
    翻成 ``os.linesep``），确保落盘内容与 ``new_content`` 逐字节一致
    （CRLF 保持 CRLF，不会被二次改写）。
    """
    mode = local_path.stat().st_mode & 0o777
    data = new_content.encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{local_path.name}.", dir=local_path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(data)
        os.chmod(temp_path, mode)
        os.replace(temp_path, local_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _file_too_large_error(action: str, local_path: Path, limit_bytes: int) -> str:
    """体积上限错误：读入整个大文件会 OOM 全进程，必须提前拒绝。"""
    size_mb = local_path.stat().st_size / (1024 * 1024)
    limit_mb = limit_bytes / (1024 * 1024)
    return (
        f"Error: File too large to {action} ({size_mb:.1f} MB, limit {limit_mb:.0f} MB). "
        "Inspect or edit it with bash (head/tail/grep/sed) instead."
    )


def _permission_error(command: str) -> str:
    if command == "view":
        return "Error: Permission denied. Cannot read file."
    if command == "create":
        return "Error: Permission denied. Cannot create file."
    return "Error: Permission denied. Cannot write to file."


# ---------- 主函数 ----------
async def execute_text_editor(
    chat_id: int,
    command: str,
    path: str,
    namespace: str | None = None,
    view_range: list[int] | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    insert_line: int | None = None,
    insert_text: str | None = None,
    file_text: str | None = None,
) -> str:
    """Safely perform one of four text-file operations inside the workspace.

    ``str_replace`` is intentionally strict: ``old_str`` must occur exactly once
    in the entire file. This prevents accidental broad edits and tells the model
    to re-view the relevant text when its context is stale.
    """
    allowed_commands = {"view", "str_replace", "create", "insert"}
    if command not in allowed_commands:
        return f"Error: Unknown command: {command}. Allowed commands are view, str_replace, create, and insert."

    allow_root = (command == "view")
    try:
        safe_path = _editor_safe_path(path, allow_root=allow_root)
    except ValueError as exc:
        return f"Error: {exc}"

    resolved_namespace = workspace_namespace(chat_id, namespace)
    try:
        await _ensure_runtime_workspace(chat_id, resolved_namespace)
    except PermissionError:
        return _permission_error(command)
    except OSError as exc:
        return f"Error: Cannot access workspace: {exc.strerror or str(exc)}"
    except RuntimeError as exc:
        # _secure_directory 拒绝符号链接化的 runtime 目录时抛 RuntimeError，
        # 此前会直接冒泡成裸异常（不在任何 except 分支里）。
        return f"Error: Cannot access workspace: {exc}"

    lock = await _get_workspace_lock(chat_id, resolved_namespace)
    async with lock:
        try:
            workspace = workspace_workdir(chat_id, resolved_namespace).resolve()
            local_path = _resolve_editor_path(workspace, safe_path, allow_root=allow_root)

            if command == "view":
                if not local_path.exists():
                    return "Error: File not found"
                if local_path.is_dir():
                    if view_range is not None:
                        return "Error: The `view_range` parameter is not allowed when `path` points to a directory."
                    return _list_directory_contents(local_path, safe_path)
                if local_path.stat().st_size > _EDITOR_MAX_VIEW_BYTES:
                    return _file_too_large_error("view", local_path, _EDITOR_MAX_VIEW_BYTES)
                try:
                    content = _read_editor_content(local_path)
                except UnicodeDecodeError:
                    return "Error: File is not valid UTF-8 text."
                lines = content.splitlines()
                total_lines = len(lines)
                if view_range is not None:
                    if (
                        not isinstance(view_range, list)
                        or len(view_range) != 2
                        or not all(_is_plain_int(value) for value in view_range)
                    ):
                        return "Error: view_range must be [start_line, end_line] with integer values."
                    start, end = view_range
                    if start < 1:
                        return "Error: view_range start_line must be at least 1."
                    if end != -1 and end < start:
                        return "Error: view_range end_line must be -1 or greater than or equal to start_line."
                    if start > total_lines:
                        return f"Error: start_line {start} exceeds total lines {total_lines}"
                    if end == -1 or end > total_lines:
                        end = total_lines
                else:
                    start, end = 1, total_lines

                if total_lines == 0:
                    return "(empty file)"
                width = len(str(total_lines))
                return _render_view_output(lines, start, end, total_lines, width)

            if command == "create":
                if not isinstance(file_text, str):
                    return "Error: Missing file_text for create."
                if local_path.exists():
                    if local_path.is_dir():
                        return "Error: A directory already exists at this path."
                    return "Error: File already exists."
                local_path.parent.mkdir(parents=True, exist_ok=True)
                data = file_text.encode("utf-8")
                try:
                    # "xb"：排他创建 + 字节写入（无平台换行翻译）。
                    with open(local_path, "xb") as file:
                        file.write(data)
                except FileExistsError:
                    return "Error: File already exists."
                _spawn_persist_task(
                    chat_id, safe_path, namespace=resolved_namespace, content_bytes=data)
                # 成功消息使用 workspace 相对路径：绝对路径会泄漏服务器
                # 目录结构，也违背「一切路径相对 workspace 根」的约定。
                return _with_latest_editor_snapshot(f"Successfully created file: {safe_path}", file_text)

            if not local_path.exists():
                return "Error: File not found"
            if local_path.is_dir():
                return "Error: Path is a directory. Text editing only supports files."
            if local_path.stat().st_size > _EDITOR_MAX_EDIT_BYTES:
                return _file_too_large_error("edit", local_path, _EDITOR_MAX_EDIT_BYTES)
            try:
                content = _read_editor_content(local_path)
            except UnicodeDecodeError:
                return "Error: File is not valid UTF-8 text."

            if command == "str_replace":
                if not isinstance(old_str, str) or not isinstance(new_str, str):
                    return "Error: Missing old_str or new_str for str_replace."
                if not old_str:
                    return "Error: old_str must be non-empty for str_replace."

                # 行尾策略：
                # - 纯 CRLF 文件：在 LF 空间匹配（old_str/new_str 一并归一），
                #   写回时统一还原成 CRLF —— 编辑后行尾风格保持不变；
                # - 其他文件：按原始字节精确匹配；
                #   匹配失败且文件含 CR 时，再按 LF 归一化重试一次（混合
                #   行尾文件），命中则写入归一化结果并在消息里明说，
                #   不做静默改写。
                crlf = _is_pure_crlf(content)
                work = content.replace("\r\n", "\n") if crlf else content
                old_work = old_str.replace("\r\n", "\n") if crlf else old_str
                new_work = new_str.replace("\r\n", "\n") if crlf else new_str
                match_count = work.count(old_work)
                normalized_note = ""
                if match_count == 0 and not crlf and "\r" in content and old_str:
                    lf_content = _normalize_editor_text(content)
                    lf_old = _normalize_editor_text(old_str)
                    lf_count = lf_content.count(lf_old)
                    if lf_count == 1:
                        work, old_work = lf_content, lf_old
                        new_work = _normalize_editor_text(new_str)
                        match_count = 1
                        normalized_note = " (file had mixed line endings; normalized to LF)"
                    elif lf_count > 1:
                        match_count = lf_count

                if match_count == 0:
                    return (
                        "Error: No match found for replacement. Recovery: call text_editor "
                        "view on this file, then retry once with an exact old_str copied from the latest view."
                    )
                if match_count > 1:
                    lines_matching = [idx + 1 for idx, line in enumerate(work.splitlines()) if old_work in line]
                    line_nums_str = str(lines_matching[:10]) + ("..." if len(lines_matching) > 10 else "")
                    return (
                        f"Error: Found {match_count} matches for replacement text in lines {line_nums_str}. "
                        "Recovery: call text_editor view, then retry once with a longer exact old_str "
                        "that includes surrounding context lines to make it unique."
                    )

                match_char_idx = work.find(old_work)
                replacement_line = work[:match_char_idx].count("\n") + 1 if match_char_idx != -1 else 1
                new_str_lines_count = new_work.count("\n")

                new_content = work.replace(old_work, new_work, 1)
                if crlf:
                    new_content = new_content.replace("\n", "\r\n")
                _write_text_editor_file(local_path, new_content)
                _spawn_persist_task(
                    chat_id, safe_path, namespace=resolved_namespace,
                    content_bytes=new_content.encode("utf-8"))
                success_msg = f"The file {safe_path} has been edited.{normalized_note}"
                return _with_editor_snippet_or_tail(
                    success_msg, new_content, target_line=replacement_line, new_lines=new_str_lines_count
                )

            # command == "insert"
            # 兼容官方参数名：insert_text (20250728) 和 new_str (20241022)
            actual_insert_text = insert_text if insert_text is not None else new_str
            if not _is_plain_int(insert_line) or not isinstance(actual_insert_text, str):
                return "Error: insert_line must be an integer between 0 and the file's line count, and insert_text (or new_str) must be a string."
            # _is_plain_int 守卫已排除 None/bool；cast 仅为让 mypy 收窄（运行时无操作）
            insert_line = cast(int, insert_line)
            crlf = _is_pure_crlf(content)
            work = content.replace("\r\n", "\n") if crlf else content
            text_to_insert = actual_insert_text.replace("\r\n", "\n") if crlf else actual_insert_text
            lines = work.splitlines(keepends=True)
            total_lines = len(lines)
            if insert_line < 0 or insert_line > total_lines:
                return f"Error: insert_line must be between 0 and {total_lines}."

            prefix = "".join(lines[:insert_line])
            suffix = "".join(lines[insert_line:])
            if prefix and not prefix.endswith(("\n", "\r")):
                prefix += "\n"
            if suffix and text_to_insert and not text_to_insert.endswith(("\n", "\r")):
                text_to_insert += "\n"
            new_content = prefix + text_to_insert + suffix
            if crlf:
                new_content = new_content.replace("\n", "\r\n")

            _write_text_editor_file(local_path, new_content)
            _spawn_persist_task(
                chat_id, safe_path, namespace=resolved_namespace,
                content_bytes=new_content.encode("utf-8"))
            success_msg = f"The file {safe_path} has been edited. Successfully inserted text after line {insert_line}."
            return _with_editor_snippet_or_tail(
                success_msg, new_content, target_line=max(1, insert_line), new_lines=text_to_insert.count("\n")
            )

        except FileNotFoundError:
            return "Error: File not found"
        except PermissionError:
            return _permission_error(command)
        except IsADirectoryError:
            return "Error: Path is a directory. Text editing only supports files."
        except OSError as exc:
            return f"Error: File operation failed: {exc.strerror or str(exc)}"
        except (ValueError, RuntimeError) as exc:
            # ValueError：_resolve_editor_path 检出逃逸 workspace 的符号
            # 链接、或写入时遇到不可编码字符（UnicodeEncodeError 是
            # ValueError 子类）。此前这类异常会直接冒泡成裸 Exception
            # （except 链只接 OSError 家族），主循环兜底成 "Exception: ..."
            # 丢失 Error: 前缀约定与恢复指引，MCP 入口（invoke 无兜底）
            # 则直接把调用打崩。RuntimeError：workspace 根目录异常。
            return f"Error: {exc}"
# ===================== 文件编辑器工具实现 =====================


# 编辑器配置
EDITOR_PREFIX = "editor"

def _editor_safe_path(path: str, allow_root: bool = False) -> str:
    """Return a normalized relative path without traversal segments.
    
    Tolerates leading slashes and common workspace prefixes (e.g. /workspace/foo.py),
    mapping them safely into the workspace while strictly rejecting directory traversal.
    If allow_root is True (used by 'view'), root paths like '.' or '/' are normalized to '.'.
    """
    if not path or not isinstance(path, str):
        raise ValueError("Invalid path: empty or non-string path not allowed")
    if "\x00" in path:
        raise ValueError("Invalid path: null byte not allowed")

    cleaned = path.strip()
    if cleaned in ("", ".", "/", "./"):
        if allow_root:
            return "."
        raise ValueError("Invalid path: empty or root path not allowed for file editing")

    # 剥离常见的虚拟工作区前缀与前导斜杠
    if cleaned.startswith("/workspace/"):
        cleaned = cleaned[len("/workspace/"):]
    elif cleaned.startswith("workspace/"):
        cleaned = cleaned[len("workspace/"):]
    elif cleaned.startswith("/"):
        cleaned = cleaned.lstrip("/")

    norm = os.path.normpath(cleaned)
    if norm in ("", "."):
        if allow_root:
            return "."
        raise ValueError("Invalid path: root path not allowed for file editing")
    if norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm):
        raise ValueError("Invalid path: directory traversal not allowed")
    return norm


def _resolve_editor_path(workspace: Path, safe_path: str, allow_root: bool = False) -> Path:
    """Resolve a path and reject any file or parent symlink escaping workspace."""
    root = workspace.resolve()
    if safe_path == ".":
        if allow_root:
            return root
        raise ValueError("Invalid path: root path not allowed for file editing")
    resolved = (root / safe_path).resolve(strict=False)
    if (resolved == root and not allow_root) or (resolved != root and root not in resolved.parents):
        raise ValueError("Invalid path: symlink escapes workspace")
    return resolved

def _editor_get_r2_key(chat_id: int, path: str) -> str:
    """生成R2存储的键，按用户隔离。"""
    safe = _editor_safe_path(path)
    return f"{EDITOR_PREFIX}/{chat_id}/{safe}"

async def persist_workspace_file(
    chat_id: int,
    rel_path: str,
    *,
    delete: bool = False,
    namespace: str | None = None,
    content_bytes: bytes | None = None,
) -> dict[str, str | bool]:
    """Persist exactly one file edited by text_editor.

    The local workspace is always the source of truth. This helper only mirrors
    the explicitly changed file to the existing R2 editor namespace; it never
    scans or syncs the whole workspace. Namespace is accepted so callers can keep
    a single workspace identity end-to-end, while the legacy R2 key remains keyed
    by chat_id for backward compatibility. ``content_bytes`` lets the caller
    persist exactly the bytes it just wrote (avoiding a re-read that could race
    with a concurrent later edit); when omitted the file is read from disk.
    """
    # Resolve the namespace here as an integrity check even though the current R2
    # key format remains chat-id based for compatibility.
    resolved_namespace = workspace_namespace(chat_id, namespace)
    workspace = workspace_workdir(chat_id, resolved_namespace)
    safe = _editor_safe_path(rel_path)
    local_path = (workspace / safe).resolve()
    if local_path != workspace and workspace not in local_path.parents:
        raise ValueError("path escapes workspace")

    key = _editor_get_r2_key(chat_id, safe)
    if delete:
        deleted = await delete_r2_object(key)
        return {"key": key, "deleted": bool(deleted)}

    if content_bytes is None:
        if not local_path.is_file():
            raise FileNotFoundError(f"workspace file not found: {safe}")
        data = await asyncio.to_thread(local_path.read_bytes)
    else:
        data = content_bytes
    content_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    url = await upload_bytes_to_r2(data, key, content_type)
    return {"key": key, "persisted": url is not None}
