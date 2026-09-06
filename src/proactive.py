# proactive.py
"""统一上下文的主动唤醒（TIMER 事件源）调度器。

背景与设计
==========

本模块让 Agent 拥有"自己的活动时间"：像人一样不定期地被唤醒；每次唤醒
与用户主动发消息（USER 事件源）共用同一份会话历史（统一上下文）。

调度节奏（本轮重构：事件驱动单 timer 模型）
==========================================

核心规则（简单、无需复杂计算）：

- 一开始就随机 5~20min 布置第一次唤醒（没有"空闲启动阈值"）；
- timer 到点触发一次 TIMER 回合；**回合结束后**再随机 5~20min 布置下一次；
- 用户发送任何消息：先取消挂起的 timer（不提前触发），等当前 agent 回合
  （含被打断后续上的 USER 回合）完整结束后，再随机 5~20min 布置下一次；
- 若 2 小时内用户都没有主动发过消息：暂停 1 小时再触发，之后继续保持
  "每 1h 看一眼"的慢节奏（用户一回来就恢复正常 5~20min）；
- ``/clear`` 后：timer 重置为随机 5~20min 下一次。

打断链路：用户消息打断进行中的 TIMER 回合时，先取消后台任务并经
turn_recovery 保全已完成的进度，随后 USER 回合正常续上；该回合结束时
再随机 5~20min 布置下一次——即"打断不影响节奏，只重算下一次"。

白名单与媒体模型隔离
====================

- 通过 ``register_authorized_check`` 注册的回调，在 ``note_user_activity``
  与 ``_fire_turn`` 两个入口都做白名单二次校验：非授权 chat 永远不会被
  创建 timer 任务、永远不会触发 TIMER 回合——这意味着即使按钮回调绕过了
  上层 ``is_authorized`` 检查，非白名单用户也收不到任何 TIMER 主动消息。
  不同 chat 的 timer 完全独立（``_schedules`` 以 chat_id 为 key），多用户
  共用同一 bot 时互不干扰。用户被移出白名单后：已挂起的 timer 触发时会被
  ``_fire_turn`` 的白名单门禁拦截（SKIP_UNAUTHORIZED，不重排下一次，
  timer 自然死亡）；用户重新入白名单并产生任意活动后调度自动恢复。
- 用户屏蔽/封禁 bot（Telegram 对该 chat 返回 403 Forbidden 类永久错误）
  时的熔断：发送层（utils 的各发送函数）识别到 403/"chat not found"
  等永久性错误后调用 ``notify_chat_unreachable``——停用该 chat 的调度
  （取消挂起 timer/watch、移除 schedule），避免 TIMER 继续每 5~20min
  空转一轮完整 LLM 回合却永远送达不了。用户解除屏蔽并再次发消息/
  点按钮时，``note_user_activity`` 会清除不可达标记并自动恢复调度
  （一次活动即视为"用户回来了"；若实际上仍被屏蔽，下一轮 403 会再次
  熔断，自愈式收敛，最多浪费一轮）。
- 通过 ``register_media_model_check`` 注册的回调，在 ``_fire_turn`` 创建
  runner 任务之前判断当前模型是否为原生图片/视频生成模型：
  * 是 → 不创建 runner 任务、不调用 ``note_turn_finished``、不布置下一次；
    timer 自然"死亡"，等用户切换回对话型模型并通过任意活动（发消息 /
    点击按钮）触发 ``note_user_activity`` 后再恢复。
  * 否 → 正常运行 TIMER 回合，回合结束时按原节奏布置下一次。
  这一设计的关键：因为根本不产生 agent 回合，所以也不会触发"回合结束
  时重置"——timer 不会自递归调度，正好满足"媒体模型时段彻底静默"的需求。

实现要点
========

- 每个 chat 维护一个挂起的 timer 任务（``_timer_fires``）与一个用户事件
  监视协程（``_user_event_watcher``）；没有轮询状态机；
- 用户输入分两类：会启动 agent 回合的消息（回合结束时由
  ``note_turn_finished`` 布置下一次）与纯命令/按钮（不产生回合，由
  watcher 在短暂延迟后布置下一次）；
- 进行中的 TIMER 回合登记在 ``_active_flows``（chat_id -> task）：
  用于 busy 互斥与用户打断。

线程与并发模型
==============

- 所有状态由 ``_schedules_lock`` 保护；timer/watch 任务在持锁期间创建，
  在锁外运行；
- 回合被用户消息打断时取消任务并触发 turn_recovery 轮次日志保全
  （已完成的 assistant/tool 进度沉淀进历史），全程静默、不发任何
  "已停止"提示。
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
# 两次唤醒之间的随机间隔（默认 5~20min；像人一样不定期）
PROACTIVE_INTERVAL_MIN_SECONDS = _env_seconds("PROACTIVE_INTERVAL_MIN_SECONDS", 5 * 60)
PROACTIVE_INTERVAL_MAX_SECONDS = _env_seconds("PROACTIVE_INTERVAL_MAX_SECONDS", 20 * 60)
# 主动消息保护（【已废弃】：不再限制每日主动消息数，默认值改为大数=不生效；
# 保留变量名仅为兼容旧配置/旧导入，如需限流可显式设置较小值）
PROACTIVE_DAILY_MESSAGE_LIMIT = _env_seconds("PROACTIVE_DAILY_MESSAGE_LIMIT", 10 ** 9)
# 用户连续多久没发消息 → 暂停 1h 再触发（慢节奏：每 1h 看一眼）
PROACTIVE_MAX_IDLE_SECONDS = _env_seconds("PROACTIVE_MAX_IDLE_SECONDS", 2 * 3600)
# 暂停时长；暂停结束后触发一次（用户仍无回应则继续暂停）
PROACTIVE_REST_SECONDS = _env_seconds("PROACTIVE_REST_SECONDS", 3600)
# 用户事件后判断"是否会有 agent 回合接管"的观望窗口（纯命令/按钮输入
# 在此窗口后没有回合运行，就直接布置下一次唤醒）
_PROACTIVE_WATCH_DELAY = _env_seconds("PROACTIVE_WATCH_DELAY", 2, minimum=1)


# =====================================================================
# 唤醒提示词
# =====================================================================
WAKEUP_PROMPT = """
[系统后台唤醒｜TIMER 主动巡检回合]

