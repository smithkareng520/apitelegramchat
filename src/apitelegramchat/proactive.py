# proactive.py
"""统一上下文的主动唤醒（TIMER 事件源）调度器。

背景与设计
==========

本模块让 Agent 拥有"自己的活动时间"：用户空闲约 20 分钟后，调度器开始像人
一样不定期地唤醒 Agent（随机 10 分钟到 1 小时）；每次唤醒与用户主动发消息
（USER 事件源）共用同一份会话历史（统一上下文）。

与旧版的区别（本轮重构）
========================

- ``send_message_to_user`` 工具已整体移除。TIMER 回合不再"完全静默 +
  纯文本消息撤回"那套特殊机制，而是与 USER 回合走**同一套草稿与交付流程**
  （见 ai_handlers.get_ai_response）：

  * /show on（默认）：TIMER 回合同样展示富文本草稿，最终回复经
    sendRichMessage 永久化送达；
  * /show off（静默模式）：过程不展示，模型通过 deliver_reply 自主选择
    是否交付最终内容（发送的是本轮最后一条助手消息正文，无需参数）；
    message_user 负责提问 / 主动留言（超时=用户不在）。

- TIMER 回合的合成 user 消息（WAKEUP_PROMPT）只进入本轮请求，**不写入
  持久历史**；回合产生的 assistant/tool 消息正常沉淀。回合被用户消息打断
  时，由 turn_recovery 的轮次日志机制保全已完成的进度（不再是"整轮丢弃"）。

调度节奏（均可用环境变量覆盖）
==============================

- 用户空闲 ``PROACTIVE_IDLE_START_SECONDS``（默认 1200s ≈ 20min，含 ±10% 抖动）
  后触发第一次唤醒；
- 之后每次唤醒间隔从 ``PROACTIVE_INTERVAL_MIN_SECONDS`` 到
  ``PROACTIVE_INTERVAL_MAX_SECONDS``（默认 10 分钟到 1 小时）随机选择；
- 用户连续 ``PROACTIVE_MAX_IDLE_SECONDS``（默认 10800s = 3h）没有发消息：
  停止高频触发，改为**休息 1 小时再触发一次**的慢节奏；
- 仅私聊参与主动唤醒；``PROACTIVE_ENABLED=false`` 可整体关闭。

线程与并发模型
==============

- 每个 chat 一个调度协程（``_chat_scheduler_loop``），轮询用户活动时间戳；
- 进行中的 TIMER 回合登记在 ``_active_flows``（chat_id -> task）：
  用于 busy 互斥与用户打断；回合被用户消息打断时取消任务并触发
  turn_recovery 轮次日志保全（已完成的 assistant/tool 进度沉淀进历史），
  全程静默、不发任何"已停止"提示。
"""

import asyncio
import logging
import os
import random
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


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


PROACTIVE_ENABLED = _env_flag("PROACTIVE_ENABLED", True)
# 用户空闲多久后开始"活动时间"（首次唤醒触发点）
PROACTIVE_IDLE_START_SECONDS = _env_seconds("PROACTIVE_IDLE_START_SECONDS", 20 * 60)
# 两次唤醒之间的随机间隔候选（像人一样不定期）
PROACTIVE_INTERVAL_MIN_SECONDS = _env_seconds("PROACTIVE_INTERVAL_MIN_SECONDS", 10 * 60)
PROACTIVE_INTERVAL_MAX_SECONDS = _env_seconds("PROACTIVE_INTERVAL_MAX_SECONDS", 60 * 60)
# 主动消息保护（【已废弃】：不再限制每日主动消息数，默认值改为大数=不生效；
# 保留变量名仅为兼容旧配置/旧导入，如需限流可显式设置较小值）
PROACTIVE_DAILY_MESSAGE_LIMIT = _env_seconds("PROACTIVE_DAILY_MESSAGE_LIMIT", 10 ** 9)
# 用户连续多久没发消息 → 停止高频触发
PROACTIVE_MAX_IDLE_SECONDS = _env_seconds("PROACTIVE_MAX_IDLE_SECONDS", 3 * 3600)
# 停止触发后的休息时长；休息结束后再触发一次（用户仍无回应则继续休息）
PROACTIVE_REST_SECONDS = _env_seconds("PROACTIVE_REST_SECONDS", 3600)
# 调度协程的轮询粒度
_PROACTIVE_POLL_SECONDS = _env_seconds("PROACTIVE_POLL_SECONDS", 10, minimum=1)


