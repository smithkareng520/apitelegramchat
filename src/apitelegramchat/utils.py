# utils.py
import json
import re
import aiohttp
import asyncio
import html
import logging
from logging import handlers as logging_handlers
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from apitelegramchat.config import BASE_URL, DEEPSEEK_API_KEY, OPENROUTER_API_KEY, LOG_LEVEL
from typing import Optional, Dict, Union
from contextlib import asynccontextmanager
import sys
from apitelegramchat.config import GROQ_API_KEY

# ---------- 配置日志 ----------
def setup_logging():
    root_logger = logging.getLogger()
    # 应用 LOG_LEVEL 环境变量（默认 INFO）
    try:
        level = getattr(logging, LOG_LEVEL, logging.INFO)
    except Exception:
        level = logging.INFO
    root_logger.setLevel(level)
    logging.getLogger('botocore').setLevel(logging.WARNING)
    logging.getLogger('aiobotocore').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}'
    ))
    root_logger.addHandler(console_handler)

    try:
        file_handler = logging_handlers.RotatingFileHandler(
            "/tmp/app.log", maxBytes=10*1024*1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}'
        ))
        root_logger.addHandler(file_handler)
    except Exception as e:
        print(f"Warning: 无法创建文件日志: {e}")

setup_logging()

logger = logging.getLogger(__name__)

# ---------- 请求ID上下文 ----------
# 使用 contextvars 替代全局 dict，避免并发协程间 request_id 互相覆盖
import contextvars
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="unknown")

def set_request_id(request_id: str):
    _request_id_var.set(request_id)

def get_request_id() -> str:
    return _request_id_var.get()

class RequestIdAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        rid = get_request_id()
        return f"[{rid}] {msg}", kwargs

def get_logger(name):
    return RequestIdAdapter(logging.getLogger(name), {})

# ---------- 工具函数 ----------
_SMART_AMP_PATTERN = re.compile(r'&(?![a-zA-Z0-9#]+;)')

def retry_async(max_retries: int = 3, delay: float = 1.0, backoff: float = 3.0, exceptions: tuple = (Exception,)):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except exceptions as e:
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(f"Retry {attempt+1}/{max_retries} for {func.__name__} due to {e}")
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
            return None
        return wrapper
    return decorator

def get_current_time() -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    return f"{days[now.weekday()]}, {months[now.month - 1]} {now.day}, {now.year}"

def smart_escape_text(text: str) -> str:
    if not text:
        return ""
    text = _SMART_AMP_PATTERN.sub('&amp;', text)
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    return text

def escape_html(text: str) -> str:
    return html.escape(text)


# ---------- 媒体 URL 转义 sanitizer ----------
# R2 presigned URL 含大量 & 查询参数（X-Amz-Algorithm、X-Amz-Credential、X-Amz-Signature 等）。
# 在 HTML 属性值 src="..." 中，未转义的 & 会被 Telegram HTML 解析器当作实体名起点，
# 导致 URL 被截断 → RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND / RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND。
# 此 sanitizer 在发送前仅转义媒体 src 属性中的裸 &，幂等（不重复转义已转义的实体）。
_VALID_HTML_ENTITIES = (
    r'amp;|lt;|gt;|quot;|apos;|nbsp;|hellip;|mdash;|ndash;|lsquo;|rsquo;|ldquo;|rdquo;'
    r'|#\d+;|#x[0-9a-fA-F]+;'
)
_BARE_AMP_RE = re.compile(rf'&(?!{_VALID_HTML_ENTITIES})')
_RICH_SRC_ATTR_RE = re.compile(
    r'''\bsrc\s*=\s*(?P<quote>["'])(?P<url>.*?)(?P=quote)''',
    re.IGNORECASE,
)


def escape_html_href_url(url: object) -> str:
    """Build an href attribute value without rewriting URL query separators.

    ``&`` is intentionally preserved. Media ``src`` values are escaped separately
    for Telegram's rich-message parser, but a clickable ``href`` must keep the
    original URL string so clients that read the raw message model do not receive
    ``&amp;`` as part of the URL.
    """
    value = str(url or "").strip()
    return value.replace('"', '&quot;')