你现在处于"主动巡检回合"。这不是一次普通的聊天请求，而是一次由你主动
发起对话、检查待办、跟进上下文的机会。系统已另行告知你当前草稿预览的
开关状态（/show）：开启时你的过程与最终回复对用户可见；静默时则由你
决定是否交付内容——把要告知的结论写成消息正文并调用 deliver_reply
且填写 send=true（系统会把该正文直接发送给用户）；TIMER 主动巡检回合
send 不填默认 false——send=false 或不调用，本轮都不会有任何消息送达，
系统不会代替你发送（与用户主动发消息的静默回合不同：那边不填默认
true、收尾有兜底）。注意：deliver_reply 必须通过 tool_calls API 真正
发起调用才有效，在正文里用文字声称"已通过 deliver_reply 发送"不会
产生任何效果。交付成功后不要再调用 deliver_reply，也不要输出
"已发送/已确认"之类的确认正文——用户已经收到。

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
# 调度器：事件驱动单 timer 模型
# =====================================================================
class _ChatSchedule:
    """单个 chat 的调度状态：最近用户活动 + 挂起的下一次唤醒计时。"""

    __slots__ = ("chat_id", "last_user_message", "timer_task", "watch_task")

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.last_user_message = time.monotonic()
        self.timer_task: Optional[asyncio.Task] = None
        self.watch_task: Optional[asyncio.Task] = None


