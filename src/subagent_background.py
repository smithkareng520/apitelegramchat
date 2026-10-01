"""后台子 agent：启动 → 句柄 → 查询/停止，完成通知「就近搭车」。

与 bash 的后台任务（bash_background）同一套使用契约：父 agent 调用
subagent 时 ``run_in_background=true`` 立即拿到任务句柄、回合内不再
阻塞；``task_action``（status / output / stop / list）查询或停止既有任务；
任务终态时把摘要推入每 chat 的待送队列，下一次真正调用模型的请求把它作为
一条尾部 system 消息带给父 agent（drain 入口见 ai.agentic_loops，队列本体
直接复用 bash_background.push_completion_notice，两类后台任务共用一个
队列、一次 drain）。

与 bash 后台任务的差异——子 agent 是进程内协程而不是操作系统进程：

- **无进程组 / 无落盘**：任务就是一个模块级 asyncio.Task，状态只存内存。
  应用重启即丢失（shutdown_all 取消全部，且不推通知）——子 agent 不可
  断点续跑，也没有可恢复的中间产物，重启后查询旧 task_id 会得到
  「未找到任务」。
- **防误杀**：runner 是模块级任务并被 ``_RUNNERS`` 强引用持有，父回合的
  取消（用户插话 / 新消息打断）不会传播到它；只有 stop 与应用关闭会取消。
- **超时**：沿用 subagent 自身的 ``timeout`` 参数（默认 900s，最大 1800s）
  作为寿命上限，由 execute_subagent 内部强制；这点与 bash 后台模式
  （忽略 timeout）不同。
- **进度**：不再向聊天里的工具卡片推实时预览（卡片在启动调用返回时即
  定稿），只把最近一条进度文本存进任务，供 status / output 查询。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from bash_background import _first_line, _fmt_duration, push_completion_notice

logger = logging.getLogger(__name__)

# ===================== 常量（env 可调） =====================
# 每个 chat 同时运行的后台子 agent 上限：每个子 agent 各自循环调用 LLM 与
# 工具，无上限会把 LLM 配额与全局工具信号量吃满。
SUBAGENT_BG_MAX_PER_CHAT = max(1, int(os.getenv("SUBAGENT_BG_MAX_PER_CHAT", "3")))
# 每个 (chat_id, namespace) 保留的已终结任务数：超出部分（按 finished_at
# 从旧到新）从内存里回收，运行中任务不受影响。
SUBAGENT_BG_FINISHED_RETENTION = max(1, int(os.getenv("SUBAGENT_BG_FINISHED_RETENTION", "20")))
# 完成通知里答复正文的字符上限（通知搭车请求，必须克制；完整答复走
# task_action=output）。
NOTICE_ANSWER_MAX_CHARS = 3000
# stop 取消后等待 runner 收尾的宽限秒数。
STOP_GRACE_SEC = 5.0

_TERMINAL_STATUSES = frozenset({"done", "failed", "stopped", "expired"})
_ACTIONS = ("status", "output", "stop", "list")


@dataclass
class SubagentTask:
    task_id: str
    chat_id: int
    namespace: str
    task: str
    description: str
    started_at: float
    lifetime: float
    status: str = "running"  # running/done/failed/stopped/expired
    result: dict[str, Any] | None = None
    progress: str = ""
    finished_at: float | None = None
    runner: asyncio.Task[None] | None = None


# 注册表：(chat_id, namespace) -> {task_id: SubagentTask}（仅内存）
_TASKS: dict[tuple[int, str], dict[str, SubagentTask]] = {}
# runner 防 GC 强引用集：父回合取消杀不掉模块级任务。
_RUNNERS: set[asyncio.Task[None]] = set()


# ===================== 格式化 =====================
def _label(task: SubagentTask) -> str:
    return task.description or _first_line(task.task, 40)


def _elapsed(task: SubagentTask) -> str:
    return _fmt_duration((task.finished_at or time.time()) - task.started_at)


def _result_stats(task: SubagentTask) -> str:
    result = task.result or {}
    return f"{result.get('rounds', 0)} 轮 · {result.get('tool_calls', 0)} 次工具调用"


def _answer_text(task: SubagentTask) -> str:
    return str((task.result or {}).get("answer") or "").strip()


def _error_text(task: SubagentTask) -> str:
    return str((task.result or {}).get("error") or "未知错误")


def _format_notice(task: SubagentTask) -> str:
    """终态通知文本：首行即摘要（emoji + 任务 + 结果）。"""
    name = f"后台子 agent {task.task_id}「{_label(task)}」"
    if task.status == "done":
        head = f"✅ {name}已完成，用时 {_elapsed(task)}（{_result_stats(task)}）"
    elif task.status == "expired":
        head = f"⌛ {name}超时被终止（上限 {task.lifetime:.0f}s）"
    else:  # failed
        head = f"❌ {name}失败，用时 {_elapsed(task)}：{_error_text(task)}"
    lines = [head, f"任务：{_first_line(task.task)}"]
    answer = _answer_text(task)
    if answer:
        if len(answer) > NOTICE_ANSWER_MAX_CHARS:
            answer = answer[:NOTICE_ANSWER_MAX_CHARS] + "…（已截断）"
        lines += ["答复：", answer]
    lines.append(f"（查看完整结果：subagent 工具 task_action=output task_id={task.task_id}）")
    return "\n".join(lines)


def _summarize_task(task: SubagentTask) -> str:
    desc = _label(task)
    if task.status == "running":
        return f"- {task.task_id} 运行中（已 {_elapsed(task)}）「{desc}」"
    marks = {"done": "✅ 已完成", "failed": "❌ 失败", "stopped": "⏹ 已停止", "expired": "⌛ 超时终止"}
    return f"- {task.task_id} {marks.get(task.status, task.status)}，用时 {_elapsed(task)}「{desc}」"


def _listing(tasks: dict[str, SubagentTask]) -> str:
    return "\n".join(_summarize_task(t) for t in tasks.values()) or "（无任务）"


# ===================== 终态与回收 =====================
def _prune_finished(chat_id: int, namespace: str) -> None:
    tasks = _TASKS.get((chat_id, namespace))
    if not tasks:
        return
    finished = [t for t in tasks.values() if t.status in _TERMINAL_STATUSES]
    overflow = len(finished) - SUBAGENT_BG_FINISHED_RETENTION
    if overflow <= 0:
        return
    finished.sort(key=lambda t: t.finished_at or 0.0)
    for old in finished[:overflow]:
        tasks.pop(old.task_id, None)


def _finish_task(
    task: SubagentTask,
    status: str,
    result: dict[str, Any] | None = None,
    notify: bool = True,
) -> None:
    """记录终态 + 入队通知。幂等：runner 自然结束与 stop 可能竞争，先到者胜出。

    notify=False 用于模型主动 stop（结果同步返回给模型，无需再通知）。
    """
    if task.status in _TERMINAL_STATUSES:
        return
    task.status = status
    task.result = result
    task.finished_at = time.time()
    if notify:
        push_completion_notice(task.chat_id, task.namespace, _format_notice(task))
    _prune_finished(task.chat_id, task.namespace)


# ===================== runner =====================
async def _run_subagent(task: SubagentTask, params: dict[str, Any]) -> None:
    # 延迟导入：subagent_tool 与 ai.* 存在模块级循环依赖链。
    from subagent_tool import execute_subagent

    async def _record_progress(text: str) -> None:
        task.progress = text

    try:
        raw = await execute_subagent(progress_callback=_record_progress, **params)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("子 agent 返回了非对象结果")
    except asyncio.CancelledError:
        # stop / 应用关闭：终态由取消方记录（关闭时刻意不记、不通知）。
        raise
    except Exception as exc:
        logger.exception("后台子 agent %s 异常", task.task_id)
        payload = {"ok": False, "error": f"子 agent 异常：{str(exc)[:200]}", "code": "exception"}

    if payload.get("ok"):
        status = "done"
    elif payload.get("code") == "timeout" or "整体超时" in str(payload.get("error") or ""):
        status = "expired"
    else:
        status = "failed"
    _finish_task(task, status, payload)


# ===================== 启动 =====================
async def start_background_task(
    chat_id: int,
    namespace: str,
    *,
    task: str,
    context: str | None = None,
    model: str | None = None,
    allowed_tools: list[str] | None = None,
    timeout: int | None = None,
    description: str = "",
) -> str:
    """启动后台子 agent 并立即返回句柄（文本，首行自带状态徽标）。"""
    task_text = (task or "").strip()
    if not task_text:
        return "Error: task is required"
    from subagent_tool import DEFAULT_TIMEOUT

    tasks = _TASKS.setdefault((chat_id, namespace), {})
    running = [t for t in tasks.values() if t.status == "running"]
    # 上限是护栏而非硬不变量：计数与创建之间没有 await，同一事件循环内
    # 不会被并发启动超越。
    if len(running) >= SUBAGENT_BG_MAX_PER_CHAT:
        lines = [f"Error: 后台子 agent 数量已达上限（每 chat {SUBAGENT_BG_MAX_PER_CHAT} 个运行中）。当前任务："]
        lines += [_summarize_task(t) for t in running]
        lines.append("可先 task_action=stop 停止不需要的任务，或等待其完成后再启动。")
        return "\n".join(lines)

    timeout_s = max(60, min(int(timeout or DEFAULT_TIMEOUT), 1800))
    task_id = f"sa-{uuid.uuid4().hex[:8]}"
    record = SubagentTask(
        task_id=task_id,
        chat_id=chat_id,
        namespace=namespace,
        task=task_text,
        description=str(description or ""),
        started_at=time.time(),
        lifetime=float(timeout_s),
    )
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "task": task_text,
        "context": context,
        "model": model,
        "allowed_tools": allowed_tools,
        "timeout": timeout_s,
    }
    tasks[task_id] = record
    runner = asyncio.create_task(_run_subagent(record, params), name=f"subagent-bg-{task_id}")
    record.runner = runner
    _RUNNERS.add(runner)
    runner.add_done_callback(_RUNNERS.discard)
    logger.info("后台子 agent 已启动 chat_id=%s task=%s", chat_id, task_id)

    return "\n".join([
        f"✅ 后台子 agent 已启动 {task_id}「{_label(record)}」",
        f"任务：{_first_line(task_text)}",
        f"超时上限：{timeout_s}s（到期强制终止）",
        "子 agent 独立运行：父回合结束、用户插话都不会影响它；应用重启会终止它。",
        "完成/失败/超时后，结果会自动随下一次 AI 请求带给本 agent，无需轮询。期间可随时查询：",
        f"  subagent task_action=status task_id={task_id}",
        f"  subagent task_action=output task_id={task_id}",
        f"  subagent task_action=stop task_id={task_id}",
        "  subagent task_action=list",
    ])


# ===================== 查询 / 停止 / 列表 =====================
def _resolve_task(
    chat_id: int, namespace: str, task_id: str | None, action: str,
) -> tuple[SubagentTask | None, str]:
    tasks = _TASKS.get((chat_id, namespace), {})
    if not task_id:
        return None, f"Error: task_action={action} 需要 task_id。当前任务：\n{_listing(tasks)}"
    found = tasks.get(task_id)
    if found is None:
        return None, f"Error: 未找到任务 {task_id}（应用重启后旧任务不会保留）。当前任务：\n{_listing(tasks)}"
    return found, ""


async def query_task(
    chat_id: int,
    namespace: str,
    action: str,
    task_id: str | None = None,
) -> str:
    action = str(action or "").strip().lower()
    if action not in _ACTIONS:
        return f"Error: 未知 task_action: {action}（可选 {'/'.join(_ACTIONS)}）"
    tasks = _TASKS.get((chat_id, namespace), {})

    if action == "list":
        running = sum(1 for t in tasks.values() if t.status == "running")
        return f"📋 后台子 agent 列表（运行中 {running} / 共 {len(tasks)}）：\n{_listing(tasks)}"

    task, err = _resolve_task(chat_id, namespace, task_id, action)
    if task is None:
        return err

    if action == "status":
        return _status_text(task)
    if action == "output":
        return _output_text(task)
    return await _stop(task)


def _status_text(task: SubagentTask) -> str:
    head = f"后台子 agent {task.task_id}"
    if task.status == "running":
        lines = [f"⏳ {head} 运行中（已 {_elapsed(task)} / 上限 {task.lifetime:.0f}s）"]
        if task.progress:
            lines.append(f"进度：{task.progress}")
    elif task.status == "done":
        lines = [f"✅ {head} 已完成：用时 {_elapsed(task)}（{_result_stats(task)}）"]
    elif task.status == "failed":
        lines = [f"❌ {head} 失败：{_error_text(task)}"]
    elif task.status == "expired":
        lines = [f"⌛ {head} 超时被终止（上限 {task.lifetime:.0f}s）"]
    else:
        lines = [f"⏹ {head} 已停止"]
    lines.append(f"任务：{_first_line(task.task)}")
    return "\n".join(lines)


def _output_text(task: SubagentTask) -> str:
    if task.status == "running":
        progress = task.progress or "（暂无进度）"
        return f"⏳ 后台子 agent {task.task_id} 仍在运行，尚无最终答复。最新进度：{progress}"
    if task.status == "stopped":
        return f"⏹ 后台子 agent {task.task_id} 已被停止，没有答复。"
    answer = _answer_text(task)
    if task.status == "done":
        head = f"📜 后台子 agent {task.task_id} 的答复（{_result_stats(task)}，用时 {_elapsed(task)}）："
        return f"{head}\n{answer or '（答复为空）'}"
    reason = "超时被终止" if task.status == "expired" else "失败"
    return f"❌ 后台子 agent {task.task_id} {reason}：{_error_text(task)}"


async def _stop(task: SubagentTask) -> str:
    if task.status != "running":
        return f"ℹ️ 后台子 agent {task.task_id} 已于先前结束（状态 {task.status}），无需停止。"
    runner = task.runner
    if runner is not None and not runner.done():
        runner.cancel()
        # asyncio.wait 不会重抛 runner 的取消/异常，且调用方自身被取消时
        # CancelledError 照常向上传播。
        _, pending = await asyncio.wait({runner}, timeout=STOP_GRACE_SEC)
        if pending:
            logger.warning("后台子 agent %s stop 后未如期退出", task.task_id)
    # runner 若在取消落地前恰好自然结束，_finish_task 的幂等守卫会保留其真实终态。
    _finish_task(task, "stopped", notify=False)
    if task.status != "stopped":
        return f"ℹ️ 后台子 agent {task.task_id} 在停止前已结束（状态 {task.status}）。"
    logger.info("后台子 agent 已停止 chat_id=%s task=%s", task.chat_id, task.task_id)
    return f"⏹ 后台子 agent {task.task_id} 已停止"


# ===================== 应用关闭 =====================
async def shutdown_all() -> None:
    """应用关闭时取消全部后台子 agent（不记终态、不推通知：进程即将退出）。"""
    runners = [r for r in list(_RUNNERS) if not r.done()]
    for runner in runners:
        runner.cancel()
    if runners:
        await asyncio.wait(runners, timeout=STOP_GRACE_SEC)
    _RUNNERS.clear()
    _TASKS.clear()
