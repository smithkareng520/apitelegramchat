# search_engine.py — 完整版（集成搜索缓存 & 抓取缓存）
# 新增 view 搜索功能：支持 search_terms 列表，返回匹配上下文
# str_replace 重构：无前置验证，occurrence 可选，默认替换第一个匹配
import asyncio
import aiohttp
import re
import logging
import json
import os
import base64
import hashlib
import ipaddress
import socket
import uuid
import math
import shutil
import mimetypes
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit
from typing import Any, Optional
try:
    import trafilatura  # type: ignore
    from trafilatura.settings import use_config  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    trafilatura = None
    def use_config():  # type: ignore
        return None
try:
    from curl_cffi.requests import AsyncSession  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    AsyncSession = None  # type: ignore
try:
    import feedparser  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    class _FeedParserStub:
        def parse(self, *args, **kwargs):
            return {"entries": []}
    feedparser = _FeedParserStub()  # type: ignore
from pathlib import Path
from apitelegramchat.workspace_paths import workspace_workdir, workspace_namespace

# 高德地图能力现已迁移到外部 MCP 服务 `amap-maps`（@amap/amap-maps on
# ModelScope）。所有地理编码 / POI / 路径 / 距离 / IP 定位工具都通过
# `call_mcp_tool("amap-maps", ...)` 调用该 MCP 的 maps_* 工具，不再保留任何
# 直接调用高德 Web 服务 API 或 OSM/Nominatim/Overpass/OSRM 的本地实现。
try:
    import qrcode  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    qrcode = None  # type: ignore
from io import BytesIO
import html
from cachetools import TTLCache
try:
    from lxml import html as lxml_html  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    lxml_html = None  # type: ignore
from apitelegramchat.state import set_editor_file_state, clear_editor_file_state

from apitelegramchat.config import (
    OPENROUTER_API_KEY,
    SEARCH_CACHE_TTL,
    FETCH_CACHE_TTL,
    SUPPORTED_MODELS,
    get_openrouter_provider_preferences,
)
from apitelegramchat.utils import retry_async
from apitelegramchat.mcp_client import call_mcp_tool, MCPToolError

OPENROUTER_PROVIDER_PREFERENCES = get_openrouter_provider_preferences()

from apitelegramchat.s3_utils import upload_bytes_to_r2, delete_r2_object
from apitelegramchat.workspace_utils import (
    _get_workspace_lock, _ensure_runtime_workspace,
)
# 任务工具：定义在 todo_tool.py / memory_tool.py / subagent_tool.py
# 本文件只做注册与转出
from apitelegramchat.todo_tool import TODO_TOOL  # noqa: E402
from apitelegramchat.memory_tool import MEMORY_TOOL  # noqa: E402
try:  # noqa: E402
    from apitelegramchat.subagent_tool import SUBAGENT_TOOL  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    SUBAGENT_TOOL = []
logger = logging.getLogger(__name__)

WIKIPEDIA_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/121.0",
]

FETCH_CONTENT_MAX_LEN = 200000
TRAFILATURA_TIMEOUT = 10
HTTP_TIMEOUT_SHORT = 10
HTTP_TIMEOUT_FETCH = 15
CURL_TIMEOUT = 20

BACKUP_TIMEOUT = 10

_TRAFILATURA_CONFIG = use_config()
if _TRAFILATURA_CONFIG is not None:
    try:
        _TRAFILATURA_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(TRAFILATURA_TIMEOUT))
    except Exception:
        pass

# ---------- 缓存 ----------
_search_cache = TTLCache(maxsize=200, ttl=SEARCH_CACHE_TTL)
_fetch_cache = TTLCache(maxsize=200, ttl=FETCH_CACHE_TTL)


def _search_cache_key(query: str, num_results: int | None = None) -> str:
    """Search cache key: keep query + result count distinct."""
    if num_results is None:
        return query
    return f"{query}{num_results}"