def _escape_media_src_urls(html_content: str) -> str:
    """
    仅转义富文本媒体 src 属性中的裸 &。
    href 不在这里处理：下载/查看链接需要保留原始 URL 查询串，避免前端把
    &amp; 当作 URL 数据。单双引号形式都支持；已经转义成实体的 src 不重复转义。
    """
    if not html_content:
        return html_content

    def _escape_one(match: re.Match) -> str:
        quote = match.group("quote")
        url = match.group("url")
        escaped = _BARE_AMP_RE.sub('&amp;', url)
        return f"src={quote}{escaped}{quote}"

    return _RICH_SRC_ATTR_RE.sub(_escape_one, html_content)


async def send_message(chat_id: int, text: str) -> None:
    await send_rich_html_message(chat_id, f"<p>{html.escape(text)}</p>")

def strip_html_tags(text: str) -> str:
    if not text:
        return ""
    text = text.replace("<br/>", "\n").replace("<br>", "\n")
    return re.sub(r'<[^>]*>', '', text)


def _rich_message_plain_text_fallback(html_content: str) -> str:
    """将被 Rich Message 服务端拒绝的 HTML 降级为一个安全的段落。

    模型或工具输出有时仅包含内联标签，或在 ``details`` 中缺少块级内容。此类
    HTML 视觉上并非空白，但 Telegram 会返回 ``RICH_MESSAGE_CONTENT_REQUIRED``。
    这里保留其可见文本并转义为单个 ``<p>``，以保证用户不会因格式问题丢失回复。
    """
    visible_text = html.unescape(strip_html_tags(html_content or ""))
    visible_text = re.sub(r"\s+", " ", visible_text).strip()
    if not visible_text:
        return ""
    return f"<p>{html.escape(visible_text)}</p>"

async def check_deepseek_balance() -> tuple:
    url = "https://api.deepseek.com/user/balance"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    info = data["balance_infos"][0]
                    return info["total_balance"], info["currency"]
                else:
                    return None, f"HTTP {resp.status}"
    except Exception as e:
        return None, str(e)[:100]

async def check_openrouter_balance() -> float:
    url = "https://openrouter.ai/api/v1/auth/key"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", {}).get("limit_remaining", 0)
                else:
                    return -1.0
    except Exception:
        return -1.0

class RateLimitError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")

@retry_async(max_retries=5, delay=0.5, backoff=3.0, exceptions=(aiohttp.ClientError, asyncio.TimeoutError, RateLimitError))
async def delete_message(chat_id: int, message_id: int) -> None:
    from apitelegramchat.state import deleted_message_ids, deleted_messages_lock, is_protected_message
    if await is_protected_message(message_id):
        logger.info(f"deleteMessage 跳过受保护消息: chat={chat_id} msg={message_id}")
        return
    async with deleted_messages_lock:
        if message_id in deleted_message_ids:
            return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{BASE_URL}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            ) as r:
                if r.status == 200:
                    async with deleted_messages_lock:
                        deleted_message_ids.add(message_id)
                    logger.debug(f"deleteMessage 成功: chat={chat_id} msg={message_id}")
                    return
                elif r.status == 429:
                    retry_after = int(r.headers.get("Retry-After", 5))
                    raise RateLimitError(retry_after)
                else:
                    body = await r.text()
                    logger.error(f"deleteMessage 失败 HTTP {r.status}: {body[:200]}")
                    raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message=body)
    except Exception as e:
        logger.exception(f"deleteMessage 异常: chat={chat_id} msg={message_id} {e}")
        raise

async def delete_message_fast(chat_id: int, message_id: int) -> bool:
    if not message_id:
        return False
    try:
        from apitelegramchat.state import deleted_message_ids, deleted_messages_lock
        async with deleted_messages_lock:
            if message_id in deleted_message_ids:
                return True
    except Exception:
        pass

    timeout = aiohttp.ClientTimeout(total=3, connect=2)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                f"{BASE_URL}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            ) as r:
                if r.status == 200:
                    try:
                        from apitelegramchat.state import deleted_message_ids, deleted_messages_lock
                        async with deleted_messages_lock:
                            deleted_message_ids.add(message_id)
                    except Exception:
                        pass
                    return True
                if r.status == 400:
                    body = await r.text()
                    if "not found" in body.lower() or "to delete not found" in body.lower():
                        try:
                            from apitelegramchat.state import deleted_message_ids, deleted_messages_lock
                            async with deleted_messages_lock:
                                deleted_message_ids.add(message_id)
                        except Exception:
                            pass
                        return True
                return False
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug(f"delete_message_fast 失败: chat={chat_id} msg={message_id} {e}")
        return False