_schedules: dict[int, _ChatSchedule] = {}
_schedules_lock = asyncio.Lock()
_stop_event = asyncio.Event()
# 熔断标记：bot 对该 chat 的发送出现 403 类永久性失败（用户屏蔽 bot /
# 账号注销 / 被踢出）。置位后停用该 chat 的主动唤醒调度；用户再次产生
# 真实活动（发消息、点按钮——能到达 webhook 本身就说明已解除屏蔽）时
# 在 note_user_activity 里清除并恢复调度。
_unreachable_chats: set[int] = set()

# 由 app.py 注册的回调：运行一个 TIMER 回合 / 判断 USER 流程是否进行中
_turn_runner_callback: Optional[Callable[[int], Awaitable[None]]] = None
_busy_check_callback: Optional[Callable[[int], bool]] = None
# 由 app.py 注册的回调：判断该 chat 当前是否为原生图片/视频生成模型
#   （native_image 或 native_video）。返回 True 时 _fire_turn 不会创建
#   runner 任务、不会布置下一次 timer（详见模块 docstring）。
_media_model_check_callback: Optional[Callable[[int], bool]] = None
# 由 app.py 注册的回调：判断该 chat_id 是否在白名单内。返回 False 时
#   note_user_activity 与 _fire_turn 都会直接返回，不会为该 chat 创建
#   任何 timer / 主动消息——保证非白名单用户永远收不到 TIMER 主动消息。
_authorized_check_callback: Optional[Callable[[int], bool]] = None


def is_chat_unreachable(chat_id: int) -> bool:
    """该 chat 是否因 403 类永久性发送失败被熔断（用户屏蔽 bot 等）。

    供发送失败后的上层（如 message_user 卡片发送失败）判断：向模型报告
    “用户收不到消息”而非笼统的发送异常，避免模型反复重试注定失败的
    message_user 调用。用户产生真实活动后由 note_user_activity 解除。
    """
    return chat_id in _unreachable_chats


def register_turn_runner(cb: Callable[[int], Awaitable[None]]) -> None:
    """注册 TIMER 回合执行器（app._handle_timer_wakeup）。"""
    global _turn_runner_callback
    _turn_runner_callback = cb


def register_busy_check(cb: Callable[[int], bool]) -> None:
    """注册"该 chat 是否有 USER 流程进行中"的判断回调。"""
    global _busy_check_callback
    _busy_check_callback = cb


def register_media_model_check(cb: Callable[[int], bool]) -> None:
    """注册"该 chat 当前模型是否为原生媒体生成模型"的判断回调。

    返回 True 时 ``_fire_turn`` 会跳过 runner 创建且**不布置下一次**——
    timer 自然死亡，等用户切换回对话型模型并产生任意活动（发消息或
    点击按钮触发 ``note_user_activity``）后再恢复调度。
    """
    global _media_model_check_callback
    _media_model_check_callback = cb


def register_authorized_check(cb: Callable[[int], bool]) -> None:
    """注册"该 chat 是否在白名单内"的判断回调。

    在 ``note_user_activity``（防止为非授权 chat 创建 timer）与
    ``_fire_turn``（防止已存在的 timer 触发后向非授权 chat 发消息）
    两个入口都做二次校验，保证非白名单用户永远收不到 TIMER 主动消息。
    """
    global _authorized_check_callback
    _authorized_check_callback = cb


def _is_chat_authorized(chat_id: int) -> bool:
    """该 chat 是否在白名单内（未注册回调或回调异常时按授权处理，
    以免白名单机制异常导致全量用户被静默）。"""
    try:
        if _authorized_check_callback is not None:
            return bool(_authorized_check_callback(chat_id))
    except Exception:
        logger.debug("[proactive] chat=%s authorized check 异常，按授权处理", chat_id, exc_info=True)
    return True


def _busy_now(chat_id: int) -> bool:
    """该 chat 当前是否有 agent 回合在运行（USER 或 TIMER）。"""
    if chat_id in _active_flows:
        return True
    try:
        if _busy_check_callback is not None:
            return bool(_busy_check_callback(chat_id))
    except Exception:
        logger.debug("[proactive] chat=%s busy check 失败，按空闲处理", chat_id, exc_info=True)
    return False


