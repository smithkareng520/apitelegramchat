# proactive.py
"""统一上下文的主动唤醒（TIMER 事件源）与 ``send_message_to_user`` 工具。

背景与设计
==========

本模块让 Agent 拥有"自己的活动时间"：用户空闲约 20 分钟后，调度器开始像人
一样不定期地唤醒 Agent（随机 10/20/40 分钟，总之 1 小时以内）；每次唤醒与
用户主动发消息（USER 事件源）共用同一份会话历史（统一上下文），但行为不同：

- TIMER 回合对用户**完全静默**：不创建草稿、不显示工具进度、最终文本也不
  推送到 Telegram——这些只存在于历史上下文中。唯一能触达用户的通道是模型
  显式调用 ``send_message_to_user``，且内容按**普通纯文本**（不带任何格式）
  通过 sendMessage 发送，像人随手发消息一样。
- 唤醒用的合成 user 消息（WAKEUP_PROMPT）只进入本轮请求，**不写入持久历史**
  （timer 唤醒不写入历史）；回合产生的 assistant/tool 消息正常沉淀，保证
  用户下次回复时模型仍知道后台做过什么（统一上下文）。
- 用户发新消息会**打断**进行中的 TIMER 回合：后台任务被取消，该回合已通过
  ``send_message_to_user`` 发出的普通消息被**静默撤回**（不显示"已停止"之类
  的提示）。

调度节奏（均可用环境变量覆盖）
==============================

- 用户空闲 ``PROACTIVE_IDLE_START_SECONDS``（默认 1200s ≈ 20min，含 ±10% 抖动）
  后触发第一次唤醒；
- 之后每次唤醒间隔从 ``PROACTIVE_INTERVAL_CHOICES``（默认 600,1200,2400 秒）
  中随机选择；
- 用户连续 ``PROACTIVE_MAX_IDLE_SECONDS``（默认 10800s = 3h）没有发消息：
  停止高频触发，改为**休息 1 小时再触发一次**的慢节奏（人在长时间没回应时
  也不会一直刷屏）；用户随时回来则立即恢复正常节奏；
- 仅私聊参与主动唤醒；``PROACTIVE_ENABLED=false`` 可整体关闭。

消息撤回与编辑
==============

``send_message_to_user`` 做成单工具 + ``action`` 参数：

- ``send``：发送新的纯文本消息，返回 ``message_id``；
- ``edit``：编辑此前发出的某条消息（传 ``message_id`` + 新 ``content``）；
- ``delete``：撤回此前发出的某条消息（传 ``message_id``）。

跨回合的消息 id 注册表（``_proactive_message_ids``）让后续 TIMER 回合仍能
编辑/撤回更早主动发出的消息；被打断的当回合消息则由打断逻辑统一撤回。

线程与并发模型
==============

- 每个 chat 一个调度协程（``_chat_scheduler_loop``），轮询用户活动时间戳；
- 进行中的 TIMER 回合登记在 ``_active_flows``；工具执行中的 sendMessage 用
  ``asyncio.shield`` 包裹并登记到 ``flow.pending_sends``——即使回合被取消，
  在途请求也会完成注册，随后由打断逻辑统一撤回，不会产生"撤不掉"的残留消息。
"""
import asyncio
import json
import os
import random
import time
from typing import Awaitable, Callable, Optional

import aiohttp

from apitelegramchat.config import BASE_URL
from apitelegramchat.utils import get_logger, delete_message_fast

logger = get_logger(__name__)


