"""Telegram Rich Message 草稿的增量构建、HTML 边界扫描与滚动切换。

从 ai_handlers.py 拆分而来，逻辑未做改动。
"""
import asyncio
import re
import random
import html
import os
import time
from typing import List, Optional

from apitelegramchat.config import STREAM_FLUSH_INTERVAL, STREAM_SILENT_FORCE_FLUSH
from apitelegramchat.token_utils import count_tokens, truncate_to_tokens
from apitelegramchat.utils import (
    send_rich_message_draft,
    send_rich_html_message,
    get_logger,
    delete_message_fast,
    mark_draft_dead,
    RateLimitError,
    escape_html,
)
from apitelegramchat.ai.error_formatting import extract_domain
from apitelegramchat.ai.attachment_content import _track_task
from apitelegramchat.ai.tool_summary import (
    _coerce_positive_int,
    _generate_action_description,
    _get_tool_description_from_args,
)
import apitelegramchat.state as state

logger = get_logger(__name__)

# ---------- Telegram Rich Message 草稿滚动 ----------
# Telegram Rich Message 的服务端硬限制仍以解析后的 Unicode 字符计量；这是
# 外部协议约束而非项目预算。项目内部的滚动/交互预算统一以 token 计量，
# 同时保留一个略低于协议上限的字符安全阈值，防止英文等低 token 密度文本超限。
TELEGRAM_RICH_VISIBLE_CHAR_LIMIT = 32768

def _positive_env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default

RICH_MESSAGE_TOKEN_BUDGET = _positive_env_int("RICH_MESSAGE_TOKEN_BUDGET", 7500)
RICH_DRAFT_ROLLOVER_TOKEN_BUDGET = max(1, min(
    RICH_MESSAGE_TOKEN_BUDGET - 64,
    _positive_env_int("RICH_DRAFT_ROLLOVER_TOKEN_BUDGET", 6800),
))
RICH_DRAFT_ARM_TOKEN_BUDGET = min(
    RICH_DRAFT_ROLLOVER_TOKEN_BUDGET,
    _positive_env_int("RICH_DRAFT_ARM_TOKEN_BUDGET", 6200),
)
RICH_DRAFT_INTERACTIVE_TOKEN_BUDGET = min(
    RICH_DRAFT_ARM_TOKEN_BUDGET,
    _positive_env_int("RICH_DRAFT_INTERACTIVE_TOKEN_BUDGET", 5000),
)
RICH_DRAFT_HARD_GUARD_TOKEN_BUDGET = max(
    RICH_DRAFT_ROLLOVER_TOKEN_BUDGET + 1,
    RICH_MESSAGE_TOKEN_BUDGET - 128,
)
RICH_MESSAGE_BLOCKS_MAX = _positive_env_int("RICH_MESSAGE_BLOCKS_MAX", 80)
RICH_DRAFT_ROLLOVER_BLOCKS = min(
    RICH_MESSAGE_BLOCKS_MAX - 1,
    _positive_env_int("RICH_DRAFT_ROLLOVER_BLOCKS", 70),
)
RICH_DRAFT_ARM_BLOCKS = min(
    RICH_DRAFT_ROLLOVER_BLOCKS,
    _positive_env_int("RICH_DRAFT_ARM_BLOCKS", 60),
)
RICH_DRAFT_INTERACTIVE_BLOCKS = min(
    RICH_DRAFT_ARM_BLOCKS,
    _positive_env_int("RICH_DRAFT_INTERACTIVE_BLOCKS", 45),
)

_RICH_HTML_TAG_RE = re.compile(r"<!--.*?-->|<[^>]*>", re.DOTALL)
_RICH_TAG_NAME_RE = re.compile(r"^<\s*(/)?\s*([A-Za-z][\w:-]*)")
# Rich Message 的 details、列表和表格等容器不能只承载裸文本；服务端会将其
# 判为没有有效内容并返回 RICH_MESSAGE_CONTENT_REQUIRED。内联样式标签不算块。
_RICH_BLOCK_OPEN_TAG_RE = re.compile(
    r"<\s*(?:p|h[1-6]|pre|blockquote|details|ul|ol|li|table|thead|tbody|tfoot|tr|hr|"
    r"figure|figcaption|tg-slideshow|tg-map|img|video|audio|tg-math-block|aside|footer)\b",
    re.IGNORECASE,
)
# 工具 UI 详情截断时必须原样保留的标签：图片/视频/音频（自闭合或成对）以及
# 完整的 <a href="URL">文本</a>（下载/查看链接）。这些标签内的 URL 一旦被
# 通用的“剥标签 + 转义纯文本”截断逻辑处理，就会从可点击链接/可渲染媒体
# 退化成一段转义后的 URL 文本，导致用户实际访问的不是原始 URL。
_MEDIA_OR_LINK_TAG_RE = re.compile(
    r'<(?:img|video|audio)\b[^>]*/?>(?:</(?:video|audio)\s*>)?|'
    r'<a\b[^>]*href\s*=\s*(?:"[^"]*"|\'[^\']*\')[^>]*>.*?</a\s*>',
    re.IGNORECASE | re.DOTALL,
)


def _rich_visible_text(text: str) -> str:
    """按 Rich Message 的近似语义计算解析后的可见文本，不计 HTML 标签和属性。"""
    if not text:
        return ""
    return html.unescape(_RICH_HTML_TAG_RE.sub("", text))


def _ensure_rich_block_content(fragment: str) -> str:
    """为只有裸文本或内联标签的片段补上 Rich Message 所需的块级容器。

    模型的推理、工具详情和最终文本均可能是普通文字或仅含 ``<b>``、``<i>``
    等内联标签。该形态在浏览器中可显示，但 Telegram Rich Message API 会拒绝
    嵌入在 ``<details>`` 中的此类内容。已有任意 Rich 块时保持原样，避免破坏
    表格、列表、媒体等有效结构。
    """
    content = (fragment or "").strip()
    if not content:
        return ""
    if _RICH_BLOCK_OPEN_TAG_RE.search(content):
        return content
    return f"<p>{content}</p>"


