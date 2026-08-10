# =====================================================================
# sandbox.py — Landlock 沙箱 + 资源限制 + Fork Bomb 看门狗
# =====================================================================
# 设计原则:
#   1. 每个 chat_id 拿到独立的 Landlock 文件系统沙箱（限制在 workspace 内）
#   2. 敏感环境变量不传入子进程
#   3. Landlock 限制不可逆、子进程继承，防止访问 workspace 之外的任何路径
#   4. 看门狗监控进程树大小，超过阈值杀掉沙箱（防 fork bomb）
#   5. rlimit 限制 CPU/文件大小/fd 数量
#
# 不使用 bwrap —— Render / Heroku / 非 privileged Docker 内核禁了
# unprivileged userns，bwrap 永远起不来。Landlock 是 Linux 5.13+ 的
# 非特权文件系统隔离方案，不需要任何 capability。
# =====================================================================

import asyncio
import ctypes
import logging
import os
import signal
from pathlib import Path
from typing import Optional

from apitelegramchat.workspace_paths import workspace_workdir

logger = logging.getLogger(__name__)

# ---------- 沙箱配置（环境变量可调） ----------
SANDBOX_MAX_PROCS = int(os.getenv("SANDBOX_MAX_PROCS", "50"))
SANDBOX_MAX_CPU_SEC = int(os.getenv("SANDBOX_MAX_CPU_SEC", "300"))   # 5 分钟 CPU
SANDBOX_MAX_FILE_SIZE = int(os.getenv("SANDBOX_MAX_FILE_SIZE", str(100 * 1024 * 1024)))  # 100MB/文件
SANDBOX_MAX_OPEN_FILES = int(os.getenv("SANDBOX_MAX_OPEN_FILES", "256"))
SANDBOX_TIMEOUT_SEC = int(os.getenv("SANDBOX_TIMEOUT_SEC", "120"))

# ---------- libc ----------
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:
    _libc = None

PR_SET_NO_NEW_PRIVS = 38


def _set_no_new_privs() -> None:
    """阻止 setuid 提权"""
    if _libc is None:
        return
    rc = _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if rc != 0:
        err = ctypes.get_errno()
        logger.warning(f"prctl(NO_NEW_PRIVS) failed: {os.strerror(err)}")


# =====================================================================
# Landlock（非特权文件系统隔离）
# =====================================================================
# Linux 5.13+ 的 Landlock 允许非特权进程限制自己的文件系统访问范围。
# 不需要 userns / CAP_SYS_ADMIN / privileged 容器。
#
# 原理：fork 后 exec 前，在子进程里调 landlock_create_ruleset +
# landlock_add_rule + landlock_restrict_self，给自己加规则：
#   - workspace 目录：可读写
#   - /usr /bin /lib /etc：只读 + 可执行（bash/python 能跑）
#   - /dev /proc /sys：只读（/dev/null /dev/urandom 等可读）
#   - 其他（state/、r2_cache/、/home、/app 源码）：全部拒绝
# 限制不可逆，子进程继承。

# Landlock 常量（<linux/landlock.h>）
LANDLOCK_RULE_PATH_BENEATH = 1

LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12

# x86_64 syscall 号
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446

# 所有 v1 的 access flags（Linux 5.13+ 通用）
_LANDLOCK_ALL_ACCESS_V1 = (
    LANDLOCK_ACCESS_FS_EXECUTE |
    LANDLOCK_ACCESS_FS_WRITE_FILE |
    LANDLOCK_ACCESS_FS_READ_FILE |
    LANDLOCK_ACCESS_FS_READ_DIR |
    LANDLOCK_ACCESS_FS_REMOVE_DIR |
    LANDLOCK_ACCESS_FS_REMOVE_FILE |
    LANDLOCK_ACCESS_FS_MAKE_CHAR |
    LANDLOCK_ACCESS_FS_MAKE_DIR |
    LANDLOCK_ACCESS_FS_MAKE_REG |
    LANDLOCK_ACCESS_FS_MAKE_SOCK |
    LANDLOCK_ACCESS_FS_MAKE_FIFO |
    LANDLOCK_ACCESS_FS_MAKE_BLOCK |
    LANDLOCK_ACCESS_FS_MAKE_SYM
)

# 只读 + 可执行
_LANDLOCK_READ_EXEC = (
    LANDLOCK_ACCESS_FS_READ_FILE |
    LANDLOCK_ACCESS_FS_READ_DIR |
    LANDLOCK_ACCESS_FS_EXECUTE
)


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


_landlock_tested: Optional[bool] = None