def _cancel_task(task: Optional[asyncio.Task]) -> None:
    if task is not None and not task.done():
        task.cancel()


def _disarm_locked(sched: _ChatSchedule) -> None:
    """取消挂起的唤醒计时（须持 _schedules_lock）。"""
    _cancel_task(sched.timer_task)
    sched.timer_task = None


def _cancel_watcher_locked(sched: _ChatSchedule) -> None:
    """取消用户事件观望协程（须持 _schedules_lock）。"""
    _cancel_task(sched.watch_task)
    sched.watch_task = None


def _next_delay(sched: _ChatSchedule) -> tuple[float, str]:
    """计算下一次唤醒延迟：2h 无用户消息 → 暂停 1h；否则随机 5~20min。"""
    idle = time.monotonic() - sched.last_user_message
    if idle >= PROACTIVE_MAX_IDLE_SECONDS:
        return float(PROACTIVE_REST_SECONDS), "rest"
    lo = min(PROACTIVE_INTERVAL_MIN_SECONDS, PROACTIVE_INTERVAL_MAX_SECONDS)
    hi = max(PROACTIVE_INTERVAL_MIN_SECONDS, PROACTIVE_INTERVAL_MAX_SECONDS)
    return float(random.randint(lo, hi)), "normal"


def _arm_next_locked(sched: _ChatSchedule) -> None:
    """布置该 chat 的下一次唤醒（须持 _schedules_lock）。"""
    _disarm_locked(sched)
    if _stop_event.is_set() or not PROACTIVE_ENABLED:
        return
    delay, mode = _next_delay(sched)
    sched.timer_task = asyncio.create_task(_timer_fires(sched.chat_id, delay, mode))
    logger.info(
        "[TIMER] chat=%s 下一次主动唤醒：%smin 后（%s）",
        sched.chat_id, max(1, round(delay / 60)),
        "暂停后看一眼" if mode == "rest" else "随机间隔",
    )


async def _timer_fires(chat_id: int, delay: float, mode: str) -> None:
    """挂起到点后触发一次 TIMER 回合；忙碌时静默改期。"""
    try:
        if await _sleep_or_stop(delay):
            return  # 调度器已停止
        if _stop_event.is_set():
            return
        # 到点：先把自己从"挂起 timer"注册里摘除——之后任何改期
        # （busy / 异常兜底）都不会把正在运行的自己取消。
        async with _schedules_lock:
            sched = _schedules.get(chat_id)
            if sched is None:
                return
            if sched.timer_task is not asyncio.current_task():
                return  # 已被 disarm / 替换
            sched.timer_task = None
        if _busy_now(chat_id):
            # 防御：正常时序下用户消息会把 timer 取消，这里只覆盖竞态窗口
            # （如 media group 聚合后启动回合并发的瞬间）。
            logger.info("[TIMER] chat=%s 到点但用户回合进行中，改期下一次", chat_id)
            async with _schedules_lock:
                sched = _schedules.get(chat_id)
                if sched is not None:
                    _arm_next_locked(sched)
            return
        await _fire_turn(chat_id)
        # _fire_turn 创建回合任务后立即返回；回合完整结束时由 _run 的
        # finally 调 note_turn_finished 布置下一次，被用户消息打断则不改期
        # （随后接管的新 USER 回合结束时再布置）。
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[proactive] chat=%s timer 任务异常", chat_id)
        async with _schedules_lock:
            sched = _schedules.get(chat_id)
            if sched is not None:
                _arm_next_locked(sched)