# ==================== Rich Message 支持 ====================
_last_sent_draft_cache = {}
_draft_send_locks: dict = {}
_draft_failure_counts: dict = {}
_dead_draft_ids: set = set()
_dead_draft_ids_lock = asyncio.Lock()
_draft_locks_lock = asyncio.Lock()
_draft_last_send_time: dict = {}
_DRAFT_MIN_INTERVAL = 0.25
# 草稿是可被后续完整状态替代的瞬态 UI；不能像永久消息一样在发送锁中
# 连续执行长超时重试，否则一帧网络抖动会让所有后续 Agent 状态长时间排队。
_DRAFT_REQUEST_TIMEOUT = 5.0
_DRAFT_CONNECT_TIMEOUT = 2.5
_DRAFT_MAX_ATTEMPTS = 2
_DRAFT_RETRY_DELAY = 0.25

async def _get_draft_send_lock(chat_id: int, draft_id: int) -> asyncio.Lock:
    key = (chat_id, draft_id)
    async with _draft_locks_lock:
        lock = _draft_send_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _draft_send_locks[key] = lock
        return lock

async def _reset_draft_failure(chat_id: int, draft_id: int) -> None:
    _draft_failure_counts.pop((chat_id, draft_id), None)

async def _bump_draft_failure(chat_id: int, draft_id: int) -> int:
    key = (chat_id, draft_id)
    _draft_failure_counts[key] = _draft_failure_counts.get(key, 0) + 1
    return _draft_failure_counts[key]

async def mark_draft_dead(draft_id) -> None:
    try:
        draft_id_int = int(draft_id)
    except (ValueError, TypeError):
        return
    async with _dead_draft_ids_lock:
        _dead_draft_ids.add(draft_id_int)
    logger.info(f"Draft {draft_id_int} marked as dead")

async def is_draft_dead(draft_id) -> bool:
    try:
        draft_id_int = int(draft_id)
    except (ValueError, TypeError):
        return True
    async with _dead_draft_ids_lock:
        return draft_id_int in _dead_draft_ids

async def _is_current_active_draft(chat_id: int, draft_id) -> bool:
    """只有当前仍然是活跃草稿时才允许继续刷新。"""
    try:
        draft_id_int = int(draft_id)
    except (ValueError, TypeError):
        return False
    try:
        from apitelegramchat.state import get_active_draft_info
        info = await get_active_draft_info(chat_id)
    except Exception:
        # 取不到状态时不要误伤发送，交给死亡标记兜底
        return True
    if not info:
        return False
    try:
        return int(info[0]) == draft_id_int
    except Exception:
        return False


async def _reassert_active_draft_content(chat_id: int, draft_id: int) -> None:
    """
    在永久消息发送之后，立刻用缓存的最新草稿内容再推一帧。

    Telegram 客户端在 bot 发出永久消息时会清掉/挤开当前 draft 预览；
    若不立刻 reassert，要等下一次 flush 间隔，用户就会看到
    「列表占了草稿位，草稿稍后在指令下方重新出现」。
    调用方必须已持有该 draft 的 send lock。
    """
    try:
        if await is_draft_dead(draft_id):
            return
        if not await _is_current_active_draft(chat_id, draft_id):
            return
        cache_key = (chat_id, draft_id)
        html_content = _last_sent_draft_cache.get(cache_key)
        if not html_content or not str(html_content).strip():
            return

        payload = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "rich_message": {
                "content": html_content,
                "html": html_content,
            },
        }
        # reassert 只是视觉保活，失败可由下一次真实 flush 恢复；不应占用草稿锁过久。
        timeout = aiohttp.ClientTimeout(total=4, connect=2)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{BASE_URL}/sendRichMessageDraft", json=payload) as resp:
                if resp.status == 200:
                    _draft_last_send_time[cache_key] = time.monotonic()
                    try:
                        data = await resp.json()
                        msg_id = data.get("result", {}).get("message_id")
                        if isinstance(msg_id, int) and msg_id > 0:
                            logger.debug(
                                f"reassert draft ok: chat={chat_id} draft={draft_id} msg_id={msg_id}"
                            )
                    except Exception:
                        pass
                else:
                    body = ""
                    try:
                        body = await resp.text()
                    except Exception:
                        pass
                    logger.debug(
                        f"reassert draft failed: chat={chat_id} draft={draft_id} "
                        f"status={resp.status} body={body[:120]}"
                    )
    except Exception as e:
        logger.debug(f"reassert draft exception: chat={chat_id} draft={draft_id} {e}")


