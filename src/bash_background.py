"""后台 bash 任务：启动 → 句柄 → 查询/停止，完成通知「就近搭车」。

与交互会话（bash_session.BashSession）的关键差异——防误杀三保障：

- **独立进程组**（start_new_session=True）：交互会话超时的 killpg、
  ``restart=true``、heredoc 隔离执行的清理都按 pgid 杀自己的进程组，
  波及不到后台任务；
- **启动即返回句柄**：回合内没有任何 await 挂在后台进程上，用户插话
  触发的 ``task.cancel()`` 在协程 await 链上找不到传播路径；
- **monitor 为模块级任务 + 防 GC 引用集**（复刻 tool_call_loop 的
  ``_DETACHED_TASKS`` 模式）：轮次取消不传播到模块级任务，只有应用
  关闭（shutdown_all）会终止它们。

超时语义：后台任务**没有 idle 超时**（输出直接落盘，无读循环可卡），
只有寿命上限兜底（BASH_TASK_MAX_LIFETIME_SEC，默认 3600s）——
``timeout`` 参数是前台概念，后台模式忽略。

完成通知（缓存安全设计）：任务终态时把格式化摘要推入每 chat 的待送
队列；下一次**真正调用 AI 的请求**构建消息时 drain 并作为一条尾部
system 消息搭车发出（见 ai_handlers.get_ai_response）。历史只增不改：
启动调用的句柄结果写一次永不改写，通知不持久化——稳定前缀逐字节一致，
前缀缓存全额命中，分叉点恰在通知本身。

历史回收（BASH_TASK_FINISHED_RETENTION，默认 20）：每 (chat_id,
namespace) 只保留最近 N 个已终结任务，超出部分在下一次 _finish_task
或注册表恢复时连同 .json/.log/.exit 一起删除。只回收终态任务，运行中
任务不受影响——防止长期运行的 chat 反复起后台任务后，内存注册表与
磁盘 tasks 目录无限增长（task_action=list 也随之越列越长）。
"""

import asyncio
import functools
import json
import logging
import os
import re
import signal
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import workspace_paths
from sandbox import (
    _preexec_sandbox,
    build_sandbox_env,
    watchdog,
)

logger = logging.getLogger(__name__)

# ===================== 常量（env 可调） =====================
# 单个后台任务的寿命上限（秒）：到期无论输出与否强制 killpg。
BASH_TASK_MAX_LIFETIME_SEC = int(os.getenv("BASH_TASK_MAX_LIFETIME_SEC", "3600"))
# 每个 chat 同时运行的后台任务上限（防任务堆积吃满 SANDBOX_MAX_PROCS）。
BASH_TASK_MAX_PER_CHAT = int(os.getenv("BASH_TASK_MAX_PER_CHAT", "3"))
# stop 的优雅宽限：SIGTERM 后等待该秒数再 SIGKILL。
BASH_TASK_STOP_GRACE_SEC = float(os.getenv("BASH_TASK_STOP_GRACE_SEC", "5"))
# 完成通知里输出尾部的行数 / 字符上限（通知搭车请求，必须克制）。
NOTICE_TAIL_MAX_LINES = 30
NOTICE_TAIL_MAX_CHARS = 1500
# task_action=output 的尾部行数 / 字符上限。
QUERY_OUTPUT_TAIL_LINES = 50
QUERY_OUTPUT_TAIL_CHARS = 4000
# 孤儿进程探活轮询间隔。
_ORPHAN_POLL_INTERVAL_SEC = 5.0
# 每个 (chat_id, namespace) 保留的已终结任务上限：超出部分（按
# finished_at 从旧到新）连同磁盘文件一起清理。只回收终态任务，运行中
# 任务永不在此清理范围内。防止长期运行的 chat 反复起后台任务后，
# 内存注册表与磁盘 tasks 目录无限增长。
BASH_TASK_FINISHED_RETENTION = int(os.getenv("BASH_TASK_FINISHED_RETENTION", "20"))

_TERMINAL_STATUSES = frozenset({"done", "failed", "stopped", "expired", "lost"})
_TASK_ID_RE = re.compile(r"bg-[0-9a-f]{8}")