# =====================================================================
# 配置（全部可用环境变量覆盖）
# =====================================================================
def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_seconds(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    try:
        value = int(str(raw).strip()) if raw is not None and str(raw).strip() else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_seconds_list(name: str, default: list[int]) -> list[int]:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return list(default)
    out: list[int] = []
    for part in str(raw).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(max(1, int(float(part))))
        except (TypeError, ValueError):
            continue
    return out or list(default)


PROACTIVE_ENABLED = _env_flag("PROACTIVE_ENABLED", True)
# 用户空闲多久后开始"活动时间"（首次唤醒触发点）
PROACTIVE_IDLE_START_SECONDS = _env_seconds("PROACTIVE_IDLE_START_SECONDS", 20 * 60)
# 两次唤醒之间的随机间隔候选（像人一样不定期）
PROACTIVE_INTERVAL_CHOICES = _env_seconds_list("PROACTIVE_INTERVAL_CHOICES", [600, 1200, 2400])
# 用户连续多久没发消息 → 停止高频触发
PROACTIVE_MAX_IDLE_SECONDS = _env_seconds("PROACTIVE_MAX_IDLE_SECONDS", 3 * 3600)
# 停止触发后的休息时长；休息结束后再触发一次（用户仍无回应则继续休息）
PROACTIVE_REST_SECONDS = _env_seconds("PROACTIVE_REST_SECONDS", 3600)
# 调度协程的轮询粒度
_PROACTIVE_POLL_SECONDS = _env_seconds("PROACTIVE_POLL_SECONDS", 10, minimum=1)
# 跨回合消息 id 注册表容量（仅用于 edit/delete 校验）
_PROACTIVE_REMEMBER_LIMIT = _env_seconds("PROACTIVE_REMEMBER_LIMIT", 50, minimum=1)
# 普通消息单条文本上限（Telegram sendMessage 协议限制 4096）
_PLAIN_TEXT_LIMIT = 4000


# =====================================================================
# 唤醒提示词与工具定义
# =====================================================================
WAKEUP_PROMPT = """
[系统后台唤醒｜TIMER 主动巡检回合]

你现在处于“主动巡检回合”。不要把这次唤醒当成普通聊天请求。
这是一次“由你主动判断现在是否应该为用户做点什么”的机会。

必须按下面顺序工作，并以“有价值才行动、没有价值就静默”为原则：

1. 检查当前 Todo
   - 优先调用 todo 的 list，检查未完成任务。
   - 判断是否存在：已到期、临近到期、长期未推进、现在适合推进、或现在值得提醒的任务。
   - 如果任务明确且安全、且无需用户补充信息，可以推进 Todo 状态；否则优先提醒用户。
   - 不要为了“有事可做”而虚构任务、修改无关任务或重复提醒已经处理过的事情。

2. 检查最近上下文
   - 回看最近对话，寻找未完成的话题、用户留下的待办、承诺、等待结果、计划和自然后续。
   - 判断是否有事情到了“现在联系用户最自然”的时间点。
   - 注意避免重复发送上一轮已经说过的内容。

3. 判断主动沟通价值
   按优先级判断：
   A. 明确任务需要推进/提醒 → 推进或提醒；
   B. 有自然、具体的跟进 → 主动联系；
   C. 有与用户上下文相关、现在有价值的新信息 → 主动分享；
   D. 没有重大事项，但存在轻量、真实、自然的交流机会 → 可以联系；
   E. 以上都没有 → 保持静默。

   “轻量交流机会”必须与用户近期上下文有真实关联，不能只是泛泛寒暄。
   宁可少发，也不要为了完成 TIMER 回合而制造消息。

4. 如果决定联系用户
   - 必须调用 send_message_to_user。
   - 不要只生成 assistant 文本；assistant 最终文本不会直接送达用户。
   - 一次回合通常只发一条最有价值的消息；只有确有必要时才发送多条。
   - 消息要短、自然、具体，像真人主动想起一件事后发来的消息。
   - 不要解释“这是 TIMER/后台唤醒”，不要报告内部检查过程。
   - 不要使用 Markdown/HTML。

5. 如果没有任何合理行动
   - 不调用 send_message_to_user。
   - 保持静默即可。

禁止：
- “明白，我会注意……”
- “有什么我可以帮您的吗？”
- “我会等待您的消息。”
- “只是想问问你最近怎么样”
- 为了完成回合而发送无意义寒暄。
- 把内部推理、工具检查过程或“我决定不联系”发送给用户。

重要：
- 本回合的目标不是“必须发消息”，而是“主动判断是否值得发消息”。
- Todo 与最近上下文是第一优先级；不要跳过 Todo 检查直接闲聊。
- 只在有充分依据时行动，不编造用户意图、时间、承诺或事实。
"""

SEND_MESSAGE_TO_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "send_message_to_user",
        "description": (
            "向当前用户推送一条即时消息（仅在系统后台唤醒的活动时间可用）。"
            "内容必须是普通纯文本：不使用任何 Markdown、HTML 或格式符号——像人随手发消息一样简短自然。"
            "同一个工具通过 action 控制行为：send 发送新消息（返回 message_id）；"
            "edit 编辑你此前发出的某条消息；delete 撤回（删除）你此前发出的某条消息。"
            "不要频繁打扰用户，但不要因为没有重大事件而避免发送。只要消息自然、有帮助或能推进关系维护，就可以主动发送。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["send", "edit", "delete"],
                    "description": "send=发送新消息；edit=修改已发送的消息；delete=撤回已发送的消息。",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "消息文本，send/edit 时必填。纯文本无格式，口语化、简短自然，"
                        "像一个真人发来的消息。"
                    ),
                },
                "message_id": {
                    "type": "integer",
                    "description": "edit/delete 时必填：此前 send 返回的 message_id。",
                },
            },
            "required": ["action"],
        },
    },
}