async def _user_event_watcher(chat_id: int) -> None:
    """用户事件后的观望协程：没有 agent 回合接管时布置下一次唤醒。

    - 会启动 agent 回合的用户消息：回合结束时由 note_turn_finished 布置，
      watcher 检测到回合在运行就直接退出（不重复布置）；
    - 纯命令 / 按钮输入（不产生回合）：观望窗口过后直接布置下一次
      随机 5~20min——用户刚刚活动过（在线），正常节奏继续。
    """
    try:
        if await _sleep_or_stop(_PROACTIVE_WATCH_DELAY):
            return
        if _stop_event.is_set():
            return
        sched = _schedules.get(chat_id)
        if sched is None or sched.watch_task is not asyncio.current_task():
            return  # 已被更新的用户事件替换
        if _busy_now(chat_id):
            return  # 有回合在运行：等它结束时布置
        async with _schedules_lock:
            sched = _schedules.get(chat_id)
            if sched is None:
                return
            _cancel_watcher_locked(sched)
            _arm_next_locked(sched)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("[proactive] chat=%s watcher 异常（可忽略）", chat_id, exc_info=True)


async def note_user_activity(chat_id: int, *, private: bool = True) -> None:
    """记录一次用户活动，并挂起主动唤醒计时。

    - 更新"最近用户消息"时间戳（慢节奏判断依据：2h 无消息 → 暂停 1h）；
    - 取消挂起的 timer（用户发消息后不提前触发，等当前 agent 回合结束后
      再随机 5~20min 下一次）；
    - 启动/重启观望协程：若短暂延迟后没有 agent 回合接管（纯命令/按钮
      输入），直接布置下一次；
    - 首次见到私聊 chat：一开始就随机 5~20min 布置第一次唤醒。

    任何授权用户消息（含命令、按钮点击）都算活动。群聊不参与主动唤醒。
    非白名单 chat 直接返回：不为它创建任何 timer，保证非授权用户永远
    收不到 TIMER 主动消息（即使按钮回调绕过了上层 is_authorized）。

    恢复语义：用户的活动能到达 webhook，本身就说明此前的"不可达"
    （屏蔽/封禁）已被解除——清除熔断标记并正常调度。若实际仍不可达
    （极端情况：消息能进来但 bot 发不出去），下一轮 TIMER 触发后发送
    会再次 403 熔断，最多浪费一轮，自愈式收敛。
    """
    if not PROACTIVE_ENABLED or chat_id is None:
        return
    if not private:
        return
    if not _is_chat_authorized(chat_id):
        logger.info("[proactive] chat=%s 非白名单 chat，跳过主动唤醒调度", chat_id)
        return
    if chat_id in _unreachable_chats:
        _unreachable_chats.discard(chat_id)
        logger.info(
            "[proactive] chat=%s 用户活动恢复可达（此前被熔断：bot 发送遇 403 类永久错误），"
            "清除熔断标记并恢复主动唤醒调度",
            chat_id,
        )
    now = time.monotonic()
    async with _schedules_lock:
        sched = _schedules.get(chat_id)
        if sched is None:
            sched = _ChatSchedule(chat_id)
            sched.last_user_message = now
            _schedules[chat_id] = sched
            _arm_next_locked(sched)  # 一开始就随机 5~20min
            logger.info("[proactive] chat=%s 开始跟踪用户活动（已布置第一次主动唤醒）", chat_id)
            return
        sched.last_user_message = now
        # 用户发消息：timer 先挂起；等回合结束（note_turn_finished）
        # 或观望窗口后无回合接管（watcher）再布置下一次。
        _disarm_locked(sched)
        _cancel_watcher_locked(sched)
        sched.watch_task = asyncio.create_task(_user_event_watcher(chat_id))


async def note_turn_finished(chat_id: int) -> None:
    """一个 agent 回合（USER 或 TIMER）完整结束：布置下一次唤醒。

    USER 回合结束由 app._cleanup_task 调用；TIMER 回合正常结束在
    _fire_turn 内部调用。回合被用户消息打断时不走这里（打断后会有新的
    USER 回合接管，由它的结束再布置）——即"打断只重算下一次，不丢节奏"。
    """
    async with _schedules_lock:
        sched = _schedules.get(chat_id)
        if sched is None:
            return
        _cancel_watcher_locked(sched)
        _arm_next_locked(sched)


