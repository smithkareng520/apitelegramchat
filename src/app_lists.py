"""role/model 列表 UI：就地编辑打 √ 与 sendMessage 发送（自 app.py 拆出）。"""
import asyncio
import json

import aiohttp

from config import BASE_URL
from state import get_user_model
from utils import delete_message, _notify_chat_unreachable, get_logger
from app_turns import _reply_params


logger = get_logger(__name__)


def _marked_keyboard(items: list, current: str | None) -> dict:
    """构造单选 inline keyboard：当前项打 √（role/model 列表共用）。"""
    formatted = [f"{x} √" if x == current else x for x in items]
    return {"inline_keyboard": [[{"text": t, "callback_data": v}] for t, v in zip(formatted, items)]}


async def update_role_list(chat_id: int, message_id: int, role_list: list, current_role: str | None) -> bool:
    keyboard = _marked_keyboard(role_list, current_role)
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
    keyboard = _marked_keyboard(model_list, current_model)
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


async def _del_after(chat_id: int, msg_id: int, delay: float) -> None:
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

    指令响应必须走 sendMessage：在 AI 生成过程中发送 /model、/role、
    /balance 等指令时，若改走 sendRichMessage，Telegram 客户端会把这条
    永久消息画在当前 draft 预览的视觉位（草稿被"转正"/挤开），紧接着
    serialize_with_active_draft 里的 _reassert_active_draft_content
    又用同一个 draft_id 推了一帧 sendRichMessageDraft——但草稿已被
    sendRichMessage 消费掉，于是 Telegram 把它当成一个全新的草稿，
    画在永久消息下方。AI 的 flush 循环随后继续刷新这个新草稿。

    用户看到的错乱会是：
      1) 列表占了草稿位（列表出现在草稿原来的位置，而不是指令下方）
      2) 草稿在指令下方重新刷新（reassert + flush 循环创建的新草稿）

    原理：Telegram Bot API 文档明确指出"once the output is finalized,
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
                    # 403 类永久性失败（用户屏蔽 bot 等）：与 sendRichMessage
                    # 路径同款熔断，停掉该 chat 的主动唤醒调度。
                    if await _notify_chat_unreachable(chat_id, resp.status, body):
                        return 0
                    logger.error(
                        f"_send_via_send_message failed: {resp.status} {body[:200]}"
                    )
    except Exception as e:
        logger.exception(f"_send_via_send_message exception: {e}")
    return 0


async def send_role_list(chat_id: int, role_list: list, current_role: str | None, reply_message_id: int | None = None) -> int:
    keyboard = _marked_keyboard(role_list, current_role)
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
    keyboard = _marked_keyboard(model_list, current_model)
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