@asynccontextmanager
async def serialize_with_active_draft(chat_id: int, *, reassert: bool = True):
    """
    将永久 bot 消息与当前活跃草稿的刷新串行化。

    修复：生成中发送 /model、/role、/balance 等指令回执时，
    sendRichMessage 与 sendRichMessageDraft 并发，客户端会把列表画在
    草稿视觉位，随后迟到的草稿刷新又出现在指令下方。

    持有活跃草稿的 send lock 期间发送永久消息，可保证：
      1) 等在途草稿刷新先结束
      2) 再发列表/确认等永久消息
      3) 可选立刻 reassert 草稿，使其稳定出现在新消息下方继续生成
    """
    draft_id = None
    try:
        from apitelegramchat.state import get_active_draft_info
        info = await get_active_draft_info(chat_id)
        if info:
            draft_id = int(info[0])
    except Exception:
        draft_id = None

    if draft_id is None:
        yield
        return

    # 即使草稿已 mark_dead，仍持有 send lock 与在途刷新串行，
    # 避免“最终回复 / 指令列表”与迟到的 draft HTTP 交错。
    # reassert 仅在草稿仍存活时执行。
    lock = await _get_draft_send_lock(chat_id, draft_id)
    async with lock:
        yield
        if reassert:
            try:
                if await is_draft_dead(draft_id):
                    return
            except Exception:
                pass
            await _reassert_active_draft_content(chat_id, draft_id)


