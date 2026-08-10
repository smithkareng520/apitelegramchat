# =====================================================================
# sandbox.py — Bubblewrap 沙箱启动器 + 资源限制 + Fork Bomb 看门狗
# =====================================================================
# 设计原则:
#   1. 每个 chat_id 拿到独立的 mount/pid/ipc/uts/cgroup 命名空间
#   2. 只暴露用户工作区与最小系统运行时，不挂载应用目录
#   3. 敏感环境变量不传入子进程
#   4. bwrap 不可用时默认禁用 bash，以免退回到不隔离的主机环境
#   5. 看门狗监控进程树大小，超过阈值杀掉沙箱 (防 fork bomb)
# =====================================================================

import asyncio
import ctypes
import logging
import os
import shutil
import signal
from pathlib import Path
from typing import Optional

from apitelegramchat.workspace_paths import workspace_workdir

logger = logging.getLogger(__name__)

# ---------- bwrap 路径 ----------
BWRAP = shutil.which("bwrap") or "/usr/bin/bwrap"

# ---------- 沙箱配置（环境变量可调） ----------
SANDBOX_UNSHARE_NET = os.getenv("SANDBOX_UNSHARE_NET", "1") == "1"
SANDBOX_MAX_PROCS = int(os.getenv("SANDBOX_MAX_PROCS", "50"))
SANDBOX_MAX_CPU_SEC = int(os.getenv("SANDBOX_MAX_CPU_SEC", "300"))   # 5 分钟 CPU
SANDBOX_MAX_FILE_SIZE = int(os.getenv("SANDBOX_MAX_FILE_SIZE", str(100 * 1024 * 1024)))  # 100MB/文件
SANDBOX_MAX_OPEN_FILES = int(os.getenv("SANDBOX_MAX_OPEN_FILES", "256"))
SANDBOX_TIMEOUT_SEC = int(os.getenv("SANDBOX_TIMEOUT_SEC", "120"))
SANDBOX_ALLOW_FALLBACK = os.getenv("SANDBOX_ALLOW_FALLBACK", "0") == "1"

# ---------- 只读共享的系统目录（每个会话只读挂载） ----------
_RO_BINDS = [
    "/usr", "/bin", "/lib", "/lib64",
    "/etc/alternatives", "/etc/ssl", "/etc/terminfo",
    "/etc/passwd", "/etc/group",          # 仅元数据，无密码
    "/etc/resolv.conf", "/etc/hosts",     # DNS 必需
    "/etc/nsswitch.conf",
]

# ---------- libc (用于 prctl / prlimit) ----------
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:
    _libc = None

PR_SET_NO_NEW_PRIVS = 38
RLIMIT_CPU = 0
RLIMIT_FSIZE = 1
RLIMIT_NOFILE = 7
RLIMIT_NPROC = 6


def _set_no_new_privs() -> None:
    """阻止 setuid 提权（fallback 模式使用）"""
    if _libc is None:
        return
    rc = _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if rc != 0:
        err = ctypes.get_errno()
        logger.warning(f"prctl(NO_NEW_PRIVS) failed: {os.strerror(err)}")


# ---------- bwrap 可用性检测（首次调用缓存） ----------
_bwrap_available: Optional[bool] = None


async def _test_bwrap() -> bool:
    """测试 bwrap 是否能在当前容器内创建沙箱"""
    if not Path(BWRAP).exists():
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            BWRAP,
            "--unshare-user-try",
            "--unshare-pid-try",
            "--unshare-ipc-try",
            "--die-with-parent",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "/bin/true",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace") if stderr else ""
            logger.warning(f"bwrap test failed (rc={proc.returncode}): {err}")
            return False
        return True
    except Exception as e:
        logger.warning(f"bwrap test exception: {e}")
        return False


async def is_bwrap_available() -> bool:
    global _bwrap_available
    if _bwrap_available is None:
        _bwrap_available = await _test_bwrap()
        logger.info(f"bwrap available: {_bwrap_available}")
    return _bwrap_available


