"""持久 bash 沙箱会话：BashSession / BashSessionManager / execute_bash（自 tool_executors.py 拆出）。"""

import os
import re
import json
import shutil
import subprocess
import asyncio
import uuid
from pathlib import Path
from typing import Optional

from sandbox import (
    build_sandbox_argv, build_sandbox_env,
    watchdog, _preexec_sandbox,
    SANDBOX_TIMEOUT_SEC,
)
from workspace_paths import (
    workspace_root, workspace_workdir, runtime_cache_root, workspace_namespace,
    workspace_upload_root, workspace_download_root,
    is_inside_upload_or_download,
)
from workspace_utils import _get_workspace_lock, _ensure_runtime_workspace
from tool_ui_render import _strip_ansi

import logging

logger = logging.getLogger(__name__)


def _format_bash_envelope(prompt_cwd: str, command: str, exit_code: int | str, output: str) -> str:
    """把一次 bash 执行渲染成终端回放式结果信封。

    形如::

        /abs/cwd$ <command>
        Exit code: <code>
        <output>

    设计（对齐真实终端的使用体验）：
    - 首行是 PS1 风格提示符：模型像人一样直接"看到"命令前面的当前
      目录；``cd`` 之后下一条命令的提示符随之变化。工作区在哪、现在
      在哪，由提示符天然承载，不再需要 Sandbox/Cwd/Command 等元数据
      行（工作区绝对路径与可写范围已在系统提示词和工具描述中声明，
      每轮重复只会浪费 token）。
    - ``Exit code:`` 紧跟命令行、位于输出之前。下游解析（tool_summary
      / tool_call_loop）用首个 ``Exit code: `` 匹配退出码——放在输出
      前面可保证命令输出里即使出现 "Exit code: N" 字样也不会遮蔽
      真实退出码（与旧信封同等安全，且整体更短）。
    - 输出为空时只有两行，与真实终端一致。
    """
    cmd_text = str(command or "").rstrip()
    header = f"{prompt_cwd}$ {cmd_text}\nExit code: {exit_code}"
    body = str(output or "").rstrip("\n")
    if not body:
        return header
    return f"{header}\n{body}"
# ---------- Bash 输出上限（环境变量可调） ----------
# 单条 Bash 命令返回给模型的内容上限（字符数）。超限时「保留开头 + 结尾、
# 省略中间」：编译错误、traceback、日志摘要几乎总是出现在输出末尾，纯
# 头部截断会把最有价值的部分默默丢掉。设为 0 表示不限制（不建议：狂刷
# 输出的命令会撑爆内存与模型上下文）。
SANDBOX_OUTPUT_MAX_CHARS = int(os.getenv("SANDBOX_OUTPUT_MAX_CHARS", "80000"))
class _BashOutputBuffer:
    """Bounded accumulator for subprocess output: keeps head + rolling tail.

    旧实现把全部输出无限累积进内存，再一刀切只留开头 20000 字符，存在
    两个问题：
      1. 狂刷输出的命令（`yes`、误写的热循环、`find /`）会让应用 OOM；
      2. 头部截断丢掉了几乎必然位于结尾的错误信息。
    本缓冲区用固定字符预算同时解决两者：预算内原样保留；超预算后保留
    开头 head_ratio 比例 + 滚动尾部，中间丢弃并精确计数，最终在结果里
    插入一条可读说明，让模型知道自己看到的是被裁剪过的输出。
    """

    __slots__ = ("keep", "head_ratio", "_head", "_tail", "_kept", "_dropped", "_capped", "_total")

    def __init__(self, keep_chars: int = SANDBOX_OUTPUT_MAX_CHARS, head_ratio: float = 0.7) -> None:
        # 下限 200 仅防退化输入（负数/极小值）；0 视为不限制。
        self.keep = max(200, int(keep_chars)) if keep_chars else 10**12
        self.head_ratio = min(max(head_ratio, 0.1), 0.9)
        self._head: list[str] = []
        self._tail: list[str] = []
        self._kept = 0
        self._dropped = 0
        self._total = 0
        self._capped = False

    @property
    def total_seen(self) -> int:
        """到目前为止接收到的全部字符数（含被丢弃的中间部分）。"""
        return self._total

    def add(self, text: str) -> None:
        if not text:
            return
        self._total += len(text)
        if not self._capped:
            self._head.append(text)
            self._kept += len(text)
            if self._kept > self.keep:
                self._enter_capped_mode()
            return
        self._tail.append(text)
        self._kept += len(text)
        self._trim_to_budget()

    def _enter_capped_mode(self) -> None:
        """把已累积内容切成「固定头部 + 滚动尾部」两段。"""
        self._capped = True
        whole = "".join(self._head)
        head_len = max(1, int(self.keep * self.head_ratio))
        tail_len = max(1, self.keep - head_len)
        self._head = [whole[:head_len]]
        self._dropped += len(whole) - head_len - tail_len
        self._tail = [whole[-tail_len:]] if tail_len else []
        self._kept = len(self._head[0]) + len(self._tail[0]) if self._tail else len(self._head[0])

    def _trim_to_budget(self) -> None:
        """超预算时优先消耗头部（挪入 dropped），头部耗尽后滚动丢弃最旧尾部。"""
        while self._kept > self.keep:
            if self._head:
                last = self._head[-1]
                excess = self._kept - self.keep
                if len(last) <= excess:
                    self._head.pop()
                    self._dropped += len(last)
                    self._kept -= len(last)
                else:
                    cut = len(last) - excess
                    self._head[-1] = last[:cut]
                    self._dropped += excess
                    self._kept -= excess
            elif self._tail:
                oldest = self._tail[0]
                excess = self._kept - self.keep
                if len(oldest) <= excess:
                    self._tail.pop(0)
                    self._dropped += len(oldest)
                    self._kept -= len(oldest)
                else:
                    self._tail[0] = oldest[excess:]
                    self._dropped += excess
                    self._kept -= excess
            else:
                break

    def finalize(self) -> str:
        """返回最终文本；中间被省略时插入明确说明。"""
        head = "".join(self._head)
        tail = "".join(self._tail)
        if not self._capped or self._dropped <= 0:
            return head + tail
        note = (
            f"\n... [output truncated: {self._dropped} chars omitted from the middle; "
            f"kept the first {len(head)} and the last {len(tail)} chars. "
            f"Redirect full output to a file (e.g. `cmd > out.log`) and inspect it "
            f"with grep/tail/text_editor if you need the omitted part] ...\n"
        )
        return head + note + tail
