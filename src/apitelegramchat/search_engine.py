# search_engine.py — 完整版（集成搜索缓存 & 抓取缓存）
# 文本编辑器：仅支持 view、str_replace、create 和 insert 四个命令。
import asyncio
import aiohttp
import re
import logging
import json
import os
import time
import base64
import hashlib
import ipaddress
import socket
import uuid
import tempfile
import mimetypes
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
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
from cachetools import TTLCache
try:
    from lxml import html as lxml_html  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    lxml_html = None  # type: ignore
from apitelegramchat.state import set_editor_file_state  # noqa: F401  (保留 set_editor_file_state 用于编辑器状态追踪)

from apitelegramchat.config import (
    OPENROUTER_API_KEY,
    FETCH_CACHE_TTL,
    SEARCH_CACHE_TTL,
    SUPPORTED_MODELS,
    get_openrouter_provider_preferences,
)
from apitelegramchat.web_search_settings import (
    WEB_SEARCH_LANGUAGE,
    WEB_SEARCH_REGION,
)
from apitelegramchat.web_search_filter import (
    BLACKLISTED_SEARCH_DOMAINS as _BLACKLISTED_SEARCH_DOMAINS,
    SEARCH_DEFAULT_RESULTS as _SEARCH_DEFAULT_RESULTS,
    SEARCH_MAX_CANDIDATES as _SEARCH_MAX_CANDIDATES,
    SEARCH_MAX_RESULTS as _SEARCH_MAX_RESULTS,
    filter_blacklisted_search_results as _filter_blacklisted_search_results,
)
from apitelegramchat.fetch_url_fallback import root_fallback_urls
from apitelegramchat.utils import retry_async, escape_html
from apitelegramchat.token_budget import truncate_to_token_budget
from apitelegramchat.mcp_client import call_mcp_tool, MCPToolError
from apitelegramchat.tool_result_condense import condense_amap_payload

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

FETCH_CONTENT_TOKEN_BUDGET = 20_000
FETCH_TITLE_TOKEN_BUDGET = 64
TRAFILATURA_TIMEOUT = 10
HTTP_TIMEOUT_SHORT = 10
CURL_TIMEOUT = 20

_TRAFILATURA_CONFIG = use_config()
if _TRAFILATURA_CONFIG is not None:
    try:
        _TRAFILATURA_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(TRAFILATURA_TIMEOUT))
    except Exception:
        pass

# ---------- 缓存 ----------
_fetch_cache = TTLCache(maxsize=200, ttl=FETCH_CACHE_TTL)

# web_search 结果缓存：agent 循环里模型重复/改写同一查询报常见，命中后
# 直接返回上次的格式化结果，省 Serper 配额与延迟；TTL 由 SEARCH_CACHE_TTL
# 控制（默认 300s，与 fetch 缓存同一套环境变量风格）。
_search_cache = TTLCache(maxsize=200, ttl=SEARCH_CACHE_TTL)


