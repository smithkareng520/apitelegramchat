"""Telegram 消息发送/删除与草稿滚动状态机（自 utils.py 拆出）。

包含 deleteMessage 两套路径、草稿节流状态机（250ms 最小间隔、
失败计数、死亡标记、与用户消息串行化）、sendRichMessage 最终发送
与 sendChatAction。
"""

import json
import logging
import re
import time
import asyncio
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any, Dict, Optional, Tuple, cast

import aiohttp

from config import BASE_URL
from core.logging_setup import logger
from core.http_session import get_http_session
from core.text_utils import retry_async
from core.chat_guard import _notify_chat_unreachable
from core.rich_media import (
    _demote_all_media_to_links,
    _rich_message_html_payload,
    _rich_message_plain_text_fallback,
    _selective_media_fallback,
)


class RateLimitError(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")

@retry_async(max_retries=5, delay=0.5, backoff=3.0, exceptions=(aiohttp.ClientError, asyncio.TimeoutError, RateLimitError))
async def delete_message(chat_id: int, message_id: int) -> None:
    from state import deleted_message_ids, deleted_messages_lock, is_protected_message
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
                elif r.status == 400:
                    # "message to delete not found"：消息已不存在（例如草稿
                    # 气泡已被永久消息挤掉）。视为幂等成功，否则会被重试
                    # 装饰器白白发 5 次请求、耗时 6.5s 后仍抛异常。
                    body = await r.text()
                    if "not found" in body.lower():
                        async with deleted_messages_lock:
                            deleted_message_ids.add(message_id)
                        logger.debug(
                            f"deleteMessage 幂等成功（消息已不存在）: chat={chat_id} msg={message_id}"
                        )
                        return
                    logger.error(f"deleteMessage 失败 HTTP 400: {body[:200]}")
                    raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message=body)
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
        from state import deleted_message_ids, deleted_messages_lock
        async with deleted_messages_lock:
            if message_id in deleted_message_ids:
                return True
    except Exception:
        logger.debug("delete_message_fast 内部忽略的异常", exc_info=True)
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
                        from state import deleted_message_ids, deleted_messages_lock
                        async with deleted_messages_lock:
                            deleted_message_ids.add(message_id)
                    except Exception:
                        logger.debug("delete_message_fast 内部忽略的异常", exc_info=True)
                        pass
                    return True
                if r.status == 400:
                    body = await r.text()
                    if "not found" in body.lower() or "to delete not found" in body.lower():
                        try:
                            from state import deleted_message_ids, deleted_messages_lock
                            async with deleted_messages_lock:
                                deleted_message_ids.add(message_id)
                        except Exception:
                            logger.debug("delete_message_fast 内部忽略的异常", exc_info=True)
                            pass
                        return True
                return False
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug(f"delete_message_fast 失败: chat={chat_id} msg={message_id} {e}")
        return False

