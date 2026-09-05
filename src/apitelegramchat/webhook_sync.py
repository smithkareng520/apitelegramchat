# webhook_sync.py
"""Telegram Webhook 启动自愈注册与投递链路观测。

背景（为什么需要这个模块）：
- Telegram 的 webhook 是"至少一次、需确认"的投递模型：update 只有在
  服务端以 2xx 签收后才会离开 Telegram 侧的队列；部署窗口/停机/非 2xx
  （如 403 token 不匹配）导致的投递失败会留在队列里按指数退避重放——
  这就是"积压消息"的来源。
- setWebhook 只修改"未来的投递路由"，**不影响**存量队列：重启、重注册
  从来不是清积压的手段。唯一清队手段是 drop_pending_updates=true
  （setWebhook / deleteWebhook 都接受该参数）。

本模块的职责：
1. 启动自愈注册：用 WEBHOOK_URL?token=WEBHOOK_TOKEN 幂等调用 setWebhook，
   保证注册信息与环境变量恒等（手工注册残留的旧 token 会被替换，避免
   "?token=旧值 vs WEBHOOK_TOKEN 新值"不一致造成的全体 403 → 无限积压）；
   可选附带 drop_pending_updates=true（DROP_PENDING_ON_STARTUP 开关）。
2. 观测：拉取 getWebhookInfo，把 pending_update_count、last_error_* 打进
   日志——积压从"用户感知"变成"启动即可见"。同份数据由 /webhookinfo
   管理员命令在聊天里随时查看（见 app.py）。

设计约束：
- 任何失败都只降级（沿用上一次注册继续运行），绝不抛出、绝不阻塞启动。
- 日志/回显里的注册 URL 一律先过 mask_webhook_url() 脱敏 token。
- 敏感值一律从 config 模块属性取（config.scrub_environment() 会在导入期
  把 WEBHOOK_TOKEN / WEBHOOK_URL 从 os.environ 抹掉，不能再读环境变量）。
"""
import re
import time
import asyncio
import logging
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import aiohttp

from apitelegramchat.config import (
    BASE_URL,
    TELEGRAM_BOT_TOKEN,
    WEBHOOK_TOKEN,
    _RAW_WEBHOOK_URL,
    DROP_PENDING_ON_STARTUP,
)
from apitelegramchat.utils import get_http_session

logger = logging.getLogger(__name__)

# Telegram secret_token 官方字符集：1-256 个 A-Za-z0-9_-。
# WEBHOOK_TOKEN 满足该字符集时，注册才会附带 secret_token 参数；
# 不满足时静默跳过（保持 query token 鉴权），绝不能让整次注册失败。
SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

# app.webhook 只消费 message 与 callback_query 两类 update（见 app.py）。
# 显式声明 allowed_updates 可以让 Telegram 不投递 edited_message/channel_post
# 等无人消费的类型，减少无谓的投递与重试。
ALLOWED_UPDATES = ["message", "callback_query"]


def build_webhook_full_url(raw_url: str, token: str) -> str:
    """把 WEBHOOK_URL 和 WEBHOOK_TOKEN 拼成带鉴权 query 的完整注册 URL。

    规则：
    - query 里已有 token= 时**替换**为环境变量值——保证注册 URL 与
      WEBHOOK_TOKEN 恒等（修掉手工注册时写死的旧 token）；
    - 无 token 时按是否已有其他 query 参数选择 ? 或 & 追加；
    - token 经 urlencode 转义，含特殊字符也安全；
    - raw_url 为空时返回空串（调用方据此跳过注册）。
    """
    if not raw_url:
        return ""
    parts = urlsplit(raw_url.strip())
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != "token"
    ]
    if token:
        query_pairs.append(("token", token))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment)
    )


def mask_webhook_url(full_url: str) -> str:
    """日志/聊天回显用：把 query 里 token 的值整体替换为 ***。

    注册 URL 内嵌了 WEBHOOK_TOKEN，属于凭据；任何落日志、发进聊天的
    URL 都必须先经过这里。
    """
    if not full_url:
        return ""
    try:
        parts = urlsplit(full_url)
        query_pairs = [
            (k, "***" if k == "token" else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        # safe="*" 让打码用的 * 保持原样，回显更可读（token 本体不会出现在这里）。
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query_pairs, safe="*"), parts.fragment)
        )
    except Exception:
        # 理论上 urlsplit 对任何字符串都不抛；防御性兜底：整串打码。
        return "***"


def secret_token_for_registration() -> Optional[str]:
    """WEBHOOK_TOKEN 满足 Telegram secret_token 字符集时返回它，否则 None。

    setWebhook 的 secret_token 让 Telegram 在每次投递时附带
    X-Telegram-Bot-Api-Secret-Token 请求头（1-256 个 A-Za-z0-9_-）。
    app.webhook 的鉴权是"query token 或 secret 头任一匹配即放行"（加法式
    强化），因此无论本函数返回什么，既有投递路径都不受影响。
    """
    if WEBHOOK_TOKEN and SECRET_TOKEN_RE.match(WEBHOOK_TOKEN):
        return WEBHOOK_TOKEN
    return None


