# state.py
import asyncio
import contextvars
import time
import uuid
from collections import OrderedDict
from apitelegramchat.config import DEFAULT_MODEL

# ---------- 用户会话 ----------
user_contexts: dict = {}
user_models: dict = {}

# ---------- 当前用户命名空间（用于按 user_id 隔离工作区/状态文件） ----------
_current_user_namespace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "apitelegramchat_current_user_namespace", default=None
)


def set_current_user_namespace(namespace: str | int | None) -> None:
    if namespace is None:
        _current_user_namespace.set(None)
        return
    value = str(namespace).strip()
    _current_user_namespace.set(value or None)


def bind_current_user_namespace(namespace: str | int | None) -> contextvars.Token[str | None]:
    """Bind a namespace for one request and return its reset token."""
    value = None if namespace is None else str(namespace).strip() or None
    return _current_user_namespace.set(value)


def reset_current_user_namespace(token: contextvars.Token[str | None]) -> None:
    """Restore the namespace that was active before a request-scoped binding."""
    _current_user_namespace.reset(token)


def get_current_user_namespace() -> str | None:
    return _current_user_namespace.get()

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
# 给 media_groups 单独加锁，避免和 get_chat_lock 抢同一把全局锁造成阻塞。
media_groups_lock = asyncio.Lock()

async def add_media_group_message(media_group_id: str, msg: dict):
    async with media_groups_lock:
        if media_group_id not in media_groups:
            media_groups[media_group_id] = []
        media_groups[media_group_id].append(msg)

async def pop_media_group(media_group_id: str) -> list:
    async with media_groups_lock:
        return media_groups.pop(media_group_id, [])

# ---------- 消息去重 ----------
# 用 OrderedDict 保留插入顺序，淘汰时按"最早插入"的 5000 条淘汰，避免
# 之前 set 无序时把刚加入的 update_id 随机淘汰导致重复处理。
# 同时记录插入时间，便于将来按时间窗口做 GC。
processed_updates: OrderedDict = OrderedDict()
_dedup_lock = asyncio.Lock()

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
            "username": f"User_{chat_id}",
            "active_skill": None,
        }
    return user_contexts[chat_id]

def get_user_model(chat_id: int) -> str:
    return user_models.get(chat_id, DEFAULT_MODEL)

# ---------- 安全的异步读写模型（自动加锁） ----------
async def safe_set_user_model(chat_id: int, model: str) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        user_models[chat_id] = model

# ---------- LLM 会话亲和键（session_id）管理 ----------
# 语义（与 OpenRouter body.session_id / agnes 会话亲和键共用）：
#   - 同一个对话窗口（chat）内的所有任务——主 agent 循环、子 agent、
#     TIMER 主动唤醒——共用同一个 session_id（同一对话 = 同一会话，
#     让网关从第一个请求起粘住同一推理副本，前缀缓存跨轮次/跨任务命中）。
#   - 用户点击"清空对话"（/clear，safe_clear_history）视为新建会话：
#     轮换会话纪元 token，生成全新的 session_id，避免旧会话的路由亲和
#     （sticky session / 副本粘性）以及旧前缀缓存干扰新对话。
# 键格式：tg-chat-{chat_id}-{纪元 token}（总长 ≤256 字符，与 OpenRouter
# 上限一致）。token 为 12 位 hex 随机串，惰性生成、重启后自然轮换
# （进程重启时对话历史也在内存中清零，语义上同样属于新会话）。
_SESSION_TOKEN_LEN = 12  # uuid4().hex 截取长度：碰撞概率可忽略，键更短


def _new_session_token() -> str:
    return uuid.uuid4().hex[:_SESSION_TOKEN_LEN]


def get_llm_session_token(chat_id: int) -> str:
    """读取（惰性生成）该 chat 当前的会话纪元 token。"""
    ctx = get_or_init_context(chat_id)
    token = ctx.get("llm_session_token")
    if not token:
        token = _new_session_token()
        ctx["llm_session_token"] = token
    return token