def _search_cache_key(
    modes: list[str],
    query: str,
    requested: int,
    page: int | None,
    gl: str | None,
    hl: str | None,
    tbs: str | None,
    image_url: str | None,
) -> str:
    """把归一化后的搜索参数序列化成稳定的缓存键。"""
    return json.dumps(
        {
            "m": list(modes),
            "q": query,
            "n": requested,
            "p": page,
            "gl": gl or "",
            "hl": hl or "",
            "tbs": tbs or "",
            "iu": (image_url or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_cacheable_search_result(value: object) -> bool:
    """只缓存成功结果与确定性空结果；服务错误/异常不缓存，保证可重试。"""
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("❌ 未找到"):
        return True  # 确定性空结果，短期内复用可省配额
    return not value.startswith("❌")


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
    """Persist only the file explicitly changed through text_editor."""
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


def _format_editor_line(line_no: int, text: str, width: int) -> str:
    """Format a text-editor view line with an absolute 1-based line number."""
    return f"{line_no:>{width}}: {text.rstrip(chr(10))}"


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


def _write_text_editor_file(local_path: Path, new_content: str) -> None:
    """Atomically replace an existing UTF-8 text file while preserving its mode."""
    mode = local_path.stat().st_mode & 0o777
    fd, temp_name = tempfile.mkstemp(prefix=f".{local_path.name}.", dir=local_path.parent)
    temp_path = Path(temp_name)
    try:
        with open(fd, "w", encoding="utf-8", closefd=True) as temp_file:
            temp_file.write(new_content)
        os.chmod(temp_path, mode)
        os.replace(temp_path, local_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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

    try:
        safe_path = _editor_safe_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    resolved_namespace = workspace_namespace(chat_id, namespace)
    try:
        await _ensure_runtime_workspace(chat_id, resolved_namespace)
    except PermissionError:
        return _permission_error(command)
    except OSError as exc:
        return f"Error: Cannot access workspace: {exc.strerror or str(exc)}"

    lock = await _get_workspace_lock(chat_id, resolved_namespace)
    async with lock:
        try:
            workspace = workspace_workdir(chat_id, resolved_namespace).resolve()
            local_path = _resolve_editor_path(workspace, safe_path)

            if command == "view":
                if not local_path.exists():
                    return "Error: File not found"
                if local_path.is_dir():
                    return "Error: Path is a directory. view only supports files."
                try:
                    content = local_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    return "Error: File is not valid UTF-8 text."
                lines = content.splitlines()
                total_lines = len(lines)
                if view_range is not None:
                    if (
                        not isinstance(view_range, list)
                        or len(view_range) != 2
                        or not all(isinstance(value, int) for value in view_range)
                    ):
                        return "Error: view_range must be [start_line, end_line]."
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

                set_editor_file_state(chat_id, safe_path, content, local_path.stat().st_mtime)
                if total_lines == 0:
                    return "(empty file)"
                width = len(str(total_lines))
                return "\n".join(
                    _format_editor_line(line_number, lines[line_number - 1], width)
                    for line_number in range(start, end + 1)
                )

            if command == "create":
                if not isinstance(file_text, str):
                    return "Error: Missing file_text for create."
                if local_path.exists():
                    if local_path.is_dir():
                        return "Error: A directory already exists at this path."
                    return "Error: File already exists."
                local_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with open(local_path, "x", encoding="utf-8") as file:
                        file.write(file_text)
                except FileExistsError:
                    return "Error: File already exists."
                set_editor_file_state(chat_id, safe_path, file_text, local_path.stat().st_mtime)
                asyncio.create_task(_persist_edited_file(chat_id, safe_path, namespace=resolved_namespace))
                return _with_latest_editor_snapshot(f"Successfully created file in {local_path}", file_text)

            if not local_path.exists():
                return "Error: File not found"
            if local_path.is_dir():
                return "Error: Path is a directory. Text editing only supports files."
            try:
                content = local_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return "Error: File is not valid UTF-8 text."

            if command == "str_replace":
                if not isinstance(old_str, str) or not isinstance(new_str, str):
                    return "Error: Missing old_str or new_str for str_replace."
                if not old_str:
                    return "Error: old_str must be non-empty for str_replace."
                match_count = content.count(old_str)
                if match_count == 0:
                    return (
                        "Error: No match found for replacement. Recovery: call text_editor "
                        "view on this file, then retry once with an exact old_str copied from the latest view."
                    )
                if match_count > 1:
                    return (
                        f"Error: Found {match_count} matches for replacement text. "
                        "Recovery: call text_editor view, then retry once with a longer exact old_str "
                        "that includes surrounding context."
                    )
                new_content = content.replace(old_str, new_str, 1)
                _write_text_editor_file(local_path, new_content)
                set_editor_file_state(chat_id, safe_path, new_content, local_path.stat().st_mtime)
                asyncio.create_task(_persist_edited_file(chat_id, safe_path, namespace=resolved_namespace))
                return _with_latest_editor_snapshot(f"Successfully replaced string in {local_path}", new_content)

            # command == "insert"
            if not isinstance(insert_line, int) or not isinstance(insert_text, str):
                return "Error: Missing insert_line or insert_text for insert."
            lines = content.splitlines(keepends=True)
            total_lines = len(lines)
            if insert_line < 0 or insert_line > total_lines:
                return f"Error: insert_line must be between 0 and {total_lines}."

            prefix = "".join(lines[:insert_line])
            suffix = "".join(lines[insert_line:])
            text_to_insert = insert_text
            if prefix and not prefix.endswith(("\n", "\r")):
                prefix += "\n"
            if suffix and text_to_insert and not text_to_insert.endswith(("\n", "\r")):
                text_to_insert += "\n"
            new_content = prefix + text_to_insert + suffix
            _write_text_editor_file(local_path, new_content)
            set_editor_file_state(chat_id, safe_path, new_content, local_path.stat().st_mtime)
            asyncio.create_task(_persist_edited_file(chat_id, safe_path, namespace=resolved_namespace))
            return _with_latest_editor_snapshot(f"Successfully inserted string in {local_path} after line {insert_line}", new_content)

        except FileNotFoundError:
            return "Error: File not found"
        except PermissionError:
            return _permission_error(command)
        except IsADirectoryError:
            return "Error: Path is a directory. Text editing only supports files."
        except OSError as exc:
            return f"Error: File operation failed: {exc.strerror or str(exc)}"


# ========== 缓存函数 ==========
def get_fetch_cache(url: str):
    return _fetch_cache.get(_normalize_fetch_cache_key(url))


def set_fetch_cache(url: str, content: str):
    """写入 fetch 缓存。

    重要安全修复：此前所有失败结果（以 ``失败：`` 开头的字符串）也被写
    入缓存。这意味着任何一次网络抖动导致的失败都会让该 URL 在
    ``FETCH_CACHE_TTL``（默认 1 小时）内对所有后续调用直接返回缓存的
    失败字符串，即使网络已恢复也不会重试。现在改为只缓存成功结果，
    失败结果仍然返回给调用方但不写入缓存，让下一次调用有机会重试。
    """
    if isinstance(content, str) and content.startswith("失败："):
        # 失败结果不缓存，避免短暂网络抖动把 URL "中毒" 一整个 TTL 周期。
        return
    _fetch_cache[_normalize_fetch_cache_key(url)] = content


# ---------- 工具函数 ----------
def _truncate(text: str, token_budget: int = FETCH_CONTENT_TOKEN_BUDGET, suffix: str = "…（内容已按 token 预算截断）") -> str:
    return truncate_to_token_budget(text, token_budget, suffix=suffix)


def _get_title_from_html(html_content: str) -> str:
    if not html_content:
        return "无标题"
    try:
        tree = lxml_html.fromstring(html_content)
        title_elem = tree.find('.//title')
        if title_elem is not None and title_elem.text:
            title = title_elem.text.strip()
            return truncate_to_token_budget(title, FETCH_TITLE_TOKEN_BUDGET, suffix="…") if title else "无标题"
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
            "description": (
                "Search Google via Serper. One tool, four modes (controlled by `mode`): "
                "search (default, web pages), images (text-to-image), videos (text-to-video), "
                "lens (reverse image search — pass `image_url`). "
                "`mode` accepts a single value or an array of values to run multiple modes "
                "in one call (e.g. [\"search\",\"images\"]). "
                "For in-depth reading of a result, follow up with fetch_url (one URL per call)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：搜索2024年诺贝尔奖"
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。search/images/videos 模式必填；lens 模式可选（用作文字约束）。",
                    },
                    "mode": {
                        "type": ["string", "array"],
                        "items": {"type": "string", "enum": ["search", "images", "videos", "lens"]},
                        "description": "搜索模式：search（默认，网页）/ images（搜图）/ videos（搜视频）/ lens（以图搜图）。可传数组以一次性执行多个模式。",
                        "default": "search",
                    },
                    "image_url": {
                        "type": "string",
                        "description": "lens 模式必填：要反向搜索的图片 URL。其他模式忽略。",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": (
                            f"可选：单个 mode 的结果数上限。search: 1-{_SEARCH_MAX_RESULTS}（多页聚合）；"
                            f"images/videos/lens: 1-100。不填时默认 {_SEARCH_DEFAULT_RESULTS} 条。"
                        ),
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选：search 模式下的结果偏移量（向后翻页），从 0 开始；其他模式忽略。",
                        "minimum": 0,
                    },
                    "gl": {
                        "type": "string",
                        "description": "可选：地区码（如 us / cn / al）。不填取默认 cn。",
                    },
                    "hl": {
                        "type": "string",
                        "description": "可选：界面语言（如 en / zh-cn / ar）。不填取默认 zh-cn。",
                    },
                    "tbs": {
                        "type": "string",
                        "description": (
                            "可选：时间筛选。常用值：qdr:h（过去1小时）/ qdr:d（过去24小时）/ "
                            "qdr:w（过去一周）/ qdr:m（过去一月）/ qdr:y（过去一年）。不填不限时间。"
                        ),
                    },
                },
                "required": [],
                "anyOf": [
                    {"required": ["query"]},
                    {"required": ["image_url"]}
                ],
            },
            "input_examples": [
                {"query": "2024 诺贝尔物理学奖 获奖者", "num_results": 5},
                {"query": "Python 3.13 新特性", "num_results": 3},
                {"query": "React Hooks 教程", "num_results": 10, "offset": 10},
                {"query": "球球大作战 官网", "mode": "images", "num_results": 8},
                {"query": "苹果发布会", "mode": "videos", "num_results": 5, "tbs": "qdr:w"},
                {"image_url": "https://example.com/photo.jpg", "mode": "lens", "num_results": 10},
                {"query": "特斯拉 model y", "mode": ["search", "images", "videos"], "num_results": 5},
            ],
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch and read the full content of a specific URL. Returns the page rendered as Telegram Rich Message HTML "
                "that mirrors the original page structure and order: headings, paragraphs, lists, tables, quotes, "
                "code blocks, links and media (images, embedded videos, iframe players such as YouTube/Bilibili, "
                "audio) all appear at their original positions; image carousels are grouped into <tg-slideshow>. "
                "You may quote or reuse the relevant HTML fragments (including <img>/<video>/<a> tags with their "
                "original URLs) directly in your reply. "
                "Use when a search result needs deeper reading or the user gave you a link. One URL per call."
            ),
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
            "description": (
                "Look up a topic on Wikipedia by keyword and return the full article rendered as "
                "Telegram Rich Message HTML mirroring the original page structure: headings, "
                "paragraphs, lists, tables (episode lists, statistics), images and links all appear "
                "at their original positions. The keyword is resolved to the best-matching page in "
                "one step (no separate search needed). You may quote or reuse the relevant HTML "
                "fragments (including <table>, <img>, <a> tags) directly in your reply. "
                "Prefer for encyclopedic / factual / definitional queries."
            ),
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
                "Get weather conditions and forecasts for a city. Returns current conditions, "
                "hourly forecast, and up to 5 days of daily forecast. "
                "Use for any weather-related question. unit='c' (default) returns Celsius, "
                "'f' returns Fahrenheit. The `hours` parameter (default 6, max 24) controls "
                "how many hourly entries are returned — pass a larger value when the user "
                "asks about the rest of the day or tomorrow morning."
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
                    "hours": {"type": "integer", "default": 6, "description": "返回的逐时预报条数（1-24，默认 6）。需要更长展望时传大值。"}
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
            "name": "qr_code",
            "description": "Generate a QR code image from text or URL and return its public URL.",
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
            "description": "将地址或地名转换为经纬度坐标（地理编码）。",
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
            "name": "text_editor",
            "description": (
                "Safely view or edit UTF-8 text files. Only four commands are available: "
                "view, str_replace, create, and insert. Always call view immediately before editing. "
                "str_replace requires old_str to match exactly once in the entire file. If it finds zero or multiple matches, "
                "the next action must be view; then retry once with exact text copied from that latest view and enough surrounding context "
                "to identify a unique match. Never infer from stale line numbers or truncated output, and never bypass a text_editor failure "
                "with bash, sed, perl, or python. After two failures of the same edit, stop and explain the blocker. "
                "view returns 1-based line numbers and accepts view_range=[start_line, end_line], "
                "where -1 means the end of the file. insert_line is also 1-based; use 0 to insert at the start."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "Briefly describe the operation (60 characters or fewer)."
                    },
                    "command": {
                        "type": "string",
                        "enum": ["view", "str_replace", "create", "insert"],
                        "description": "The text-editor operation to perform."
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative path of a text file inside the workspace. Directories are not supported."
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "For view: [start_line, end_line], 1-based; end_line=-1 reads to the end."
                    },
                    "old_str": {
                        "type": "string",
                        "description": "For str_replace: exact existing text. It must have exactly one match."
                    },
                    "new_str": {
                        "type": "string",
                        "description": "For str_replace: replacement text; may be an empty string."
                    },
                    "file_text": {
                        "type": "string",
                        "description": "For create: complete initial file content; may be empty."
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "For insert: insert after this 1-based line number; 0 inserts at the beginning."
                    },
                    "insert_text": {
                        "type": "string",
                        "description": "For insert: text to add after insert_line; may be empty."
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
                "Execute bash commands inside the user's per-session workspace. Use this tool for "
                "installs, tests, builds, running scripts, git operations, and inspecting workspace "
                "files.\n"
                "\n"
                "ENVIRONMENT & NETWORK (important — read once, saves you wasted calls):\n"
                "- Outbound network IS allowed. curl and wget are available (if the image lacks the "
                "real binary, an equivalent Python stdlib shim is installed automatically).\n"
                "- Toolchain already in the image: python3 (+pip), node/npm, gcc/g++, make, cmake, "
                "ccache, git, jq, zip/unzip, LibreOffice, pandoc, ImageMagick, poppler, tesseract.\n"
                "- Install extra Python packages with `pip install --user <pkg>` (cache persists). "
                "apt-get is NOT usable — the filesystem outside your workspace is read-only.\n"
                "- If a command genuinely returns `command not found`, do NOT retry it unchanged; "
                "substitute a python3 stdlib equivalent (urllib.request for HTTP, etc.) or use the "
                "fetch_url tool.\n"
                "- Very long output is preserved head+tail: if you see a truncation notice, the "
                "middle was omitted — redirect output to a file (`cmd > out.log`) and inspect it "
                "with grep/tail/text_editor when you need everything.\n"
                "\n"
                "WORKSPACE & WRITABLE SCOPE (read once — this saves 5+ wasted calls):\n"
                "- Your starting cwd IS the workspace root. Its absolute path is in the $WORKSPACE\n"
                "  env var (`echo $WORKSPACE`); every bash result also begins with a\n"
                "  terminal-style prompt line — `/abs/cwd$ <command>` — showing the\n"
                "  directory that command ran in, so you always know where you are\n"
                "  (it updates after `cd`, just like a real shell prompt).\n"
                "- A Landlock sandbox makes ONLY the workspace tree writable. ALL other paths\n"
                "  — /tmp, /home, /root, /workspace, / — are unwritable (most are unreadable\n"
                "  too): `curl -o` there fails with exit code 23, Python writes raise\n"
                "  PermissionError, `ls` may report Permission denied.\n"
                "- NEVER `cd` out of the workspace to download or create files (including the\n"
                "  habitual `cd /tmp`). Download straight into your cwd or a subdir:\n"
                "  `curl -LO <url>`, or `mkdir -p assets && curl -o assets/x.bin <url>`.\n"
                "- TMPDIR already points to a writable private cache inside the sandbox, so\n"
                "  mktemp / Python tempfile / build-tool temp files work unchanged.\n"
                "\n"
                "Avoid interactive or long-running programs (vim, top, less, watch, -it shells, "
                "daemons); they will block the session. If a command appears stuck, set restart=true "
                "to reset the session and retry with a non-interactive variant.\n"
                "\n"
                "UPLOAD & DOWNLOAD DIRECTORIES (inside your workspace root):\n"
                "- download/ holds files the user has sent you (uploaded documents etc.).\n"
                "  Read them directly from your cwd: `ls download/`, `cat download/brief.pdf`.\n"
                "- upload/ is the staging area for outgoing files. To send a file to the\n"
                "  user, copy it here first (`cp report.pdf upload/report.pdf`), then call\n"
                "  present_files.\n"
                "- Both directories live at the root of your workspace (your starting cwd).\n"
                "  You MAY freely read and write files inside them via relative paths.\n"
                "- You MAY NOT `cd` into upload/ or download/, and you MAY NOT execute any\n"
                "  command while your cwd is inside either of them. The sandbox rejects\n"
                "  `cd upload/...` and any subsequent command; if a command is rejected,\n"
                "  `cd` back to your workdir first, then operate via relative paths.\n"
                "\n"
                "To read a skill's instructions, `cd skills/<skill_id>` from your cwd."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": (
                            "意图描述：用一句话说明本次命令的目的（≤60字），会作为执行进度"
                            "展示给用户。示例：查看项目文件列表 / 安装依赖并运行测试 / "
                            "读取用户上传的文档"
                        )
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
                {"_description": "查看项目文件列表", "command": "ls -la"},
                {"_description": "安装依赖并运行测试", "command": "pip install --user pytest && python3 -m pytest -q"},
                {"_description": "读取用户上传的文档", "command": "head -c 2000 download/brief.pdf | strings | head -40"},
                {"_description": "把报告放入发送暂存区", "command": "cp report.pdf upload/report.pdf"},
                {"_description": "重启卡死的会话", "restart": True}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "present_files",
            "description": (
                "Send one or more files from upload/ to the chat as attachments. Files MUST already be "
                "staged under upload/ via bash (e.g. `cp out.txt upload/out.txt`); files left "
                "elsewhere in the workdir are NOT directly sendable. Pass paths relative to "
                "upload/ (e.g. `hello.py` or `out/report.pdf`). A single leading `upload/` "
                "prefix and a leading `./` are tolerated; absolute paths inside the per-chat "
                "upload/ root are also accepted. Wildcards are not supported."
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
                    "Generate a short video from a text prompt. Use when the user explicitly asks to create / generate / make a video. Do NOT use for animated images or GIFs (use generate_image_from_text instead). Generation is async and may take 1-5 minutes. On success, it returns a stable HTTPS URL in the exact form `视频链接：https://...`, just like image-generation tools return image URLs. In your next final response, embed that exact URL as a separate rich-media block: <figure><video src=\"URL\"></video><figcaption>已生成视频</figcaption></figure>; never send only a bare URL or ordinary hyperlink. "
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

# 搜索通过直连 Serper 官方 REST API（见 serper_api.py）实现。一个工具支持
# 4 种 mode：search / images / videos / lens（以图搜图）。每 mode 的请求与
# 响应字段均严格遵循 https://serper.dev 的官方文档。

from apitelegramchat.serper_api import (
    SerperError,
    SerperUnavailableError,
    SERPER_DEFAULT_TIMEOUT as _SERPER_DEFAULT_TIMEOUT,
)


class SerperSearchTransientError(Exception):
    """Serper 上游临时未返回结果（例如 organic 为空），可重试。"""


SERPER_PAGE_SIZE = 10  # search 端点单页固定 10 条


def _serper_api_timeout() -> float:
    """读取 SERPER_API_TIMEOUT 配置（默认 12s）。"""
    try:
        from apitelegramchat.config import SERPER_API_TIMEOUT
        if isinstance(SERPER_API_TIMEOUT, (int, float)) and SERPER_API_TIMEOUT >= 1.0:
            return float(SERPER_API_TIMEOUT)
    except Exception:
        pass
    return _SERPER_DEFAULT_TIMEOUT


def _parse_serper_search_result(data: dict[str, Any] | None) -> list[dict]:
    """从 serper.dev /search 响应中提取 organic 列表为统一字段。

    Serper /search 的 organic item 字段（实测）:
      title, link, snippet, date(可选), rating(可选), ratingCount(可选), position
    本函数保留前 5 个有用字段；position 已被列表顺序编码，无需重复存储。
    rating 为浮点（如 4.3），ratingCount 为整数（如 30740），两者通常同时出现
    但偶有单独出现的情况，统一存为字符串便于下游条件性渲染。
    """
    if not isinstance(data, dict):
        return []
    organic = data.get("organic")
    if not isinstance(organic, list):
        return []
    items: list[dict] = []
    for result in organic:
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip() or "无标题"
        link = str(result.get("link") or "").strip()
        snippet = str(result.get("snippet") or "").strip()
        if not link:
            continue
        # rating 可能是 float 或字符串；统一规范化成可读字符串
        rating_raw = result.get("rating")
        rating = ""
        if isinstance(rating_raw, (int, float)) and rating_raw > 0:
            rating = f"{float(rating_raw):.1f}"
        elif isinstance(rating_raw, str) and rating_raw.strip():
            rating = rating_raw.strip()
        rating_count_raw = result.get("ratingCount")
        rating_count = ""
        if isinstance(rating_count_raw, int) and rating_count_raw > 0:
            rating_count = str(rating_count_raw)
        items.append({
            "title": title,
            "link": link,
            "snippet": snippet,
            "date": str(result.get("date") or "").strip(),
            "rating": rating,
            "rating_count": rating_count,
        })
    return items


def _parse_serper_images_result(data: dict[str, Any] | None) -> list[dict]:
    """从 serper.dev /images 响应中提取图片列表为统一字段。"""
    if not isinstance(data, dict):
        return []
    images = data.get("images")
    if not isinstance(images, list):
        return []
    items: list[dict] = []
    for result in images:
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip() or "无标题"
        image_url = str(result.get("imageUrl") or "").strip()
        link = str(result.get("link") or "").strip() or image_url
        if not image_url:
            continue
        items.append({
            "title": title,
            "image_url": image_url,
            "link": link,
            "thumbnail_url": str(result.get("thumbnailUrl") or "").strip(),
            "source": str(result.get("source") or result.get("domain") or "").strip(),
            "width": result.get("imageWidth"),
            "height": result.get("imageHeight"),
        })
    return items


def _parse_serper_videos_result(data: dict[str, Any] | None) -> list[dict]:
    """从 serper.dev /videos 响应中提取视频列表为统一字段。

    Serper /videos 的 item 字段（实测）:
      title, link(观看页 URL), snippet, imageUrl(封面图直链),
      videoUrl(可选，视频媒体直链), duration(可选), source, channel(可选), date, position

    ⚠️ 区分两类 URL:
      - link   = YouTube / Bilibili / Facebook 等观看页 HTML URL —— 给 <a href> 用，
                 不能塞进 <video src>，否则 Telegram 会报 RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND。
      - videoUrl = Google CDN 上的视频媒体直链 (encrypted-vtbn0.gstatic.com/video?q=...)，
                 是真正能塞进 <video src> 的 URL；不是每个 item 都有，缺失时留空。
    本函数把两者分别保存为 link / video_url，避免下游 AI 把它们搞混。
    duration 形如 "20:40" 或 "0:54"；channel 是发布者名（YouTube 频道、FB 主页等）。
    """
    if not isinstance(data, dict):
        return []
    videos = data.get("videos")
    if not isinstance(videos, list):
        return []
    items: list[dict] = []
    for result in videos:
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip() or "无标题"
        link = str(result.get("link") or "").strip()
        if not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "snippet": str(result.get("snippet") or "").strip(),
            "image_url": str(result.get("imageUrl") or "").strip(),
            "video_url": str(result.get("videoUrl") or "").strip(),
            "duration": str(result.get("duration") or "").strip(),
            "source": str(result.get("source") or "").strip(),
            "channel": str(result.get("channel") or "").strip(),
            "date": str(result.get("date") or "").strip(),
        })
    return items