# =====================================================================
# 回合注册表：进行中的 TIMER 回合 + 消息追踪
# =====================================================================
class _ProactiveFlow:
    """一次进行中的 TIMER 回合及其发出的普通消息。"""

    __slots__ = ("chat_id", "task", "sent_message_ids", "pending_sends", "interrupted")

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.task: Optional[asyncio.Task] = None
        # 本回合通过 send_message_to_user 发出的消息（打断时整体静默撤回）
        self.sent_message_ids: list[int] = []
        # 被 shield 保护、仍在途的发送任务（取消后也会完成注册）
        self.pending_sends: set[asyncio.Task] = set()
        # 打断标记：置位后新消息立即自清理，不再进入撤回列表
        self.interrupted: bool = False


_active_flows: dict[int, _ProactiveFlow] = {}
# 跨回合消息 id 注册表：chat_id -> 最近发出的 message_id 列表（edit/delete 校验用）
_proactive_message_ids: dict[int, list[int]] = {}


def is_proactive_flow_active(chat_id: int) -> bool:
    """该 chat 是否有进行中的 TIMER 回合。"""
    return chat_id in _active_flows


def _remember_proactive_message(chat_id: int, message_id: int) -> None:
    ids = _proactive_message_ids.setdefault(chat_id, [])
    if message_id in ids:
        return
    ids.append(message_id)
    if len(ids) > _PROACTIVE_REMEMBER_LIMIT:
        del ids[: len(ids) - _PROACTIVE_REMEMBER_LIMIT]


def _forget_proactive_messages(chat_id: int, message_ids: list[int]) -> None:
    ids = _proactive_message_ids.get(chat_id)
    if not ids:
        return
    drop = {int(m) for m in message_ids}
    ids[:] = [m for m in ids if m not in drop]


def _is_known_proactive_message(chat_id: int, message_id) -> bool:
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return False
    if mid <= 0:
        return False
    return mid in _proactive_message_ids.get(chat_id, [])


def _register_sent_message(flow: _ProactiveFlow, chat_id: int, message_id) -> None:
    """把一条已发出的普通消息登记到回合与跨回合注册表。"""
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return
    if mid <= 0:
        return
    if flow.interrupted:
        # 回合已被打断、撤回已经执行过：这条迟到消息立即自清理，
        # 避免出现"撤不掉"的残留。
        asyncio.create_task(_recall_message_quietly(chat_id, mid))
        return
    flow.sent_message_ids.append(mid)
    _remember_proactive_message(chat_id, mid)


