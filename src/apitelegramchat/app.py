# app.py
from quart import Quart, request
import asyncio
import aiohttp
import json
import logging
import uuid
import os
import time
import hmac
import mimetypes
from apitelegramchat.workspace_paths import workspace_download_root
from apitelegramchat.token_budget import count_tokens

from apitelegramchat.utils import (
    send_rich_html_message,
    delete_message,
    mark_draft_dead,
    check_deepseek_balance,
    check_openrouter_balance,
    send_chat_action,
    get_logger,
    set_request_id,
    extract_message_text,
    extract_sticker_metadata,
    sticker_metadata_to_text,
    transcribe_audio_with_groq,
)
from apitelegramchat.ai_handlers import get_ai_response, _get_cached_audio_data
from apitelegramchat.config import (
    BASE_URL,
    SUPPORTED_MODELS,
    SUPPORTED_ROLES,
    WEBHOOK_TOKEN,
    DEFAULT_MODEL,
    WHITELIST_USERS,
    ADMIN_USERS,
    save_whitelist,
    GROQ_API_KEY,
    LOG_TRUNCATE_LIMIT,
    LOG_LEVEL,
    global_lock,
)
from apitelegramchat.state import (
    user_contexts,
    user_models,
    role_message_ids,
    get_or_init_context,
    get_user_model,
    safe_clear_history,
    safe_set_user_model,
    get_chat_lock,
    add_media_group_message,
    pop_media_group,
    get_user_role,
    set_user_role,
    safe_clear_active_skill,
    get_active_draft_info,
    clear_active_draft,
    mark_preserved_draft,
    get_show_drafts,
    set_show_drafts,
    set_current_user_namespace,
    mark_update_processed_if_new,
)
from apitelegramchat import turn_recovery
from apitelegramchat.message_user_tool import (
    get_pending_for_chat,
    resolve_callback as resolve_message_user_callback,
    resolve_text as resolve_message_user_text,
)
from apitelegramchat.file_handlers import download_file
from apitelegramchat.workspace_utils import _get_workspace_lock, init_workspace
from apitelegramchat.context_manager import select_request_context
from apitelegramchat.tool_context_compaction import compact_older_tool_calls, _eligible_calls
from apitelegramchat import proactive

