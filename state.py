# state.py
import asyncio
import time
from typing import Optional
from config import DEFAULT_MODEL

# ---------- 用户会话 ----------
user_contexts: dict = {}
user_models: dict = {}

# ---------- 细粒度锁 ----------
_chat_locks: dict = {}
_chat_locks_lock = asyncio.Lock()

async def get_chat_lock(chat_id: int) -> asyncio.Lock:
    async with _chat_locks_lock:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = asyncio.Lock()
        return _chat_locks[chat_id]

# ---------- 媒体组 ----------
media_groups: dict = {}

# ---------- 消息去重 ----------
processed_updates: set = set()

# ---------- 角色菜单消息ID ----------
role_message_ids: dict = {}

# ---------- 已删除消息ID ----------
deleted_message_ids: set = set()
deleted_messages_lock = asyncio.Lock()

# ---------- 受保护消息ID（例如停止消息） ----------
protected_message_ids: set = set()
protected_messages_lock = asyncio.Lock()

async def mark_protected_message(message_id: int) -> None:
    try:
        message_id_int = int(message_id)
    except (TypeError, ValueError):
        return
    async with protected_messages_lock:
        protected_message_ids.add(message_id_int)

async def is_protected_message(message_id: int) -> bool:
    try:
        message_id_int = int(message_id)
    except (TypeError, ValueError):
        return False
    async with protected_messages_lock:
        return message_id_int in protected_message_ids

# ---------- 图片缓存状态 ----------
_image_cache_r2_attempted: set = set()
_image_cache_r2_attempted_lock = asyncio.Lock()

async def mark_r2_attempted(file_id: str):
    async with _image_cache_r2_attempted_lock:
        _image_cache_r2_attempted.add(file_id)

async def is_r2_attempted(file_id: str) -> bool:
    async with _image_cache_r2_attempted_lock:
        return file_id in _image_cache_r2_attempted

# ---------- 基本上下文操作（带锁） ----------
def get_or_init_context(chat_id: int) -> dict:
    if chat_id not in user_contexts:
        user_contexts[chat_id] = {
            "conversation_history": [],
            "search_mode": False,
            "username": f"User_{chat_id}",
            "total_prompt_tokens": 0,
            "last_usage": None,
        }
    return user_contexts[chat_id]

def get_user_model(chat_id: int) -> str:
    return user_models.get(chat_id, DEFAULT_MODEL)

def set_user_model(chat_id: int, model: str) -> None:
    user_models[chat_id] = model

# ---------- 安全的异步读写模型（自动加锁） ----------
async def safe_get_user_model(chat_id: int) -> str:
    lock = await get_chat_lock(chat_id)
    async with lock:
        return user_models.get(chat_id, DEFAULT_MODEL)

async def safe_set_user_model(chat_id: int, model: str) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        user_models[chat_id] = model

# ---------- 安全读写历史 ----------
async def safe_append_message(chat_id: int, message: dict) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        ctx["conversation_history"].append(message)

async def safe_get_history(chat_id: int) -> list:
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        return ctx["conversation_history"].copy()

async def safe_clear_history(chat_id: int) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        ctx["conversation_history"] = []

def get_history_length(chat_id: int) -> int:
    ctx = user_contexts.get(chat_id)
    if not ctx:
        return 0
    return len(ctx.get("conversation_history", []))

async def safe_get_history_length(chat_id: int) -> int:
    lock = await get_chat_lock(chat_id)
    async with lock:
        return get_history_length(chat_id)

# ---------- 媒体组操作 ----------
async def add_media_group_message(media_group_id: str, msg: dict):
    async with _chat_locks_lock:
        if media_group_id not in media_groups:
            media_groups[media_group_id] = []
        media_groups[media_group_id].append(msg)

async def pop_media_group(media_group_id: str) -> list:
    async with _chat_locks_lock:
        return media_groups.pop(media_group_id, [])

# ---------- 角色选择管理 ----------
_role_selections: dict = {}
_role_lock = asyncio.Lock()

async def get_user_role(chat_id: int) -> str | None:
    async with _role_lock:
        return _role_selections.get(chat_id)

async def set_user_role(chat_id: int, role: str | None) -> None:
    async with _role_lock:
        if role is None:
            _role_selections.pop(chat_id, None)
        else:
            _role_selections[chat_id] = role

# ========== 编辑器文件状态管理 ==========
_editor_file_state: dict = {}
_editor_state_lock = asyncio.Lock()

def get_editor_file_state(chat_id: int, path: str) -> dict | None:
    key = (chat_id, path)
    return _editor_file_state.get(key)

def set_editor_file_state(chat_id: int, path: str, content: str, mtime: float) -> None:
    key = (chat_id, path)
    # 区分 None（清除状态）和 ""（合法的空文件内容），避免空文件被误判为未跟踪
    if content is None:
        _editor_file_state.pop(key, None)
        return
    _editor_file_state[key] = {"content": content, "mtime": mtime}

def clear_editor_file_state(chat_id: int, path: str) -> None:
    key = (chat_id, path)
    _editor_file_state.pop(key, None)

def update_editor_file_state(chat_id: int, path: str, content: str = None, mtime: float = None) -> None:
    key = (chat_id, path)
    if content is None and mtime is None:
        _editor_file_state.pop(key, None)
        return
    if content is None and mtime is not None:
        if key in _editor_file_state:
            _editor_file_state[key]["mtime"] = mtime
        return
    if mtime is None:
        mtime = time.time()
    _editor_file_state[key] = {"content": content, "mtime": mtime}

# ========== 最近生成的图片 URL 缓存 ==========
_last_generated_image: dict = {}
_last_generated_image_lock = asyncio.Lock()

async def set_last_generated_image_url(chat_id: int, url: str) -> None:
    async with _last_generated_image_lock:
        _last_generated_image[chat_id] = url

async def get_last_generated_image_url(chat_id: int) -> Optional[str]:
    async with _last_generated_image_lock:
        return _last_generated_image.get(chat_id)


# ---------- 活跃草稿追踪 ----------
_active_drafts: dict = {}
_active_drafts_lock = asyncio.Lock()

# 被明确“冻结”为停止输出的草稿，会被保留在状态里，避免后续清理误删/误收回。
_preserved_draft_ids: set[int] = set()
_preserved_draft_ids_lock = asyncio.Lock()

async def set_active_draft(chat_id: int, draft_id: int, message_id: int) -> None:
    async with _active_drafts_lock:
        _active_drafts[chat_id] = (draft_id, message_id)

async def get_active_draft_message_id(chat_id: int) -> Optional[int]:
    async with _active_drafts_lock:
        info = _active_drafts.get(chat_id)
        return info[1] if info else None

async def get_active_draft_info(chat_id: int) -> tuple:
    async with _active_drafts_lock:
        return _active_drafts.get(chat_id)

async def clear_active_draft(chat_id: int, draft_id: int = None) -> None:
    async with _active_drafts_lock:
        info = _active_drafts.get(chat_id)
        if info is None:
            return
        if draft_id is None or info[0] == draft_id:
            _active_drafts.pop(chat_id, None)

async def mark_preserved_draft(draft_id: int) -> None:
    try:
        draft_id_int = int(draft_id)
    except (TypeError, ValueError):
        return
    async with _preserved_draft_ids_lock:
        _preserved_draft_ids.add(draft_id_int)

async def is_preserved_draft(draft_id: int) -> bool:
    try:
        draft_id_int = int(draft_id)
    except (TypeError, ValueError):
        return False
    async with _preserved_draft_ids_lock:
        return draft_id_int in _preserved_draft_ids