def _scan_rich_html_boundaries(html_content: str) -> tuple[list[tuple[int, int, int, int]], int, int, int]:
    """返回完整最外层块边界：(源码位置、token 数、可见字符数、结构块数)。"""
    content = html_content or ""
    boundaries: list[tuple[int, int, int, int]] = []
    open_tags: list[str] = []
    visible_text_parts: list[str] = []
    visible_chars = 0
    block_count = 0
    cursor = 0

    def snapshot(position: int) -> None:
        visible_text = html.unescape("".join(visible_text_parts))
        boundaries.append((position, count_tokens(visible_text), len(visible_text), block_count))

    for match in _RICH_HTML_TAG_RE.finditer(content):
        text_part = html.unescape(content[cursor:match.start()])
        visible_text_parts.append(text_part)
        visible_chars += len(text_part)
        token = match.group(0)
        cursor = match.end()
        if token.startswith("<!--"):
            continue
        parsed = _RICH_TAG_NAME_RE.match(token)
        if not parsed:
            continue
        is_close = bool(parsed.group(1))
        tag = parsed.group(2).lower()
        is_self_closing = token.rstrip().endswith("/>") or tag in _RICH_VOID_TAGS

        if is_close:
            for idx in range(len(open_tags) - 1, -1, -1):
                if open_tags[idx] == tag:
                    del open_tags[idx:]
                    break
            if tag in _RICH_COUNTED_BLOCK_TAGS:
                block_count += 1
            if not open_tags:
                snapshot(match.end())
        elif is_self_closing:
            if tag in _RICH_COUNTED_BLOCK_TAGS:
                block_count += 1
            if not open_tags:
                snapshot(match.end())
        else:
            open_tags.append(tag)

    tail = html.unescape(content[cursor:])
    visible_text_parts.append(tail)
    visible_chars += len(tail)
    if not open_tags and content.strip() and (not boundaries or boundaries[-1][0] != len(content)):
        snapshot(len(content))
    return boundaries, count_tokens("".join(visible_text_parts)), visible_tokens, block_count


async def _swallow_flush_task(t: "asyncio.Task", name: str, draft_id: int) -> None:
    """后台监听 flush 子 task 的结束，仅用于日志。绝不阻塞调用方。"""
    try:
        await t
    except asyncio.CancelledError:
        logger.debug(f"{name} 已取消: draft_id={draft_id}")
    except Exception as e:
        logger.debug(f"{name} 异常（可忽略）: draft_id={draft_id} {e}")