# ---------- 构造 bwrap argv ----------
def build_bwrap_argv(workspace: Path, chat_id: int) -> list:
    """
    为单个 chat_id 构造 bwrap 启动参数
    workspace: 该 chat 的根工作区绝对路径
    """
    ws = str(workspace.absolute())
    workspace_mount = "/workspace"
    workdir = workspace_workdir(chat_id)
    workdir_abs = workspace_mount

    argv = [
        BWRAP,
        # ===== 命名空间隔离 =====
        "--unshare-user",            # 新 user namespace
        "--unshare-pid",             # 新 PID namespace（看不见宿主进程）
        "--unshare-ipc",             # IPC 隔离
        "--unshare-uts",             # hostname 隔离
        "--unshare-cgroup-try",      # cgroup 隔离（best-effort）

        # ===== 生命周期绑定 =====
        "--die-with-parent",         # Python 进程死亡时沙箱一起死
        "--new-session",             # detach from controlling tty

        # ===== 文件系统骨架 =====
        "--dev", "/dev",             # /dev  tmpfs
        "--proc", "/proc",           # /proc procfs
        "--tmpfs", "/tmp",           # /tmp tmpfs
        "--tmpfs", "/run",           # /run tmpfs
        "--tmpfs", "/var/tmp",       # /var/tmp tmpfs

        # ===== 只读挂载系统目录 =====
    ]

    for d in _RO_BINDS:
        if Path(d).exists():
            argv += ["--ro-bind", d, d]

    # 将用户工作区仅挂载到沙箱内部的 /workspace，避免暴露主机上的真实路径
    argv += ["--dir", workspace_mount]
    argv += ["--bind", ws, workspace_mount]

    # ===== 网络 =====
    if SANDBOX_UNSHARE_NET:
        argv += ["--unshare-net"]
        logger.info(f"Sandbox {chat_id}: network isolated")
    # 否则共享宿主网络（需显式设置 SANDBOX_UNSHARE_NET=0，便于 curl / pip install）

    # ===== 环境变量（白名单） =====
    # bwrap --clearenv 会清空所有环境，然后我们只放白名单
    safe_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "HOME": workdir_abs,
        "USER": f"chat{chat_id}",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
        "SHELL": "/bin/bash",
        "HISTFILE": "/dev/null",
        "HISTSIZE": "0",
        "HISTFILESIZE": "0",
    }
    argv += ["--clearenv"]
    for k, v in safe_env.items():
        argv += ["--setenv", k, v]

    # ===== workspace =====
    argv += ["--chdir", workspace_mount]

    # ===== 实际启动的进程 =====
    argv += ["/bin/bash", "--noprofile", "--norc", "-s"]

    return argv


# ---------- 构造 fallback argv（无 bwrap 时） ----------
def build_fallback_argv(workspace: Path, chat_id: int) -> list:
    """弱模式：仅 env 清洗 + no-new-privs（无命名空间隔离）"""
    return ["/bin/bash", "--noprofile", "--norc", "-s"]


def build_fallback_env(workspace: Path, chat_id: int) -> dict:
    """fallback 模式下的最小 env（同样不传任何 API Key）"""
    workdir = workspace_workdir(chat_id)
    workdir_abs = str(workdir.absolute())
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "HOME": workdir_abs,
        "USER": f"chat{chat_id}",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
        "SHELL": "/bin/bash",
        "PWD": workdir_abs,
        "HISTFILE": "/dev/null",
        "HISTSIZE": "0",
        "HISTFILESIZE": "0",
        # ★ 刻意不带任何 *API_KEY *TOKEN *SECRET *PASSWORD
    }