# =====================================================================
# 唤醒提示词
# =====================================================================
WAKEUP_PROMPT = """
[系统后台唤醒｜TIMER 主动巡检回合]

你现在处于"主动巡检回合"。这不是一次普通的聊天请求，而是一次由你主动
发起对话、检查待办、跟进上下文的机会。系统已另行告知你当前草稿预览的
开关状态（/show）：开启时你的过程与最终回复对用户可见；静默时则由你
决定是否交付内容——把要告知的结论写成消息正文并调用 deliver_reply
（无需参数，系统会把该正文直接发送给用户）；不调用则本轮不会有
任何消息送达，系统不会代替你发送。

按下面的优先级决定做什么、说什么：

1. 检查当前 Todo（第一优先级）
   - 调用 todo 的 list，检查未完成任务：已到期、临近到期、长期未推进、
     现在适合推进、或现在值得提醒的任务。
   - 任务明确且安全、且无需用户补充信息时，可以推进 Todo 状态；否则提醒用户。
   - 不要为了"有事可做"而虚构任务、修改无关任务或重复提醒已处理过的事。

2. 检查最近上下文
   - 回看最近对话，寻找未完成的话题、用户留下的待办、承诺、等待结果、计划
     和自然后续；有话到了"现在联系最自然"的时间点就顺着聊。
   - 可以（适度）用 web_search 查证与用户上下文相关的新信息再分享，但不要
     每个回合都变成新闻播报。

3. 没有要紧事 → 主动找话题聊（这是本回合的兜底动作，不是可选项）
   - 从最近聊天里挑一个用户表现出兴趣的点接着聊（爱好、近况、之前提过的事）；
   - 结合当下时间（早晨/午后/晚上/深夜/周几）自然开场；
   - 抛一个轻松、具体、开放式的小问题，或分享一个有趣的想法；
   - 像朋友随手发消息那样：短、自然、能让人想回一句；
   - 换着花样来，不要每次都用同一种开场方式，不要重复上一轮说过的话。

需要用户回应时（确认、选择、留言）用 message_user——它同时就是"给用户
发消息"的通道：用户回复了就继续；超时说明用户暂时不在，安静收尾即可。

消息风格（无论哪个优先级都适用）：
- 短、口语化、像真人主动想起一件事后随手发的消息；不要 Markdown。
- 不要解释"这是 TIMER/后台唤醒"，不要报告你的检查过程或决策过程。

禁止：
- 空洞的服务性套话："明白，我会注意……""有什么我可以帮您的吗？""我会等待您的消息。"
- 查完 Todo 和上下文之后什么都不发。
- 把内部推理、工具检查过程或"我决定不发"之类的说明发给用户。
- 编造用户没说过的承诺、时间或事实。
"""


# =====================================================================
# 回合注册表：进行中的 TIMER 回合（busy 互斥 + 打断句柄）
# =====================================================================
_active_flows: dict[int, asyncio.Task] = {}


def is_proactive_flow_active(chat_id: int) -> bool:
    """该 chat 是否有进行中的 TIMER 回合。"""
    return chat_id in _active_flows


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

    任何授权用户消息（含命令、按钮点击）都算活动。群聊不参与主动唤醒。
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