class RichMessageBuilder:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        # draft_id 必须在 2^53 (9007199254740992) 以内，否则 JSON 双精度浮点解析会丢失精度，
        # 导致服务端把同一次请求视为不同草稿，出现两个草稿同时更新的 bug。
        self.draft_id: int = int(time.time() * 1000000) + random.randint(0, 999)
        self.draft_message_id: Optional[int] = None
        self.blocks: List[str] = []
        self.block_types: List[str] = []
        self._tool_groups = []
        self._current_group_idx = -1
        self._stream_buffer: str = ""
        self._stream_text_index: int = -1
        self._last_flush_time: float = time.monotonic()
        self._flush_sequence: int = 0
        # 每次内容变更递增。刷新完成时仅在版本未变化的情况下清除 dirty，
        # 从而既不会漏掉在途更新，也不会重复发送已由显式 flush 覆盖的同一帧。
        self._flush_revision: int = 0
        self._flush_task: Optional[asyncio.Task] = None
        self._pending_flush_task: Optional[asyncio.Task] = None
        # request_flush 合并高频更新时，若网络发送仍在进行，新内容不能仅靠
        # “已有 pending task”被吞掉；该标记保证当前帧结束后必补发最新状态。
        self._flush_dirty: bool = False
        self._stop_flush = False
        self._thinking_removed: bool = False
        self._pending_reasoning_html: str = ""
        self._flush_lock = asyncio.Lock()
        self._rollover_lock = asyncio.Lock()
        self._rate_limited_until: float = 0.0
        self._force_flush_requested: bool = False
        # 容量预警与实际切换严格分离：flush 只能置位 pending，只有完成一整轮
        # 模型返回/工具批次后才能 await rollover_at_turn_boundary()。
        self._rollover_pending = False
        self._rollover_in_progress = False
        # 防御性兜底：理论上回合边界期间不再有模型增量；若调用方违约，增量进入
        # handoff 缓冲，永久化完成后随新草稿写入，绝不能被旧 remainder 快照覆盖。
        self._handoff_text: list[str] | None = None
        # 每一次滚动均记录旧草稿、新草稿和永久消息 ID，既用于诊断，也确保最终
        # 收尾只处理当前草稿而不会重复发送已完成的段落。
        self._rollover_history: list[dict[str, int | str | None]] = []
        self._rollover_count: int = 0

    def _get_reasoning_summary(self, content: str) -> str:
        """从包含 HTML 标签的思考内容中提取纯文本摘要，长度不超过 30 字符"""
        if not content:
            return "思考中…"
        plain = re.sub(r'<[^>]+>', '', content).strip()
        if not plain:
            return "思考中…"
        if len(plain) > 30:
            return plain[:30] + "…"
        return plain

    def request_flush(self, force: bool = False) -> None:
        """异步触发刷新，确保在途发送期间的新内容一定会补发。"""
        if self._stop_flush:
            return
        # 无论是否已有在途 flush，只要状态发生变化，都记录为脏数据。旧逻辑在
        # pending task 存在时直接 return，会把发送期间的新增工具状态/流式文本
        # 合并掉；若之后没有新的 request_flush，用户便会看到草稿长时间不动。
        self._flush_dirty = True
        self._flush_revision += 1
        if force:
            self._force_flush_requested = True
        if self._pending_flush_task and not self._pending_flush_task.done():
            return

        async def _runner():
            try:
                while not self._stop_flush:
                    force_now = self._force_flush_requested
                    self._force_flush_requested = False
                    # 显式 flush 可能已在当前 task 启动前把该版本发送出去；此时
                    # 不重复发送，而是只等待下一次真实内容变更。
                    if not self._flush_dirty and not force_now:
                        break
                    revision_before_send = self._flush_revision
                    await self.flush(force=force_now)
                    # 发送过程没有新版本则结束；有新版本则立即补发最新状态。
                    if (
                        self._flush_revision <= revision_before_send
                        or time.monotonic() < self._rate_limited_until
                    ):
                        break
            finally:
                self._pending_flush_task = None
                # 仅在不处于本地限流冷却时补排；冷却期由全局刷新循环等待后续发送，
                # 防止失败帧触发紧密自旋。
                if (
                    (self._flush_dirty or self._force_flush_requested)
                    and not self._stop_flush
                    and time.monotonic() >= self._rate_limited_until
                ):
                    self.request_flush(force=self._force_flush_requested)

        try:
            self._pending_flush_task = asyncio.create_task(_runner())
        except RuntimeError:
            self._pending_flush_task = None

    # ---------- 工具组管理 ----------
    def start_new_tool_group(self) -> int:
        self._commit_stream_buffer()
        if self._stream_text_index >= 0:
            self.end_stream()
        idx = len(self.blocks)
        self.blocks.append("")
        self.block_types.append("tool_group")
        group = {
            "items": [],
            "placeholder_idx": idx,
            "outer_summary": "",
            "finished": False,
            "reasoning_html": self._pending_reasoning_html,
            "text_content": "",
        }
        self._pending_reasoning_html = ""
        self._tool_groups.append(group)
        self._current_group_idx = len(self._tool_groups) - 1
        self.request_flush(force=False)
        return self._current_group_idx

    def _get_current_group(self):
        for idx in range(len(self._tool_groups) - 1, -1, -1):
            if not self._tool_groups[idx].get("finished", False):
                self._current_group_idx = idx
                return self._current_group_idx
        return self.start_new_tool_group()

    def add_tool_item(self, tool_id: str, tool_type: str, summary: str,
                      action_description: str = None,
                      search_query: str = None, domain: str = None,
                      fn_args: dict = None):
        group_idx = self._get_current_group()
        group = self._tool_groups[group_idx]

        new_summary = summary
        # web_search 的单工具进行态摘要就是搜索词；不要再生成 Search for ...。
        # fetch_url 则按规范显示目标域名。
        if not _get_tool_description_from_args(fn_args or {}) and domain:
            new_summary = f"Fetching from {domain}"

        for item in group["items"]:
            if item["id"] == tool_id:
                if search_query:
                    item["search_query"] = search_query
                if domain:
                    item["domain"] = domain
                if action_description:
                    item["action_description"] = action_description
                if fn_args:
                    item["fn_args"] = fn_args
                item["summary"] = new_summary
                self._refresh_outer_summary(group)
                self.request_flush(force=False)
                return

        item = {
            "id": tool_id,
            "type": tool_type,
            "summary": new_summary,
            "details_html": "",
            "status": "running",
            "search_query": search_query,
            "domain": domain,
            "action_description": action_description,
            "fn_args": fn_args or {},  # 存储参数
        }
        group["items"].append(item)
        self._refresh_outer_summary(group)
        self.request_flush(force=False)

    def update_tool_item(self, tool_id: str, summary: str, details_html: str, status: str = "done"):
        for group in self._tool_groups:
            for item in group["items"]:
                if item["id"] == tool_id:
                    item["summary"] = summary
                    item["details_html"] = details_html
                    item["status"] = status
                    self._refresh_outer_summary(group)
                    self.request_flush(force=False)
                    return

    def update_tool_preview(self, tool_id: str, preview_html: str, summary: str = None):
        for group in self._tool_groups:
            for item in group["items"]:
                if item["id"] == tool_id:
                    if summary and item["summary"] != summary:
                        item["summary"] = summary
                        self._refresh_outer_summary(group)
                    item["details_html"] = preview_html
                    self.request_flush(force=False)
                    return

    def append_to_current_tool_group_text(self, text: str):
        if self._handoff_text is not None:
            self._handoff_text.append(text)
            return
        group_idx = self._get_current_group()
        if group_idx < 0:
            return
        group = self._tool_groups[group_idx]
        group["text_content"] += text
        self.request_flush(force=False)

    # ---- 修改点3：_refresh_outer_summary（工具组进行时，规范第二部分） ----
    def _refresh_outer_summary(self, group: dict):
        """
        刷新工具组的外部摘要（进行时状态）
        优先使用自定义 _description，否则使用规范中的进行时固定文本。
        """
        if group.get("finished", False):
            group["outer_summary"] = self._generate_group_summary(group)
            self.request_flush(force=False)
            return

        items = group.get("items", [])
        if not items:
            group["outer_summary"] = ""
            self.request_flush(force=False)
            return

        active_items = [it for it in items if it["status"] in ("running", "waiting")]
        target = active_items[-1] if active_items else items[-1]
        t = target["type"]
        fn_args = target.get("fn_args", {})

        # web_search 工具组进行态固定为 Searching the web。
        if t == "web_search":
            group["outer_summary"] = "Searching the web"
            self.request_flush(force=False)
            return

        custom_desc = _get_tool_description_from_args(fn_args)
        if custom_desc:
            group["outer_summary"] = custom_desc
            self.request_flush(force=False)
            return

        # ---------- 按规范进行时文本 ----------
        elif t == "fetch_url":
            url = (fn_args.get("url") or "").strip()
            domain = extract_domain(url) if url else ""
            group["outer_summary"] = f"Fetching from {domain}" if domain else "Fetching a page"
        elif t == "bash":
            cmd = (fn_args.get("command") or "").strip()
            if cmd:
                short = cmd[:30] + "..." if len(cmd) > 30 else cmd
                group["outer_summary"] = short
            else:
                group["outer_summary"] = "Running command"
        elif t == "text_editor":
            command = fn_args.get("command", "")
            # 进行时只显示动作，不显示路径
            mapping = {
                "view": "Viewing file",
                "create": "Creating file",
                "str_replace": "Replacing exact text",
                "insert": "Inserting text",
            }
            group["outer_summary"] = mapping.get(command, "Editing file")
        elif t == "present_files":
            group["outer_summary"] = "Presenting file(s)"
        elif t == "fetch_download":
            group["outer_summary"] = "Fetching from download/"
        elif t == "stage_upload":
            group["outer_summary"] = "Staging to upload/"
        elif t == "list_download":
            group["outer_summary"] = "Listing download/"
        elif t == "list_upload":
            group["outer_summary"] = "Listing upload/"
        elif t == "ask_user":
            group["outer_summary"] = "Waiting for your answer"
        elif t == "wikipedia":
            group["outer_summary"] = "Looking up on Wikipedia"
        elif t == "news":
            group["outer_summary"] = "Fetching news"
        elif t == "book_lookup":
            group["outer_summary"] = "Looking up a book"
        elif t == "ip_geo":
            group["outer_summary"] = "Looking up IP location"
        elif t == "geocode":
            group["outer_summary"] = "Geocoding address"
        elif t == "route":
            group["outer_summary"] = "Planning route"
        elif t == "distance":
            group["outer_summary"] = "Measuring distance"
        elif t == "poi_keyword_search":
            group["outer_summary"] = "Searching POI by keyword"
        elif t == "poi_nearby_search":
            group["outer_summary"] = "Searching nearby POI"
        elif t == "poi_details":
            group["outer_summary"] = "Fetching POI details"
        elif t == "exchange_rate":
            group["outer_summary"] = "Checking exchange rates"
        elif t == "crypto_price":
            group["outer_summary"] = "Fetching crypto prices"
        elif t == "weather":
            group["outer_summary"] = "Fetching weather"
        elif t == "qr_code":
            group["outer_summary"] = "Generating QR code"
        elif t == "generate_image_from_text":
            num_images = _coerce_positive_int(fn_args.get("num_images"), 1)
            if num_images == 1:
                group["outer_summary"] = "Generating an image"
            else:
                group["outer_summary"] = f"Generating {num_images} images"
        elif t == "edit_image_with_reference":
            num_images = _coerce_positive_int(fn_args.get("num_images"), 1)
            if num_images == 1:
                group["outer_summary"] = "Editing an image"
            else:
                group["outer_summary"] = f"Editing {num_images} images"
        else:
            action = target.get("action_description") or _generate_action_description(t, fn_args)
            group["outer_summary"] = action.capitalize() + "..." if action else "Running..."

        self.request_flush(force=False)

    # ---- 修改点4：_generate_group_summary（工具组结束态，规范第一部分） ----
    # 工具组摘要的固定描述模板（单数/复数）
    # 键为组类型（字符串），值为 (单数模板, 复数模板) 或直接为固定字符串（不区分单复数）
    # 使用 {n} 占位符表示数量
    _GROUP_SUMMARY_TEMPLATES = {
        "web_search": ("Searched the web", "Searched the web"),
        "bash": ("Ran a command", "Ran {n} commands"),
        "text_editor_view": ("Viewed a file", "Viewed {n} files"),
        "text_editor_edit": ("Edited a file", "Edited {n} files"),
        "text_editor_create": ("Created a file", "Created {n} files"),
        "text_editor_delete": ("Deleted a file", "Deleted {n} files"),
        "present_files": ("Presented a file", "Presented {n} files"),
        "fetch_download": ("Fetched a file from download/", "Fetched {n} files from download/"),
        "stage_upload": ("Staged a file to upload/", "Staged {n} files to upload/"),
        "list_download": ("Listed download/", "Listed download/"),
        "list_upload": ("Listed upload/", "Listed upload/"),
        "wikipedia": ("Looked up on Wikipedia", "Looked up on Wikipedia"),
        "news": ("Fetched news", "Fetched news from {n} sources"),
        "fetch_url": ("Fetched a page", "Fetched {n} pages"),
        "book_lookup": ("Looked up a book", "Looked up {n} books"),
        "ip_geo": ("Looked up IP location", "Looked up IP location"),
        "geocode": ("Geocoded an address", "Geocoded {n} addresses"),
        "nearby_search": ("Searched nearby", "Searched nearby for {n} categories"),
        "route": ("Planned a route", "Planned {n} routes"),
        "distance": ("Measured a distance", "Measured a distance"),
        "poi_keyword_search": ("Searched POIs by keyword", "Searched POIs by keyword"),
        "poi_nearby_search": ("Searched nearby POIs", "Searched nearby POIs"),
        "poi_details": ("Fetched POI details", "Fetched details for {n} POIs"),
        "exchange_rate": ("Checked exchange rates", "Checked exchange rates"),
        "crypto_price": ("Fetched crypto prices", "Fetched price for {n} coins"),
        "public_holidays": ("Looked up holidays", "Looked up holidays for {n} countries"),
        "weather": ("Fetched weather", "Fetched weather for {n} cities"),
        "convert": ("Calculated a result", "Ran {n} calculations"),
        "qr_code": ("Generated a QR code", "Generated {n} QR codes"),
        "generate_image_from_text": ("Generated an image", "Generated {n} images"),
        "edit_image_with_reference": ("Edited an image", "Edited {n} images"),
        "ask_user": ("Asked you a question", "Asked you questions"),
    }

    def _get_group_type_for_item(self, item: dict) -> str:
        t = item.get("type", "unknown")
        if t == "text_editor":
            command = item.get("fn_args", {}).get("command", "")
            if command == "view":
                return "text_editor_view"
            if command == "create":
                return "text_editor_create"
            if command == "delete":
                return "text_editor_delete"
            return "text_editor_edit"
        return t

    def _generate_group_summary(self, group: dict) -> str:
        """完成态工具组摘要：只统计成功工具；同类工具只展示一次，顺序按首次成功调用。"""
        done_items = [it for it in group.get("items", []) if it.get("status") == "done"]
        if not done_items:
            return ""
        type_order = []
        type_counts = {}
        for item in done_items:
            gtype = self._get_group_type_for_item(item)
            if gtype not in type_counts:
                type_order.append(gtype)
                type_counts[gtype] = 0
            type_counts[gtype] += 1
        descs = []
        for gtype in type_order:
            count = type_counts[gtype]
            singular, plural = self._GROUP_SUMMARY_TEMPLATES.get(gtype, ("Ran an action", "Ran {n} actions"))
            desc = singular if count == 1 else plural.format(n=count)
            descs.append(desc[:1].upper() + desc[1:] if desc else desc)
        return ", ".join(descs)

    # ---- 修改点5：finish_group 增加默认标题 ----
    def finish_group(self, group_idx: int = None):
        if group_idx is None:
            group_idx = len(self._tool_groups) - 1
        if group_idx < 0 or group_idx >= len(self._tool_groups):
            return
        group = self._tool_groups[group_idx]
        if group.get("finished", False):
            return
        group["finished"] = True
        self._commit_stream_buffer()
        group["outer_summary"] = self._generate_group_summary(group)
        # 若所有工具均失败，设置一个默认标题
        if not group["outer_summary"]:
            group["outer_summary"] = "Tools failed"
        self.request_flush(force=False)

    # ---------- 思考块管理 ----------
    def remove_thinking(self) -> None:
        self._commit_stream_buffer()
        new_blocks = []
        new_types = []
        for b, t in zip(self.blocks, self.block_types):
            if t == "html" and b.startswith("<tg-thinking>"):
                continue
            new_blocks.append(b)
            new_types.append(t)
        self.blocks = new_blocks
        self.block_types = new_types
        self._thinking_removed = True
        self.request_flush(force=False)

    def remove_last_reasoning(self):
        for i in range(len(self.blocks) - 1, -1, -1):
            if self.block_types[i] == "reasoning":
                del self.blocks[i]
                del self.block_types[i]
                break

    def add_initial_thinking(self, text: str = "Thinking...") -> int:
        self._commit_stream_buffer()
        block = f"<tg-thinking>{escape_html(text)}</tg-thinking>"
        self.blocks.append(block)
        self.block_types.append("html")
        # 不在此处调用 request_flush，由 get_ai_response 中显式 await flush() 统一触发，
        # 避免与显式 flush 产生重复的 sendRichMessageDraft API 调用。
        return len(self.blocks) - 1

    def set_thinking_status(self, text: str, *, force: bool = True) -> bool:
        """更新首个仍存在的思考占位，使准备阶段也有可见进度。"""
        safe_text = escape_html((text or "Thinking...").strip() or "Thinking...")
        for index, (block, block_type) in enumerate(zip(self.blocks, self.block_types)):
            if block_type == "html" and block.startswith("<tg-thinking>"):
                updated = f"<tg-thinking>{safe_text}</tg-thinking>"
                if block != updated:
                    self.blocks[index] = updated
                    self.request_flush(force=force)
                return True
        return False

    def add_text(self, text: str):
        if not text or not text.strip():
            return
        if self._handoff_text is not None:
            self._handoff_text.append(text)
            return
        self._commit_stream_buffer()
        self.blocks.append(text)
        self.block_types.append("text")
        self._stream_text_index = -1
        self.request_flush(force=False)

    def replace_trailing_text(self, original: str, replacement: str = "") -> bool:
        """替换最近一个文本块的尾部，用于从草稿中撤回模型误输出的伪工具调用 XML。"""
        if not original:
            return False
        self._commit_stream_buffer()
        for idx in range(len(self.blocks) - 1, -1, -1):
            if self.block_types[idx] != "text":
                continue
            block = self.blocks[idx]
            if not block.endswith(original):
                continue
            updated = block[:-len(original)] + replacement
            if updated.strip():
                self.blocks[idx] = updated
            else:
                del self.blocks[idx]
                del self.block_types[idx]
                if self._stream_text_index == idx:
                    self._stream_text_index = -1
                elif self._stream_text_index > idx:
                    self._stream_text_index -= 1
            self.request_flush(force=False)
            return True
        for group in reversed(self._tool_groups):
            text_content = group.get("text_content", "")
            if not text_content.endswith(original):
                continue
            group["text_content"] = text_content[:-len(original)] + replacement
            self.request_flush(force=False)
            return True
        return False

    # ---------- 流式管理 ----------
    def begin_stream(self, stream_type: str = "text"):
        self._commit_stream_buffer()
        self.blocks.append("")
        self.block_types.append(stream_type)
        self._stream_text_index = len(self.blocks) - 1
        self._stream_buffer = ""
        self.request_flush(force=False)

    def begin_stream_text(self):
        self.begin_stream("text")

    def begin_stream_reasoning(self):
        self.begin_stream("reasoning")

    def append_stream_delta(self, delta: str):
        if not delta:
            return
        if self._handoff_text is not None:
            self._handoff_text.append(delta)
            return
        self._stream_buffer += delta
        # 流式增量未必每片都调用 request_flush；将其标记为新版本，可确保一旦
        # 当前发送结束，后台刷新不会把已累积的增量误认为已经展示。
        self._flush_dirty = True
        self._flush_revision += 1

    def _commit_stream_buffer(self):
        if self._stream_buffer and self._stream_text_index >= 0:
            self.blocks[self._stream_text_index] += self._stream_buffer
            self._stream_buffer = ""
        elif self._stream_buffer:
            self.blocks.append(self._stream_buffer)
            self.block_types.append("text")
            self._stream_buffer = ""

    def end_stream(self) -> str:
        self._commit_stream_buffer()
        if self._stream_text_index >= 0 and self._stream_text_index < len(self.blocks):
            text = self.blocks[self._stream_text_index]
        else:
            text = ""
        self._stream_text_index = -1
        return text

    def end_stream_text(self) -> str:
        return self.end_stream()

    def finalize_reasoning_block(self, has_tool_calls: bool = False):
        self._commit_stream_buffer()

    def _build_tool_group_html(self, group: dict) -> str:
        items = group.get("items", [])
        if not items:
            return ""

        outer_summary = (group.get("outer_summary", "") or "").strip()
        if not outer_summary:
            # 防御性兜底：正常情况下 _refresh_outer_summary / finish_group 总会
            # 写入一个非空摘要。如果由于某个未预见的路径（例如未来新增的状态值）
            # 仍然为空，绝不能让整组内容从渲染结果里直接消失——那样用户会看到
            # 草稿"卡住不动"，而实际上内容其实还在 builder 里，只是没被渲染。
            # 进行中的组用通用占位符，已结束的组按状态兜底展示。
            outer_summary = "Working..." if not group.get("finished", False) else "Tool activity"

        reasoning_html = group.get("reasoning_html", "")
        text_content = group.get("text_content", "")

        inner_parts = []
        if reasoning_html:
            inner_parts.append(_ensure_rich_block_content(reasoning_html))
        if text_content:
            inner_parts.append(_ensure_rich_block_content(text_content))

        # 工具详情直接渲染完整内容。展示层不再进行二次裁剪，避免
        # 搜索结果等长输出出现“工具输出已截断”。
        for item in items:
            inner_parts.append(self._get_inner_content(item))

        inner_html = "\n".join(inner_parts)
        return f"<details><summary>{outer_summary}</summary>\n{inner_html}\n</details>"

    def _get_inner_content(self, item: dict) -> str:
        inner_summary = item["summary"]
        if item["details_html"].strip():
            inner_body = item["details_html"]
            inner_body = _ensure_rich_block_content(inner_body)
            return f"<details><summary>{inner_summary}</summary>\n{inner_body}\n</details>"
        else:
            # 修复 RICH_MESSAGE_CONTENT_REQUIRED：
            # details_html 为空时（工具刚被 LLM 声明、args 还没流到），
            # 不能只返回裸 inner_summary 纯文本——外层 <details> 会变成
            # "只有纯文本、无块级子元素" 的结构，Telegram sendRichMessageDraft
            # 会返回 400 RICH_MESSAGE_CONTENT_REQUIRED。
            # 用 <p> 包一层保证块级内容。
            return f"<p>{inner_summary}</p>"

    # ========== 关键修改：_build_html 不再将 tool_group 合并到 reasoning 中 ==========
    def _build_html(self) -> str:
        html_parts = []
        i = 0
        group_idx = 0
        while i < len(self.blocks):
            b_type = self.block_types[i]
            block = self.blocks[i]

            if b_type == "reasoning":
                reasoning_content = block
                i += 1
                # 不再收集后续 tool_group，只渲染 reasoning 自身
                summary = self._get_reasoning_summary(reasoning_content)
                reasoning_body = _ensure_rich_block_content(reasoning_content)
                html_parts.append(f"<details><summary>{summary}</summary>\n{reasoning_body}\n</details>")
                continue

            elif b_type == "tool_group":
                if group_idx < len(self._tool_groups):
                    html_parts.append(self._build_tool_group_html(self._tool_groups[group_idx]))
                    group_idx += 1
                i += 1
                continue

            else:
                if b_type == "skip":
                    i += 1
                    continue
                content = block
                if i == self._stream_text_index:
                    content += self._stream_buffer
                if b_type == "text":
                    html_parts.append(content)
                elif b_type == "html":
                    html_parts.append(content)
                else:
                    html_parts.append(content)
                i += 1

        result = "".join(html_parts)
        return result if result.strip() else " "

    # ========== 关键修改：_build_html_no_thinking 同样修改 ==========
    def _build_html_no_thinking(self) -> str:
        html_parts = []
        i = 0
        group_idx = 0
        while i < len(self.blocks):
            b_type = self.block_types[i]
            block = self.blocks[i]

            if b_type == "html" and block.startswith("<tg-thinking>"):
                i += 1
                continue

            if b_type == "reasoning":
                reasoning_content = block
                i += 1
                # 不再收集后续 tool_group，只渲染 reasoning 自身
                summary = self._get_reasoning_summary(reasoning_content)
                reasoning_body = _ensure_rich_block_content(reasoning_content)
                html_parts.append(f"<details><summary>{summary}</summary>\n{reasoning_body}\n</details>")
                continue

            elif b_type == "tool_group":
                if group_idx < len(self._tool_groups):
                    html_parts.append(self._build_tool_group_html(self._tool_groups[group_idx]))
                    group_idx += 1
                i += 1
                continue

            else:
                if b_type == "skip":
                    i += 1
                    continue
                content = block
                if i == self._stream_text_index:
                    content += self._stream_buffer
                if b_type == "text":
                    html_parts.append(content)
                elif b_type == "html":
                    html_parts.append(content)
                else:
                    html_parts.append(content)
                i += 1

        result = "".join(html_parts)
        return result if result.strip() else " "

    # ---------- 容量预警与回合边界滚动 ----------
    @staticmethod
    def _plain_text_cut(text: str, limit_tokens: int) -> int:
        """在不超过 token 预算的前提下尽量停在空白或句末。"""
        if count_tokens(text) <= limit_tokens:
            return len(text)
        upper = len(truncate_to_tokens(text, limit_tokens, suffix=""))
        lower = max(1, int(upper * 0.80))
        candidates = [
            text.rfind('\n', lower, upper + 1),
            text.rfind('。', lower, upper + 1),
            text.rfind('！', lower, upper + 1),
            text.rfind('？', lower, upper + 1),
            text.rfind('. ', lower, upper + 1),
            text.rfind(' ', lower, upper + 1),
        ]
        return max([candidate for candidate in candidates if candidate > 0] or [upper])

    def _pick_rollover_boundary(self, html_content: str) -> tuple[int | None, int, int]:
        """选择完整最外层块结束位置；必要时允许在真实限制前的安全余量内提交。"""
        boundaries, visible_tokens, _visible_chars, block_count = _scan_rich_html_boundaries(html_content)
        preferred = None
        legal_complete_block = None
        legal_tokens = RICH_DRAFT_HARD_GUARD_TOKEN_BUDGET
        legal_chars = TELEGRAM_RICH_VISIBLE_CHAR_LIMIT - 512
        legal_blocks = max(RICH_DRAFT_ROLLOVER_BLOCKS, RICH_MESSAGE_BLOCKS_MAX - 1)
        for boundary in boundaries:
            _, tokens_at_boundary, chars_at_boundary, blocks_at_boundary = boundary
            if (tokens_at_boundary <= legal_tokens and chars_at_boundary <= legal_chars
                    and blocks_at_boundary <= legal_blocks):
                legal_complete_block = boundary
                if (tokens_at_boundary <= RICH_DRAFT_ROLLOVER_TOKEN_BUDGET
                        and blocks_at_boundary <= RICH_DRAFT_ROLLOVER_BLOCKS):
                    preferred = boundary
        selected = preferred or legal_complete_block
        return (selected[0] if selected else None), visible_tokens, block_count

    def _replace_with_rollover_remainder(self, remainder: str, handoff_text: str = "") -> None:
        """将未提交内容和交接期间的缓冲内容作为新草稿，并维持流式通道。"""
        remainder = remainder.lstrip()
        thinking = "<tg-thinking>Thinking...</tg-thinking>"
        self.blocks = [thinking]
        self.block_types = ["html"]
        if remainder:
            self.blocks.append(remainder)
            self.block_types.append("text")
        if handoff_text:
            self.blocks.append(handoff_text)
            self.block_types.append("text")
        self._tool_groups = []
        self._current_group_idx = -1
        self._stream_buffer = ""
        self._stream_text_index = -1

    async def _register_active_draft(self, message_id: int = 0) -> None:
        try:
            from apitelegramchat.state import set_active_draft
            await set_active_draft(self.chat_id, self.draft_id, message_id)
        except Exception as exc:
            logger.debug("更新活跃草稿状态失败: chat=%s draft=%s err=%s", self.chat_id, self.draft_id, exc)

    def _arm_rollover_if_needed(self, html_content: str | None = None) -> bool:
        """接近容量时只设置待切换标志，绝不在 flush 中创建后台滚动任务。"""
        if self._stop_flush or self._rollover_pending or self._rollover_in_progress:
            return self._rollover_pending
        if html_content is None:
            html_content = self._build_html_no_thinking()
        _cut_at, visible_tokens, block_count = self._pick_rollover_boundary(html_content)
        if (
            visible_tokens < RICH_DRAFT_INTERACTIVE_TOKEN_BUDGET
            and block_count < RICH_DRAFT_INTERACTIVE_BLOCKS
        ):
            return False
        self._rollover_pending = True
        logger.info(
            "草稿交互容量预警，下一完整回合边界滚动: chat=%s draft=%s tokens=%s blocks=%s "
            "interactive_tokens=%s interactive_blocks=%s api_arm_tokens=%s api_arm_blocks=%s",
            self.chat_id, self.draft_id, visible_tokens, block_count,
            RICH_DRAFT_INTERACTIVE_TOKEN_BUDGET, RICH_DRAFT_INTERACTIVE_BLOCKS,
            RICH_DRAFT_ARM_TOKEN_BUDGET, RICH_DRAFT_ARM_BLOCKS,
        )
        return True

    def _restore_handoff_text(self) -> None:
        """将异常退出的交接缓冲恢复到当前构建器，避免任何流式增量丢失。"""
        if self._handoff_text is None:
            return
        buffered = "".join(self._handoff_text)
        self._handoff_text = None
        if buffered:
            self.blocks.append(buffered)
            self.block_types.append("text")
            self._stream_text_index = -1

    async def rollover_at_turn_boundary(self, *, start_next_draft: bool = True) -> bool:
        """在完整模型返回/完整工具批次后，按回合去向完成草稿分段。

        调用方必须保证下一次模型请求尚未开始，且本轮并行工具均已得到最终状态。
        到达容量阈值后，本函数总会先永久化已完成的旧段；只有 ``start_next_draft``
        为真（即工具批次或纠错路径还会继续请求模型）时，才生成并刷新新草稿。
        对终局文本传入假值时，函数只结束旧草稿，保留尾段给统一最终发送路径提交。
        """
        if self._stop_flush or self._rollover_in_progress:
            return False

        # 回合边界是唯一允许换草稿的时点，因此必须在这里重新统计真实容量。
        # 不能只依赖 flush 之前留下的 _rollover_pending：draft API 限流冷却、
        # 刷新短路或未来调用路径遗漏 flush 时，旧实现会让已到 30k 的草稿跨越
        # 多个工具回合仍不切换。先提交流式缓冲，确保本轮最后一段也参与统计。
        self._commit_stream_buffer()
        async with self._rollover_lock:
            if self._stop_flush or self._rollover_in_progress:
                return False
            current_html = self._build_html_no_thinking()
            cut_at, visible_tokens, block_count = self._pick_rollover_boundary(current_html)
            rollover_due = (
                visible_tokens >= RICH_DRAFT_INTERACTIVE_TOKEN_BUDGET
                or block_count >= RICH_DRAFT_INTERACTIVE_BLOCKS
            )
            if not rollover_due:
                return False

            # 交互性能预警就是下一次完整工具回合边界的实际切换阈值。这样一旦
            # 草稿增长到可能拖慢客户端重绘的体量，紧随其后的工具批次收束后便会
            # 完成永久化和新草稿首帧，再发起下一次模型请求，绝不继续堆积数轮。
            # 即使此前的 flush 未运行或被限流跳过，也必须在本次边界切换。
            self._rollover_pending = True

            used_fallback = False
            if cut_at is not None:
                completed_html = current_html[:cut_at].strip()
                remainder = current_html[cut_at:]
            else:
                # 已到预警/切换阈值却没有完整 Rich Block 边界时，立即采用安全的
                # 纯文本分段。不能等待下一轮或 hard guard，否则新草稿会被拖延。
                plain = _rich_visible_text(current_html)
                text_cut = self._plain_text_cut(plain, RICH_DRAFT_ROLLOVER_TOKEN_BUDGET)
                completed_html = f"<p>{escape_html(plain[:text_cut].rstrip())}</p>"
                remainder = f"<p>{escape_html(plain[text_cut:].lstrip())}</p>"
                used_fallback = True

            if not completed_html or not _rich_visible_text(completed_html).strip():
                return False

            old_draft_id = self.draft_id
            old_draft_message_id = self.draft_message_id
            self._rollover_in_progress = True
            self._handoff_text = []

        try:
            # 此处被调用在回合边界；等待永久消息期间不会启动下一次模型请求。
            completed_message_id = await send_rich_html_message(
                self.chat_id,
                completed_html,
                reassert_draft=False,
            )
            if not completed_message_id:
                self._restore_handoff_text()
                self._rollover_in_progress = False
                logger.warning(
                    "草稿边界滚动永久化失败，保留当前草稿重试: chat=%s draft=%s tokens=%s blocks=%s",
                    self.chat_id, old_draft_id, visible_tokens, block_count,
                )
                return False

            if self._stop_flush:
                self._restore_handoff_text()
                self._rollover_in_progress = False
                return False

            # 先停止旧草稿，再立即登记并发送新草稿。删除旧预览是后台清理，绝不阻塞新首帧。
            await mark_draft_dead(old_draft_id)
            async with self._rollover_lock:
                if self._stop_flush or self.draft_id != old_draft_id:
                    self._restore_handoff_text()
                    self._rollover_in_progress = False
                    return False

                handoff_text = "".join(self._handoff_text or [])
                self._handoff_text = None
                self.draft_message_id = None
                self._rate_limited_until = 0.0
                self._replace_with_rollover_remainder(remainder, handoff_text)
                self._rollover_pending = False
                self._rollover_in_progress = False
                self._rollover_count += 1

                if start_next_draft:
                    new_draft_id = int(time.time() * 1000000) + random.randint(0, 999)
                    while new_draft_id == old_draft_id:
                        new_draft_id += 1
                    self.draft_id = new_draft_id
                    rollover_mode = "plain_text_fallback" if used_fallback else "complete_block"
                else:
                    # 终局分支没有下一次模型请求：旧草稿已结束，不能创建空的新草稿。
                    new_draft_id = None
                    rollover_mode = "terminal_plain_text_fallback" if used_fallback else "terminal_complete_block"

                # 限制 _rollover_history 长度：此前每次 rollover 都 append 一条，
                # 没有上限，长时间运行的会话会让该 list 无限增长。保留最近 50 条
                # 用于诊断即可。
                self._rollover_history.append({
                    "old_draft_id": old_draft_id,
                    "new_draft_id": new_draft_id,
                    "completed_message_id": completed_message_id if isinstance(completed_message_id, int) else None,
                    "visible_tokens": count_tokens(_rich_visible_text(completed_html)),
                    "blocks": block_count,
                    "mode": rollover_mode,
                })
                if len(self._rollover_history) > 50:
                    # 删除最早的元素，保留最近 50 条。
                    del self._rollover_history[: len(self._rollover_history) - 50]

            if start_next_draft:
                await self._register_active_draft(0)
                await self.flush(force=True)
                logger.info(
                    "草稿已在回合边界滚动: chat=%s old=%s new=%s permanent=%s tokens=%s blocks=%s mode=%s",
                    self.chat_id, old_draft_id, self.draft_id, completed_message_id,
                    count_tokens(_rich_visible_text(completed_html)), block_count,
                    rollover_mode,
                )
            else:
                logger.info(
                    "草稿已在终局边界结束，不创建新草稿: chat=%s draft=%s permanent=%s tokens=%s blocks=%s mode=%s",
                    self.chat_id, old_draft_id, completed_message_id,
                    count_tokens(_rich_visible_text(completed_html)), block_count,
                    rollover_mode,
                )

            if old_draft_message_id:
                async def _cleanup_old_preview():
                    deleted = await delete_message_fast(self.chat_id, old_draft_message_id)
                    if not deleted:
                        logger.debug(
                            "旧草稿预览异步清理未完成: chat=%s msg=%s",
                            self.chat_id, old_draft_message_id,
                        )
                _track_task(_cleanup_old_preview())
            return True
        except asyncio.CancelledError:
            self._restore_handoff_text()
            self._rollover_in_progress = False
            raise
        except Exception:
            self._restore_handoff_text()
            self._rollover_in_progress = False
            raise

    # ---------- 刷新与清理 ----------
    async def flush(self, force: bool = False):
        now = time.monotonic()
        if now < self._rate_limited_until:
            logger.debug(
                "草稿帧跳过（本地限流冷却）: chat=%s draft=%s wait_ms=%s",
                self.chat_id, self.draft_id, int((self._rate_limited_until - now) * 1000),
            )
            return

        html_content = self._build_html()
        self._arm_rollover_if_needed(html_content)

        async with self._flush_lock:
            now = time.monotonic()
            if now < self._rate_limited_until:
                return

            html_content = self._build_html()
            if not html_content.strip() or html_content.strip() == " ":
                html_content = "<p>Working...</p>"

            # flush 已不再依赖字符阈值，frame_chars 仅用于日志统计。
            # 无论进入哪条执行路径都必须初始化，避免 UnboundLocalError。
            frame_chars = len(_rich_visible_text(html_content))
            _frame_boundaries, _ignored_chars, frame_blocks = _scan_rich_html_boundaries(html_content)
            frame_revision = self._flush_revision
            frame_started = time.monotonic()
            try:
                msg_id = await send_rich_message_draft(
                    self.chat_id, self.draft_id, html_content, force=force
                )
                self._last_flush_time = time.monotonic()
                if self._flush_revision == frame_revision:
                    self._flush_dirty = False
                self._flush_sequence += 1
                logger.debug(
                    "草稿帧完成: chat=%s draft=%s seq=%s force=%s result=%s tokens=%s blocks=%s elapsed_ms=%s",
                    self.chat_id, self.draft_id, self._flush_sequence, force, msg_id,
                    frame_chars, frame_blocks, int((time.monotonic() - frame_started) * 1000),
                )
                if msg_id:
                    self.draft_message_id = msg_id
                    await self._register_active_draft(msg_id)
            except RateLimitError as e:
                retry_after = e.retry_after + 2
                self._rate_limited_until = time.monotonic() + retry_after
                logger.warning(
                    f"Rate limited on draft {self.draft_id}, cooling until "
                    f"{self._rate_limited_until:.1f} (retry_after={e.retry_after}s)"
                )
            except Exception as e:
                # 修复：原代码用 "429" 子串判断 HTTP 429，对任何巧合
                # 含 "429" 字符串的异常（如 request_id）会误报为 rate limit。
                # 优先看异常的 status_code 属性，再回退到子串匹配。
                status_code = getattr(e, "status_code", None) or getattr(e, "status", None)
                err_msg = str(e)
                if status_code == 429 or "429" in err_msg:
                    self._rate_limited_until = time.monotonic() + 10.0
                    logger.warning(
                        f"Flush hit 429 (fallback, status={status_code}), "
                        f"cooling until {self._rate_limited_until:.1f}"
                    )
                else:
                    logger.warning(f"Flush failed: {e}")

    async def _stream_flush_loop(self):
        while not self._stop_flush:
            now = time.monotonic()
            if now < self._rate_limited_until:
                wait_time = self._rate_limited_until - now + 0.5
                await asyncio.sleep(min(wait_time, 5.0))
                if self._stop_flush:
                    break
                continue

            await asyncio.sleep(0.1)
            if self._stop_flush:
                break
            now = time.monotonic()
            if now < self._rate_limited_until:
                continue

            time_elapsed = now - self._last_flush_time
            # 此循环的 builder 就是当前请求唯一正在刷新的草稿。无论静默来自
            # 普通模型、工具、图片还是视频，只要内容长时间未变化，都统一强制
            # 重申这一帧，避免 send_rich_message_draft 因内容相同而短路。
            silent_too_long = time_elapsed >= STREAM_SILENT_FORCE_FLUSH
            should_flush = (
                    (self._flush_dirty and time_elapsed >= STREAM_FLUSH_INTERVAL)
                    or silent_too_long
            )
            if should_flush:
                self._commit_stream_buffer()
                await self.flush(force=silent_too_long)
                if not silent_too_long:
                    self._last_flush_time = now

    def start_flush_loop(self):
        if self._flush_task is None or self._flush_task.done():
            self._stop_flush = False
            self._flush_task = asyncio.create_task(self._stream_flush_loop())

    async def stop_flush_loop(self):
        """停止并限时等待草稿刷新子任务；回合边界滚动不再存在后台任务。"""
        self._stop_flush = True
        self._rollover_pending = False
        self._restore_handoff_text()

        pending: list[tuple[str, asyncio.Task]] = []
        for attr in ("_flush_task", "_pending_flush_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                pending.append((attr, task))
            setattr(self, attr, None)

        for attr, task in pending:
            try:
                await asyncio.wait_for(task, timeout=0.5)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.debug("%s 未在 0.5s 内停止，转入后台清理: draft_id=%s", attr, self.draft_id)
                asyncio.create_task(_swallow_flush_task(task, attr, self.draft_id))
            except Exception as exc:
                logger.debug("%s 停止时出现异常（可忽略）: %s", attr, exc)


