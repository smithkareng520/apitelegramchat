"""回合任务管理与上下文守卫（自 app.py 拆出）。

包含：active_tasks 登记、旧回合打断与进度保全、token 估算与
pre_flight 自动压缩、历史/台账写入、6 类消息 handler、proactive
门禁回调与 TIMER 唤醒回合。
"""
import asyncio
import json
import time
from typing import Any, cast

from token_budget import count_tokens

from utils import (
    send_rich_html_message,
    mark_draft_dead,
    get_logger,
    extract_message_text,
)
from ai_handlers import get_ai_response
from config import SUPPORTED_MODELS, is_admin_identity, is_whitelisted_identity
from state import (
    user_contexts,
    user_models,
    get_or_init_context,
    get_user_model,
    get_chat_lock,
    get_active_draft_info,
    clear_active_draft,
    mark_preserved_draft,
)
import turn_recovery
import proactive
from context_window import (
    CONTEXT_COMPACT_TARGET_RATIO,
    CONTEXT_COMPACT_TRIGGER_RATIO,
    CONTEXT_PROTECTED_TURNS,
    apply_eviction_plan,
    build_digest_text,
    effective_digest_budget,
    plan_turn_eviction,
    resolve_history_budget,
)
from tool_context_compaction import compact_older_tool_calls, _eligible_calls
from workspace_utils import init_workspace


logger = get_logger(__name__)

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
        # message_id 为 None 时 int(None) 抛 TypeError，由下方 except 统一
        # 返回 None（即"无 reply 目标"）；cast 仅为类型标注，不改运行时行为。
        mid = int(cast(int, message_id))
    except (TypeError, ValueError):
        return None
    if mid <= 0:
        return None
    # 被回复的消息可能已被删除（例如模型/角色列表已到 delete_after 定时
    # 清理时间，而客户端仍显示旧按钮，用户点击后回调才到达）。
    # allow_sending_without_reply 让反馈消息在引用目标丢失时降级为普通
    # 消息继续送达，避免"操作已生效但用户看不到任何反馈"。
    return {"message_id": mid, "allow_sending_without_reply": True}


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
      4. 冻结旧草稿（mark dead + 注销活跃注册 + 标记保留）。旧任务的取消
         路径已先尝试把草稿内容经 sendRichMessage 固定为永久消息
         （ai_handlers → builder.finalize_interrupted_draft），本步骤只
         作为固定失败 / 无可见内容时的兜底现场；不再发送"⏹️ 已停止
         输出"之类的提示消息——该提示在打断保全机制下已无价值。

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
def get_user_info(msg: dict) -> tuple[str, str]:
    from_user = msg.get("from", {})
    username = (from_user.get("username") or "").strip()
    user_id = str(from_user.get("id", ""))
    return username, user_id

def is_admin(username: str, user_id: str) -> bool:
    # 委托 config.is_admin_identity：管理员判断集中在一处，用户名大小写
    # 不敏感（Telegram 用户名语义），数字 ID 精确匹配。
    return is_admin_identity(username, user_id)

def is_authorized(username: str, user_id: str) -> bool:
    # 管理员始终授权（与用户白名单无关）；普通用户查用户白名单
    # （config.is_whitelisted_identity 内部做大小写归一化，与
    # /adduser 存储时同源，避免大小写不一致导致的授权失败）。
    if is_admin(username, user_id):
        return True
    return is_whitelisted_identity(username, user_id)

async def reply_unauthorized(chat_id: int, reply_message_id: int | None = None) -> None:
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
# ---------- Token 估算及上下文修剪 ----------
_MEDIA_TOKEN_OVERHEAD = 64
_MESSAGE_WRAPPER_TOKENS = 4

def estimate_tokens(text: str) -> int:
    """Return the exact tokenizer count for model-facing text."""
    return count_tokens(text)

def _estimate_content_tokens(content: Any) -> int:
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

def _estimate_history_tokens(history: list[dict]) -> int:
    """按请求侧同一口径估算当前持久历史的 token 量。

    旧版经由 select_request_context 生成快照再估算（每轮产生整份浅拷贝
    且语义上依赖"滑动视图"）；新策略下历史本身就是请求上下文，直接计数。
    """
    return sum(_estimate_message_tokens(message) for message in history)