_RUNTIME_STATE_FILENAME = "runtime.json"


def _runtime_state_path(chat_id: int, namespace: str | None = None) -> Path:
    return workspace_root(chat_id, namespace) / _RUNTIME_STATE_FILENAME


def _tool_version(exe: str) -> str | None:
    path = shutil.which(exe)
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2,
            check=False,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
                "LC_ALL": "C.UTF-8",
                "LANG": "C.UTF-8",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (proc.stdout or "").splitlines()
    return line[0].strip()[:300] if line else None


def _prepare_runtime_once(
    chat_id: int,
    cache_root: Path,
    namespace: str | None = None,
) -> dict:
    """Record host toolchain discovery once per persistent workspace.

    This deliberately does *not* install compilers per Bash invocation. The base image
    owns the toolchain; the workspace owns only reusable caches and a small manifest.
    """
    state_path = _runtime_state_path(chat_id, namespace)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and state.get("schema") == 1:
                return state
        except (OSError, ValueError, TypeError):
            pass

    tools = {
        name: {
            "path": shutil.which(name),
            "version": _tool_version(name),
        }
        for name in ("python3", "gcc", "g++", "clang", "clang++", "make", "cmake", "ccache")
    }
    state = {
        "schema": 1,
        "prepared": True,
        "cache_root": str(cache_root),
        "tools": tools,
    }
    try:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Unable to persist runtime state chat_id=%s: %s", chat_id, exc)
    return state


