# telegram_polling.py
"""Telegram getUpdates 长轮询摄取通道（webhook 的等价替代）。

为什么需要这个模块（事故根因 1：入站 WAF 误杀）
------------------------------------------------------------------
Render 的所有服务默认位于 Cloudflare 边缘之后，且**用户无法关闭或调整**
其托管 WAF 规则集（官方 feature request 长期 open）。该规则集里有一条
"Command Injection - Generic - body"：它检查**入站请求体**，命中
`>` / 反引号等 shell 重定向/替换符紧邻 `curl` / `wget` 并带参数的模式时，
直接在边缘返回 403，请求根本不会到达容器。

于是 Telegram 投递 `>curl -v "test"` 这类消息时：

    Telegram → POST /webhook（body 含 ">curl -v ...")
             → Cloudflare 边缘 WAF 命中 → 403
             → 应用完全无感知（无日志、无 TCP 打印）

Telegram 的 webhook 是**串行、需 2xx 签收**的投递模型：这条 update 不被
签收就永远排在队头，按指数退避无限重投，**后续所有消息一起被堵死**——
正是"消息积压之后不能响应任何 Webhook 请求"的现象。

修复思路：不去和 WAF 规则搏斗（Render 上也改不了），而是**换一条数据流
方向**。改用 getUpdates 长轮询后，update 内容位于我们发起的 HTTPS 请求的
**响应体**里。Cloudflare 的入站请求体检查对出站响应不生效。

为什么还要加主管任务（事故根因 2：轮询任务静默死亡）
------------------------------------------------------------------
2026-09-15 现场：进程活着、event loop 健康（loop_lag=0.00s）、/health 恒
返回 200、心跳每分钟准点输出 `queue=0/1000 active_tasks=0 tasks=12` 一动
不动，但**连续数十分钟没有任何一行 "telegram polling fetched" /
"queued update_id" / "telegram worker processing update"**——用户发消息
石沉大海。

这不是"没人发消息"，而是**摄取通道已经死了，却没有任何人发现**：

  · 旧版 `poll_updates_forever` 对 `asyncio.CancelledError` 一律
    `raise`（注释写的是"shutdown"）。但 CancelledError 并不只来自关停：
    aiohttp 的 `ClientTimeout` 内部就是靠取消请求任务实现的，取消信号落
    在 `async with` 退出/连接归还的窗口里时会以 CancelledError（而不是
    TimeoutError）逸出；上层任何一次误取消同理。一次就够——任务退出，
    **进程永久失聪**。
  · 没有任何主管：`_telegram_polling_task` 在 before_serving 里创建后，
    除了关停路径再没人看过它一眼，`.done()` 永远没人检查。
  · `/health` 是写死的 200：Render 健康检查与 Docker HEALTHCHECK 都认为
    实例健康，于是**永远不会重启**，故障可以挂到天荒地老。
  · 心跳不含摄取通道状态：日志里"空闲"和"失聪"长得一模一样，事后无法
    区分——这正是本次排查最费劲的地方。

因此本模块现在提供三层保障：

  1. **循环自愈**：非关停来源的 CancelledError 不再杀死循环，记 CRITICAL
     后 uncancel 并继续（关停/主管重启会先置 `_stop_requested`，语义不被
     稀释）。
  2. **主管重启**：`_supervise` 持有轮询子任务，子任务无论以何种方式结束
     都按退避重新拉起；并监测"停摆"（连续 STALL_SECONDS 没有一次成功的
     getUpdates，含正常空轮询），超时即强制重启。
  3. **对外可观测**：`ingest_state()` / `is_ingest_broken()` 供 /health 与
     心跳使用——摄取通道死了，健康检查必须跟着变红，让平台重启实例。

设计要点（不变部分）
------------------------------------------------------------------
1. 复用既有 update_queue / telegram_worker：轮询器只负责把 update 投进
   队列，业务链路（去重、chat lock、AI 派发）完全不动。
2. offset 严格按 "max(update_id)+1" 推进，且**只有成功入队后才推进**，
   与 webhook 的"至少一次"语义一致，不丢消息。
3. 队列满时不丢弃、不推进 offset：等待队列腾出空间后重投，天然背压。
4. 网络错误指数退避（1s→32s 封顶），Telegram 409/401 等致命错误单独提示。
5. 启动前先 deleteWebhook：Telegram 不允许 webhook 与 getUpdates 并存
   （否则 getUpdates 恒返回 409 Conflict）。
"""
import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

from config import (
    BASE_URL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_POLL_LIMIT,
    TELEGRAM_POLL_TIMEOUT,
    DROP_PENDING_ON_STARTUP,
)
from utils import get_http_session

logger = logging.getLogger(__name__)