# ---------- 不加锁的强制发送函数 ----------
async def send_rich_message_draft_unlocked(
    chat_id: int,
    draft_id,
    html_content: str,
    message_thread_id: Optional[int] = None,
) -> Optional[int]:
    """
    强制发送草稿更新，不做任何锁检查、死亡检查、缓存或速率限制。
    用于强制更新停止状态，仅发送一次，不重试。
    如果内容为空，自动填充占位符。
    """
    if not html_content or not html_content.strip():
        html_content = "<i>⏹️ 已停止输出</i>"
    html_content = html_content.strip()
    # 自动转义富文本 src/href 属性中的裸 &（R2 预签名 URL 等），防止 Telegram 误解析媒体或链接
    html_content = _escape_media_src_urls(html_content)

    try:
        draft_id_int = int(draft_id)
        if draft_id_int == 0:
            raise ValueError("draft_id must be non-zero")
    except (ValueError, TypeError) as e:
        logger.error(f"send_rich_message_draft_unlocked: invalid draft_id={draft_id!r}: {e}")
        return None

    payload = {
        "chat_id": chat_id,
        "draft_id": draft_id_int,
        "rich_message": {
            "content": html_content,
            "html": html_content,
        },
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id

    timeout = aiohttp.ClientTimeout(total=5, connect=3)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{BASE_URL}/sendRichMessageDraft", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    msg_id = data.get("result", {}).get("message_id")
                    logger.info(f"强制发送停止状态成功: chat={chat_id} draft={draft_id} msg_id={msg_id}")
                    return msg_id if isinstance(msg_id, int) and msg_id > 0 else 0
                else:
                    body = await resp.text()
                    logger.warning(f"强制发送停止状态失败: {resp.status} {body[:200]}")
                    return 0
    except Exception as e:
        logger.warning(f"强制发送停止状态异常: {e}")
        return 0

# ---------- 常规草稿发送（带锁） ----------
async def send_rich_message_draft(
    chat_id: int,
    draft_id,
    html_content: str,
    message_thread_id: Optional[int] = None,
    force: bool = False,
) -> Optional[int]:
    if not html_content or not html_content.strip():
        return 0
    html_content = html_content.strip()
    # 防止 Telegram 返回 400 RICH_MESSAGE_CONTENT_REQUIRED：
    # 只含 HTML 标签（如 <br/>、<b></b>、&nbsp;）但无可见文本时，Telegram 会拒绝。
    # 用一个简单的 tag-stripping 检查：剥掉所有 <xxx> 标签和 HTML 实体后若为空/纯空白，直接跳过。
    _visible_text = re.sub(r'<[^>]+>', ' ', html_content)
    _visible_text = re.sub(r'&[a-zA-Z#0-9]+;', ' ', _visible_text)
    _visible_text = re.sub(r'\s+', ' ', _visible_text).strip()
    if not _visible_text:
        logger.debug(f"send_rich_message_draft: skip empty-after-strip content (len={len(html_content)})")
        return 0
    # 自动转义富文本 src/href 属性中的裸 &（R2 预签名 URL 等），防止 Telegram 误解析媒体或链接
    html_content = _escape_media_src_urls(html_content)
    try:
        draft_id_int = int(draft_id)
        if draft_id_int == 0:
            raise ValueError("draft_id must be non-zero")
    except (ValueError, TypeError) as e:
        logger.error(f"send_rich_message_draft: invalid draft_id={draft_id!r}: {e}")
        return None

    lock = await _get_draft_send_lock(chat_id, draft_id_int)
    async with lock:
        if await is_draft_dead(draft_id_int):
            return 0
        if not await _is_current_active_draft(chat_id, draft_id_int):
            return 0

        cache_key = (chat_id, draft_id_int)
        last_sent = _last_sent_draft_cache.get(cache_key)
        if not force and last_sent == html_content:
            return 0

        if not force:
            last_time = _draft_last_send_time.get(cache_key, 0.0)
            wait_for_slot = _DRAFT_MIN_INTERVAL - (time.monotonic() - last_time)
            if wait_for_slot > 0:
                # 不直接丢弃这次新状态。等待至多 250ms 后发送，避免 builder 把
                # pending_chars 清零、随后只能等静默保活周期才重新显示更新。
                await asyncio.sleep(wait_for_slot)
                if await is_draft_dead(draft_id_int):
                    return 0
                if not await _is_current_active_draft(chat_id, draft_id_int):
                    return 0
                if _last_sent_draft_cache.get(cache_key) == html_content:
                    return 0

        payload = {
            "chat_id": chat_id,
            "draft_id": draft_id_int,
            "rich_message": {
                "content": html_content,
                "html": html_content,
            },
        }
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        # 草稿帧可被更晚的完整帧覆盖。把单次等待限制在 5 秒，并至多做一次
        # 短暂重试，避免网络抖动时 12s × 3 的锁占用造成前端数十秒“卡住”。
        timeout = aiohttp.ClientTimeout(
            total=_DRAFT_REQUEST_TIMEOUT,
            connect=_DRAFT_CONNECT_TIMEOUT,
        )

        for attempt in range(_DRAFT_MAX_ATTEMPTS):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(f"{BASE_URL}/sendRichMessageDraft", json=payload) as resp:
                        body = ""
                        if resp.status != 200:
                            try:
                                body = await resp.text()
                            except Exception:
                                body = ""

                        if resp.status == 200:
                            _draft_last_send_time[cache_key] = time.monotonic()
                            try:
                                data = await resp.json()
                                msg_id = data.get("result", {}).get("message_id")
                                _last_sent_draft_cache[cache_key] = html_content
                                await _reset_draft_failure(chat_id, draft_id_int)
                                if isinstance(msg_id, int) and msg_id > 0:
                                    return msg_id
                            except Exception:
                                pass
                            _last_sent_draft_cache[cache_key] = html_content
                            await _reset_draft_failure(chat_id, draft_id_int)
                            return 0

                        if resp.status == 429:
                            try:
                                data = json.loads(body)
                                retry_after = int(data.get("parameters", {}).get("retry_after", 5))
                            except Exception:
                                retry_after = 5
                            raise RateLimitError(retry_after)

                        body_lower = body.lower()
                        hard_not_found = (
                            resp.status in (404, 410)
                            or "not found" in body_lower
                            or "message to edit not found" in body_lower
                        )
                        not_modified = (
                            resp.status == 400 and "message is not modified" in body_lower
                        )

                        if not_modified:
                            _last_sent_draft_cache[cache_key] = html_content
                            await _reset_draft_failure(chat_id, draft_id_int)
                            return 0

                        # RICH_MESSAGE_CONTENT_REQUIRED：内容暂时没有块级元素（<details> 里
                        # 只有纯文本、或空 <details>）。这是流式过程中的瞬态结构问题，
                        # 下一帧 flush 通常会自带块级内容。不刷 WARNING、不累计 failure，
                        # 当作"本帧跳过"处理，避免日志噪音和无谓的 draft 死亡标记。
                        content_required = (
                            resp.status == 400 and "rich_message_content_required" in body_lower
                        )
                        if content_required:
                            logger.debug(
                                f"sendRichMessageDraft skip (RICH_MESSAGE_CONTENT_REQUIRED), "
                                f"will retry on next flush: chat={chat_id} draft={draft_id_int} "
                                f"len={len(html_content)}"
                            )
                            return 0

                        failures = await _bump_draft_failure(chat_id, draft_id_int)
                        logger.warning(
                            f"sendRichMessageDraft failed (attempt {attempt+1}/3, failures={failures}): "
                            f"{resp.status} {body[:200]}"
                        )
                        if hard_not_found and failures >= 5:
                            await mark_draft_dead(draft_id_int)
                        elif failures >= 6:
                            await mark_draft_dead(draft_id_int)
                        return 0

            except RateLimitError:
                raise
            except (aiohttp.ClientConnectorError, asyncio.TimeoutError, aiohttp.ServerDisconnectedError, aiohttp.ClientOSError) as e:
                logger.warning(
                    f"send_rich_message_draft transient error "
                    f"(attempt {attempt + 1}/{_DRAFT_MAX_ATTEMPTS}): {e}"
                )
                if attempt < _DRAFT_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_DRAFT_RETRY_DELAY * (attempt + 1))
                    continue
                failures = await _bump_draft_failure(chat_id, draft_id_int)
                if failures >= 6:
                    await mark_draft_dead(draft_id_int)
                return 0
            except aiohttp.ClientError as e:
                logger.warning(
                    f"send_rich_message_draft client error "
                    f"(attempt {attempt + 1}/{_DRAFT_MAX_ATTEMPTS}): {e}"
                )
                if attempt < _DRAFT_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_DRAFT_RETRY_DELAY * (attempt + 1))
                    continue
                failures = await _bump_draft_failure(chat_id, draft_id_int)
                if failures >= 6:
                    await mark_draft_dead(draft_id_int)
                return 0
            except Exception as e:
                logger.exception(f"send_rich_message_draft unexpected error: {e}")
                failures = await _bump_draft_failure(chat_id, draft_id_int)
                if failures >= 6:
                    await mark_draft_dead(draft_id_int)
                return 0

    return 0

