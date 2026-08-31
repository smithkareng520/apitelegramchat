"""Human-in-the-loop interaction for the agent: the ``message_user`` tool.

（工具原名 ask_user，现改名为 message_user——意图扩展为"向用户发消息
并等待回复"：带选项时是提问卡，不带选项时是纯通知/主动消息。）

The agent can pause on a ``message_user`` tool call while the Telegram draft
keeps streaming. A persistent message with an InlineKeyboard collects the
answer; the resolved value is returned to the original tool call and the same
agent loop continues.

双用途语义：

- 提问（带 options）：发按钮卡等待用户点选；
- 给用户发消息（不带 options）：像现实中给同学发一条消息——发送后
  等待用户自由回复；用户在下一条非命令文本里的任何回复都会作为 custom
  答案回填工具，原轮次继续（"用户回复了就是正常"）；
- 超时（默认 2 分钟，ASK_USER_TIMEOUT 可配）：返回 {"type": "expired"}
  ——含义是"用户当前不在"，不是错误，就像发消息等了两分钟没人回。
  模型据此结束回合即可，用户回来后的下一次交互会重新建立对话。
  发消息模式超时后，已发送的消息卡片会被编辑成纯文本正文本身
  （去掉「📨 助手消息」标题与过期提示），安静地留在聊天记录里。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from apitelegramchat.config import BASE_URL
from apitelegramchat.utils import send_rich_html_message, escape_html
from apitelegramchat.token_budget import truncate_to_token_budget

logger = logging.getLogger(__name__)

ASK_USER_QUESTION_TOKEN_BUDGET = 300
ASK_USER_LABEL_TOKEN_BUDGET = 32
ASK_USER_OPTION_DESCRIPTION_TOKEN_BUDGET = 64
ASK_USER_ID_TOKEN_BUDGET = 32
ASK_USER_CUSTOM_ANSWER_TOKEN_BUDGET = 1_000
MAX_OPTIONS = 8
# 修复：24h 超时太长——一个未回答的 message_user 会把 agent 循环挂起整整一天，
# 中间所有事件循环资源（chat lock、内存里的消息、模型 prompt cache 等）都
# 不能释放。默认 2 分钟：像现实中给同学发消息——等两分钟没人回，就是不在；
# 足够用户看到消息并做选择，又不至于让会话僵死。
# 如需更长等待可通过环境变量 ASK_USER_TIMEOUT 覆盖。
INTERACTION_TIMEOUT = int(os.getenv("ASK_USER_TIMEOUT", str(2 * 60)))


@dataclass
class AskUserInteraction:
    id: str
    chat_id: int
    question: str
    options: list[dict[str, str]]
    multiple: bool
    allow_custom: bool
    message_id: int | None = None
    selected_indices: set[int] = field(default_factory=set)
    awaiting_text: bool = False
    created_at: float = field(default_factory=time.time)
    status: str = "waiting"
    future: asyncio.Future | None = None

    def selected_payload(self) -> list[dict[str, str]]:
        return [self.options[i] for i in sorted(self.selected_indices) if 0 <= i < len(self.options)]


_lock = asyncio.Lock()
_pending: dict[str, AskUserInteraction] = {}
_pending_by_chat: dict[int, str] = {}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _option_text(option: dict[str, Any]) -> tuple[str, str]:
    label = str(option.get("label") or option.get("title") or option.get("id") or "选项").strip()
    desc = str(option.get("description") or "").strip()
    return (
        truncate_to_token_budget(label, ASK_USER_LABEL_TOKEN_BUDGET, suffix="…"),
        truncate_to_token_budget(desc, ASK_USER_OPTION_DESCRIPTION_TOKEN_BUDGET, suffix="…"),
    )


def _normalized_options(options: Any) -> list[dict[str, str]]:
    if not isinstance(options, list):
        return []
    out: list[dict[str, str]] = []
    for idx, raw in enumerate(options[:MAX_OPTIONS]):
        if isinstance(raw, str):
            label = truncate_to_token_budget(raw.strip(), ASK_USER_LABEL_TOKEN_BUDGET, suffix="…")
            oid = f"option_{idx + 1}"
            desc = ""
        elif isinstance(raw, dict):
            label, desc = _option_text(raw)
            oid = truncate_to_token_budget(str(raw.get("id") or f"option_{idx + 1}").strip(), ASK_USER_ID_TOKEN_BUDGET, suffix="…")
        else:
            continue
        if not label:
            continue
        out.append({"id": oid or f"option_{idx + 1}", "label": label, "description": desc})
    return out


def _build_keyboard(interaction: AskUserInteraction) -> dict:
    """Build a compact inline keyboard attached to this question only."""
    if interaction.awaiting_text:
        return {
            "inline_keyboard": [[
                {
                    "text": "取消自定义回答",
                    "callback_data": f"ask:{interaction.id}:cancel",
                }
            ]]
        }

    rows: list[list[dict[str, str]]] = []
    option_buttons: list[dict[str, str]] = []
    for idx, option in enumerate(interaction.options):
        prefix = "✅ " if idx in interaction.selected_indices else ""
        option_buttons.append({
            "text": f"{prefix}{option['label']}",
            "callback_data": f"ask:{interaction.id}:o:{idx}",
        })

    # Short choices look substantially better in two columns; long labels stay one per row.
    two_columns = all(len(b["text"]) <= 18 for b in option_buttons) and len(option_buttons) <= 6
    if two_columns:
        for i in range(0, len(option_buttons), 2):
            rows.append(option_buttons[i:i + 2])
    else:
        rows.extend([[button] for button in option_buttons])

    if interaction.multiple:
        rows.append([{
            "text": "✅ 提交选择",
            "callback_data": f"ask:{interaction.id}:submit",
        }])

    if interaction.allow_custom:
        rows.append([{
            "text": "✏️ 自定义回答",
            "callback_data": f"ask:{interaction.id}:custom",
        }])

    rows.append([{
        "text": "取消",
        "callback_data": f"ask:{interaction.id}:cancel",
    }])
    return {"inline_keyboard": rows}


def _question_html(interaction: AskUserInteraction) -> str:
    """构造 message_user 消息卡片 HTML。

    安全修复：question / label / description 均来自 LLM 工具调用参数，
    若不转义，LLM 一旦输出含 ``<script>`` 或 ``<img onerror=...>`` 的
    文本，就会作为原始 HTML 渲染在用户的客户端。所有插值必须经
    escape_html 转义。
    """
    question = escape_html(interaction.question)
    if not interaction.options:
        # 发消息模式（给用户发消息）：无需选择，用户直接回复文本即可。
        # 超时后本卡片会被编辑成只剩纯文本正文（见 wait_for_answer）。
        return (
            f"<p>📨 <b>助手消息</b></p><p>{question}</p>"
            f"<p><i>直接回复文本即可；长时间不回复本消息会自动过期。</i></p>"
        )
    lines = [f"<p>🤔 <b>需要你的确认</b></p><p>{question}</p>"]
    lines.append("<ul>")
    for option in interaction.options:
        label = escape_html(option.get("label", ""))
        desc = option.get("description") or ""
        if desc:
            lines.append(f"<li><b>{label}</b>：{escape_html(desc)}</li>")
        else:
            lines.append(f"<li><b>{label}</b></li>")
    lines.append("</ul>")
    if interaction.multiple:
        lines.append("<i>可多选，完成后点击“提交选择”；也可以直接回复文字。</i>")
    else:
        lines.append("<i>请选择一项，或直接回复文字：</i>")
    return "".join(lines)


def _answer_json(answer: dict[str, Any]) -> str:
    return json.dumps(answer, ensure_ascii=False, separators=(",", ":"))


async def create_ask_user_interaction(
    chat_id: int,
    question: str,
    options: Any,
    *,
    multiple: bool = False,
    allow_custom: bool = True,
) -> AskUserInteraction:
    """创建一次 message_user 交互。

    - options 非空：提问卡（按钮选择）；
    - options 为空：通知模式——不显示选项按钮，用户任意文本直接作为
      回复回填（awaiting_text 置位）。
    """
    question = truncate_to_token_budget(str(question or "").strip(), ASK_USER_QUESTION_TOKEN_BUDGET, suffix="…")
    if not question:
        raise ValueError("message_user.question 不能为空")
    normalized = _normalized_options(options)

    async with _lock:
        old_id = _pending_by_chat.get(chat_id)
        if old_id and old_id in _pending:
            old = _pending[old_id]
            old.status = "cancelled"
            if old.future and not old.future.done():
                old.future.cancel()
            _pending.pop(old_id, None)

        interaction = AskUserInteraction(
            id=_new_id(),
            chat_id=chat_id,
            question=question,
            options=normalized,
            multiple=bool(multiple) if normalized else False,
            allow_custom=bool(allow_custom),
            # 通知模式：没有选项可点，任何文本回复都作为 custom 答案。
            awaiting_text=not normalized,
        )
        interaction.future = asyncio.get_running_loop().create_future()
        _pending[interaction.id] = interaction
        _pending_by_chat[chat_id] = interaction.id

    message_id = await send_rich_html_message(
        chat_id,
        _question_html(interaction),
        reply_markup=_build_keyboard(interaction),
        reassert_draft=True,
    )
    # send_rich_html_message 在 HTTP 200 但解析不到 message_id 时返回 True；
    # isinstance(True, int) 为真，必须显式排除 bool，否则后续 Telegram API
    # 收到 message_id=true 必然 400，交互卡永远无法收尾。
    if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
        interaction.message_id = message_id
    else:
        await cancel_interaction(interaction.id, remove_ui=False)
        raise RuntimeError("无法发送 message_user 交互消息")
    return interaction


async def get_pending_for_chat(chat_id: int) -> AskUserInteraction | None:
    async with _lock:
        interaction_id = _pending_by_chat.get(chat_id)
        interaction = _pending.get(interaction_id) if interaction_id else None
        return interaction


async def _clear_pending_unlocked(interaction: AskUserInteraction) -> None:
    _pending.pop(interaction.id, None)
    if _pending_by_chat.get(interaction.chat_id) == interaction.id:
        _pending_by_chat.pop(interaction.chat_id, None)


async def _clear_pending(interaction: AskUserInteraction) -> None:
    async with _lock:
        await _clear_pending_unlocked(interaction)


async def _set_markup(message_id: int, chat_id: int, markup: dict | None) -> None:
    if not message_id:
        return
    payload = {"chat_id": chat_id, "message_id": message_id}
    if markup is not None:
        payload["reply_markup"] = markup
    else:
        payload["reply_markup"] = {"inline_keyboard": []}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8, connect=3)) as session:
            async with session.post(f"{BASE_URL}/editMessageReplyMarkup", json=payload) as resp:
                if resp.status != 200:
                    logger.debug("ask_user editMessageReplyMarkup failed: %s %s", resp.status, (await resp.text())[:200])
    except Exception as exc:
        logger.debug("ask_user editMessageReplyMarkup exception: %s", exc)


async def _edit_question_message(interaction: AskUserInteraction, body_html: str) -> None:
    if not interaction.message_id:
        return
    payload = {
        "chat_id": interaction.chat_id,
        "message_id": interaction.message_id,
        "rich_message": {
            "content": body_html,
            "html": body_html,
        },
        "reply_markup": {"inline_keyboard": []},
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8, connect=3)) as session:
            async with session.post(f"{BASE_URL}/editMessageText", json=payload) as resp:
                if resp.status != 200:
                    logger.debug("ask_user editMessageText failed: %s %s", resp.status, (await resp.text())[:200])
    except Exception as exc:
        logger.debug("ask_user editMessageText exception: %s", exc)


def _answered_html(interaction: AskUserInteraction, answer: dict[str, Any]) -> str:
    """构造回答后的问题卡片 HTML。

    安全修复：q 来自 LLM 工具参数，selected 选项的 label 同样来自 LLM，
    custom 的 value 是用户自由文本——都必须 escape，否则任意一方包含
    HTML 字符都会注入到用户客户端的渲染上下文。
    """
    q = escape_html(interaction.question)
    kind = answer.get("type")
    if kind == "choice":
        selected = answer.get("selected") or []
        labels = [str(item.get("label", "")) for item in selected if isinstance(item, dict)]
        chosen_raw = "、".join(x for x in labels if x) or "已选择"
        chosen = escape_html(chosen_raw)
        return f"<p>✅ <b>已收到你的选择</b></p><p>{q}</p><p><b>{chosen}</b></p>"
    if kind == "custom":
        value = truncate_to_token_budget(str(answer.get("value", "")), ASK_USER_CUSTOM_ANSWER_TOKEN_BUDGET, suffix="…")
        return f"<p>✅ <b>已收到你的回答</b></p><p>{q}</p><p><blockquote>{escape_html(value)}</blockquote></p>"
    if kind == "cancelled":
        return f"<p>✖️ <b>已取消</b></p><p>{q}</p>"
    if kind == "expired":
        return f"<p>⌛ <b>用户未回复</b>（可能不在线）</p><p>{q}</p>"
    return f"<p>✅ <b>已收到回答</b></p><p>{q}</p>"


async def resolve_callback(chat_id: int, callback_from_id: int, interaction_id: str, action: str, arg: str = "") -> tuple[bool, str]:
    async with _lock:
        interaction = _pending.get(interaction_id)
        if interaction is None:
            return False, "这个问题已经结束或失效了"
        # 类型校验：Telegram 偶发会传非数值 chat_id（如 channel post），
        # 此前直接 int() 会抛 ValueError 让整个 callback 500。先校验。
        try:
            chat_id_int = int(chat_id) if chat_id is not None else None
            from_id_int = int(callback_from_id) if callback_from_id is not None else None
        except (TypeError, ValueError):
            return False, "无效的 chat_id 或 callback_from_id"
        if chat_id_int is None or from_id_int is None:
            return False, "无效的 chat_id 或 callback_from_id"
        if int(interaction.chat_id) != chat_id_int or from_id_int != chat_id_int:
            return False, "无权限"
        if interaction.status != "waiting":
            return False, "这个问题已经处理过了"

        if action == "o":
            try:
                idx = int(arg)
            except (TypeError, ValueError):
                return False, "无效选项"
            if idx < 0 or idx >= len(interaction.options):
                return False, "无效选项"
            if interaction.multiple:
                if idx in interaction.selected_indices:
                    interaction.selected_indices.remove(idx)
                else:
                    interaction.selected_indices.add(idx)
                markup = _build_keyboard(interaction)
                notice = "已选择" if idx in interaction.selected_indices else "已取消选择"
                message_id = interaction.message_id
            else:
                interaction.status = "answered"
                answer = {
                    "type": "choice",
                    "multiple": False,
                    "selected": [interaction.options[idx]],
                }
                if interaction.future and not interaction.future.done():
                    interaction.future.set_result(answer)
                markup = None
                notice = f"已选择：{interaction.options[idx]['label']}"
                message_id = interaction.message_id
                await _clear_pending_unlocked(interaction)
                asyncio.create_task(_edit_question_message(interaction, _answered_html(interaction, answer)))
                return True, notice
        elif action == "submit":
            if not interaction.multiple:
                return False, "当前问题无需提交"
            if not interaction.selected_indices:
                return False, "请至少选择一个选项"
            interaction.status = "answered"
            answer = {
                "type": "choice",
                "multiple": True,
                "selected": interaction.selected_payload(),
            }
            if interaction.future and not interaction.future.done():
                interaction.future.set_result(answer)
            markup = None
            notice = "已提交选择"
            message_id = interaction.message_id
            await _clear_pending_unlocked(interaction)
            asyncio.create_task(_edit_question_message(interaction, _answered_html(interaction, answer)))
            return True, notice
        elif action == "custom":
            if not interaction.allow_custom:
                return False, "此问题不支持自定义回答"
            interaction.awaiting_text = True
            interaction.selected_indices.clear()
            markup = _build_keyboard(interaction)
            notice = "请直接发送你的回答"
            message_id = interaction.message_id
        elif action == "cancel":
            interaction.status = "cancelled"
            if interaction.future and not interaction.future.done():
                interaction.future.set_result({"type": "cancelled"})
            message_id = interaction.message_id
            await _clear_pending_unlocked(interaction)
            asyncio.create_task(_edit_question_message(interaction, _answered_html(interaction, {"type": "cancelled"})))
            return True, "已取消"
        else:
            return False, "未知操作"

    asyncio.create_task(_set_markup(message_id, interaction.chat_id, markup))
    return True, notice


async def resolve_text(chat_id: int, text: str) -> bool:
    """把用户的一条自由文本作为当前 message_user 的回复。

    提问卡与通知卡均适用：只要还有等待中的交互，用户直接打字即视为
    回复（"用户回复了就是正常"），不再要求先点“自定义回答”按钮。
    命令（以 / 开头）由上层拦截，不会进入本函数。
    """
    text = str(text or "").strip()
    if not text:
        return False
    async with _lock:
        interaction_id = _pending_by_chat.get(chat_id)
        interaction = _pending.get(interaction_id) if interaction_id else None
        if not interaction or interaction.status != "waiting":
            return False
        interaction.status = "answered"
        answer = {"type": "custom", "value": truncate_to_token_budget(text, ASK_USER_CUSTOM_ANSWER_TOKEN_BUDGET, suffix="…")}
        if interaction.future and not interaction.future.done():
            interaction.future.set_result(answer)
        await _clear_pending_unlocked(interaction)
    asyncio.create_task(_edit_question_message(interaction, _answered_html(interaction, answer)))
    return True


async def wait_for_answer(interaction: AskUserInteraction) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(interaction.future, timeout=INTERACTION_TIMEOUT)
    except asyncio.TimeoutError:
        interaction.status = "expired"
        if interaction.future and not interaction.future.done():
            interaction.future.set_result({"type": "expired"})
        if interaction.options:
            # 提问卡超时：显示「用户未回复」状态卡。
            await _edit_question_message(interaction, _answered_html(interaction, {"type": "expired"}))
        else:
            # 发消息模式超时：把消息编辑成纯文本正文本身——去掉
            # 「📨 助手消息」标题与「会自动过期」提示，也不显示
            # 「用户未回复」状态。就像现实中给同学发消息：等了两分钟
            # 没人回，消息本身安静地留在聊天记录里就够了。
            await _edit_question_message(
                interaction,
                f"<p>{escape_html(interaction.question)}</p>",
            )
        await _clear_pending(interaction)
        return {"type": "expired"}
    except asyncio.CancelledError:
        interaction.status = "cancelled"
        if interaction.future and not interaction.future.done():
            interaction.future.cancel()
        await _edit_question_message(interaction, _answered_html(interaction, {"type": "cancelled"}))
        await _clear_pending(interaction)
        raise


async def cancel_interaction(interaction_id: str, remove_ui: bool = True) -> None:
    async with _lock:
        interaction = _pending.get(interaction_id)
        if not interaction:
            return
        interaction.status = "cancelled"
        if interaction.future and not interaction.future.done():
            interaction.future.cancel()
        message_id = interaction.message_id
        chat_id = interaction.chat_id
        await _clear_pending_unlocked(interaction)
    if remove_ui:
        await _set_markup(message_id, chat_id, None)


def answer_to_tool_result(answer: dict[str, Any]) -> str:
    """把交互结果转成 message_user 工具的 tool 消息内容。"""
    result = dict(answer or {})
    result.setdefault("type", "unknown")
    if result.get("type") == "expired":
        # "用户不在"语义：明确告诉模型这不是错误，可以结束回合，
        # 也可以继续做不需要用户参与的事。
        result["note"] = "用户在超时时间内没有回复（用户可能不在）。这不是错误；可结束本回合，用户回来后会再联系。"
    return _answer_json(result)