async def pre_flight_context_check(chat_id: int, new_user_message: dict) -> bool:
    """上下文窗口自动压缩（auto-compaction，主流 Agent 上下文策略）。

    策略要点（详见 CACHE_OPTIMIZATION.md）：

    - **快路径（常态）**：历史 + 新输入 ≤ 触发水位（budget ×
      ``CONTEXT_COMPACT_TRIGGER_RATIO``）→ 一字节不改直接放行。两次
      压缩事件之间请求前缀字节级一致，provider 端 prompt/KV 缓存
      全量命中——这是本策略的第一目标。
    - **压缩事件（罕见、摊销）**：两级杠杆按顺序使用，直到降到
      目标水位（budget × ``CONTEXT_COMPACT_TARGET_RATIO``），而不是
      "刚好塞得下"：
        L1（无损）：较老一半的工具负载归档为指针（payload →
          workspace 归档文件，模型可经 text_editor 取回）；
        L2（结构）：待淘汰区内的工具调用先归档，然后从最老的用户
          轮块开始整块淘汰（保护最近 ``CONTEXT_PROTECTED_TURNS``
          轮），被淘汰轮合并进历史头部稳定槽位的滚动摘要。
      一次事件清出约 40% 预算的空间，之后很多轮内不再触发（滞后 /
      hysteresis）。
    - 旧版每轮"从历史前端逐块删到塞得下"的行为被完全取代：淘汰
      不再是历史长度的连续函数，而是离散事件。

    返回 False 仅当新消息自身超过预算（即便空历史也放不下）；
    历史侧超限由请求守卫（context_manager.select_request_context）
    在出站视图上兜底，并在下一轮的压缩事件中收敛回预算内。
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
        budget = resolve_history_budget(max_context, max_output)
        trigger_tokens = int(budget * CONTEXT_COMPACT_TRIGGER_RATIO)
        target_tokens = int(budget * CONTEXT_COMPACT_TARGET_RATIO)
        new_input_est = max(1, _estimate_message_tokens(new_user_message))

        history_est = _estimate_history_tokens(history)
        if history_est + new_input_est <= trigger_tokens:
            return True

        # ───────── 压缩事件（单一代码路径） ─────────
        archived_calls = 0
        evicted_blocks = 0
        evicted_messages = 0
        # L1：较老一半 eligible 工具调用 → 归档指针（无损，可取回）。
        first_pass = await compact_older_tool_calls(chat_id, history)
        history_est = _estimate_history_tokens(history)
        archived_calls += first_pass.compacted_calls
        if first_pass.compacted_calls:
            logger.info(
                "Context compaction L1 (tool payload archive): chat=%s calls=%s "
                "archived_bytes=%s history_tokens=%s target=%s",
                chat_id, first_pass.compacted_calls, first_pass.archived_bytes,
                history_est, target_tokens,
            )

        # L2：仍超目标水位 → 结构性淘汰（先归档待淘汰区，再整块淘汰进摘要）。
        history_target = max(0, target_tokens - new_input_est)
        if history_est > history_target:
            plan = plan_turn_eviction(
                history,
                target_tokens=history_target,
                protected_turns=CONTEXT_PROTECTED_TURNS,
                token_fn=_estimate_message_tokens,
            )
            if plan.evicted_blocks:
                # 待淘汰区内的 eligible 调用先归档，让摘要 T 行能指向
                # 可取回的归档文件，负载不随轮块一起丢弃。
                # 注意下标口径：_eligible_calls 的 idx 是含摘要消息的
                # 全历史下标，块内消息数需要补上摘要槽位的偏移。
                digest_offset = 1 if plan.digest_message is not None else 0
                region_end = digest_offset + plan.evicted_message_count
                region_calls = sum(
                    1 for idx, _tc, _tr in _eligible_calls(history)
                    if digest_offset <= idx < region_end
                )
                if region_calls > 0:
                    region_pass = await compact_older_tool_calls(
                        chat_id, history, calls_to_compact=region_calls,
                    )
                    archived_calls += region_pass.compacted_calls
                    if region_pass.compacted_calls:
                        history_est = _estimate_history_tokens(history)
                # 载荷变指针后历史变小，重新规划（往往可以少淘汰几轮）。
                plan = plan_turn_eviction(
                    history,
                    target_tokens=history_target,
                    protected_turns=CONTEXT_PROTECTED_TURNS,
                    token_fn=_estimate_message_tokens,
                )
            if plan.evicted_blocks:
                prev_digest_text = (
                    plan.digest_message.get("content")
                    if plan.digest_message is not None else None
                )
                digest_text = build_digest_text(
                    prev_digest_text,
                    plan.evicted_blocks,
                    budget_tokens=effective_digest_budget(budget),
                )
                apply_eviction_plan(history, plan, digest_text)
                history_est = _estimate_history_tokens(history)
                evicted_blocks = len(plan.evicted_blocks)
                evicted_messages = plan.evicted_message_count
                # 结构性淘汰后旧台账不再与剩余历史一一对应。
                ctx["token_ledger"] = []
                ctx["last_prompt_tokens"] = 0
                ctx["last_completion_tokens"] = 0

        logger.info(
            "Context compaction event: chat=%s model=%s budget=%s trigger=%s target=%s "
            "archived_calls=%s evicted_blocks=%s evicted_messages=%s history_tokens=%s "
            "elapsed_ms=%s",
            chat_id, cm, budget, trigger_tokens, target_tokens,
            archived_calls, evicted_blocks, evicted_messages, history_est,
            int((time.monotonic() - _pf_start) * 1000),
        )

        # 唯一不可服务情形：新消息自身超预算（空历史也放不下）。
        if new_input_est >= budget:
            return False
        if history_est + new_input_est > budget:
            logger.warning(
                "Pre-flight compaction 未达预算（保护尾过大）: chat=%s "
                "history_tokens=%s budget=%s —— 出站视图将由请求守卫兜底裁剪。",
                chat_id, history_est, budget,
            )
        return True

async def update_conversation_and_ledger(chat_id: int, user_message: dict | None, new_msgs: list, usage: dict[str, Any] | None = None) -> None:
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
        # 历史有界性维护已全部收敛到 pre_flight_context_check 的压缩事件
        # （触发水位 × 预算，滞后触发）。工具负载压缩不按消息条数触发：
        # 条数与 token 预算无关，会在大窗口模型上过早、小窗口模型上过晚
        # 触发，且与 pre-flight 属于两套互不知情的口径。
async def _cancel_old_task(chat_id: int) -> None:
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

async def _cleanup_task(chat_id: int, task: asyncio.Task) -> None:
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
# 注意：这里不发送任何 sendChatAction：chat action 描述的是 bot 自己
# 正在做的动作，用户上传媒体时回发这些动作会被客户端渲染成“bot 正在
# 上传照片/语音/…”，语义完全相反；typing 也只在模型流式输出期间才有
# 意义（见 chat_actions.py 与 ai/agentic_loops.py 的实现）。
async def _handle_text_message(chat_id: int, user_input: str, username: str, user_message: dict) -> None:
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

async def _handle_photo_message(chat_id: int, user_message: dict, username: str) -> None:
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

async def _handle_document_message(chat_id: int, user_message: dict, username: str) -> None:
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

async def _handle_audio_message(chat_id: int, user_message: dict, username: str) -> None:
    # 用户上传语音时回发 upload_voice 是错误语义（那是“bot 正在上传语音”
    # 的指示）——用户上传的内容与 chat action 无关，这里不发送任何动作。
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

async def _handle_video_message(chat_id: int, user_message: dict, username: str) -> None:
    """处理直接上传的视频 / 圆形视频消息（video_note）。

    与图片消息对称：user_message 携带 file_id / mime_type 等元数据存入
    对话历史，每轮由 _resolve_multimodal_content 按当前模型能力重新解析
    ——支持视频输入的模型（stealth/ox-alpha、Gemini 系列等）收到
    video_url content part，不支持的模型收到文本占位；切换模型不丢信息。
    """
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

async def _handle_sticker_message(chat_id: int, user_message: dict, username: str) -> None:
    """处理用户发送的贴纸（sticker）。

    贴纸本体（TGS / WebP / WebM）目前主流 LLM 不可直接识别，因此只把
    Telegram Sticker 对象携带的可语义化字段——emoji / emoji_list / type /
    set_name / custom_emoji_id / format——拼成文本作为 user 消息发给模型。
    不携带 file_id 附件，避免 _resolve_multimodal_content 把不可识别的
    媒体推到模型 API 触发 400。
    """
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


def _is_chat_authorized(chat_id: int) -> bool:
    """该 chat_id 是否在白名单内（供 proactive 调度器二次校验）。

    私聊场景下 chat_id == user_id；白名单条目可能是 @username 或纯数字
    user_id 字符串。这里同时查两条路径：
      1. ``str(chat_id)`` 直接查 user_id 路径；
      2. 从会话上下文读出之前缓存的 ``username``，查 @username 路径。
    这样无论用户是以哪种形式被 /adduser 加入白名单，proactive 都能
    准确判断该 chat 是否授权，避免非白名单用户因 callback 等旁路入口
    被误创建 timer 进而收到 TIMER 主动消息。
    """
    uid = str(chat_id)
    if is_admin_identity(user_id=uid) or is_whitelisted_identity(user_id=uid):
        return True
    try:
        ctx = get_or_init_context(chat_id)
        # 只用真实的 Telegram 用户名做白名单匹配（tg_username），绝不使用
        # ctx["username"]——后者在用户无 username 时会回退成 first_name，
        # 而 first_name 不唯一，用它匹配白名单可能把同名陌生人误判为授权。
        username = (ctx.get("tg_username") or "").strip()
    except Exception:
        username = ""
    if username and (is_admin_identity(username=username) or is_whitelisted_identity(username=username)):
        return True
    return False


def _is_media_model_active(chat_id: int) -> bool:
    """该 chat 当前模型是否为原生图片/视频生成模型（供 proactive 门禁）。

    返回 True 时 proactive._fire_turn 会跳过 runner 创建且不重排下一次，
    timer 自然死亡——等用户切换回对话型模型并产生任意活动后再恢复。
    """
    try:
        cm = get_user_model(chat_id)
    except Exception:
        logger.debug("_is_media_model_active 读取模型失败，按非媒体模型处理", exc_info=True)
        return False
    model_info = SUPPORTED_MODELS.get(cm)
    if model_info is None:
        return False
    return bool(
        getattr(model_info, "native_image", False)
        or getattr(model_info, "native_video", False)
    )


async def _handle_timer_wakeup(chat_id: int) -> None:
    """TIMER 事件源回合：系统后台唤醒 agent 的“自己的活动时间”。

    与用户回合共用同一份会话历史（统一上下文），并走**同一套草稿与交付
    流程**（由 /show 开关统一决定，见 ai_handlers.get_ai_response；
    静默交付的 send 缺省值按事件源区分——TIMER 缺省 false，USER 缺省
    true）：
    - /show on：展示富文本草稿，最终回复经 sendRichMessage 送达用户；
    - /show off：静默运行（TIMER 回合 send 缺省 false，不填 / 不调用均
      不发送），模型经 deliver_reply(send=true) / message_user 触达用户；
    - 向请求上下文追加合成 user 消息（WAKEUP_PROMPT），但不写入持久历史；
    - 回合被用户消息打断时由 proactive.interrupt_proactive_flow 取消
      任务并触发 turn_recovery 轮次日志保全（已完成的进度沉淀进历史，
      此处无需感知）。
    """
    try:
        # 纵深防御：proactive._fire_turn 在创建本 runner 之前已经做过一次
        # 媒体模型门禁（且不会重排下一次 timer）。这里再检查一次是为了覆盖
        # 极小的竞态窗口——proactive 检查通过后、runner 启动前用户刚好把
        # 模型切到原生图片/视频生成模型。这里发现就 return（防止把
        # WAKEUP_PROMPT 喂给媒体模型导致意外生成媒体并推给用户）；return
        # 后 _run 的 finally 会调 note_turn_finished 重排下一次 timer，
        # 那次 timer 触发时 proactive 层的门禁会彻底终止自递归调度。
        #
        # 白名单同款复检：_fire_turn 检查通过后、本 runner 真正开跑前，用户
        # 可能刚被管理员 /deluser 移出白名单——此处拦截后整轮不执行，
        # finally 的 note_turn_finished 只会重排 timer，下一次触发时会被
        # proactive 层的白名单门禁（SKIP_UNAUTHORIZED）彻底拦下。
        if not _is_chat_authorized(chat_id):
            logger.info(
                "[TIMER] chat=%s 回合开跑前复检发现已被移出白名单，本轮不执行",
                chat_id,
            )
            return
        lock = await get_chat_lock(chat_id)
        async with lock:
            cm = get_user_model(chat_id)
            ctx = get_or_init_context(chat_id)
            username = ctx.get("username") or f"User_{chat_id}"
        model_info = SUPPORTED_MODELS.get(cm)
        if model_info is not None and (
            getattr(model_info, "native_image", False) or getattr(model_info, "native_video", False)
        ):
            logger.info(
                f"[proactive] chat={chat_id} 当前模型 {cm} 为原生媒体模型"
                f"（runner 兜底命中，回合空转退出；下一次 timer 触发时 proactive 层门将彻底终止自递归）"
            )
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

async def spawn_turn_task(chat_id: int, coro: Any) -> asyncio.Task:
    """派发新回合任务并登记为可打断任务（process_update 各分支共用样板）。

    合并原先在 process_update 各消息分支重复 8 处的派发样板：
    打断旧回合 → create_task → 写入 active_tasks → 挂自动清理回调。
    """
    await _interrupt_active_generation(chat_id)
    task = asyncio.create_task(coro)
    async with active_tasks_lock:
        active_tasks[chat_id] = task
    task.add_done_callback(lambda t: asyncio.create_task(_cleanup_task(chat_id, t)))
    return task