async def reset_proactive_timer(chat_id: int) -> None:
    """重置该 chat 的唤醒节奏：立即布置随机 5~20min 的下一次。

    供 /clear 等语义边界使用：历史清空意味着重新开始，timer 也从头计。
    """
    async with _schedules_lock:
        sched = _schedules.get(chat_id)
        if sched is None:
            return
        sched.last_user_message = time.monotonic()
        _cancel_watcher_locked(sched)
        _arm_next_locked(sched)
        logger.info(
            "[proactive] chat=%s 唤醒计时已重置（随机 %s~%smin 下一次）",
            chat_id, PROACTIVE_INTERVAL_MIN_SECONDS // 60,
            PROACTIVE_INTERVAL_MAX_SECONDS // 60,
        )


async def deactivate_chat(chat_id: int, *, reason: str = "") -> None:
    """停止对某个 chat 的主动唤醒调度：取消挂起 timer/watch、移除 schedule。

    移除（pop）schedule 后，``note_turn_finished`` / ``reset_proactive_timer``
    都会因 schedule 不存在而直接返回，不会重新布置下一次——正在运行的
    回合自然收尾后调度即彻底停止；唯一的恢复入口是 ``note_user_activity``
    （用户真实活动 → 重建 schedule）。
    """
    async with _schedules_lock:
        sched = _schedules.pop(chat_id, None)
    if sched is not None:
        _cancel_task(sched.timer_task)
        _cancel_task(sched.watch_task)
    logger.info(
        "[proactive] chat=%s 已停用主动唤醒%s",
        chat_id, f"（{reason}）" if reason else "",
    )


async def notify_chat_unreachable(chat_id: int, reason: str = "") -> None:
    """发送层识别到 403 类永久性错误（用户屏蔽 bot / 账号注销 / 被踢出）后
    的熔断入口：停用该 chat 的主动唤醒调度。

    背景：白名单无法覆盖这一场景——用户仍在白名单里，但已把 bot 屏蔽，
    Telegram 对该 chat 的所有发送永久 403。若不熔断，TIMER 会每 5~20min
    照常触发一轮完整 LLM 回合（白白消耗 token），且所有送达永远失败，
    形成无限空转循环。此处 pop 掉 schedule 后：进行中的回合照常结束，
    但 ``note_turn_finished`` 找不到 schedule，不再重排下一次；用户解除
    屏蔽并产生任意活动时由 ``note_user_activity`` 自动恢复。

    幂等：重复调用安全（pop 无则 None；set 重复 add 无副作用）。
    """
    first_time = chat_id not in _unreachable_chats
    _unreachable_chats.add(chat_id)
    if first_time:
        logger.warning(
            "[proactive] chat=%s 发送永久失败（%s）——判定 chat 不可达"
            "（用户可能已屏蔽 bot），熔断主动唤醒；用户回来后会自动恢复",
            chat_id, reason or "403 Forbidden",
        )
    await deactivate_chat(chat_id, reason=reason or "chat unreachable")


async def _sleep_or_stop(seconds: float) -> bool:
    """睡眠指定秒数；调度器停止时提前返回 True；被取消时抛 CancelledError。"""
    try:
        await asyncio.wait_for(_stop_event.wait(), timeout=max(0.05, seconds))
        return True
    except asyncio.TimeoutError:
        return False