# ---------- preexec_fn（仅 fallback 模式） ----------
def _preexec_fallback():
    """在子进程 fork 后、exec 前调用"""
    import resource
    _set_no_new_privs()
    # Python 3.12+ 已移除 resource.error，统一使用 OSError
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_MAX_CPU_SEC, SANDBOX_MAX_CPU_SEC))
        resource.setrlimit(resource.RLIMIT_FSIZE, (SANDBOX_MAX_FILE_SIZE, SANDBOX_MAX_FILE_SIZE))
        resource.setrlimit(resource.RLIMIT_NOFILE, (SANDBOX_MAX_OPEN_FILES, SANDBOX_MAX_OPEN_FILES))
    except (ValueError, OSError) as e:
        logger.warning(f"setrlimit failed: {e}")


# ---------- Fork Bomb 看门狗 ----------
def _count_descendants(root_pid: int) -> int:
    """通过 /proc 统计进程树大小"""
    children_map = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as f:
                    data = f.read().decode("utf-8", errors="replace")
                rparen = data.rfind(")")
                parts = data[rparen + 2:].split()
                ppid = int(parts[1])
                children_map.setdefault(ppid, []).append(int(entry))
            except (IOError, ValueError, IndexError):
                continue
    except OSError:
        return 1

    count = 0
    queue = [root_pid]
    seen = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        count += 1
        queue.extend(children_map.get(pid, []))
    return count


async def watchdog(proc: asyncio.subprocess.Process,
                   max_procs: int = SANDBOX_MAX_PROCS,
                   interval: float = 1.0) -> None:
    """周期性检查子进程树，超过 max_procs 立即 kill"""
    if proc.returncode is not None:
        return
    while proc.returncode is None:
        try:
            n = _count_descendants(proc.pid)
            if n > max_procs:
                logger.warning(
                    f"🚨 Watchdog: sandbox pid={proc.pid} spawned {n} > {max_procs} procs, killing"
                )
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return
        except Exception as e:
            logger.debug(f"watchdog tick error: {e}")
        await asyncio.sleep(interval)


# ---------- 资源限制（bwrap 模式下也可外层套一层） ----------
def apply_prlimit(proc: asyncio.subprocess.Process) -> None:
    """对 bwrap 子进程应用 rlimit（在 Python 端调用 prlimit）"""
    if _libc is None:
        return
    try:
        import ctypes.util
        # RLIMIT 定义见 /usr/include/bits/resource.h
        # struct rlimit { rlim_t rlim_cur; rlim_t rlim_max; }
        class Rlimit(ctypes.Structure):
            _fields_ = [("rlim_cur", ctypes.c_ulong), ("rlim_max", ctypes.c_ulong)]

        # prlimit(pid, resource, new_rlimit, old_rlimit)
        _libc.prlimit.argtypes = [
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(Rlimit), ctypes.POINTER(Rlimit)
        ]
        _libc.prlimit.restype = ctypes.c_int

        # CPU
        rl = Rlimit(SANDBOX_MAX_CPU_SEC, SANDBOX_MAX_CPU_SEC)
        _libc.prlimit(proc.pid, RLIMIT_CPU, ctypes.byref(rl), None)

        # FSIZE
        rl = Rlimit(SANDBOX_MAX_FILE_SIZE, SANDBOX_MAX_FILE_SIZE)
        _libc.prlimit(proc.pid, RLIMIT_FSIZE, ctypes.byref(rl), None)

        # NOFILE
        rl = Rlimit(SANDBOX_MAX_OPEN_FILES, SANDBOX_MAX_OPEN_FILES)
        _libc.prlimit(proc.pid, RLIMIT_NOFILE, ctypes.byref(rl), None)

        # NPROC — 注意: 在 user namespace 内对子进程无效，主要靠看门狗兜底
        rl = Rlimit(SANDBOX_MAX_PROCS, SANDBOX_MAX_PROCS)
        _libc.prlimit(proc.pid, RLIMIT_NPROC, ctypes.byref(rl), None)
    except Exception as e:
        logger.warning(f"prlimit failed: {e}")
