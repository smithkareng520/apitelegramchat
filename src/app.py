# app.py —— Telegram bot 服务装配层（Quart 路由 / 生命周期 / update 分发骨架）。
# 原单体的回合管理、媒体组聚合、命令/回调、列表 UI 已拆分至：
#   app_state.py / app_turns.py / app_media_groups.py / app_lists.py / app_commands.py
from quart import Quart, request
import asyncio
import hmac
import json
import logging
import mimetypes
import os
import time
import uuid
from typing import Any

from utils import (
    get_logger,
    set_request_id,
    extract_sticker_metadata,
    sticker_metadata_to_text,
    transcribe_audio_with_groq,
)
from ai_handlers import _get_cached_audio_data
from config import (
    SUPPORTED_MODELS,
    WEBHOOK_TOKEN,
    DEFAULT_MODEL,
    GROQ_API_KEY,
    LOG_TRUNCATE_LIMIT,
    LOG_LEVEL,
    INGEST_MODE,
)
from state import (
    user_models,
    get_or_init_context,
    get_user_model,
    get_chat_lock,
    add_media_group_message,
    set_current_user_namespace,
    mark_update_processed_if_new,
)
from message_user_tool import (
    get_pending_for_chat,
    resolve_text as resolve_message_user_text,
)
from file_handlers import download_file
from workspace_paths import workspace_download_root
from workspace_utils import _get_workspace_lock
import proactive
from webhook_sync import run_sync_with_deadline
import telegram_polling

import app_state
# 兼容 re-export：tests 与 telegram_polling 经 `app.update_queue` 引用（同一对象，不重赋值）
from app_state import update_queue as update_queue  # noqa: F401
from app_state import WEBHOOK_QUEUE_MAXSIZE as WEBHOOK_QUEUE_MAXSIZE  # noqa: F401
from app_turns import (
    active_tasks,
    _interrupt_active_generation,
    spawn_turn_task,
    _handle_text_message,
    _handle_photo_message,
    _handle_document_message,
    _handle_audio_message,
    _handle_video_message,
    _handle_sticker_message,
    _is_user_flow_active,
    _is_chat_authorized,
    _is_media_model_active,
    _handle_timer_wakeup,
    get_user_info,
    is_authorized,
    reply_unauthorized,
    _get_reply_context,
    _get_reply_media,
)
from app_media_groups import (
    _media_group_tasks,
    _video_group_tasks,
    _document_group_tasks,
    _schedule_media_group,
    _schedule_video_group,
    _schedule_document_group,
)
from app_commands import (
    _handle_admin_commands,
    _handle_start_command,
    _handle_text_command,
    _handle_callback_query,
)