def _landlock_supported() -> bool:
    """检测当前内核是否支持 Landlock（结果缓存）"""
    global _landlock_tested
    if _landlock_tested is not None:
        return _landlock_tested
    if _libc is None:
        _landlock_tested = False
        return False
    try:
        # landlock_create_ruleset(NULL, 0, 0) 返回支持的属性数量
        rc = _libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, 0, 0)
        _landlock_tested = rc >= 0
        if _landlock_tested:
            logger.info(f"Landlock supported (v{rc} attributes)")
    except Exception:
        _landlock_tested = False
    return _landlock_tested


def _apply_landlock(workspace_path: str) -> bool:
    """
    在当前进程上应用 Landlock 限制。
    必须在 fork 后、exec 前调用（即 preexec_fn 里）。

    workspace_path: 允许读写的目录（递归）
    返回 True 表示成功施加限制，False 表示 Landlock 不可用或失败。
    """
    if not _landlock_supported():
        return False

    try:
        # 1. 创建 ruleset
        attr = _LandlockRulesetAttr(handled_access_fs=_LANDLOCK_ALL_ACCESS_V1)
        ruleset_fd = _libc.syscall(
            SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(attr),
            ctypes.sizeof(attr),
            0,
        )
        if ruleset_fd < 0:
            logger.warning(f"landlock_create_ruleset failed: {ctypes.get_errno()}")
            return False

        try:
            # 2. 添加 workspace 规则（全权限：读写执行创建删除）
            ws_fd = os.open(workspace_path, os.O_PATH | os.O_CLOEXEC)
            try:
                beneath = _LandlockPathBeneathAttr(
                    allowed_access=_LANDLOCK_ALL_ACCESS_V1,
                    parent_fd=ws_fd,
                )
                rc = _libc.syscall(
                    SYS_LANDLOCK_ADD_RULE,
                    ruleset_fd,
                    LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.byref(beneath),
                    0,
                )
                if rc < 0:
                    logger.warning(f"landlock_add_rule(workspace) failed: {ctypes.get_errno()}")
                    return False
            finally:
                os.close(ws_fd)

            # 3. 添加只读系统目录规则（bash/python 能跑、库能加载）
            for d in ("/usr", "/bin", "/sbin", "/lib", "/lib64",
                      "/etc", "/dev", "/proc", "/sys"):
                if not os.path.exists(d):
                    continue
                try:
                    d_fd = os.open(d, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    continue
                try:
                    beneath = _LandlockPathBeneathAttr(
                        allowed_access=_LANDLOCK_READ_EXEC,
                        parent_fd=d_fd,
                    )
                    _libc.syscall(
                        SYS_LANDLOCK_ADD_RULE,
                        ruleset_fd,
                        LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(beneath),
                        0,
                    )
                except OSError:
                    pass
                finally:
                    os.close(d_fd)

            # 4. 施加限制（不可逆）
            rc = _libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
            if rc < 0:
                logger.warning(f"landlock_restrict_self failed: {ctypes.get_errno()}")
                return False
            return True
        finally:
            os.close(ruleset_fd)
    except Exception as e:
        logger.warning(f"Landlock apply failed: {e}")
        return False


# =====================================================================
# preexec_fn —— fork 后 exec 前调用
# =====================================================================
def _preexec_sandbox(workspace_path: str):
    """
    在子进程 fork 后、exec 前调用：
    1. no-new-privs（阻止 setuid 提权）
    2. Landlock（文件系统隔离，限制在 workspace 内）
    3. setrlimit（CPU / 文件大小 / fd 上限）

    workspace_path: bash 进程允许读写的唯一目录。
    """
    import resource
    _set_no_new_privs()
    if not _apply_landlock(workspace_path):
        logger.warning("Landlock 不可用，仅靠 rlimit + no-new-privs（无文件系统隔离）")
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_MAX_CPU_SEC, SANDBOX_MAX_CPU_SEC))
        resource.setrlimit(resource.RLIMIT_FSIZE, (SANDBOX_MAX_FILE_SIZE, SANDBOX_MAX_FILE_SIZE))
        resource.setrlimit(resource.RLIMIT_NOFILE, (SANDBOX_MAX_OPEN_FILES, SANDBOX_MAX_OPEN_FILES))
    except (ValueError, OSError) as e:
        logger.warning(f"setrlimit failed: {e}")


# =====================================================================
# 构造 bash argv / env
# =====================================================================
def build_sandbox_argv() -> list:
    """bash 进程的启动参数"""
    return ["/bin/bash", "--noprofile", "--norc", "-s"]


def build_sandbox_env(workspace: Path, chat_id: int) -> dict:
    """沙箱环境变量（白名单，不传任何 API Key / Token / Secret）"""
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


# =====================================================================
# Fork Bomb 看门狗
# =====================================================================
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