def rotate_llm_session_token(chat_id: int) -> str:
    """轮换该 chat 的会话纪元 token（清空对话/新建会话时调用），返回新 token。

    必须在持有该 chat 锁的上下文中调用（当前唯一调用方 safe_clear_history
    已持有），保证与历史清空同原子：新历史与新 session_id 同步生效。
    正在进行中的旧请求继续用旧键完成本轮，不受影响（键只在 loop 开始时
    解析一次）。
    """
    ctx = get_or_init_context(chat_id)
    token = _new_session_token()
    ctx["llm_session_token"] = token
    return token


def get_llm_session_key(chat_id) -> str:
    """LLM 网关会话亲和键：tg-chat-{chat_id}-{纪元 token}（≤256 字符）。

    - 同一对话窗口/同一任务内的全部请求（主循环全部轮次、子 agent、
      TIMER 回合）共用同一键：粘性路由与前缀缓存跨轮次稳定。
    - 清空对话（safe_clear_history）后键自动轮换，旧亲和性不再干扰新对话。
    - chat_id 为 None（无法定位会话）返回空串，调用方按"无键"处理。
    """
    if chat_id is None:
        return ""
    key = f"tg-chat-{chat_id}-{get_llm_session_token(chat_id)}".strip()
    return key[:256]


# ---------- 安全读写历史 ----------
async def safe_clear_history(chat_id: int) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        ctx["conversation_history"] = []
        # 清空对话 = 新建会话：同步轮换 LLM 会话亲和键，旧会话的路由
        # 亲和性（OpenRouter 粘性路由 / agnes 副本粘性）不再作用于新对话。
        rotate_llm_session_token(chat_id)


# ---------- 草稿预览开关（/show on|off，USER 与 TIMER 回合统一生效） ----------
async def get_show_drafts(chat_id: int) -> bool:
    """读取该 chat 的草稿预览开关；默认开启（True）。"""
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        return bool(ctx.get("show_drafts", True))


async def set_show_drafts(chat_id: int, enabled: bool) -> None:
    """设置该 chat 的草稿预览开关。

    False = 静默模式：交付走 deliver_reply，send 缺省值按事件源区分
    （USER 回合默认 true、收尾有兜底；TIMER 回合默认 false、无兜底）。
    """
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        ctx["show_drafts"] = bool(enabled)


async def safe_set_active_skill(chat_id: int, skill: dict | None) -> None:
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        ctx["active_skill"] = skill


async def safe_clear_active_skill(chat_id: int) -> None:
    await safe_set_active_skill(chat_id, None)

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

# ========== 活跃草稿追踪 ==========
_active_drafts: dict = {}
_active_drafts_lock = asyncio.Lock()

# 被明确"冻结"为停止输出的草稿，会被保留在状态里，避免后续清理误删/误收回。
_preserved_draft_ids: set[int] = set()
_preserved_draft_ids_lock = asyncio.Lock()

async def set_active_draft(chat_id: int, draft_id: int, message_id: int) -> None:
    async with _active_drafts_lock:
        _active_drafts[chat_id] = (draft_id, message_id)

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


# ---------- 消息去重辅助函数 ----------
async def mark_update_processed_if_new(uid: object) -> bool:
    """原子地检查并标记 update_id。

    返回 True 表示首次见到（应当处理）；False 表示已处理过（应当跳过）。
    检查与标记在同一把锁内完成，避免 webhook 并发重投时同一 update
    被两个协程同时通过检查导致双重处理。
    """
    async with _dedup_lock:
        if uid in processed_updates:
            return False
        _record_processed_unlocked(uid)
        return True


def _record_processed_unlocked(uid: object) -> None:
    """在已持有 _dedup_lock 的前提下记录 uid 并执行容量淘汰。"""
    processed_updates[uid] = time.time()
    # 上限 10000，超过则淘汰最早的 5000 条（按插入顺序，确定性）。
    if len(processed_updates) > 10000:
        for _ in range(5000):
            try:
                processed_updates.popitem(last=False)
            except KeyError:
                break