# ---------- 发送普通富文本消息 ----------
async def send_rich_html_message(
    chat_id: int,
    html_content: str,
    reply_parameters: Optional[Dict] = None,
    reply_markup: Optional[Dict] = None,
    message_thread_id: Optional[int] = None,
    reassert_draft: bool = False,
) -> int | bool:
    """
    发送永久富文本消息。

    reassert_draft:
      False — 仅串行发送，不重新挂回草稿。适合绝大多数永久消息，
              例如停止提示、清空确认、错误提示、最终回复等。
      True  — 若该 chat 仍有活跃草稿，则在发送后立刻 reassert 草稿，
              仅在你确实想让草稿继续贴在新消息下方时使用。
    """
    if not html_content or not html_content.strip():
        return False
    html_content = html_content.strip()
    # 自动转义富文本 src/href 属性中的裸 &（R2 预签名 URL 等），防止 Telegram 误解析媒体或链接
    html_content = _escape_media_src_urls(html_content)

    payload = {
        "chat_id": chat_id,
        "rich_message": {
            "content": html_content,
            "html": html_content,
        },
        "disable_notification": False,
        "protect_content": False,
    }
    if reply_parameters:
        payload["reply_parameters"] = reply_parameters
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id

    # 永久消息需要比草稿更强的送达可靠性，因此保留重试；但此前完全没有设置
    # timeout（aiohttp 默认是几分钟级），一旦网络抖动或 Telegram 侧偶发变慢，
    # 三次重试 × 每次可能挂到默认超时，会让调用方（草稿滚动）阻塞数分钟。
    # 给一个不算激进的有界超时：单次总超时 15s、连接超时 5s，三次重试封顶
    # 约 45~90s（含 1s/4s/7s 退避），比之前的"无上限"收窄了一个数量级，
    # 同时仍然给网络抖动足够的恢复空间。
    @retry_async(max_retries=3, delay=1, backoff=3, exceptions=(aiohttp.ClientError, asyncio.TimeoutError))
    async def _send_inner():
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{BASE_URL}/sendRichMessage", json=payload) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            msg_id = data.get("result", {}).get("message_id")
                            if isinstance(msg_id, int) and msg_id > 0:
                                return msg_id
                        except Exception as e:
                            logger.debug(f"sendRichHtmlMessage parse response failed: {e}")
                        return True
                    body = await resp.text()
                    body_lower = body.lower()
                    content_required = (
                        resp.status == 400 and "rich_message_content_required" in body_lower
                    )
                    fallback_html = _rich_message_plain_text_fallback(html_content)
                    if content_required and fallback_html and fallback_html != html_content:
                        fallback_payload = {
                            **payload,
                            "rich_message": {
                                "content": fallback_html,
                                "html": fallback_html,
                            },
                        }
                        logger.warning(
                            "sendRichHtmlMessage received RICH_MESSAGE_CONTENT_REQUIRED; "
                            "retrying once with a safe paragraph fallback"
                        )
                        async with session.post(f"{BASE_URL}/sendRichMessage", json=fallback_payload) as fallback_resp:
                            if fallback_resp.status == 200:
                                try:
                                    fallback_data = await fallback_resp.json()
                                    fallback_msg_id = fallback_data.get("result", {}).get("message_id")
                                    if isinstance(fallback_msg_id, int) and fallback_msg_id > 0:
                                        return fallback_msg_id
                                except Exception as e:
                                    logger.debug(f"sendRichHtmlMessage fallback parse failed: {e}")
                                return True
                            fallback_body = await fallback_resp.text()
                            logger.error(
                                "sendRichHtmlMessage fallback failed: %s %s",
                                fallback_resp.status,
                                fallback_body[:200],
                            )
                            return False
                    logger.error(f"sendRichHtmlMessage failed: {resp.status} {body[:200]}")
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise
        except Exception as e:
            logger.exception(f"sendRichHtmlMessage unexpected exception: {e}")
            return False

    async with serialize_with_active_draft(chat_id, reassert=reassert_draft):
        return await _send_inner()

