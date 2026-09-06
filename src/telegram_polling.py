# telegram_polling.py
"""Telegram getUpdates 长轮询摄取通道（webhook 的等价替代）。

为什么需要这个模块（事故根因）
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

这也精确解释了报错文档里的对照矩阵：
  · `>curl -v ""` / `> curl -v ""` / 行内代码包裹 → 全部命中同一条规则；
  · 改成 `<curl`、去掉 `>`、换 `$`/`#` 前导符 → 不再匹配"重定向符+curl"；
  · `--verbose` 全拼 → 规则只认短参数形态；
  · 单独一个 `>` → 没有命令名，不构成注入特征。
webhook.site 能收到，只是因为它前面没有这套 WAF。

修复思路
------------------------------------------------------------------
不去和 WAF 规则搏斗（Render 上也改不了），而是**换一条数据流方向**：
改用 getUpdates 长轮询后，update 内容位于我们发起的 HTTPS 请求的
**响应体**里。Cloudflare 的入站请求体检查对出站响应不生效，任何文本
都能安全抵达，从根上消除这类误杀。

设计要点
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
    except Exception:
        logger.error("❌ deleteWebhook 请求异常", exc_info=True)
        return False


async def _fetch_updates(offset: Optional[int]) -> Optional[list]:
    """执行一次 getUpdates。

    返回 update 列表；网络/协议异常返回 None（调用方据此退避重试）。
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

    session = await get_http_session()
    # 服务端挂起 TELEGRAM_POLL_TIMEOUT 秒，客户端留 15s 余量避免自己先超时。
    async with session.post(
        f"{BASE_URL}/getUpdates",
        json=body,
        timeout=aiohttp.ClientTimeout(total=TELEGRAM_POLL_TIMEOUT + 15),
    ) as resp:
        payload = await resp.json(content_type=None)

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

    return payload.get("result") or []


async def poll_updates_forever(queue: asyncio.Queue) -> None:
    """长轮询主循环：把 update 投进既有 update_queue，业务链路完全复用。

    offset 语义（与 webhook 的"至少一次"对齐）：
      · 只有 update **成功入队**后才把 offset 推进到 update_id+1；
      · 队列满时原地等待，不推进 offset、不丢弃——Telegram 会在下一轮
        重新返回这批 update，形成天然背压；
      · 进程崩溃时未确认的 update 会被重新拉取，由 worker 侧去重兜底。
    """
    logger.info(
        "telegram polling started (timeout=%ss limit=%s allowed_updates=%s)",
        TELEGRAM_POLL_TIMEOUT,
        TELEGRAM_POLL_LIMIT,
        ALLOWED_UPDATES,
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
            logger.warning("telegram polling cancelled (shutdown)")
            raise
        except Exception:
            # 任何未预期异常都只退避重试，轮询循环绝不能死掉——它死了
            # 整个 bot 就彻底收不到消息（等价于旧版 webhook 被堵死）。
            logger.exception("telegram polling loop error（%.1fs 后重试）", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


async def start_polling(queue: asyncio.Queue) -> asyncio.Task:
    """注销 webhook 并启动长轮询任务（供 app 启动钩子调用）。"""
    await delete_webhook(drop_pending=DROP_PENDING_ON_STARTUP)
    return asyncio.create_task(poll_updates_forever(queue), name="telegram-polling")
