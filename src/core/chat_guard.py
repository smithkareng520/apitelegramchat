"""chat 不可达熔断：403 类永久性发送失败识别与 proactive 通知（自 utils.py 拆出）。"""

from typing import Optional

import logging

logger = logging.getLogger(__name__)


# ---------- chat 不可达熔断（403 类永久性发送失败） ----------
# 用户把 bot 屏蔽/封禁后，Telegram 对该 chat 的所有发送一律返回
# 403 Forbidden（"bot was blocked by the user" / "user is deactivated" /
# "bot was kicked ..."）；"chat not found"（400）则表示 chat 已不存在。
# 这些都是**永久性**错误：重试、降级富文本、换 sendMessage 都救不回来。
# 白名单管不住这种用户（他仍在白名单里），若不熔断，proactive TIMER 会
# 每 5~20min 触发一轮完整 LLM 回合却永远送达不了，无限空转烧 token。
# 识别到这类错误后统一通知 proactive 停用该 chat 的调度；用户解除屏蔽
# 并再次发消息时由 note_user_activity 自动恢复（详见 proactive.py）。
def _permanent_chat_error_reason(status: int, body: str) -> Optional[str]:
    """判断一次 Telegram 发送失败是否为该 chat 的永久性不可达。

    返回人类可读的原因字符串；非永久性错误（429 限流、5xx、网络错误、
    400 内容错误等）返回 None——那些应该走既有的重试/降级/失败计数路径。
    """
    body_lower = (body or "").lower()
    if status == 403:
        # Telegram Bot API 对 chat 定向的 403 一律是权限级永久失败
        #（bot 被屏蔽 / 账号停用 / 被踢出群）。区别于 401（bot token
        # 级认证失败，会影响所有 chat，不能据此熔断单个 chat）。
        return body[:120] or "403 Forbidden"
    if "chat not found" in body_lower:
        return body[:120] or "chat not found"
    return None


async def _notify_chat_unreachable(chat_id: int, status: int, body: str) -> bool:
    """若该失败是永久性不可达，通知 proactive 熔断该 chat 的主动唤醒。

    返回 True 表示已判定为永久性错误（调用方应立即放弃重试/降级路径）。
    惰性导入 proactive 以保持 utils 作为底层 Telegram 助手模块的分层
    （proactive 不反向依赖 utils，无循环导入风险）。
    """
    try:
        reason = _permanent_chat_error_reason(status, body)
        if reason is None:
            return False
        import proactive
        await proactive.notify_chat_unreachable(chat_id, reason=reason)
        return True
    except Exception:
        logger.debug("notify_chat_unreachable 失败（可忽略）", exc_info=True)
        return False