app = Quart(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

logger = get_logger(__name__)


@app.before_serving
async def _startup_load_whitelist() -> None:
    """启动时加载白名单。之前没人调过 load_whitelist，导致默认是空 set。"""
    try:
        from apitelegramchat.config import load_whitelist
        await load_whitelist()
    except Exception:
        logger.warning("startup load_whitelist failed", exc_info=True)
    # ── 主动唤醒（TIMER 事件源）初始化 ──
    # 注册回调后启动调度器：chat 在首次授权用户活动时才被跟踪。
    try:
        proactive.register_turn_runner(_handle_timer_wakeup)
        proactive.register_busy_check(_is_user_flow_active)
        await proactive.start_proactive_scheduler()
    except Exception:
        logger.warning("startup proactive scheduler failed", exc_info=True)


@app.after_serving
async def _shutdown_close_http_session() -> None:
    """优雅关闭：先停掉主动唤醒调度器，再关掉所有持久 bash 沙箱进程，
    最后关全局 aiohttp session。
    """
    try:
        await proactive.stop_proactive_scheduler()
    except Exception:
        logger.warning("shutdown proactive scheduler failed", exc_info=True)
    try:
        from apitelegramchat.tool_executors import _bash_manager
        await _bash_manager.cleanup_all()
    except Exception:
        logger.warning("shutdown _bash_manager.cleanup_all failed", exc_info=True)
    try:
        from apitelegramchat.utils import close_http_session
        await close_http_session()
    except Exception:
        logger.warning("shutdown close_http_session failed", exc_info=True)

def _cmd_match(t: str, name: str) -> bool:
    """严格命令匹配：以 / 开头，首段等于命令名（兼容 /cmd@botname 形式）。

    防止 /roleplay 误触发 /role、/clearall 误触发 /clear（后者会直接
    清空对话历史，属于数据丢失级误操作）。
    """
    if not t.startswith("/"):
        return False
    first = t.split(None, 1)[0] if t.strip() else t
    return first == name or first.startswith(name + "@")


def _reply_params(message_id: int | None) -> dict | None:
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return None
    if mid <= 0:
        return None
    # 被回复的消息可能已被删除（例如模型/角色列表已到 delete_after 定时
    # 清理时间，而客户端仍显示旧按钮，用户点击后回调才到达）。
    # allow_sending_without_reply 让反馈消息在引用目标丢失时降级为普通
    # 消息继续送达，避免"操作已生效但用户看不到任何反馈"。
    return {"message_id": mid, "allow_sending_without_reply": True}

# ---------- 上下文管理常量 ----------
# 触发工具负载压缩的历史长度阈值（可配）。注意：压缩会重写历史里的旧
# tool 消息（全量 payload → 指针文本），每次重写都会打碎 prompt 前缀
# 缓存。因此配套 HISTORY_COMPACTION_MIN_BATCH：未归档的可压缩调用
# 累积到一定数量才触发一次，而不是每轮都重写几条，让窗口内的前缀
# 在两次压缩之间保持字节稳定。
MAX_HISTORY_MESSAGES = int(os.getenv("HISTORY_COMPACTION_TRIGGER", "30") or "30")  # Trigger a tool-payload compaction pass; do not delete turns.
try:
    HISTORY_COMPACTION_MIN_BATCH = max(0, int(os.getenv("HISTORY_COMPACTION_MIN_BATCH", "8")))
except (TypeError, ValueError):
    HISTORY_COMPACTION_MIN_BATCH = 8
MEDIA_GROUP_TIMEOUT = 5
REPLY_MARKER = "💡 引用回复:"

active_tasks: dict[int, asyncio.Task] = {}
active_tasks_lock = asyncio.Lock()
# 消息去重已下沉到 state.mark_update_processed_if_new（原子检查+标记），
# 这里不需要 app 级别的去重锁。

# ==================== 打断旧轮次（保全进度 + 冻结旧草稿） ====================

async def _interrupt_active_generation(chat_id: int) -> None:
    """
    中断当前正在进行的生成任务，并把已完成的进度沉淀进历史。

    设计（打断保全，见 turn_recovery.py）：
      1. 打断进行中的 TIMER 主动唤醒回合（兜底，webhook 中心入口已调用过；
         重复调用无害）；
      2. 彻底取消并等待旧 USER 任务结束——包括其草稿刷新循环与所有在途
         刷新请求；
      3. 旧任务停止后，把该轮已完成的 assistant/tool 消息补齐占位
         tool_result 并写入持久历史（进度不丢弃，新轮次从断点继续）；
      4. 冻结旧草稿（mark dead + 注销活跃注册 + 标记保留），不再发送
         "⏹️ 已停止输出"之类的提示消息——该提示在打断保全机制下已无价值。

    时序说明：必须先确认旧任务（含草稿刷新循环）真正结束，再冻结草稿并
    启动新任务；否则旧任务迟到的刷新帧会排在新消息之后，出现"草稿位置
    错乱、在新消息下方重新刷新"的旧 bug。
    """
    # 0) 新用户回合接管时，同样要打断进行中的 TIMER 主动唤醒回合。
    #    （webhook 中心入口已对每条用户消息调用过一次；这里是兜底，
    #     覆盖媒体组聚合等待期结束后才启动用户回合等时序，重复调用无害。）
    try:
        await proactive.interrupt_proactive_flow(chat_id)
    except Exception as e:
        logger.warning(f"interrupt_proactive_flow 异常: {e}")

    # 0.1) 提前记录当前活跃草稿信息，因为取消旧任务后它的 finally 里可能
    #    会自己清掉这个注册，届时就取不到了。
    try:
        draft_info = await get_active_draft_info(chat_id)
    except Exception:
        logger.debug("_interrupt_active_generation 内部忽略的异常", exc_info=True)
        draft_info = None

    # 1) 先彻底停掉旧任务（包括其草稿刷新循环），确保没有任何后台刷新
    #    还在飞行中，再继续后面的步骤。
    await _cancel_old_task(chat_id)

    # 2) 旧任务已完全停止：轮次日志保全——已完成的 assistant/tool 消息
    #    补齐占位 tool_result 后沉淀进持久历史。新轮次的 user 消息会在
    #    get_ai_response 里按规则合并/追加（见 turn_recovery.persist_user_message_entry）。
    try:
        salvaged = await turn_recovery.finalize_pending_turns(chat_id, reason="user-interrupt")
        if salvaged:
            logger.info(f"打断保全：chat={chat_id} 已沉淀旧轮次进度 {salvaged} 条消息")
    except Exception as e:
        logger.warning(f"finalize_pending_turns 异常: {e}")

    if not draft_info:
        return

    draft_id, _msg_id = draft_info

    # 3) 旧任务已停止，标记草稿死亡：旧草稿冻结在当前位置，不再接收任何
    #    刷新。不再发送"已停止输出"提示消息（用户要求移除）。
    try:
        await mark_draft_dead(draft_id)
        logger.info(f"已标记草稿死亡: chat={chat_id} draft={draft_id}")
    except Exception as e:
        logger.warning(f"mark_draft_dead 异常: {e}")

    # 4) 注销活跃草稿注册，让新轮次的草稿能干净地接管。
    try:
        await clear_active_draft(chat_id, draft_id)
        logger.info(f"已清除活跃草稿注册: chat={chat_id} draft={draft_id}")
    except Exception as e:
        logger.warning(f"clear_active_draft 异常: {e}")

    # 5) 标记保留：冻结的旧草稿作为本轮进度的可见现场，任何"只删除草稿"
    #    的清理路径都不得碰它。
    try:
        await mark_preserved_draft(draft_id)
        logger.info(f"已标记草稿保留: chat={chat_id} draft={draft_id}")
    except Exception as e:
        logger.warning(f"mark_preserved_draft 异常: {e}")
# ---------------------------------------------------------------------------
# 辅助函数（保持不变）
# ---------------------------------------------------------------------------
async def _send_temp_message(chat_id: int, text: str) -> int:
    try:
        async with aiohttp.ClientSession() as session:
            payload = {"chat_id": chat_id, "text": text}
            async with session.post(f"{BASE_URL}/sendMessage", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # `data.get("result", {})` 在 result 为 None 时返回 None，
                    # 再 `.get` 会 AttributeError。用 `or {}` 兜底。
                    return (data.get("result") or {}).get("message_id")
    except Exception as e:
        logger.warning(f"_send_temp_message failed (chat_id={chat_id}): {e}")
    return None

@app.route('/health', methods=['GET'])
async def health_check():
    # 健康检查端点对外可访问，不应暴露内部统计信息（白名单数量、活跃任务数）。
    # 这些信息可能被探测方用于侧信道推断。
    return {
        "status": "ok",
    }, 200

# ---------- 权限辅助 ----------
def get_user_info(msg: dict) -> tuple[str, str]:
    from_user = msg.get("from", {})
    username = from_user.get("username", "").strip()
    user_id = str(from_user.get("id", ""))
    return username, user_id

def is_admin(username: str, user_id: str) -> bool:
    return bool(
        (username and username in ADMIN_USERS)
        or (user_id and user_id in ADMIN_USERS)
    )

def is_authorized(username: str, user_id: str) -> bool:
    if is_admin(username, user_id):
        return True
    return bool(
        (username and username in WHITELIST_USERS)
        or (user_id and user_id in WHITELIST_USERS)
    )

async def reply_unauthorized(chat_id: int, reply_message_id: int | None = None):
    await send_rich_html_message(
        chat_id,
        """
❌ <b>未授权访问</b></br>
您未被授权使用此机器人。</br>
请联系管理员 <b>@dearella</b> 申请白名单。
""",
        reply_parameters=_reply_params(reply_message_id),
    )

# ---------- 工具函数 ----------
def _get_reply_context(msg: dict) -> str:
    if "reply_to_message" not in msg:
        return ""
    reply = msg["reply_to_message"]
    quote_obj = msg.get("quote")
    quote = quote_obj.get("text", "") if quote_obj else ""
    if not quote:
        quote = extract_message_text(reply)
    if not quote:
        if any(key in reply for key in ("photo", "video", "audio", "document", "sticker", "voice")):
            quote = "[该消息为媒体内容，无文字引用]"
        else:
            quote = "[该消息无文字内容]"
    if REPLY_MARKER in quote:
        quote = quote.split(REPLY_MARKER)[-1].strip()
    if len(quote) > 800:
        quote = quote[:800] + "...(truncated)"
    return f"{REPLY_MARKER}\n> {quote}\n\n"

def _get_reply_media(msg: dict) -> dict:
    reply = msg.get("reply_to_message")
    if not reply:
        return {}
    if "photo" in reply:
        photos = reply["photo"]
        if photos:
            return {"type": "photo", "file_ids": [photos[-1]["file_id"]], "file_name": "photo.jpg"}
    if "document" in reply:
        doc = reply["document"]
        return {"type": "document", "file_id": doc["file_id"], "file_name": doc.get("file_name", "document"), "mime_type": doc.get("mime_type", "")}
    if "audio" in reply:
        audio = reply["audio"]
        return {"type": "audio", "file_id": audio["file_id"], "file_name": audio.get("file_name", "audio")}
    if "voice" in reply:
        voice = reply["voice"]
        return {"type": "voice", "file_id": voice["file_id"], "file_name": voice.get("file_name", "voice.ogg")}
    if "video" in reply:
        video = reply["video"]
        return {
            "type": "video",
            "file_id": video["file_id"],
            "file_name": video.get("file_name", "video.mp4"),
            "mime_type": video.get("mime_type", "video/mp4"),
        }
    if "video_note" in reply:
        vn = reply["video_note"]
        return {
            "type": "video",
            "file_id": vn["file_id"],
            "file_name": "video_note.mp4",
            "mime_type": "video/mp4",
        }
    return {}

# ---------- Token 估算及上下文修剪 ----------
_MEDIA_TOKEN_OVERHEAD = 64
_MESSAGE_WRAPPER_TOKENS = 4

def estimate_tokens(text: str) -> int:
    """Return the exact tokenizer count for model-facing text."""
    return count_tokens(text)

def _estimate_content_tokens(content) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            total += _estimate_content_tokens(part)
        return total
    if isinstance(content, dict):
        part_type = str(content.get("type", "")).lower()
        if part_type == "text":
            return estimate_tokens(str(content.get("text", "")))
        if part_type in {"image_url", "image", "input_image"}:
            return _MEDIA_TOKEN_OVERHEAD
        if part_type in {"file", "input_file", "document"}:
            filename = ""
            file_obj = content.get("file")
            if isinstance(file_obj, dict):
                filename = str(file_obj.get("filename", ""))
            return _MEDIA_TOKEN_OVERHEAD * 2 + estimate_tokens(filename)

        total = _MEDIA_TOKEN_OVERHEAD
        for value in content.values():
            if isinstance(value, (str, list, dict)):
                total += _estimate_content_tokens(value)
        return total
    return estimate_tokens(str(content))

def _estimate_message_tokens(message: dict) -> int:
    tokens = _MESSAGE_WRAPPER_TOKENS
    tokens += _estimate_content_tokens(message.get("content", ""))
    if message.get("name"):
        tokens += estimate_tokens(str(message["name"]))
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        try:
            tokens += estimate_tokens(json.dumps(tool_calls, ensure_ascii=False))
        except Exception:
            logger.debug("_estimate_message_tokens 内部忽略的异常", exc_info=True)
            tokens += _MEDIA_TOKEN_OVERHEAD * len(tool_calls)
    return tokens

def _estimate_request_snapshot(history: list[dict]) -> tuple[object, int]:
    """Select the next API snapshot and estimate its prompt cost."""
    snapshot = select_request_context(history)
    return snapshot, sum(_estimate_message_tokens(message) for message in snapshot.messages)


def _drop_oldest_non_system_block(history: list[dict]) -> bool:
    """Drop one oldest structural block without leaving orphaned tool messages.

    A user message owns every following non-system message up to the next user
    message.  If history starts with an assistant tool call, that call and its
    contiguous paired tool results are dropped together.  System messages are
    never selected for deletion.
    """
    start = next((index for index, message in enumerate(history) if message.get("role") != "system"), None)
    if start is None:
        return False

    first = history[start]
    if first.get("role") == "user":
        end = start + 1
        while end < len(history):
            role = history[end].get("role")
            if role in {"user", "system"}:
                break
            end += 1
        del history[start:end]
        return True

    if first.get("role") == "assistant":
        end = start + 1
        calls = first.get("tool_calls")
        expected_ids = {
            call.get("id")
            for call in calls
            if isinstance(call, dict) and isinstance(call.get("id"), str)
        } if isinstance(calls, list) else set()
        while end < len(history) and history[end].get("role") == "tool":
            call_id = history[end].get("tool_call_id")
            if expected_ids and call_id not in expected_ids:
                break
            end += 1
        del history[start:end]
        return True

    # A stray non-system message is removed only as a last structural unit.
    del history[start]
    return True


async def pre_flight_context_check(chat_id: int, new_user_message: dict) -> bool:
    """Apply two reversible compaction passes, then structural trimming if needed.

    The first pass archives the older half of eligible target-tool calls.  If
    still over budget, the second archives half of the remaining complete tool calls
    (about 75% cumulatively for even-sized sets).  Only if both passes fail does
    the final fallback remove the oldest non-system conversation blocks.
    """
    _pf_start = time.monotonic()
    _pf_lock_wait_start = time.monotonic()
    lock = await get_chat_lock(chat_id)
    _pf_lock_wait_ms = int((time.monotonic() - _pf_lock_wait_start) * 1000)
    if _pf_lock_wait_ms > 1000:
        logger.warning(
            "pre_flight_context_check 等待 chat_lock 超时: chat=%s wait_ms=%s",
            chat_id, _pf_lock_wait_ms,
        )
    async with lock:
        ctx = get_or_init_context(chat_id)
        history = ctx.setdefault("conversation_history", [])
        cm = get_user_model(chat_id)
        model_info = SUPPORTED_MODELS.get(cm)
        max_context = getattr(model_info, "max_context", None) or 128000
        max_output = getattr(model_info, "max_output_tokens", None) or 8192
        safe_limit = max_context - max_output
        new_input_est = max(1, _estimate_message_tokens(new_user_message))

        _, request_estimate = _estimate_request_snapshot(history)
        if request_estimate + new_input_est <= safe_limit:
            return True

        first_pass = await compact_older_tool_calls(chat_id, history)
        _, request_estimate = _estimate_request_snapshot(history)
        if first_pass.compacted_calls:
            logger.info(
                "Pre-flight tool compaction pass=1 chat=%s calls=%s archived_bytes=%s request_estimate=%s",
                chat_id,
                first_pass.compacted_calls,
                first_pass.archived_bytes,
                request_estimate,
            )
        if request_estimate + new_input_est <= safe_limit:
            return True

        remaining_calls = max(0, first_pass.eligible_calls - first_pass.compacted_calls_count)
        second_pass_count = max(1, remaining_calls // 2) if remaining_calls else 0
        second_pass = await compact_older_tool_calls(
            chat_id,
            history,
            calls_to_compact=second_pass_count,
        )
        _, request_estimate = _estimate_request_snapshot(history)
        if second_pass.compacted_calls:
            logger.info(
                "Pre-flight tool compaction pass=2 chat=%s calls=%s archived_bytes=%s request_estimate=%s",
                chat_id,
                second_pass.compacted_calls,
                second_pass.archived_bytes,
                request_estimate,
            )
        if request_estimate + new_input_est <= safe_limit:
            return True

        deleted_blocks = 0
        while request_estimate + new_input_est > safe_limit:
            if not _drop_oldest_non_system_block(history):
                break
            deleted_blocks += 1
            _, request_estimate = _estimate_request_snapshot(history)

        if deleted_blocks:
            # The old ledger no longer maps one-to-one to remaining history.
            ctx["token_ledger"] = []
            ctx["last_prompt_tokens"] = 0
            ctx["last_completion_tokens"] = 0
            logger.warning(
                "Pre-flight structural trim: chat=%s deleted_blocks=%s request_estimate=%s safe_limit=%s",
                chat_id,
                deleted_blocks,
                request_estimate,
                safe_limit,
            )

        # The only unserviceable case is a new user message that is oversized by
        # itself, or a system-only snapshot that is already beyond the budget.
        return request_estimate + new_input_est <= safe_limit

async def update_conversation_and_ledger(chat_id: int, user_message: dict | None, new_msgs: list, usage: dict = None) -> None:
    """将本轮对话写入持久历史并维护 token 台账。

    user_message 为 None 时（TIMER 主动唤醒回合）：不写入唤醒用的合成
    user 消息（timer 唤醒不写入历史），只沉淀回合产生的 assistant/tool
    消息，保证统一上下文的前提下不污染触发器痕迹。

    打断保全配合（见 turn_recovery.py）：USER 回合的 user 消息已在
    get_ai_response 开始时提前持久化（带 early-persisted 标记），此处
    跳过重复 append；无论哪种路径，消息写入完成后立即注销轮次登记
    （note_turn_persisted）——注销点与 append 同在 chat 锁内，保证
    "写入"与"注销"原子成对，取消竞态下既不双写也不漏写。
    """
    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        history = ctx.setdefault("conversation_history", [])
        if user_message is not None and not user_message.get(turn_recovery.EARLY_PERSIST_FLAG):
            block_content = user_message.get("content", "")
            if isinstance(block_content, str) and REPLY_MARKER in block_content:
                user_message["content"] = block_content.split(REPLY_MARKER)[-1].strip()
            history.append(user_message)
        # 历史标记清理：早持久化的消息进入历史时去掉内部标记。
        if isinstance(user_message, dict):
            user_message.pop(turn_recovery.EARLY_PERSIST_FLAG, None)
        for msg in new_msgs:
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                msg["content"] = msg["content"].strip()
            history.append(msg)
        # 消息已落历史：立即注销该轮的 in-flight 登记（在释放 chat 锁前）。
        if new_msgs:
            turn_recovery.note_turn_persisted(chat_id, new_msgs)
        if usage:
            if hasattr(usage, "model_dump"):
                usage_dict = usage.model_dump()
            elif hasattr(usage, "dict"):
                usage_dict = usage.dict()
            elif isinstance(usage, dict):
                usage_dict = usage
            else:
                usage_dict = {"prompt_tokens": getattr(usage, "prompt_tokens", 0),
                              "completion_tokens": getattr(usage, "completion_tokens", 0)}
            current_prompt = usage_dict.get("prompt_tokens", 0)
            current_comp = usage_dict.get("completion_tokens", 0)
            last_prompt = ctx.get("last_prompt_tokens", 0)
            last_comp = ctx.get("last_completion_tokens", 0)
            t_input = max(0, current_prompt - last_prompt - last_comp)
            t_output = current_comp
            ctx["last_prompt_tokens"] = current_prompt
            ctx["last_completion_tokens"] = current_comp
            ledger = ctx.setdefault("token_ledger", [])
            ledger.append({"input_tokens": t_input, "output_tokens": t_output})
        if len(history) > MAX_HISTORY_MESSAGES:
            # 批量触发：只有未归档的可压缩调用积累到 MIN_BATCH 才执行。
            # 每轮都重写少量旧消息会让 prompt 前缀缓存持续 miss；攒一批
            # 一次压缩，两次压缩之间的若干轮里历史字节保持稳定。
            unarchived_eligible = 0
            try:
                unarchived_eligible = len(_eligible_calls(history))
            except Exception:
                logger.debug("统计未归档工具调用失败", exc_info=True)
            if unarchived_eligible >= HISTORY_COMPACTION_MIN_BATCH:
                stats = await compact_older_tool_calls(chat_id, history)
                if stats.compacted_calls:
                    logger.info(
                        "History-size tool compaction: chat=%s calls=%s archived_bytes=%s",
                        chat_id,
                        stats.compacted_calls,
                        stats.archived_bytes,
                    )

# ---------------------------------------------------------------------------
# 业务处理
# ---------------------------------------------------------------------------
async def _cancel_old_task(chat_id: int):
    async with active_tasks_lock:
        task = active_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()
        # 给旧任务足够的退出窗口：它的 finally 里会 await 草稿刷新循环
        # （含所有在途的刷新请求）真正停止后才返回。这里的超时必须
        # 大于 RichMessageBuilder.stop_flush_loop() 内部的等待时间，
        # 否则我们会在旧的刷新请求还没落地前就抢先发送新消息，导致
        # 旧草稿的刷新“迟到”出现在新消息之后（显示错乱）。
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning(f"旧任务取消超时（>3s）: chat_id={chat_id}，转入后台等待其结束")
            asyncio.create_task(_log_task_cancel(task, chat_id))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"旧任务取消异常: chat_id={chat_id} {e}")

async def _log_task_cancel(task: "asyncio.Task", chat_id: int) -> None:
    try:
        await task
    except asyncio.CancelledError:
        logger.debug(f"旧任务已取消: chat_id={chat_id}")
    except Exception as e:
        logger.warning(f"旧任务清理异常: chat_id={chat_id} {e}")

async def _cleanup_task(chat_id: int, task: asyncio.Task):
    async with active_tasks_lock:
        is_current = chat_id in active_tasks and active_tasks.get(chat_id) == task
        if is_current:
            del active_tasks[chat_id]
    if is_current:
        # 当前 USER 回合完整结束（含异常收尾）：通知主动唤醒调度器布置
        # 下一次 TIMER（随机 5~20min）。若该任务是"被打断后由新回合替换"
        # 的旧任务（is_current=False），不打扰新回合——由新回合结束时再布置。
        try:
            await proactive.note_turn_finished(chat_id)
        except Exception as e:
            logger.warning(f"note_turn_finished 异常: {e}")

# -------------------- 各类型消息处理 --------------------
async def _handle_text_message(chat_id: int, user_input: str, username: str, user_message: dict):
    await send_chat_action(chat_id, "typing")
    # 后台预初始化 workspace：与模型生成响应并行，避免第一个工具调用
    # 是 no-op。
    asyncio.create_task(init_workspace(chat_id))
    try:
        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的内容过长，已超过模型单次处理极限，请分批或精简发送。")
            return
        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_handle_text_message 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理消息时出错</b>\n<code>{str(e)[:100]}</code>")

async def _handle_photo_message(chat_id: int, user_message: dict, username: str):
    await send_chat_action(chat_id, "upload_photo")
    asyncio.create_task(init_workspace(chat_id))
    try:
        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的图片附加内容过长，已超过模型单次处理极限，请精简发送。")
            return
        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_handle_photo_message 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理图片时出错</b>\n<code>{str(e)[:100]}</code>")

async def _handle_document_message(chat_id: int, user_message: dict, username: str):
    await send_chat_action(chat_id, "upload_document")
    asyncio.create_task(init_workspace(chat_id))
    try:
        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的文档内容过长，已超过模型单次处理极限，请精简发送。")
            return
        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_handle_document_message 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理文档时出错</b>\n<code>{str(e)[:100]}</code>")

async def _handle_audio_message(chat_id: int, user_message: dict, username: str):
    await send_chat_action(chat_id, "upload_voice")
    asyncio.create_task(init_workspace(chat_id))
    try:
        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的音频转录文本过长，已超过模型单次处理极限，请精简发送。")
            return
        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_handle_audio_message 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理音频时出错</b>\n<code>{str(e)[:100]}</code>")

async def _handle_video_message(chat_id: int, user_message: dict, username: str):
    """处理直接上传的视频 / 圆形视频消息（video_note）。

    与图片消息对称：user_message 携带 file_id / mime_type 等元数据存入
    对话历史，每轮由 _resolve_multimodal_content 按当前模型能力重新解析
    ——支持视频输入的模型（stealth/ox-alpha、Gemini 系列等）收到
    video_url content part，不支持的模型收到文本占位；切换模型不丢信息。
    """
    await send_chat_action(chat_id, "upload_video")
    asyncio.create_task(init_workspace(chat_id))
    try:
        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的视频附加内容过长，已超过模型单次处理极限，请精简发送。")
            return
        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_handle_video_message 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理视频时出错</b>\n<code>{str(e)[:100]}</code>")

async def _handle_sticker_message(chat_id: int, user_message: dict, username: str):
    """处理用户发送的贴纸（sticker）。

    贴纸本体（TGS / WebP / WebM）目前主流 LLM 不可直接识别，因此只把
    Telegram Sticker 对象携带的可语义化字段——emoji / emoji_list / type /
    set_name / custom_emoji_id / format——拼成文本作为 user 消息发给模型。
    不携带 file_id 附件，避免 _resolve_multimodal_content 把不可识别的
    媒体推到模型 API 触发 400。
    """
    await send_chat_action(chat_id, "typing")
    asyncio.create_task(init_workspace(chat_id))
    try:
        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的贴纸附加内容过长，已超过模型单次处理极限，请精简发送。")
            return
        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_handle_sticker_message 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理贴纸时出错</b>\n<code>{str(e)[:100]}</code>")


# ==================== TIMER 主动唤醒（统一事件源调度） ====================

def _is_user_flow_active(chat_id: int) -> bool:
    """该 chat 是否有用户发起（USER 事件源）的回合正在运行。

    注册给 proactive 调度器作为 busy check：用户回合进行中（含 message_user
    等待期）不触发 TIMER 唤醒，避免两个回合并发竞争同一份历史。
    """
    task = active_tasks.get(chat_id)
    return task is not None and not task.done()


async def _handle_timer_wakeup(chat_id: int):
    """TIMER 事件源回合：系统后台唤醒 agent 的“自己的活动时间”。

    与用户回合共用同一份会话历史（统一上下文），并走**同一套草稿与交付
    流程**（由 /show 开关统一决定，见 ai_handlers.get_ai_response）：
    - /show on：展示富文本草稿，最终回复经 sendRichMessage 送达用户；
    - /show off：静默运行，模型经 deliver_reply / message_user 触达用户；
    - 向请求上下文追加合成 user 消息（WAKEUP_PROMPT），但不写入持久历史；
    - 回合被用户消息打断时由 proactive.interrupt_proactive_flow 取消
      任务并触发 turn_recovery 轮次日志保全（已完成的进度沉淀进历史，
      此处无需感知）。
    """
    try:
        # 原生图片/视频模型不适合后台回合（会直接生成媒体并推给用户），跳过
        lock = await get_chat_lock(chat_id)
        async with lock:
            cm = get_user_model(chat_id)
            ctx = get_or_init_context(chat_id)
            username = ctx.get("username") or f"User_{chat_id}"
        model_info = SUPPORTED_MODELS.get(cm)
        if model_info is not None and (
            getattr(model_info, "native_image", False) or getattr(model_info, "native_video", False)
        ):
            logger.info(f"[proactive] chat={chat_id} 当前模型 {cm} 为原生媒体模型，跳过本次后台唤醒")
            return

        # TIMER 运行日志：只记录运行元数据，不记录模型隐藏推理或完整私密上下文。
        history_count = 0
        try:
            history_count = len(ctx.get("conversation_history", []) or [])
        except Exception:
            logger.debug("_handle_timer_wakeup 内部忽略的异常", exc_info=True)
            pass
        logger.info(
            "[TIMER] chat=%s 开始主动巡检：model=%s history_messages=%s username=%s",
            chat_id, cm, history_count, username,
        )

        # 与用户消息同款的上下文预算检查；超限则静默跳过本回合
        wakeup_msg = {"role": "user", "content": proactive.WAKEUP_PROMPT}
        is_safe = await pre_flight_context_check(chat_id, wakeup_msg)
        if not is_safe:
            logger.warning(f"[proactive] chat={chat_id} 上下文超限，跳过本次后台唤醒")
            return

        # 后台预初始化 workspace（与用户回合一致，避免首个工具调用 no-op）
        asyncio.create_task(init_workspace(chat_id))

        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=wakeup_msg,
            event_source="TIMER",
        )
        # 持久化：唤醒 user 消息不写入历史（user_message=None），
        # 仅沉淀 assistant/tool 消息；失败/空回合不落库。
        if new_msgs and not (full or "").startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, None, new_msgs, usage)

        # TIMER 可观测性：不输出隐藏推理，只记录本轮是否产生了持久化活动。
        logger.info(
            "[TIMER] chat=%s 巡检完成：assistant_tool_messages=%s final_text_chars=%s",
            chat_id, len(new_msgs or []), len((full or "").strip()),
        )
    except asyncio.CancelledError:
        # 被用户消息打断：轮次进度由打断方经 turn_recovery 保全，直接退出。
        raise
    except Exception as e:
        logger.exception(f"_handle_timer_wakeup 异常: {e}")

