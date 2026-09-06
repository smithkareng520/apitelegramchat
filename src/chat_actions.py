"""Telegram sendChatAction 状态指示器的集中管理。

语义约定（严格对齐 https://core.telegram.org/bots/api#sendchataction）：
chat action 描述的是 **bot 自己** 正在对 chat 做的动作，绝不用于描述用户
上传了什么。因此旧版在消息入口处按「用户发送的媒体类型」回发
upload_photo / upload_voice / upload_document / upload_video 的做法全部
移除——那些动作会被 Telegram 客户端渲染成“bot 正在上传照片/语音/…”，
与真实语义（用户在上传）完全相反。

本项目仅允许出现以下五个动作，且只允许在下列位置触发，其他位置一律
不使用：

  typing          模型流式输出期间（reasoning / content 字段增量真实到达，
                  即“模型正在打字”）。非流式返回（一次拿到完整文本）不触发
                  ——那等价于模型把文本直接粘贴发送，没有输入过程。
  record_video    视频生成过程：generate_video 工具调用生视频模型，或原生
                  视频模型（_agentic_loop_native_video）自身的生成阶段。
  upload_video    bot 使用「发送视频」方法时。仅限两类位置：a) 原生视频
                  模型路径（_agentic_loop_native_video）——模型输出就是
                  最终要发给用户的视频，其下载 / R2 上传 / 发送全程属于
                  发送动作；b) 携带 <video> 的永久消息发送（utils.send_
                  rich_html_message 钩子）。generate_video 工具自身的下载 /
                  R2 上传不触发——工具结果是 AI 收到的信息，不是 bot 在
                  发送视频。
  upload_document bot 使用「发送文件」方法时（present_files → sendDocument）。
  find_location   模型调用查找位置类方法时（amap 地图工具族）。

时长与循环：sendChatAction 的状态在客户端最多显示约 5 秒；超过 5 秒的
操作必须循环重发。本模块以 4 秒为周期重发（留 1 秒余量避免闪烁断档）。

状态被消息清除：bot 发出任何消息（含 sendRichMessageDraft 草稿刷新）后
状态指示会被 Telegram 自动清除。这是 Telegram 的既定行为，无需也不应
规避：/show on 时草稿流式本身就是“模型正在输入”的可视化，状态被草稿
刷新反复清除属于预期设计（typing 循环每 4 秒补发一次，用户仍能周期性
看到“正在输入”指示）。

实现要点：
  - 每 chat 一个状态对象，引用计数管理并发作用域（同一批次并行调用
    多个地图工具时 find_location 只显示一条，全部结束才熄灭）；
  - 同一 chat 同时只显示一个动作：引用计数最高者优先，同计数时后
    开始的优先（反映最新进行的工作）；
  - 新回合开始时 reset（清空引用并熄灭指示），轮次收尾/异常/打断路径
    兜底 stop_all —— 三重防线保证后台重发任务绝不泄漏；
  - 所有公开入口对 chat_id=None / 非法 action 均静默降级，绝不影响
    主流程。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

from utils import get_logger, send_chat_action

logger = get_logger(__name__)

# Telegram 的 chat action 状态最多持续约 5 秒；这里每 4 秒重发一次，
# 保证长任务（视频生成、文件上传、模型长输出）期间指示不闪断。
CHAT_ACTION_RESEND_INTERVAL = 4.0

# 本项目允许使用的全部 chat action（白名单）。新增动作必须先在此登记，
# 并在本模块 docstring 中写明允许的触发位置。
VALID_CHAT_ACTIONS = frozenset({
    "typing",          # 模型流式输出（思考/文本字段增量）期间
    "record_video",    # 视频生成过程（工具调用生视频模型 / 原生视频模型）
    "upload_video",    # bot 发送视频（仅原生视频模型路径与实际消息发送；
                       # generate_video 工具结果是 AI 收到信息，不触发）
    "upload_document", # bot 发送文件（sendDocument）
    "find_location",   # 模型调用查找位置类（amap 地图）工具
})


class _ChatActionState:
    """单个 chat 的 chat action 状态（引用计数 + 单一重发循环任务）。"""

    __slots__ = ("chat_id", "_refs", "_task", "_shown", "_lock")

    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        # action -> 活跃作用域计数。dict 保持插入序：计数归零再重启时
        # 重新插入尾部，使“同计数时后开始的优先”自然成立。
        self._refs: dict[str, int] = {}
        self._task: Optional[asyncio.Task] = None
        self._shown: Optional[str] = None
        self._lock = asyncio.Lock()

    # ---------- 内部：计算当前应显示的动作 ----------
    def _desired_action(self) -> Optional[str]:
        best_action: Optional[str] = None
        best_rc = 0
        for action, rc in self._refs.items():
            if rc > 0 and rc >= best_rc:
                best_action = action
                best_rc = rc
        return best_action

    # ---------- 内部：切换后台重发任务（必须在持锁状态下调用） ----------
    def _restart_task_locked(self, desired: Optional[str]) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._shown = desired
        if desired is None:
            return
        try:
            self._task = asyncio.create_task(self._run_loop(desired))
        except RuntimeError:
            # 没有运行中的事件循环（极端边缘，如解释器关闭）：降级放弃。
            self._task = None
            self._shown = None
            logger.debug(
                "chat action 无法启动重发循环（无事件循环）: chat=%s action=%s",
                self.chat_id, desired,
            )

    async def _run_loop(self, action: str) -> None:
        """立即发送一次，随后每 4 秒重发，直到被取消。

        send_chat_action 内部已捕获全部异常并自带 5 秒超时，这里只需
        防御循环体自身的意外错误，绝不让异常悄悄终结循环。
        """
        try:
            while True:
                await send_chat_action(self.chat_id, action)
                await asyncio.sleep(CHAT_ACTION_RESEND_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "chat action 重发循环意外退出: chat=%s action=%s", self.chat_id, action,
            )

    # ---------- 对外 ----------
    async def start(self, action: str) -> None:
        async with self._lock:
            rc = self._refs.get(action, 0) + 1
            # 归零重启的动作重新插入到 dict 尾部（最新开始的优先显示）。
            self._refs.pop(action, None)
            self._refs[action] = rc
            desired = self._desired_action()
            if desired != self._shown:
                self._restart_task_locked(desired)

    async def stop(self, action: str) -> None:
        async with self._lock:
            rc = self._refs.get(action, 0)
            if rc <= 0:
                # 未登记的停止请求（例如 start 因 chat_id 校验被跳过后的
                # 对称调用，或重复 stop）：幂等忽略。
                return
            if rc == 1:
                self._refs.pop(action, None)
            else:
                self._refs[action] = rc - 1
            desired = self._desired_action()
            if desired != self._shown:
                self._restart_task_locked(desired)

    async def stop_all(self) -> None:
        async with self._lock:
            if not self._refs and self._shown is None:
                return
            self._refs.clear()
            self._restart_task_locked(None)


# ---------------- 全局注册表：chat_id -> 状态 ----------------
_states: dict[int, _ChatActionState] = {}


def _get_state(chat_id: int) -> _ChatActionState:
    state = _states.get(chat_id)
    if state is None:
        state = _ChatActionState(chat_id)
        _states[chat_id] = state
    return state


def _validate(chat_id: Optional[int], action: str) -> bool:
    if chat_id is None:
        return False
    if action not in VALID_CHAT_ACTIONS:
        # 白名单之外的动作一律拒绝：这是“只用于指定位置”的硬约束。
        logger.warning("chat action 被拒绝（不在白名单）: chat=%s action=%r", chat_id, action)
        return False
    return True


async def start_chat_action(chat_id: int, action: str) -> None:
    """开始（或加入）一个 chat action 作用域。

    幂等安全：同一动作的并发作用域会累计引用计数，只显示一条状态；
    chat_id 非法 / 动作不在白名单时静默跳过，绝不影响调用方主流程。
    """
    try:
        if not _validate(chat_id, action):
            return
        await _get_state(chat_id).start(action)
    except Exception:
        logger.debug("start_chat_action 异常（忽略）", exc_info=True)


async def stop_chat_action(chat_id: int, action: str) -> None:
    """结束一个 chat action 作用域（引用计数减一，归零熄灭）。"""
    try:
        if chat_id is None or action not in VALID_CHAT_ACTIONS:
            return
        state = _states.get(chat_id)
        if state is None:
            return
        await state.stop(action)
    except Exception:
        logger.debug("stop_chat_action 异常（忽略）", exc_info=True)


async def stop_all_chat_actions(chat_id: int) -> None:
    """熄灭该 chat 的全部 chat action（轮次收尾 / 异常路径兜底）。"""
    try:
        if chat_id is None:
            return
        state = _states.get(chat_id)
        if state is None:
            return
        await state.stop_all()
    except Exception:
        logger.debug("stop_all_chat_actions 异常（忽略）", exc_info=True)


async def reset_chat_actions(chat_id: int) -> None:
    """新回合开始时清场：清空引用并熄灭指示。

    场景：上一回合被取消/打断时，若取消信号恰好落在作用域收尾的
    await 之间，引用可能泄漏、重发任务可能残留。新回合必然意味着
    旧回合已彻底结束（app._interrupt_active_generation 会先等待旧任务
    退出），此时无论残余状态如何一律清空，确保指示绝不跨回合存活。
    """
    try:
        if chat_id is None:
            return
        state = _states.get(chat_id)
        if state is None:
            return
        await state.stop_all()
    except Exception:
        logger.debug("reset_chat_actions 异常（忽略）", exc_info=True)


@asynccontextmanager
async def chat_action_scope(chat_id: int, action: str) -> AsyncIterator[None]:
    """以 async with 语法包裹一段“bot 正在做某动作”的操作。

    用法::

        async with chat_action_scope(chat_id, "find_location"):
            return await execute_geocode(address)

    进入时开始（引用 +1 并按需启动 4 秒重发循环），退出时结束（引用 -1，
    归零熄灭）。异常与取消（CancelledError）同样会走 finally 收尾，
    不会留下孤儿循环；即便二次取消打断了收尾，新回合开始时的
    reset_chat_actions 仍会兜底清场。
    """
    started = _validate(chat_id, action)
    if started:
        await start_chat_action(chat_id, action)
    try:
        yield
    finally:
        if started:
            await stop_chat_action(chat_id, action)