# ==================== Rich Message 支持 ====================
# 草稿发送侧状态曾以 4 个平行的 module-level dict 存储
# （_last_sent_draft_cache / _draft_send_locks / _draft_failure_counts /
# _draft_last_send_time），键一致但锁语义分散：注册表的增删由注册表锁
# 保护，而值的读写依赖「持有该 draft 的 send lock」这一隐式约定，
# 后续改动容易顾此失彼（改了 A dict 忘了 B dict）。现合并为每个
# (chat_id, draft_id) 一个 _DraftSendState 对象：
#   - 注册表 _draft_states 的条目增删由 _draft_states_lock 保护；
#   - 同一 draft 的字段值只在持有该 draft 的 state.lock（即原 send
#     lock）的协程里读写，不变量收敛到单一对象上。
# 并发模型说明（为什么不用 contextvars）：草稿状态按 (chat_id, draft_id)
# 跨任务共享——发送循环、serialize_with_active_draft、mark_draft_dead
# 等 caller 分属不同 asyncio Task，必须看到同一份状态；contextvars 的
# 「每任务隔离」语义正好相反，故不采用。
@dataclass
class _DraftSendState:
    """单个 (chat_id, draft_id) 草稿的全部发送侧可变状态。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_html: Optional[str] = None   # 最近一次成功发送（或降级重发）的草稿 HTML
    failures: int = 0                 # 连续发送失败计数（达到阈值判死）
    last_send_time: float = 0.0       # 最近一次发送时刻（time.monotonic()）


_draft_states: Dict[Tuple[int, int], _DraftSendState] = {}
_draft_states_lock = asyncio.Lock()
_dead_draft_ids: set[int] = set()
_dead_draft_ids_lock = asyncio.Lock()
_DRAFT_MIN_INTERVAL = 0.25
# 草稿是可被后续完整状态替代的瞬态 UI；不能像永久消息一样在发送锁中
# 连续执行长超时重试，否则一帧网络抖动会让所有后续 Agent 状态长时间排队。
_DRAFT_REQUEST_TIMEOUT = 5.0
_DRAFT_CONNECT_TIMEOUT = 2.5
_DRAFT_MAX_ATTEMPTS = 2
_DRAFT_RETRY_DELAY = 0.25

async def _get_draft_state(chat_id: int, draft_id: int) -> _DraftSendState:
    """原子地取 (chat_id, draft_id) 的发送侧状态，不存在则创建。"""
    key = (chat_id, draft_id)
    async with _draft_states_lock:
        state = _draft_states.get(key)
        if state is None:
            state = _DraftSendState()
            _draft_states[key] = state
        return state


async def _peek_draft_state(chat_id: int, draft_id: int) -> Optional[_DraftSendState]:
    """只读查找，不创建（用于保活路径，避免为已回收的草稿复活注册表条目）。"""
    async with _draft_states_lock:
        return _draft_states.get((chat_id, draft_id))


async def _get_draft_send_lock(chat_id: int, draft_id: int) -> asyncio.Lock:
    return (await _get_draft_state(chat_id, draft_id)).lock

async def _reset_draft_failure(chat_id: int, draft_id: int) -> None:
    state = await _get_draft_state(chat_id, draft_id)
    state.failures = 0

async def _bump_draft_failure(chat_id: int, draft_id: int) -> int:
    state = await _get_draft_state(chat_id, draft_id)
    state.failures += 1
    return state.failures

async def _cleanup_dead_draft_state(chat_id: int, draft_id: int) -> None:
    """草稿生命周期结束后，主动清理其发送侧状态（注册表条目整体移除）。

    此前 4 个 module-level dict 没有清理路径，长时间运行会让每个草稿的
    元数据永久驻留，造成内存泄漏。这里在 mark_draft_dead 之后统一回收。
    """
    if not isinstance(chat_id, int) or not isinstance(draft_id, int):
        return
    key = (chat_id, draft_id)
    async with _draft_states_lock:
        _draft_states.pop(key, None)

async def mark_draft_dead(draft_id: int | str | None) -> None:
    try:
        draft_id_int = int(draft_id)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return
    async with _dead_draft_ids_lock:
        _dead_draft_ids.add(draft_id_int)
    logger.info(f"Draft {draft_id_int} marked as dead")
    # 顺手清理可能仍持有的草稿状态。chat_id 在 mark 阶段无法可靠得到，
    # 我们只能扫描所有 (chat_id, draft_id_int) 键，但数量通常很小。
    async with _draft_states_lock:
        stale_keys = [k for k in _draft_states if k[1] == draft_id_int]
    for key in stale_keys:
        await _cleanup_dead_draft_state(key[0], draft_id_int)

async def is_draft_dead(draft_id: int | str | None) -> bool:
    try:
        draft_id_int = int(draft_id)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return True
    async with _dead_draft_ids_lock:
        return draft_id_int in _dead_draft_ids

async def _is_current_active_draft(chat_id: int, draft_id: int | str | None) -> bool:
    """只有当前仍然是活跃草稿时才允许继续刷新。"""
    try:
        draft_id_int = int(draft_id)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    try:
        from state import get_active_draft_info
        info = await get_active_draft_info(chat_id)
    except Exception:
        # 取不到状态时不要误伤发送，交给死亡标记兜底
        logger.debug("_is_current_active_draft 内部忽略的异常", exc_info=True)
        return True
    if not info:
        return False
    try:
        return int(info[0]) == draft_id_int
    except Exception:
        logger.debug("_is_current_active_draft 内部忽略的异常", exc_info=True)
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
        state = await _peek_draft_state(chat_id, draft_id)
        html_content = state.last_html if state is not None else None
        if not html_content or not str(html_content).strip():
            return
        # html_content 非空蕴含 state 非 None（last_html 只存在 state 里）。
        assert state is not None

        payload = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "rich_message": _rich_message_html_payload(html_content),
        }
        # reassert 只是视觉保活，失败可由下一次真实 flush 恢复；不应占用草稿锁过久。
        session = await get_http_session()
        async with session.post(
            f"{BASE_URL}/sendRichMessageDraft",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=4, connect=2),
        ) as resp:
                if resp.status == 200:
                    state.last_send_time = time.monotonic()
                    try:
                        data = await resp.json()
                        msg_id = (data.get("result") or {}).get("message_id")
                        if isinstance(msg_id, int) and msg_id > 0:
                            logger.debug(
                                f"reassert draft ok: chat={chat_id} draft={draft_id} msg_id={msg_id}"
                            )
                    except Exception:
                        logger.debug("_reassert_active_draft_content 内部忽略的异常", exc_info=True)
                        pass
                else:
                    body = ""
                    try:
                        body = await resp.text()
                    except Exception:
                        logger.debug("_reassert_active_draft_content 内部忽略的异常", exc_info=True)
                        pass
                    logger.debug(
                        f"reassert draft failed: chat={chat_id} draft={draft_id} "
                        f"status={resp.status} body={body[:120]}"
                    )
    except Exception as e:
        logger.debug(f"reassert draft exception: chat={chat_id} draft={draft_id} {e}")


@asynccontextmanager
async def serialize_with_active_draft(chat_id: int, *, reassert: bool = True) -> AsyncIterator[None]:
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
        from state import get_active_draft_info
        info = await get_active_draft_info(chat_id)
        if info:
            draft_id = int(info[0])
    except Exception:
        logger.debug("serialize_with_active_draft 内部忽略的异常", exc_info=True)
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
                logger.debug("serialize_with_active_draft 内部忽略的异常", exc_info=True)
                pass
            await _reassert_active_draft_content(chat_id, draft_id)


# ---------- 常规草稿发送（带锁） ----------
async def send_rich_message_draft(
    chat_id: int,
    draft_id: int | str | None,
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
    try:
        draft_id_int = int(draft_id)  # type: ignore[arg-type]
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

        state = await _get_draft_state(chat_id, draft_id_int)
        last_sent = state.last_html
        if not force and last_sent == html_content:
            return 0

        if not force:
            last_time = state.last_send_time
            wait_for_slot = _DRAFT_MIN_INTERVAL - (time.monotonic() - last_time)
            if wait_for_slot > 0:
                # 不直接丢弃这次新状态。等待至多 250ms 后发送，避免 builder 把
                # pending_chars 清零、随后只能等静默保活周期才重新显示更新。
                await asyncio.sleep(wait_for_slot)
                if await is_draft_dead(draft_id_int):
                    return 0
                if not await _is_current_active_draft(chat_id, draft_id_int):
                    return 0
                if state.last_html == html_content:
                    return 0

        payload = {
            "chat_id": chat_id,
            "draft_id": draft_id_int,
            "rich_message": _rich_message_html_payload(html_content),
        }
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        # 草稿帧可被更晚的完整帧覆盖。把单次等待限制在 5 秒，并至多做一次
        # 短暂重试（按请求传入超时），避免网络抖动时的锁占用造成前端"卡住"。
        for attempt in range(_DRAFT_MAX_ATTEMPTS):
            try:
                session = await get_http_session()
                async with session.post(
                    f"{BASE_URL}/sendRichMessageDraft",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(
                        total=_DRAFT_REQUEST_TIMEOUT,
                        connect=_DRAFT_CONNECT_TIMEOUT,
                    ),
                ) as resp:
                        body = ""
                        if resp.status != 200:
                            try:
                                body = await resp.text()
                            except Exception:
                                logger.debug("send_rich_message_draft 内部忽略的异常", exc_info=True)
                                body = ""

                        if resp.status == 200:
                            state.last_send_time = time.monotonic()
                            state.last_html = html_content
                            await _reset_draft_failure(chat_id, draft_id_int)
                            try:
                                data = await resp.json()
                                msg_id = (data.get("result") or {}).get("message_id")
                                if isinstance(msg_id, int) and msg_id > 0:
                                    return msg_id
                            except Exception:
                                logger.debug("send_rich_message_draft 内部忽略的异常", exc_info=True)
                                pass
                            return 0

                        if resp.status == 429:
                            try:
                                data = json.loads(body)
                                retry_after = int(data.get("parameters", {}).get("retry_after", 5))
                            except Exception:
                                logger.debug("send_rich_message_draft 内部忽略的异常", exc_info=True)
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
                            state.last_html = html_content
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

                        # 媒体抓取失败类错误（RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND /
                        # RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND）是不可恢复的内容问题：
                        # 同一个 URL 再发多少次都会被 Telegram 拒绝。若只 bump
                        # failure 并 return，builder 的 flush 循环会继续用原始内容
                        # 重试，最多累计 6 次失败才 mark dead，用户看到草稿卡很久。
                        # 因此立即在同一个调用内把所有媒体降级为 <a> 链接并重试一次，
                        # 失败一次就直接降级，不让上层循环重复无效请求。
                        media_not_found = (
                            "rich_message_photo_no_media_found" in body_lower
                            or "rich_message_video_no_media_found" in body_lower
                        )
                        if media_not_found:
                            demoted = _demote_all_media_to_links(html_content)
                            if demoted and demoted != html_content:
                                demoted_payload = {
                                    **payload,
                                    "rich_message": _rich_message_html_payload(demoted),
                                }
                                logger.warning(
                                    "sendRichMessageDraft 媒体抓取失败，立即降级为链接重试: "
                                    "chat=%s draft=%s orig_len=%s demoted_len=%s",
                                    chat_id, draft_id_int, len(html_content), len(demoted),
                                )
                                try:
                                    async with session.post(
                                        f"{BASE_URL}/sendRichMessageDraft",
                                        json=demoted_payload,
                                        timeout=aiohttp.ClientTimeout(
                                            total=_DRAFT_REQUEST_TIMEOUT,
                                            connect=_DRAFT_CONNECT_TIMEOUT,
                                        ),
                                    ) as demoted_resp:
                                        if demoted_resp.status == 200:
                                            state.last_send_time = time.monotonic()
                                            state.last_html = demoted
                                            await _reset_draft_failure(chat_id, draft_id_int)
                                            try:
                                                demoted_data = await demoted_resp.json()
                                                demoted_msg_id = (demoted_data.get("result") or {}).get("message_id")
                                                if isinstance(demoted_msg_id, int) and demoted_msg_id > 0:
                                                    return demoted_msg_id
                                            except Exception:
                                                logger.debug("send_rich_message_draft 内部忽略的异常", exc_info=True)
                                                pass
                                            return 0
                                        demoted_body = await demoted_resp.text()
                                        logger.warning(
                                            "sendRichMessageDraft 降级后仍失败: %s %s",
                                            demoted_resp.status, demoted_body[:200],
                                        )
                                except Exception as demoted_err:
                                    logger.warning(
                                        "sendRichMessageDraft 降级重试异常: %s", demoted_err,
                                    )

                        # 403 类永久性失败（用户屏蔽 bot 等）：熔断该 chat 的
                        # 主动唤醒调度，并立即判死本草稿——flush 循环继续用
                        # 原内容重试只会无限撞墙，草稿永远出不去。
                        if await _notify_chat_unreachable(chat_id, resp.status, body):
                            await mark_draft_dead(draft_id_int)
                            return 0

                        failures = await _bump_draft_failure(chat_id, draft_id_int)
                        logger.warning(
                            f"sendRichMessageDraft failed (attempt {attempt+1}/{_DRAFT_MAX_ATTEMPTS}, failures={failures}): "
                            f"{resp.status} {body[:200]}"
                        )
                        if hard_not_found and failures >= 5 or failures >= 6:
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
# 检测永久消息是否携带 <video> 媒体块：命中时在发送期间显示 upload_video
# （bot 侧“发送视频”动作，与 chat_actions.py 白名单语义一致）。
# 只匹配真正的标签开头，避免误匹配纯文本里的 “<video” 字样或已转义的
# &lt;video；大小写不敏感，兼容自闭合 <video/>。
_VIDEO_TAG_RE = re.compile(r"<video[\s/>]", re.IGNORECASE)


def _rich_html_contains_video(html_content: Optional[str]) -> bool:
    """永久富文本是否携带 <video> 媒体块（用于触发 upload_video 状态）。"""
    try:
        return bool(html_content) and bool(_VIDEO_TAG_RE.search(cast(str, html_content)))
    except Exception:
        logger.debug("_rich_html_contains_video 内部忽略的异常", exc_info=True)
        return False


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

    chat action：当消息携带 <video> 媒体块时，发送期间会显示 upload_video
    （bot 正在发送视频；4 秒循环重发，覆盖 Telegram 服务端拉取视频可能
    耗费的数十秒）。草稿刷新（sendRichMessageDraft）不触发任何动作：
    草稿是流式预览，属 typing 语义，且高频刷新会与状态循环互相干扰。

    reassert_draft:
      False — 仅串行发送，不重新挂回草稿。适合绝大多数永久消息，
              例如停止提示、清空确认、错误提示、最终回复等。
      True  — 若该 chat 仍有活跃草稿，则在发送后立刻 reassert 草稿，
              仅在你确实想让草稿继续贴在新消息下方时使用。
    """
    if not html_content or not html_content.strip():
        return False

    # 记录调用方交付给 Telegram 的原始富文本，不对内容做压缩、截断或预览。
    # 保留 strip 之前的版本，便于排查空白、换行和富媒体 URL 在发送前后的差异。
    raw_html_content = html_content
    # INFO 只输出长度与前 200 字符，避免大消息打爆日志。
    logger.info(
        "[%s] Telegram sendRichMessage 原始内容（长度=%s）：%s",
        chat_id,
        len(raw_html_content),
        raw_html_content[:200],
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[%s] Telegram sendRichMessage 完整原始内容（未截断；长度=%s）：\n%s",
            chat_id,
            len(raw_html_content),
            raw_html_content,
        )
    html_content = html_content.strip()

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": _rich_message_html_payload(html_content),
        "disable_notification": False,
        "protect_content": False,
    }
    # 记录实际 HTTP payload 中的完整 HTML；该内容与上方原始 HTML 一致（仅去首尾空白）。
    payload_html_content = payload["rich_message"]["html"]
    logger.info(
        "[%s] Telegram sendRichMessage payload HTML（长度=%s）：%s",
        chat_id,
        len(payload_html_content),
        payload_html_content[:200],
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[%s] Telegram sendRichMessage 完整 payload HTML（未截断；长度=%s）：\n%s",
            chat_id,
            len(payload_html_content),
            payload_html_content,
        )
    if reply_parameters:
        payload["reply_parameters"] = reply_parameters
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id

    # 永久消息需要比草稿更强的送达可靠性，因此保留重试；但不能不设
    # timeout（aiohttp 默认是几分钟级），否则一旦网络抖动或 Telegram 侧
    # 偶发变慢，三次重试 × 每次可能挂到默认超时，会让调用方（草稿滚动）
    # 阻塞数分钟。这里给一个不算激进的有界超时：单次总超时 15s、连接
    # 超时 5s，三次重试封顶约 45~90s（含 1s/4s/7s 退避），同时仍然给
    # 网络抖动足够的恢复空间。
    @retry_async(max_retries=3, delay=1, backoff=3, exceptions=(aiohttp.ClientError, asyncio.TimeoutError))
    async def _send_inner() -> int:
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # ---------- 第 1 次尝试：原样发送 ----------
                async with session.post(f"{BASE_URL}/sendRichMessage", json=payload) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            msg_id = (data.get("result") or {}).get("message_id")
                            if isinstance(msg_id, int) and msg_id > 0:
                                return msg_id
                        except Exception as e:
                            logger.debug(f"sendRichHtmlMessage parse response failed: {e}")
                        return True
                    body = await resp.text()
                    body_lower = body.lower()
                    # 只有明确的内容错误才进入针对性兜底。网络错误由装饰器重试，
                    # 认证、权限、限流和参数错误不能靠改 HTML 修复，必须原样失败。
                    if resp.status != 400:
                        # 403 类永久性失败（用户屏蔽 bot / 账号注销 / chat 不
                        # 存在）：重试与降级都救不回来。熔断该 chat 的主动唤
                        # 醒调度，避免 TIMER 每 5~20min 空转一轮完整 LLM
                        # 回合却永远送达不了；用户解除屏蔽后会自动恢复。
                        if await _notify_chat_unreachable(chat_id, resp.status, body):
                            return False
                        logger.error(f"sendRichHtmlMessage failed: {resp.status} {body[:200]}")
                        return False

                    body_lower = body.lower()
                    media_kinds: set[str] = set()
                    if "rich_message_photo_" in body_lower or "rich_message_photo_url_invalid" in body_lower:
                        media_kinds.add("img")
                    if "rich_message_video_" in body_lower or "rich_message_video_url_invalid" in body_lower:
                        media_kinds.add("video")
                    if "rich_message_audio_" in body_lower or "rich_message_audio_url_invalid" in body_lower:
                        media_kinds.add("audio")

                    # ---------- 第 2 次尝试：逐个排查有问题的媒体 ----------
                    # 不再一次性降级所有同类型媒体，而是逐个尝试找出有问题的那个。
                    # 策略：对每个媒体类型，提取所有该类型的媒体，逐个降级测试。
                    if media_kinds:
                        success_result = await _selective_media_fallback(
                            session, BASE_URL, payload, html_content, media_kinds
                        )
                        if success_result:
                            return success_result
                        
                        # 逐个排查失败，最后兜底：降级该类型所有媒体
                        media_demoted = _demote_all_media_to_links(
                            html_content,
                            media_kinds,
                        )
                        if media_demoted and media_demoted != html_content:
                            media_payload = {
                                **payload,
                                "rich_message": _rich_message_html_payload(media_demoted),
                            }
                            logger.warning(
                                "sendRichHtmlMessage retrying with ALL affected media demoted (last resort) "
                                "(kinds=%s, orig_len=%s, demoted_len=%s)",
                                sorted(media_kinds), len(html_content), len(media_demoted),
                            )
                            async with session.post(f"{BASE_URL}/sendRichMessage", json=media_payload) as fb_resp:
                                if fb_resp.status == 200:
                                    try:
                                        fb_data = await fb_resp.json()
                                        fb_msg_id = (fb_data.get("result") or {}).get("message_id")
                                        if isinstance(fb_msg_id, int) and fb_msg_id > 0:
                                            return fb_msg_id
                                    except Exception as e:
                                        logger.debug(f"sendRichHtmlMessage media-demoted parse failed: {e}")
                                    return True
                                fb_body = await fb_resp.text()
                                logger.warning(
                                    "sendRichHtmlMessage all-media fallback failed: %s %s",
                                    fb_resp.status, fb_body[:200],
                                )
                                return False

                    # ---------- 结构/内容错误：保留可见文字，去掉全部富文本标记 ----------
                    # CONTENT_REQUIRED 或未知 Rich Message 400 不是媒体问题，不应把
                    # 无辜的媒体改成链接；纯文本段落是最后一道、语义不丢失的兜底。
                    if "rich_message_content_required" in body_lower or "rich_message_" in body_lower:
                        plain_html = _rich_message_plain_text_fallback(html_content)
                        if plain_html and plain_html != html_content:
                            plain_payload = {
                                **payload,
                                "rich_message": _rich_message_html_payload(plain_html),
                            }
                            logger.warning(
                                "sendRichHtmlMessage retrying with plain-text paragraph fallback "
                                "after content/structure error (orig_len=%s, plain_len=%s)",
                                len(html_content), len(plain_html),
                            )
                            async with session.post(f"{BASE_URL}/sendRichMessage", json=plain_payload) as fb_resp:
                                if fb_resp.status == 200:
                                    try:
                                        fb_data = await fb_resp.json()
                                        fb_msg_id = (fb_data.get("result") or {}).get("message_id")
                                        if isinstance(fb_msg_id, int) and fb_msg_id > 0:
                                            return fb_msg_id
                                    except Exception as e:
                                        logger.debug(f"sendRichHtmlMessage plain fallback parse failed: {e}")
                                    return True
                                fb_body = await fb_resp.text()
                                logger.warning(
                                    "sendRichHtmlMessage plain-text fallback failed: %s %s",
                                    fb_resp.status, fb_body[:200],
                                )
                    logger.error("sendRichHtmlMessage 400 未命中可恢复错误类型: %s %s", resp.status, body[:200])
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise
        except Exception:
            logger.exception("sendRichHtmlMessage unexpected exception")
            return False

    # —— chat action：bot 正在发送视频 ——
    # 仅当永久消息携带 <video> 媒体块时触发 upload_video；发送（含内部
    # 至多 3 次重试、媒体降级重试）期间由 chat_actions 的 4 秒循环保活。
    # 周期导入：chat_actions 顶层依赖 utils，这里函数内延迟导入避免循环。
    _video_action = _rich_html_contains_video(html_content)
    if _video_action:
        from chat_actions import start_chat_action
        await start_chat_action(chat_id, "upload_video")
    try:
        async with serialize_with_active_draft(chat_id, reassert=reassert_draft):
            return await _send_inner()
    finally:
        if _video_action:
            from chat_actions import stop_chat_action
            await stop_chat_action(chat_id, "upload_video")

