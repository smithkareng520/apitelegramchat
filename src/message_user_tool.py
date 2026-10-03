"""Human-in-the-loop interaction for the agent.

message_user 有两种模式：
1. 普通消息：只发送一条消息并等待用户下一条普通回复；回复会原子地交给
   原 message_user tool，不会创建新的 agent turn，也不会打断正在等待的轮次。
2. 交互表单：一个消息里包含多个问题；每题可以单选/多选，可选自定义输入，
   用户可用上一题/下一题切换，最后进入 Review / Submit 页面。允许只提交已
   回答的问题，未回答的问题不会出现在最终结果里。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Coroutine

import aiohttp

from config import BASE_URL
from utils import send_rich_html_message
from markdown_converter import render_telegram_fragment as convert_markdown_to_telegram_html, render_telegram_block
from core.rich_media import _rich_message_html_payload
from token_budget import truncate_to_token_budget

logger = logging.getLogger(__name__)

ASK_USER_QUESTION_TOKEN_BUDGET = 300
ASK_USER_LABEL_TOKEN_BUDGET = 32
ASK_USER_OPTION_DESCRIPTION_TOKEN_BUDGET = 64
ASK_USER_ID_TOKEN_BUDGET = 32
ASK_USER_CUSTOM_ANSWER_TOKEN_BUDGET = 1_000
MAX_OPTIONS = 8
MAX_QUESTIONS = 8
INTERACTION_TIMEOUT = int(os.getenv("ASK_USER_TIMEOUT", str(2 * 60)))

MESSAGE_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "message_user",
        "description": (
            "Send a normal message to the user, or collect answers with one mixed question form. "
            "Normal message mode uses `message` and waits for the user's next ordinary text reply. "
            "Form mode uses `questions`: each question can independently be single-select "
            "(`multiSelect: false`) or multi-select (`multiSelect: true`), every option may have "
            "a description, and a question may allow custom text input. Questions can be mixed. "
            "The user can navigate between questions and finally review and submit; unanswered "
            "questions are omitted from the submitted result. Never call more than once in one batch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["message", "form"],
                    "default": "message",
                    "description": "message = 普通消息；form = 多问题混合选择表单。",
                },
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "description": "普通消息模式要发送的消息。",
                },
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_QUESTIONS,
                    "description": "混合问题列表；每个问题可独立单选、多选或允许自定义输入。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "minLength": 1},
                            "options": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": MAX_OPTIONS,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string", "minLength": 1},
                                        "label": {"type": "string", "minLength": 1},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["id", "label"],
                                    "additionalProperties": False,
                                },
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "default": False,
                                "description": "false = 单选；true = 多选。",
                            },
                            "allowCustom": {
                                "type": "boolean",
                                "default": True,
                                "description": "允许点击“自定义输入”后用文字回答这一题。",
                            },
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                },
                # Legacy aliases are intentionally accepted at runtime only; schema guides new calls.
                "question": {"type": "string", "description": "旧版兼容：普通消息文本。"},
                "options": {"type": "array", "description": "旧版兼容字段。"},
                "multiple": {"type": "boolean", "description": "旧版兼容：单个问题是否多选。"},
                "allow_custom": {"type": "boolean", "description": "旧版兼容：是否允许自定义输入。"},
            },
            "additionalProperties": False,
        },
    },
}


@dataclass
class AskUserInteraction:
    id: str
    chat_id: int
    mode: str
    message: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)
    current_index: int = 0
    # question index -> {type: choice/custom, selected: [...]} ; only answered questions are stored
    answers: dict[int, dict[str, Any]] = field(default_factory=dict)
    selected_indices: set[int] = field(default_factory=set)
    awaiting_custom: bool = False
    message_id: int | None = None
    created_at: float = field(default_factory=time.time)
    # Sliding timeout: every valid user interaction refreshes the deadline.
    last_activity_at: float = field(default_factory=time.time)
    status: str = "waiting"
    future: asyncio.Future | None = None


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
            oid, desc = f"option_{idx + 1}", ""
        elif isinstance(raw, dict):
            label, desc = _option_text(raw)
            oid = truncate_to_token_budget(str(raw.get("id") or f"option_{idx + 1}").strip(), ASK_USER_ID_TOKEN_BUDGET, suffix="…")
        else:
            continue
        if label:
            out.append({"id": oid or f"option_{idx + 1}", "label": label, "description": desc})
    return out


def _normalize_questions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:MAX_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        q = truncate_to_token_budget(str(item.get("question") or "").strip(), ASK_USER_QUESTION_TOKEN_BUDGET, suffix="…")
        if not q:
            continue
        # Accept both the new camelCase spelling and a tolerant snake_case alias.
        multi = bool(item.get("multiSelect", item.get("multi_select", False)))
        custom = bool(item.get("allowCustom", item.get("allow_custom", True)))
        out.append({
            "question": q,
            "options": _normalized_options(item.get("options", [])),
            "multiSelect": multi,
            "allowCustom": custom,
        })
    return out


def _current_question(interaction: AskUserInteraction) -> dict[str, Any] | None:
    if 0 <= interaction.current_index < len(interaction.questions):
        return interaction.questions[interaction.current_index]
    return None


def _load_current_selection(interaction: AskUserInteraction) -> None:
    interaction.selected_indices.clear()
    interaction.awaiting_custom = False
    answer = interaction.answers.get(interaction.current_index)
    if not answer:
        return
    if answer.get("type") == "choice":
        for item in answer.get("selected", []):
            if not isinstance(item, dict):
                continue
            for i, option in enumerate((_current_question(interaction) or {}).get("options", [])):
                if option.get("id") == item.get("id"):
                    interaction.selected_indices.add(i)
                    break
    elif answer.get("type") == "custom":
        # 已经提交过的自定义答案应回到正常表单视图，并把答案直接显示出来。
        # 只有用户再次点击“自定义输入”时才进入 awaiting_custom 输入态。
        interaction.awaiting_custom = False


def _build_keyboard(interaction: AskUserInteraction) -> dict:
    if interaction.mode == "message":
        # 普通消息就是普通聊天消息：不显示任何交互按钮，也不存在用户侧的取消动作。
        return {"inline_keyboard": []}
    if interaction.current_index >= len(interaction.questions):
        rows = []
        if interaction.questions:
            rows.append([{"text": "✏️ 修改答案", "callback_data": f"ask:{interaction.id}:review"}])
        rows.append([{"text": "Submit answers", "callback_data": f"ask:{interaction.id}:submit"}, {"text": "Cancel", "callback_data": f"ask:{interaction.id}:cancel"}])
        return {"inline_keyboard": rows}

    q = _current_question(interaction) or {}
    if interaction.awaiting_custom:
        rows = [[{"text": "← 返回选项", "callback_data": f"ask:{interaction.id}:custom_cancel"}]]
    else:
        buttons = []
        for idx, option in enumerate(q.get("options", [])):
            prefix = "✅ " if idx in interaction.selected_indices else ""
            buttons.append({"text": f"{prefix}{option['label']}", "callback_data": f"ask:{interaction.id}:o:{idx}"})
        rows = []
        if len(buttons) <= 6 and all(len(b["text"]) <= 18 for b in buttons):
            for i in range(0, len(buttons), 2):
                rows.append(buttons[i:i + 2])
        else:
            rows.extend([[b] for b in buttons])
        if q.get("allowCustom", True):
            rows.append([{"text": "✏️ 自定义输入", "callback_data": f"ask:{interaction.id}:custom"}])

    nav = []
    if interaction.current_index > 0:
        nav.append({"text": "← 上一题", "callback_data": f"ask:{interaction.id}:prev"})
    if interaction.current_index < len(interaction.questions) - 1:
        nav.append({"text": "下一题 →", "callback_data": f"ask:{interaction.id}:next"})
    else:
        nav.append({"text": "查看答案 →", "callback_data": f"ask:{interaction.id}:review"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "Cancel", "callback_data": f"ask:{interaction.id}:cancel"}])
    return {"inline_keyboard": rows}


def _question_rich_text(question: str) -> str:
    return render_telegram_block(str(question or "")) if str(question or "") else ""


def _question_html(interaction: AskUserInteraction) -> str:
    if interaction.mode == "message":
        # 普通消息从发送开始就直接呈现为普通聊天文本，不暴露 message_user
        # 的内部等待状态，也不显示“普通消息/直接回复文本即可”等提示。
        return _question_rich_text(interaction.message)
    if interaction.current_index >= len(interaction.questions):
        return _review_html(interaction)
    q = _current_question(interaction) or {}
    index = interaction.current_index + 1
    total = len(interaction.questions)
    lines = [f"<p>📝 <b>问题 {index}/{total}</b></p>{_question_rich_text(q['question'])}"]
    for option in q.get("options", []):
        label = convert_markdown_to_telegram_html(option.get("label", ""))
        desc = convert_markdown_to_telegram_html(option.get("description", "")) if option.get("description") else ""
        lines.append(f"<p><b>• {label}</b>{f'<br/><i>{desc}</i>' if desc else ''}</p>")
    current_answer = interaction.answers.get(interaction.current_index)
    if current_answer and current_answer.get("type") == "custom":
        value = truncate_to_token_budget(
            str(current_answer.get("value", "")),
            ASK_USER_CUSTOM_ANSWER_TOKEN_BUDGET,
            suffix="…",
        )
        if value:
            lines.append(
                f"<p><b>→ 自定义回答：</b>{convert_markdown_to_telegram_html(value)}</p>"
            )
    if interaction.awaiting_custom:
        lines.append("<p><i>请直接发送这一题的回答。</i></p>")
    elif q.get("options"):
        mode = "可多选" if q.get("multiSelect") else "单选"
        lines.append(f"<p><i>{mode}；也可以点击“自定义输入”回答这一题。</i></p>")
    return "".join(lines)


def _review_html(interaction: AskUserInteraction) -> str:
    lines = ["<p>📋 <b>Review your answers</b></p>"]
    if len(interaction.answers) < len(interaction.questions):
        lines.append("<p><i>You have not answered all questions</i></p>")
    for idx, q in enumerate(interaction.questions):
        answer = interaction.answers.get(idx)
        if not answer:
            continue
        lines.append(f"<p><b>{_question_rich_text(q['question'])}</b></p>")
        if answer.get("type") == "choice":
            labels = [str(x.get("label", "")) for x in answer.get("selected", []) if isinstance(x, dict)]
            lines.append(f"<p>→ {convert_markdown_to_telegram_html('，'.join(labels))}</p>")
        elif answer.get("type") == "custom":
            value = truncate_to_token_budget(str(answer.get("value", "")), ASK_USER_CUSTOM_ANSWER_TOKEN_BUDGET, suffix="…")
            lines.append(f"<p>→ {convert_markdown_to_telegram_html(value)}</p>")
    lines.append("<p><i>Ready to submit your answers?</i></p>")
    return "".join(lines)


def _answer_json(answer: dict[str, Any]) -> str:
    return json.dumps(answer, ensure_ascii=False, separators=(",", ":"))


async def create_ask_user_interaction(
    chat_id: int,
    question: str = "",
    options: Any = None,
    *,
    multiple: bool = False,
    allow_custom: bool = True,
    mode: str | None = None,
    message: str | None = None,
    questions: Any = None,
) -> AskUserInteraction:
    # New API: explicit mode. Legacy API is translated into the same interaction model.
    if mode == "form" or questions is not None:
        normalized_questions = _normalize_questions(questions)
        if not normalized_questions:
            raise ValueError("message_user form 至少需要一个有效问题")
        interaction = AskUserInteraction(id=_new_id(), chat_id=chat_id, mode="form", questions=normalized_questions)
    elif mode == "message" or message is not None or (not options and question):
        text = truncate_to_token_budget(str(message if message is not None else question).strip(), ASK_USER_QUESTION_TOKEN_BUDGET, suffix="…")
        if not text:
            raise ValueError("message_user.message 不能为空")
        interaction = AskUserInteraction(id=_new_id(), chat_id=chat_id, mode="message", message=text)
    else:
        # Legacy single-question call, now represented as a one-question form.
        q = _normalize_questions([{
            "question": question,
            "options": options or [],
            "multiSelect": multiple,
            "allowCustom": allow_custom,
        }])
        if not q:
            raise ValueError("message_user.question 不能为空")
        interaction = AskUserInteraction(id=_new_id(), chat_id=chat_id, mode="form", questions=q)

    async with _lock:
        old_id = _pending_by_chat.get(chat_id)
        if old_id and old_id in _pending:
            old = _pending[old_id]
            old.status = "cancelled"
            if old.future and not old.future.done():
                old.future.cancel()
            _pending.pop(old_id, None)
        interaction.future = asyncio.get_running_loop().create_future()
        _pending[interaction.id] = interaction
        _pending_by_chat[chat_id] = interaction.id

    message_id = await send_rich_html_message(
        chat_id,
        _question_html(interaction),
        # 普通消息没有任何按钮；表单才使用 inline keyboard。
        reply_markup=(_build_keyboard(interaction) if interaction.mode == "form" else None),
        reassert_draft=True,
    )
    if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
        interaction.message_id = message_id
        return interaction
    await cancel_interaction(interaction.id, remove_ui=False)
    raise RuntimeError("无法发送 message_user 交互消息")


async def get_pending_for_chat(chat_id: int) -> AskUserInteraction | None:
    async with _lock:
        interaction_id = _pending_by_chat.get(chat_id)
        return _pending.get(interaction_id) if interaction_id else None


async def _clear_pending_unlocked(interaction: AskUserInteraction) -> None:
    _pending.pop(interaction.id, None)
    if _pending_by_chat.get(interaction.chat_id) == interaction.id:
        _pending_by_chat.pop(interaction.chat_id, None)


async def _clear_pending(interaction: AskUserInteraction) -> None:
    async with _lock:
        await _clear_pending_unlocked(interaction)


_UI_FOLLOWUP_TASKS: set["asyncio.Task[Any]"] = set()


def _spawn_ui_followup(coro: Coroutine[Any, Any, Any]) -> None:
    def _reap(task: "asyncio.Task[Any]") -> None:
        _UI_FOLLOWUP_TASKS.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("问答界面后续编辑失败: %s", task.exception())
    task = asyncio.create_task(coro)
    _UI_FOLLOWUP_TASKS.add(task)
    task.add_done_callback(_reap)


async def _set_markup(message_id: int | None, chat_id: int, markup: dict | None) -> None:
    if not message_id:
        return
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": markup or {"inline_keyboard": []}}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8, connect=3)) as session:
            async with session.post(f"{BASE_URL}/editMessageReplyMarkup", json=payload) as resp:
                if resp.status != 200:
                    logger.debug("message_user edit markup failed: %s", (await resp.text())[:200])
    except Exception:
        logger.debug("message_user edit markup exception", exc_info=True)


async def _edit_question_message(interaction: AskUserInteraction, body_html: str, markup: dict | None = None) -> None:
    if not interaction.message_id:
        return
    payload = {
        "chat_id": interaction.chat_id,
        "message_id": interaction.message_id,
        "rich_message": _rich_message_html_payload(body_html),
        "reply_markup": markup or {"inline_keyboard": []},
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8, connect=3)) as session:
            async with session.post(f"{BASE_URL}/editMessageText", json=payload) as resp:
                if resp.status != 200:
                    logger.debug("message_user edit text failed: %s", (await resp.text())[:200])
    except Exception:
        logger.debug("message_user edit text exception", exc_info=True)


def _answered_html(interaction: AskUserInteraction, answer: dict[str, Any]) -> str:
    if interaction.mode == "message":
        # 普通消息始终保持普通聊天消息外观；用户的回复属于下一条用户消息，
        # 不要把“普通消息”标签或取消/回复提示重新写回机器人消息。
        return _question_rich_text(interaction.message)
    return _review_html(interaction)


def _touch_activity(interaction: AskUserInteraction) -> None:
    """Refresh the sliding inactivity timeout after a valid interaction."""
    interaction.last_activity_at = time.time()


async def _finish(interaction: AskUserInteraction, answer: dict[str, Any], *, body: str | None = None) -> None:
    interaction.status = "answered"
    if interaction.future and not interaction.future.done():
        interaction.future.set_result(answer)
    await _clear_pending_unlocked(interaction)
    _spawn_ui_followup(_edit_question_message(interaction, body or _answered_html(interaction, answer)))


async def resolve_callback(chat_id: int, callback_from_id: int, interaction_id: str, action: str, arg: str = "") -> tuple[bool, str]:
    async with _lock:
        interaction = _pending.get(interaction_id)
        if interaction is None:
            return False, "这个问题已经结束或失效了"
        try:
            if int(interaction.chat_id) != int(chat_id) or int(callback_from_id) != int(chat_id):
                return False, "无权限"
        except (TypeError, ValueError):
            return False, "无效的 chat_id 或 callback_from_id"
        if interaction.status != "waiting":
            return False, "这个问题已经处理过了"

        if interaction.mode == "message":
            # 普通消息没有按钮，因此正常不会进入 callback；即使收到旧消息
            # 残留的 cancel callback，也不能再提供“取消发送”语义。
            return False, "普通消息没有可用按钮，请直接回复文本"

        if action == "o":
            _touch_activity(interaction)
            q = _current_question(interaction) or {}
            try:
                idx = int(arg)
            except (TypeError, ValueError):
                return False, "无效选项"
            if idx < 0 or idx >= len(q.get("options", [])):
                return False, "无效选项"
            if q.get("multiSelect"):
                if idx in interaction.selected_indices:
                    interaction.selected_indices.remove(idx)
                else:
                    interaction.selected_indices.add(idx)
            else:
                interaction.selected_indices = {idx}
            selected = [q["options"][i] for i in sorted(interaction.selected_indices)]
            interaction.answers[interaction.current_index] = {"type": "choice", "multiple": bool(q.get("multiSelect")), "selected": selected}
            markup = _build_keyboard(interaction)
            body = _question_html(interaction)
            message_id = interaction.message_id
            _spawn_ui_followup(_edit_question_message(interaction, body, markup))
            return True, "已选择"

        if action == "custom":
            _touch_activity(interaction)
            q = _current_question(interaction) or {}
            if not q.get("allowCustom", True):
                return False, "此问题不支持自定义输入"
            interaction.awaiting_custom = True
            interaction.selected_indices.clear()
            markup = _build_keyboard(interaction)
            _spawn_ui_followup(_edit_question_message(interaction, _question_html(interaction), markup))
            return True, "请直接发送这一题的回答"

        if action == "custom_cancel":
            _touch_activity(interaction)
            _load_current_selection(interaction)
            markup = _build_keyboard(interaction)
            _spawn_ui_followup(_edit_question_message(interaction, _question_html(interaction), markup))
            return True, "已返回选项"

        if action in {"prev", "next"}:
            if action == "prev" and interaction.current_index > 0:
                interaction.current_index -= 1
            elif action == "next" and interaction.current_index < len(interaction.questions) - 1:
                interaction.current_index += 1
            else:
                return False, "已经在边界"
            _touch_activity(interaction)
            _load_current_selection(interaction)
            _spawn_ui_followup(_edit_question_message(interaction, _question_html(interaction), _build_keyboard(interaction)))
            return True, "已切换问题"

        if action == "review":
            _touch_activity(interaction)
            interaction.current_index = len(interaction.questions)
            _spawn_ui_followup(_edit_question_message(interaction, _review_html(interaction), _build_keyboard(interaction)))
            return True, "已打开答案预览"

        if action == "submit":
            _touch_activity(interaction)
            # Only answered questions are serialized, exactly matching the review page.
            submitted = []
            for idx, answer in sorted(interaction.answers.items()):
                if 0 <= idx < len(interaction.questions):
                    submitted.append({"index": idx, "question": interaction.questions[idx]["question"], **answer})
            result = {"type": "form", "answers": submitted, "answeredCount": len(submitted), "questionCount": len(interaction.questions)}
            await _finish(interaction, result, body=_review_html(interaction))
            return True, "已提交答案"

        if action == "cancel":
            await _finish(interaction, {"type": "cancelled"}, body="<p>✖️ <b>已取消</b></p>")
            return True, "已取消"
        return False, "未知操作"


async def resolve_text(chat_id: int, text: str) -> bool:
    """原子消费一条用户文本；成功返回 True，调用方绝不能再创建新 turn。"""
    text = str(text or "").strip()
    if not text:
        return False
    async with _lock:
        interaction_id = _pending_by_chat.get(chat_id)
        interaction = _pending.get(interaction_id) if interaction_id else None
        if not interaction or interaction.status != "waiting":
            return False
        if interaction.mode == "form":
            q = _current_question(interaction)
            if q is None or not interaction.awaiting_custom:
                return False
            answer = {"type": "custom", "value": truncate_to_token_budget(text, ASK_USER_CUSTOM_ANSWER_TOKEN_BUDGET, suffix="…")}
            _touch_activity(interaction)
            interaction.answers[interaction.current_index] = answer
            interaction.awaiting_custom = False
            # Do not auto-submit. Stay on this question so the user can review/navigate.
            _spawn_ui_followup(_edit_question_message(interaction, _question_html(interaction), _build_keyboard(interaction)))
            return True
        answer = {"type": "custom", "value": truncate_to_token_budget(text, ASK_USER_CUSTOM_ANSWER_TOKEN_BUDGET, suffix="…")}
        await _finish(interaction, answer)
        return True


async def wait_for_answer(interaction: AskUserInteraction) -> dict[str, Any]:
    assert interaction.future is not None
    try:
        # Sliding inactivity timeout: the user gets INTERACTION_TIMEOUT seconds
        # after the most recent valid interaction, rather than one fixed 2-minute
        # window from the moment the form was opened.
        while True:
            remaining = INTERACTION_TIMEOUT - (time.time() - interaction.last_activity_at)
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                return await asyncio.wait_for(
                    asyncio.shield(interaction.future), timeout=remaining
                )
            except asyncio.TimeoutError:
                # A callback/text operation may have refreshed last_activity_at
                # at the same moment the timer fired. Re-check before expiring.
                if time.time() - interaction.last_activity_at < INTERACTION_TIMEOUT:
                    continue
                raise
    except asyncio.TimeoutError:
        async with _lock:
            if interaction.status != "waiting":
                return await asyncio.shield(interaction.future)
            interaction.status = "expired"
            if interaction.future and not interaction.future.done():
                interaction.future.set_result({"type": "expired"})
            await _clear_pending_unlocked(interaction)
        if interaction.mode == "message":
            await _edit_question_message(interaction, _question_rich_text(interaction.message))
        else:
            await _edit_question_message(interaction, _review_html(interaction))
        return {"type": "expired"}
    except asyncio.CancelledError:
        async with _lock:
            if interaction.status == "waiting":
                interaction.status = "cancelled"
                await _clear_pending_unlocked(interaction)
        await _edit_question_message(interaction, _answered_html(interaction, {"type": "cancelled"}))
        raise


async def cancel_interaction(interaction_id: str, remove_ui: bool = True) -> None:
    async with _lock:
        interaction = _pending.get(interaction_id)
        if not interaction:
            return
        interaction.status = "cancelled"
        if interaction.future and not interaction.future.done():
            interaction.future.cancel()
        await _clear_pending_unlocked(interaction)
        message_id, chat_id = interaction.message_id, interaction.chat_id
    if remove_ui:
        await _set_markup(message_id, chat_id, None)


def answer_to_tool_result(answer: dict[str, Any]) -> str:
    result = dict(answer or {})
    result.setdefault("type", "unknown")
    if result.get("type") == "expired":
        result["note"] = "用户在超时时间内没有回复（用户可能不在）。这不是错误；可结束本回合，用户回来后会再联系。"
    return _answer_json(result)