# ---------------------------------------------------------------------------
# 媒体组和文档组处理（保持不变）
# ---------------------------------------------------------------------------
_media_group_tasks: dict[str, asyncio.Task] = {}
_document_group_tasks: dict[str, asyncio.Task] = {}
# 视频组任务表：key 为 f"{media_group_id}:video"（与图片组 :photo 后缀分流，
# 避免混合相册里两类分片互相争抢聚合存储 / 互相取消任务）。
_video_group_tasks: dict[str, asyncio.Task] = {}

async def _process_media_group_once(chat_id: int, media_group_id: str) -> None:
    temp_msg_id = await _send_temp_message(chat_id, "📷 正在处理您发送的图片组，请稍候…")
    try:
        await send_chat_action(chat_id, "upload_photo")
        await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
        messages = await pop_media_group(media_group_id)
        _media_group_tasks.pop(media_group_id, None)
        if not messages:
            return

        first_msg = messages[0]
        username, user_id = get_user_info(first_msg)
        set_current_user_namespace(user_id or str(chat_id))
        if not is_authorized(username, user_id):
            await reply_unauthorized(chat_id, first_msg.get("message_id"))
            return

        file_ids = []
        captions = []
        for msg in messages:
            if "photo" in msg:
                file_ids.append(msg["photo"][-1]["file_id"])
            if msg.get("caption"):
                captions.append(msg["caption"].strip())
        if not file_ids:
            return

        combined_caption = " ".join(c for c in captions if c)
        context_prefix = _get_reply_context(first_msg)
        if context_prefix:
            combined_caption = context_prefix + combined_caption

        content_text = f"📎 用户上传了图片组（共 {len(file_ids)} 张）"
        if combined_caption:
            content_text += f"\n\n{combined_caption}"
        else:
            content_text += "\n\n请描述这组图片的内容"

        user_message = {
            "role": "user",
            "content": content_text,
            "file_ids": file_ids,
            "type": "photo_group",
            "attachments": [
                {
                    "kind": "photo",
                    "file_id": fid,
                }
                for fid in file_ids
            ],
        }

        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的图片组附加内容过长，已超过模型单次处理极限，请精简发送。")
            return

        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_process_media_group_once 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理图片组时出错</b>\n<code>{str(e)[:100]}</code>")
    finally:
        if temp_msg_id:
            try:
                await delete_message(chat_id, temp_msg_id)
            except Exception:
                logger.debug("_process_media_group_once 内部忽略的异常", exc_info=True)
                pass

