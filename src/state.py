# state.py
import asyncio
import contextvars
import time
import uuid
from collections import OrderedDict
from config import DEFAULT_MODEL

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

async def add_media_group_message(media_group_id: str, msg: dict) -> None:
    async with media_groups_lock:
        if media_group_id not in media_groups:
            media_groups[media_group_id] = []
        media_groups[media_group_id].append(msg)

async def pop_media_group(media_group_id: str) -> list:
    async with media_groups_lock:
        return media_groups.pop(media_group_id, [])

# ---------- 相册媒体登记表（回复引用补齐整组媒体） ----------
# 背景：用户回复相册（media group）中的任意分片时，Telegram 的
# reply_to_message 只携带被长按回复的那一个分片消息——如果只看
# reply_to_message，模型只能看到一张图（表现即"回复相册只带上最后/
# 单张图片"）。聚合分片存储 media_groups 在聚合处理时会被 pop 清空，
# 无法在回复时反查，因此聚合完成后在这里另登记一份"整组媒体摘要"。
#
# key 为 Telegram 原始 media_group_id（不带聚合存储的 ":photo"/":video"
# 后缀），value 为 record_album_media() 写入的摘要 dict。查询入口
# get_album_media() 校验 chat_id 归属，避免跨 chat 串组。
#
# 有界（LRU，最多 _ALBUM_REGISTRY_MAX 条）+ 进程内存级，不持久化：
# 重启或登记被淘汰后，回复相册退化为单分片引用（旧行为），不影响
# 正确性，只是少带几张图。
_ALBUM_REGISTRY_MAX = 200
album_media_registry: OrderedDict = OrderedDict()


def record_album_media(media_group_id: str, chat_id: int, messages: list) -> None:
    """相册聚合处理时登记整组媒体摘要，供回复引用时补齐全部图片等。

    messages 为该相册的全部分片（Telegram update 的 message dict）。
    重复登记同一组时以最后一次为准（并把该组移到 LRU 最新端）。
    """
    if not media_group_id or not messages:
        return
    photos: list[str] = []
    audios: list[dict] = []
    videos: list[dict] = []
    documents: list[dict] = []
    captions: list[str] = []
    message_ids: list[int] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        mid = msg.get("message_id")
        if mid:
            message_ids.append(mid)
        photo = msg.get("photo")
        if isinstance(photo, list) and photo:
            fid = (photo[-1] or {}).get("file_id")
            if fid and fid not in photos:
                photos.append(fid)
        audio = msg.get("audio")
        if isinstance(audio, dict) and audio.get("file_id"):
            audios.append({
                "file_id": audio["file_id"],
                "file_name": audio.get("file_name") or f"audio_{audio['file_id'][:8]}",
                "mime_type": audio.get("mime_type") or "",
            })
        video = msg.get("video") or msg.get("video_note")
        if isinstance(video, dict) and video.get("file_id"):
            videos.append({
                "file_id": video["file_id"],
                "file_name": video.get("file_name") or f"video_{video['file_id'][:8]}.mp4",
                "mime_type": video.get("mime_type") or "video/mp4",
            })
        doc = msg.get("document")
        if isinstance(doc, dict) and doc.get("file_id"):
            documents.append({
                "file_id": doc["file_id"],
                "file_name": doc.get("file_name") or f"document_{doc['file_id'][:8]}.bin",
                "mime_type": doc.get("mime_type") or "",
            })
        if msg.get("caption"):
            captions.append(str(msg["caption"]).strip())
    entry = {
        "chat_id": chat_id,
        "photos": photos,
        "audios": audios,
        "videos": videos,
        "documents": documents,
        "captions": captions,
        "message_ids": message_ids,
        "ts": time.time(),
    }
    # 纯内存 dict 读写，无 await 点，asyncio 单线程语义下无需加锁。
    album_media_registry.pop(media_group_id, None)
    album_media_registry[media_group_id] = entry
    while len(album_media_registry) > _ALBUM_REGISTRY_MAX:
        album_media_registry.popitem(last=False)


def get_album_media(chat_id: int | None, media_group_id: str) -> dict | None:
    """按原始 media_group_id 查询登记的整组媒体摘要。

    校验 chat_id 归属（不同 chat 出现相同 media_group_id 时隔离）；
    命中时把该组移到 LRU 最新端。
    """
    if not media_group_id:
        return None
    entry = album_media_registry.get(media_group_id)
    if not isinstance(entry, dict):
        return None
    if chat_id is not None and entry.get("chat_id") is not None \
            and entry.get("chat_id") != chat_id:
        return None
    album_media_registry.move_to_end(media_group_id)
    return entry


# ---------- 消息去重 ----------
# 用 OrderedDict 保留插入顺序，淘汰时按"最早插入"的 5000 条淘汰，避免
# 之前 set 无序时把刚加入的 update_id 随机淘汰导致重复处理。
# 同时记录插入时间，便于将来按时间窗口做 GC。
processed_updates: OrderedDict = OrderedDict()
_dedup_lock = asyncio.Lock()

# ---------- 角色菜单消息ID ----------
role_message_ids: dict = {}

# ---------- 已删除消息ID ----------
class BoundedIDSet:
    """有界 ID 集合：超容量时按"最早插入"淘汰（OrderedDict 保持插入序）。

    用于只需覆盖"最近窗口"的持久 ID 集合（已删除消息 / 死亡草稿 /
    冻结草稿），替代无界 set，避免长期运行进程内存缓慢增长。重复 add
    会刷新位置（视为最新），与"最新标记最不可能被淘汰"的语义一致。
    """

    def __init__(self, maxsize: int = 10000) -> None:
        self._maxsize = maxsize
        self._items: OrderedDict[int, None] = OrderedDict()

    def add(self, item: int) -> None:
        self._items.pop(item, None)
        self._items[item] = None
        while len(self._items) > self._maxsize:
            self._items.popitem(last=False)

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __len__(self) -> int:
        return len(self._items)


deleted_message_ids: BoundedIDSet = BoundedIDSet()
deleted_messages_lock = asyncio.Lock()

# ---------- 图片缓存状态 ----------
_image_cache_r2_attempted: set = set()
_image_cache_r2_attempted_lock = asyncio.Lock()

async def mark_r2_attempted(file_id: str) -> None:
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
            # 真实 Telegram 用户名（可能为空）：授权路径专用，与展示用的
            # "username"（无 username 时回退 first_name/ID）严格分离。
            "tg_username": "",
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


def get_llm_session_key(chat_id: int | None) -> str:
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
# 有界集合：死亡/冻结标记只需覆盖"最近活跃窗口"，无界增长无意义。
_preserved_draft_ids: BoundedIDSet = BoundedIDSet()
_preserved_draft_ids_lock = asyncio.Lock()

async def set_active_draft(chat_id: int, draft_id: int, message_id: int) -> None:
    async with _active_drafts_lock:
        _active_drafts[chat_id] = (draft_id, message_id)

async def get_active_draft_info(chat_id: int) -> tuple[int, int] | None:
    async with _active_drafts_lock:
        return _active_drafts.get(chat_id)

async def clear_active_draft(chat_id: int, draft_id: int | None = None) -> None:
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