# =====================================================================
# BashSession —— 每会话独立沙箱
# =====================================================================
class BashSession:
    def __init__(self, chat_id: int, namespace: str | None = None) -> None:
        self.chat_id = chat_id
        self.namespace = workspace_namespace(chat_id, namespace)
        self.proc: Optional[asyncio.subprocess.Process] = None
        # start() 可能从两条锁路径被调用（execute 的 workspace 锁内、
        # manager 的全局锁内），两把锁互不互斥；每实例锁串行化 spawn，
        # 防止并发双开 bash 导致先 spawn 的进程泄漏、新进程无看门狗。
        self._start_lock = asyncio.Lock()
        self.workspace = workspace_root(chat_id, self.namespace)
        self.workdir = workspace_workdir(chat_id, self.namespace)
        self._watchdog_task: Optional[asyncio.Task] = None
        self._runtime_state: Optional[dict] = None
        self._runtime_prepare_lock = asyncio.Lock()
        # cwd 必须由模型通过 `cd` 自己控制；选择使用 skill 后可进入
        # `skills/<skill_id>`，persistent bash 会保持当前目录与 shell 状态。
        # 跟踪上一次命令结束后的真实 PWD，用于在 upload/ 或 download/ 子树内
        # 拒绝执行下一条命令。None 表示尚未执行过命令，假定位于 workdir。
        self._last_cwd: Optional[str] = str(self.workdir.absolute())
        # persistent shell 自身的 cwd（隔离/heredoc 执行不回写该值）。
        # 结果信封的提示符用它：保证模型看到的 ``path$`` 永远是命令真正
        # 运行的目录，不会被子 shell 的 cd 污染。
        self._persistent_cwd: Optional[str] = str(self.workdir.absolute())

    async def start(self) -> None:
        """启动 bash 进程，套上 Landlock 沙箱 + rlimit + no-new-privs"""
        async with self._start_lock:
            return await self._start_locked()

    async def _start_locked(self) -> None:
        """start() 的实际实现；调用方必须已持有 self._start_lock。"""
        if self.proc is not None and self.proc.returncode is None:
            return

        # workspace 目录权限 700，防跨 chat 读取
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workspace, 0o700)
        os.chmod(self.workdir, 0o700)
        # ★ 显式预创建 upload/ 和 download/：bash 进程一启动，cwd 就是
        # workspace root，模型几乎立刻会跑 `cp out.txt upload/out.txt`
        # 或 `cat download/x.pdf`。如果不在这里预创建，bash 进程已经
        # 在跑、第一次 execute() 时才补创建，会出两个问题：
        #   1) 如果 execute() 里 _ensure_runtime_workspace 抛异常被
        #      try/except 吞掉，目录就永远不存在，cp 第一次必然失败，
        #      模型不得不多跑一轮 `mkdir -p upload && cp ...` 才能补救；
        #   2) _ensure_runtime_workspace(self.chat_id) 没传 namespace，
        #      依赖 ContextVar；如果 bash 工具从 background task 里
        #      调用、ContextVar 不可见，upload/ 会被建到错误的 namespace
        #      下，bash 进程实际看到的 cwd 下仍然没有 upload/。
        # 用 self.namespace 直接走 workspace_upload_root /
        # workspace_download_root，确保和 bash 进程的 cwd 完全一致。
        workspace_upload_root(self.chat_id, self.namespace)
        workspace_download_root(self.chat_id, self.namespace)

        # 新进程的 cwd 必然是 workdir；重置 _last_cwd，避免上一次会话
        # 残留的 cwd 状态误拒下一条命令。
        self._last_cwd = str(self.workdir.absolute())
        self._persistent_cwd = str(self.workdir.absolute())

        argv = build_sandbox_argv()
        env = build_sandbox_env(self.workspace, self.chat_id, self.namespace)
        cache_root = runtime_cache_root(self.chat_id, self.namespace)
        async with self._runtime_prepare_lock:
            if self._runtime_state is None:
                # One-time discovery per persistent workspace. Subsequent Bash restarts
                # reuse the manifest instead of "preparing" the toolchain again.
                self._runtime_state = await asyncio.to_thread(
                    _prepare_runtime_once, self.chat_id, cache_root, self.namespace
                )

        # ★ Landlock：把文件系统访问限制在该 chat 的 workspace 层，
        #   runtime/、skills/ 都在这里；R2 不再对工作区做全量同步。
        #   通过 functools.partial 把 workspace 路径传给 preexec。
        import functools
        preexec = functools.partial(
            _preexec_sandbox,
            str(self.workspace.absolute()),
        )

        logger.info(
            "Starting bash session chat_id=%s runtime_prepared=%s cache=%s",
            self.chat_id,
            bool(self._runtime_state and self._runtime_state.get("prepared")),
            cache_root,
        )

        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            text=False,
            bufsize=0,
            env=env,  # ★ 关键: 不传任何敏感变量
            cwd=str(self.workdir.absolute()),  # ★ 关键: 沙箱进程启动即位于 workspace root
            start_new_session=True,  # ★ 关键: 创建新会话，便于 killpg
            preexec_fn=preexec,  # Landlock + no-new-privs + rlimit
        )

        # 启动看门狗（fork bomb 防护）。无条件跟随本次 spawn 的进程重建：
        # 复用旧看门狗会让重启后的新进程处于无防护状态
        # （它盯的是已死的旧进程对象）。
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = asyncio.create_task(
            watchdog(self.proc), name=f"watchdog-{self.chat_id}"
        )


    # ===================== 命令安全检查（最小黑名单） =====================
    # 设计原则: 不限制语法（heredoc/管道/重定向/&&/|| 全部允许），
    #          只拦截极端灾难模式，剩余靠沙箱兜底
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

    # ===================== upload/ & download/ 子树保护 =====================
    # 这两棵子树是“产物暂存区”和“用户上传落地”，不允许 bash 在其中执行命令。
    # 主要威胁：模型 cd 进 upload/ 之后跑 `pip install`，会把整个依赖树装进
    # upload/，污染即将发给用户的产物；同理 download/ 也不允许被执行污染。
    #
    # 检测策略：
    #   1. 命令字符串里的 `cd` 目标若指向 upload/ 或 download/（任意前缀形式：
    #      `upload/`, `./upload/`, `../upload/`, `../upload/sub`, 绝对路径等）
    #      直接拒绝。
    #   2. 每次执行前检查 _last_cwd；若已经在 upload/ 或 download/ 内，拒绝执行
    #      并提示模型先 `cd` 回 workdir。
    _UPLOAD_DOWNLOAD_CD_PATTERN = re.compile(
        r"""(?:^|[\s;&|`(])       # 命令起始或分隔符
            cd\s+                 # cd 命令
            (?:['"]?)             # 可选引号
            (?:\./)?              # 可选 ./
            (?:\.\./)*            # 任意数量的 ../
            (?:upload|download)   # 目标目录名
            (?:/|['"]|\s|$)       # 后续分隔
        """,
        re.VERBOSE,
    )

    def _is_safe(self, command: str) -> bool:
        """最小黑名单，仅拦极端操作；其余靠沙箱"""
        if not command or not command.strip():
            return False
        for pattern, name in self._DANGEROUS_PATTERNS:
            if pattern.search(command):
                logger.warning(f"🚫 Bash rejected ({name}) chat_id={self.chat_id}: {command[:200]}")
                return False
        # 允许进入 download/upload 目录做只读检查。
        # 旧逻辑会直接拒绝:
        #   cd download && ls -lh
        # 这会阻止 AI 分析用户上传文件。
        # 写入、删除、执行等危险操作仍由 _DANGEROUS_PATTERNS 和沙箱控制。
        # 拒绝在 upload/ 或 download/ 子树内执行任何命令
        if self._last_cwd and is_inside_upload_or_download(self._last_cwd):
            # 修复问题2：如果当前在 upload/download 内，但命令是返回工作目录的 cd 命令，
            # 则允许执行，让模型能够逃离陷阱。检测模式：
            # - cd $WORKSPACE
            # - cd /path/to/workspace
            # - cd (不带参数，返回 HOME，但沙箱中 HOME=WORKSPACE)
            # - cd .. (可能需要多次才能离开，但至少允许尝试)
            cmd_stripped = command.strip()
            if re.match(r'^cd(\s+\$WORKSPACE|\s+\$HOME|\s*$|\s+\.\.(/\.\.)*)(\s*[;&|]|$)', cmd_stripped):
                logger.info(
                    f"✓ Bash allowed escape-cd from upload/download chat_id={self.chat_id} cwd={self._last_cwd} cmd={command[:100]}"
                )
                return True
            # 也允许绝对路径 cd 到工作目录
            workspace_path = str(self.workdir.absolute())
            if re.match(rf'^cd\s+["\']?{re.escape(workspace_path)}["\']?(\s*[;&|]|$)', cmd_stripped):
                logger.info(
                    f"✓ Bash allowed escape-cd (absolute) from upload/download chat_id={self.chat_id} cwd={self._last_cwd}"
                )
                return True
            logger.warning(
                f"🚫 Bash rejected (cwd inside upload/download) chat_id={self.chat_id} cwd={self._last_cwd}"
            )
            return False
        return True

    @staticmethod
    async def _is_unterminated(command: str) -> bool:
        """Detect commands bash would keep waiting on (unclosed heredoc,
        quote, backtick, paren, etc.) using `bash -n` as ground truth.

        A persistent stdin-backed shell deadlocks whenever the model emits
        any syntactically incomplete command — not just heredocs. An
        unterminated `"..."` or `'...'` string is just as fatal: the shell
        keeps reading stdin waiting for the closing quote, and our synthetic
        end-marker line is silently swallowed as part of that string instead
        of being executed. `bash -n` performs a pure syntax check (no
        execution) and reports "unexpected EOF while looking for matching"
        for exactly this class of problem, so it is a much more reliable
        signal than trying to enumerate every unterminated-token regex by
        hand (heredocs, quotes, backticks, `$(`, `((`, `{`, ...).
        """
        try:
            # 强制使用 C locale：bash 在 zh_CN.UTF-8 / ja_JP.UTF-8 等环境下
            # 会输出本地化错误信息（"未预期的文件结束符"），从而让下面
            # 的英文子串匹配（"unexpected EOF"）彻底失效，导致本应被路由
            # 到隔离执行的危险命令直接进入持久 shell，触发 300s 卡死。
            import os as _os
            env = {
                "LC_ALL": "C.UTF-8",
                "LANG": "C.UTF-8",
                "PATH": _os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            }
            proc = await asyncio.create_subprocess_exec(
                "bash", "-n", "-c", command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                # Can't prove it's safe; route to isolated execution to be safe.
                return True
            if proc.returncode != 0:
                msg = (stderr or b"").decode("utf-8", errors="replace")
                # 仅 "unexpected EOF" / 未终止 token 错误才说明持久 shell 会
                # 卡住；其他语法错误（如拼写错误）可以由持久 shell 正常报错，
                # 因为它们不会吞掉 end marker。
                if "unexpected EOF" in msg or "unexpected end of file" in msg:
                    return True
            return False
        except Exception:
            # If we can't run the syntax check at all, don't block execution —
            # fall through to the existing heredoc regex as a safety net.
            logger.debug("_is_unterminated 内部忽略的异常", exc_info=True)
            return False

    async def _execute_heredoc_isolated(self, command: str, timeout: int) -> str:
        """Execute heredoc-heavy (or otherwise syntactically risky) commands
        in a one-shot bash process.

        A persistent stdin-backed shell can deadlock when a model emits an
        incomplete heredoc or unterminated quote: the shell keeps waiting for
        the terminator, while our synthetic end marker is consumed as input
        to that still-open construct.  A one-shot `bash -c` receives an
        actual EOF at the end of `command`, so malformed input terminates
        with a shell error instead of hanging the session.
        """
        workspace = self.workspace
        cwd = self._last_cwd or str(self.workdir.absolute())
        env = build_sandbox_env(self.workspace, self.chat_id, self.namespace)
        import functools
        preexec = functools.partial(_preexec_sandbox, str(workspace.absolute()))

        marker = f"__ONE_SHOT_END_{uuid.uuid4().hex[:8]}__"
        full_cmd = command.rstrip() + f"\nprintf '{marker} %s\n' \"$?\"\nprintf '__ONE_SHOT_CWD__ %s\n' \"$PWD\"\n"

        # 与持久会话（sandbox.py 的 /bin/bash --noprofile --norc）保持一致：
        # 登录 shell（-l）会 source /etc/profile 重置 PATH，可能让 runtime_bin
        # 里的 curl/wget shim 失效，且两条执行路径语义分叉。
        proc = await asyncio.create_subprocess_exec(
            "bash", "--noprofile", "--norc", "-c", full_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            text=False,
            bufsize=0,
            env=env,
            cwd=cwd,
            start_new_session=True,
            preexec_fn=preexec,
        )

        output_buffer = _BashOutputBuffer()

        try:
            # stdout=PIPE 由上方 create_subprocess_exec 调用保证；仅作类型收窄。
            assert proc.stdout is not None
            while True:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=timeout)
                if not chunk:
                    break
                output_buffer.add(chunk.decode("utf-8", errors="replace"))
                # Once the first byte arrived, reset the idle read timer to keep
                # long-running commands alive while still detecting a total hang.
                timeout = max(timeout, 1)
        except asyncio.TimeoutError:
            logger.warning("Bash isolated timeout chat_id=%s cmd=%s", self.chat_id, command[:120])
            partial_output = ""
            try:
                partial_output = _strip_ansi(output_buffer.finalize()).strip()
            except Exception:
                pass
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                logger.debug("_execute_heredoc_isolated 内部忽略的异常", exc_info=True)
                pass
            msg = f"Error: Command timed out after {timeout} seconds (isolated bash killed)"
            if partial_output:
                return f"{msg}\n\nCaptured partial output before timeout:\n{partial_output}"
            return msg
        except asyncio.CancelledError:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                logger.debug("_execute_heredoc_isolated 内部忽略的异常", exc_info=True)
                pass
            raise

        await proc.wait()
        output = output_buffer.finalize()
        exit_code = proc.returncode if proc.returncode is not None else "unknown"
        marker_match = re.search(rf"(?m)^{re.escape(marker)}\s+(-?\d+)\s*$", output)
        if marker_match:
            exit_code = marker_match.group(1)
            output = re.sub(rf"(?m)^{re.escape(marker)}\s+-?\d+\s*$\n?", "", output)
        cwd_match = re.search(r"(?m)^__ONE_SHOT_CWD__\s+(.+)$", output)
        actual_cwd = cwd_match.group(1).strip() if cwd_match else cwd
        output = re.sub(r"(?m)^__ONE_SHOT_CWD__\s+.*$\n?", "", output)
        self._last_cwd = actual_cwd
        output = _strip_ansi(output)
        # 终端式信封：提示符 = 本次隔离进程的起始 cwd（命令的真实执行
        # 位置）。隔离命令里的 cd 不会影响 persistent shell，下一条命令
        # 的提示符会如实回到 persistent cwd——和真实终端的子 shell 语义
        # 一致，模型看提示符即可自行推断。
        return _format_bash_envelope(cwd, command, exit_code, output)

    # ===================== 执行命令 =====================
    async def execute(self, command: str, timeout: int = SANDBOX_TIMEOUT_SEC) -> str:
        """在沙箱中执行 bash 命令，超时自动终止

        v2.3：不再接受 ``progress_callback``——bash 工具执行期间不推送
        任何进度预览。原始 stdout 对用户价值有限（多为命令日志），
        频繁刷新草稿只换来视觉抖动 + Telegram API 限流压力。
        卡片摘要由 ``tool_call_loop`` 用 ``_generate_initial_tool_summary``
        生成的命令片段保持不变；最终结果由 ``update_tool_item``
        一次性写入包含 Input/Output 块级结构的完整卡片。
        """
        # ★ init 在 workspace lock 外面执行：R2 网络同步可能耗时数秒，
        #   不应阻塞其他工具调用获取 workspace lock。init 只需要 init_lock
        #   （在 _ensure_workspace_initialized 内部获取），与 workspace lock 独立。
        #   init 失败不阻断 bash：本地 workspace 可能不全但 bash 仍可运行。
        # ★ 显式传 self.namespace：避免依赖 ContextVar 在 background task
        #   里不可见时把 upload/download 建到错误的 namespace 下。
        #   start() 已经预创建过这两棵子树，这里只是兜底——任何路径下
        #   失败都不会让 cp 报 "No such file or directory"，因为 start()
        #   时目录已经存在。
        try:
            await _ensure_runtime_workspace(self.chat_id, self.namespace)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"_ensure_workspace_initialized failed (continue): {e}")

        lock = await _get_workspace_lock(self.chat_id)
        async with lock:
            if self.proc is None or self.proc.returncode is not None:
                await self.start()

            if not self._is_safe(command):
                # 给出更可操作的错误信息，让模型知道为什么被拒、该怎么做。
                if self._last_cwd and is_inside_upload_or_download(self._last_cwd):
                    return (
                        f"Error: Command rejected — current shell cwd is inside an "
                        f"upload/ or download/ staging tree ({self._last_cwd}). "
                        f"These directories are read/write data buffers, not execution "
                        f"roots: running commands here (e.g. pip install) would pollute "
                        f"the staging area. Run `cd` to return to your workdir first, "
                        f"then re-issue the command."
                    )
                if self._UPLOAD_DOWNLOAD_CD_PATTERN.search(command):
                    return (
                        "Error: Command rejected — `cd` into upload/ or download/ is "
                        "not allowed. These directories are data buffers directly inside "
                        "your workspace root: read and write files in them via relative "
                        "paths from your workdir (e.g. `cp out.txt upload/out.txt`, "
                        "`cat download/doc.pdf`), but never execute commands from "
                        "inside them."
                    )
                return f"Error: Command rejected for security reasons: {command}"

            # Any command containing a heredoc, OR any command bash would
            # consider syntactically unterminated (unclosed quote/backtick/
            # paren — e.g. a truncated `python3 -c "..."` multi-line string),
            # is executed in a one-shot shell instead of the persistent one.
            # A persistent stdin-backed shell blocks forever on unterminated
            # input and silently consumes our synthetic end marker as part of
            # it, which is what previously caused ~300s hangs before the
            # sandbox timeout kicked in and force-restarted the session.
            has_heredoc = bool(re.search(r"<<-?\s*(?:[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?)", command))
            if has_heredoc or await self._is_unterminated(command):
                return await self._execute_heredoc_isolated(
                    command, timeout=timeout
                )

            tag = uuid.uuid4().hex[:8]
            marker = f"__END_{tag}__"
            cwd_marker = f"__CWD_{tag}__"
            # 信封提示符取 persistent shell 当前 cwd（命令的真实执行位置）。
            # 隔离执行（heredoc）不回写 _persistent_cwd，因此提示符不会
            # 被子 shell 的 cd 污染，永远真实。
            prompt_cwd = self._persistent_cwd or str(self.workdir.absolute())
            # 默认 shell 启动目录为 workspace/workspace root。模型决定使用 skill 后，
            # 可自行 `cd skills/<skill_id>`；persistent bash 会保留该 cwd。
            # ★ 关键：在输出 marker 前先输出一个换行，确保 marker 单独占一行。
            #   如果命令输出不以换行结尾（如 cat 无换行文件、printf 无 \n），
            #   echo 的输出会粘在前一行，readline() 永远读不到以 marker 开头的行，
            #   导致整个会话 hang 死。
            # 同时记录命令结束后的真实 PWD，用于结果显示；不会改变 shell 状态。
            # ★ 包裹命令的三个关键点：
            #   1) $? 必须放在引号外（若把 $? 包进单引号，bash 不展开，
            #      退出码永远是 unknown）；
            #   2) 退出码必须在命令结束的下一刻立刻捕获（__rc=$?），
            #      否则中间的 echo/printf 会把 $? 重置为 0，失败命令
            #      在模型眼里和成功无异；
            #   3) 模型生成的多行命令（如 `python3 -c "..."` 跨行书写）
            #      几乎总带尾随换行，直接用 "; __rc=$?..." 拼接会让分号
            #      落在新一行的行首——bash 对行首的孤立分号直接报
            #      syntax error near unexpected token `;'——marker 永远不会
            #      被输出，退出码停留在 unknown。
            #      因此先 rstrip() 去掉尾随空白/换行，再用 "\n" 换行拼接
            #      退出码捕获。换行在 bash 里同样是命令分隔符，且能正确
            #      终结行尾注释（`cmd # note` 后直接拼 `;` 会把整段包装
            #      代码吞进注释，导致 marker 丢失、会话卡到超时）。
            #   另：每次执行使用带唯一后缀的退出码变量名（__rc_<tag>）。
            #   持久 shell 里同名变量会在多次 execute() 之间残留，若某次
            #   赋值被跳过（如命令以反斜杠续行符结尾时，`__rc=$?` 会被
            #   join 进上一条命令的参数里），echo 会读到上一次的陈旧退出码，
            #   把失败伪装成成功。唯一变量名保证最坏情况是 "unknown"
            #   而不是错误的旧值。
            rc_var = f"__rc_{tag}"
            cmd_body = command.rstrip()
            full_cmd = (
                f"{cmd_body}\n{rc_var}=$?; echo; printf '{cwd_marker} %s\n' \"$PWD\"; "
                f"echo '{marker}' \"${rc_var}\"\n"
            )

            # start() 已保证 proc 非空且 stdin=PIPE（见 _start_locked）；
            # 以下断言仅用于类型收窄，不改变运行时行为。
            assert self.proc is not None and self.proc.stdin is not None
            pending = ""  # 仅供超时兑底里的防御性 locals() 检查（历史行为保持：恒为空，不追加尾部输出）
            try:
                self.proc.stdin.write(full_cmd.encode('utf-8'))
                await self.proc.stdin.drain()

                output_buffer = _BashOutputBuffer()
                exit_code = "unknown"

                async def read_until_marker() -> None:
                    nonlocal exit_code
                    # stdout=PIPE 由 _start_locked 保证；仅作类型收窄。
                    assert self.proc is not None and self.proc.stdout is not None
                    # marker 可能跨 chunk 被拆开，因此只保留一个很小的尾部用于跨 chunk 匹配；
                    # 已经确定不可能包含 marker 的前缀立即写入输出缓冲，避免每次都 O(n) 拼接。
                    pending = ""
                    keep_tail = len(marker) + 64
                    while True:
                        chunk = await self.proc.stdout.read(4096)
                        if not chunk:
                            if pending:
                                output_buffer.add(pending)
                                pending = ""
                            break

                        pending += chunk.decode('utf-8', errors='replace')
                        marker_pos = pending.find(marker)
                        if marker_pos >= 0:
                            # marker 前是命令真实输出；后面紧接着是 echo 的退出码。
                            if marker_pos:
                                output_buffer.add(pending[:marker_pos])
                            marker_tail = pending[marker_pos:]
                            match = re.search(rf"{re.escape(marker)}\s+(-?\d+)", marker_tail)
                            if match:
                                exit_code = match.group(1)
                            pending = ""
                            break

                        if len(pending) > keep_tail:
                            output_buffer.add(pending[:-keep_tail])
                            pending = pending[-keep_tail:]

                await asyncio.wait_for(read_until_marker(), timeout=timeout)

                # 有界缓冲：预算内完整保留；超预算保留头+尾并省略中间，
                # 上限由 SANDBOX_OUTPUT_MAX_CHARS 控制（默认 80000）。
                output = output_buffer.finalize()
                output = _strip_ansi(output)

                # 提取命令结束后的真实 PWD，同时把内部 marker 从用户输出中移除。
                actual_cwd = str(self.workdir.absolute())
                cwd_match = re.search(r'(?m)^' + re.escape(cwd_marker) + r' (.+)$', output)
                if cwd_match:
                    actual_cwd = cwd_match.group(1).strip()
                    output = re.sub(r'(?m)^' + re.escape(cwd_marker) + r' .*$\n?', '', output)

                # 记录最新 cwd，下一次 _is_safe 会据此拒绝在 upload/ 或 download/
                # 子树内继续执行命令。即便 cd 进入被拒，模型也可能通过 pushd /
                # 子 shell 等方式间接进入，这里再做一次防御性检查。
                self._last_cwd = actual_cwd
                self._persistent_cwd = actual_cwd

                # 合并后台同步；不会为每次 Bash 创建一个新的全量上传任务。

                # 终端式信封：模型像人看终端一样，从提示符直接读出命令运行
                # 目录；cd 之后下一条命令的提示符随之变化。
                return _format_bash_envelope(prompt_cwd, command, exit_code, output)

            except asyncio.CancelledError:
                # 外层 asyncio.wait_for 超时、请求取消或应用关闭时，必须同步清理
                # 当前 bash 进程；否则“工具已超时”但实际命令仍在后台继续执行，
                # 下一次 Bash 还可能复用同一个脏 session。
                logger.warning(
                    "Bash execution cancelled; killing session chat_id=%s cmd=%s",
                    self.chat_id,
                    command[:100],
                )
                try:
                    await asyncio.shield(self.close())
                except Exception:
                    logger.exception("Failed to clean up cancelled bash session chat_id=%s", self.chat_id)
                raise

            except asyncio.TimeoutError:
                logger.warning(f"Bash timeout chat_id={self.chat_id} cmd={command[:100]}")
                partial_output = ""
                try:
                    if 'pending' in locals() and pending:
                        output_buffer.add(pending)
                    partial_output = _strip_ansi(output_buffer.finalize()).strip()
                except Exception:
                    pass
                try:
                    # 同上：start() 保证会话进程存在，仅作类型收窄。
                    assert self.proc is not None
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except ProcessLookupError:
                    pass
                # 重启会话
                await self.close()
                msg = f"Error: Command timed out after {timeout} seconds (sandbox killed & session will restart)"
                if partial_output:
                    return f"{msg}\n\nCaptured partial output before timeout:\n{partial_output}"
                return msg

            except Exception as e:
                logger.exception(f"Bash execute error chat_id={self.chat_id}")
                return f"Error: {str(e)}"

    # ===================== 关闭会话 =====================
    async def close(self) -> None:
        # 取消看门狗
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("bash session force-close wait timed out chat_id=%s", self.chat_id)
            except Exception:
                logger.exception("bash session close wait failed chat_id=%s", self.chat_id)
            finally:
                self.proc = None
            return
        self.proc = None