# 与 webhook_sync.ALLOWED_UPDATES 保持一致：只消费真正会处理的两类。
ALLOWED_UPDATES = ["message", "callback_query"]

# 网络抖动退避区间（秒）
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 32.0

# 主管巡检间隔（秒）：既是子任务存活检查周期，也是停摆判定的采样粒度。
_SUPERVISOR_INTERVAL = 10.0

# 停摆判定阈值（秒）：一次长轮询最长 TELEGRAM_POLL_TIMEOUT+15s（客户端
# 超时余量），连续 3 个周期没有**任何一次成功返回**（空轮询也算成功）就
# 认为链路异常。正常空闲时每 ≤25s 就会成功一次，不会误判。
STALL_SECONDS = float(
    os.getenv("TELEGRAM_POLL_STALL_SECONDS", str((TELEGRAM_POLL_TIMEOUT + 15) * 3))
)

# ---------------------------------------------------------------------------
# 摄取通道运行期状态（供 /health、心跳、/webhookinfo 读取）
# ---------------------------------------------------------------------------
# _started：本进程是否真的启动过摄取通道。webhook 模式与测试进程为 False，
#           此时 is_ingest_broken() 恒为 False——健康检查只对"本该运行却
#           没在跑"的情况变红，不误伤未启用轮询的部署。
_started = False
# _shutting_down：应用正在关停（after_serving 置位）。
_shutting_down = False
# _stop_requested：本次取消是"有意为之"（关停或主管发起的重启）。轮询循环
#                  据此区分「该退出」与「被误取消，必须自愈」。
_stop_requested = False
_last_success_monotonic: Optional[float] = None
_poll_task: Optional[asyncio.Task] = None
_restart_count = 0


def mark_shutdown() -> None:
    """关停开始：让轮询循环把随后的 CancelledError 当作真正的退出信号。"""
    global _shutting_down, _stop_requested
    _shutting_down = True
    _stop_requested = True


def _note_success() -> None:
    global _last_success_monotonic
    _last_success_monotonic = time.monotonic()


def _seconds_since_success() -> Optional[float]:
    if _last_success_monotonic is None:
        return None
    return time.monotonic() - _last_success_monotonic


def ingest_state() -> dict:
    """摄取通道快照（纯内存读取，无 IO，可在健康检查热路径调用）。"""
    age = _seconds_since_success()
    alive = bool(_poll_task is not None and not _poll_task.done())
    stalled = bool(_started and not _shutting_down and age is not None and age > STALL_SECONDS)
    return {
        "started": _started,
        "shutting_down": _shutting_down,
        "alive": alive,
        "stalled": stalled,
        "last_success_age": None if age is None else round(age, 1),
        "restarts": _restart_count,
    }


def is_ingest_broken() -> bool:
    """摄取通道是否处于"本该在收消息却收不到"的状态。

    /health 用它决定是否返回 503：Render 健康检查连续失败会重建实例，
    把"进程活着但永久失聪"从需要人肉发现的故障降级为自动恢复。

    仅在**确实启动过**轮询且**不在关停中**时才可能为真——webhook 模式、
    单测进程不受影响。
    """
    if not _started or _shutting_down:
        return False
    state = ingest_state()
    return (not state["alive"]) or bool(state["stalled"])


