"""Human-in-the-loop interaction for the agent.

The agent can pause on an ask_user tool call while the Telegram draft keeps
streaming. A persistent message with an InlineKeyboard collects the answer;
the resolved value is returned to the original tool call and the same agent
loop continues.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from apitelegramchat.config import BASE_URL
from apitelegramchat.utils import send_rich_html_message

logger = logging.getLogger("apitelegramchat.ask_user")

MAX_QUESTION_CHARS = 1200
MAX_OPTIONS = 8
MAX_LABEL_CHARS = 48
MAX_OPTION_DESC_CHARS = 180
INTERACTION_TIMEOUT = 24 * 60 * 60


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
    return label[:MAX_LABEL_CHARS], desc[:MAX_OPTION_DESC_CHARS]


def _normalized_options(options: Any) -> list[dict[str, str]]:
    if not isinstance(options, list):
        return []
    out: list[dict[str, str]] = []
    for idx, raw in enumerate(options[:MAX_OPTIONS]):
        if isinstance(raw, str):
            label = raw.strip()[:MAX_LABEL_CHARS]
            oid = f"option_{idx + 1}"
            desc = ""
        elif isinstance(raw, dict):
            label, desc = _option_text(raw)
            oid = str(raw.get("id") or f"option_{idx + 1}").strip()[:80]
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
                    "style": "danger",
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
            "style": "primary",
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
            "style": "success",
            "callback_data": f"ask:{interaction.id}:submit",
        }])

    if interaction.allow_custom:
        rows.append([{
            "text": "✏️ 自定义回答",
            "callback_data": f"ask:{interaction.id}:custom",
        }])

    rows.append([{
        "text": "取消",
        "style": "danger",
        "callback_data": f"ask:{interaction.id}:cancel",
    }])
    return {"inline_keyboard": rows}


def _question_html(interaction: AskUserInteraction) -> str:
    question = interaction.question
    lines = [f"<p>🤔 <b>需要你的确认</b></p><p>{question}</p>"]
    if interaction.options:
        lines.append("<ul>")
        for option in interaction.options:
            label = option["label"]
            desc = option.get("description") or ""
            if desc:
                lines.append(f"<li><b>{label}</b>：{desc}</li>")
            else:
                lines.append(f"<li><b>{label}</b></li>")
        lines.append("</ul>")
    if interaction.multiple:
        lines.append("<i>可多选，完成后点击“提交选择”。</i>")
    else:
        lines.append("<i>请选择一项：</i>")
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
    question = str(question or "").strip()[:MAX_QUESTION_CHARS]
    normalized = _normalized_options(options)
    if not question:
        raise ValueError("ask_user.question 不能为空")
    if not normalized:
        raise ValueError("ask_user.options 至少需要一个有效选项")

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
            multiple=bool(multiple),
            allow_custom=bool(allow_custom),
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
    if isinstance(message_id, int) and message_id > 0:
        interaction.message_id = message_id
    else:
        await cancel_interaction(interaction.id, remove_ui=False)
        raise RuntimeError("无法发送 ask_user 交互消息")
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
    q = interaction.question
    kind = answer.get("type")
    if kind == "choice":
        selected = answer.get("selected") or []
        labels = [str(item.get("label", "")) for item in selected if isinstance(item, dict)]
        chosen = "、".join(x for x in labels if x) or "已选择"
        return f"<p>✅ <b>已收到你的选择</b></p><p>{q}</p><p><b>{chosen}</b></p>"
    if kind == "custom":
        value = str(answer.get("value", ""))[:4000]
        return f"<p>✅ <b>已收到你的回答</b></p><p>{q}</p><p><blockquote>{value}</blockquote></p>"
    if kind == "cancelled":
        return f"<p>✖️ <b>已取消</b></p><p>{q}</p>"
    if kind == "expired":
        return f"<p>⌛ <b>问题已过期</b></p><p>{q}</p>"
    return f"<p>✅ <b>已收到回答</b></p><p>{q}</p>"


async def resolve_callback(chat_id: int, callback_from_id: int, interaction_id: str, action: str, arg: str = "") -> tuple[bool, str]:
    async with _lock:
        interaction = _pending.get(interaction_id)
        if interaction is None:
            return False, "这个问题已经结束或失效了"
        if int(interaction.chat_id) != int(chat_id) or int(callback_from_id) != int(chat_id):
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
    text = str(text or "").strip()
    if not text:
        return False
    async with _lock:
        interaction_id = _pending_by_chat.get(chat_id)
        interaction = _pending.get(interaction_id) if interaction_id else None
        if not interaction or interaction.status != "waiting" or not interaction.awaiting_text:
            return False
        interaction.status = "answered"
        answer = {"type": "custom", "value": text[:4000]}
        if interaction.future and not interaction.future.done():
            interaction.future.set_result(answer)
        message_id = interaction.message_id
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
        await _edit_question_message(interaction, _answered_html(interaction, {"type": "expired"}))
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
    result = dict(answer or {})
    result.setdefault("type", "unknown")
    return _answer_json(result)