def _fmt_last_error(info: dict) -> Optional[str]:
    """从 getWebhookInfo result 里格式化最近一次投递错误（含本地时间）。"""
    msg = info.get("last_error_message")
    if not msg:
        return None
    ts = info.get("last_error_date")
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "未知时间"
    return f"[{when}] {msg}"


async def get_webhook_info(*, timeout: float = 10.0) -> Optional[dict]:
    """调用 getWebhookInfo，返回 result dict；失败返回 None（观测不抛错）。"""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("getWebhookInfo 跳过：未配置 TELEGRAM_BOT_TOKEN")
        return None
    try:
        session = await get_http_session()
        async with session.post(
            f"{BASE_URL}/getWebhookInfo",
            json={},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            payload = await resp.json(content_type=None)
        if not payload.get("ok"):
            logger.warning(f"getWebhookInfo 响应异常: {payload}")
            return None
        return payload.get("result") or {}
    except Exception:
        logger.warning("getWebhookInfo 请求失败", exc_info=True)
        return None


async def sync_webhook_on_startup(*, timeout: float = 15.0) -> Optional[dict]:
    """启动自愈：幂等重注册 webhook + getWebhookInfo 观测日志。

    返回 getWebhookInfo 的 result dict（任一步失败返回 None）。
    本函数保证不抛异常、不在启动路径上长时间阻塞（内部自带超时）——
    注册失败只降级为"沿用上一次注册"，因为注册信息保存在 Telegram 侧，
    不随进程重启消失。

    幂等性：对同一 URL 重复 setWebhook 无副作用，也不会触碰存量积压队列
    （除非显式传 drop_pending_updates=true）。
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Webhook 自愈注册跳过：未配置 TELEGRAM_BOT_TOKEN")
        return None
    if not _RAW_WEBHOOK_URL:
        logger.warning("Webhook 自愈注册跳过：未配置 WEBHOOK_URL")
        return None
    if not WEBHOOK_TOKEN:
        logger.warning("Webhook 自愈注册跳过：未配置 WEBHOOK_TOKEN")
        return None

    full_url = build_webhook_full_url(_RAW_WEBHOOK_URL, WEBHOOK_TOKEN)
    body: dict = {"url": full_url, "allowed_updates": ALLOWED_UPDATES}
    secret = secret_token_for_registration()
    if secret:
        body["secret_token"] = secret
    if DROP_PENDING_ON_STARTUP:
        body["drop_pending_updates"] = True

    try:
        session = await get_http_session()
        async with session.post(
            f"{BASE_URL}/setWebhook",
            json=body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            payload = await resp.json(content_type=None)
        if payload.get("ok"):
            logger.info(
                f"✅ setWebhook 自愈注册成功: url={mask_webhook_url(full_url)}, "
                f"allowed_updates={ALLOWED_UPDATES}, drop_pending={DROP_PENDING_ON_STARTUP}, "
                f"secret_token={'已附带' if secret else '未附带(token字符集不符或未配置)'}"
            )
        else:
            logger.error(
                f"❌ setWebhook 自愈注册失败（沿用上一次注册继续运行）: "
                f"{payload} url={mask_webhook_url(full_url)}"
            )
    except Exception:
        logger.error("❌ setWebhook 自愈注册请求异常（沿用上一次注册继续运行）", exc_info=True)

    info = await get_webhook_info()
    if info:
        pending = info.get("pending_update_count", 0)
        summary = (
            f"📊 getWebhookInfo: pending_update_count={pending}, "
            f"url={mask_webhook_url(info.get('url') or '')}"
        )
        last_err = _fmt_last_error(info)
        if last_err:
            summary += f", last_error={last_err}"
        if pending:
            logger.warning(summary)
            logger.warning(
                "⚠️ Telegram 侧存在 %d 条未签收 update（积压）。应用健康运行并对重放返回 200 "
                "后会自动排干；若希望重启即丢弃，设置 DROP_PENDING_ON_STARTUP=true 后重启。",
                pending,
            )
        else:
            logger.info(summary)
    return info


async def run_sync_with_deadline(deadline: float = 20.0) -> None:
    """带总死线的自愈入口（供启动钩子的 fire-and-forget 任务调用）。

    再包一层 asyncio.wait_for：即便内部实现对 Telegram 的请求意外挂起，
    也不会让这个后台任务无限存活占用资源。
    """
    try:
        await asyncio.wait_for(sync_webhook_on_startup(), timeout=deadline)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("启动 webhook 自愈任务异常（已忽略，不影响服务）", exc_info=True)