async def _schedule_media_group(chat_id: int, media_group_id: str) -> None:
    """将图片组的等待/处理任务作为当前 chat 的可取消生成任务登记。"""
    if media_group_id in _media_group_tasks:
        return
    task = asyncio.create_task(_process_media_group_once(chat_id, media_group_id))
    _media_group_tasks[media_group_id] = task
    async with active_tasks_lock:
        active_tasks[chat_id] = task

    def _done(done_task: asyncio.Task) -> None:
        if _media_group_tasks.get(media_group_id) is done_task:
            _media_group_tasks.pop(media_group_id, None)
        asyncio.create_task(_cleanup_task(chat_id, done_task))

    task.add_done_callback(_done)

async def _process_video_group_once(chat_id: int, group_key: str) -> None:
    """聚合处理视频相册（对称 _process_media_group_once）。

    group_key 为 f"{media_group_id}:video"。等待聚合期结束后弹出全部分片，
    组装 type="video_group" 的 user_message（file_ids / file_names /
    mime_types 数组），由 _resolve_multimodal_content 按当前模型能力解析：
    支持视频的模型收到多个 video_url content part，不支持的模型收到文本
    占位——与单视频、图片组行为完全一致，切换模型不丢信息。
    """
    temp_msg_id = await _send_temp_message(chat_id, "🎬 正在处理您发送的视频组，请稍候…")
    try:
        await send_chat_action(chat_id, "upload_video")
        await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
        messages = await pop_media_group(group_key)
        _video_group_tasks.pop(group_key, None)
        if not messages:
            return

        first_msg = messages[0]
        username, user_id = get_user_info(first_msg)
        set_current_user_namespace(user_id or str(chat_id))
        if not is_authorized(username, user_id):
            await reply_unauthorized(chat_id, first_msg.get("message_id"))
            return

        video_items = []
        captions = []
        for gmsg in messages:
            media = gmsg.get("video") or gmsg.get("video_note")
            if media and media.get("file_id"):
                fid = media["file_id"]
                video_items.append({
                    "file_id": fid,
                    "file_name": media.get("file_name") or f"video_{fid[:8]}.mp4",
                    "mime_type": media.get("mime_type") or "video/mp4",
                })
            if gmsg.get("caption"):
                captions.append(gmsg["caption"].strip())
        if not video_items:
            return

        combined_caption = " ".join(c for c in captions if c)
        context_prefix = _get_reply_context(first_msg)
        if context_prefix:
            combined_caption = context_prefix + combined_caption

        content_text = f"📎 用户上传了视频组（共 {len(video_items)} 个）"
        if combined_caption:
            content_text += f"\n\n{combined_caption}"
        else:
            content_text += "\n\n请分析这组视频的内容"

        user_message = {
            "role": "user",
            "content": content_text,
            "file_ids": [v["file_id"] for v in video_items],
            "file_names": [v["file_name"] for v in video_items],
            "mime_types": [v["mime_type"] for v in video_items],
            "type": "video_group",
            "attachments": [
                {
                    "kind": "video",
                    "file_id": v["file_id"],
                    "file_name": v["file_name"],
                    "mime_type": v["mime_type"],
                }
                for v in video_items
            ],
        }

        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的视频组附加内容过长，已超过模型单次处理极限，请精简发送。")
            return

        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_process_video_group_once 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理视频组时出错</b>\n<code>{str(e)[:100]}</code>")
    finally:
        if temp_msg_id:
            try:
                await delete_message(chat_id, temp_msg_id)
            except Exception:
                logger.debug("_process_video_group_once 内部忽略的异常", exc_info=True)
                pass

async def _schedule_video_group(chat_id: int, group_key: str) -> None:
    """将视频组的等待/处理任务作为当前 chat 的可取消生成任务登记（对称 _schedule_media_group）。"""
    if group_key in _video_group_tasks:
        return
    task = asyncio.create_task(_process_video_group_once(chat_id, group_key))
    _video_group_tasks[group_key] = task
    async with active_tasks_lock:
        active_tasks[chat_id] = task

    def _done(done_task: asyncio.Task) -> None:
        if _video_group_tasks.get(group_key) is done_task:
            _video_group_tasks.pop(group_key, None)
        asyncio.create_task(_cleanup_task(chat_id, done_task))

    task.add_done_callback(_done)

async def _process_document_group_once(chat_id: int, media_group_id: str) -> None:
    # 与图片/视频组对称的异常保护：下载/建目录等任一环节抛异常时，
    # 用户能收到错误提示，而不是任务静默终止（旧实现无 try/except，
    # 异常只会变成 "Task exception was never retrieved"）。
    try:
        await _process_document_group_inner(chat_id, media_group_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"处理文档组异常 group={media_group_id}: {e}")
        try:
            await send_rich_html_message(
                chat_id, "❌ <b>处理文档组时出错</b>\n<code>" + str(e)[:100] + "</code>"
            )
        except Exception:
            logger.debug("_process_document_group_once 内部忽略的异常", exc_info=True)
            pass


async def _process_document_group_inner(chat_id: int, media_group_id: str) -> None:
    await send_chat_action(chat_id, "upload_document")
    asyncio.create_task(init_workspace(chat_id))
    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
    messages = await pop_media_group(media_group_id)
    _document_group_tasks.pop(media_group_id, None)
    if not messages:
        return

    first_msg = messages[0]
    username, user_id = get_user_info(first_msg)
    set_current_user_namespace(user_id or str(chat_id))
    if not is_authorized(username, user_id):
        await reply_unauthorized(chat_id, first_msg.get("message_id"))
        return

    file_ids = []
    file_names = []
    mime_types = []
    captions = []
    for msg in messages:
        if "document" in msg:
            doc = msg["document"]
            file_ids.append(doc["file_id"])
            fname = doc.get("file_name") or f"document_{doc['file_id'][:8]}.bin"
            file_names.append(fname)
            mime_types.append(doc.get("mime_type", mimetypes.guess_type(fname)[0] or "application/pdf"))
            if msg.get("caption"):
                captions.append(msg["caption"].strip())

    combined_caption = " ".join(c for c in captions if c) if captions else ""

    lock = await get_chat_lock(chat_id)
    async with lock:
        current_model = get_user_model(chat_id)
        model_info = SUPPORTED_MODELS.get(current_model)
        supports_native_document = bool(model_info.native_document) if model_info else False

    if supports_native_document:
        content_text = f"📎 用户上传了文档组（共 {len(file_ids)} 个文件）：{', '.join(file_names)}"
        if combined_caption:
            content_text += f"\n\n{combined_caption}"
        else:
            content_text += "\n\n请直接阅读并分析这些文档。"

        user_message = {
            "role": "user",
            "content": content_text,
            "file_ids": file_ids,
            "file_names": file_names,
            "mime_types": mime_types,
            "type": "document_group",
            "attachments": [
                {
                    "kind": "document",
                    "file_id": fid,
                    "file_name": fname,
                    "mime_type": mime,
                }
                for fid, fname, mime in zip(file_ids, file_names, mime_types)
            ],
        }
    else:
        workspace = workspace_download_root(chat_id)
        workspace.mkdir(parents=True, exist_ok=True)
        downloaded = []
        failed = []
        for fid, fname in zip(file_ids, file_names):
            safe_fname = os.path.basename(fname)
            target_path = workspace / safe_fname
            counter = 1
            while target_path.exists():
                name, ext = os.path.splitext(safe_fname)
                target_path = workspace / f"{name}_{counter}{ext}"
                counter += 1
            # download_file 内部已经把字节缓存到 R2 的 telegram/{file_id} 前缀，
            # download/ 只是本地落地缓冲，不需要再往 R2 镜像一份。
            success = await download_file(fid, str(target_path))
            if success:
                downloaded.append(target_path.name)
            else:
                failed.append(safe_fname)

        if not downloaded:
            content_text = "📎 用户上传了文档组，但所有文件下载失败，请稍后重试。"
        else:
            file_list = "、".join(downloaded)
            content_text = (
                f"📎 用户上传了文档组（共 {len(downloaded)} 个文件）：{file_list}，"
                f"已保存在工作区根目录的 download/ 子目录，可直接访问。"
            )
            if failed:
                content_text += f"\n⚠️ 以下文件下载失败：{', '.join(failed)}，请重新发送。"
            if combined_caption:
                content_text += f"\n\n用户指令：{combined_caption}"
            else:
                content_text += (
                    "\n\n请根据用户指令处理这些文档。可用 bash 执行 `ls -la download/` 查看可用文件，"
                    "再直接读取（如 `cat download/<文件名>`），或用 text_editor（path 填 download/<文件名>）查看。"
                )

        user_message = {"role": "user", "content": content_text, "file_ids": file_ids, "file_names": file_names, "mime_types": mime_types, "type": "document_group", "attachments": [{"kind": "document", "file_id": fid, "file_name": fname, "mime_type": mime} for fid, fname, mime in zip(file_ids, file_names, mime_types)]}

    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        ctx["username"] = username or f"User_{chat_id}"
    await _handle_text_message(chat_id, user_message.get("content", ""), username, user_message)

async def _schedule_document_group(chat_id: int, media_group_id: str) -> None:
    """将文档组的等待/处理任务作为当前 chat 的可取消生成任务登记。"""
    if media_group_id in _document_group_tasks:
        return
    task = asyncio.create_task(_process_document_group_once(chat_id, media_group_id))
    _document_group_tasks[media_group_id] = task
    async with active_tasks_lock:
        active_tasks[chat_id] = task

    def _done(done_task: asyncio.Task) -> None:
        if _document_group_tasks.get(media_group_id) is done_task:
            _document_group_tasks.pop(media_group_id, None)
        asyncio.create_task(_cleanup_task(chat_id, done_task))

    task.add_done_callback(_done)

# ---------------------------------------------------------------------------
# 角色与模型列表 UI
# ---------------------------------------------------------------------------
async def update_role_list(chat_id: int, message_id: int, role_list: list, current_role: str) -> bool:
    formatted = [f"{r} √" if r == current_role else r for r in role_list]
    keyboard = {"inline_keyboard": [[{"text": t, "callback_data": r}] for t, r in zip(formatted, role_list)]}
    payload = {
        "chat_id": chat_id, "message_id": message_id,
        "rich_message": {"markdown": "选择角色设定 (再次点击取消):", "skip_entity_detection": True},
        "reply_markup": json.dumps(keyboard),
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BASE_URL}/editMessageText", json=payload) as resp:
            return resp.status == 200