async def send_rich_message(
    chat_id: int,
    text: str,
    parse_mode: str = "html",
    reply_parameters: dict = None,
) -> bool:
    if not text or not text.strip():
        text = "⚠️ No content to send"
    return await send_rich_html_message(chat_id, text, reply_parameters, reassert_draft=False)

# ==================== 发送 Chat Action ====================
async def send_chat_action(chat_id: int, action: str) -> None:
    payload = {"chat_id": chat_id, "action": action}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{BASE_URL}/sendChatAction", json=payload) as resp:
                if resp.status != 200:
                    logger.warning(f"sendChatAction failed: {await resp.text()}")
    except Exception as e:
        logger.warning(f"sendChatAction exception: {e}")

# ========== 富消息文本提取 ==========
def _extract_rich_message_text(rich_obj: Union[dict, list, str]) -> str:
    if isinstance(rich_obj, str):
        return rich_obj
    if isinstance(rich_obj, list):
        parts = []
        for item in rich_obj:
            parts.append(_extract_rich_message_text(item))
        return "".join(parts)
    if isinstance(rich_obj, dict):
        block_type = rich_obj.get("type")
        if block_type == "paragraph":
            return _extract_rich_message_text(rich_obj.get("text", ""))
        elif block_type == "list":
            items = rich_obj.get("items", [])
            item_texts = []
            for item in items:
                label = item.get("label", "")
                blocks = item.get("blocks", [])
                content = _extract_rich_message_text(blocks)
                if label:
                    item_texts.append(f"{label} {content}")
                else:
                    item_texts.append(content)
            return "\n".join(item_texts)
        elif block_type in ("bold", "italic", "underline", "strikethrough", "code", "spoiler"):
            return _extract_rich_message_text(rich_obj.get("text", ""))
        else:
            result = []
            for key, value in rich_obj.items():
                if key in ("text", "blocks", "items"):
                    result.append(_extract_rich_message_text(value))
            return "".join(result)
    return ""