# =====================================================================
# BashSessionManager —— 多 chat 共享管理
# =====================================================================
class BashSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[tuple[int, str], BashSession] = {}
        self._lock = asyncio.Lock()

    async def get_session(self, chat_id: int, namespace: str | None = None) -> BashSession:
        resolved_namespace = workspace_namespace(chat_id, namespace)
        key = (chat_id, resolved_namespace)
        async with self._lock:
            if key not in self._sessions:
                session = BashSession(chat_id, resolved_namespace)
                await session.start()
                self._sessions[key] = session
            else:
                # 进程已死则重建
                s = self._sessions[key]
                if s.proc is None or s.proc.returncode is not None:
                    await s.start()
            return self._sessions[key]

    async def restart_session(self, chat_id: int, namespace: str | None = None) -> str:
        resolved_namespace = workspace_namespace(chat_id, namespace)
        key = (chat_id, resolved_namespace)
        async with self._lock:
            session = self._sessions.get(key)
            if session:
                await session.close()
                del self._sessions[key]
            new_session = BashSession(chat_id, resolved_namespace)
            await new_session.start()
            self._sessions[key] = new_session
            return "Bash session restarted (sandbox=landlock)"

    async def cleanup_all(self) -> None:
        """优雅关闭所有会话（应用退出时调用）"""
        async with self._lock:
            for s in self._sessions.values():
                try:
                    await s.close()
                except Exception:
                    logger.debug("cleanup_all 内部忽略的异常", exc_info=True)
                    pass
            self._sessions.clear()

_bash_manager = BashSessionManager()

# =====================================================================
# execute_bash —— 工具调用入口
# v2.3：移除 ``progress_callback`` 参数。bash 执行期间不再推送任何进度
# 预览（卡片摘要保持命令片段，最终结果由 update_tool_item 一次性写入
# 包含 Input/Output 块级结构的完整卡片）。
# =====================================================================
async def execute_bash(
    chat_id: int,
    command: str = "",
    restart: bool = False,
    namespace: str | None = None,
) -> str:
    resolved_namespace = workspace_namespace(chat_id, namespace)
    if restart:
        result = await _bash_manager.restart_session(chat_id, resolved_namespace)
        return result
    if not command:
        return "Error: command is required (or set restart=true)"
    try:
        session = await _bash_manager.get_session(chat_id, resolved_namespace)
    except RuntimeError as e:
        return f"Error: {e}"
    # 执行命令；workspace 本地文件不会自动同步到 R2。
    return await session.execute(command)