async def _wait_activity_or_stop(chat_id: int, timeout: float) -> str:
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

    async def _run() -> None:
        try:
            await runner(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[proactive] chat=%s TIMER 回合异常", chat_id)
        finally:
            if _active_flows.get(chat_id) is task:
                _active_flows.pop(chat_id, None)

    task = asyncio.create_task(_run())
    _active_flows[chat_id] = task

    def _log_unexpected(t: asyncio.Task, _cid: int = chat_id) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("[proactive] chat=%s TIMER 回合任务异常退出: %s", _cid, exc)

    task.add_done_callback(_log_unexpected)
    logger.info("[TIMER] chat=%s 决策=RUN：开始主动巡检（todo→context→info→chat）", chat_id)


async def _chat_scheduler_loop(chat_id: int) -> None:
    """单个 chat 的唤醒节奏状态机。

    阶段 1（空闲等待）：等用户空闲达到 idle_start（±10% 抖动）；
    阶段 2（活动时间）：立即触发首次唤醒，此后每隔随机 10~90min 唤醒一次；
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
                    # 人类化随机：大部分集中在较短关注窗口，少量延迟更久
                    roll = random.random()
                    if roll < 0.65:
                        interval = random.randint(PROACTIVE_INTERVAL_MIN_SECONDS, 30 * 60)
                    elif roll < 0.9:
                        interval = random.randint(30 * 60, PROACTIVE_INTERVAL_MAX_SECONDS)
                    else:
                        interval = random.randint(PROACTIVE_INTERVAL_MAX_SECONDS, 90 * 60)
                    logger.info(
                        "[TIMER] chat=%s 下一次巡检计划：%smin 后（human_random=true）",
                        chat_id, max(1, round(interval / 60)),
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
        "[proactive] 调度器启动：idle_start=%ss interval_range=%s-%ss max_idle=%ss rest=%ss",
        PROACTIVE_IDLE_START_SECONDS, PROACTIVE_INTERVAL_MIN_SECONDS,
        PROACTIVE_INTERVAL_MAX_SECONDS, PROACTIVE_MAX_IDLE_SECONDS,
        PROACTIVE_REST_SECONDS,
    )


async def stop_proactive_scheduler() -> None:
    """应用关停：取消所有调度协程与进行中的 TIMER 回合。"""
    _stop_event.set()
    tasks: list[asyncio.Task] = []
    async with _schedules_lock:
        for sched in _schedules.values():
            if sched.task and not sched.task.done():
                sched.task.cancel()
                tasks.append(sched.task)
    for task in list(_active_flows.values()):
        if not task.done():
            task.cancel()
            tasks.append(task)
    if tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("[proactive] 关停时部分任务未及时结束")
    logger.info("[proactive] 调度器已停止")


async def interrupt_proactive_flow(chat_id: int) -> bool:
    """用户发来新消息：打断进行中的 TIMER 回合并保全其进度。

    - 取消后台 agent 任务（含工具调用）；
    - 任务停止后通过 turn_recovery 保全该回合已完成的 assistant/tool
      进度（未配对的 tool_use 补占位结果），沉淀进统一上下文——
      下一个 USER 回合可直接从断点继续，而不是整轮作废；
    - 全程静默：不发送"已停止"之类的任何提示（该提示已随旧消息撤回
      机制一起移除）。

    返回 True 表示确实打断了一个进行中的回合。
    """
    task = _active_flows.get(chat_id)
    if task is None:
        return False

    logger.info("[proactive] chat=%s 用户消息打断 TIMER 回合，取消后台任务", chat_id)

    if not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("[proactive] chat=%s TIMER 回合取消超时（>2s），继续保全", chat_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("[proactive] chat=%s TIMER 回合取消时出现异常", chat_id, exc_info=True)

    # 旧任务已停止：轮次日志保全（只处理该任务对应的登记；
    # 仍在运行的 USER 回合登记不受影响）。
    try:
        from apitelegramchat import turn_recovery
        salvaged = await turn_recovery.finalize_interrupted_turn(
            chat_id, expect_task=task, reason="user-interrupt-timer",
        )
        logger.info(
            "[proactive] chat=%s TIMER 回合已打断，轮次进度保全 %s 条消息",
            chat_id, salvaged,
        )
    except Exception:
        logger.debug("[proactive] chat=%s 轮次保全失败（可忽略）", chat_id, exc_info=True)

    if _active_flows.get(chat_id) is task:
        _active_flows.pop(chat_id, None)
    return True
