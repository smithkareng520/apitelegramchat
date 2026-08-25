"""In-process runtime state with bounded retention.

Only short-lived execution state belongs here. Durable user settings are stored by
specialized repositories (for example authorization.py), not in module globals.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
from typing import Optional

from apitelegramchat.config import DEFAULT_MODEL


def _positive_int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


SESSION_TTL_SECONDS = _positive_int_env("SESSION_TTL_SECONDS", 86_400, 300)
UPDATE_DEDUP_TTL_SECONDS = _positive_int_env("UPDATE_DEDUP_TTL_SECONDS", 3_600, 60)
TEMP_MARK_TTL_SECONDS = _positive_int_env("TEMP_MARK_TTL_SECONDS", 86_400, 300)
MEDIA_GROUP_TTL_SECONDS = _positive_int_env("MEDIA_GROUP_TTL_SECONDS", 600, 30)
MAX_PROCESSED_UPDATES = _positive_int_env("MAX_PROCESSED_UPDATES", 10_000, 100)

# ---------- 用户会话 ----------
user_contexts: dict[int, dict] = {}
user_models: dict[int, str] = {}
_chat_last_access: dict[int, float] = {}

# ---------- 当前用户命名空间（用于按 user_id 隔离工作区/状态文件） ----------
_current_user_namespace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "apitelegramchat_current_user_namespace", default=None
)


def set_current_user_namespace(namespace: str | int | None) -> None:
    value = None if namespace is None else str(namespace).strip() or None
    _current_user_namespace.set(value)


def bind_current_user_namespace(namespace: str | int | None) -> contextvars.Token[str | None]:
    """Bind a namespace for one request and return its reset token."""
    value = None if namespace is None else str(namespace).strip() or None
    return _current_user_namespace.set(value)


def reset_current_user_namespace(token: contextvars.Token[str | None]) -> None:
    """Restore the namespace that was active before a request-scoped binding."""
    _current_user_namespace.reset(token)


def get_current_user_namespace() -> str | None:
    return _current_user_namespace.get()


def _touch_chat(chat_id: int, now: float | None = None) -> None:
    _chat_last_access[chat_id] = time.monotonic() if now is None else now
    context = user_contexts.get(chat_id)
    if context is not None:
        context["_last_access"] = _chat_last_access[chat_id]

# ---------- 细粒度锁 ----------
_chat_locks: dict[int, asyncio.Lock] = {}
_chat_locks_lock = asyncio.Lock()


async def get_chat_lock(chat_id: int) -> asyncio.Lock:
    _touch_chat(chat_id)
    async with _chat_locks_lock:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = asyncio.Lock()
        return _chat_locks[chat_id]

# ---------- 媒体组 ----------
media_groups: dict[str, list[dict]] = {}
_media_group_created_at: dict[str, float] = {}

# ---------- 消息去重 ----------
processed_updates: dict[int, float] = {}
_processed_updates_lock = asyncio.Lock()


async def remember_update(update_id: int) -> bool:
    """Remember an update id once; return False when it is a valid duplicate."""
    now = time.monotonic()
    async with _processed_updates_lock:
        cutoff = now - UPDATE_DEDUP_TTL_SECONDS
        for old_id, seen_at in list(processed_updates.items()):
            if seen_at < cutoff:
                processed_updates.pop(old_id, None)
        if update_id in processed_updates:
            return False
        processed_updates[update_id] = now
        overflow = len(processed_updates) - MAX_PROCESSED_UPDATES
        if overflow > 0:
            for old_id, _seen_at in sorted(processed_updates.items(), key=lambda item: item[1])[:overflow]:
                processed_updates.pop(old_id, None)
        return True

# ---------- 角色菜单消息ID ----------
role_message_ids: dict[int, int] = {}

# ---------- 已删除消息ID ----------
# 保留 set 兼容 utils.py 的既有引用；时间戳用于统一回收。
deleted_message_ids: set[int] = set()
_deleted_message_at: dict[int, float] = {}
deleted_messages_lock = asyncio.Lock()


async def mark_deleted_message(message_id: int) -> None:
    try:
        message_id_int = int(message_id)
    except (TypeError, ValueError):
        return
    async with deleted_messages_lock:
        deleted_message_ids.add(message_id_int)
        _deleted_message_at[message_id_int] = time.monotonic()

# ---------- 受保护消息ID（例如停止消息） ----------
protected_message_ids: dict[int, float] = {}
protected_messages_lock = asyncio.Lock()


async def mark_protected_message(message_id: int) -> None:
    try:
        message_id_int = int(message_id)
    except (TypeError, ValueError):
        return
    async with protected_messages_lock:
        protected_message_ids[message_id_int] = time.monotonic()


async def is_protected_message(message_id: int) -> bool:
    try:
        message_id_int = int(message_id)
    except (TypeError, ValueError):
        return False
    async with protected_messages_lock:
        return message_id_int in protected_message_ids

# ---------- 图片缓存状态 ----------
_image_cache_r2_attempted: dict[str, float] = {}
_image_cache_r2_attempted_lock = asyncio.Lock()


async def mark_r2_attempted(file_id: str) -> None:
    async with _image_cache_r2_attempted_lock:
        _image_cache_r2_attempted[file_id] = time.monotonic()


async def is_r2_attempted(file_id: str) -> bool:
    async with _image_cache_r2_attempted_lock:
        return file_id in _image_cache_r2_attempted

# ---------- 基本上下文操作（带锁） ----------
def get_or_init_context(chat_id: int) -> dict:
    now = time.monotonic()
    if chat_id not in user_contexts:
        user_contexts[chat_id] = {
            "conversation_history": [],
            "search_mode": False,
            "username": f"User_{chat_id}",
            "total_prompt_tokens": 0,
            "last_usage": None,
            "active_skill": None,
            "_last_access": now,
        }
    _touch_chat(chat_id, now)
    return user_contexts[chat_id]


def get_user_model(chat_id: int) -> str:
    _touch_chat(chat_id)
    return user_models.get(chat_id, DEFAULT_MODEL)


def set_user_model(chat_id: int, model: str) -> None:
    _touch_chat(chat_id)
    user_models[chat_id] = model

# ---------- 安全的异步读写模型（自动加锁） ----------
async def safe_get_user_model(chat_id: int) -> str:
    lock = await get_chat_lock(chat_id)
    async with lock:
        return get_user_model(chat_id)


async def safe_set_user_model(chat_id: int, model: str) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        set_user_model(chat_id, model)

# ---------- 安全读写历史 ----------
async def safe_append_message(chat_id: int, message: dict) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        get_or_init_context(chat_id)["conversation_history"].append(message)


async def safe_get_history(chat_id: int) -> list:
    lock = await get_chat_lock(chat_id)
    async with lock:
        return get_or_init_context(chat_id)["conversation_history"].copy()


async def safe_clear_history(chat_id: int) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        get_or_init_context(chat_id)["conversation_history"] = []


def get_active_skill(chat_id: int) -> dict | None:
    context = user_contexts.get(chat_id)
    if not context:
        return None
    _touch_chat(chat_id)
    return context.get("active_skill")


async def safe_get_active_skill(chat_id: int) -> dict | None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        return get_active_skill(chat_id)


async def safe_set_active_skill(chat_id: int, skill: dict | None) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        get_or_init_context(chat_id)["active_skill"] = skill


async def safe_clear_active_skill(chat_id: int) -> None:
    await safe_set_active_skill(chat_id, None)


def get_history_length(chat_id: int) -> int:
    context = user_contexts.get(chat_id)
    if not context:
        return 0
    _touch_chat(chat_id)
    return len(context.get("conversation_history", []))


async def safe_get_history_length(chat_id: int) -> int:
    lock = await get_chat_lock(chat_id)
    async with lock:
        return get_history_length(chat_id)

# ---------- 媒体组操作 ----------
async def add_media_group_message(media_group_id: str, msg: dict) -> None:
    async with _chat_locks_lock:
        if media_group_id not in media_groups:
            media_groups[media_group_id] = []
            _media_group_created_at[media_group_id] = time.monotonic()
        media_groups[media_group_id].append(msg)


async def pop_media_group(media_group_id: str) -> list:
    async with _chat_locks_lock:
        _media_group_created_at.pop(media_group_id, None)
        return media_groups.pop(media_group_id, [])

# ---------- 角色选择管理 ----------
_role_selections: dict[int, str] = {}
_role_lock = asyncio.Lock()


async def get_user_role(chat_id: int) -> str | None:
    _touch_chat(chat_id)
    async with _role_lock:
        return _role_selections.get(chat_id)


async def set_user_role(chat_id: int, role: str | None) -> None:
    _touch_chat(chat_id)
    async with _role_lock:
        if role is None:
            _role_selections.pop(chat_id, None)
        else:
            _role_selections[chat_id] = role

# ========== 编辑器文件状态管理 ==========
_editor_file_state: dict[tuple[int, str], dict] = {}
_editor_state_lock = asyncio.Lock()


def get_editor_file_state(chat_id: int, path: str) -> dict | None:
    _touch_chat(chat_id)
    return _editor_file_state.get((chat_id, path))


def set_editor_file_state(chat_id: int, path: str, content: str | None, mtime: float) -> None:
    _touch_chat(chat_id)
    key = (chat_id, path)
    if content is None:
        _editor_file_state.pop(key, None)
        return
    _editor_file_state[key] = {"content": content, "mtime": mtime}


def clear_editor_file_state(chat_id: int, path: str) -> None:
    _touch_chat(chat_id)
    _editor_file_state.pop((chat_id, path), None)


def update_editor_file_state(chat_id: int, path: str, content: str | None = None, mtime: float | None = None) -> None:
    _touch_chat(chat_id)
    key = (chat_id, path)
    if content is None and mtime is None:
        _editor_file_state.pop(key, None)
        return
    if content is None:
        if key in _editor_file_state:
            _editor_file_state[key]["mtime"] = mtime
        return
    _editor_file_state[key] = {"content": content, "mtime": time.time() if mtime is None else mtime}

# ========== 最近生成的图片 URL 缓存 ==========
_last_generated_image: dict[int, str] = {}
_last_generated_image_lock = asyncio.Lock()


async def set_last_generated_image_url(chat_id: int, url: str) -> None:
    _touch_chat(chat_id)
    async with _last_generated_image_lock:
        _last_generated_image[chat_id] = url


async def get_last_generated_image_url(chat_id: int) -> Optional[str]:
    _touch_chat(chat_id)
    async with _last_generated_image_lock:
        return _last_generated_image.get(chat_id)

# ---------- 活跃草稿追踪 ----------
_active_drafts: dict[int, tuple[int, int]] = {}
_active_drafts_lock = asyncio.Lock()
_preserved_draft_ids: dict[int, float] = {}
_preserved_draft_ids_lock = asyncio.Lock()


async def set_active_draft(chat_id: int, draft_id: int, message_id: int) -> None:
    _touch_chat(chat_id)
    async with _active_drafts_lock:
        _active_drafts[chat_id] = (draft_id, message_id)


async def get_active_draft_message_id(chat_id: int) -> Optional[int]:
    _touch_chat(chat_id)
    async with _active_drafts_lock:
        info = _active_drafts.get(chat_id)
        return info[1] if info else None


async def get_active_draft_info(chat_id: int) -> tuple[int, int] | None:
    _touch_chat(chat_id)
    async with _active_drafts_lock:
        return _active_drafts.get(chat_id)


async def clear_active_draft(chat_id: int, draft_id: int | None = None) -> None:
    async with _active_drafts_lock:
        info = _active_drafts.get(chat_id)
        if info is not None and (draft_id is None or info[0] == draft_id):
            _active_drafts.pop(chat_id, None)


async def mark_preserved_draft(draft_id: int) -> None:
    try:
        draft_id_int = int(draft_id)
    except (TypeError, ValueError):
        return
    async with _preserved_draft_ids_lock:
        _preserved_draft_ids[draft_id_int] = time.monotonic()


async def is_preserved_draft(draft_id: int) -> bool:
    try:
        draft_id_int = int(draft_id)
    except (TypeError, ValueError):
        return False
    async with _preserved_draft_ids_lock:
        return draft_id_int in _preserved_draft_ids


async def cleanup_expired_runtime_state() -> dict[str, int]:
    """Prune stale process-local state; safe to run after every webhook update."""
    now = time.monotonic()
    stale_before = now - SESSION_TTL_SECONDS
    temp_before = now - TEMP_MARK_TTL_SECONDS
    removed: dict[str, int] = {"sessions": 0, "media_groups": 0, "temporary_marks": 0}

    async with _active_drafts_lock:
        active_chat_ids = set(_active_drafts)
    stale_chat_ids = [chat_id for chat_id, last_seen in _chat_last_access.items() if last_seen < stale_before and chat_id not in active_chat_ids]
    if stale_chat_ids:
        async with _chat_locks_lock:
            for chat_id in stale_chat_ids:
                lock = _chat_locks.get(chat_id)
                if lock is not None and lock.locked():
                    continue
                user_contexts.pop(chat_id, None)
                user_models.pop(chat_id, None)
                _chat_last_access.pop(chat_id, None)
                _chat_locks.pop(chat_id, None)
                role_message_ids.pop(chat_id, None)
                _role_selections.pop(chat_id, None)
                _last_generated_image.pop(chat_id, None)
                for key in [key for key in _editor_file_state if key[0] == chat_id]:
                    _editor_file_state.pop(key, None)
                removed["sessions"] += 1

    async with _chat_locks_lock:
        for group_id, created_at in list(_media_group_created_at.items()):
            if created_at < now - MEDIA_GROUP_TTL_SECONDS:
                _media_group_created_at.pop(group_id, None)
                media_groups.pop(group_id, None)
                removed["media_groups"] += 1

    async with deleted_messages_lock:
        for message_id, marked_at in list(_deleted_message_at.items()):
            if marked_at < temp_before:
                _deleted_message_at.pop(message_id, None)
                deleted_message_ids.discard(message_id)
                removed["temporary_marks"] += 1
    async with protected_messages_lock:
        for message_id, marked_at in list(protected_message_ids.items()):
            if marked_at < temp_before:
                protected_message_ids.pop(message_id, None)
                removed["temporary_marks"] += 1
    async with _image_cache_r2_attempted_lock:
        for file_id, marked_at in list(_image_cache_r2_attempted.items()):
            if marked_at < temp_before:
                _image_cache_r2_attempted.pop(file_id, None)
                removed["temporary_marks"] += 1
    async with _preserved_draft_ids_lock:
        for draft_id, marked_at in list(_preserved_draft_ids.items()):
            if marked_at < temp_before:
                _preserved_draft_ids.pop(draft_id, None)
                removed["temporary_marks"] += 1
    return removed