# ==================== 发送 Chat Action ====================
# 低层原语：直接 POST sendChatAction（单次、无循环）。
# 状态最多持续约 5 秒，长任务必须循环重发——该职责由 chat_actions.py
# 统一承担（4 秒重发循环 + 白名单 + 引用计数）。业务代码请勿直接调用
# 本函数，一律走 chat_actions。
async def send_chat_action(chat_id: int, action: str) -> None:
    payload = {"chat_id": chat_id, "action": action}
    # 必须设置超时：此前完全没设 timeout，Telegram API 偶尔 stall 时
    # 会无限期挂起协程，间接阻塞整个 chat 的活跃任务。
    timeout = aiohttp.ClientTimeout(total=5, connect=3)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{BASE_URL}/sendChatAction", json=payload) as resp:
                if resp.status != 200:
                    body = ""
                    try:
                        body = await resp.text()
                    except Exception:
                        pass
                    # 用户屏蔽 bot 时连 typing 指示都会 403：顺手熔断
                    #（幂等，仅标记 + 停调度，不影响本调用返回）。
                    await _notify_chat_unreachable(chat_id, resp.status, body)
                    logger.warning(f"sendChatAction failed: {body[:200]}")
    except asyncio.CancelledError:
        # chat_actions 的保活循环在任务收尾时会 cancel 本协程。CancelledError
        # 是协作式取消信号，不是错误：必须原样向上传播，否则取消语义被吞掉，
        # 调用方的 await task 会挂到超时。日志里那条空的
        # "sendChatAction exception: " 正是它——CancelledError 的 str() 为空串。
        raise
    except Exception as e:
        # 同样避免空日志：aiohttp 的多个异常（ServerTimeoutError、
        # ClientOSError 等）str() 常为空，只打 {e} 会得到无信息的一行。
        # 补上异常类型名，必要时还能看到 repr。
        detail = str(e) or repr(e)
        logger.warning(
            "sendChatAction exception: chat=%s action=%s %s: %s",
            chat_id, action, type(e).__name__, detail,
        )