async def delete_webhook(*, drop_pending: bool = False, timeout: float = 15.0) -> bool:
    """注销 webhook，把投递权交还 getUpdates。

    Telegram 侧 webhook 与 getUpdates 互斥：webhook 还在注册状态时
    getUpdates 会恒定返回 409 Conflict。轮询模式启动前必须先调用本函数。

    drop_pending=True 时一并丢弃 Telegram 侧积压队列——**默认 False**，
    这样停机窗口内积压的消息（包括当初把队列堵死的那条）会被正常拉取，
    不再永久丢失。
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("deleteWebhook 跳过：未配置 TELEGRAM_BOT_TOKEN")
        return False
    try:
        session = await get_http_session()
        async with session.post(
            f"{BASE_URL}/deleteWebhook",
            json={"drop_pending_updates": bool(drop_pending)},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            payload = await resp.json(content_type=None)
        if payload.get("ok"):
            logger.info(
                "✅ deleteWebhook 成功（切换到 getUpdates 长轮询）, drop_pending=%s",
                drop_pending,
            )
            return True
        logger.error("❌ deleteWebhook 失败: %s", payload)
        return False
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("❌ deleteWebhook 请求异常", exc_info=True)
        return False


async def _fetch_updates(offset: Optional[int]) -> Optional[list]:
    """执行一次 getUpdates。

    返回 update 列表；网络/协议异常返回 None（调用方据此退避重试）。

    ⚠️ 本函数**自己兑现这个契约**：旧版只在文档里写"异常返回 None"，实际
    没有任何 try/except，网络抖动一律以异常形态逸出到主循环——主循环再按
    异常类型做判断，CancelledError 分支就此变成失聪的单点。现在网络层异常
    在这里就地收敛成 None，只有真正的取消信号才继续向上传播。

    注意与 webhook 的关键差异：update 内容在**响应体**里，不经过
    Cloudflare 入站请求体的 WAF 检查——这正是本次修复的核心。
    """
    body: dict = {
        "timeout": TELEGRAM_POLL_TIMEOUT,
        "limit": TELEGRAM_POLL_LIMIT,
        "allowed_updates": ALLOWED_UPDATES,
    }
    if offset is not None:
        body["offset"] = offset

    try:
        session = await get_http_session()
        # 服务端挂起 TELEGRAM_POLL_TIMEOUT 秒，客户端留 15s 余量避免自己先超时。
        async with session.post(
            f"{BASE_URL}/getUpdates",
            json=body,
            timeout=aiohttp.ClientTimeout(total=TELEGRAM_POLL_TIMEOUT + 15),
        ) as resp:
            payload = await resp.json(content_type=None)
    except asyncio.CancelledError:
        # 取消语义必须原样上抛，由主循环结合 _stop_requested 判定是关停
        # 还是误取消（自愈）。
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("getUpdates 网络异常（%s: %s），退避重试", type(e).__name__, e or repr(e))
        return None
    except Exception:
        logger.exception("getUpdates 未预期异常，退避重试")
        return None

    if not isinstance(payload, dict):
        logger.warning("getUpdates 返回了非 JSON 对象响应（type=%s）", type(payload).__name__)
        return None

    if not payload.get("ok"):
        desc = str(payload.get("description") or "")
        code = payload.get("error_code")
        if code == 409:
            # webhook 仍处于注册状态 → 自愈：注销后下一轮即可正常拉取。
            logger.error(
                "getUpdates 409 Conflict：webhook 仍在注册状态，正在自动注销…（%s）", desc
            )
            await delete_webhook(drop_pending=False)
        elif code == 401:
            logger.critical("getUpdates 401 Unauthorized：TELEGRAM_BOT_TOKEN 无效（%s）", desc)
        else:
            logger.warning("getUpdates 响应异常: %s", payload)
        return None

    # 成功拿到一次应答（哪怕是空列表）——链路活着，刷新停摆判定基准。
    _note_success()
    return payload.get("result") or []


async def poll_updates_forever(queue: asyncio.Queue) -> None:
    """长轮询主循环：把 update 投进既有 update_queue，业务链路完全复用。

    offset 语义（与 webhook 的"至少一次"对齐）：
      · 只有 update **成功入队**后才把 offset 推进到 update_id+1；
      · 队列满时原地等待，不推进 offset、不丢弃——Telegram 会在下一轮
        重新返回这批 update，形成天然背压；
      · 进程崩溃时未确认的 update 会被 Telegram 重新拉取。worker 侧
        去重集合是进程内存态，重启后清零——因此崩溃恢复窗口内同一
        update 可能被完整处理两次（at-least-once 语义，重复回复概率
        低但存在；跨重启精确一次需要把去重集合持久化）。

    取消语义（2026-09-15 失聪事故修复点）：
      · `_stop_requested`（关停 / 主管重启）→ 正常退出；
      · 其它来源的 CancelledError（典型：aiohttp 超时取消逸出）→ 记
        CRITICAL、uncancel、退避后**继续循环**。这个循环死掉等于整个 bot
        收不到任何消息，绝不能因为一次误取消就永久退出。
    """
    logger.info(
        "telegram polling started (timeout=%ss limit=%s allowed_updates=%s stall_threshold=%.0fs)",
        TELEGRAM_POLL_TIMEOUT,
        TELEGRAM_POLL_LIMIT,
        ALLOWED_UPDATES,
        STALL_SECONDS,
    )
    offset: Optional[int] = None
    backoff = _BACKOFF_MIN

    while True:
        try:
            updates = await _fetch_updates(offset)

            if updates is None:
                # 协议层错误：退避后重试，避免打爆 Telegram / 刷屏日志。
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue

            # 成功一次就重置退避
            backoff = _BACKOFF_MIN

            if not updates:
                # 长轮询正常超时（该周期内无新消息），立即发起下一轮。
                continue

            logger.info("telegram polling fetched %d update(s)", len(updates))

            for update in updates:
                uid = update.get("update_id")
                if uid is None:
                    logger.warning("polling 收到缺少 update_id 的 payload，已跳过")
                    continue
                # 队列满时 await 阻塞在这里：不丢消息，也不推进 offset。
                await queue.put(update)
                # 入队成功才确认这条：offset 单调递增到 uid+1。
                offset = uid + 1
                logger.info("telegram polling queued update_id=%s", uid)

        except asyncio.CancelledError:
            if _stop_requested:
                logger.warning("telegram polling cancelled (shutdown/restart)")
                raise
            # ⚠️ 历史事故点：这里旧版无条件 raise，一次误取消就让 bot 永久
            # 失聪（进程活着、/health 200、日志只剩心跳）。取消信号不是只有
            # 关停才会来：aiohttp 的 ClientTimeout 本身就是靠取消实现的。
            _uncancel_self()
            logger.critical(
                "🚨 telegram polling 收到非关停来源的取消信号，已吸收并继续轮询"
                "（%.1fs 后重试）——若频繁出现，检查 aiohttp 超时与上层 task.cancel()",
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
        except Exception:
            # 任何未预期异常都只退避重试，轮询循环绝不能死掉——它死了
            # 整个 bot 就彻底收不到消息（等价于旧版 webhook 被堵死）。
            logger.exception("telegram polling loop error（%.1fs 后重试）", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


def _uncancel_self() -> None:
    """清掉当前任务的"正在取消"标记（Python 3.11+）。

    吞掉 CancelledError 却不 uncancel，任务会停留在 cancelling 状态，
    3.11+ 下后续 await 可能被再次打断。没有该 API 的旧版本直接跳过。
    """
    try:
        task = asyncio.current_task()
        uncancel = getattr(task, "uncancel", None)
        if uncancel is not None:
            uncancel()
    except Exception:
        logger.debug("uncancel 当前任务失败（忽略）", exc_info=True)


async def _reap(task: asyncio.Task) -> None:
    """取消并等待子任务收尾，吞掉取消异常。"""
    if task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:
        logger.debug("轮询子任务收尾异常（忽略）", exc_info=True)


async def _supervise(queue: asyncio.Queue) -> None:
    """轮询主管：保证"摄取通道一直在跑"，并在停摆时强制重启。

    自愈两条腿（缺一不可）：
      · 子任务以任何方式结束（异常 / 被外部取消 / 意外 return）→ 按退避
        重新拉起，而不是像旧版那样再也没人管；
      · 子任务还活着但 STALL_SECONDS 内一次成功的 getUpdates 都没有（请求
        挂死、连接池饿死等）→ 主动取消重启。

    主管自身被取消（关停）时负责把子任务一并收尾，不留悬挂协程。
    """
    global _poll_task, _restart_count, _stop_requested
    backoff = _BACKOFF_MIN
    first = True

    while not _shutting_down:
        if not first:
            _restart_count += 1
            logger.critical(
                "🚨 telegram polling 通道中断，%.1fs 后第 %d 次重启摄取通道",
                backoff, _restart_count,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
            if _shutting_down:
                break
        first = False

        _stop_requested = False
        _note_success()  # 新子任务的停摆基准从此刻算起
        task = asyncio.create_task(poll_updates_forever(queue), name="telegram-polling-loop")
        _poll_task = task

        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=_SUPERVISOR_INTERVAL)
                if done:
                    break
                if _shutting_down:
                    break
                age = _seconds_since_success()
                if age is not None and age > STALL_SECONDS:
                    logger.critical(
                        "🚨 telegram polling 停摆：已连续 %.0fs 没有一次成功的 getUpdates"
                        "（阈值 %.0fs），强制重启摄取通道",
                        age, STALL_SECONDS,
                    )
                    _stop_requested = True
                    await _reap(task)
                    break
        except asyncio.CancelledError:
            # 主管被取消 = 应用关停：子任务必须一起收尾，否则悬挂在 loop 上。
            _stop_requested = True
            await _reap(task)
            raise

        if _shutting_down:
            _stop_requested = True
            await _reap(task)
            break

        # 走到这里说明子任务已结束：记录原因供事后定位。
        if task.cancelled():
            logger.error("telegram polling 子任务被取消而结束")
        else:
            exc = task.exception()
            if exc is not None:
                logger.error("telegram polling 子任务异常退出: %r", exc)
            else:
                logger.error("telegram polling 子任务意外正常返回（不应发生）")

    logger.info("telegram polling supervisor exited (shutting_down=%s)", _shutting_down)


async def start_polling(queue: asyncio.Queue) -> asyncio.Task:
    """注销 webhook 并启动长轮询主管任务（供 app 启动钩子调用）。

    返回**主管**任务句柄：调用方 cancel 它即可连带停掉轮询子任务。
    """
    global _started, _shutting_down, _stop_requested, _restart_count
    _shutting_down = False
    _stop_requested = False
    _restart_count = 0
    await delete_webhook(drop_pending=DROP_PENDING_ON_STARTUP)
    _started = True
    _note_success()
    return asyncio.create_task(_supervise(queue), name="telegram-polling-supervisor")