def _parse_serper_lens_result(data: dict[str, Any] | None) -> list[dict]:
    """从 serper.dev /lens 响应中提取 organic 列表为统一字段。"""
    if not isinstance(data, dict):
        return []
    organic = data.get("organic")
    if not isinstance(organic, list):
        return []
    items: list[dict] = []
    for result in organic:
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip() or "无标题"
        link = str(result.get("link") or "").strip()
        image_url = str(result.get("imageUrl") or "").strip()
        if not link and not image_url:
            continue
        items.append({
            "title": title,
            "link": link,
            "image_url": image_url,
            "thumbnail_url": str(result.get("thumbnailUrl") or "").strip(),
            "source": str(result.get("source") or "").strip(),
        })
    return items


async def _serper_search_one_mode(
    mode: str,
    *,
    query: str | None,
    image_url: str | None,
    num: int | None,
    page: int | None,
    gl: str | None,
    hl: str | None,
    tbs: str | None,
) -> list[dict]:
    """执行单个 mode 的搜索，返回统一字段的结果列表。

    对于 search mode，单页固定 10 条；当 num > 10 时按页并发取回再合并，
    保持原有 offset 语义。其他 mode 直接用 serper 返回的列表。
    """
    timeout = _serper_api_timeout()

    if mode == "search":
        if not query:
            raise SerperSearchTransientError("search mode requires query")
        # search 端点 num 实际是 page 数：每页固定 10 条。把 num_results 折算成页。
        requested_num = num if isinstance(num, int) and num > 0 else _SEARCH_DEFAULT_RESULTS
        requested_num = min(max(requested_num, 1), _SEARCH_MAX_CANDIDATES)
        # 处理 offset（向后翻页）
        offset = max(int(page or 1) - 1, 0) * SERPER_PAGE_SIZE if page else 0
        # page 数从 1 开始
        first_page = offset // SERPER_PAGE_SIZE + 1
        last_idx = offset + requested_num - 1
        last_page = last_idx // SERPER_PAGE_SIZE + 1
        pages = range(first_page, last_page + 1)

        from apitelegramchat.serper_api import search as serper_search_api
        async def _fetch_page(p: int) -> list[dict]:
            data = await serper_search_api(
                query, gl=gl, hl=hl, tbs=tbs, page=p, timeout=timeout,
            )
            return _parse_serper_search_result(data)

        outcomes = await asyncio.gather(
            *(_fetch_page(p) for p in pages), return_exceptions=True,
        )
        items: list[dict] = []
        first_error: BaseException | None = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if first_error is None:
                    first_error = outcome
                continue
            items.extend(outcome)

        if not items and first_error is not None:
            if isinstance(first_error, SerperError):
                raise first_error
            raise SerperSearchTransientError(
                f"Serper search returned no results; first error: {first_error}"
            )
        start = offset % SERPER_PAGE_SIZE
        return items[start:start + requested_num]

    if mode == "images":
        if not query:
            raise SerperSearchTransientError("images mode requires query")
        from apitelegramchat.serper_api import images as serper_images_api
        data = await serper_images_api(
            query, num=num, page=page, gl=gl, hl=hl, tbs=tbs, timeout=timeout,
        )
        return _parse_serper_images_result(data)

    if mode == "videos":
        if not query:
            raise SerperSearchTransientError("videos mode requires query")
        from apitelegramchat.serper_api import videos as serper_videos_api
        data = await serper_videos_api(
            query, num=num, page=page, gl=gl, hl=hl, tbs=tbs, timeout=timeout,
        )
        return _parse_serper_videos_result(data)

    if mode == "lens":
        if not image_url:
            raise SerperSearchTransientError("lens mode requires image_url")
        from apitelegramchat.serper_api import lens as serper_lens_api
        data = await serper_lens_api(
            image_url, query=query, num=num, page=page, gl=gl, hl=hl, tbs=tbs, timeout=timeout,
        )
        return _parse_serper_lens_result(data)

    raise SerperSearchTransientError(f"unknown serper mode: {mode}")