app = Quart(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

logger = get_logger(__name__)

@app.before_serving
async def _startup_load_whitelist() -> None:
    """启动/部署时加载白名单。

    load_whitelist() 内部：R2（权威源，key 见 config.WHITELIST_R2_KEY）
    优先拉取全量白名单——管理员在 R2 控制台手动编辑过的名单也会在此
    批量生效；R2 未配置或拉取失败时回退本地缓存文件；R2 有配置但对象
    不存在时把本地数据播种上 R2。内存 set 原地更新，from-import 引用
    永远有效。
    """
    try:
        from config import load_whitelist
        await load_whitelist()
    except Exception:
        logger.warning("startup load_whitelist failed", exc_info=True)
    # ── 主动唤醒（TIMER 事件源）初始化 ──
    # 注册回调后启动调度器：chat 在首次授权用户活动时才被跟踪。
    try:
        proactive.register_turn_runner(_handle_timer_wakeup)
        proactive.register_busy_check(_is_user_flow_active)
        # 白名单二次校验：保证非授权 chat 永远不会被创建 timer / 触发
        # TIMER 主动消息（即使按钮回调绕过了上层 is_authorized）。
        proactive.register_authorized_check(_is_chat_authorized)
        # 媒体模型门禁：当前模型为原生图片/视频生成模型时 _fire_turn
        # 直接返回，不创建 runner 任务、不重排下一次——timer 自然死亡，
        # 等用户切换回对话型模型后由 note_user_activity 恢复。
        proactive.register_media_model_check(_is_media_model_active)
        await proactive.start_proactive_scheduler()
    except Exception:
        logger.warning("startup proactive scheduler failed", exc_info=True)


@app.before_serving
async def _startup_sync_webhook() -> None:
    """启动摄取通道：polling 模式启动长轮询，webhook 模式做自愈注册。

    ⚠️ 为什么默认是 polling（历史事故根因，勿轻易改回 webhook）：
    Render 的服务位于其托管 Cloudflare 之后，用户**无法关闭该 WAF**。
    托管规则 "Command Injection - Generic - body" 会检查入站请求体，
    命中 `>`/反引号 紧邻 `curl`/`wget` 的 shell 注入特征时在边缘直接
    403，请求根本到不了容器。而 Telegram webhook 是串行、需 2xx 签收
    的投递模型：这条 update 不被签收就永远堵在队头无限重投，导致**后续
    全部消息一起卡死**。改用 getUpdates 后 update 走出站响应体，不受
    入站请求体检查影响，从根上消除该类误杀。
    """
    if INGEST_MODE == "polling":
        app_state._telegram_polling_task = await telegram_polling.start_polling(app_state.update_queue)
        logger.info("摄取通道：getUpdates 长轮询（INGEST_MODE=polling）")
        return

    # webhook 模式：仅当 WEBHOOK_URL 指向能规避 WAF 的自建代理时才推荐。
    logger.warning(
        "摄取通道：webhook（INGEST_MODE=webhook）。若 WEBHOOK_URL 直连 Render 域名，"
        "含 shell 注入特征的消息（如 '>curl -v \"\"'）会被边缘 WAF 403 拦截并堵死投递队列；"
        "建议改用 INGEST_MODE=polling。"
    )
    # fire-and-forget：不阻塞 /health 就绪；内部自带单请求超时与总死线，
    # 任何失败都只降级为"沿用上一次注册"。
    asyncio.create_task(run_sync_with_deadline())


@app.after_serving
async def _shutdown_close_http_session() -> None:
    """优雅关闭：先停 update worker 与主动唤醒调度器，再关掉所有持久
    bash 沙箱进程，最后关全局 aiohttp session。
    """
    # 1) 先停摄取源（polling 模式）：轮询器停掉后不再有新 update 进队列。
    #    polling 模式下未确认的 update 因 offset 未推进，会在下次启动时被
    #    Telegram 重新投递，不会丢失（worker 侧去重保证不会重复处理）——
    #    这比旧 webhook 模式（已 200 ACK 的队列内 update 随进程退出永久
    #    丢失）更安全。
    if app_state._telegram_polling_task is not None:
        app_state._telegram_polling_task.cancel()
        try:
            await app_state._telegram_polling_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("telegram polling shutdown raised", exc_info=True)
        app_state._telegram_polling_task = None

    # 2) 再停后台 worker：不再消费队列、不再派发新的业务任务，避免与
    #    下面的清理逻辑产生新的并发工作。
    if app_state._telegram_worker_task is not None:
        app_state._telegram_worker_task.cancel()
        try:
            await app_state._telegram_worker_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("telegram worker shutdown raised", exc_info=True)
        app_state._telegram_worker_task = None
    if app_state._loop_watchdog_task is not None:
        app_state._loop_watchdog_task.cancel()
        try:
            await app_state._loop_watchdog_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("loop watchdog shutdown raised", exc_info=True)
        app_state._loop_watchdog_task = None
    try:
        await proactive.stop_proactive_scheduler()
    except Exception:
        logger.warning("shutdown proactive scheduler failed", exc_info=True)
    try:
        from tool_executors import _bash_manager
        await _bash_manager.cleanup_all()
    except Exception:
        logger.warning("shutdown _bash_manager.cleanup_all failed", exc_info=True)
    try:
        from utils import close_http_session
        await close_http_session()
    except Exception:
        logger.warning("shutdown close_http_session failed", exc_info=True)


@app.route('/health', methods=['GET'])
async def health_check() -> tuple[dict[str, str], int]:
    # 健康检查端点对外可访问，不应暴露内部统计信息（白名单数量、活跃任务数）。
    # 这些信息可能被探测方用于侧信道推断。
    return {
        "status": "ok",
    }, 200

# ---------- 权限辅助 ----------

# ---------------------------------------------------------------------------
# Webhook 入口（非阻塞）+ 后台 update worker
# ---------------------------------------------------------------------------
# 数据流（入口与业务彻底解耦）：
#
#   Telegram
#     │  POST /webhook（Header: X-Telegram-Bot-Api-Secret-Token 或 ?token=）
#     ▼
#   webhook()：校验 token → 读 JSON → update_queue.put_nowait() → 立即 200
#     │               （不做任何业务处理，绝不调 AI / 数据库 / 长网络 IO）
#     ▼
#   update_queue（有界队列；满时 429 把背压交还 Telegram）
#     ▼
#   telegram_worker()（后台串行消费）→ process_update()
#     （解析 message / 去重 / chat lock / 命令分发 / AI 任务派发 / 回调查询）
#
# 关键收益：即使某条 update 把 AI 卡死，webhook 入口依然对后续投递秒回
# 200，Telegram 不会因超时判定 webhook 挂掉（避免 504 → 重试风暴 → 死锁）。
# 日志定位：只看到 "telegram webhook arrived" 没有 "accepted" → token 问题；
# 有 "accepted" 没有 "telegram worker processing update" → queue/worker 问题；
# 有 "worker processing" 之后的异常 → 业务/AI 问题。

async def telegram_worker() -> None:
    """后台 worker：串行消费 update 队列，执行全部业务逻辑。

    - 串行消费天然保持 update 顺序；同 chat 的真正并发由既有
      get_chat_lock / active_tasks 机制管理，AI 回合本身仍是
      asyncio.create_task 派发，不在这里同步等待完成。
    - 每条 update 用 create_task 包一层再 await：task 会拷贝当前
      contextvars，process_update 内部的 set（request_id / 用户
      namespace 等）不会泄漏到下一条 update，语义与旧版"每个 webhook
      请求一个全新上下文"一致。
    - 任何业务异常都在这里兜底记录，绝不杀死 worker 循环。
    """
    logger.info("telegram worker started")
    while True:
        update = await app_state.update_queue.get()
        try:
            uid = (update or {}).get("update_id")
            logger.info(f"telegram worker processing update (update_id={uid})")
            await asyncio.create_task(process_update(update))
        except asyncio.CancelledError:
            logger.warning("telegram worker cancelled (shutdown)")
            raise
        except Exception:
            logger.exception("telegram update failed")
        finally:
            app_state.update_queue.task_done()

@app.before_serving
async def _startup_start_telegram_worker() -> None:
    """启动后台 update 消费 worker（webhook 非阻塞化的另一半）。"""
    app_state._telegram_worker_task = asyncio.create_task(telegram_worker(), name="telegram-worker")

# ---------------------------------------------------------------------------
# Event loop watchdog（循环健康心跳）
# ---------------------------------------------------------------------------
# 目的：把"消息没有任何日志"这类事故在事后日志里 10 秒定位。单进程单 loop
# 架构下（quart run = 1 worker），任何同步阻塞 / CPU 密集任务都会冻结整个
# loop：期间 webhook 无法 ACK、日志无法写出、/health 无法响应——表现恰好
# 是"Render 没新日志"。watchdog 用 sleep 前后单调时钟差直接测量 loop 实际
# 响应延迟，并把队列/任务水位写进周期心跳，三种故障一目了然：
#
#   a) heartbeat 出现 ≥阈值 gap（本该 10s 一条却隔了几十秒）→ loop 被冻结；
#   b) gap 正常、queue 深度持续增长、"arrived" 有而 "worker processing" 停
#      滞 → telegram_worker 被某条 update 卡死；
#   c) gap 正常、queue=0、完全没有 "arrived" → Telegram 侧未投递（此时再
#      用 /webhookinfo 查 pending/last_error）。
LOOP_WATCHDOG_INTERVAL_S = float(os.getenv("LOOP_WATCHDOG_INTERVAL_S", "10"))
LOOP_LAG_WARN_S = float(os.getenv("LOOP_LAG_WARN_S", "5"))
# _loop_watchdog_task 句柄在 app_state（与其他后台任务句柄一致）


async def _loop_watchdog() -> None:
    """每 LOOP_WATCHDOG_INTERVAL_S 醒一次，报告 loop lag 与队列水位。"""
    interval = LOOP_WATCHDOG_INTERVAL_S
    tick = 0
    while True:
        start = time.monotonic()
        await asyncio.sleep(interval)
        # loop 实际多睡了多久 = 本 tick 内被同步/CPU 任务占死的时间
        lag = time.monotonic() - start - interval
        tick += 1
        if lag >= LOOP_LAG_WARN_S:
            logger.critical(
                f"🚨 EVENT LOOP BLOCKED: 期望休眠 {interval:.1f}s 实际 {lag + interval:.1f}s "
                f"(lag={lag:.2f}s) —— 该窗口内 webhook/日志/健康检查全部不可调度，"
                f"Render 健康检查连续失败会触发重启"
            )
        # 常规水位：每 6 tick（默认 1 分钟）打一条，事故时留时间线证据
        if tick % 6 == 0 or lag >= LOOP_LAG_WARN_S:
            try:
                logger.info(
                    f"heartbeat: loop_lag={lag:.2f}s "
                    f"queue={app_state.update_queue.qsize()}/{WEBHOOK_QUEUE_MAXSIZE} "
                    f"active_tasks={len(active_tasks)} tasks={len(asyncio.all_tasks())}"
                )
            except Exception:
                # watchdog 自身绝不能死：水位统计失败只降级为纯 lag 心跳
                logger.info(f"heartbeat: loop_lag={lag:.2f}s (stats unavailable)")


@app.before_serving
async def _startup_start_loop_watchdog() -> None:
    """启动 event loop watchdog（与 telegram_worker 互不干扰）。"""
    app_state._loop_watchdog_task = asyncio.create_task(_loop_watchdog(), name="loop-watchdog")

@app.route('/webhook', methods=['GET', 'POST', 'HEAD'])
async def webhook() -> tuple:
    """Telegram webhook 入口：只做 校验 → 读 JSON → 入队 → 立即 ACK。

    这里绝不做业务处理（不解析 message、不拿 chat lock、不调 AI、不查
    数据库）——所有可能慢的操作都在 telegram_worker / process_update。
    """
    _t0 = time.monotonic()
    try:
        request_id = str(uuid.uuid4())[:8]
        # 最外层 TCP 级心跳：print 直写 stdout（PYTHONUNBUFFERED=1 立即刷出），
        # 完全绕过 logging 管线，排除"logger 被配置过滤/handler 异常"的干扰。
        # 判读：本行有、下方 "telegram webhook arrived" 无 → logging 管线问题；
        #       两行都无 → 请求根本没进 Quart（Telegram 未投递，或 event loop
        #       被同步/CPU 任务冻结——配合 loop watchdog 心跳 gap 即可区分）。
        print(f">>> WEBHOOK TCP ARRIVED id={request_id}", flush=True)
        set_request_id(request_id)
        logger.info(f"telegram webhook arrived {request_id}")

        # ── 1. Telegram secret 校验（双路径，任一匹配即放行）───────────────
        #  a) URL query ?token=…（历史注册方式，保持完全兼容）；
        #  b) X-Telegram-Bot-Api-Secret-Token 请求头——webhook_sync.py 启动
        #     自愈以 secret_token=WEBHOOK_TOKEN 注册后，Telegram 每次投递都
        #     会携带该头，token 不再出现在 URL/访问日志里。
        #  两路均用 hmac.compare_digest 恒定时间比较防止时序攻击。
        token = request.args.get("token")
        token_ok = False
        if WEBHOOK_TOKEN:
            if token and hmac.compare_digest(str(token), str(WEBHOOK_TOKEN)):
                token_ok = True
            else:
                secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
                if secret_header and hmac.compare_digest(str(secret_header), str(WEBHOOK_TOKEN)):
                    token_ok = True
        if not token_ok:
            logger.warning("Invalid telegram webhook token")
            return "Forbidden", 403
        logger.info("telegram webhook accepted")

        if request.method in ('GET', 'HEAD'):
            return "OK - Webhook is alive", 200

        # polling 模式下本入口保持鉴权可探活，但不再接收业务 update：
        # 两条摄取链路同时入队会造成同一 update 被处理两次（去重集合虽能
        # 兜底，但 offset 与去重窗口的竞态没有必要引入）。返回 200 而非
        # 错误码，避免任何残留的 webhook 注册触发 Telegram 重试风暴。
        if INGEST_MODE == "polling":
            logger.warning(
                "收到 webhook 投递但当前 INGEST_MODE=polling，已忽略（说明 Telegram 侧仍有"
                "残留 webhook 注册，重启会自动 deleteWebhook）"
            )
            return "OK - polling mode, webhook ignored", 200

        # ── 2. 快速读取 update（只做入队前必要校验）────────────────────────
        try:
            data = await request.json
        except Exception:
            logger.exception("Invalid webhook json")
            return "Bad Request", 400
        if not data:
            return "OK", 200
        uid = data.get('update_id')
        # 缺少 update_id 的非法 payload 直接 400 拒绝，避免污染 worker 侧去重集合
        if uid is None:
            return "Bad Request", 400

        # ── 3. 丢队列（去重由 worker 侧 process_update 原子完成）────────────
        try:
            app_state.update_queue.put_nowait(data)
        except asyncio.QueueFull:
            # 队列满：429 把背压交还 Telegram（按指数退避重投），而不是
            # 200 吞掉丢消息，也不是无限扩队列打爆内存。去重标记在
            # worker 侧，此时尚未标记，Telegram 重投后可正常入队。
            logger.error(
                f"telegram webhook queue full (maxsize={WEBHOOK_QUEUE_MAXSIZE}), "
                f"update_id={uid} rejected with 429, Telegram will retry"
            )
            return "Too Many Requests", 429

        logger.info(f"telegram webhook queued update_id={uid}")
        return "OK", 200
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 入口意外异常返回 500 让 Telegram 重投：去重标记发生在 worker 侧，
        # 此时该 update 一定尚未入队/未被标记，重投不会产生重复处理。
        logger.exception(f"Webhook 入口顶层异常: {e}")
        return "Internal Server Error", 500
    finally:
        _elapsed = time.monotonic() - _t0
        if _elapsed > 0.5:
            logger.warning(f"⚠️ Webhook 入口耗时 {_elapsed:.2f}s（只做校验+入队，正常应 <100ms）")
        else:
            logger.info(f"Webhook 入口耗时 {_elapsed:.3f}s")

async def process_update(data: dict) -> None:
    """原 webhook 的全部业务逻辑（telegram_worker 内执行）。

    职责：解析 update → 去重 → 命令/媒体/文本分发 → 派发 AI 任务或处理
    回调查询。运行在 worker 的独立 task 上下文中，不处于 Quart 请求
    上下文——不得访问 request / g 等请求期对象。
    """
    _t0 = time.monotonic()
    try:
        # worker 复用同一 task 上下文循环消费；这里为本条 update 生成独立
        # request_id 供日志串联（worker 已用 create_task 拷贝上下文执行本
        # 协程，这里的 set 不会泄漏到下一条 update）。
        set_request_id(str(uuid.uuid4())[:8])

        # 排障期用于确认"消息是否真的进到了应用"的临时打点。根因已定位为
        # 边缘 WAF 拦截（消息压根到不了这里），改用 polling 后不再需要无条件
        # 打印用户原文——降级为 DEBUG，避免把消息内容长期写进生产日志。
        if logger.isEnabledFor(logging.DEBUG):
            _dbg = data.get("message") or {}
            logger.debug(
                "[ENTITIES] text=%r entities=%s",
                _dbg.get("text"),
                _dbg.get("entities"),
            )

        uid = data.get('update_id')
        # 防御：webhook 入口已拦截缺少 update_id 的非法 payload，这里再拦
        # 一层（防止其它调用路径把 None 塞进去重集合）。
        if uid is None:
            logger.warning("telegram worker update missing update_id, skipped")
            return

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

        # 去重（原子"检查并标记"，容量淘汰见 state._record_processed_unlocked）：
        # 放在 worker 侧而非 webhook 入口——队满被 429 拒收的 update，
        # Telegram 重投时不会被误判为重复；单一 worker 串行消费也天然
        # 消除了旧版"webhook 并发双副本同时通过检查"的竞态。
        if not await mark_update_processed_if_new(uid):
            logger.info(f"telegram worker duplicate update_id={uid}, skipped")
            return

        # ── 消息处理 ──────────────────────────────────────────────────────
        if "message" in data and isinstance(data["message"], dict):
            msg = data["message"]
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id is None:
                logger.warning(f"Webhook: missing chat_id in message, update_id={uid}")
                return
            from_user = msg.get("from", {})
            username, user_id = get_user_info(msg)
            set_current_user_namespace(user_id or str(chat_id))

            text = msg.get("text", "") or ""
            if await _handle_admin_commands(chat_id, msg, text, username, user_id):
                return
            if await _handle_start_command(chat_id, msg, text, username, user_id):
                return
            if not is_authorized(username, user_id):
                await reply_unauthorized(chat_id, msg.get("message_id"))
                return

            lock = await get_chat_lock(chat_id)
            async with lock:
                ctx = get_or_init_context(chat_id)
                ctx["username"] = (from_user.get("username") or from_user.get("first_name") or str(from_user.get("id", chat_id)))
                # tg_username 只存真实 Telegram 用户名（可能为空），专供
                # _is_chat_authorized 等授权路径使用；ctx["username"] 的
                # first_name/ID 回退仅用于展示，不参与白名单匹配。
                ctx["tg_username"] = (from_user.get("username") or "").strip()
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
                    return

                content_text = (
                    f"📎 用户分享了当前位置\n"
                    f"坐标：{lat:.6f}, {lon:.6f} (WGS-84)\n\n"
                    f"如果用户问起『附近』『周边』等，请直接以此坐标作为中心点，"
                    f"调用 search_poi / route / distance 等工具，无需再调用 geocode。"
                    f"如需反查中文地址，请调用 amap-maps MCP 的 maps_regeocode 工具。"
                )
                # 首次绑定即标注 dict[str, Any]：后续媒体分支会写入 list/dict
                # 值（file_ids/attachments/sticker_meta 等 Telegram 载荷）。
                user_message: dict[str, Any] = {"role": "user", "content": content_text}
                await spawn_turn_task(chat_id, _handle_text_message(chat_id, content_text, username, user_message))
                return

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
                return

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
                return

            # ── 文档组 ─────────────────────────────────────────────────────
            if "media_group_id" in msg and "document" in msg:
                mg = msg["media_group_id"]
                await add_media_group_message(mg, msg)
                # 文档组同样只在首个分片时中断旧草稿；同组后续文件继续聚合。
                if mg not in _document_group_tasks:
                    await _interrupt_active_generation(chat_id)
                    await _schedule_document_group(chat_id, mg)
                return

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

                await spawn_turn_task(chat_id, _handle_photo_message(chat_id, user_message, username))
                return

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

                await spawn_turn_task(chat_id, _handle_document_message(chat_id, user_message, username))
                return

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
                    await spawn_turn_task(chat_id, _handle_audio_message(chat_id, user_message, username))
                    return
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
                    await spawn_turn_task(chat_id, _handle_audio_message(chat_id, user_message, username))
                    return
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

                    await spawn_turn_task(chat_id, _handle_video_message(chat_id, user_message, username))
                    return

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

                content_lines = ["📎 用户发送了贴纸"]
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

                await spawn_turn_task(chat_id, _handle_sticker_message(chat_id, user_message, username))
                return

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
                    return
                if await _handle_text_command(chat_id, msg, user_input):
                    return
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

                await spawn_turn_task(chat_id, _handle_text_message(chat_id, user_input, username, user_message))
                return
        if "callback_query" in data:
            await _handle_callback_query(data["callback_query"])
            return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # worker 兜底吞掉业务异常：单条 poison update 最多浪费一次消费
        # 循环，不再影响 webhook ACK（入口已立即 200），也不会触发
        # Telegram 对这条消息的重试风暴。
        logger.exception(f"process_update 顶层异常: {e}")
        return
    finally:
        _elapsed = time.monotonic() - _t0
        # worker 侧耗时不再影响 Telegram ACK；>30s 仅作为"疑似卡住"的观测告警
        if _elapsed > 30:
            logger.warning(f"⚠️ worker update 处理耗时 {_elapsed:.2f}s, 超过 30s（疑似 AI/下载卡住）")
        else:
            logger.info(f"worker update 处理耗时 {_elapsed:.3f}s")