def extract_message_text(message: dict) -> str:
    if not message:
        return ""
    text = message.get("text")
    if text:
        return text
    caption = message.get("caption")
    if caption:
        return caption
    rich = message.get("rich_message")
    if rich:
        if isinstance(rich, str):
            plain = _rich_message_to_text(rich)
            if plain:
                return plain
        elif isinstance(rich, (dict, list)):
            plain = _extract_rich_message_text(rich)
            if plain:
                return plain
    if message.get("photo") or message.get("video") or message.get("audio") or message.get("document"):
        return "[媒体内容]"
    if message.get("sticker"):
        return "[贴纸]"
    if message.get("voice"):
        return "[语音消息]"
    content = message.get("content")
    if isinstance(content, str):
        return content
    return ""

def _rich_message_to_text(rich_content: str) -> str:
    if not rich_content:
        return ""
    text = rich_content.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    def replace_table(match):
        table_html = match.group(0)
        rows = re.findall(r'<tr>(.*?)</tr>', table_html, re.DOTALL)
        lines = []
        for row in rows:
            cells = re.findall(r'<t[dh]>(.*?)</t[dh]>', row, re.DOTALL)
            cell_texts = [re.sub(r'<[^>]+>', '', cell).strip() for cell in cells]
            lines.append("| " + " | ".join(cell_texts) + " |")
        return "\n".join(lines)
    text = re.sub(r'<table[^>]*>.*?</table>', replace_table, text, flags=re.DOTALL)
    def replace_list(match):
        list_html = match.group(0)
        if '<ol' in list_html:
            items = re.findall(r'<li>(.*?)</li>', list_html, re.DOTALL)
            numbered = [f"{i+1}. {re.sub(r'<[^>]+>', '', item).strip()}" for i, item in enumerate(items)]
            return "\n".join(numbered)
        else:
            items = re.findall(r'<li>(.*?)</li>', list_html, re.DOTALL)
            bulleted = [f"• {re.sub(r'<[^>]+>', '', item).strip()}" for item in items]
            return "\n".join(bulleted)
    text = re.sub(r'<(ul|ol)[^>]*>.*?</\1>', replace_list, text, flags=re.DOTALL)
    def replace_details(match):
        details = match.group(0)
        summary = re.search(r'<summary>(.*?)</summary>', details, re.DOTALL)
        summary_text = re.sub(r'<[^>]+>', '', summary.group(1)).strip() if summary else "详情"
        content = re.sub(r'<summary>.*?</summary>', '', details, flags=re.DOTALL)
        content_text = re.sub(r'<[^>]+>', '', content).strip()
        return f"[{summary_text}]\n{content_text}" if content_text else f"[{summary_text}]"
    text = re.sub(r'<details[^>]*>.*?</details>', replace_details, text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    return text.strip()

async def transcribe_audio_with_groq(audio_bytes: bytes, file_ext: str = ".ogg") -> str:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY 未设置，无法转录")

    ext_map = {
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4a-latm",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
    }
    content_type = ext_map.get(file_ext.lower(), "audio/ogg")

    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    data = {
        "model": "whisper-large-v3-turbo",
        "language": "zh",
        "response_format": "json"
    }

    form = aiohttp.FormData()
    form.add_field(
        "file",
        audio_bytes,
        filename=f"audio{file_ext}",
        content_type=content_type
    )
    for key, value in data.items():
        form.add_field(key, str(value))

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=form, timeout=30) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise Exception(f"Groq 转录失败 (HTTP {resp.status}): {err_text[:200]}")
            result = await resp.json()
            return result.get("text", "").strip()