# ===================== 危险命令黑名单 =====================
# 自 BashSession._is_safe 抽出为模块级函数：后台任务启动与前台交互会话
# 共用同一份最小黑名单（设计原则不变：只拦极端灾难模式，其余靠沙箱兜底）。
_DANGEROUS_PATTERNS = [
    # rm -rf / 或 rm -rf /*
    (re.compile(r'\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*)\s+/(?:\s|$|\*)'),
     "rm -rf /"),
    # fork bomb
    (re.compile(r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'),
     "fork bomb"),
    # 写裸设备
    (re.compile(r'\bdd\s+if=\S+\s+of=/dev/(?!null|zero|random|urandom)'),
     "dd to raw device"),
    # mkfs 任意设备
    (re.compile(r'\bmkfs\.\w+\s+/dev/'),
     "mkfs on device"),
    # 写 /dev/mem /dev/kmem
    (re.compile(r'\bof=/dev/(mem|kmem|port)'),
     "write to kernel memory"),
    # :(){...} 的变体
    (re.compile(r'\.\s*\(\s*\)\s*\{'),
     "anonymous fork function"),
]


def _command_is_safe(command: str) -> bool:
    """最小黑名单，仅拦极端操作；其余靠 Landlock/rlimit 沙箱兜底。"""
    if not command or not command.strip():
        return False
    for pattern, name in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            logger.warning(f"🚫 Bash rejected ({name}): {command[:200]}")
            return False
    return True


# ===================== 任务数据结构 =====================
@dataclass
class BackgroundTask:
    task_id: str
    chat_id: int
    namespace: str
    command: str
    description: str
    cwd: str
    pid: int
    log_path: str
    state_path: str
    started_at: float
    lifetime: float
    # 以下字段在磁盘恢复的只读条目上为 None。
    proc: "asyncio.subprocess.Process | None" = None
    monitor: "asyncio.Task | None" = None
    watchdog: "asyncio.Task | None" = None
    status: str = "running"  # running/done/failed/stopped/expired/lost
    exit_code: int | None = None
    finished_at: float | None = None


# 注册表：(chat_id, namespace) -> {task_id: BackgroundTask}
_TASKS: dict[tuple[int, str], dict[str, BackgroundTask]] = {}
# 懒加载标记：tasks 目录只扫描一次（避免每次查询都遍历磁盘）。
_LOADED_KEYS: set[tuple[int, str]] = set()
# monitor / 孤儿探活任务防 GC 引用集：轮次取消杀不掉模块级任务。
_MONITOR_TASKS: set[asyncio.Task] = set()
# 完成通知待送队列：(chat_id, namespace) -> [notice, ...]。
# 由 ai_handlers.get_ai_response 在下一次 AI 请求构建消息时 drain。
_PENDING_NOTICES: dict[tuple[int, str], list[str]] = {}


# ===================== 磁盘状态 =====================
def _tasks_dir(chat_id: int, namespace: str) -> Path:
    return workspace_paths.runtime_cache_root(chat_id, namespace) / "tasks"


def _dump_state(task: BackgroundTask) -> None:
    """把任务元数据写入 .json（终态时随后补写 .exit 崩溃安全标记）。"""
    payload = {
        "task_id": task.task_id,
        "chat_id": task.chat_id,
        "namespace": task.namespace,
        "command": task.command,
        "description": task.description,
        "cwd": task.cwd,
        "pid": task.pid,
        "log_path": task.log_path,
        "started_at": task.started_at,
        "lifetime": task.lifetime,
        "status": task.status,
        "exit_code": task.exit_code,
        "finished_at": task.finished_at,
    }
    try:
        Path(task.state_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        logger.debug("后台任务状态写入失败", exc_info=True)


def _mark_terminal_on_disk(task: BackgroundTask) -> None:
    """终态落盘：.json 更新 + .exit 标记（顺序保证：先 json 后 exit，
    重启扫描以 .exit 的存在与否判定「终态已记录」还是「进程遗孤」。）"""
    _dump_state(task)
    try:
        Path(task.state_path).with_suffix(".exit").write_text(
            task.status, encoding="utf-8")
    except OSError:
        logger.debug("后台任务终态标记写入失败", exc_info=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pid_matches_command(pid: int, command: str) -> bool:
    """孤儿进程身份校验：/proc/<pid>/cmdline 必须仍含原命令片段。

    应用重启后 pid 可能被内核复用给无关进程——探活与终止前先核对身份，
    防止把 killpg 打到无辜进程组上。

    两种匹配形态都要试：bash -c "cmd" 对单条简单命令会 exec 优化直接
    替换自身，命令内的空格在 /proc/cmdline 里变成参数分隔符 NUL——
    "sleep 60" 的 cmdline 是 "sleep\x0060\x00"。只按原文（含空格）匹配
    会在 exec 后失配，把活着的孤儿误判为已死并错推「因重启被中止」。
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    needle = command.strip()[:32]
    if not needle:
        return False
    return needle in cmdline or needle.replace(" ", "\x00") in cmdline


def _signal_task_process(task: BackgroundTask, sig: int) -> bool:
    """向任务进程组发信号。后台进程以 start_new_session 启动，pid 即 pgid，
    killpg 连子进程一起覆盖。孤儿（proc 为 None）先做身份校验再发。"""
    if task.proc is not None:
        try:
            os.killpg(os.getpgid(task.proc.pid), sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    if not _pid_alive(task.pid) or not _pid_matches_command(task.pid, task.command):
        return False
    try:
        os.killpg(task.pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ===================== 输出尾部 =====================
def _tail_file(path: str | Path, max_lines: int, max_chars: int) -> str:
    """读取日志尾部（最多回看 256KB，避免大日志整读进内存）。"""
    p = Path(path)
    try:
        size = p.stat().st_size
        window = min(size, 262144)
        with p.open("rb") as f:
            f.seek(size - window)
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    truncated = window < size
    if truncated:
        # 窗口起点处多半是半行，丢弃到下一个换行
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    body = "\n".join(lines).strip("\n")
    if len(body) > max_chars:
        body = "…" + body[-max_chars:]
        truncated = True
    if truncated and body:
        body = f"...（更早输出已省略，完整内容见日志文件）\n{body}"
    return body


def _first_line(text: str, limit: int = 120) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    h, m2 = divmod(m, 60)
    return f"{h}h{m2:02d}m"


# ===================== 完成通知队列 =====================
# 单 chat 待送通知上限：长期没有 AI 请求时防无限堆积（丢最旧，任务状态
# 仍可随时 task_action=status 查询，不因通知丢弃而丢失信息）。
_PENDING_NOTICES_MAX_PER_CHAT = 20

# push / drain 并发防护：monitor 入队与请求侧 drain 当前同处一个事件
# 循环，但两侧对队列做的都是复合操作（setdefault+append / 检查+截断 /
# 整段取走）——一旦任何一侧被移入线程（如 to_thread 包装的请求构建），
# 无锁就会出现同一通知双发或丢失（drain 在 setdefault 与 append 之间
# pop 走队列，push 追加到已孤儿化的列表上→通知无声丢失）。每
# (chat_id, namespace) 一把 threading.Lock，粒度与队列键一致。
_NOTICE_LOCKS: dict[tuple[int, str], threading.Lock] = {}


def _notice_lock(chat_id: int, namespace: str) -> threading.Lock:
    """取（或惰性创建）该 chat 的通知队列锁。

    setdefault 保证跨线程并发创建时只有一把锁胜出——两个线程同时拿到
    None 都会走 setdefault，后写者返回先写者的实例，双方最终持有同一
    把锁，不存在“两把锁各自为政”的窗口。锁与队列同生命周期、不删除：
    删除会有“他线程正阻塞等待被删除的锁对象、新键又建了第二把锁”的
    分裂窗口，而锁数量上界与 chat 数同阶，无泄漏风险。
    """
    key = (chat_id, namespace)
    lock = _NOTICE_LOCKS.get(key)
    if lock is None:
        lock = _NOTICE_LOCKS.setdefault(key, threading.Lock())
    return lock


def push_completion_notice(chat_id: int, namespace: str, text: str) -> None:
    """任务终态通知入队（持锁），等待下一次 AI 请求搭车（见 drain_completion_notices）。"""
    dropped: str | None = None
    with _notice_lock(chat_id, namespace):
        queue = _PENDING_NOTICES.setdefault((chat_id, namespace), [])
        queue.append(text)
        if len(queue) > _PENDING_NOTICES_MAX_PER_CHAT:
            dropped = queue.pop(0)
    if dropped is not None:
        logger.warning(
            "后台任务通知队列溢出（chat=%s），丢弃最旧一条: %.80s", chat_id, dropped)


def drain_completion_notices(chat_id: int, namespace: str | None = None) -> list[str]:
    """原子「整段换空」：锁内一次性 pop 整个待送队列（发完即消费，不持久化）。

    并发语义：与 push 持同一把 per-chat 锁。锁外新到的 push 落入新建
    队列、留给下一次请求，既不丢也不重；锁内 pop 保证同一段通知只会
    被消费一次。懒加载在锁外执行——内部为死孤儿补推通知时会短暂持
    锁（同线程无重入，不构成死锁）。

    必须在事件循环内调用：内部懒加载可能为重启后的孤儿任务创建探活
    monitor。namespace 传 None 时与 dispatch 侧同一套 ContextVar 解析，
    保证队列键一致。
    """
    ns = workspace_paths.workspace_namespace(chat_id, namespace)
    _ensure_registry_loaded(chat_id, ns)
    with _notice_lock(chat_id, ns):
        queue = _PENDING_NOTICES.pop((chat_id, ns), None)
    return list(queue or [])


def _format_notice(task: BackgroundTask) -> str:
    """终态通知文本：首行即卡片摘要（emoji + 任务 + 结果），供
    format_tool_result 直接取第一行做工具卡 summary。"""
    desc = task.description or _first_line(task.command, 40)
    elapsed = _fmt_duration((task.finished_at or time.time()) - task.started_at)
    if task.status == "done":
        head = f"✅ 后台任务 {task.task_id}「{desc}」成功结束（exit code 0），用时 {elapsed}"
    elif task.status == "failed":
        head = f"❌ 后台任务 {task.task_id}「{desc}」失败结束（exit code {task.exit_code}），用时 {elapsed}"
    elif task.status == "expired":
        head = f"⌛ 后台任务 {task.task_id}「{desc}」超过寿命上限 {task.lifetime:.0f}s 被强制终止（exit code {task.exit_code}）"
    elif task.status == "stopped":
        head = f"⏹ 后台任务 {task.task_id}「{desc}」已被停止"
    else:  # lost
        head = f"❔ 后台任务 {task.task_id}「{desc}」已因重启被中止（退出码未知），用时 {elapsed}"
    lines = [head, f"命令：{_first_line(task.command)}"]
    tail = _tail_file(task.log_path, NOTICE_TAIL_MAX_LINES, NOTICE_TAIL_MAX_CHARS)
    if tail:
        lines += ["输出末尾：", tail]
    lines.append(f"（查看完整输出：bash 工具 task_action=output task_id={task.task_id}）")
    return "\n".join(lines)


# ===================== 注册表懒加载（重启恢复） =====================
def _ensure_registry_loaded(chat_id: int, namespace: str) -> None:
    """扫描 tasks 目录恢复注册表（每 key 一次）。

    以 .exit 标记为唯一判据三分（.json 无 .exit = 关机时还在跑）：
    - completed（有 .exit）：上一实例已记录终态，恢复为只读条目（可查
      状态/输出，不重发通知）。.exit 内容即终态——先写 .json 后写
      .exit 的顺序保证 .exit 存在时终态必定已落，即使 .json 写入失败
      （磁盘满等）也不退回 was-running 分支造成重复通知；
    - was-running（无 .exit）且进程仍活：应用崩溃式重启遗留的孤儿——
      重新纳入探活 monitor，存活期继续计时，仍可 stop；
    - was-running（无 .exit）且进程已死：关机时被杀或关机窗口内退出，
      进程组已死、退出码不可考 → 状态 lost 并补推「因重启被中止」
      通知——若只补「关机期间完成」的，这类运行中任务会无声消失。
    """
    key = (chat_id, namespace)
    if key in _LOADED_KEYS:
        return
    _LOADED_KEYS.add(key)
    tasks = _TASKS.setdefault(key, {})
    tdir = _tasks_dir(chat_id, namespace)
    if not tdir.is_dir():
        return
    for state_file in sorted(tdir.glob("*.json")):
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task_id = str(payload.get("task_id") or state_file.stem)
        if task_id in tasks:
            continue
        task = BackgroundTask(
            task_id=task_id,
            chat_id=chat_id,
            namespace=namespace,
            command=str(payload.get("command") or ""),
            description=str(payload.get("description") or ""),
            cwd=str(payload.get("cwd") or ""),
            pid=int(payload.get("pid") or 0),
            log_path=str(payload.get("log_path") or (tdir / f"{task_id}.log")),
            state_path=str(state_file),
            started_at=float(payload.get("started_at") or time.time()),
            lifetime=float(payload.get("lifetime") or BASH_TASK_MAX_LIFETIME_SEC),
            status=str(payload.get("status") or "running"),
        )
        tasks[task_id] = task
        exit_marker = state_file.with_suffix(".exit")
        try:
            exit_status = exit_marker.read_text(encoding="utf-8").strip()
        except OSError:
            exit_status = ""
        if exit_status in _TERMINAL_STATUSES:
            # completed：终态已由上一实例落盘（.exit 内容为权威）→ 只读恢复。
            task.status = exit_status
            task.exit_code = payload.get("exit_code")
            task.finished_at = payload.get("finished_at")
            continue
        if _pid_alive(task.pid) and _pid_matches_command(task.pid, task.command):
            # was-running 且进程仍活：重新纳入探活（剩余寿命继续计时）
            monitor = asyncio.create_task(
                _monitor_orphan(task), name=f"bash-bg-orphan-{task_id}")
            task.monitor = monitor
            _MONITOR_TASKS.add(monitor)
            monitor.add_done_callback(_MONITOR_TASKS.discard)
            logger.info("后台任务 %s 为应用重启遗留的存活孤儿，已重新纳入探活", task_id)
        else:
            # was-running 且进程已死：补推「因重启被中止」，不再无声消失。
            _finish_task(task, status="lost", exit_code=None, notify=True)
    # completed 分支（磁盘上早已终态、本次直接 continue 恢复）不经过
    # _finish_task，不会触发其内部的裁剪；这里统一补一次，防止「旧安装
    # 多年积累的历史任务文件」在重启后被整批读入内存又从不清理。
    _prune_finished_tasks(chat_id, namespace)


# ===================== 终态与通知 =====================
def _finish_task(
    task: BackgroundTask,
    status: str,
    exit_code: int | None = None,
    notify: bool = True,
) -> None:
    """记录终态：内存状态 + 磁盘（.json/.exit）+ 完成通知入队。

    幂等守卫：终态只记录一次——monitor、stop 路径、重启懒加载三方都可能
    竞争同一次进程退出（如 stop 的 SIGTERM 到达时 monitor 恰好也在
    await proc.wait() 上苏醒），先到者胜出，后到者直接返回，防止状态被
    二次改写或同一任务推送两条通知。

    notify=False 用于模型主动 stop（结果同步返回给模型，无需再通知）。
    """
    if task.status in _TERMINAL_STATUSES:
        logger.debug(
            "后台任务 %s 终态已被先行记录为 %s，忽略后续 %s 记录请求",
            task.task_id, task.status, status)
        return
    task.status = status
    task.exit_code = exit_code
    task.finished_at = time.time()
    _mark_terminal_on_disk(task)
    if notify:
        push_completion_notice(task.chat_id, task.namespace, _format_notice(task))
    _prune_finished_tasks(task.chat_id, task.namespace)


def _prune_finished_tasks(chat_id: int, namespace: str) -> None:
    """回收超出 BASH_TASK_FINISHED_RETENTION 的已终结任务（内存 + 磁盘）。

    只清理终态任务（status in _TERMINAL_STATUSES），运行中任务不受影响。
    按 finished_at 排序，保留最近的 N 个，其余的内存条目与
    .json/.log/.exit 三件磁盘文件一并删除——避免长期运行的 chat 反复
    起后台任务后，注册表和 tasks 目录无限增长（list/status 也随之越
    列越长）。删除是尽力而为：单个文件删除失败不影响其余清理，也不
    影响调用方（_finish_task）的主流程。
    """
    tasks = _TASKS.get((chat_id, namespace))
    if not tasks:
        return
    finished = [t for t in tasks.values() if t.status in _TERMINAL_STATUSES]
    if len(finished) <= BASH_TASK_FINISHED_RETENTION:
        return
    finished.sort(key=lambda t: t.finished_at or 0.0)
    overflow = finished[: len(finished) - BASH_TASK_FINISHED_RETENTION]
    for old_task in overflow:
        tasks.pop(old_task.task_id, None)
        for path_str in (old_task.log_path, old_task.state_path):
            try:
                Path(path_str).unlink(missing_ok=True)
            except OSError:
                logger.debug("后台任务旧文件清理失败: %s", path_str, exc_info=True)
        try:
            Path(old_task.state_path).with_suffix(".exit").unlink(missing_ok=True)
        except OSError:
            logger.debug("后台任务旧 .exit 清理失败: %s", old_task.state_path, exc_info=True)
    logger.debug(
        "后台任务注册表回收 chat_id=%s namespace=%s 清理=%d 保留=%d",
        chat_id, namespace, len(overflow), BASH_TASK_FINISHED_RETENTION,
    )


def _cancel_aux_tasks(task: BackgroundTask) -> None:
    for attr in ("monitor", "watchdog"):
        t = getattr(task, attr)
        if t is not None and not t.done():
            t.cancel()
        setattr(task, attr, None)


# ===================== monitor =====================
async def _monitor_task(task: BackgroundTask) -> None:
    """等待后台进程退出并记录终态。应用关闭时 monitor 被 cancel——
    此时**不写终态**（.exit 缺失），下次启动按孤儿/lost 恢复，不会把
    关闭时杀掉的进程误报为 failed。"""
    assert task.proc is not None
    wd = asyncio.create_task(
        watchdog(task.proc), name=f"watchdog-bg-{task.task_id}")
    task.watchdog = wd
    try:
        try:
            await asyncio.wait_for(task.proc.wait(), timeout=task.lifetime)
        except asyncio.TimeoutError:
            _signal_task_process(task, signal.SIGKILL)
            try:
                await asyncio.wait_for(task.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("后台任务 %s 寿命到期后进程未如期退出", task.task_id)
            _finish_task(task, status="expired", exit_code=task.proc.returncode)
        else:
            exit_code = task.proc.returncode
            _finish_task(
                task,
                status="done" if exit_code == 0 else "failed",
                exit_code=exit_code,
            )
    except asyncio.CancelledError:
        raise
    finally:
        if task.watchdog is not None and not task.watchdog.done():
            task.watchdog.cancel()
        task.watchdog = None


async def _monitor_orphan(task: BackgroundTask) -> None:
    """应用重启遗留的存活孤儿：无 proc 句柄，只能按 pid 轮询探活。
    身份校验（cmdline 匹配）防止 pid 复用导致的误杀。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, task.started_at + task.lifetime - time.time())
    try:
        while True:
            if not (_pid_alive(task.pid) and _pid_matches_command(task.pid, task.command)):
                _finish_task(task, status="lost", exit_code=None)
                return
            if loop.time() >= deadline:
                _signal_task_process(task, signal.SIGKILL)
                _finish_task(task, status="expired", exit_code=None)
                return
            await asyncio.sleep(_ORPHAN_POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        raise


# ===================== 启动 =====================
def _interactive_last_cwd(chat_id: int, namespace: str) -> str:
    """取交互会话的最后 cwd，让后台任务与前台命令的目录观感一致。
    仅读取，不触发生成；会话不存在/未 cd 过则回退工作区根。"""
    try:
        from bash_session import _bash_manager  # 延迟导入避免环
        session = _bash_manager._sessions.get((chat_id, namespace))
        if session is not None and session._last_cwd:
            return session._last_cwd
    except Exception:
        logger.debug("读取交互会话 cwd 失败（回退工作区根）", exc_info=True)
    return ""


async def start_background_task(
    chat_id: int,
    namespace: str,
    command: str,
    description: str = "",
) -> str:
    """启动后台任务并立即返回句柄。进程独立成组、输出落盘、无 idle 超时；
    ``timeout`` 是前台概念，此处不适用（寿命上限统一兜底）。"""
    if not command or not command.strip():
        return "Error: command is required"
    if not _command_is_safe(command):
        return f"Error: Command rejected for security reasons: {command}"
    _ensure_registry_loaded(chat_id, namespace)
    tasks = _TASKS.setdefault((chat_id, namespace), {})
    running = [t for t in tasks.values() if t.status == "running"]
    # 上限是护栏而非硬不变量：计数与 spawn 之间存在 await，理论上可被
    # 并发启动轻微超越，可接受。
    if len(running) >= BASH_TASK_MAX_PER_CHAT:
        lines = [f"Error: 后台任务数量已达上限（每 chat {BASH_TASK_MAX_PER_CHAT} 个运行中）。当前任务："]
        lines += [_summarize_task(t) for t in running]
        lines.append("可先 task_action=stop 停止不需要的任务，或等待其完成后再启动。")
        return "\n".join(lines)

    workdir = workspace_paths.workspace_workdir(chat_id, namespace)
    cwd = _interactive_last_cwd(chat_id, namespace) or str(workdir.absolute())
    try:
        env = build_sandbox_env(workdir, chat_id, namespace)
    except Exception as e:
        return f"Error: 沙箱环境构建失败: {e}"

    tdir = _tasks_dir(chat_id, namespace)
    tdir.mkdir(parents=True, exist_ok=True)
    task_id = f"bg-{uuid.uuid4().hex[:8]}"
    log_path = tdir / f"{task_id}.log"
    state_path = tdir / f"{task_id}.json"
    try:
        # stdout/stderr 直接落盘（无管道、无读循环 → 无 idle 超时问题，
        # 也无输出缓冲上限问题；RLIMIT_FSIZE=100MB 由沙箱兜底）。
        with open(log_path, "wb") as fh:
            proc = await asyncio.create_subprocess_exec(
                "bash", "--noprofile", "--norc", "-c", command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=fh,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=cwd,
                start_new_session=True,   # 独立会话/进程组：免疫交互会话 killpg
                preexec_fn=functools.partial(
                    _preexec_sandbox, str(workdir.absolute())),
            )
    except Exception as e:
        # Windows / 缺 bash / preexec_fn 不支持（ValueError）等全部归一为
        # 可操作的错误文本返回给模型。
        return f"Error: 后台任务启动失败: {e}"

    task = BackgroundTask(
        task_id=task_id,
        chat_id=chat_id,
        namespace=namespace,
        command=command,
        description=str(description or ""),
        cwd=cwd,
        pid=proc.pid,
        log_path=str(log_path),
        state_path=str(state_path),
        started_at=time.time(),
        lifetime=float(BASH_TASK_MAX_LIFETIME_SEC),
        proc=proc,
    )
    tasks[task_id] = task
    _dump_state(task)
    monitor = asyncio.create_task(_monitor_task(task), name=f"bash-bg-{task_id}")
    task.monitor = monitor
    _MONITOR_TASKS.add(monitor)
    monitor.add_done_callback(_MONITOR_TASKS.discard)
    logger.info(
        "后台任务已启动 chat_id=%s task=%s pid=%s cmd=%s",
        chat_id, task_id, proc.pid, command[:100],
    )

    lines = [
        f"✅ 后台任务已启动 {task_id}「{description or _first_line(command, 40)}」",
        f"命令：{_first_line(command)}",
        f"日志：{log_path}",
        f"寿命上限：{BASH_TASK_MAX_LIFETIME_SEC}s（到期强制终止）",
        "任务独立于交互会话：前台命令超时、restart=true、用户插话都不会影响它；",
        "完成/失败/到期后，结果摘要会自动随下一次 AI 请求带给本 agent。期间可随时查询：",
        f"  bash task_action=status task_id={task_id}",
        f"  bash task_action=output task_id={task_id}",
        f"  bash task_action=stop task_id={task_id}",
        "  bash task_action=list",
    ]
    return "\n".join(lines)


# ===================== 查询 / 停止 / 列表 =====================
def _summarize_task(task: BackgroundTask) -> str:
    desc = task.description or _first_line(task.command, 40)
    if task.status == "running":
        elapsed = _fmt_duration(time.time() - task.started_at)
        return f"- {task.task_id} 运行中（已 {elapsed}）「{desc}」{_first_line(task.command)}"
    if task.status in ("done", "failed"):
        mark = "✅" if task.status == "done" else "❌"
        return (
            f"- {task.task_id} {mark} exit code {task.exit_code}，"
            f"用时 {_fmt_duration((task.finished_at or time.time()) - task.started_at)}「{desc}」"
        )
    labels = {"stopped": "⏹ 已停止", "expired": "⌛ 超寿命终止", "lost": "❔ 因重启被中止（退出码未知）"}
    return f"- {task.task_id} {labels.get(task.status, task.status)}「{desc}」"


def _resolve_task(chat_id: int, namespace: str, task_id: str | None, action: str) -> tuple[BackgroundTask | None, str]:
    tasks = _TASKS.get((chat_id, namespace), {})
    if not task_id:
        listing = "\n".join(_summarize_task(t) for t in tasks.values()) or "（无任务）"
        return None, f"Error: task_action={action} 需要 task_id。当前任务：\n{listing}"
    task = tasks.get(task_id)
    if task is None:
        listing = "\n".join(_summarize_task(t) for t in tasks.values()) or "（无任务）"
        return None, f"Error: 未找到任务 {task_id}。当前任务：\n{listing}"
    return task, ""


async def query_task(
    chat_id: int,
    namespace: str,
    action: str,
    task_id: str | None = None,
) -> str:
    action = str(action or "").strip().lower()
    if action not in ("status", "output", "stop", "list"):
        return f"Error: 未知 task_action: {action}（可选 status/output/stop/list）"
    _ensure_registry_loaded(chat_id, namespace)
    tasks = _TASKS.get((chat_id, namespace), {})

    if action == "list":
        running = sum(1 for t in tasks.values() if t.status == "running")
        listing = "\n".join(_summarize_task(t) for t in tasks.values()) or "（无任务）"
        return f"📋 后台任务列表（运行中 {running} / 共 {len(tasks)}）：\n{listing}"

    task, err = _resolve_task(chat_id, namespace, task_id, action)
    if task is None:
        return err

    if action == "status":
        if task.status == "running":
            elapsed = _fmt_duration(time.time() - task.started_at)
            body = (
                f"⏳ 后台任务 {task.task_id} 运行中（已 {elapsed} / 上限 {task.lifetime:.0f}s）\n"
                f"命令：{_first_line(task.command)}\n"
                f"日志：{task.log_path}"
            )
        elif task.status in ("done", "failed"):
            mark = "✅" if task.status == "done" else "❌"
            body = (
                f"{mark} 后台任务 {task.task_id} 已结束：exit code {task.exit_code}，"
                f"用时 {_fmt_duration((task.finished_at or time.time()) - task.started_at)}\n"
                f"命令：{_first_line(task.command)}\n"
                f"日志：{task.log_path}"
            )
        else:
            labels = {"stopped": "⏹ 已停止", "expired": "⌛ 超寿命被强制终止", "lost": "❔ 因重启被中止（退出码未知）"}
            body = (
                f"{labels.get(task.status, task.status)}：后台任务 {task.task_id}\n"
                f"命令：{_first_line(task.command)}\n"
                f"日志：{task.log_path}"
            )
        return body

    if action == "output":
        tail = _tail_file(task.log_path, QUERY_OUTPUT_TAIL_LINES, QUERY_OUTPUT_TAIL_CHARS)
        head = f"📜 后台任务 {task.task_id} 输出尾部（最多 {QUERY_OUTPUT_TAIL_LINES} 行）："
        if not tail:
            tail = "（暂无输出）"
        return f"{head}\n{tail}\n完整日志：{task.log_path}"

    # action == "stop"
    if task.status != "running":
        return (
            f"ℹ️ 后台任务 {task.task_id} 已于先前结束（状态 {task.status}，"
            f"exit code {task.exit_code}），无需停止。"
        )
    # 先取消 monitor/watchdog 再发信号（与 shutdown_all 同序）：否则
    # SIGTERM 杀死进程后，monitor 的 await proc.wait() 与本路径的 wait
    # 同时就绪，monitor 可能先行把退出记录成 failed（exit -15）并推送
    # 通知——"模型主动 stop 同步得知、不重复入队"的约定被打破。
    _cancel_aux_tasks(task)
    graceful = _signal_task_process(task, signal.SIGTERM)
    if task.proc is not None:
        try:
            await asyncio.wait_for(task.proc.wait(), timeout=BASH_TASK_STOP_GRACE_SEC)
        except asyncio.TimeoutError:
            graceful = False
            _signal_task_process(task, signal.SIGKILL)
            try:
                await asyncio.wait_for(task.proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("后台任务 %s stop 后进程未如期退出", task.task_id)
    else:
        # 孤儿：无 wait 句柄，SIGTERM 后轮询探活
        deadline = asyncio.get_running_loop().time() + BASH_TASK_STOP_GRACE_SEC
        while (_pid_alive(task.pid) and _pid_matches_command(task.pid, task.command)
               and asyncio.get_running_loop().time() < deadline):
            await asyncio.sleep(0.5)
        if _pid_alive(task.pid) and _pid_matches_command(task.pid, task.command):
            graceful = False
            _signal_task_process(task, signal.SIGKILL)
            await asyncio.sleep(1.0)
    _finish_task(
        task,
        status="stopped",
        exit_code=task.proc.returncode if task.proc is not None else None,
        notify=False,
    )
    method = "SIGTERM 优雅终止" if graceful else "SIGTERM 超时后 SIGKILL 强制终止"
    logger.info("后台任务已停止 chat_id=%s task=%s (%s)", chat_id, task.task_id, method)
    return f"⏹ 后台任务 {task.task_id} 已停止（{method}）"


# ===================== 应用关闭 =====================
async def shutdown_all() -> None:
    """应用关闭时终止全部后台任务。

    顺序很关键：先取消 monitor（防止 kill 之后 monitor 把进程退出记录成
    failed 终态），再杀进程。.exit 不写——下次启动按孤儿/lost 恢复，
    不会把「关闭时被杀」误报成「任务失败」。

    除了挂在 _TASKS 条目上的 monitor，还必须清空模块级 _MONITOR_TASKS
    引用集：测试/热重载等场景可能清空注册表后仍残留上一批探活任务，
    它们捕获的 BackgroundTask 已不在注册表里，不取消会一直轮询到进程
    结束并对已终态的任务做无效 _finish_task（幂等守卫会挡住，但任务
    本身会泄漏到事件循环关闭）。
    """
    for tasks in _TASKS.values():
        for task in tasks.values():
            _cancel_aux_tasks(task)
    for stray in list(_MONITOR_TASKS):
        if not stray.done():
            stray.cancel()
    _MONITOR_TASKS.clear()
    for tasks in _TASKS.values():
        for task in tasks.values():
            if task.proc is not None and task.proc.returncode is None:
                _signal_task_process(task, signal.SIGKILL)
                try:
                    await asyncio.wait_for(task.proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("后台任务 %s 关闭时进程未如期退出", task.task_id)
    _TASKS.clear()
    _LOADED_KEYS.clear()