def _normalize_fetch_cache_key(url: str) -> str:
    """Drop fragment so the same page maps to one cache entry."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except Exception:
        return url


# ===================== 显式持久化单个编辑文件 =====================
async def _persist_edited_file(
    chat_id: int,
    rel_path: str,
    *,
    delete: bool = False,
    namespace: str | None = None,
):
    """Persist only the file explicitly changed through file_editor."""
    try:
        result = await persist_workspace_file(
            chat_id, rel_path, delete=delete, namespace=namespace
        )
        logger.debug("显式持久化成功：%s", result.get("key", rel_path))
    except Exception as e:
        logger.error("显式持久化失败 %s: %s", rel_path, e)


def _normalize_editor_text(text: str) -> str:
    if text is None:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_editor_lines(text: str) -> list[str]:
    return _normalize_editor_text(text).splitlines(keepends=True)


def _format_editor_line(line_no: int, text: str, width: int, highlight: bool = False) -> str:
    mark = "❯" if highlight else " "
    clean_text = text.rstrip("\n")
    return f"{mark} {line_no:>{width}} │ {clean_text}"


def _line_range_preview(lines: list[str], start_line: int, end_line: int, highlight_line: int | None = None, radius: int = 2) -> str:
    if not lines:
        return ""
    start = max(1, start_line - radius)
    end = min(len(lines), end_line + radius)
    width = len(str(len(lines)))
    preview = []
    for i in range(start, end + 1):
        preview.append(_format_editor_line(i, lines[i - 1], width, highlight=highlight_line and i == highlight_line))
    return "\n".join(preview)


def _write_editor_file(local_path: Path, new_content: str) -> None:
    backup_path = local_path.with_suffix(local_path.suffix + ".backup")
    shutil.copy2(local_path, backup_path)
    with open(local_path, "w", encoding="utf-8") as f:
        f.write(new_content)


# ---------- 主函数 ----------
async def execute_file_editor(
    chat_id: int,
    command: str,
    path: str,
    namespace: str | None = None,
    view_range: list[int] | None = None,
    search_terms: list[str] | None = None,        # 新增：view 搜索关键词
    old_str: str | None = None,
    new_str: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    occurrence: int | None = None,                # 改为 None，由内部决定默认
    allow_multi: bool = False,
    use_regex: bool = False,
    delete_range: list[int] | None = None,
    insert_line: int | None = None,
    insert_text: str | None = None,
    file_text: str | None = None,
    confirm: bool = False,
) -> str:
    # list 命令 + view 命令遇到目录时，允许 path 为空 / "." / "./"，对应 workspace 根
    allow_root_path = command in ("list", "view") and (
        not path or not isinstance(path, str) or path.strip() in ("", "/", ".", "./")
    )
    try:
        if allow_root_path:
            safe_path = "."
        else:
            safe_path = _editor_safe_path(path)
    except ValueError as e:
        return f"Error: {e}"

    # ★ init 在 workspace lock 外面执行：R2 网络同步可能耗时数秒，
    #   不应阻塞其他工具调用获取 workspace lock。init 只需要 init_lock
    #   （在 _ensure_workspace_initialized 内部获取），与 workspace lock 独立。
    resolved_namespace = workspace_namespace(chat_id, namespace)
    await _ensure_runtime_workspace(chat_id, resolved_namespace)

    lock = await _get_workspace_lock(chat_id, resolved_namespace)
    async with lock:
        workspace = workspace_workdir(chat_id, resolved_namespace).resolve()
        try:
            local_path = workspace if allow_root_path else _resolve_editor_path(workspace, safe_path)
        except ValueError as exc:
            return f"Error: {exc}"

        # ----- view (增强：支持搜索关键词) -----
        if command == "view":
            if not local_path.exists():
                return f"Error: File not found: {path}"
            # 如果是目录，转走 list 命令的逻辑（向后兼容）
            if local_path.is_dir():
                return _list_directory(local_path, path)
            with open(local_path, "r", encoding="utf-8") as f:
                content = f.read()
            # 自动更新状态（隐式 view）
            mtime = local_path.stat().st_mtime
            set_editor_file_state(chat_id, safe_path, content, mtime)
            lines = content.splitlines()
            total_lines = len(lines)
            width = len(str(total_lines))

            # 如果提供了 search_terms，执行搜索并返回上下文
            if search_terms and isinstance(search_terms, list) and len(search_terms) > 0:
                # 确保每个词都是字符串
                terms = [str(t).strip() for t in search_terms if str(t).strip()]
                if not terms:
                    # 如果没有有效关键词，回退到普通查看
                    pass
                else:
                    result_parts = []
                    result_parts.append(f"File: {path}")
                    for term in terms:
                        # 搜索 term（区分大小写，可按需添加 flags）
                        matches = []
                        for idx, line in enumerate(lines, start=1):
                            if term in line:
                                matches.append(idx)
                        if not matches:
                            result_parts.append(f"Search term '{term}': No matches found.")
                            continue
                        result_parts.append(f"Search term '{term}': Found {len(matches)} match(es):")
                        # 对于每个匹配，显示前后各2行
                        for match_line in matches[:10]:  # 最多显示10个匹配，避免过长
                            start_ctx = max(0, match_line - 3)  # 0-based index
                            end_ctx = min(total_lines, match_line + 2)
                            context_lines = lines[start_ctx:end_ctx]
                            # 加上行号
                            result_parts.append(f"  At line {match_line}:")
                            for i, line in enumerate(context_lines, start=start_ctx+1):
                                result_parts.append("  " + _format_editor_line(i, line, width, highlight=i == match_line))
                        if len(matches) > 10:
                            result_parts.append(f"  ... and {len(matches)-10} more match(es).")
                    return "\n".join(result_parts)

            # 如果没有 search_terms，走原有逻辑
            if view_range:
                start, end = view_range
                if start < 1:
                    start = 1
                if end == -1 or end > total_lines:
                    end = total_lines
                selected = lines[start-1:end]
                numbered = [_format_editor_line(idx+1, line, width) for idx, line in enumerate(selected, start=start)]
                return "\n".join(numbered)
            else:
                numbered = [_format_editor_line(idx+1, line, width) for idx, line in enumerate(lines)]
                return "\n".join(numbered)

        # ----- list (列出目录内容；如果 bash 沙箱不可用，这是模型查看
        # 工作区文件结构的唯一途径) -----
        elif command == "list":
            display = path.strip() if path and path.strip() not in ("", "/", ".", "./") else "./"
            if not local_path.exists():
                # 兜底：列出 workspace 根
                return _list_directory(workspace, "./")
            return _list_directory(local_path, display)

        # ----- create -----
        elif command == "create":
            if file_text is None:
                return "Error: Missing file_text for create."
            if local_path.exists():
                return f"Error: File already exists: {path}. Use str_replace to edit or delete+confirm to remove."
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(file_text)
            mtime = local_path.stat().st_mtime
            set_editor_file_state(chat_id, safe_path, file_text, mtime)
            asyncio.create_task(_persist_edited_file(chat_id, safe_path, namespace=resolved_namespace))
            return f"File created successfully: {path}"

        # ----- replace_lines -----
        elif command == "replace_lines":
            replacement_text = new_str if new_str is not None else insert_text
            if delete_range is None or replacement_text is None:
                return "Error: replace_lines requires delete_range and new_str (or insert_text)."
            if not local_path.exists():
                return f"Error: File not found: {path}"
            with open(local_path, "r", encoding="utf-8") as f:
                full_content = f.read()
            lines = _split_editor_lines(full_content)
            total = len(lines)
            start_del, end_del = delete_range
            if start_del < 1:
                start_del = 1
            if end_del == -1 or end_del > total:
                end_del = total
            if start_del > total:
                return f"Error: start_line {start_del} exceeds total lines {total}"
            replacement_text = _normalize_editor_text(replacement_text)
            if replacement_text and not replacement_text.endswith("\n") and end_del < total:
                replacement_text += "\n"
            new_full = "".join(lines[:start_del - 1]) + replacement_text + "".join(lines[end_del:])
            _write_editor_file(local_path, new_full)
            mtime = local_path.stat().st_mtime
            set_editor_file_state(chat_id, safe_path, new_full, mtime)
            asyncio.create_task(_persist_edited_file(chat_id, safe_path, namespace=resolved_namespace))
            preview_lines = new_full.splitlines()
            preview = _line_range_preview(preview_lines, start_del, min(len(preview_lines), start_del + max(0, len(replacement_text.splitlines()) - 1) if replacement_text else start_del), highlight_line=start_del)
            return f"Successfully replaced lines {start_del}-{end_del} in {path}.\n\nResult around edit:\n{preview}"

        # ----- str_replace (简化版，无前置验证) -----
        elif command == "str_replace":
            if old_str is None or new_str is None:
                return "Error: Missing old_str or new_str for str_replace"
            # 拒绝空 old_str，否则 str.count("") 返回 len+1，str.replace("",x) 会在每个字符间插入 x
            if not old_str:
                return "Error: old_str must be non-empty for str_replace"

            if not local_path.exists():
                return f"Error: File not found: {path}"

            # 读取文件并自动更新状态
            with open(local_path, 'r', encoding='utf-8') as f:
                full_content = f.read()
            mtime = local_path.stat().st_mtime
            set_editor_file_state(chat_id, safe_path, full_content, mtime)

            lines = full_content.splitlines(keepends=True)
            total_lines = len(lines)

            _start = start_line if start_line is not None else 1
            _end = end_line if end_line is not None else -1

            if _start < 1:
                _start = 1
            if _end == -1 or _end > total_lines:
                _end = total_lines
            if _start > total_lines:
                return f"Error: start_line {_start} exceeds total lines {total_lines}"

            slice_content = "".join(lines[_start-1:_end])

            # 计算匹配
            if use_regex:
                pattern = re.compile(old_str, re.DOTALL)
                matches = list(pattern.finditer(slice_content))
                match_count = len(matches)
            else:
                match_count = slice_content.count(old_str)
                if match_count == 0:
                    # 尝试 strip 空格再试一次
                    stripped_old = old_str.strip()
                    if stripped_old != old_str:
                        old_str = stripped_old
                        match_count = slice_content.count(old_str)

            if match_count == 0:
                return f"No match found for replacement text in lines {_start}-{_end}. Please adjust old_str or range."

            # 执行替换
            if use_regex:
                if allow_multi:
                    new_slice = pattern.sub(new_str, slice_content)
                else:
                    # 确定 occurrence
                    if occurrence is None:
                        occurrence = 1
                    elif occurrence < 1 or occurrence > match_count:
                        return f"Error: occurrence must be between 1 and {match_count}."
                    m = matches[occurrence - 1]
                    new_slice = slice_content[:m.start()] + new_str + slice_content[m.end():]
            else:
                if allow_multi:
                    new_slice = slice_content.replace(old_str, new_str)
                else:
                    if occurrence is None:
                        occurrence = 1
                    elif occurrence < 1 or occurrence > match_count:
                        return f"Error: occurrence must be between 1 and {match_count}."
                    # 分割替换
                    parts = slice_content.split(old_str)
                    before = old_str.join(parts[:occurrence])
                    after = old_str.join(parts[occurrence:])
                    new_slice = before + new_str + after

            # 写入文件
            new_full = "".join(lines[:_start - 1]) + new_slice + "".join(lines[_end:])
            _write_editor_file(local_path, new_full)
            mtime = local_path.stat().st_mtime
            set_editor_file_state(chat_id, safe_path, new_full, mtime)
            asyncio.create_task(_persist_edited_file(chat_id, safe_path, namespace=resolved_namespace))

            # 生成预览
            new_lines = new_full.splitlines()
            # 估算修改的行范围
            changed_start = _start
            changed_end = _start + len(new_slice.splitlines()) - 1
            if changed_end > len(new_lines):
                changed_end = len(new_lines)
            preview = _line_range_preview(new_lines, max(1, changed_start-2), min(len(new_lines), changed_end+2), highlight_line=changed_start)

            if allow_multi:
                replace_info = f"Replaced all {match_count} matches."
            else:
                replace_info = f"Replaced occurrence {occurrence} of {match_count} match(es)."
            return f"Successfully replaced text in {path} (lines {changed_start}-{changed_end}). {replace_info}\nTotal matches found: {match_count}\nResult around edit:\n{preview}"

        # ----- insert -----
        elif command == "insert":
            if insert_line is None or insert_text is None:
                return "Error: Missing insert_line or insert_text"
            if not local_path.exists():
                return f"Error: File not found: {path}"
            with open(local_path, "r", encoding="utf-8") as f:
                content = f.read()
            lines = content.splitlines(keepends=True)
            total = len(lines)
            if insert_line < 0:
                insert_line = 0
            if insert_line > total:
                insert_line = total
            if insert_line == 0:
                new_content = insert_text + content
            else:
                if insert_line == total and content and not content.endswith("\n"):
                    content += "\n"
                    # 修复：必须基于更新后的 content 重新计算 lines，否则追加的换行会被丢弃
                    lines = content.splitlines(keepends=True)
                new_content = "".join(lines[:insert_line]) + insert_text + "".join(lines[insert_line:])
            _write_editor_file(local_path, new_content)
            mtime = local_path.stat().st_mtime
            set_editor_file_state(chat_id, safe_path, new_content, mtime)
            asyncio.create_task(_upload_edited_file(chat_id, safe_path, new_content))
            return f"Successfully inserted text after line {insert_line}."

        # ----- delete -----
        elif command == "delete":
            if local_path.is_dir() or path.endswith("/"):
                if not confirm:
                    return "Error: Directory deletion requires confirm=True."
                if not local_path.exists():
                    return f"Directory {path} does not exist."
                # Directory deletion is local to the runtime tree. Persist deletions
                # for each existing file explicitly; the commit boundary is still file-based.
                removed = []
                for child in local_path.rglob("*"):
                    if child.is_file() and not child.is_symlink():
                        rel = child.relative_to(workspace).as_posix()
                        removed.append(rel)
                shutil.rmtree(local_path)
                for rel in removed:
                    await _persist_edited_file(chat_id, rel, delete=True)
                clear_editor_file_state(chat_id, safe_path)
                return f"Successfully deleted directory: {path}"
            else:
                if delete_range:
                    if not local_path.exists():
                        return f"Error: File not found: {path}"
                    with open(local_path, "r", encoding="utf-8") as f:
                        lines = f.read().splitlines(keepends=True)
                    total = len(lines)
                    start_del, end_del = delete_range
                    if start_del < 1:
                        start_del = 1
                    if end_del == -1 or end_del > total:
                        end_del = total
                    if start_del > total:
                        return f"Error: start_line {start_del} exceeds total lines {total}"
                    new_lines = lines[:start_del-1] + lines[end_del:]
                    new_content = "".join(new_lines)
                    _write_editor_file(local_path, new_content)
                    mtime = local_path.stat().st_mtime
                    set_editor_file_state(chat_id, safe_path, new_content, mtime)
                    asyncio.create_task(_upload_edited_file(chat_id, safe_path, new_content))
                    return f"Successfully deleted lines {start_del}-{end_del} from {path}"
                else:
                    if not local_path.exists():
                        return f"Error: File not found: {path}"
                    os.remove(local_path)
                    clear_editor_file_state(chat_id, safe_path)
                    await _persist_edited_file(chat_id, safe_path, delete=True)
                    return f"Successfully deleted file: {path}"

        # ----- undo_edit -----
        elif command == "undo_edit":
            if not local_path.exists():
                return f"Error: File not found: {path}"
            backup_path = local_path.with_suffix(local_path.suffix + ".backup")
            if not backup_path.exists():
                return f"Error: No backup found for {path}. Unable to undo."
            shutil.copy2(backup_path, local_path)
            os.remove(backup_path)
            with open(local_path, "r", encoding="utf-8") as f:
                content = f.read()
            mtime = local_path.stat().st_mtime
            set_editor_file_state(chat_id, safe_path, content, mtime)
            asyncio.create_task(_persist_edited_file(chat_id, safe_path, namespace=resolved_namespace))
            return f"Undo successful. Reverted {path} to previous version."

        else:
            return f"Error: Unknown command: {command}"


# ========== 缓存函数 ==========
def get_search_cache(query: str, num_results: int | None = None):
    return _search_cache.get(_search_cache_key(query, num_results))


def set_search_cache(query: str, result: str, num_results: int | None = None):
    _search_cache[_search_cache_key(query, num_results)] = result


def get_fetch_cache(url: str):
    return _fetch_cache.get(_normalize_fetch_cache_key(url))


def set_fetch_cache(url: str, content: str):
    _fetch_cache[_normalize_fetch_cache_key(url)] = content


# ---------- 工具函数 ----------
def _truncate(text: str, max_len: int = FETCH_CONTENT_MAX_LEN, suffix: str = "…（内容已截断）") -> str:
    if text and len(text) > max_len:
        return text[:max_len] + suffix
    return text


def _get_title_from_html(html_content: str) -> str:
    if not html_content:
        return "无标题"
    try:
        tree = lxml_html.fromstring(html_content)
        title_elem = tree.find('.//title')
        if title_elem is not None and title_elem.text:
            title = title_elem.text.strip()
            return title[:200] if title else "无标题"
    except Exception:
        pass
    return "无标题"


def _get_image_models_by_capability():
    """
    返回两个列表：
    - text_only_models: 仅支持文生图（native_image=True, vision=False）
    - edit_models: 支持图生图/编辑（native_image=True, vision=True）
    """
    text_only = []
    edit_models = []
    for model_id, cfg in SUPPORTED_MODELS.items():
        if not cfg.native_image:
            continue
        if cfg.vision:
            edit_models.append(model_id)
        else:
            text_only.append(model_id)
    return text_only, edit_models

# ----- 工具 1：纯文生图 -----
TEXT_ONLY_MODELS, EDIT_MODELS = _get_image_models_by_capability()


def _get_video_models() -> list[str]:
    """返回所有支持原生视频生成的模型 ID（native_video=True）。"""
    return [model_id for model_id, cfg in SUPPORTED_MODELS.items() if cfg.native_video]


# ----- 工具 2：视频生成 -----
VIDEO_MODELS = _get_video_models()

# ---------- 工具定义 ----------
ASK_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Pause the agent and ask the current user for a required clarification or choice. "
            "Use this only when the next step materially depends on missing user preference or confirmation. "
            "The tool suspends until the user answers in Telegram, then returns a structured result and the same agent turn continues. "
            "Prefer 2-6 concise options. Do not use for information you can reasonably infer or discover yourself. "
            "Never call this tool more than once in the same tool-call batch; ask one question at a time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "清晰、具体的问题。不要重复用户已经明确提供的信息。"
                },
                "options": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "稳定的内部选项 ID。"},
                            "label": {"type": "string", "description": "按钮上显示的简短文字。"},
                            "description": {"type": "string", "description": "可选的补充说明。"}
                        },
                        "required": ["id", "label"]
                    }
                },
                "multiple": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否允许多选。多选时用户需要点击提交。"
                },
                "allow_custom": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否允许用户放弃预设选项，直接输入自定义回答。"
                }
            },
            "required": ["question", "options"]
        }
    }
}

SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for real-time information (titles, snippets, URLs). Multiple independent queries can be issued in one response. To read a result in depth, follow up with fetch_url (one URL per call).",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：搜索2024年诺贝尔奖"
                    },
                    "query": {"type": "string", "description": "搜索关键词"},
                    "num_results": {"type": "integer", "description": "返回结果数（1-10）", "default": 10}
                },
                "required": ["query"]
            },
            "input_examples": [
                {"query": "2024 诺贝尔物理学奖 获奖者", "num_results": 5},
                {"query": "Python 3.13 新特性", "num_results": 3}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and read full content of a specific URL. Use when a search result needs deeper reading or the user gave you a link. One URL per call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整的 URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wikipedia",
            "description": "Look up a topic on Wikipedia and return an encyclopedic summary. Prefer for factual / definitional queries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "条目标题或关键词"},
                    "lang": {"type": "string", "description": "语言代码（zh/en）", "default": "zh"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "exchange_rate",
            "description": "Get real-time exchange rates for a base currency. Optionally filter to a single target currency.",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：查询美元兑人民币汇率"
                    },
                    "base": {"type": "string", "description": "基础货币代码（如 USD、CNY）"},
                    "target": {"type": "string", "description": "目标货币（可选）"}
                },
                "required": ["base"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_lookup",
            "description": "Look up book metadata (title, author, cover, rating, abstract) by title, author, or ISBN.",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：查找三体作者"
                    },
                    "query": {"type": "string", "description": "书名、作者或 ISBN"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": (
                "Get comprehensive weather data for a city. Returns three blocks:\n"
                "- current: temp, feels_like, humidity, wind, pressure, visibility, uvIndex, precip, condition, obs_time.\n"
                "- hourly: up to 24 hours of forecast (temp, condition, precip, wind, uvIndex, etc.).\n"
                "- daily: up to 5 days (max/min/avg temp, condition, sunrise/sunset, moon phase, probabilities).\n"
                "Use for any weather-related question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：查询北京今日天气"
                    },
                    "city": {"type": "string", "description": "城市名（如 Beijing、Shanghai）"},
                    "unit": {"type": "string", "enum": ["c", "f"], "default": "c"},
                    "hours": {"type": "integer", "default": 6, "description": "摘要中展示的小时数（完整数据始终可用）"}
                },
                "required": ["city"]
            },
            "input_examples": [
                {"city": "Beijing", "unit": "c", "hours": 12},
                {"city": "New York", "unit": "f"}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "news",
            "description": "Get latest headlines from major news sources (bbc / reuters / cna / cnn / nytimes / guardian / zaobao / xinhua / all).",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["bbc", "reuters", "cna", "cnn", "nytimes", "guardian", "zaobao", "xinhua", "all"], "default": "bbc"},
                    "limit": {"type": "integer", "default": 5, "description": "返回条数（1-10）"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "crypto_price",
            "description": "Get the current spot price of a cryptocurrency (btc / eth / doge / etc.) in the requested currency.",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：查询比特币价格"
                    },
                    "coin": {"type": "string", "description": "币种符号（btc、eth、doge 等）"},
                    "currency": {"type": "string", "default": "usd", "description": "计价货币"}
                },
                "required": ["coin"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ip_geo",
            "description": "Get geolocation info (country, region, city, ISP, ASN) for an IPv4 address via the amap-maps MCP `maps_ip_location` tool. If ip omitted, queries the server's own IP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "IPv4 地址（可选，缺省时查服务器自身）"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "qr_code",
            "description": "Generate a QR code image from text or URL and return its R2 URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要编码的文本或 URL"}
                },
                "required": ["text"]
            }
        }
    },
    # ===================== 地图工具（全部由 amap-maps MCP 提供） =====================
    {
        "type": "function",
        "function": {
            "name": "geocode",
            "description": "将地址或地名转换为经纬度坐标（地理编码）。委托给 amap-maps MCP 的 maps_geo 工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "address": {"type": "string", "description": "地址或地名，如“北京市海淀区中关村”。"}
                },
                "required": ["address"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "route",
            "description": "统一规划骑行、步行、驾车或公交路线。origin 与 destination 必须是高德坐标“经度,纬度”；公交跨城时必须同时提供 city 和 cityd。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "origin": {"type": "string", "description": "起点经纬度，格式为“经度,纬度”，例如“116.397128,39.916527”。"},
                    "destination": {"type": "string", "description": "终点经纬度，格式为“经度,纬度”。"},
                    "mode": {"type": "string", "enum": ["cycling", "walking", "driving", "transit"], "default": "driving", "description": "骑行、步行、驾车或公交。"},
                    "city": {"type": "string", "description": "公交起点城市；跨城公交时必填。"},
                    "cityd": {"type": "string", "description": "公交终点城市；跨城公交时必填。"}
                },
                "required": ["origin", "destination"]
            },
            "input_examples": [
                {"origin": "116.397128,39.916527", "destination": "116.481488,39.990464", "mode": "cycling"},
                {"origin": "116.397128,39.916527", "destination": "121.473701,31.230416", "mode": "transit", "city": "北京", "cityd": "上海"}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "distance",
            "description": "测量两个高德经纬度坐标之间的直线距离。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "origin": {"type": "string", "description": "起点经纬度，格式“经度,纬度”。"},
                    "destination": {"type": "string", "description": "终点经纬度，格式“经度,纬度”。"}
                },
                "required": ["origin", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "poi_keyword_search",
            "description": "按关键词搜索 POI；有明确城市范围时传 city，不要将 POI ID 传入本工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "keywords": {"type": "string", "description": "搜索关键词，如“故宫博物院”。"},
                    "city": {"type": "string", "description": "可选的查询城市，如“北京”。"}
                },
                "required": ["keywords"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "poi_nearby_search",
            "description": "在指定中心点附近搜索 POI。location 必须是“经度,纬度”，radius 单位为米。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "keywords": {"type": "string", "description": "搜索关键词，如“咖啡馆”。"},
                    "location": {"type": "string", "description": "中心点经纬度，格式“经度,纬度”。"},
                    "radius": {"type": "integer", "description": "半径，单位米，范围 1–50000，默认 1000。"}
                },
                "required": ["keywords", "location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "poi_details",
            "description": "根据关键词搜索或周边搜索返回的 POI ID 获取地点详情；不要传地点名称。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "id": {"type": "string", "description": "关键词搜索或周边搜索返回的 POI ID。"}
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_editor",
            "description": (
                "View / create / edit / delete / undo text files (and directories). Recommended workflow:\n"
                "  1. 'view' once to inspect the target area; pass 'search_terms' to jump to keywords.\n"
                "  2. For line-accurate edits use command='replace_lines' with 'delete_range' + 'new_str'.\n"
                "  3. For text matches use command='str_replace' with a narrow start_line/end_line scope.\n"
                "  4. If a replacement misses, re-view the smallest relevant range instead of retrying blindly.\n"
                "  5. For repeated structures set 'allow_multi'=True or 'use_regex'=True.\n"
                "For delete use 'delete_range' (e.g. [10, 20]); for undo use command='undo_edit'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：创建配置文件"
                    },
                    "command": {
                        "type": "string",
                        "enum": ["view", "list", "str_replace", "replace_lines", "create", "insert", "delete", "undo_edit"],
                        "description": "要执行的命令。list 用于列出工作区目录内容（即使 bash 沙箱不可用也能用）。"
                    },
                    "path": {
                        "type": "string",
                        "description": "文件或目录路径。list 命令时传空串、'.' 或 './' 表示列出 workspace 根。目录可省略尾 / 。"
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "view 命令的 [起始行, 结束行]（1-indexed，-1 表示到末尾）。"
                    },
                    "search_terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "view 时可选的关键词列表，返回每个匹配的行号与上下文。"
                    },
                    "old_str": {
                        "type": "string",
                        "description": "str_replace 时要替换的文本。use_regex=True 时作为正则。"
                    },
                    "new_str": {
                        "type": "string",
                        "description": "str_replace / replace_lines 的新文本。"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "替换/删除范围的起始行（1-indexed，默认 1）。"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "范围的结束行（1-indexed，-1 表示到末尾，默认 -1）。"
                    },
                    "occurrence": {
                        "type": "integer",
                        "description": "仅 allow_multi=False 时有效：替换第 N 次出现（1-indexed）。未提供且多处匹配时替换首个。"
                    },
                    "allow_multi": {
                        "type": "boolean",
                        "description": "True 则替换范围内的所有匹配。默认 False。"
                    },
                    "use_regex": {
                        "type": "boolean",
                        "description": "True 则将 old_str 作为正则。默认 False。"
                    },
                    "delete_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "delete 命令的 [起始行, 结束行]（1-indexed）。"
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "insert 命令插入位置（0 表示文件开头）。"
                    },
                    "insert_text": {
                        "type": "string",
                        "description": "insert 命令要插入的文本。"
                    },
                    "file_text": {
                        "type": "string",
                        "description": "create 命令的文件内容。"
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "删除目录时必填，必须为 true。"
                    }
                },
                "required": ["command", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute shell commands in the user workspace. The workspace is local-only and is never synchronized wholesale to R2. "
                "Use for installs, tests, builds, scripts, and system operations. Generated files and dependencies remain local to this workspace. "
                "Avoid interactive commands (vim, top) and long-running processes. Set 'restart'=true to reset the session. "
                "When the model chooses to use a skill, it can `cd skills/<skill_id>` and read the skill instructions there. To list files without Bash, use file_editor command='list'.\n"
                "\n"
                "CRITICAL — upload/ and download/ are staging buffers, not execution roots:\n"
                "- You MAY read and write files in upload/ and download/ via relative paths from your cwd, "
                "e.g. `cp out.txt ../upload/out.txt` or `cat ../download/brief.pdf > /dev/null`.\n"
                "- You MAY NOT `cd` into upload/ or download/, and you MAY NOT execute any command while your "
                "cwd is inside them. The sandbox enforces this: `cd ../upload/...` is rejected, and any "
                "command run after a forbidden cd will also be rejected.\n"
                "- This prevents dependency installs / build tools from polluting the staging area.\n"
                "- To move a file from your workdir into upload/ prefer `stage_upload`; to move a file from "
                "download/ into your workdir use `fetch_download`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：查看项目文件列表"
                    },
                    "command": {
                        "type": "string",
                        "description": "要执行的 bash 命令。"
                    },
                    "restart": {
                        "type": "boolean",
                        "description": "true 则重启 bash 会话（清空状态）。"
                    }
                }
            },
            "input_examples": [
                {"command": "ls -la"},
                {"command": "pwd"},
                {"command": "python3 script.py", "restart": False},
                {"command": "cp report.pdf ../upload/report.pdf"},
                {"restart": True}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_download",
            "description": (
                "Copy one or more user-uploaded files from download/ into the agent's ephemeral workdir (workspace root/). "
                "User-uploaded documents land in download/ automatically when the model cannot ingest them natively; bash "
                "cannot `cd` into download/, so this tool is the canonical way to make a downloaded file available to "
                "file_editor / bash / other tools. After fetch_download the file lives at the same relative path inside "
                "the workdir.\n"
                "download/ is a local-only buffer (not mirrored to R2); if it is empty after a process restart, ask the "
                "user to re-send the document.\n"
                "Call list_download first when you do not know the exact filenames."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。"
                    },
                    "filenames": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "要取回的文件相对路径列表（相对 download/ 根）。"
                    },
                    "overwrite": {
                        "type": "boolean",
                        "default": False,
                        "description": "true 则覆盖工作区已存在的同名文件；默认 false 会跳过并返回 skipped。"
                    }
                },
                "required": ["filenames"]
            },
            "input_examples": [
                {"filenames": ["brief.pdf"]},
                {"filenames": ["data.csv", "notes.txt"], "overwrite": True}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stage_upload",
            "description": (
                "Copy one or more files from the agent workspace (workspace root/) into upload/, the staging "
                "area for outgoing attachments. present_files ONLY reads from upload/, so you must call stage_upload "
                "before present_files can send a file to the user. The staged file is also mirrored to R2 so it "
                "survives process restarts.\n"
                "Pass exact file paths relative to the workdir root; directories and wildcards are not accepted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。"
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "要暂存的文件路径列表（相对工作区根，workspace root/ 之下）。"
                    }
                },
                "required": ["paths"]
            },
            "input_examples": [
                {"paths": ["report.pdf"]},
                {"paths": ["out/data.csv", "out/summary.md"]}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_download",
            "description": (
                "List all files currently in download/ (user-upplied documents that have not been fetched into the "
                "workdir yet). Returns JSON: {\"files\": [{\"path\": ..., \"size\": ...}], \"count\": N}."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_upload",
            "description": (
                "List all files currently staged in upload/ (waiting to be sent to the user via present_files). "
                "Returns JSON: {\"files\": [{\"path\": ..., \"size\": ...}], \"count\": N}."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "present_files",
            "description": (
                "Send one or more files from the upload/ staging tree to the chat as attachments. "
                "Files MUST already be staged under upload/ — either via the stage_upload tool or via bash "
                "(e.g. `cp out.txt ../upload/out.txt`). Files left in the ephemeral workdir are NOT directly "
                "sendable; this is the execution/persistence boundary.\n"
                "Pass exact paths relative to upload/; wildcards are not supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要发送的文件路径列表（相对 upload/ 根）。"
                    }
                },
                "required": ["paths"]
            }
        }
    },
    *(
        [{
            "type": "function",
            "function": {
                "name": "generate_image_from_text",
                "description": (
                    "Generate a new image from a text prompt only (no reference image). Use when the user wants to create an image from scratch. "
                    f"Available models: {', '.join(TEXT_ONLY_MODELS)}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                        "prompt": {"type": "string", "description": "详细的图片描述"},
                        "model": {
                            "type": "string",
                            "enum": TEXT_ONLY_MODELS,
                            "description": "选择一个支持文生图的模型。"
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
                            "default": "1:1"
                        },
                        "image_size": {
                            "type": "string",
                            "enum": ["1K", "2K", "4K"],
                            "default": "1K"
                        },
                        "num_images": {
                            "type": "integer",
                            "default": 1,
                            "minimum": 1,
                            "maximum": 4
                        }
                    },
                    "required": ["prompt", "model"]
                }
            }
        }]
        if TEXT_ONLY_MODELS else []
    ),
    *(
        [{
            "type": "function",
            "function": {
                "name": "edit_image_with_reference",
                "description": (
                    "Edit an existing image using a reference image + a text prompt. Use when the user provides an image and wants to change something (style, object, background, angle, etc.). "
                    f"Available models: {', '.join(EDIT_MODELS)}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                        "prompt": {"type": "string", "description": "编辑指令（如 '改成水彩画风格'）"},
                        "image_url": {
                            "type": "string",
                            "description": "参考图的 URL 或 base64 数据。用户上传过图片时必填。"
                        },
                        "model": {
                            "type": "string",
                            "enum": EDIT_MODELS,
                            "description": "选择一个支持图生图编辑的模型。"
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
                            "default": "1:1"
                        },
                        "image_size": {
                            "type": "string",
                            "enum": ["1K", "2K", "4K"],
                            "default": "1K"
                        },
                        "num_images": {
                            "type": "integer",
                            "default": 1,
                            "minimum": 1,
                            "maximum": 4
                        }
                    },
                    "required": ["prompt", "image_url", "model"]
                }
            }
        }]
        if EDIT_MODELS else []
    ),
    *(
        [{
            "type": "function",
            "function": {
                "name": "generate_video",
                "description": (
                    "Generate a short video from a text prompt. Use when the user explicitly asks to create / generate / make a video. Do NOT use for animated images or GIFs (use generate_image_from_text instead). Generation is async and may take 1-5 minutes; the returned URL is auto-embedded in the tool card and can also be inlined via <figure><video src=\"URL\"></video></figure>. "
                    f"Available models: {', '.join(VIDEO_MODELS) if VIDEO_MODELS else '(none configured)'}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                        "prompt": {
                            "type": "string",
                            "description": "视频场景详细描述（主体、运动、镜头、风格等）。"
                        },
                        "model": {
                            "type": "string",
                            "enum": VIDEO_MODELS,
                            "description": "选择一个支持文生视频的模型。"
                        },
                        "duration": {
                            "type": "integer",
                            "description": "视频时长（秒），范围 3-30，默认 5。",
                            "default": 5,
                            "minimum": 3,
                            "maximum": 30
                        }
                    },
                    "required": ["prompt", "model"]
                }
            }
        }]
        if VIDEO_MODELS else []
    ),
    ASK_USER_TOOL,
    # ===================== 任务 / 待办工具 =====================
    # 让 agent 拥有持久化的待办清单能力：add/list/done/undone/delete/clear/edit。
    # 数据按用户隔离，存放在 ./state/{user_id}/todos.json 并随 R2 同步。
    # 仅在工具结果区显示富文本摘要；交互由 ask_user 工具统一处理。
    TODO_TOOL,
    # ===================== 长期记忆工具 =====================
    # 跨会话保留的事实/偏好/人物/事件——不同于会自动修剪的对话历史。
    # 数据落在 ./state/{user_id}/memories.json，随 R2 同步。
    MEMORY_TOOL,
    # ===================== 子 Agent 工具 =====================
    # 派生一个干净上下文的子 agent 处理子任务，自带最小 agentic loop，
    # 工具白名单受控，禁递归调用 subagent/memory。
    SUBAGENT_TOOL,
]


# =============================================================================
# 工具实现
# =============================================================================

# Google CSE / DuckDuckGo 抓取实现已移除，搜索改为通过外部 MCP 服务
# （bing-cn-mcp-server，见 mcp_client.py）调用其 bing_search 工具。

# 专门用于触发 retry_async 重试的临时异常——MCP 搜索服务限流/超时或空结果时抛出
class MCPSearchTransientError(Exception):
    """外部 MCP 搜索服务临时不可用 / 空结果，可重试。"""


@retry_async(max_retries=2, delay=1.5, backoff=2.0,
            exceptions=(MCPToolError, MCPSearchTransientError, asyncio.TimeoutError))
async def _search_via_mcp(query: str, num_results: int) -> list[dict] | None:
    """通过外部 MCP 服务（bing-cn-mcp-server 的 bing_search 工具）执行网页搜索。

    工具协议：bing_search(query: str, count?: int, offset?: int) -> 纯文本，
    形如：
        搜索关键词: ...
        找到约 N 条结果
        返回前 M 条结果:
        ====...====
        [1] 标题
            链接: https://...
            摘要: ...
        [2] ...
    这里把该文本解析为统一的 {title, link, snippet} 列表，交给
    _format_search_results 统一格式化，保持对外的返回格式不变。
    """
    query = (query or "").strip()
    if not query:
        return None

    try:
        raw_text = await call_mcp_tool(
            "bing-cn-mcp-server",
            "bing_search",
            {"query": query, "count": num_results},
        )
    except MCPToolError as e:
        logger.warning(f"MCP 搜索调用失败: {e}")
        raise

    items = _parse_bing_mcp_result(raw_text, num_results)
    if items:
        return items

    logger.info("MCP 搜索服务返回空结果")
    raise MCPSearchTransientError("MCP search returned no results")


def _parse_bing_mcp_result(raw_text: str, num_results: int) -> list[dict]:
    """解析 bing-cn-mcp-server 的 bing_search 返回，提取 title/link/snippet。

    当前服务端返回的是 JSON 字符串（见 test_mcp_bing_search.py 实测输出）：
        {"query": "...", "results": [{"uuid","title","url","snippet","displayUrl"}, ...], "totalResults": N}
    优先按 JSON 解析；如果服务端将来又切回旧的纯文本格式
        [1] 标题
            链接: https://...
            摘要: ...
    则回退到原来的正则解析，保证兼容两种返回。
    """
    if not raw_text:
        return []

    # ---- 优先尝试 JSON 格式 ----
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        data = None

    if isinstance(data, dict) and isinstance(data.get("results"), list):
        items: list[dict] = []
        for r in data["results"]:
            if not isinstance(r, dict):
                continue
            title = (r.get("title") or "").strip() or "无标题"
            link = (r.get("url") or r.get("link") or "").strip()
            snippet = (r.get("snippet") or "").strip()
            if not link:
                continue
            items.append({"title": title, "link": link, "snippet": snippet})
            if len(items) >= num_results:
                break
        return items

    # ---- 回退：旧的纯文本格式 ----
    items = []
    entry_re = re.compile(
        r'^\s*\[\d+\]\s*(?P<title>.+?)\s*$'
        r'(?:\n\s*链接[:：]\s*(?P<link>\S+))?'
        r'(?:\n\s*摘要[:：]\s*(?P<snippet>.*?))?'
        r'(?=\n\s*\[\d+\]|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    for m in entry_re.finditer(raw_text):
        title = (m.group("title") or "").strip() or "无标题"
        link = (m.group("link") or "").strip()
        snippet = (m.group("snippet") or "").replace("\n", " ").strip()
        if not link:
            continue
        items.append({"title": title, "link": link, "snippet": snippet})
        if len(items) >= num_results:
            break
    return items


def _format_search_results(items: list, query: str, engine: str, requested: int | None = None) -> str:
    success_count = len(items)
    requested_count = requested if isinstance(requested, int) and requested > 0 else success_count
    lines = [f"🔍 [成功: {engine}] 搜索「{query}」的结果（{success_count}/{requested_count}）：\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        link = item.get("link", "")
        snippet = item.get("snippet", "")
        lines.append(f"{i}. 标题：{title}\n   摘要：{snippet}\n   链接：{link}\n")
    return "\n".join(lines)


async def execute_web_search(query: str, num_results: int = 5) -> str:
    """通过外部 MCP 搜索服务（bing-cn-mcp-server）执行网页搜索。

    对外的函数签名 / 返回文本格式保持不变，仅内部实现从
    Google CSE + DuckDuckGo 换成了 MCP 工具调用，调用方无需改动。
    """
    query = (query or "").strip()
    requested = min(max(int(num_results or 5), 1), 10)
    if not query:
        return "❌ 搜索关键词为空。"

    try:
        items = await _search_via_mcp(query, requested)
    except Exception as e:
        logger.warning(f"MCP 搜索失败: {e}")
        items = None

    if items:
        return _format_search_results(items, query, "Bing", requested=requested)

    return f"❌ 未找到与「{query}」相关的结果。"


# --------------------- fetch_url (增加重试循环) ---------------------
async def _extract_with_trafilatura(url: str) -> str | None:
    try:
        downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
        if downloaded:
            extracted = await asyncio.to_thread(
                trafilatura.extract,
                downloaded,
                output_format='txt',
                include_comments=False,
                include_tables=True,
            )
            if extracted:
                return re.sub(r'\s+', ' ', extracted).strip()
    except Exception as e:
        logger.error(f"trafilatura extract error for {url}: {e}")
    return None


async def _fetch_html_with_curl(url: str) -> str | None:
    try:
        async with AsyncSession() as session:
            response = await session.get(url, timeout=CURL_TIMEOUT, impersonate="chrome120",
                                         headers={"Accept": "text/html,application/xhtml+xml,*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            if response.status_code == 200:
                return response.text
            return None
    except Exception as e:
        logger.error(f"curl_cffi 请求异常: {e}, URL: {url}")
        return None


def _extract_text_from_html(html: str) -> str | None:
    if not html:
        return None
    try:
        extracted = trafilatura.extract(
            html,
            output_format='txt',
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            with_metadata=False,
        )
        if extracted:
            return re.sub(r'\s+', ' ', extracted).strip()
    except Exception:
        pass
    return None


# --------------------- SSRF 防护 ---------------------
_ALLOWED_FETCH_SCHEMES = {"http", "https"}


def _is_safe_url_to_fetch(url: str) -> tuple[bool, str]:
    """
    SSRF 防护：只允许 http/https 协议；拒绝解析到私网/回环/链路本地/保留 IP 的主机。
    返回 (是否安全, 失败原因)。
    """
    if not url or not isinstance(url, str):
        return False, "URL 为空"
    try:
        parts = urlsplit(url)
    except Exception as e:
        return False, f"URL 解析失败: {e}"
    if parts.scheme.lower() not in _ALLOWED_FETCH_SCHEMES:
        return False, f"不支持的协议: {parts.scheme}"
    host = parts.hostname or ""
    if not host:
        return False, "URL 缺少主机名"
    # 先处理 IPv6/IPv4 字面量与域名
    try:
        # getaddrinfo 同步可能阻塞，但仅查一次；为了安全可接受
        infos = socket.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f"DNS 解析失败: {e}"
    for family, _stype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False, f"目标地址 {ip} 属于禁止访问的范围（私网/回环/链路本地等）"
    return True, ""


async def execute_fetch_url(url: str, redirect_depth: int = 0, start_time: float = None) -> str:
    # SSRF 防护：先校验 URL
    ok, reason = _is_safe_url_to_fetch(url)
    if not ok:
        logger.warning(f"fetch_url 拒绝不安全 URL: {url} ({reason})")
        return f"失败：拒绝抓取不安全的 URL：{reason}"

    # 检查缓存
    cached = get_fetch_cache(url)
    if cached is not None:
        logger.debug(f"Fetch cache hit for {url}")
        return cached

    if start_time is None:
        start_time = asyncio.get_event_loop().time()
    # 总超时 30 秒
    if asyncio.get_event_loop().time() - start_time > 30:
        result = f"失败：抓取超时（总时间 >30s）：{url}"
        set_fetch_cache(url, result)
        return result

    if redirect_depth > 3:
        result = f"失败：重定向层次过深 (>{3})，已放弃：{url}"
        set_fetch_cache(url, result)
        return result

    original_url = url

    # ---- 重试循环：最多尝试2次 ----
    for attempt in range(2):
        try:
            # 先用 curl_cffi 获取 HTML
            html = await _fetch_html_with_curl(url)
            if not html:
                # 如果 curl 失败，尝试 trafilatura 直接提取
                try:
                    content = await _extract_with_trafilatura(url)
                    if content:
                        result = f"✅ [成功] 🏷️ {urlparse(url).netloc}\n🔗 {url}\n📄 内容：\n\n{_truncate(content)}"
                        set_fetch_cache(url, result)
                        return result
                except Exception:
                    pass
                # 第一次尝试失败，等待后重试
                if attempt == 0:
                    logger.warning(f"fetch_url attempt {attempt+1} failed for {url}, retrying...")
                    await asyncio.sleep(1)
                    continue
                else:
                    result = f"失败：无法获取页面内容：{url}"
                    set_fetch_cache(url, result)
                    return result

            # 获取标题（用于后续展示）
            title = _get_title_from_html(html)

            # 尝试用 trafilatura 提取正文
            content = _extract_text_from_html(html)
            if content and len(content) > 200:
                result = f"✅ [成功] 🏷️ {title}\n🔗 {url}\n📄 内容：\n\n{_truncate(content)}"
                set_fetch_cache(url, result)
                return result

            # ---- 检测 JavaScript 重定向 ----
            js_pattern = re.compile(r'window\.location\.href\s*=\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE)
            match = js_pattern.search(html)
            if match:
                new_url = urljoin(url, match.group(1))
                if new_url == url:
                    result = f"失败：页面重定向到自身，无法抓取：{url}"
                    set_fetch_cache(url, result)
                    return result
                logger.info(f"[fetch_url] 跟随 JS 跳转: {original_url} -> {new_url}")
                result = await execute_fetch_url(new_url, redirect_depth + 1, start_time)
                set_fetch_cache(url, result)
                return result

            # ---- 检测 Meta Refresh 重定向 ----
            meta_pattern = re.compile(r'<meta\s+http-equiv=["\']refresh["\']\s+content=["\']\d+;\s*url=([^"\']+)["\']', re.IGNORECASE)
            match = meta_pattern.search(html)
            if match:
                new_url = urljoin(url, match.group(1))
                if new_url == url:
                    result = f"失败：页面重定向到自身，无法抓取：{url}"
                    set_fetch_cache(url, result)
                    return result
                logger.info(f"[fetch_url] 跟随 Meta Refresh: {original_url} -> {new_url}")
                result = await execute_fetch_url(new_url, redirect_depth + 1, start_time)
                set_fetch_cache(url, result)
                return result

            # 未提取到有效正文
            result = f"失败：无法提取有效正文（标题：{title}）\n🔗 {url}"
            set_fetch_cache(url, result)
            return result

        except asyncio.TimeoutError:
            if attempt == 0:
                logger.warning(f"fetch_url timeout (attempt {attempt+1}) for {url}, retrying...")
                await asyncio.sleep(1)
                continue
            else:
                result = f"失败：抓取超时，请稍后重试：{url}"
                set_fetch_cache(url, result)
                return result
        except Exception as e:
            logger.error(f"fetch_url unexpected error (attempt {attempt+1}): {e}")
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            else:
                result = f"失败：抓取异常，请稍后重试：{url}"
                set_fetch_cache(url, result)
                return result

    # 如果循环结束仍未返回（理论上不会）
    result = f"失败：多次尝试均失败：{url}"
    set_fetch_cache(url, result)
    return result


# --------------------- wikipedia ---------------------
async def execute_wikipedia(query: str, lang: str = "zh") -> str:
    for l in [lang, "en"]:
        try:
            async with AsyncSession() as session:
                search_resp = await session.get(
                    f"https://{l}.wikipedia.org/w/api.php",
                    params={"action": "query", "list": "search", "srsearch": query, "srlimit": 3, "format": "json", "utf8": 1},
                    headers={"Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                    impersonate="chrome120", timeout=CURL_TIMEOUT
                )
                if search_resp.status_code != 200:
                    continue
                search_data = search_resp.json()
                results = search_data.get("query", {}).get("search", [])
                if not results:
                    continue
                page_id = results[0]["pageid"]
                page_resp = await session.get(
                    f"https://{l}.wikipedia.org/w/api.php",
                    params={"action": "query", "pageids": page_id, "prop": "extracts|info", "explaintext": True, "inprop": "url", "format": "json", "utf8": 1},
                    headers={"Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                    impersonate="chrome120", timeout=CURL_TIMEOUT
                )
                if page_resp.status_code != 200:
                    continue
                page_data = page_resp.json()
                pages = page_data.get("query", {}).get("pages", {})
                page = next(iter(pages.values()))
                title = page.get("title", results[0].get("title", query))
                extract = page.get("extract", "").strip()
                if not extract:
                    continue
                extract = _truncate(extract)
                page_url = page.get("fullurl", f"https://{l}.wikipedia.org/wiki/{quote(title)}")
                return f"<b>Wikipedia — {title}</b><br/><br/>{extract}<br/><br/>链接：{page_url}"
        except Exception:
            continue
    return f"失败：Wikipedia 查询「{query}」未找到结果。"


# --------------------- exchange_rate ---------------------
async def execute_exchange_rate(base: str, target: str = None) -> str:
    base = base.upper().strip()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://open.er-api.com/v6/latest/{base}", timeout=HTTP_TIMEOUT_SHORT) as resp:
                if resp.status != 200:
                    return f"失败：汇率查询失败（HTTP {resp.status}）"
                data = await resp.json()
        if data.get("result") != "success":
            return f"失败：汇率查询失败：{data.get('error-type', '未知错误')}"
        rates = data.get("rates", {})
        update_time = data.get("time_last_update_utc", "未知")
        if target:
            target = target.upper().strip()
            if target not in rates:
                return f"失败：不支持的目标货币代码：{target}"
            return f"<b>汇率查询成功</b><br/>1 {base} = {rates[target]} {target}<br/>更新时间：{update_time}"
        major = ["CNY", "USD", "EUR", "JPY", "GBP", "HKD", "KRW", "SGD", "AUD", "CAD"]
        lines = [f"<b>{base} 汇率</b><br/>更新时间：{update_time}<br/>"]
        for cur in major:
            if cur in rates and cur != base:
                lines.append(f"1 {base} = {rates[cur]:.4f} {cur}")
        return "<br/>".join(lines)
    except Exception as e:
        return f"失败：汇率查询出错：{str(e)[:100]}"


# --------------------- book_lookup ---------------------
async def execute_book_lookup(query: str) -> str:
    headers = {"User-Agent": "TelegramAIAssistant/1.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://openlibrary.org/search.json", params={"q": query, "limit": 5, "fields": "*"}, headers=headers, timeout=HTTP_TIMEOUT_SHORT) as resp:
                if resp.status != 200:
                    return f"失败：书籍查询失败（HTTP {resp.status}）"
                data = await resp.json()
        docs = data.get("docs", [])
        if not docs:
            return f"失败：未找到与「{query}」相关的书籍"
        lines = [f"<b>书籍查询结果：「{query}」</b><br/>"]
        for i, doc in enumerate(docs[:5], 1):
            title = doc.get("title", "无标题")
            authors = "、".join(doc.get("author_name", ["未知作者"])[:3])
            year = doc.get("first_publish_year", "未知")
            subjects = "、".join(doc.get("subject", [])[:3])
            key = doc.get("key", "")
            ol_url = f"https://openlibrary.org{key}" if key else ""
            lines.append(f"{i}. 《{title}》<br/>   作者：{authors}<br/>   首次出版：{year} 年<br/>" + (f"   主题：{subjects}<br/>" if subjects else "") + (f"   详情：{ol_url}<br/>" if ol_url else ""))
        return "<br/>".join(lines)
    except Exception as e:
        return f"失败：书籍查询出错：{str(e)[:100]}"


# --------------------- weather ---------------------
async def execute_weather(city: str, unit: str = "c", hours: int = 6) -> str:
    url = f"https://wttr.in/{city}?format=j1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=HTTP_TIMEOUT_SHORT) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return json.dumps({"error": f"无法解析天气数据：{text[:200]}"}, ensure_ascii=False)

                if resp.status != 200:
                    error_msg = data.get("error", {}).get("message", text)
                    return json.dumps({"error": f"天气查询失败（HTTP {resp.status}）：{error_msg[:200]}"}, ensure_ascii=False)

                current = data.get("current_condition", [{}])[0]
                current_data = {
                    "temp": current.get(f"temp_{unit.upper()}", "N/A"),
                    "feels_like": current.get(f"FeelsLike{unit.upper()}", "N/A"),
                    "humidity": current.get("humidity", "N/A"),
                    "wind": current.get("windspeedKmph", "N/A"),
                    "wind_gust": current.get("windgustKmph", "N/A"),
                    "pressure": current.get("pressure", "N/A"),
                    "visibility": current.get("visibility", "N/A"),
                    "cloudcover": current.get("cloudcover", "N/A"),
                    "uvIndex": current.get("uvIndex", "N/A"),
                    "precip": current.get("precipMM", "0.0"),
                    "wind_dir": current.get("winddir16Point", "N/A"),
                    "wind_deg": current.get("winddirDegree", "N/A"),
                    "condition": current.get("weatherDesc", [{}])[0].get("value", "未知"),
                    "weather_code": current.get("weatherCode", "N/A"),
                    "obs_time": current.get("localObsDateTime") or current.get("observation_time", ""),
                }

                first_day = data.get("weather", [{}])[0]
                hourly_list = first_day.get("hourly", [])
                hourly_data = []
                for h in hourly_list[:24]:
                    time_str = h.get("time", "0")
                    time_label = "00:00"
                    try:
                        val = int(time_str)
                    except (ValueError, TypeError):
                        val = None
                    if val is not None:
                        if 0 <= val <= 23:
                            time_label = f"{val:02d}:00"
                        elif 100 <= val <= 2359:
                            hours_val = val // 100
                            if 0 <= hours_val <= 23:
                                time_label = f"{hours_val:02d}:00"
                            else:
                                hrs = val // 60
                                mins = val % 60
                                if 0 <= hrs <= 23:
                                    time_label = f"{hrs:02d}:{mins:02d}"
                                else:
                                    time_label = str(val)
                        else:
                            hrs = val // 60
                            mins = val % 60
                            if 0 <= hrs <= 23:
                                time_label = f"{hrs:02d}:{mins:02d}"
                            else:
                                time_label = str(val)
                    else:
                        if ":" in str(time_str):
                            time_label = time_str
                        else:
                            time_label = time_str

                    temp_key = f"temp{unit.upper()}"
                    hourly_data.append({
                        "time": time_label,
                        "temp": h.get(temp_key, "N/A"),
                        "condition": h.get("weatherDesc", [{}])[0].get("value", ""),
                        "precip": h.get("precipMM", "0"),
                        "humidity": h.get("humidity", "N/A"),
                        "pressure": h.get("pressure", "N/A"),
                        "wind_gust": h.get("WindGustKmph", "N/A"),
                        "uvIndex": h.get("uvIndex", "N/A"),
                        "cloudcover": h.get("cloudcover", "N/A"),
                        "visibility": h.get("visibility", "N/A"),
                        "wind_speed": h.get("windspeedKmph", "N/A"),
                        "wind_dir": h.get("winddir16Point", "N/A"),
                        "chance_of_rain": h.get("chanceofrain", "0"),
                        "chance_of_snow": h.get("chanceofsnow", "0"),
                        "chance_of_thunder": h.get("chanceofthunder", "0"),
                        "chance_of_fog": h.get("chanceoffog", "0"),
                        "chance_of_frost": h.get("chanceoffrost", "0"),
                        "chance_of_overcast": h.get("chanceofovercast", "0"),
                        "chance_of_sunshine": h.get("chanceofsunshine", "0"),
                        "chance_of_windy": h.get("chanceofwindy", "0"),
                        "chance_of_hightemp": h.get("chanceofhightemp", "0"),
                        "chance_of_remdry": h.get("chanceofremdry", "0"),
                        "DewPointC": h.get("DewPointC", "N/A"),
                        "HeatIndexC": h.get("HeatIndexC", "N/A"),
                        "WindChillC": h.get("WindChillC", "N/A"),
                        "shortRad": h.get("shortRad", "0"),
                        "diffRad": h.get("diffRad", "0"),
                    })
                daily_list = data.get("weather", [])
                daily_data = []
                for day in daily_list[:5]:
                    astro = day.get("astronomy", [{}])[0] if day.get("astronomy") else {}
                    first_hour = day.get("hourly", [{}])[0] if day.get("hourly") else {}
                    daily_data.append({
                        "date": day.get("date", ""),
                        "max": day.get(f"maxtemp{unit.upper()}", "N/A"),
                        "min": day.get(f"mintemp{unit.upper()}", "N/A"),
                        "avg": day.get(f"avgtemp{unit.upper()}", "N/A"),
                        "condition": first_hour.get("weatherDesc", [{}])[0].get("value", ""),
                        "uvIndex": day.get("uvIndex", "N/A"),
                        "sunrise": astro.get("sunrise", ""),
                        "sunset": astro.get("sunset", ""),
                        "moonrise": astro.get("moonrise", ""),
                        "moonset": astro.get("moonset", ""),
                        "moon_phase": astro.get("moon_phase", ""),
                        "moon_illumination": astro.get("moon_illumination", "0"),
                        "chance_of_rain": first_hour.get("chanceofrain", "0"),
                        "chance_of_snow": first_hour.get("chanceofsnow", "0"),
                        "chance_of_thunder": first_hour.get("chanceofthunder", "0"),
                        "chance_of_fog": first_hour.get("chanceoffog", "0"),
                        "chance_of_frost": first_hour.get("chanceoffrost", "0"),
                    })

                result = {
                    "city": city,
                    "unit": unit.upper(),
                    "current": current_data,
                    "hourly": hourly_data,
                    "daily": daily_data,
                }
                return json.dumps(result, ensure_ascii=False)
    except asyncio.TimeoutError:
        return json.dumps({"error": "天气查询超时"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"天气查询异常：{str(e)[:100]}"}, ensure_ascii=False)


# --------------------- news ---------------------
NEWS_FEEDS = {
    "bbc": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    "reuters": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    "cna": "https://www.cna.com.tw/rss/cna/rnews.xml",
    "cnn": "http://rss.cnn.com/rss/edition.rss",
    "nytimes": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "guardian": "https://www.theguardian.com/world/rss",
    "zaobao": "https://www.zaobao.com.sg/rss.xml",
    "xinhua": "http://www.xinhuanet.com/english/rss/world.xml",
}

async def execute_news(source: str = "bbc", limit: int = 5) -> str:
    limit = min(max(limit, 1), 10)
    source_key = source.lower()
    if source_key == "all":
        all_items = []
        for src, url in NEWS_FEEDS.items():
            try:
                feed = await asyncio.to_thread(feedparser.parse, url)
                if not feed.bozo:
                    for item in feed.entries[:min(2, limit)]:
                        all_items.append((src, item.title, item.link))
            except Exception:
                continue
        if not all_items:
            return "失败：无法获取任何新闻源。"
        lines = ["<ul>"]
        for src, title, link in all_items[:limit*2]:
            lines.append(f'<li><b>{html.escape(title)}</b> (<i>{src.upper()}</i>) <a href="{html.escape(link)}">🔗 阅读原文</a></li>')
        lines.append("</ul>")
        return "\n".join(lines)
    url = NEWS_FEEDS.get(source_key)
    if not url:
        return f"失败：不支持的新闻源：{source}。可用：{', '.join(NEWS_FEEDS.keys())} 或 all。"
    try:
        feed = await asyncio.to_thread(feedparser.parse, url)
        if feed.bozo:
            return f"失败：解析新闻源 {source} 失败。"
        items = feed.entries[:limit]
        if not items:
            return f"失败：未找到 {source} 的新闻。"
        lines = ["<ul>"]
        for item in items:
            lines.append(f'<li><b>{html.escape(item.title)}</b> <a href="{html.escape(item.link)}">🔗 阅读原文</a></li>')
        lines.append("</ul>")
        return "\n".join(lines)
    except Exception as e:
        return f"失败：新闻获取失败：{str(e)[:100]}"


# --------------------- crypto_price ---------------------
COIN_MAP = {
    "btc": "bitcoin", "eth": "ethereum", "doge": "dogecoin",
    "sol": "solana", "xrp": "ripple", "ada": "cardano",
    "dot": "polkadot", "ltc": "litecoin", "bch": "bitcoin-cash",
    "matic": "matic-network", "avax": "avalanche-2", "uni": "uniswap"
}

async def execute_crypto_price(coin: str, currency: str = "usd") -> str:
    coin_id = COIN_MAP.get(coin.lower(), coin.lower())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies={currency}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=HTTP_TIMEOUT_SHORT) as resp:
                if resp.status != 200:
                    return f"失败：无法获取 {coin} 的价格（HTTP {resp.status}）。"
                data = await resp.json()
    except Exception as e:
        return f"失败：价格查询失败：{str(e)[:100]}"
    if coin_id not in data:
        return f"失败：未找到加密货币：{coin}。支持：{', '.join(COIN_MAP.keys())}"
    price = data[coin_id].get(currency)
    if price is None:
        return f"失败：不支持的目标货币：{currency}"
    return f"<b>{coin.upper()} 当前价格</b><br/>{price} {currency.upper()}"


# --------------------- ip_geo ---------------------
async def execute_ip_geo(ip: str = None) -> str:
    """IP 地理位置：委托给 amap-maps MCP 的 maps_ip_location 工具。

    未配置 GAODE_MCP_TOKEN 时 mcp_client.py 不会注册 amap-maps 服务，
    调用会以 MCPToolError 形式抛回，这里转成 JSON 错误信息给上层。
    """
    args: dict[str, Any] = {}
    if ip:
        args["ip"] = ip
    try:
        raw = await call_mcp_tool("amap-maps", "maps_ip_location", args)
    except MCPToolError as e:
        return json.dumps(
            {"status": "error", "message": f"amap-maps MCP 调用失败：{e}"},
            ensure_ascii=False,
        )
    return raw if raw else json.dumps(
        {"status": "error", "message": "amap-maps 返回空"},
        ensure_ascii=False,
    )


# --------------------- qr_code ---------------------
async def execute_qr_code(text: str) -> str:
    if not text:
        return "失败：请提供要编码的文本或 URL。"
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    key = f"qr/{hashlib.md5(text.encode()).hexdigest()}.png"
    url = await upload_bytes_to_r2(img_bytes, key, "image/png")
    if url:
        return f"✅ 二维码生成成功\n内容：{html.escape(text[:200])}\n图片链接：{url}"
    else:
        return "失败：R2 上传失败，请检查配置。"


# --------------------- done ---------------------
async def execute_done() -> str:
    return "Tool round completed."


# --------------------- image API helpers ---------------------
def _extract_modelscope_error_detail(body_text: str) -> tuple[str, str]:
    detail = body_text[:500] if body_text else ""
    request_id = ""
    cleaned = body_text.strip() if body_text else ""
    if not cleaned:
        return detail, request_id
    if " - {" in cleaned:
        cleaned = cleaned.split(" - ", 1)[1].strip()
    if cleaned.startswith("{") or cleaned.startswith("["):
        try:
            payload = json.loads(cleaned)
        except Exception:
            try:
                import ast
                payload = ast.literal_eval(cleaned)
            except Exception:
                payload = None
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or err.get("detail") or payload.get("message") or detail
                request_id = err.get("request_id") or payload.get("request_id") or ""
            elif isinstance(err, str):
                detail = err
                request_id = payload.get("request_id") or ""
            elif payload.get("message"):
                detail = str(payload.get("message"))
                request_id = payload.get("request_id") or ""
    return detail[:500], request_id


def _looks_like_image_payload(value: str) -> bool:
    """检查字符串是否可能是图片 URL 或 base64 data URL"""
    if not value:
        return False
    value = value.strip()
    return (value.startswith('http://') or
            value.startswith('https://') or
            value.startswith('data:image/'))

async def _download_image_bytes(session: aiohttp.ClientSession, image_url: str) -> bytes | None:
    if not image_url:
        return None
    if image_url.startswith("data:image"):
        try:
            _, b64 = image_url.split(",", 1)
            return base64.b64decode(b64)
        except Exception:
            return None
    try:
        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30), headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        return None
    return None


def _extract_image_items(response_json: dict) -> list[dict]:
    if not isinstance(response_json, dict):
        return []

    items = []
    seen = set()

    def add_url(url):
        if url and url not in seen:
            seen.add(url)
            items.append({"image_url": {"url": url}})

    def add_b64(b64):
        if b64 and b64 not in seen:
            seen.add(b64)
            items.append({"b64_json": b64})

    # 顶层直接字段
    for key in ('data', 'choices', 'output', 'results', 'images', 'output_images', 'outputs'):
        val = response_json.get(key)
        if not val:
            continue
        if isinstance(val, list):
            for elem in val:
                if isinstance(elem, str):
                    add_url(elem)
                elif isinstance(elem, dict):
                    url = elem.get('url') or elem.get('image_url')
                    if url:
                        add_url(url)
                    b64 = elem.get('b64_json') or elem.get('base64')
                    if b64:
                        add_b64(b64)
                    if isinstance(elem.get('image_url'), dict):
                        add_url(elem['image_url'].get('url'))
        elif isinstance(val, dict):
            url = val.get('url') or val.get('image_url')
            if url:
                add_url(url)
            b64 = val.get('b64_json') or val.get('base64')
            if b64:
                add_b64(b64)
            if isinstance(val.get('image_url'), dict):
                add_url(val['image_url'].get('url'))
        elif isinstance(val, str):
            add_url(val)

    # choices[0].message.images/content（OpenAI 格式）
    choices = response_json.get('choices')
    if isinstance(choices, list) and choices:
        msg = choices[0].get('message')
        if isinstance(msg, dict):
            images = msg.get('images')
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict):
                        add_url(img.get('image_url', {}).get('url'))
                        add_b64(img.get('b64_json'))
                    elif isinstance(img, str):
                        add_url(img)
            content = msg.get('content')
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'image_url':
                        add_url(part.get('image_url', {}).get('url'))

    # 顶层 b64_json / url
    add_b64(response_json.get('b64_json'))
    add_url(response_json.get('url'))
    if isinstance(response_json.get('image_url'), dict):
        add_url(response_json['image_url'].get('url'))
    elif isinstance(response_json.get('image_url'), str):
        add_url(response_json['image_url'])

    return items

async def _images_response_to_bytes(data: dict) -> list[bytes]:
    image_bytes_list: list[bytes] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        for item in _extract_image_items(data):   # ← 修正函数名
            img_url = ""
            if isinstance(item.get("image_url"), dict):
                img_url = str(item["image_url"].get("url") or "").strip()
            img_url = img_url or str(item.get("url") or "").strip()
            b64_json = str(item.get("b64_json") or "").strip()
            if b64_json:
                try:
                    image_bytes_list.append(base64.b64decode(b64_json))
                    continue
                except Exception:
                    pass
            if img_url:
                img_bytes = await _download_image_bytes(session, img_url)
                if img_bytes:
                    image_bytes_list.append(img_bytes)
    return image_bytes_list


def _format_image_api_error(api_name: str, status_code: int, detail: str = "", request_id: str = "", endpoint: str = "", model: str = "") -> str:
    parts = [f"❌ {html.escape(api_name)} 请求失败"]
    if status_code:
        parts.append(f"HTTP 状态：{status_code}")
    if model:
        parts.append(f"模型：{html.escape(model)}")
    if request_id:
        parts.append(f"Request ID：{html.escape(request_id)}")
    if detail:
        clean = detail.strip().replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in clean.split("\n") if line.strip()]
        clean = "<br/>".join(html.escape(line) for line in lines)
        if len(clean) > 800:
            clean = clean[:800] + "…"
        parts.append(f"详情：{clean}")
    return "<br/>".join(parts)


async def execute_generate_image(
    prompt: str,
    model: str,   # 移除默认值，让模型必须传
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
    num_images: int = 1,
    image_url: Optional[str] = None,
) -> str:
    from apitelegramchat.ai_handlers import _request_modelscope_native_image
    MODEL_ALIAS_MAP = {
        "flux-schnell": "black-forest-labs/flux-schnell",
        "flux-1.1-pro": "black-forest-labs/flux-1.1-pro",
        "flux-pro": "black-forest-labs/flux-pro",
        "sd-3.5": "stabilityai/stable-diffusion-3.5-large",
    }
    if model in MODEL_ALIAS_MAP:
        model = MODEL_ALIAS_MAP[model]

    model_info = SUPPORTED_MODELS.get(model)
    provider = model_info.provider if model_info else "openrouter"
    num_images = min(max(num_images, 1), 4)

    # ModelScope：走专门的 /v1/images/generations，避免误打到 chat/completions
    if provider == "modelscope":
        response_json, endpoint, error_detail, status_code, request_id = await _request_modelscope_native_image(
            prompt=prompt,
            image_urls=[image_url] if image_url else [],
            num_images=num_images,
            builder=None,
            model=model,
        )
        if response_json is None:
            return _format_image_api_error(
                api_name="ModelScope 图像接口",
                status_code=status_code,
                detail=error_detail,
                request_id=request_id,
                endpoint=endpoint,
                model=model,
            )
        try:
            image_bytes_list = await _images_response_to_bytes(response_json)
            if not image_bytes_list:
                return _format_image_api_error(
                    api_name="ModelScope 图像接口",
                    status_code=200,
                    detail="接口返回成功，但未找到可下载的图片数据。",
                    endpoint="/v1/images/generations",
                    model=model,
                )
            uploaded_urls = []
            for idx, img_bytes in enumerate(image_bytes_list):
                key = f"generated/{uuid.uuid4().hex}_{idx}.png"
                img_url = await upload_bytes_to_r2(img_bytes, key, "image/png")
                if img_url:
                    uploaded_urls.append(img_url)
            if not uploaded_urls:
                return _format_image_api_error(
                    api_name="ModelScope 图像接口",
                    status_code=200,
                    detail="图片已生成，但上传 R2 全部失败。",
                    endpoint="/v1/images/generations",
                    model=model,
                )
            links = "\n".join(uploaded_urls)
            count = len(uploaded_urls)
            if count == len(image_bytes_list):
                return f"✅ 已生成 {count} 张图片。\n图片链接：\n{links}"
            return f"✅ 已生成 {count} 张图片（部分图片上传失败）。\n图片链接：\n{links}"
        except Exception as e:
            logger.exception(f"ModelScope generate_image 异常: {e}")
            return _format_image_api_error(
                api_name="ModelScope 图像接口",
                status_code=getattr(e, "status", getattr(e, "status_code", 500)),
                detail=str(e),
                endpoint="/v1/images/generations",
                model=model,
            )

    # 其他厂商：保留原有 OpenRouter 兼容逻辑
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    if image_url:
        content_part = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]
    else:
        content_part = prompt

    payload = {
        "model": model,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": content_part}],
        "image_config": {
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
        },
        "n": num_images,
        "provider": OPENROUTER_PROVIDER_PREFERENCES,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    if "not a valid model ID" in err_text and model != "google/gemini-2.5-flash-image":
                        logger.warning(f"模型 {model} 无效，回退到默认模型 google/gemini-2.5-flash-image")
                        return await execute_generate_image(
                            prompt=prompt,
                            model="google/gemini-2.5-flash-image",
                            aspect_ratio=aspect_ratio,
                            image_size=image_size,
                            num_images=num_images,
                            image_url=image_url,
                        )
                    return f"❌ 图像生成失败 (HTTP {resp.status}): {err_text[:200]}"

                data = await resp.json()
                msg = data.get("choices", [{}])[0].get("message", {})
                images = msg.get("images", [])

                if not images:
                    content = msg.get("content", "")
                    urls = re.findall(r'https?://[^\s]+\.(?:png|jpg|jpeg|gif)', content)
                    if urls:
                        images = [{"image_url": {"url": u}} for u in urls]

                if not images:
                    return "⚠️ 生成的响应中未找到图片。"

                image_bytes_list = []
                download_errors = []
                for idx, img_data in enumerate(images):
                    img_url = img_data.get("image_url", {}).get("url")
                    if not img_url:
                        continue
                    if img_url.startswith("data:image"):
                        try:
                            header, base64_data = img_url.split(",", 1)
                            img_bytes = base64.b64decode(base64_data)
                            image_bytes_list.append(img_bytes)
                            continue
                        except Exception as e:
                            logger.error(f"Base64 解码失败: {e}")
                            download_errors.append(f"图片 {idx+1} (Base64 解码失败)")
                            continue
                    elif img_url.startswith("http"):
                        max_retries = 3
                        downloaded = False
                        for attempt in range(max_retries):
                            try:
                                async with session.get(
                                    img_url,
                                    timeout=aiohttp.ClientTimeout(total=30),
                                    headers={"User-Agent": "Mozilla/5.0"}
                                ) as img_resp:
                                    if img_resp.status == 200:
                                        img_bytes = await img_resp.read()
                                        image_bytes_list.append(img_bytes)
                                        downloaded = True
                                        break
                            except Exception as e:
                                logger.warning(f"下载图片 {img_url} 异常: {e}")
                            await asyncio.sleep(1 + attempt)
                        if not downloaded:
                            download_errors.append(f"图片 {idx+1}")
                    else:
                        download_errors.append(f"图片 {idx+1} (不支持的 URL 格式)")

                if not image_bytes_list:
                    return f"⚠️ 图片生成成功，但下载全部失败。失败项: {', '.join(download_errors)}"

                uploaded_urls = []
                for idx, img_bytes in enumerate(image_bytes_list):
                    key = f"generated/{uuid.uuid4().hex}_{idx}.png"
                    url = await upload_bytes_to_r2(img_bytes, key, "image/png")
                    if url:
                        uploaded_urls.append(url)
                    else:
                        logger.warning("一张图片上传 R2 失败")

                if not uploaded_urls:
                    return "❌ 图片生成成功，但 R2 上传全部失败，请稍后重试。"

                links = "\n".join(uploaded_urls)
                count = len(uploaded_urls)
                if count == len(image_bytes_list):
                    return f"✅ 已生成 {count} 张图片。\n图片链接：\n{links}"
                else:
                    return f"✅ 已生成 {count} 张图片（部分图片上传失败）。\n图片链接：\n{links}"

    except Exception as e:
        logger.error(f"execute_generate_image 异常: {e}", exc_info=True)
        return f"❌ 图像生成异常: {str(e)[:150]}"


# ========== 视频生成（工具版本） ==========
async def execute_generate_video(
    prompt: str,
    model: str,
    duration: int = 5,
    chat_id: Optional[int] = None,
) -> str:
    """
    视频生成工具：复用 ai_handlers 中已有的 _request_agnes_video / _request_openrouter_video
    轮询逻辑，下载视频字节并上传 R2（拿到稳定的 HTTPS URL + 正确的 video/mp4 MIME）。

    与 _agentic_loop_native_video 的区别：
    - 这条路径是由 LLM 在任意对话模型下主动调用工具触发的；
    - 视频不会单独 sendRichMessage 发出，而是把 R2 URL 以结构化文本返回给上层，
      由 format_tool_result 在工具结果卡片里以 <figure><video> 内嵌渲染
      （Telegram Rich Message 支持视频作为独立 block 与文本同消息共存，
      参见 Rich Message Formatting Options 文档）。

    返回格式（供 format_tool_result 解析）：
        ✅ 已生成视频。
        视频链接：https://...
    """
    # 局部导入避免与 ai_handlers 产生循环依赖
    from apitelegramchat.ai_handlers import (
        _request_agnes_video,
        _request_openrouter_video,
    )

    if not prompt or not prompt.strip():
        return "❌ 视频生成失败：未提供提示词。"
    if not model:
        return "❌ 视频生成失败：未指定模型。"

    # 时长范围约束（与 _agentic_loop_native_video 保持一致）
    try:
        duration = int(duration)
    except Exception:
        duration = 5
    duration = max(3, min(duration, 30))

    model_info = SUPPORTED_MODELS.get(model)
    if not model_info:
        return f"❌ 未知视频模型：{model}"
    if not model_info.native_video:
        return f"❌ 模型 {model} 不支持视频生成。"

    provider = model_info.provider
    video_url: Optional[str] = None
    error: Optional[str] = None
    video_meta: Optional[dict] = None

    if provider == "agnes":
        video_url, error, video_meta = await _request_agnes_video(
            prompt=prompt, duration=duration, model=model, builder=None,
        )
    elif provider == "openrouter":
        video_url, error, video_meta = await _request_openrouter_video(
            prompt=prompt, duration=duration, model=model, builder=None,
        )
    else:
        return f"❌ 暂不支持的视频提供商：{provider}"

    if error:
        return f"❌ 视频生成失败：{error}"
    if not video_url:
        return "❌ 视频生成失败：未获取到视频链接。"

    # 下载并上传 R2，确保 Telegram 拿到合法 video/mp4 MIME 的稳定 HTTPS URL
    # （Rich Message 媒体 block 仅支持 HTTP/HTTPS URL）
    final_video_url = video_url
    video_bytes_len = 0
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as dl_session:
            async with dl_session.get(video_url) as dl_resp:
                if dl_resp.status == 200:
                    video_bytes = await dl_resp.read()
                    video_bytes_len = len(video_bytes)
                    r2_key = f"generated/{uuid.uuid4().hex}.mp4"
                    r2_url = await upload_bytes_to_r2(video_bytes, r2_key, "video/mp4")
                    if r2_url:
                        final_video_url = r2_url
                    else:
                        logger.warning("[generate_video] R2 上传失败，回退原始 URL")
                else:
                    logger.warning(
                        "[generate_video] 视频下载非 200: status=%s url=%s",
                        dl_resp.status, str(video_url)[:200],
                    )
    except Exception:
        logger.exception("[generate_video] 视频下载/上传异常，回退原始 URL: %s", str(video_url)[:200])

    if video_bytes_len == 0 and isinstance(video_meta, dict):
        out_size = video_meta.get("perf_output_size")
        if isinstance(out_size, (int, float)):
            video_bytes_len = int(out_size)

    # 结构化返回：format_tool_result 解析“视频链接：”那一行构造内嵌 <figure><video>。
    # 不附带元数据 caption —— 工具结果卡片只展示视频本体，与图片工具行为对称。
    return (
        f"✅ 已生成视频。\n"
        f"视频链接：{final_video_url}"
    )



# ===================== 地图工具实现（amap-maps MCP 委托） =====================
# ---------------------------------------------------------------------------
# 内部：调用 amap-maps MCP 工具的统一封装
# ---------------------------------------------------------------------------
async def _call_amap_mcp(tool_name: str, arguments: dict[str, Any]) -> str:
    """调用 amap-maps MCP 服务的某个工具，返回 MCP 输出的纯文本。

    出错时返回 JSON 错误信息（{"status": "error", "message": ...}），让上层
    工具的调用方（LLM / format_tool_result）能直接看到失败原因。
    """
    try:
        raw = await call_mcp_tool("amap-maps", tool_name, arguments)
    except MCPToolError as e:
        return json.dumps(
            {"status": "error", "message": f"amap-maps MCP 调用失败（{tool_name}）：{e}"},
            ensure_ascii=False,
        )
    if not raw:
        return json.dumps(
            {"status": "error", "message": f"amap-maps MCP 返回空（{tool_name}）"},
            ensure_ascii=False,
        )
    return raw


def _empty_mcp_error(tool_name: str) -> str:
    return json.dumps(
        {"status": "error", "message": f"amap-maps MCP 返回空（{tool_name}）"},
        ensure_ascii=False,
    )


def _amap_error(message: str, *, code: str = "invalid_request") -> str:
    """返回统一、可被 MCP 调用方解析的地图工具错误。"""
    return json.dumps({"status": "error", "code": code, "message": message}, ensure_ascii=False)


def _is_unknown_mcp_tool_error(error: Exception) -> bool:
    """仅在服务端明确提示工具不存在时启用别名回退，避免吞掉真实业务错误。"""
    text = str(error).lower()
    markers = ("unknown tool", "tool not found", "method not found", "不存在", "未找到工具")
    return any(marker in text for marker in markers)


async def _call_amap_mcp_candidates(tool_names: list[str], arguments: dict[str, Any]) -> str:
    """按优先级调用高德 MCP 工具，并仅对未知工具名启用兼容别名。"""
    if not tool_names:
        return _amap_error("未配置高德 MCP 工具名", code="configuration_error")
    last_error: Exception | None = None
    for index, tool_name in enumerate(tool_names):
        try:
            raw = await call_mcp_tool("amap-maps", tool_name, arguments)
            if raw:
                return raw
            return _empty_mcp_error(tool_name)
        except MCPToolError as exc:
            last_error = exc
            if index < len(tool_names) - 1 and _is_unknown_mcp_tool_error(exc):
                logger.info("高德 MCP 工具 %s 不可用，尝试兼容别名", tool_name)
                continue
            return _amap_error(f"amap-maps MCP 调用失败（{tool_name}）：{exc}", code="upstream_error")
    return _amap_error(f"amap-maps MCP 调用失败：{last_error}", code="upstream_error")


def _normalize_amap_coordinate(value: Any, field_name: str) -> str:
    """校验并规范化高德坐标为 ``经度,纬度``（WGS/GCJ 坐标系由上游约定）。"""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        raw_lng, raw_lat = value
    elif isinstance(value, str):
        parts = [item.strip() for item in value.split(",")]
        if len(parts) != 2:
            raise ValueError(f"{field_name} 必须是“经度,纬度”格式，例如 116.397128,39.916527")
        raw_lng, raw_lat = parts
    else:
        raise ValueError(f"{field_name} 必须是“经度,纬度”字符串")
    try:
        lng = float(raw_lng)
        lat = float(raw_lat)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须包含合法数字经纬度") from exc
    if not (-180 <= lng <= 180):
        raise ValueError(f"{field_name} 的经度必须在 -180 到 180 之间")
    if not (-90 <= lat <= 90):
        raise ValueError(f"{field_name} 的纬度必须在 -90 到 90 之间")
    return f"{lng:.6f},{lat:.6f}"


# ---------------------------------------------------------------------------
# 内部辅助：通过 amap-maps MCP 把地址转坐标
# ---------------------------------------------------------------------------
async def _geocode_coords(address: str) -> tuple[float, float, str] | None:
    """通过 amap-maps MCP 的 maps_geo 工具将地址转为坐标。

    返回 (lat, lon, display_name)；调用失败或解析失败时返回 None。
    适配 amap-maps MCP 的常见返回 schema：
        {"location": "lng,lat", "formatted_address": "...", ...}
        或
        {"lat": ..., "lon": ..., "formatted_address": "..."}
    """
    if not address or not address.strip():
        return None
    try:
        raw = await call_mcp_tool("amap-maps", "maps_geo", {"address": address.strip()})
    except MCPToolError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    # 优先取 amap 标准的 "location": "lng,lat" 字段
    location = data.get("location") or data.get("pos") or data.get("coord")
    if isinstance(location, str) and "," in location:
        parts = [p.strip() for p in location.split(",")]
        if len(parts) >= 2:
            try:
                lng = float(parts[0])
                lat = float(parts[1])
                name = data.get("formatted_address") or data.get("address") or address
                return lat, lng, name
            except ValueError:
                pass

    # 回退：直接读 lat/lon 字段
    lat = data.get("lat") or data.get("latitude")
    lon = data.get("lon") or data.get("lng") or data.get("longitude")
    if lat is not None and lon is not None:
        try:
            return (
                float(lat),
                float(lon),
                data.get("formatted_address") or data.get("address") or address,
            )
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# 1. 地理编码
# ---------------------------------------------------------------------------
async def execute_geocode(address: str) -> str:
    """地理编码：地址 -> 经纬度。委托给 amap-maps MCP 的 maps_geo 工具。"""
    if not address or not address.strip():
        return json.dumps({"status": "error", "message": "地址为空"}, ensure_ascii=False)
    return await _call_amap_mcp("maps_geo", {"address": address.strip()})


# ---------------------------------------------------------------------------
# 2. POI 关键词、周边与详情搜索
# ---------------------------------------------------------------------------
async def execute_keyword_search(keywords: str, city: str | None = None) -> str:
    """按关键词搜索 POI；可用 city 将查询限定在指定城市。"""
    keyword_text = str(keywords or "").strip()
    if not keyword_text:
        return _amap_error("keywords 不能为空")
    arguments: dict[str, Any] = {"keywords": keyword_text}
    if city is not None:
        city_text = str(city).strip()
        if not city_text:
            return _amap_error("city 如提供则不能为空")
        arguments["city"] = city_text
    return await _call_amap_mcp("maps_text_search", arguments)


async def execute_nearby_search(
    keywords: str,
    location: str,
    radius: int | None = None,
) -> str:
    """在中心点周边检索 POI；location 必须为 ``经度,纬度``。"""
    keyword_text = str(keywords or "").strip()
    if not keyword_text:
        return _amap_error("keywords 不能为空")
    try:
        normalized_location = _normalize_amap_coordinate(location, "location")
    except ValueError as exc:
        return _amap_error(str(exc))
    if radius is None:
        radius_value = 1000
    else:
        if isinstance(radius, bool):
            return _amap_error("radius 必须是 1 到 50000 之间的整数（米）")
        try:
            radius_value = int(radius)
        except (TypeError, ValueError):
            return _amap_error("radius 必须是 1 到 50000 之间的整数（米）")
        if radius_value < 1 or radius_value > 50000:
            return _amap_error("radius 必须在 1 到 50000 米之间")
    return await _call_amap_mcp(
        "maps_around_search",
        {"keywords": keyword_text, "location": normalized_location, "radius": str(radius_value)},
    )


async def execute_poi_details(id: str) -> str:
    """按关键词或周边搜索结果中的 POI ID 查询地点详情。"""
    poi_id = str(id or "").strip()
    if not poi_id:
        return _amap_error("id 不能为空；请传入关键词搜索或周边搜索返回的 POI ID")
    return await _call_amap_mcp_candidates(["maps_search_detail"], {"id": poi_id})


# ---------------------------------------------------------------------------
# 3. 统一路线规划：用 mode 合并骑行、步行、驾车和公交四类能力
# ---------------------------------------------------------------------------
_ROUTE_TOOL_CANDIDATES: dict[str, list[str]] = {
    # 官方不同发布版本对骑行工具名存在差异，未知工具名时才按顺序降级。
    "cycling": ["maps_bicycling", "maps_direction_bicycling"],
    "walking": ["maps_direction_walking"],
    "driving": ["maps_direction_driving"],
    "transit": ["maps_direction_transit_integrated"],
}

_ROUTE_MODE_ALIASES = {
    "bicycle": "cycling",
    "bicycling": "cycling",
    "bike": "cycling",
    "cycling": "cycling",
    "walk": "walking",
    "walking": "walking",
    "drive": "driving",
    "driving": "driving",
    "car": "driving",
    "transit": "transit",
    "public_transit": "transit",
    "public-transport": "transit",
}


async def execute_route(
    origin: str,
    destination: str,
    mode: str = "driving",
    city: str | None = None,
    cityd: str | None = None,
) -> str:
    """规划骑行、步行、驾车或公交路线；坐标统一使用 ``经度,纬度``。"""
    try:
        normalized_origin = _normalize_amap_coordinate(origin, "origin")
        normalized_destination = _normalize_amap_coordinate(destination, "destination")
    except ValueError as exc:
        return _amap_error(str(exc))

    mode_key = _ROUTE_MODE_ALIASES.get(str(mode or "").strip().lower())
    if mode_key is None:
        return _amap_error("mode 必须是 cycling、walking、driving 或 transit")

    arguments: dict[str, Any] = {
        "origin": normalized_origin,
        "destination": normalized_destination,
    }
    city_text = str(city).strip() if city is not None else ""
    cityd_text = str(cityd).strip() if cityd is not None else ""
    if city is not None and not city_text:
        return _amap_error("city 如提供则不能为空")
    if cityd is not None and not cityd_text:
        return _amap_error("cityd 如提供则不能为空")
    if mode_key != "transit" and (city_text or cityd_text):
        return _amap_error("city 和 cityd 仅适用于 transit 公交路径规划")
    if cityd_text and not city_text:
        return _amap_error("跨城 transit 路线必须同时提供 city 和 cityd")
    if city_text:
        arguments["city"] = city_text
    if cityd_text:
        arguments["cityd"] = cityd_text

    return await _call_amap_mcp_candidates(_ROUTE_TOOL_CANDIDATES[mode_key], arguments)


# ---------------------------------------------------------------------------
# 4. 两点距离
# ---------------------------------------------------------------------------
async def execute_distance(origin: str, destination: str) -> str:
    """测量两点直线距离；origin 与 destination 均为 ``经度,纬度``。"""
    try:
        normalized_origin = _normalize_amap_coordinate(origin, "origin")
        normalized_destination = _normalize_amap_coordinate(destination, "destination")
    except ValueError as exc:
        return _amap_error(str(exc))
    return await _call_amap_mcp(
        "maps_distance",
        {"origins": normalized_origin, "destination": normalized_destination, "type": "1"},
    )


# ===================== 文件编辑器工具实现 =====================


# 编辑器配置
MAX_EDITOR_FILE_SIZE = 5 * 1024 * 1024  # 1MB
EDITOR_PREFIX = "editor"

def _editor_safe_path(path: str) -> str:
    """Return a normalized relative path without traversal segments."""
    if not path or not isinstance(path, str) or path.strip() in ("", "/", "."):
        raise ValueError("Invalid path: empty or root path not allowed")
    if "\x00" in path:
        raise ValueError("Invalid path: null byte not allowed")
    norm = os.path.normpath(path)
    if norm == "." or norm.startswith("..") or os.path.isabs(norm):
        raise ValueError("Invalid path: directory traversal not allowed")
    return norm


def _resolve_editor_path(workspace: Path, safe_path: str) -> Path:
    """Resolve a path and reject any file or parent symlink escaping workspace."""
    root = workspace.resolve()
    resolved = (root / safe_path).resolve(strict=False)
    if resolved == root or root not in resolved.parents:
        raise ValueError("Invalid path: symlink escapes workspace")
    return resolved

def _editor_get_r2_key(chat_id: int, path: str) -> str:
    """生成R2存储的键，按用户隔离。"""
    safe = _editor_safe_path(path)
    return f"{EDITOR_PREFIX}/{chat_id}/{safe}"

def _editor_get_backup_key(chat_id: int, path: str) -> str:
    """生成备份文件的R2键。"""
    safe = _editor_safe_path(path)
    return f"{EDITOR_PREFIX}/{chat_id}/{safe}.backup"


async def persist_workspace_file(
    chat_id: int,
    rel_path: str,
    *,
    delete: bool = False,
    namespace: str | None = None,
) -> dict[str, str | bool]:
    """Persist exactly one file edited by file_editor.

    The local workspace is always the source of truth. This helper only mirrors
    the explicitly changed file to the existing R2 editor namespace; it never
    scans or syncs the whole workspace. Namespace is accepted so callers can keep
    a single workspace identity end-to-end, while the legacy R2 key remains keyed
    by chat_id for backward compatibility.
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

    if not local_path.is_file():
        raise FileNotFoundError(f"workspace file not found: {safe}")
    data = await asyncio.to_thread(local_path.read_bytes)
    content_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    url = await upload_bytes_to_r2(data, key, content_type)
    return {"key": key, "persisted": url is not None}


def _list_directory(dir_path: Path, display_path: str) -> str:
    """List a verified workspace directory without following child symlinks."""
    if not dir_path.exists():
        return f"Error: Directory not found: {display_path}"
    if not dir_path.is_dir():
        return f"Error: Not a directory: {display_path}"
    try:
        entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        return f"Error: Permission denied: {display_path}"
    if not entries:
        return f"Directory {display_path} is empty."
    out = [f"Directory: {display_path} ({len(entries)} entries)"]
    for entry in entries:
        try:
            if entry.is_symlink():
                out.append(f"  [link]  {entry.name}  (not followed)")
                continue
            if entry.is_dir():
                out.append(f"  [dir ]  {entry.name}/")
            else:
                out.append(f"  [file]  {entry.name}  ({entry.stat().st_size} bytes)")
        except OSError as exc:
            out.append(f"  [????]  {entry.name}  (stat failed: {exc})")
    return "\n".join(out)