def _format_search_results(items: list, query: str, engine: str, requested: int | None = None) -> str:
    """渲染 search 模式的 envelope section。

    字段顺序固定为：标题 → 摘要 → 时间(可选) → 链接 → 评分(可选)。
    时间/评分行仅在对应字段非空时才出现，避免给 AI 灌空行。
    评分行格式：`评分：4.3 ⭐ (30740 评价)`，无评价数则只输出星标。
    """
    success_count = len(items)
    requested_count = requested if isinstance(requested, int) and requested > 0 else success_count
    lines = [f"🔍 [成功: {engine}] 搜索「{query}」的结果（{success_count}/{requested_count}）：\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        snippet = item.get("snippet", "")
        date = item.get("date", "")
        link = item.get("link", "")
        rating = item.get("rating", "")
        rating_count = item.get("rating_count", "")
        block = f"{i}. 标题：{title}\n   摘要：{snippet}\n"
        if date:
            block += f"   时间：{date}\n"
        block += f"   链接：{link}\n"
        if rating:
            rating_line = f"   评分：{rating} ⭐"
            if rating_count:
                rating_line += f" ({rating_count} 评价)"
            rating_line += "\n"
            block += rating_line
        lines.append(block)
    return "\n".join(lines)


def _format_image_results(items: list, query: str, requested: int | None = None) -> str:
    success_count = len(items)
    requested_count = requested if isinstance(requested, int) and requested > 0 else success_count
    lines = [f"🖼️ [成功: Serper Images] 搜图「{query}」的结果（{success_count}/{requested_count}）：\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        image_url = item.get("image_url", "")
        link = item.get("link", "")
        source = item.get("source", "")
        lines.append(
            f"{i}. 标题：{title}\n"
            f"   图片：{image_url}\n"
            f"   来源：{source}\n"
            f"   页面：{link}\n"
        )
    return "\n".join(lines)


def _format_video_results(items: list, query: str, requested: int | None = None) -> str:
    """渲染 videos 模式的 envelope section。

    字段命名上做了关键区分，避免 AI 把观看页 URL 误当视频媒体 URL：
      页面 = link 字段，YouTube/Bilibili 等观看页 HTML URL —— 给 <a href> 用
      封面 = image_url 字段，封面图直链 —— 给 <img src> 用
      视频 = video_url 字段，Google CDN 视频媒体直链 —— 给 <video src> 用
    视频行只在 video_url 非空时才出现（不是每个 item 都有 videoUrl）。
    时长/频道/时间 同样条件性输出。
    """
    success_count = len(items)
    requested_count = requested if isinstance(requested, int) and requested > 0 else success_count
    lines = [f"🎬 [成功: Serper Videos] 搜视频「{query}」的结果（{success_count}/{requested_count}）：\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        snippet = item.get("snippet", "")
        duration = item.get("duration", "")
        source = item.get("source", "")
        channel = item.get("channel", "")
        date = item.get("date", "")
        link = item.get("link", "")
        image_url = item.get("image_url", "")
        video_url = item.get("video_url", "")
        block = f"{i}. 标题：{title}\n   摘要：{snippet}\n"
        if duration:
            block += f"   时长：{duration}\n"
        if source:
            block += f"   来源：{source}\n"
        if channel:
            block += f"   频道：{channel}\n"
        if date:
            block += f"   时间：{date}\n"
        block += f"   页面：{link}\n"
        if image_url:
            block += f"   封面：{image_url}\n"
        if video_url:
            block += f"   视频：{video_url}\n"
        lines.append(block)
    return "\n".join(lines)


def _format_lens_results(items: list, image_url: str, requested: int | None = None) -> str:
    success_count = len(items)
    requested_count = requested if isinstance(requested, int) and requested > 0 else success_count
    lines = [f"🔎 [成功: Serper Lens] 以图搜图「{image_url}」的结果（{success_count}/{requested_count}）：\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        link = item.get("link", "")
        image_url_item = item.get("image_url", "")
        source = item.get("source", "")
        lines.append(
            f"{i}. 标题：{title}\n"
            f"   来源：{source}\n"
            f"   页面：{link}\n"
            f"   图片：{image_url_item}\n"
        )
    return "\n".join(lines)


def _normalize_modes(mode: str | list[str] | None) -> list[str]:
    """把 mode 参数规范化为去重后的有序 list。默认 ["search"]。"""
    if mode is None:
        return ["search"]
    if isinstance(mode, str):
        m = mode.strip().lower()
        if not m:
            return ["search"]
        return [m]
    if isinstance(mode, list):
        out: list[str] = []
        seen: set[str] = set()
        for m in mode:
            if not isinstance(m, str):
                continue
            normalized = m.strip().lower()
            if not normalized or normalized in seen:
                continue
            if normalized not in {"search", "images", "videos", "lens"}:
                continue
            seen.add(normalized)
            out.append(normalized)
        return out or ["search"]
    return ["search"]


async def execute_web_search(
    query: str | None = None,
    num_results: int | None = None,
    offset: int | None = None,
    *,
    mode: str | list[str] | None = "search",
    image_url: str | None = None,
    gl: str | None = None,
    hl: str | None = None,
    tbs: str | None = None,
) -> str:
    """通过 Serper 直连 API 搜索，支持 search / images / videos / lens 四种 mode。

    带结果缓存：归一化参数相同的重复查询在 SEARCH_CACHE_TTL（默认 300s）
    内直接返回上次的格式化结果。缓存覆盖主 agent、子 agent 与重试路径，
    agent 循环里模型重复同一查询时不再消耗 Serper 配额。

    单次调用可同时执行多个 mode（mode 为 list 时并发执行）。各 mode 的失败
    互不影响：成功 mode 的结果正常返回，失败 mode 在结果末尾以错误说明列出。

    参数：
      query:       搜索关键词。search / images / videos 必填；lens 可选。
      num_results: 单 mode 的结果数上限。search: 1-50（多页聚合）；
                   images / videos / lens: 1-100。
      offset:      search mode 的偏移量（向后翻页），其他 mode 忽略；
                   为兼容老调用方，等价于 page = offset // 10 + 1。
      mode:        "search"（默认） / "images" / "videos" / "lens"，或它们的 list。
      image_url:   lens mode 必填；其他 mode 忽略。
      gl:          地区码（如 us / cn），默认取 WEB_SEARCH_REGION。
      hl:          界面语言（如 en / zh-cn），默认取 WEB_SEARCH_LANGUAGE。
      tbs:         时间筛选（如 qdr:d 当天 / qdr:w 一周 / qdr:m 一月 / qdr:y 一年）。
    """
    # ---- 参数归一化（与缓存 key 保持同一套逻辑） ----
    modes = _normalize_modes(mode)
    requested = _SEARCH_DEFAULT_RESULTS
    if num_results is not None:
        requested = max(1, min(int(num_results), _SEARCH_MAX_RESULTS))
    page: int | None = None
    if offset is not None:
        page = max(int(offset), 0) // SERPER_PAGE_SIZE + 1
    query_str = (query or "").strip()

    cache_key = _search_cache_key(
        modes, query_str, requested, page, gl, hl, tbs, image_url,
    )
    cached = _search_cache.get(cache_key)
    if cached is not None:
        logger.debug("Search cache hit: %s", cache_key[:160])
        return cached

    result = await _execute_web_search_uncached(
        query=query,
        num_results=num_results,
        offset=offset,
        mode=mode,
        image_url=image_url,
        gl=gl,
        hl=hl,
        tbs=tbs,
    )
    if _is_cacheable_search_result(result):
        _search_cache[cache_key] = result
    return result


async def _execute_web_search_uncached(
    query: str | None = None,
    num_results: int | None = None,
    offset: int | None = None,
    *,
    mode: str | list[str] | None = "search",
    image_url: str | None = None,
    gl: str | None = None,
    hl: str | None = None,
    tbs: str | None = None,
) -> str:
    """execute_web_search 的无缓存实现（原函数体，逻辑未变）。"""
    modes = _normalize_modes(mode)
    requested = _SEARCH_DEFAULT_RESULTS
    if num_results is not None:
        requested = max(1, min(int(num_results), _SEARCH_MAX_RESULTS))

    # 对 search mode 的 offset，转为 page（向后兼容旧调用方）
    page: int | None = None
    if offset is not None:
        page = max(int(offset), 0) // SERPER_PAGE_SIZE + 1

    query_str = (query or "").strip()
    needs_query = any(m in {"search", "images", "videos"} for m in modes)
    if needs_query and not query_str:
        return "❌ 搜索关键词为空。"
    if "lens" in modes and not (image_url or "").strip():
        return "❌ 以图搜图（lens）模式需要 image_url 参数。"

    timeout = _serper_api_timeout()

    # 单 mode 时走轻量路径；多 mode 时并发执行。
    if len(modes) == 1:
        single_mode = modes[0]
        try:
            items = await _serper_search_one_mode(
                single_mode,
                query=query_str or None,
                image_url=(image_url or "").strip() or None,
                num=requested,
                page=page,
                gl=gl,
                hl=hl,
                tbs=tbs,
            )
        except SerperUnavailableError as exc:
            logger.warning("Serper API 未配置: %s", exc)
            return exc.user_message("网页搜索服务")
        except SerperError as exc:
            logger.warning(
                "Serper API 调用失败 mode=%s category=%s status=%s retryable=%s: %s",
                single_mode, exc.category,
                exc.status_code if exc.status_code is not None else "unknown",
                exc.retryable, exc,
            )
            return exc.user_message("网页搜索服务")
        except SerperSearchTransientError as exc:
            logger.warning("Serper 未返回有效结果 mode=%s: %s", single_mode, exc)
            return "❌ 网页搜索服务暂未返回有效结果；请稍后重试。"
        except Exception as exc:
            logger.exception("Serper 搜索发生未分类异常 mode=%s", single_mode)
            return "❌ 网页搜索服务发生未分类异常；请稍后重试。"

        # 仅 search mode 应用本地黑名单过滤；其他 mode 不涉及域名黑名单语义。
        if single_mode == "search" and items:
            items, filtered_count = _filter_blacklisted_search_results(items)
            items = items[:requested]
            if filtered_count:
                logger.info(
                    "web_search 已过滤 %s 条黑名单域名结果，domains=%s",
                    filtered_count, ", ".join(_BLACKLISTED_SEARCH_DOMAINS),
                )
            if items:
                return _format_search_results(items, query_str, "Serper / Google", requested=requested)
            return f"❌ 未找到与「{query_str}」相关的结果。"
        if single_mode == "images" and items:
            return _format_image_results(items[:requested], query_str, requested=requested)
        if single_mode == "videos" and items:
            return _format_video_results(items[:requested], query_str, requested=requested)
        if single_mode == "lens" and items:
            return _format_lens_results(items[:requested], (image_url or "").strip(), requested=requested)
        # 无结果
        if single_mode == "lens":
            return f"❌ 未找到与图片「{(image_url or '').strip()}」相关的结果。"
        return f"❌ 未找到与「{query_str}」相关的结果。"

    # 多 mode：并发执行，逐 mode 拼接结果
    async def _run_one(m: str) -> tuple[str, str | None, Exception | None]:
        try:
            items = await _serper_search_one_mode(
                m,
                query=query_str or None,
                image_url=(image_url or "").strip() or None,
                num=requested,
                page=page,
                gl=gl,
                hl=hl,
                tbs=tbs,
            )
            if m == "search" and items:
                items, _ = _filter_blacklisted_search_results(items)
                items = items[:requested]
            elif items:
                items = items[:requested]
            if m == "search":
                text = _format_search_results(items, query_str, "Serper / Google", requested=requested) if items else None
            elif m == "images":
                text = _format_image_results(items, query_str, requested=requested) if items else None
            elif m == "videos":
                text = _format_video_results(items, query_str, requested=requested) if items else None
            elif m == "lens":
                text = _format_lens_results(items, (image_url or "").strip(), requested=requested) if items else None
            else:
                text = None
            return m, text, None
        except (SerperError, SerperSearchTransientError) as exc:
            return m, None, exc
        except Exception as exc:
            return m, None, exc

    outcomes = await asyncio.gather(*(_run_one(m) for m in modes))
    sections: list[str] = []
    errors: list[tuple[str, str]] = []
    for m, text, exc in outcomes:
        if exc is not None:
            if isinstance(exc, SerperError):
                errors.append((m, exc.user_message(f"{m} 搜索")))
            else:
                errors.append((m, f"❌ {m} 搜索失败：{exc}"))
            continue
        if text:
            sections.append(text)
        else:
            label = {
                "search": f"「{query_str}」",
                "images": f"「{query_str}」",
                "videos": f"「{query_str}」",
                "lens": f"「{(image_url or '').strip()}」",
            }.get(m, "")
            errors.append((m, f"❌ {m} 未找到与{label}相关的结果。"))

    if not sections and errors:
        # 全失败：返回第一个错误（让上层 retry_async 触发重试）
        first_mode, first_msg = errors[0]
        logger.warning("web_search 全部 mode 失败 modes=%s first=%s", modes, first_msg)
        return first_msg

    body = "\n\n".join(sections)
    if errors:
        body += "\n\n" + "\n".join(msg for _, msg in errors)
    return body


# --------------------- fetch_url (Telegram Rich HTML 输出) ---------------------

# 字符编码检测：优先级与 WHATWG / HTML5 规范对齐。
#   1. BOM (UTF-8-SIG / UTF-16-LE / UTF-16-BE)
#   2. HTTP Content-Type 头里的 charset
#   3. HTML <meta charset="..."> / <meta http-equiv="Content-Type" content="...; charset=...">
#   4. chardet/charset_normalizer（若已安装）作为兜底
#   5. UTF-8 with errors='replace'（最后防线）
#
# 这条路径之前直接用 response.text（curl_cffi 仅按 HTTP 头 charset 解码），
# 对于在 meta 标签里写 charset=gb2312 但 HTTP 头里没声明 charset 的网站
# （如 jxrb.jxwmw.cn），全部会得到 UTF-8 误码后的"馊字"，导致标题和正文
# 提取都失败。改成从 raw bytes 开始按上面优先级解码后，标题/正文恢复正确。
_BOM_TABLE = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
# 在头部 4KB 内扫这两条 meta 形式足够覆盖大多数中文站点。
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:]+)""",
    re.IGNORECASE,
)


def _detect_html_encoding(raw: bytes, http_encoding: str | None) -> str:
    """按 HTML5 规范的优先级返回最可能的字符集名称。

    raw 是 HTTP 响应体（未经 .text 转换的原始字节）。http_encoding 是
    curl_cffi 从 Content-Type 头解析出来的字符集（可能为 None / 空 / "None"）。
    """
    if not raw:
        return "utf-8"
    # 1) BOM
    for bom, enc in _BOM_TABLE:
        if raw.startswith(bom):
            return enc
    head = raw[:4096]
    # 2) HTTP 头里给的 charset（curl_cffi 会自动把 .encoding 设成这个）
    if http_encoding:
        enc = http_encoding.strip().lower()
        # 显式 ISO-8859-1 通常只是 curl_cffi 的兜底，不应优先于 meta
        if enc and enc not in {"iso-8859-1", "latin-1", "ascii"}:
            return _normalize_encoding_name(enc)
    # 3) HTML meta charset
    m = _META_CHARSET_RE.search(head)
    if m:
        enc = m.group(1).decode("ascii", errors="ignore").strip().lower()
        if enc:
            return _normalize_encoding_name(enc)
    # 4) chardet / charset_normalizer 兜底
    try:
        import chardet  # type: ignore
        guess = chardet.detect(raw[:32768])
        if isinstance(guess, dict):
            enc = (guess.get("encoding") or "").strip().lower()
            conf = float(guess.get("confidence") or 0.0)
            if enc and conf >= 0.7:
                return _normalize_encoding_name(enc)
    except Exception:
        pass
    try:
        # charset_normalizer 是 requests / chardet 的常见替代品
        from charset_normalizer import from_bytes  # type: ignore
        best = from_bytes(raw[:32768]).best()
        if best is not None:
            enc = (best.encoding or "").strip().lower()
            if enc:
                return _normalize_encoding_name(enc)
    except Exception:
        pass
    # 5) 最后防线：UTF-8 with errors='replace'
    return "utf-8"


def _normalize_encoding_name(name: str) -> str:
    """把 'gb2312' / 'gbk' / 'utf8' 等常见别名规范化为 Python codecs 认得的形式。"""
    if not name:
        return "utf-8"
    n = name.strip().lower().replace("_", "-")
    # gb_2312-80 / gb2312-80 / gb2312 → gbk（GBK 是 GB2312 的超集，更稳）
    if n in {"gb2312", "gb-2312", "gb_2312", "gb2312-80", "gb_2312-80", "chinese", "csiso58gb231280", "csgb2312"}:
        return "gbk"
    if n in {"utf8", "utf-8-8", "utf8-8"}:
        return "utf-8"
    if n == "utf-8-sig":
        return "utf-8-sig"
    if n in {"utf-16le", "utf_16_le"}:
        return "utf-16-le"
    if n in {"utf-16be", "utf_16_be"}:
        return "utf-16-be"
    return n


def _decode_html_bytes(raw: bytes | None, http_encoding: str | None) -> str | None:
    """把原始字节按检测出的编码安全解码为 str。"""
    if not raw:
        return None
    enc = _detect_html_encoding(raw, http_encoding)
    try:
        return raw.decode(enc, errors="replace")
    except (LookupError, TypeError):
        # 未知编码名 → 退回 UTF-8
        return raw.decode("utf-8", errors="replace")


async def _fetch_html_with_curl(url: str) -> str | None:
    try:
        async with AsyncSession() as session:
            response = await session.get(url, timeout=CURL_TIMEOUT, impersonate="chrome120",
                                         headers={"Accept": "text/html,application/xhtml+xml,*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            if response.status_code != 200:
                return None
            # 优先按 HTTP 头 + meta + chardet 检测的编码解码，避免 GBK 站点被
            # 错误地按 UTF-8 解析产生馊字标题。
            raw = response.content
            http_enc = getattr(response, "encoding", None)
            decoded = _decode_html_bytes(raw, http_enc)
            if decoded is not None:
                return decoded
            # 兜底：让 curl_cffi 自己用 .text（HTTP 头声明的编码）解码。
            return response.text
    except Exception as e:
        logger.error(f"curl_cffi 请求异常: {e}, URL: {url}")
        return None


async def _download_html_with_trafilatura(url: str) -> str | None:
    """curl_cffi 失败时用 trafilatura 自带下载器兜底获取原始 HTML。

    trafilatura.fetch_url 内部会按 HTML5 规范做编码检测（含 BOM / meta /
    chardet 兜底），因此对 GBK 站点不会出现馊字。返回值是已解码的 str。
    """
    if trafilatura is None:
        return None
    try:
        downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
        return downloaded or None
    except Exception as e:
        logger.debug(f"trafilatura 下载失败: {url}: {e}")
        return None


def _build_rich_fetch_payload(url: str, html: str) -> str | None:
    """把原始 HTML 转换为【返回给模型】的 Telegram Rich HTML（同步、CPU 密集）。

    提取链路（结果忠实于原页面文档顺序，媒体原位呈现，无聚合媒体区）：
      1. trafilatura XML（保留链接/图片/格式/表格及其顺序）→ Telegram HTML 块；
      2. DOM 文档序收集内嵌视频/iframe 播放器/音频/懒加载图片（带位置）；
      3. 锚定 + 原位插回正文流；轮播图 → <tg-slideshow>；预算内整块截断。
    注意：本函数的返回值只进入模型上下文；Telegram 工具 UI 的展示由
    tool_executors.format_tool_result 单独负责（保持历史简单样式）。
    返回 None 表示完全提不出内容（调用方继续走重定向检测/失败路径）。
    """
    if not html:
        return None
    try:
        from apitelegramchat.fetch_rich_content import (
            build_model_facing_html,
            build_fallback_text_from_html,
            extract_body_blocks,
            extract_title_from_html,
        )
    except Exception as e:
        logger.error(f"[fetch_url] fetch_rich_content 导入失败: {e}")
        return None

    title = extract_title_from_html(html)

    # 正文提取：trafilatura XML → Telegram HTML 块（含中文页面退化检测与
    # favor_precision/favor_recall 回退），链接/图片/格式/表格全保留。
    body_blocks = extract_body_blocks(html, url)
    body_len = sum(len(b) for b in body_blocks)

    fallback_text = ""
    if body_len < 200:
        # 结构化提取失败：纯文本兜底（meta 描述 + 段落），媒体仍会原位插入。
        fallback_text = build_fallback_text_from_html(html)
        if not fallback_text.strip():
            # 连兜底文本都没有：若 DOM 也完全没有媒体则直接失败；
            # 有媒体时仍交给 build_model_facing_html 产出媒体型结果。
            probe = build_model_facing_html(url, html, body_blocks=[], title=title)
            if not probe:
                return None

    result = build_model_facing_html(
        url, html, body_blocks=body_blocks, title=title, fallback_text=fallback_text,
    )
    if not result:
        return None
    # 最终防御：结果可见文本过短且无任何媒体时视为提取失败。
    visible = re.sub(r'<[^>]+>', '', result)
    has_media = bool(re.search(r'<(img|video|audio|tg-slideshow)\b', result))
    if len(re.sub(r'\s+', '', visible)) < 60 and not has_media:
        return None
    return result


# --------------------- SSRF 防护 ---------------------
_ALLOWED_FETCH_SCHEMES = {"http", "https"}


def _is_safe_url_to_fetch_sync(url: str) -> tuple[bool, str]:
    """
    SSRF 防护（同步部分）：URL 协议/格式校验 + IP 字面量校验。
    不做 DNS 解析。需要 DNS 解析的部分由 `_is_safe_url_to_fetch` 的 async 包装完成。
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
    # 如果 host 本身就是 IP 字面量，直接校验，无需 DNS 解析
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 不是 IP 字面量，是域名，DNS 解析交由 async 部分处理
        return True, ""
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return False, f"目标地址 {ip} 属于禁止访问的范围（私网/回环/链路本地等）"
    return True, ""


def _check_ip_safe(ip_str: str) -> tuple[bool, str]:
    """单个 IP 字符串的 SSRF 校验。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True, ""  # 不是 IP，跳过
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return False, f"目标地址 {ip} 属于禁止访问的范围（私网/回环/链路本地等）"
    return True, ""


async def _is_safe_url_to_fetch(url: str) -> tuple[bool, str]:
    """
    SSRF 防护：先做同步部分（URL 协议/IP 字面量校验），再做异步 DNS 解析。
    注意：DNS 解析必须用 asyncio 的非阻塞版本，不能用同步 socket.getaddrinfo，
    否则恶意 LLM 高频调用 fetch_url 即可拖垮整个事件循环。
    """
    ok, reason = _is_safe_url_to_fetch_sync(url)
    if not ok:
        return False, reason
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host:
        return False, "URL 缺少主机名"
    # 如果是 IP 字面量，同步部分已校验，无需 DNS
    try:
        ipaddress.ip_address(host)
        return True, ""
    except ValueError:
        pass
    # 异步 DNS 解析，避免阻塞事件循环
    try:
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo(
            host, parts.port or 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        return False, f"DNS 解析失败: {e}"
    except Exception as e:
        return False, f"DNS 解析异常: {e}"
    for _family, _stype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        ok, reason = _check_ip_safe(ip_str)
        if not ok:
            return False, reason
    return True, ""


async def _try_root_url_fallback(
    url: str,
    redirect_depth: int,
    start_time: float,
) -> str | None:
    """在根路径抓取失败后尝试配置的同站点首页路径。"""
    candidates = root_fallback_urls(url)
    for fallback_url in candidates:
        if time.monotonic() - start_time > 30:
            logger.warning("[fetch_url] 首页回退超出总超时：%s", url)
            break
        logger.info("[fetch_url] 根路径回退：%s -> %s", url, fallback_url)
        result = await execute_fetch_url(
            fallback_url,
            redirect_depth=redirect_depth + 1,
            start_time=start_time,
        )
        if not result.startswith("失败："):
            # 同时缓存原始根路径，后续相同请求不再重复经历失败链路。
            set_fetch_cache(url, result)
            return result
        logger.info("[fetch_url] 首页回退失败：%s", fallback_url)
    return None


async def execute_fetch_url(url: str, redirect_depth: int = 0, start_time: float = None) -> str:
    # 先检查缓存（避免 SSRF 校验浪费），再做 SSRF 校验
    cached = get_fetch_cache(url)
    if cached is not None:
        logger.debug(f"Fetch cache hit for {url}")
        return cached
    # SSRF 防护：先校验 URL（含异步 DNS 解析）
    ok, reason = await _is_safe_url_to_fetch(url)
    if not ok:
        logger.warning(f"fetch_url 拒绝不安全 URL: {url} ({reason})")
        return f"失败：拒绝抓取不安全的 URL：{reason}"

    if start_time is None:
        # 使用 time.monotonic 而非 asyncio.get_event_loop().time()：
        # 后者在 Python 3.10+ 没有运行 loop 时会发出 DeprecationWarning，
        # 且与 time.monotonic 不是同一个时钟。
        start_time = time.monotonic()
    # 总超时 30 秒
    if time.monotonic() - start_time > 30:
        result = f"失败：抓取超时（总时间 >30s）：{url}"
        return result

    if redirect_depth > 3:
        result = f"失败：重定向层次过深 (>{3})，已放弃：{url}"
        return result

    original_url = url

    # ---- 重试循环：最多尝试2次 ----
    for attempt in range(2):
        try:
            # 先用 curl_cffi 获取 HTML
            html = await _fetch_html_with_curl(url)
            if not html:
                # curl 失败：trafilatura 自带下载器兜底（拿到 HTML 后仍走富 HTML 提取）
                html = await _download_html_with_trafilatura(url)
            if not html:
                # 第一次尝试失败，等待后重试
                if attempt == 0:
                    logger.warning(f"fetch_url attempt {attempt+1} failed for {url}, retrying...")
                    await asyncio.sleep(1)
                    continue
                else:
                    fallback_result = await _try_root_url_fallback(
                        url, redirect_depth, start_time,
                    )
                    if fallback_result is not None:
                        return fallback_result
                    result = f"失败：无法获取页面内容：{url}"
                    return result

            # 获取标题（用于失败提示与展示兜底）
            title = _get_title_from_html(html)

            # 转 Telegram Rich HTML（CPU 密集，放线程池避免阻塞事件循环）。
            # 内容 + 内嵌视频/播放器/音频/图片 都在这一步提取。
            payload = await asyncio.to_thread(_build_rich_fetch_payload, url, html)
            if payload:
                set_fetch_cache(url, payload)
                return payload

            # ---- 检测 JavaScript 重定向 ----
            js_pattern = re.compile(r'window\.location\.href\s*=\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE)
            match = js_pattern.search(html)
            if match:
                new_url = urljoin(url, match.group(1))
                if new_url == url:
                    result = f"失败：页面重定向到自身，无法抓取：{url}"
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
                    return result
                logger.info(f"[fetch_url] 跟随 Meta Refresh: {original_url} -> {new_url}")
                result = await execute_fetch_url(new_url, redirect_depth + 1, start_time)
                set_fetch_cache(url, result)
                return result

            # 未提取到有效正文：仅根路径可继续尝试配置的同站点首页路径。
            fallback_result = await _try_root_url_fallback(
                url, redirect_depth, start_time,
            )
            if fallback_result is not None:
                return fallback_result
            result = f"失败：无法提取有效正文（标题：{title}）\n🔗 {url}"
            return result

        except asyncio.TimeoutError:
            if attempt == 0:
                logger.warning(f"fetch_url timeout (attempt {attempt+1}) for {url}, retrying...")
                await asyncio.sleep(1)
                continue
            else:
                result = f"失败：抓取超时，请稍后重试：{url}"
                return result
        except Exception as e:
            logger.error(f"fetch_url unexpected error (attempt {attempt+1}): {e}")
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            else:
                result = f"失败：抓取异常，请稍后重试：{url}"
                return result

    # 如果循环结束仍未返回（理论上不会）
    result = f"失败：多次尝试均失败：{url}"
    return result


# --------------------- wikipedia ---------------------
async def execute_wikipedia(query: str, lang: str = "zh") -> str:
    """Wikipedia 关键词查询 → 忠实原文结构的 Telegram Rich HTML。

    链路：
      1. list=search 把关键词解析为最匹配的页面（web_search+fetch_url 需要
         两轮才能做到，且不保证维基百科排第一）；
      2. action=parse 获取该页面的完整解析后 HTML（MediaWiki API 并非只有
         纯文本：prop=extracts&explaintext 才是纯文本摘要；action=parse 的
         prop=text 返回含表格/列表/图片的完整 HTML，比抓取网页更稳定）；
      3. 复用 fetch_url 的富提取管线（trafilatura 结构化提取 + 媒体原位 +
         预算感知压缩），结果格式与 fetch_url 完全一致，模型可同样复用其中
         的 <img>/<a> 等片段；
      4. parse 失败或富转换提不出内容时，退化为旧的纯文本摘要路径。
    """
    try:
        from apitelegramchat.fetch_rich_content import build_model_facing_html
    except Exception as e:
        logger.error(f"[wikipedia] fetch_rich_content 导入失败: {e}")
        build_model_facing_html = None

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

                # ---- 主路径：action=parse 完整 HTML → 富管线 ----
                if build_model_facing_html is not None:
                    try:
                        parse_resp = await session.get(
                            f"https://{l}.wikipedia.org/w/api.php",
                            params={
                                "action": "parse", "pageid": page_id, "prop": "text|displaytitle",
                                "redirects": 1, "disablelimitreport": 1, "disableeditsection": 1,
                                "disabletoc": 1, "format": "json", "utf8": 1,
                            },
                            headers={"Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                            impersonate="chrome120", timeout=CURL_TIMEOUT
                        )
                        if parse_resp.status_code == 200:
                            parse_data = (parse_resp.json() or {}).get("parse", {}) or {}
                            page_html = ((parse_data.get("text") or {}).get("*") or "").strip()
                            title = (parse_data.get("title") or results[0].get("title") or query).strip()
                            if page_html:
                                page_url = f"https://{l}.wikipedia.org/wiki/{quote(title)}"
                                # CPU 密集转换放到线程池，不阻塞事件循环
                                # （与 _build_rich_fetch_payload 同一调度方式）。
                                rich = await asyncio.to_thread(
                                    build_model_facing_html, page_url, page_html, None, title
                                )
                                if rich:
                                    return rich
                    except Exception as e:
                        logger.debug(f"[wikipedia] 富 HTML 路径失败（回退纯文本摘要）: {e}")

                # ---- 退化路径：纯文本摘要（历史行为）----
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
                page = next(iter(pages.values()), {})
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
                # 强制 float 转换：上游 API 偶尔返回字符串（如 "0.1234"），
                # 直接 :.4f 会抛 ValueError 被 outer except 吞成"汇率查询出错"。
                try:
                    rate_val = float(rates[cur])
                except (TypeError, ValueError):
                    continue
                lines.append(f"1 {base} = {rate_val:.4f} {cur}")
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
        lines = [f"<b>书籍查询结果：「{escape_html(query)}」</b><br/>"]
        for i, doc in enumerate(docs[:5], 1):
            title = escape_html(doc.get("title", "无标题"))
            authors = escape_html("、".join(doc.get("author_name", ["未知作者"])[:3]))
            year = escape_html(str(doc.get("first_publish_year", "未知")))
            subjects = escape_html("、".join(doc.get("subject", [])[:3]))
            key = doc.get("key", "")
            ol_url = f"https://openlibrary.org{key}" if key else ""
            ol_url_html = escape_html(ol_url) if ol_url else ""
            lines.append(f"{i}. 《{title}》<br/>   作者：{authors}<br/>   首次出版：{year} 年<br/>" + (f"   主题：{subjects}<br/>" if subjects else "") + (f"   详情：{ol_url_html}<br/>" if ol_url_html else ""))
        return "<br/>".join(lines)
    except Exception as e:
        return f"失败：书籍查询出错：{str(e)[:100]}"


# --------------------- weather ---------------------
async def execute_weather(city: str, unit: str = "c", hours: int = 6) -> str:
    """查询 wttr.in 天气并打包为 JSON。

    注意：本函数返回的是「完整数据」（UI 折叠面板的月相/露点等展示依赖它）。
    hours 参数不在这一层生效 —— 发给模型的逐时条数与字段白名单由
    tool_result_condense.condense_for_model 的 weather 视图控制
    （默认 6 条，与工具 schema 的 hours 参数一致）。
    """
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

                current = (data.get("current_condition") or [{}])[0]
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

                first_day = (data.get("weather") or [{}])[0]
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
            lines.append(f'<li><b>{escape_html(title)}</b> (<i>{escape_html(src.upper())}</i>) <a href="{escape_html(link)}">🔗 阅读原文</a></li>')
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
            lines.append(f'<li><b>{escape_html(item.title)}</b> <a href="{escape_html(item.link)}">🔗 阅读原文</a></li>')
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
    # 安全修复：coin / currency 直接来自 LLM 工具调用参数，若不 quote
    # 就拼到 URL，LLM 可能传 "btc&ids=ethereum" 之类的字符串做参数注入。
    # 这里强制白名单（coin_id 只允许字母数字和连字符），currency 同理。
    coin_raw = (coin or "").lower().strip()
    currency_raw = (currency or "usd").lower().strip() or "usd"
    if not re.match(r'^[a-z0-9-]+$', coin_raw):
        return f"失败：币种标识 {coin!r} 包含非法字符。"
    if not re.match(r'^[a-z]{3}$', currency_raw):
        return f"失败：货币代码 {currency!r} 必须是 3 个小写字母。"
    coin_id = COIN_MAP.get(coin_raw, coin_raw)
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={quote(coin_id, safe='')}&vs_currencies={quote(currency_raw, safe='')}"
    )
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
        return f"✅ 二维码生成成功\n内容：{text[:200]}\n图片链接：{url}"
    else:
        return "失败：R2 上传失败，请检查配置。"


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
    parts = [f"❌ {api_name} 请求失败"]
    if status_code:
        parts.append(f"HTTP 状态：{status_code}")
    if model:
        parts.append(f"模型：{model}")
    if request_id:
        parts.append(f"Request ID：{request_id}")
    if detail:
        clean = detail.strip().replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in clean.split("\n") if line.strip()]
        clean = "<br/>".join(line for line in lines)
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
            prompt=prompt, duration=duration, model=model,
        )
    elif provider == "openrouter":
        video_url, error, video_meta = await _request_openrouter_video(
            prompt=prompt, duration=duration, model=model,
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
    """调用 amap-maps MCP 服务的某个工具，返回清洗后的纯文本。

    出错时返回 JSON 错误信息（{"status": "error", "message": ...}），让上层
    工具的调用方（LLM / format_tool_result）能直接看到失败原因。

    成功输出在返回前经过 condense_amap_payload 清洗：删除 polyline / tmcs
    等导航渲染专用的大体积字段与全部空值字段。这是“源头清洗”而非仅在
    发给 LLM 前过滤——polyline 坐标串可达几十 KB，若不先清洗，
    _truncate_tool_result 的 20k token 预算会被坐标串吃光，把 POI
    名称/地址/导航步骤等真正有用的字段挤出模型视野。UI 渲染层
    （_render_poi_cards / _render_map_route_card 等）只依赖保留名单内
    的字段，清洗对用户可见的展示零影响。
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
    return condense_amap_payload(raw)


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
                # 与 _call_amap_mcp 同源的清洗策略（见其 docstring）。
                return condense_amap_payload(raw)
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
# _geocode_coords 已删除：全仓库无任何调用方（旧 amap_integration 集成期的
# 遗留函数）。地址→坐标统一走 execute_geocode / maps_geo 工具链路。


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

async def persist_workspace_file(
    chat_id: int,
    rel_path: str,
    *,
    delete: bool = False,
    namespace: str | None = None,
) -> dict[str, str | bool]:
    """Persist exactly one file edited by text_editor.

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