async def update_model_list(
    chat_id: int,
    message_id: int,
    model_list: list,
    current_model: str,
    banner_html: str = "",
) -> bool:
    """就地更新模型列表：给当前模型按钮打 √，其余保持原样。

    仿照 /role 列表的交互模式：点击模型后不再立即删除列表，而是更新按钮
    标记当前选择；列表仍由 send_model_list 发送时安排的 delete_after
    定时清理。这样在列表尚未消失的窗口内再次点击其他模型，用户同样
    能看到 "按钮 √ 变化 + ✅ 切换消息" 的双重反馈，明确知道切换是否
    生效。

    列表可能已被定时清理或并发删除，此时 editMessageText 会失败；该
    失败属于预期内的竞态，仅记录日志并返回 False，绝不能让调用方
    误以为切换失败。重复点击同一模型时 Telegram 会返回 400 "message
    is not modified"，同样按可忽略处理。
    """
    formatted = [f"{m} √" if m == current_model else m for m in model_list]
    keyboard = {"inline_keyboard": [[{"text": t, "callback_data": m}] for t, m in zip(formatted, model_list)]}
    content = ""
    if banner_html:
        content += banner_html.rstrip() + "\n\n"
    content += "🤖 请选择一个模型:"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": content,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard),
    }
    try:
        timeout = aiohttp.ClientTimeout(total=5, connect=2)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{BASE_URL}/editMessageText", json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.info(
                        f"update_model_list 未生效(列表可能已清理): chat={chat_id} "
                        f"msg={message_id} status={resp.status} body={body[:120]}"
                    )
                return resp.status == 200
    except Exception as e:
        logger.warning(f"update_model_list 异常(可忽略): chat={chat_id} msg={message_id} {e}")
        return False


async def _del_after(chat_id, msg_id, delay):
    await asyncio.sleep(delay)
    await delete_message(chat_id, msg_id)


async def _send_via_send_message(
    chat_id: int,
    html_content: str,
    reply_message_id: int | None = None,
    reply_markup: dict | str | None = None,
    delete_after: float | None = None,
) -> int:
    """
    使用 sendMessage（而非 sendRichMessage）发送指令响应。

    【修复】在 AI 生成过程中发送 /model、/role、/balance 等指令时，
    旧实现走 sendRichMessage。Telegram 客户端在收到 sendRichMessage 时
    会把这条永久消息画在当前 draft 预览的视觉位（草稿被"转正"/挤开），
    紧接着 serialize_with_active_draft 里的 _reassert_active_draft_content
    又用同一个 draft_id 推了一帧 sendRichMessageDraft——但旧草稿已被
    sendRichMessage 消费掉，于是 Telegram 把它当成一个全新的草稿，
    画在永久消息下方。AI 的 flush 循环随后继续刷新这个新草稿。

    用户看到的错乱就是：
      1) 列表占了草稿位（列表出现在草稿原来的位置，而不是指令下方）
      2) 草稿在指令下方重新刷新（reassert + flush 循环创建的新草稿）

    修复思路：Telegram Bot API 文档明确指出"once the output is finalized,
    you must call sendRichMessage to persist it"——也就是说，只有
    sendRichMessage 会触发"草稿转正"行为。改用 sendMessage（普通文本
    消息）发送指令响应，不会消费/挤开活跃草稿，列表自然出现在指令
    下方，草稿继续在原位生成。

    sendMessage 的 parse_mode=HTML 不支持 <br/>，需要转换为 \n。
    <b>、<code>、<i>、<u>、<s>、<a>、<blockquote> 等标签均被支持。
    """
    if not html_content or not html_content.strip():
        return 0
    text = (
        html_content
        .replace("<br/>", "\n")
        .replace("<br>", "\n")
        .replace("</br>", "\n")
        .replace("<br />", "\n")
    )
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    reply_parameters = _reply_params(reply_message_id)
    if reply_parameters:
        payload["reply_parameters"] = reply_parameters
    if reply_markup is not None:
        payload["reply_markup"] = (
            json.dumps(reply_markup) if isinstance(reply_markup, dict) else reply_markup
        )
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{BASE_URL}/sendMessage", json=payload) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    mid = res.get("result", {}).get("message_id")
                    if isinstance(mid, int) and mid > 0:
                        if delete_after is not None:
                            asyncio.create_task(_del_after(chat_id, mid, delete_after))
                        return mid
                else:
                    body = await resp.text()
                    logger.error(
                        f"_send_via_send_message failed: {resp.status} {body[:200]}"
                    )
    except Exception as e:
        logger.exception(f"_send_via_send_message exception: {e}")
    return 0


async def send_role_list(chat_id: int, role_list: list, current_role: str, reply_message_id: int | None = None) -> int:
    formatted = [f"{r} √" if r == current_role else r for r in role_list]
    keyboard = {"inline_keyboard": [[{"text": t, "callback_data": r}] for t, r in zip(formatted, role_list)]}
    content = "选择角色设定 (再次点击取消):"
    # 使用 sendMessage 避免在 AI 生成中挤占活跃草稿的位置
    return await _send_via_send_message(
        chat_id,
        content,
        reply_message_id=reply_message_id,
        reply_markup=keyboard,
        delete_after=6,
    )


async def send_model_list(
    chat_id: int,
    model_list: list,
    reply_message_id: int | None = None,
    banner_html: str = "",
) -> int:
    # 与 /role 列表一致：当前模型打 √，让用户在切换前就能看到当前选择；
    # 点击后由 update_model_list 就地移动 √（不删除列表），列表在
    # delete_after 到期前持续可点，期间每次点击均有反馈。
    current_model = get_user_model(chat_id)
    formatted = [f"{m} √" if m == current_model else m for m in model_list]
    keyboard = {"inline_keyboard": [[{"text": t, "callback_data": m}] for t, m in zip(formatted, model_list)]}
    content = ""
    if banner_html:
        content += banner_html.rstrip() + "\n\n"
    content += "🤖 请选择一个模型:"
    # 使用 sendMessage 避免在 AI 生成中挤占活跃草稿的位置
    return await _send_via_send_message(
        chat_id,
        content,
        reply_message_id=reply_message_id,
        reply_markup=keyboard,
        delete_after=10,
    )