async def _recall_message_quietly(chat_id: int, message_id: int) -> bool:
    """静默撤回一条普通消息：尽力而为、不抛异常、不产生任何用户可见提示。"""
    try:
        ok = await delete_message_fast(chat_id, message_id)
        if ok:
            _forget_proactive_messages(chat_id, [message_id])
        return ok
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


# =====================================================================
# Telegram 普通消息 HTTP 原语（纯文本、无 parse_mode，像人发消息）
# =====================================================================
async def _telegram_post(method: str, payload: dict, *, retries: int = 1) -> tuple[bool, dict | None, str]:
    """调用 Telegram Bot API，返回 (ok, result_dict, body_text)。

    超时设计：单次 5s / 连接 2s。send 路径最多重试 2 次（最坏 ~11s），
    必须落在 tool_call_loop 的外层超时（send_message_to_user 已加入
    LONG_RUNNING_TOOLS，45s）之内——否则外层会误判超时并可能重发，
    造成重复消息。
    """
    url = f"{BASE_URL}/{method}"
    last_body = ""
    for attempt in range(max(1, retries)):
        try:
            timeout = aiohttp.ClientTimeout(total=5, connect=2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    body = await resp.text()
                    last_body = body
                    if resp.status == 200:
                        try:
                            return True, await resp.json(), body
                        except Exception:
                            return True, None, body
                    # 4xx 属于确定性失败，重试没有意义
                    if 400 <= resp.status < 500:
                        return False, None, body
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[proactive] %s 请求异常(第 %s 次): %s", method, attempt + 1, e)
            last_body = str(e)
        if attempt < max(1, retries) - 1:
            await asyncio.sleep(1.0)
    return False, None, last_body


async def _send_plain_message(chat_id: int, text: str) -> Optional[int]:
    """发送一条普通纯文本消息（不带任何格式），返回 message_id。"""
    payload = {"chat_id": chat_id, "text": text}
    ok, data, body = await _telegram_post("sendMessage", payload, retries=2)
    if not ok:
        # 403 = 用户屏蔽/移除了 bot：停止对该 chat 的主动唤醒，避免永远空转
        if "403" in body or "blocked" in body.lower() or "kicked" in body.lower():
            logger.warning("[proactive] chat=%s sendMessage 被拒（%s），停用该会话的主动唤醒", chat_id, body[:120])
            await _deactivate_chat(chat_id)
        else:
            logger.warning("[proactive] chat=%s sendMessage 失败: %s", chat_id, body[:200])
        return None
    try:
        msg_id = (data or {}).get("result", {}).get("message_id")
        if isinstance(msg_id, int) and msg_id > 0:
            return msg_id
    except Exception:
        pass
    return None


async def _edit_plain_message(chat_id: int, message_id: int, text: str) -> bool:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    ok, _data, body = await _telegram_post("editMessageText", payload, retries=1)
    if not ok and "message is not modified" in body.lower():
        # 幂等：内容没变化视为成功
        return True
    if not ok:
        logger.warning("[proactive] chat=%s editMessageText 失败: %s", chat_id, body[:200])
    return ok


# =====================================================================
# send_message_to_user 工具执行器（由 dispatch_tool_call 调用）
# =====================================================================
async def execute_send_message_to_user(chat_id: int, arguments: dict) -> str:
    """执行 send_message_to_user 工具调用。

    仅在 TIMER 回合中可用：USER 回合的工具列表不包含本工具，若模型仍
    幻觉调用，这里会返回明确的错误说明。
    """
    if chat_id is None:
        return json.dumps({"error": "chat_id is required"}, ensure_ascii=False)

    flow = _active_flows.get(chat_id)
    if flow is None:
        return (
            "失败：当前没有处于后台唤醒状态的 agent 回合，send_message_to_user 不可用。"
            "请直接把回复内容写给用户即可。"
        )
    if flow.interrupted:
        return "失败：本回合已被用户的新消息打断，不要再发送消息。"

    action = str(arguments.get("action") or "send").strip().lower()
    content = arguments.get("content")

    if action == "send":
        if not isinstance(content, str) or not content.strip():
            return "失败：action=send 需要 content（纯文本消息内容）。"
        text = content.strip()
        if len(text) > _PLAIN_TEXT_LIMIT:
            text = text[:_PLAIN_TEXT_LIMIT]

        async def _do_send() -> Optional[int]:
            msg_id = await _send_plain_message(chat_id, text)
            if msg_id:
                _register_sent_message(flow, chat_id, msg_id)
            return msg_id

        # shield + 注册表：回合被取消时，在途请求仍会完成注册，
        # 随后由打断逻辑统一撤回，不会产生撤不掉的残留消息。
        send_task = asyncio.ensure_future(_do_send())
        flow.pending_sends.add(send_task)
        send_task.add_done_callback(lambda _t, _f=flow: _f.pending_sends.discard(_t))
        try:
            msg_id = await asyncio.shield(send_task)
        except asyncio.CancelledError:
            raise  # 让打断流程负责收尾与撤回
        if msg_id:
            logger.info(
                "[TIMER] chat=%s action=SEND message_id=%s chars=%s",
                chat_id, msg_id, len(text),
            )
            preview = text if len(text) <= 60 else text[:60] + "…"
            return f"已发送（message_id={msg_id}）：{preview}"
        return "失败：消息发送失败（网络或 Telegram 错误），可稍后重试。"

    elif action == "edit":
        message_id = arguments.get("message_id")
        if not isinstance(content, str) or not content.strip():
            return "失败：action=edit 需要 content（新的纯文本内容）。"
        if not _is_known_proactive_message(chat_id, message_id):
            return (
                "失败：message_id 无效——只能编辑本助手通过 send_message_to_user "
                "发出的消息（使用 send 返回的 message_id）。"
            )
        text = content.strip()
        if len(text) > _PLAIN_TEXT_LIMIT:
            text = text[:_PLAIN_TEXT_LIMIT]
        ok = await _edit_plain_message(chat_id, int(message_id), text)
        if ok:
            preview = text if len(text) <= 60 else text[:60] + "…"
            return f"已编辑消息 {int(message_id)}：{preview}"
        return "失败：编辑消息失败（消息可能已被删除或超过可编辑时限）。"

    elif action == "delete":
        message_id = arguments.get("message_id")
        if not _is_known_proactive_message(chat_id, message_id):
            return (
                "失败：message_id 无效——只能撤回本助手通过 send_message_to_user "
                "发出的消息（使用 send 返回的 message_id）。"
            )
        mid = int(message_id)
        ok = await _recall_message_quietly(chat_id, mid)
        if ok:
            # 该回合自己的列表里也移除，避免打断时重复删除
            try:
                flow.sent_message_ids.remove(mid)
            except ValueError:
                pass
            return f"已撤回消息 {mid}。"
        return "失败：撤回失败（消息可能已不存在），可忽略并继续。"

    return f"失败：未知 action：{action}（可选 send / edit / delete）。"


# =====================================================================
# 调度器：per-chat 状态机
# =====================================================================
class _ChatSchedule:
    __slots__ = ("chat_id", "last_activity", "task")

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.last_activity = time.monotonic()
        self.task: Optional[asyncio.Task] = None


_schedules: dict[int, _ChatSchedule] = {}
_schedules_lock = asyncio.Lock()
_stop_event = asyncio.Event()

# 由 app.py 注册的回调：运行一个 TIMER 回合 / 判断 USER 流程是否进行中
_turn_runner_callback: Optional[Callable[[int], Awaitable[None]]] = None
_busy_check_callback: Optional[Callable[[int], bool]] = None


def register_turn_runner(cb: Callable[[int], Awaitable[None]]) -> None:
    """注册 TIMER 回合执行器（app._handle_timer_wakeup）。"""
    global _turn_runner_callback
    _turn_runner_callback = cb


def register_busy_check(cb: Callable[[int], bool]) -> None:
    """注册"该 chat 是否有 USER 流程进行中"的判断回调。"""
    global _busy_check_callback
    _busy_check_callback = cb


def _next_idle_start() -> float:
    """首次唤醒的空闲阈值，带 ±10% 抖动（"20min 左右"）。"""
    jitter = random.uniform(0.9, 1.1)
    return PROACTIVE_IDLE_START_SECONDS * jitter


async def note_user_activity(chat_id: int, *, private: bool = True) -> None:
    """记录一次用户活动；首次见到私聊 chat 时启动其调度协程。

    任何授权用户消息（含命令）都算活动。群聊不参与主动唤醒。
    """
    if not PROACTIVE_ENABLED or chat_id is None:
        return
    if not private:
        return
    now = time.monotonic()
    async with _schedules_lock:
        sched = _schedules.get(chat_id)
        if sched is None:
            sched = _ChatSchedule(chat_id)
            sched.last_activity = now
            _schedules[chat_id] = sched
        else:
            sched.last_activity = now
            # 防御：调度协程意外退出时自动复活
            if sched.task is None or sched.task.done():
                if not _stop_event.is_set():
                    sched.task = asyncio.create_task(_chat_scheduler_loop(chat_id))
                    logger.info("[proactive] chat=%s 调度协程复活", chat_id)
                return
            return
    logger.info("[proactive] chat=%s 开始跟踪用户活动（空闲 %ss 后进入活动时间）",
                chat_id, int(PROACTIVE_IDLE_START_SECONDS))
    if not _stop_event.is_set():
        sched.task = asyncio.create_task(_chat_scheduler_loop(chat_id))


async def _deactivate_chat(chat_id: int) -> None:
    """停止对某个 chat 的主动唤醒（例如 bot 被用户屏蔽）。"""
    async with _schedules_lock:
        sched = _schedules.pop(chat_id, None)
    if sched and sched.task and not sched.task.done():
        sched.task.cancel()
    logger.info("[proactive] chat=%s 已停用主动唤醒", chat_id)


async def _sleep_or_stop(seconds: float) -> bool:
    """睡眠指定秒数；调度器停止时提前返回 True。"""
    try:
        await asyncio.wait_for(_stop_event.wait(), timeout=max(0.05, seconds))
        return True
    except asyncio.TimeoutError:
        return False


async def _wait_activity_or_stop(chat_id: int, timeout: float) -> bool:
    """等待 timeout 秒；期间用户有新活动返回 'activity'，调度器停止返回 'stop'。"""
    deadline = time.monotonic() + timeout
    sched = _schedules.get(chat_id)
    base_activity = sched.last_activity if sched else None
    while True:
        if _stop_event.is_set():
            return "stop"
        sched = _schedules.get(chat_id)
        if sched is None:
            return "stop"
        if sched.last_activity != base_activity:
            return "activity"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        if await _sleep_or_stop(min(_PROACTIVE_POLL_SECONDS, remaining)):
            return "stop"


async def _fire_turn(chat_id: int) -> None:
    """触发一次 TIMER 唤醒回合（忙碌时静默跳过）。"""
    if _stop_event.is_set():
        return
    if chat_id in _active_flows:
        # 上一回合尚未结束：跳过本次唤醒，等下一个间隔
        logger.info("[proactive] chat=%s 上一个 TIMER 回合仍在进行，跳过本次唤醒", chat_id)
        return
    try:
        if _busy_check_callback is not None and _busy_check_callback(chat_id):
            logger.info("[TIMER] chat=%s 决策=SKIP：用户流程进行中", chat_id)
            return
    except Exception:
        logger.debug("[proactive] chat=%s busy check 失败，按空闲处理", chat_id, exc_info=True)
    runner = _turn_runner_callback
    if runner is None:
        return

    flow = _ProactiveFlow(chat_id)

    async def _run() -> None:
        try:
            await runner(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[proactive] chat=%s TIMER 回合异常", chat_id)
        finally:
            await _drain_pending_sends(flow, timeout=2.0)
            logger.info(
                "[TIMER] chat=%s 回合收尾：sent_messages=%s interrupted=%s",
                chat_id, len(flow.sent_message_ids), flow.interrupted,
            )
            if _active_flows.get(chat_id) is flow:
                _active_flows.pop(chat_id, None)

    _active_flows[chat_id] = flow
    flow.task = asyncio.create_task(_run())

    def _log_unexpected(task: asyncio.Task, _cid: int = chat_id) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("[proactive] chat=%s TIMER 回合任务异常退出: %s", _cid, exc)

    flow.task.add_done_callback(_log_unexpected)
    logger.info(
        "[TIMER] chat=%s 决策=RUN：开始执行主动巡检（todo→context→value→action）",
        chat_id,
    )


async def _drain_pending_sends(flow: _ProactiveFlow, *, timeout: float) -> None:
    """等待仍在途的 shield 发送完成注册（用于回合收尾与打断撤回前）。"""
    pending = [t for t in flow.pending_sends if not t.done()]
    if not pending:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("[proactive] chat=%s 仍有 %s 个在途发送未完成", flow.chat_id, len(pending))


async def _chat_scheduler_loop(chat_id: int) -> None:
    """单个 chat 的唤醒节奏状态机。

    阶段 1（空闲等待）：等用户空闲达到 idle_start（±10% 抖动）；
    阶段 2（活动时间）：立即触发首次唤醒，此后每隔随机 10/20/40min 唤醒一次；
        若用户连续 MAX_IDLE（默认 3h）没有消息：停止高频触发，
        改为"休息 REST（默认 1h）→ 触发一次"的慢节奏循环。
    任何时刻用户发来新消息都回到阶段 1 重新计空闲。
    """
    logger.info("[proactive] chat=%s 调度协程启动", chat_id)
    try:
        while not _stop_event.is_set():
            # ---- 阶段 1：等待空闲达标 ----
            idle_start = _next_idle_start()
            while not _stop_event.is_set():
                sched = _schedules.get(chat_id)
                if sched is None:
                    return
                idle = time.monotonic() - sched.last_activity
                if idle >= idle_start:
                    logger.info(
                        "[TIMER] chat=%s 空闲阈值达到：idle=%ss threshold=%ss，进入主动巡检",
                        chat_id, int(idle), int(idle_start),
                    )
                    break
                if await _sleep_or_stop(min(_PROACTIVE_POLL_SECONDS, idle_start - idle + 0.5)):
                    return

            # ---- 阶段 2：活动时间 ----
            while not _stop_event.is_set():
                sched = _schedules.get(chat_id)
                if sched is None:
                    return
                idle = time.monotonic() - sched.last_activity
                if idle < PROACTIVE_MAX_IDLE_SECONDS:
                    # 正常节奏：先触发（首次进入时立即，之后在间隔后）
                    await _fire_turn(chat_id)
                    interval = random.choice(PROACTIVE_INTERVAL_CHOICES)
                    logger.info(
                        "[TIMER] chat=%s 下一次巡检计划：%ss 后（候选=%s）",
                        chat_id, interval, PROACTIVE_INTERVAL_CHOICES,
                    )
                    state = await _wait_activity_or_stop(chat_id, interval)
                    if state == "activity":
                        break  # 用户回来了：回到阶段 1 重新计空闲
                    if state == "stop":
                        return
                else:
                    # 连续空闲超限（默认 3h）：休息 1h 再触发一次
                    logger.info(
                        "[proactive] chat=%s 用户已连续空闲 %smin，进入休息模式（%smin 后再看一眼）",
                        chat_id, int(idle // 60), int(PROACTIVE_REST_SECONDS // 60),
                    )
                    state = await _wait_activity_or_stop(chat_id, PROACTIVE_REST_SECONDS)
                    if state == "activity":
                        break
                    if state == "stop":
                        return
                    await _fire_turn(chat_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[proactive] chat=%s 调度协程异常退出", chat_id)
    finally:
        logger.info("[proactive] chat=%s 调度协程退出", chat_id)


# =====================================================================
# 生命周期与打断
# =====================================================================
async def start_proactive_scheduler() -> None:
    """应用启动时初始化调度器（chat 在首次用户活动时才被跟踪）。"""
    if not PROACTIVE_ENABLED:
        logger.info("[proactive] 主动唤醒已通过 PROACTIVE_ENABLED=false 关闭")
        return
    _stop_event.clear()
    logger.info(
        "[proactive] 调度器启动：idle_start=%ss intervals=%s max_idle=%ss rest=%ss",
        PROACTIVE_IDLE_START_SECONDS, PROACTIVE_INTERVAL_CHOICES,
        PROACTIVE_MAX_IDLE_SECONDS, PROACTIVE_REST_SECONDS,
    )


async def stop_proactive_scheduler() -> None:
    """应用关停：取消所有调度协程与进行中的 TIMER 回合（不撤回消息）。"""
    _stop_event.set()
    tasks: list[asyncio.Task] = []
    async with _schedules_lock:
        for sched in _schedules.values():
            if sched.task and not sched.task.done():
                sched.task.cancel()
                tasks.append(sched.task)
    for flow in list(_active_flows.values()):
        if flow.task and not flow.task.done():
            flow.task.cancel()
            tasks.append(flow.task)
    if tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("[proactive] 关停时部分任务未及时结束")
    logger.info("[proactive] 调度器已停止")


async def interrupt_proactive_flow(chat_id: int) -> bool:
    """用户发来新消息：打断进行中的 TIMER 回合并静默撤回其普通消息。

    - 取消后台 agent 任务（含工具调用）；
    - 等待在途发送完成注册后，逐条删除该回合发出的普通消息；
    - 全程静默：不发送"已停止"之类的任何提示。

    返回 True 表示确实打断了一个进行中的回合。
    """
    flow = _active_flows.get(chat_id)
    if flow is None:
        return False

    logger.info("[proactive] chat=%s 用户消息打断 TIMER 回合，开始静默撤回", chat_id)

    # 1) 标记打断：此后落地的新消息会立即自清理
    flow.interrupted = True

    # 2) 取消回合任务（工具调用里的 CancelledError 会正常向上传播）
    task = flow.task
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("[proactive] chat=%s TIMER 回合取消超时（>2s），继续撤回", chat_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("[proactive] chat=%s TIMER 回合取消时出现异常", chat_id, exc_info=True)

    # 3) 等待 shield 保护的在途发送完成注册，确保撤回不漏
    await _drain_pending_sends(flow, timeout=2.0)

    # 4) 静默撤回该回合发出的全部普通消息
    recalled = 0
    for mid in list(flow.sent_message_ids):
        if await _recall_message_quietly(chat_id, mid):
            recalled += 1
    _forget_proactive_messages(chat_id, flow.sent_message_ids)
    flow.sent_message_ids.clear()

    # 5) 注销回合
    if _active_flows.get(chat_id) is flow:
        _active_flows.pop(chat_id, None)

    logger.info("[proactive] chat=%s TIMER 回合已打断，静默撤回 %s 条消息", chat_id, recalled)
    return True