async def _fire_turn(chat_id: int) -> None:
    """触发一次 TIMER 唤醒回合；回合正常结束后布置下一次。

    两道前置门禁（都在创建 runner 任务之前，因此不会触发
    ``note_turn_finished`` 的自递归重排）：
    1. 白名单校验：非授权 chat 直接返回，不创建任务、不重排下一次。
       这是 timer 已挂起但用户中途被移出白名单时的兜底。
    2. 媒体模型校验：当前模型为原生图片/视频生成模型时直接返回，
       不创建任务、不重排下一次——timer 自然死亡，等用户切换回对话型
       模型并产生任意活动（发消息 / 点击按钮触发 note_user_activity）
       后再恢复调度。
    """
    if _stop_event.is_set():
        return
    if not _is_chat_authorized(chat_id):
        logger.info("[TIMER] chat=%s 决策=SKIP_UNAUTHORIZED：非白名单 chat，不触发主动唤醒", chat_id)
        return
    if chat_id in _active_flows:
        # 上一回合尚未结束（防御）：改期下一次
        logger.info("[proactive] chat=%s 上一个 TIMER 回合仍在进行，改期本次唤醒", chat_id)
        async with _schedules_lock:
            sched = _schedules.get(chat_id)
            if sched is not None:
                _arm_next_locked(sched)
        return
    try:
        if _busy_check_callback is not None and _busy_check_callback(chat_id):
            logger.info("[TIMER] chat=%s 决策=SKIP：用户回合进行中，改期下一次", chat_id)
            async with _schedules_lock:
                sched = _schedules.get(chat_id)
                if sched is not None:
                    _arm_next_locked(sched)
            return
    except Exception:
        logger.debug("[proactive] chat=%s busy check 失败，按空闲处理", chat_id, exc_info=True)
    # 媒体模型门禁：原生图片/视频模型不适合后台回合（会直接生成媒体并推
    # 给用户）。返回前不调用 note_turn_finished，timer 不会自递归下一次；
    # 用户切换回对话型模型并产生任意活动后由 note_user_activity 恢复。
    try:
        if _media_model_check_callback is not None and _media_model_check_callback(chat_id):
            logger.info(
                "[TIMER] chat=%s 决策=SKIP_MEDIA：当前模型为原生图片/视频生成模型，"
                "不进行后台唤醒回合；不重排下一次（等用户切换模型后由其活动恢复）",
                chat_id,
            )
            return
    except Exception:
        logger.debug("[proactive] chat=%s media model check 异常，按正常处理", chat_id, exc_info=True)
    runner = _turn_runner_callback
    if runner is None:
        return

    async def _run() -> None:
        interrupted = False
        try:
            await runner(chat_id)
        except asyncio.CancelledError:
            # 被用户消息打断：不在这里布置下一次——打断后会有新的 USER
            # 回合接管，由它的结束（note_turn_finished）再布置。
            interrupted = True
            raise
        except Exception:
            logger.exception("[proactive] chat=%s TIMER 回合异常", chat_id)
        finally:
            if _active_flows.get(chat_id) is task:
                _active_flows.pop(chat_id, None)
            if not interrupted and not _stop_event.is_set():
                # 回合完整结束（含异常收尾）：随机 5~20min（或慢节奏 1h）下一次
                try:
                    await note_turn_finished(chat_id)
                except Exception:
                    logger.debug("[proactive] chat=%s 回合结束后布置下一次失败", chat_id, exc_info=True)

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
        "[proactive] 调度器启动（事件驱动单 timer 模型）：interval_range=%s-%ss "
        "max_idle=%ss rest=%ss",
        PROACTIVE_INTERVAL_MIN_SECONDS, PROACTIVE_INTERVAL_MAX_SECONDS,
        PROACTIVE_MAX_IDLE_SECONDS, PROACTIVE_REST_SECONDS,
    )


async def stop_proactive_scheduler() -> None:
    """应用关停：取消所有挂起的 timer/watch 任务与进行中的 TIMER 回合。"""
    _stop_event.set()
    tasks: list[asyncio.Task] = []
    async with _schedules_lock:
        for sched in _schedules.values():
            for t in (sched.timer_task, sched.watch_task):
                if t is not None and not t.done():
                    t.cancel()
                    tasks.append(t)
            sched.timer_task = None
            sched.watch_task = None
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
        import turn_recovery
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