# ---------------------------------------------------------------------------
# Webhook 路由
# ---------------------------------------------------------------------------
@app.route('/webhook', methods=['GET', 'POST', 'HEAD'])
async def webhook() -> tuple:
    _t0 = time.monotonic()
    try:
        request_id = str(uuid.uuid4())[:8]
        set_request_id(request_id)
        logger.info(f"Received webhook request {request_id}")

        token = request.args.get("token")
        # 使用 hmac.compare_digest 进行恒定时间比较，防止时序攻击
        if not token or not WEBHOOK_TOKEN or not hmac.compare_digest(str(token), str(WEBHOOK_TOKEN)):
            return "Forbidden", 403
        if request.method in ('GET', 'HEAD'):
            return "OK - Webhook is alive", 200

        data = await request.json
        uid = data.get('update_id')
        # 缺少 update_id 的非法 payload 直接拒绝，避免污染去重集合
        if uid is None:
            return "Bad Request", 400

        if LOG_LEVEL == "DEBUG" or LOG_TRUNCATE_LIMIT == 0:
            msg_debug = json.dumps(data, ensure_ascii=False, default=str)
            logger.debug(f"完整 Webhook 数据: {msg_debug}")
        else:
            _msg_obj = data.get("message") or {}
            chat_id = _msg_obj.get("chat", {}).get("id")
            update_id = data.get("update_id")
            logger.info(f"Webhook: update_id={update_id}, chat_id={chat_id}")
            if logger.isEnabledFor(logging.DEBUG):
                msg_debug = json.dumps(data, ensure_ascii=False, default=str)
                if len(msg_debug) > LOG_TRUNCATE_LIMIT:
                    msg_debug = msg_debug[:LOG_TRUNCATE_LIMIT] + "... (truncated)"
                logger.debug(f"截断的 Webhook 数据: {msg_debug}")

        # 使用 OrderedDict 保留插入顺序，超过 10000 条时按插入顺序淘汰最早的 5000 条。
        # 之前用 set + list 刉片是随机淘汰，可能把刚加入的 uid 误伤。
        # 原子地"检查并标记"：旧实现先查后标两段加锁，Telegram 超时重投 +
        # webhook 并发时同一 update 的两个副本可能同时通过检查被双重处理。
        if not await mark_update_processed_if_new(uid):
            return "OK", 200

        # ── 消息处理 ──────────────────────────────────────────────────────
        if "message" in data and isinstance(data["message"], dict):
            msg = data["message"]
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id is None:
                logger.warning(f"Webhook: missing chat_id in message, update_id={uid}")
                return "OK", 200
            from_user = msg.get("from", {})
            username, user_id = get_user_info(msg)
            set_current_user_namespace(user_id or str(chat_id))

            text = msg.get("text", "") or ""
            is_admin_cmd = (_cmd_match(text, "/adduser") or _cmd_match(text, "/deluser")
                            or _cmd_match(text, "/listusers"))
            if is_admin_cmd:
                if not is_admin(username, user_id):
                    await send_rich_html_message(chat_id, "❌ <b>权限不足</b>\n只有管理员可以执行此操作。", reply_parameters=_reply_params(msg["message_id"]))
                    return "OK", 200
                async with global_lock:
                    if _cmd_match(text, "/adduser"):
                        parts = text.split()
                        if len(parts) != 2:
                            await send_rich_html_message(chat_id, "❌ <b>用法错误</b>\n用法：<code>/adduser @username</code> 或 <code>/adduser 123456789</code>", reply_parameters=_reply_params(msg["message_id"]))
                            return "OK", 200
                        target = parts[1].strip().lstrip("@")
                        if not target:
                            await send_rich_html_message(chat_id, "❌ <b>输入无效</b>\n请输入有效的用户名或ID。", reply_parameters=_reply_params(msg["message_id"]))
                            return "OK", 200
                        WHITELIST_USERS.add(target)
                        await save_whitelist()
                        await send_rich_html_message(chat_id, f"✅ <b>添加成功</b>\n已添加 <code>{target}</code> 到白名单。", reply_parameters=_reply_params(msg["message_id"]))
                        return "OK", 200
                    elif _cmd_match(text, "/deluser"):
                        parts = text.split()
                        if len(parts) != 2:
                            await send_rich_html_message(chat_id, "❌ <b>用法错误</b>\n用法：<code>/deluser @username</code> 或 <code>/deluser 123456789</code>", reply_parameters=_reply_params(msg["message_id"]))
                            return "OK", 200
                        target = parts[1].strip().lstrip("@")
                        if not target:
                            await send_rich_html_message(chat_id, "❌ <b>输入无效</b>\n请输入有效的用户名或ID。", reply_parameters=_reply_params(msg["message_id"]))
                            return "OK", 200
                        if target not in WHITELIST_USERS:
                            await send_rich_html_message(chat_id, f"❌ <b>用户不存在</b>\n<code>{target}</code> 不在白名单中。", reply_parameters=_reply_params(msg["message_id"]))
                            return "OK", 200
                        WHITELIST_USERS.remove(target)
                        await save_whitelist()
                        await send_rich_html_message(chat_id, f"✅ <b>移除成功</b>\n已移除 <code>{target}</code>。", reply_parameters=_reply_params(msg["message_id"]))
                        return "OK", 200
                    elif _cmd_match(text, "/listusers"):
                        if not WHITELIST_USERS:
                            await send_rich_html_message(chat_id, "📋 <b>白名单为空</b>", reply_parameters=_reply_params(msg["message_id"]))
                        else:
                            users_list = "".join(f"<li><code>{str(u)}</code></li>" for u in sorted(WHITELIST_USERS))
                            await send_rich_html_message(chat_id, f"📋 <b>当前白名单用户：</b>\n<ul>{users_list}</ul>", reply_parameters=_reply_params(msg["message_id"]))
                        return "OK", 200

            if _cmd_match(text, "/start"):
                authorized = is_authorized(username, user_id)
                if authorized:
                    welcome_msg = """
<b>🤖 欢迎使用 AI 助手！</b></br>
✅ 您已获得授权，可以直接发送消息与我对话。</br>
<u>支持的功能：</u></br>
<blockquote>
<ul>
    <li><b>📝 文本对话</b> - 自然语言交流</li>
    <li><b>🖼️ 图片分析</b> - 图像识别与描述</li>
    <li><b>📄 文档解析</b> - PDF、Word等文件处理</li>
    <li><b>🔗 联网搜索</b> - 实时信息查询</li>
</ul>
</blockquote>
</br>💡 <i>提示：如需帮助，请联系管理员 @dearella</i>
"""
                else:
                    welcome_msg = """
<b>🤖 欢迎使用 AI 助手！</b></br>
⚠️ <b>注意</b>：此机器人启用了白名单机制。</br>
请联系管理员 <b>@dearella</b></br> 申请白名单后，才能使用全部功能。
"""
                await send_rich_html_message(chat_id, welcome_msg, reply_parameters=_reply_params(msg["message_id"]))
                return "OK", 200

            if not is_authorized(username, user_id):
                await reply_unauthorized(chat_id, msg.get("message_id"))
                return "OK", 200

            lock = await get_chat_lock(chat_id)
            async with lock:
                ctx = get_or_init_context(chat_id)
                ctx["username"] = (from_user.get("username") or from_user.get("first_name") or str(from_user.get("id", chat_id)))
                user_models.setdefault(chat_id, DEFAULT_MODEL)
            username = ctx["username"]

            # ── 统一事件源入口（USER）────────────────────────────────────
            # 1) 记录用户活动：重置该 chat 的空闲计时（主动唤醒调度器据此
            #    决定何时进入 agent 的"活动时间"）。仅私聊参与主动唤醒。
            # 2) 打断进行中的 TIMER 主动唤醒回合：取消后台 agent 任务，并
            #    通过 turn_recovery 保全该回合已完成的进度（补占位
            #    tool_result 后沉淀进历史，全程静默，不显示"已停止"提示）。
            # 该入口位于所有消息类型/命令分发之前，覆盖全部授权用户输入。
            try:
                await proactive.note_user_activity(
                    chat_id,
                    private=(msg.get("chat") or {}).get("type") == "private",
                )
            except Exception as e:
                logger.warning(f"note_user_activity 异常: {e}")
            try:
                await proactive.interrupt_proactive_flow(chat_id)
            except Exception as e:
                logger.warning(f"interrupt_proactive_flow 异常: {e}")

            # ── Telegram 原生 location（用户分享位置 / 实时位置） ───────
            # 直接把坐标交给 LLM；如需反查中文地址，LLM 可在后续轮次调用
            # amap-maps MCP 的 maps_regeocode 工具。
            if "location" in msg and "text" not in msg:
                loc = msg["location"]
                lat = loc.get("latitude")
                lon = loc.get("longitude")
                if lat is None or lon is None:
                    return "OK", 200

                content_text = (
                    f"📎 用户分享了当前位置\n"
                    f"坐标：{lat:.6f}, {lon:.6f} (WGS-84)\n\n"
                    f"如果用户问起『附近』『周边』等，请直接以此坐标作为中心点，"
                    f"调用 search_poi / route / distance 等工具，无需再调用 geocode。"
                    f"如需反查中文地址，请调用 amap-maps MCP 的 maps_regeocode 工具。"
                )
                user_message = {"role": "user", "content": content_text}
                await _interrupt_active_generation(chat_id)
                task = asyncio.create_task(
                    _handle_text_message(chat_id, content_text, username, user_message)
                )
                async with active_tasks_lock:
                    active_tasks[chat_id] = task
                task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))
                return "OK", 200

            # ── 媒体组（图片） ─────────────────────────────────
            # 存储与任务 key 加 ":photo" 后缀：混合相册（photo+video 同
            # media_group_id）里两类分片各占一个子组，互不争抢聚合存储。
            if "media_group_id" in msg and "photo" in msg:
                mg = msg["media_group_id"]
                group_key = f"{mg}:photo"
                await add_media_group_message(group_key, msg)
                # 图片组存在聚合等待期；只在首个分片到达时中断旧草稿并登记任务。
                # 同一组的后续分片不能取消正在等待自身完整内容的聚合任务。
                # 混合相册：同组视频任务已在等待时不中断它，让两组各自完成。
                if group_key not in _media_group_tasks:
                    if f"{mg}:video" not in _video_group_tasks:
                        await _interrupt_active_generation(chat_id)
                    await _schedule_media_group(chat_id, group_key)
                return "OK", 200

            # ── 视频相册（聚合，对称图片组） ─────────────────────
            # Telegram 相册可含多个视频分片（或 photo+video 混合）。聚合
            # 等待期后合并为一条 type="video_group" 消息触发一轮 AI。
            if "media_group_id" in msg and ("video" in msg or "video_note" in msg):
                mg = msg["media_group_id"]
                group_key = f"{mg}:video"
                await add_media_group_message(group_key, msg)
                if group_key not in _video_group_tasks:
                    # 同一相册的图片组任务已在等待时不中断它（混合相册）
                    if f"{mg}:photo" not in _media_group_tasks:
                        await _interrupt_active_generation(chat_id)
                    await _schedule_video_group(chat_id, group_key)
                return "OK", 200

            # ── 文档组 ─────────────────────────────────────────────────────
            if "media_group_id" in msg and "document" in msg:
                mg = msg["media_group_id"]
                await add_media_group_message(mg, msg)
                # 文档组同样只在首个分片时中断旧草稿；同组后续文件继续聚合。
                if mg not in _document_group_tasks:
                    await _interrupt_active_generation(chat_id)
                    await _schedule_document_group(chat_id, mg)
                return "OK", 200

            # ── 单张图片 ──────────────────────────────────────────────────
            if "photo" in msg:
                fid = msg["photo"][-1]["file_id"]
                cap = msg.get("caption", "").strip()
                context_prefix = _get_reply_context(msg)
                if context_prefix:
                    cap = context_prefix + cap

                file_name = f"photo_{fid[:8]}.jpg"
                content_text = f"📎 用户上传了图片「{file_name}」"
                if cap:
                    content_text += f"\n\n{cap}"
                else:
                    content_text += "\n\n请描述这张图片的内容"

                user_message = {
                    "role": "user",
                    "content": content_text,
                    "file_ids": [fid],
                    "file_names": [file_name],
                    "type": "photo_group",
                    "attachments": [
                        {
                            "kind": "photo",
                            "file_id": fid,
                            "file_name": file_name,
                        }
                    ],
                }

                await _interrupt_active_generation(chat_id)
                task = asyncio.create_task(_handle_photo_message(chat_id, user_message, username))
                async with active_tasks_lock:
                    active_tasks[chat_id] = task
                task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))
                return "OK", 200

            # ── 单个文档 ──────────────────────────────────────────────────
            if "document" in msg and "media_group_id" not in msg:
                doc = msg["document"]
                fid = doc["file_id"]
                fname = doc.get("file_name") or f"document_{fid[:8]}.bin"
                mime_type = doc.get("mime_type") or mimetypes.guess_type(fname)[0] or "application/pdf"
                cap = msg.get("caption", "").strip()
                context_prefix = _get_reply_context(msg)
                if context_prefix:
                    cap = context_prefix + cap

                async with lock:
                    cm = get_user_model(chat_id)
                    model_info = SUPPORTED_MODELS.get(cm)
                    supports_native_document = bool(model_info.native_document) if model_info else False

                if supports_native_document:
                    content_text = f"📎 用户上传了文档「{fname}」"
                    if cap:
                        content_text += f"\n\n{cap}"
                    else:
                        content_text += "\n\n请直接阅读并分析这个文档。"
                    user_message = {
                        "role": "user",
                        "content": content_text,
                        "file_id": fid,
                        "file_name": fname,
                        "mime_type": mime_type,
                        "type": "document",
                        "attachments": [
                            {
                                "kind": "document",
                                "file_id": fid,
                                "file_name": fname,
                                "mime_type": mime_type,
                            }
                        ],
                    }
                else:
                    workspace = workspace_download_root(chat_id)
                    workspace.mkdir(parents=True, exist_ok=True)
                    safe_fname = os.path.basename(fname)
                    target_path = workspace / safe_fname

                    workspace_lock = await _get_workspace_lock(chat_id)
                    async with workspace_lock:
                        # download_file 内部已经把字节缓存到 R2 的 telegram/{file_id} 前缀，
                        # download/ 只是本地落地缓冲，不需要再往 R2 镜像一份。
                        success = await download_file(fid, str(target_path))
                        if success:
                            content_text = (
                                f"📎 用户上传了文档「{safe_fname}」，已保存在工作区根目录的 "
                                f"download/ 子目录，可直接访问（如 `cat download/{safe_fname}`，"
                                f"或用 text_editor 并把 path 填为 download/{safe_fname}）。"
                            )
                            if cap:
                                content_text += f"\n\n用户指令：{cap}"
                            else:
                                content_text += "\n\n请根据用户指令处理该文档，可用 text_editor 或 bash 直接查看。"
                        else:
                            content_text = f"📎 用户上传了文档「{safe_fname}」，但下载失败，请稍后重试。"

                    user_message = {"role": "user", "content": content_text, "file_id": fid, "file_name": safe_fname, "mime_type": mime_type, "type": "document", "attachments": [{"kind": "document", "file_id": fid, "file_name": safe_fname, "mime_type": mime_type}]}

                await _interrupt_active_generation(chat_id)
                task = asyncio.create_task(_handle_document_message(chat_id, user_message, username))
                async with active_tasks_lock:
                    active_tasks[chat_id] = task
                task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))
                return "OK", 200

            # ── 语音 / 音频 ───────────────────────────────────────────────
            if "voice" in msg or "audio" in msg:
                media_key = "voice" if "voice" in msg else "audio"
                media = msg[media_key]
                fid = media["file_id"]
                fname = media.get("file_name", f"{media_key}_{fid[:8]}.ogg")
                cap = msg.get("caption", "").strip()
                context_prefix = _get_reply_context(msg)
                if context_prefix:
                    cap = context_prefix + cap

                async with lock:
                    current_model = get_user_model(chat_id)
                    model_info = SUPPORTED_MODELS.get(current_model)
                    supports_audio = model_info.audio if model_info else False

                if supports_audio:
                    content_text = f"📎 用户上传了音频「{fname}」"
                    if cap:
                        content_text += f"\n\n{cap}"
                    else:
                        content_text += "\n\n请分析这段音频"
                    user_message = {
                        "role": "user",
                        "content": content_text,
                        "file_id": fid,
                        "type": media_key,
                        "attachments": [
                            {
                                "kind": media_key,
                                "file_id": fid,
                                "file_name": fname,
                            }
                        ],
                    }
                    await _interrupt_active_generation(chat_id)
                    task = asyncio.create_task(_handle_audio_message(chat_id, user_message, username))
                    async with active_tasks_lock:
                        active_tasks[chat_id] = task
                    task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))
                    return "OK", 200
                else:
                    content_text_parts = []
                    if cap:
                        content_text_parts.append(cap)
                    if GROQ_API_KEY:
                        audio_bytes = await _get_cached_audio_data(chat_id, fid)
                        if audio_bytes:
                            ext = os.path.splitext(fname)[1] or ".ogg"
                            try:
                                transcribed_text = await transcribe_audio_with_groq(audio_bytes, ext)
                                if transcribed_text:
                                    content_text_parts.append(transcribed_text)
                            except Exception as e:
                                logger.error(f"Groq 转录失败: {e}")
                    if not content_text_parts:
                        content_text_parts.append("请分析这段音频")
                    content_text = "\n\n".join(content_text_parts)
                    user_message = {
                        "role": "user",
                        "content": content_text,
                        "file_id": fid,
                        "file_name": fname,
                        "type": media_key,
                        "attachments": [
                            {
                                "kind": media_key,
                                "file_id": fid,
                                "file_name": fname,
                            }
                        ],
                    }
                    await _interrupt_active_generation(chat_id)
                    task = asyncio.create_task(_handle_audio_message(chat_id, user_message, username))
                    async with active_tasks_lock:
                        active_tasks[chat_id] = task
                    task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))
                    return "OK", 200
            # ── 视频 / 圆形视频（单发，无 media_group_id） ─────
            # 相册里的视频分片已在上方"视频相册"分支聚合处理；到达这里的
            # 一定是单独发送的视频。video_note（圆形视频）无 file_name /
            # mime_type，给默认值。
            if "video" in msg or "video_note" in msg:
                is_video_note = "video_note" in msg and "video" not in msg
                media = msg.get("video") or msg.get("video_note") or {}
                fid = media.get("file_id", "")
                if fid:
                    mime_type = media.get("mime_type") or "video/mp4"
                    if is_video_note:
                        fname = f"video_note_{fid[:8]}.mp4"
                    else:
                        fname = media.get("file_name") or f"video_{fid[:8]}.mp4"
                    cap = msg.get("caption", "").strip()
                    context_prefix = _get_reply_context(msg)
                    if context_prefix:
                        cap = context_prefix + cap

                    content_text = f"📎 用户上传了视频「{fname}」"
                    if cap:
                        content_text += f"\n\n{cap}"
                    else:
                        content_text += "\n\n请分析这段视频。"

                    user_message = {
                        "role": "user",
                        "content": content_text,
                        "file_id": fid,
                        "file_name": fname,
                        "mime_type": mime_type,
                        "type": "video",
                        "attachments": [
                            {
                                "kind": "video",
                                "file_id": fid,
                                "file_name": fname,
                                "mime_type": mime_type,
                            }
                        ],
                    }

                    await _interrupt_active_generation(chat_id)
                    task = asyncio.create_task(_handle_video_message(chat_id, user_message, username))
                    async with active_tasks_lock:
                        active_tasks[chat_id] = task
                    task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))
                    return "OK", 200

            # ── 贴纸（sticker）─────────────────────────────────────────────
            # Sticker 本体（TGS / WebP / WebM）当前主流 LLM 不可直接识别，
            # 但 Sticker 对象携带 emoji / emoji_list / type / set_name 等
            # 可语义化字段，把它们的元数据拼成文本作为 user 消息发给模型；
            # 不把 file_id 推为附件，避免 _resolve_multimodal_content 把
            # 不可识别媒体推到模型 API 触发 400。
            if "sticker" in msg:
                sticker_obj = msg["sticker"] or {}
                sticker_meta = extract_sticker_metadata(sticker_obj)
                sticker_text = sticker_metadata_to_text(sticker_obj)

                context_prefix = _get_reply_context(msg)

                content_lines = [f"📎 用户发送了贴纸"]
                if sticker_text and sticker_text != "[贴纸]":
                    content_lines.append(sticker_text)
                else:
                    # 退化场景：Sticker 没有任何可读元数据时也至少给个
                    # 占位，避免把空内容塞给模型。
                    content_lines.append("[贴纸]（无可读元数据）")
                if context_prefix:
                    content_lines.append("")
                    content_lines.append(context_prefix.rstrip())
                content_text = "\n".join(l for l in content_lines if l is not None).strip()

                user_message = {
                    "role": "user",
                    "content": content_text,
                    "type": "sticker",
                    # 保留 sticker 元数据快照，方便后续历史回看 / 工具调用
                    # 引用，但不会出现在出站 LLM 消息里（_append_history_async
                    # 已经过滤掉所有非 OpenAI 协议字段）。
                    "sticker_meta": sticker_meta,
                }

                await _interrupt_active_generation(chat_id)
                task = asyncio.create_task(_handle_sticker_message(chat_id, user_message, username))
                async with active_tasks_lock:
                    active_tasks[chat_id] = task
                task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))
                return "OK", 200

            # ── 文本消息 ──────────────────────────────────────────────────
            if "text" in msg:
                user_input = msg["text"]

                # 若当前 message_user 正在等待回复，优先把这条消息交给原 agent turn，
                # 不要启动新的 AI turn。命令仍保留为真正的 bot 指令入口。
                # 提问卡与通知卡均适用：用户直接打字即视为回复
                # （"用户回复了就是正常"），无需先点"自定义回答"按钮。
                pending_ask = await get_pending_for_chat(chat_id)
                if (
                    pending_ask
                    and not user_input.startswith("/")
                    and await resolve_message_user_text(chat_id, user_input)
                ):
                    return "OK", 200

                if _cmd_match(user_input, "/role"):
                    cr = await get_user_role(chat_id)
                    prev_mid = role_message_ids.get(chat_id)
                    if not prev_mid or not await update_role_list(chat_id, prev_mid, SUPPORTED_ROLES, cr):
                        mid = await send_role_list(chat_id, SUPPORTED_ROLES, cr, msg.get("message_id"))
                        if mid:
                            role_message_ids[chat_id] = mid
                    return "OK", 200

                if _cmd_match(user_input, "/balance"):
                    parts = user_input.split(maxsplit=1)
                    svc = parts[1].lower() if len(parts) > 1 else None
                    msgs = []
                    if not svc or svc == "all":
                        b, u = await check_deepseek_balance()
                        if b is not None:
                            msgs.append(f"💰 <b>DeepSeek</b>: <code>{b} {u}</code>")
                        else:
                            msgs.append(f"⚠️ <b>DeepSeek</b>: 查询失败 <i>({u if u else '未知错误'})</i>")
                        orb = await check_openrouter_balance()
                        if orb is not None and orb >= 0:
                            msgs.append(f"💰 <b>OpenRouter</b>: <code>${orb:.3f} USD</code>")
                        else:
                            msgs.append("⚠️ <b>OpenRouter</b>: 查询失败")
                    elif svc in ("deepseek", "ds"):
                        b, u = await check_deepseek_balance()
                        if b is not None:
                            msgs.append(f"💰 <b>DeepSeek</b>: <code>{b} {u}</code>")
                        else:
                            msgs.append(f"⚠️ <b>DeepSeek</b>: 查询失败 <i>({u if u else '未知错误'})</i>")
                    elif svc in ("openrouter", "or"):
                        orb = await check_openrouter_balance()
                        if orb is not None and orb >= 0:
                            msgs.append(f"💰 <b>OpenRouter</b>: <code>${orb:.3f} USD</code>")
                        else:
                            msgs.append("⚠️ <b>OpenRouter</b>: 查询失败")
                    else:
                        msgs.append("❌ 无效服务名，可用: <code>deepseek</code>, <code>openrouter</code>, <code>all</code>")
                    # 使用 sendMessage 避免在 AI 生成中挤占活跃草稿的位置
                    await _send_via_send_message(
                        chat_id,
                        "\n".join(msgs),
                        reply_message_id=msg["message_id"],
                    )
                    return "OK", 200

                if _cmd_match(user_input, "/model"):
                    if (msg.get("chat") or {}).get("type") != "private":
                        await _send_via_send_message(
                            chat_id,
                            "❌ <b>操作受限</b>\n模型切换仅限私聊使用。",
                            reply_message_id=msg["message_id"],
                        )
                        return "OK", 200
                    async with global_lock:
                        current_role = await get_user_role(chat_id)
                    banner_html = ""
                    if current_role:
                        banner_html = f"⚠️ <b>兼容性提示</b>\n当前角色「<b>{current_role}</b>」可能与新模型不兼容，请留意。"
                    await send_model_list(
                        chat_id,
                        list(SUPPORTED_MODELS.keys()),
                        msg.get("message_id"),
                        banner_html=banner_html,
                    )
                    return "OK", 200

                if _cmd_match(user_input, "/clear"):
                    await _interrupt_active_generation(chat_id)
                    await safe_clear_history(chat_id)
                    await safe_clear_active_skill(chat_id)
                    lock = await get_chat_lock(chat_id)
                    async with lock:
                        ctx = get_or_init_context(chat_id)
                        ctx["last_prompt_tokens"] = 0
                        ctx["last_completion_tokens"] = 0
                        ctx["token_ledger"] = []
                    # /clear 是语义边界：历史清空意味着重新开始，主动唤醒
                    # timer 也重置为随机 5~20min 下一次。
                    try:
                        await proactive.reset_proactive_timer(chat_id)
                    except Exception as e:
                        logger.warning(f"reset_proactive_timer 异常: {e}")
                    await send_rich_html_message(chat_id, "✅ <b>操作成功</b>\n对话历史已清空", reply_parameters=_reply_params(msg["message_id"]))
                    return "OK", 200

                # ── /show on|off：草稿预览开关（USER 与 TIMER 回合统一生效）──
                if _cmd_match(user_input, "/show"):
                    parts = user_input.split()
                    arg = parts[1].strip().lower() if len(parts) > 1 else ""
                    if arg in ("on", "开", "1", "true"):
                        await set_show_drafts(chat_id, True)
                        await _send_via_send_message(
                            chat_id,
                            "✅ <b>草稿预览已开启</b>\n"
                            "你发送消息和后台主动巡检（TIMER）时，都会实时展示"
                            "富文本草稿，最终回复自动送达。",
                            reply_message_id=msg["message_id"],
                        )
                    elif arg in ("off", "关", "0", "false"):
                        await set_show_drafts(chat_id, False)
                        await _send_via_send_message(
                            chat_id,
                            "✅ <b>草稿预览已关闭（静默模式）</b>\n"
                            "过程与最终回复不再自动展示，也不会自动发送——AI 认为"
                            "需要你看到结论时，会主动把最终回复直接发送给你"
                            "（deliver_reply，不经草稿）；不发送则本轮完全静默。"
                            "提问/留言走 message_user。",
                            reply_message_id=msg["message_id"],
                        )
                    else:
                        current_show = await get_show_drafts(chat_id)
                        state_line = "开启（/show on）" if current_show else "关闭（/show off）"
                        await _send_via_send_message(
                            chat_id,
                            f"ℹ️ 当前草稿预览：<b>{state_line}</b>\n"
                            "用法：<code>/show on</code> 开启 · <code>/show off</code> 关闭",
                            reply_message_id=msg["message_id"],
                        )
                    return "OK", 200

                # 普通文本对话
                context_prefix = _get_reply_context(msg)
                # 先记录原始输入是否为空，再拼接引用上下文（避免上下文使空输入检查失效）
                if context_prefix:
                    user_input = context_prefix + user_input

                reply_media = _get_reply_media(msg)

                async with lock:
                    cm = get_user_model(chat_id)
                    model_info = SUPPORTED_MODELS.get(cm)
                    supports_audio = model_info.audio if model_info else False
                    supports_native_document = bool(model_info.native_document) if model_info else False

                if reply_media:
                    media_type = reply_media.get("type")
                    file_name = reply_media.get("file_name", f"{media_type}_{reply_media.get('file_id', '')[:8]}")

                    if media_type == "photo":
                        file_ids = reply_media.get("file_ids", [])
                        content_text = f"📎 用户引用了图片「{file_name}」"
                        if user_input:
                            content_text += f"\n\n{user_input}"
                        else:
                            content_text += "\n\n请分析这张图片"
                        user_message = {
                            "role": "user",
                            "content": content_text,
                            "file_ids": file_ids,
                            "type": "photo_group",
                            "attachments": [
                                {
                                    "kind": "photo",
                                    "file_id": fid,
                                    "file_name": file_name,
                                }
                                for fid in file_ids
                            ],
                        }

                    elif media_type == "document":
                        safe_fname = os.path.basename(file_name)
                        mime_type = reply_media.get("mime_type") or mimetypes.guess_type(safe_fname)[0] or "application/pdf"

                        if supports_native_document:
                            content_text = f"📎 用户引用了文档「{safe_fname}」"
                            if user_input:
                                content_text += f"\n\n{user_input}"
                            else:
                                content_text += "\n\n请直接阅读并分析这个文档。"
                            user_message = {
                                "role": "user",
                                "content": content_text,
                                "file_id": reply_media["file_id"],
                                "file_name": safe_fname,
                                "mime_type": mime_type,
                                "type": "document",
                            }
                        else:
                            workspace = workspace_download_root(chat_id)
                            workspace.mkdir(parents=True, exist_ok=True)
                            target_path = workspace / safe_fname
                            workspace_lock = await _get_workspace_lock(chat_id)
                            async with workspace_lock:
                                # download_file 内部已经把字节缓存到 R2 的 telegram/{file_id} 前缀，
                                # download/ 只是本地落地缓冲，不需要再往 R2 镜像一份。
                                success = await download_file(reply_media["file_id"], str(target_path))
                                if success:
                                    content_text = (
                                        f"📎 用户引用了文档「{safe_fname}」，已保存在工作区根目录的 "
                                        f"download/ 子目录，可直接访问（如 `cat download/{safe_fname}`）。"
                                    )
                                else:
                                    content_text = f"📎 用户引用了文档「{safe_fname}」，但下载失败。"
                                if user_input:
                                    content_text += f"\n\n用户指令：{user_input}"
                                else:
                                    content_text += "\n\n请根据用户指令处理该文档。"
                            user_message = {"role": "user", "content": content_text}

                    elif media_type in ("audio", "voice"):
                        if supports_audio:
                            content_text = f"📎 用户引用了音频「{file_name}」"
                            if user_input:
                                content_text += f"\n\n{user_input}"
                            else:
                                content_text += "\n\n请分析这段音频"
                            user_message = {
                                "role": "user",
                                "content": content_text,
                                "file_id": reply_media["file_id"],
                                "file_name": file_name,
                                "type": media_type,
                                "attachments": [
                                    {
                                        "kind": media_type,
                                        "file_id": reply_media["file_id"],
                                        "file_name": file_name,
                                    }
                                ],
                            }
                        else:
                            content_text_parts = []
                            if user_input:
                                content_text_parts.append(user_input)
                            if GROQ_API_KEY:
                                audio_bytes = await _get_cached_audio_data(chat_id, reply_media["file_id"])
                                if audio_bytes:
                                    ext = os.path.splitext(file_name)[1] or ".ogg"
                                    try:
                                        transcribed_text = await transcribe_audio_with_groq(audio_bytes, ext)
                                        if transcribed_text:
                                            content_text_parts.append(transcribed_text)
                                    except Exception as e:
                                        logger.error(f"Groq 转录失败: {e}")
                            if not content_text_parts:
                                content_text_parts.append("请分析这段音频")
                            content_text = "\n\n".join(content_text_parts)
                            user_message = {
                                "role": "user",
                                "content": content_text,
                                "file_id": reply_media["file_id"],
                                "file_name": file_name,
                                "type": media_type,
                                "attachments": [
                                    {
                                        "kind": media_type,
                                        "file_id": reply_media["file_id"],
                                        "file_name": file_name,
                                    }
                                ],
                            }
                    elif media_type == "video":
                        content_text = f"📎 用户引用了视频「{file_name}」"
                        if user_input:
                            content_text += f"\n\n{user_input}"
                        else:
                            content_text += "\n\n请分析这个视频"
                        user_message = {
                            "role": "user",
                            "content": content_text,
                            "file_id": reply_media["file_id"],
                            "file_name": file_name,
                            "mime_type": reply_media.get("mime_type", "video/mp4"),
                            "type": "video",
                            "attachments": [
                                {
                                    "kind": "video",
                                    "file_id": reply_media["file_id"],
                                    "file_name": file_name,
                                    "mime_type": reply_media.get("mime_type", "video/mp4"),
                                }
                            ],
                        }
                    else:
                        content_text = f"📎 用户引用了媒体「{file_name}」\n\n{user_input}" if user_input else f"📎 用户引用了媒体「{file_name}」"
                        user_message = {"role": "user", "content": content_text}
                else:
                    user_message = {"role": "user", "content": user_input}

                logger.debug(f"最终 user_message 内容: {user_message.get('content', '')[:500]}")

                await _interrupt_active_generation(chat_id)
                task = asyncio.create_task(_handle_text_message(chat_id, user_input, username, user_message))
                async with active_tasks_lock:
                    active_tasks[chat_id] = task
                task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))
                return "OK", 200

        # ── 回调查询 ───────────────────────────────────────────────────────
        if "callback_query" in data:
            cb = data["callback_query"]
            chat_id = cb["message"]["chat"]["id"]
            uid = cb["from"]["id"]
            mid = cb["message"]["message_id"]
            sel = cb["data"]

            if str(uid) != str(chat_id):
                await _send_via_send_message(chat_id, "❌ <b>无权限</b>", reply_message_id=mid)
                async with aiohttp.ClientSession() as s:
                    await s.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": "无权限"})
                return "OK", 200

            # 按钮点击也是用户活动：重置主动唤醒的空闲计时
            # （不打断 TIMER 回合——按钮交互不产生新的 USER 模型回合）
            try:
                await proactive.note_user_activity(chat_id, private=True)
            except Exception as e:
                logger.warning(f"note_user_activity(callback) 异常: {e}")

            try:
                if sel in SUPPORTED_ROLES:
                    async with global_lock:
                        prev = await get_user_role(chat_id)
                        if prev == sel:
                            await set_user_role(chat_id, None)
                            notice = "已取消角色设定"
                        else:
                            await set_user_role(chat_id, sel)
                            rn = {"china": "中国", "think": "思考", "neko_catgirl": "猫娘", "succubus": "魅魔", "isla": "Isla"}.get(sel, sel)
                            notice = f"已切换到: <b>{rn}</b>"
                        cr = await get_user_role(chat_id)
                        if role_message_ids.get(chat_id) == mid:
                            ok = await update_role_list(chat_id, mid, SUPPORTED_ROLES, cr)
                        else:
                            ok = False
                        if not ok:
                            nm = await send_role_list(chat_id, SUPPORTED_ROLES, cr, mid)
                            if nm:
                                role_message_ids[chat_id] = nm
                    # 使用 sendMessage 避免在 AI 生成中挤占活跃草稿的位置
                    await _send_via_send_message(chat_id, f"✅ <b>{notice}</b>", reply_message_id=mid)
                elif sel in SUPPORTED_MODELS:
                    async with aiohttp.ClientSession() as s:
                        await s.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": f"切换到 {SUPPORTED_MODELS[sel].name}..."})
                    await safe_set_user_model(chat_id, sel)
                    model_name = SUPPORTED_MODELS[sel].name
                    confirmation = (
                        f"✅ <b>模型切换成功</b>\n已切换到模型：<b>{model_name}</b>\n<i>（对话历史已保留）</i>"
                    )
                    # 使用 sendMessage 避免在 AI 生成中挤占活跃草稿的位置。
                    # 【修复】快速连点时，本次点击的列表消息可能已被 delete_after
                    # 定时清理删除；reply 到已不存在的消息会被 Telegram 以 400
                    # 拒绝，旧实现静默吞错，导致"模型已切换却没有任何反馈"。
                    # 这里失败后去掉 reply 补发一次，确保切换结果总能送达。
                    sent_mid = await _send_via_send_message(chat_id, confirmation, reply_message_id=mid)
                    if not sent_mid:
                        await _send_via_send_message(chat_id, confirmation)
                    # 【修复】仿照 /role：点击后不删除列表，而是就地给当前模型
                    # 打 √，列表仍由发送时的 delete_after 定时清理。列表消失前
                    # 的每一次点击都会得到 "√ 移动 + ✅ 消息" 双重反馈。
                    try:
                        async with global_lock:
                            current_role = await get_user_role(chat_id)
                        banner_html = ""
                        if current_role:
                            banner_html = f"⚠️ <b>兼容性提示</b>\n当前角色「<b>{current_role}</b>」可能与新模型不兼容，请留意。"
                        # 读取当前模型与就地更新必须在同一把 chat 锁内完成：
                        # 快速连点时多个回调并发执行，若读与编辑分离，后完成的
                        # 编辑可能携带旧值，使列表上的 √ 停留在已被覆盖的模型上。
                        lock = await get_chat_lock(chat_id)
                        async with lock:
                            await update_model_list(
                                chat_id, mid, list(SUPPORTED_MODELS.keys()),
                                get_user_model(chat_id), banner_html,
                            )
                    except Exception as list_err:
                        logger.warning(f"模型列表就地更新失败(可忽略): chat={chat_id} msg={mid} {list_err}")
                    return "OK", 200
                elif isinstance(sel, str) and sel.startswith("ask:"):
                    parts = sel.split(":", 3)
                    interaction_id = parts[1] if len(parts) > 1 else ""
                    action = parts[2] if len(parts) > 2 else ""
                    arg = parts[3] if len(parts) > 3 else ""
                    ok, notice = await resolve_message_user_callback(
                        chat_id, uid, interaction_id, action, arg
                    )
                    async with aiohttp.ClientSession() as s:
                        await s.post(
                            f"{BASE_URL}/answerCallbackQuery",
                            json={
                                "callback_query_id": cb["id"],
                                "text": notice[:200],
                                "show_alert": not ok,
                            },
                        )
                    return "OK", 200
                else:
                    async with aiohttp.ClientSession() as s:
                        await s.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": "未知操作"})
                    return "OK", 200
            except Exception as e:
                logger.exception(f"Callback query error: {e}")
                await _send_via_send_message(chat_id, f"❌ <b>操作失败</b>\n<code>{str(e)[:100]}</code>", reply_message_id=mid)
                async with aiohttp.ClientSession() as s:
                    await s.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": "操作失败"})
                return "OK", 200

            async with aiohttp.ClientSession() as s:
                await s.post(f"{BASE_URL}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": "已处理"})
            return "OK", 200

    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 应用层异常返回 200 而非 500，避免 Telegram 无限重试同一条 poison update
        logger.exception(f"Webhook 顶层异常: {e}")
        return "OK", 200
    finally:
        _elapsed = time.monotonic() - _t0
        if _elapsed > 1.0:
            logger.warning(f"⚠️ Webhook 处理耗时 {_elapsed:.2f}s, 超过 1s 阈值（应当 < 500ms）")
        else:
            logger.info(f"Webhook 处理耗时 {_elapsed:.3f}s")
