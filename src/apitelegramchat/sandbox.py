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

from apitelegramchat.workspace_paths import workspace_workdir, runtime_cache_root

logger = logging.getLogger(__name__)

# ---------- 沙箱配置（环境变量可调） ----------
SANDBOX_MAX_PROCS = int(os.getenv("SANDBOX_MAX_PROCS", "50"))
SANDBOX_MAX_CPU_SEC = int(os.getenv("SANDBOX_MAX_CPU_SEC", "300"))   # 5 分钟 CPU
SANDBOX_MAX_FILE_SIZE = int(os.getenv("SANDBOX_MAX_FILE_SIZE", str(100 * 1024 * 1024)))  # 100MB/文件
SANDBOX_MAX_OPEN_FILES = int(os.getenv("SANDBOX_MAX_OPEN_FILES", "256"))
SANDBOX_TIMEOUT_SEC = int(os.getenv("SANDBOX_TIMEOUT_SEC", "300"))

# ---------- libc ----------
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:
    _libc = None

PR_SET_NO_NEW_PRIVS = 38


def _set_no_new_privs() -> bool:
    """阻止 setuid 提权；失败时返回 False。"""
    if _libc is None:
        return False
    rc = _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if rc != 0:
        err = ctypes.get_errno()
        logger.error("prctl(NO_NEW_PRIVS) failed: %s", os.strerror(err))
        return False
    return True


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
# x86_64 syscall numbers. The deployment image is x86_64; fail closed on
# unsupported architectures rather than guessing syscall numbers.
_SYS_LANDLOCK_SYSCALLS = {
    "x86_64": (444, 445, 446),
    "amd64": (444, 445, 446),
    "aarch64": (444, 445, 446),
    "arm64": (444, 445, 446),
}

LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

try:
    SYS_LANDLOCK_CREATE_RULESET, SYS_LANDLOCK_ADD_RULE, SYS_LANDLOCK_RESTRICT_SELF = _SYS_LANDLOCK_SYSCALLS[os.uname().machine]
except KeyError:
    SYS_LANDLOCK_CREATE_RULESET = SYS_LANDLOCK_ADD_RULE = SYS_LANDLOCK_RESTRICT_SELF = -1

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


class SandboxSetupError(RuntimeError):
    """The filesystem sandbox could not be installed."""


_landlock_abi: Optional[int] = None


def _landlock_abi_version() -> int:
    """Return the Landlock ABI version, or 0 when unavailable.

    The VERSION flag is mandatory here. Calling landlock_create_ruleset(NULL, 0, 0)
    is not a valid feature probe and returns ENOSYS/EFAULT on many kernels.
    """
    global _landlock_abi
    if _landlock_abi is not None:
        return _landlock_abi
    if _libc is None or SYS_LANDLOCK_CREATE_RULESET < 0:
        _landlock_abi = 0
        return 0
    ctypes.set_errno(0)
    rc = _libc.syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        None,
        0,
        LANDLOCK_CREATE_RULESET_VERSION,
    )
    if rc < 0:
        _landlock_abi = 0
        return 0
    _landlock_abi = int(rc)
    logger.info("Landlock supported (ABI %d)", _landlock_abi)
    return _landlock_abi


def _landlock_supported() -> bool:
    return _landlock_abi_version() >= 1


def _handled_access_mask(abi: int) -> int:
    """Return only access bits understood by the detected ABI."""
    mask = _LANDLOCK_ALL_ACCESS_V1
    # ABI 2: LANDLOCK_ACCESS_FS_REFER
    if abi >= 2:
        mask |= 1 << 13
    # ABI 3: LANDLOCK_ACCESS_FS_TRUNCATE
    if abi >= 3:
        mask |= 1 << 14
    return mask


def _apply_landlock(workspace_path: str) -> bool:
    """Install a deny-by-default Landlock filesystem policy for the child.

    The workspace tree is the writable application sandbox. R2 persistence is
    deliberately handled outside the workspace tree; the workspace is never mirrored wholesale to R2.
    System trees needed to execute
    bash are explicitly read/execute-only. Every syscall and every rule-add
    operation is checked; a partial policy is never accepted.
    """
    abi = _landlock_abi_version()
    if abi < 1:
        return False

    handled = _handled_access_mask(abi)
    try:
        workspace = os.path.realpath(workspace_path)
        if not os.path.isdir(workspace):
            raise SandboxSetupError(f"workspace is not a directory: {workspace}")

        attr = _LandlockRulesetAttr(handled_access_fs=handled)
        ruleset_fd = _libc.syscall(
            SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(attr),
            ctypes.sizeof(attr),
            0,
        )
        if ruleset_fd < 0:
            logger.error("landlock_create_ruleset failed: errno=%s", ctypes.get_errno())
            return False

        try:
            def add_path_rule(path: str, allowed: int) -> None:
                fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    rule = _LandlockPathBeneathAttr(
                        allowed_access=allowed,
                        parent_fd=fd,
                    )
                    rc = _libc.syscall(
                        SYS_LANDLOCK_ADD_RULE,
                        ruleset_fd,
                        LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(rule),
                        0,
                    )
                    if rc < 0:
                        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
                finally:
                    os.close(fd)

            # The only writable/readable user tree. Since Landlock is scoped to
            # this directory fd, ../ resolves outside the rule and is denied.
            add_path_rule(workspace, handled)

            # Read/execute-only runtime dependencies. No WRITE/MAKE/REMOVE bits
            # are granted here, so the shell cannot modify the application image.
            runtime_ro = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR | LANDLOCK_ACCESS_FS_EXECUTE
            for d in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/dev", "/proc", "/sys"):
                if not os.path.isdir(d):
                    continue
                add_path_rule(d, runtime_ro)

            rc = _libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
            if rc < 0:
                logger.error("landlock_restrict_self failed: errno=%s", ctypes.get_errno())
                return False
            return True
        finally:
            os.close(ruleset_fd)
    except Exception as exc:
        logger.error("Landlock policy installation failed: %s", exc)
        return False


# =====================================================================
# preexec_fn —— fork 后 exec 前调用
# =====================================================================
def _preexec_sandbox(workspace_path: str):
    """Install all mandatory child restrictions before exec("bash")."""
    import resource

    if _libc is None:
        raise SandboxSetupError("libc is unavailable; cannot install sandbox")

    # no_new_privs is part of the sandbox contract, not a best-effort warning.
    if _set_no_new_privs() is False:
        raise SandboxSetupError("PR_SET_NO_NEW_PRIVS failed")

    if not _apply_landlock(workspace_path):
        raise SandboxSetupError("Landlock filesystem sandbox could not be installed")

    resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_MAX_CPU_SEC, SANDBOX_MAX_CPU_SEC))
    resource.setrlimit(resource.RLIMIT_FSIZE, (SANDBOX_MAX_FILE_SIZE, SANDBOX_MAX_FILE_SIZE))
    resource.setrlimit(resource.RLIMIT_NOFILE, (SANDBOX_MAX_OPEN_FILES, SANDBOX_MAX_OPEN_FILES))


# =====================================================================
# 构造 bash argv / env
# =====================================================================
def build_sandbox_argv() -> list:
    """bash 进程的启动参数"""
    return ["/bin/bash", "--noprofile", "--norc", "-s"]


def build_sandbox_env(
    workspace: Path,
    chat_id: int,
    namespace: str | None = None,
) -> dict:
    """Build the shell environment from persistent, workspace-local runtime paths.

    Runtime caches live under the same workspace tree that Landlock already permits.
    Nothing is installed on every command: the host toolchain (/usr/bin/python3, gcc,
    etc.) is reused and package/build caches survive Bash session restarts.
    """
    workdir = workspace_workdir(chat_id, namespace)
    workdir_abs = str(workdir.absolute())
    cache_root = runtime_cache_root(chat_id, namespace)
    cache_root.mkdir(parents=True, exist_ok=True)
    pip_cache = cache_root / "pip"
    ccache_dir = cache_root / "ccache"
    tmp_dir = cache_root / "tmp"
    runtime_bin = cache_root / "bin"

    # All common ML/model-download caches are explicitly rooted in runtime.
    # Do not rely only on HOME: some libraries use their own environment variables.
    xdg_cache = cache_root / "xdg_cache"
    hf_home = cache_root / "huggingface"
    hf_hub_cache = hf_home / "hub"
    hf_datasets_cache = hf_home / "datasets"
    hf_modules_cache = hf_home / "modules"
    torch_home = cache_root / "torch"
    transformers_cache = cache_root / "transformers"
    for d in (
        pip_cache, ccache_dir, tmp_dir, runtime_bin,
        xdg_cache, hf_home, hf_hub_cache, hf_datasets_cache,
        hf_modules_cache, torch_home, transformers_cache,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # ccache is installed in the image, so make compiler invocation cache-aware
    # without modifying files under /usr. These symlinks are idempotent and survive
    # Bash session restarts because they live under the workspace runtime cache.
    ccache_path = "/usr/bin/ccache"
    if os.path.isfile(ccache_path) and os.access(ccache_path, os.X_OK):
        for compiler_name in ("gcc", "g++", "cc", "c++"):
            link = runtime_bin / compiler_name
            try:
                if link.is_symlink() or link.exists():
                    if link.is_symlink() and os.readlink(link) == ccache_path:
                        continue
                    link.unlink()
                link.symlink_to(ccache_path)
            except OSError as exc:
                logger.debug("Unable to prepare ccache wrapper %s: %s", link, exc)

    # Keep runtime_bin first only for local wrappers. The actual compiler remains the
    # system toolchain baked into the image; no apt/pip install happens per Bash run.
    return {
        "PATH": f"{runtime_bin}:{cache_root / 'python_user' / 'bin'}:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "HOME": str(cache_root),
        "USER": f"chat{chat_id}",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
        "SHELL": "/bin/bash",
        "PWD": workdir_abs,
        "HISTFILE": "/dev/null",
        "HISTSIZE": "0",
        "HISTFILESIZE": "0",
        "TMPDIR": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "PYTHONUNBUFFERED": "1",
        # Python bytecode 不是用户文件；禁止写入用户 files 层，避免进入 R2 同步。
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CACHE_DIR": str(pip_cache),
        "PYTHONUSERBASE": str(cache_root / "python_user"),
        "npm_config_cache": str(cache_root / "npm"),
        "CARGO_HOME": str(cache_root / "cargo"),
        "RUSTUP_HOME": str(cache_root / "rustup"),
        "CCACHE_DIR": str(ccache_dir),
        "PYTHONPYCACHEPREFIX": str(cache_root / "pycache"),
        # Explicit ML/model caches: keep downloaded weights and package metadata
        # out of the workspace tree even when a library does not derive the path from HOME.
        "XDG_CACHE_HOME": str(xdg_cache),
        "HF_HOME": str(hf_home),
        "HF_HUB_CACHE": str(hf_hub_cache),
        "HF_DATASETS_CACHE": str(hf_datasets_cache),
        "HF_MODULES_CACHE": str(hf_modules_cache),
        "TRANSFORMERS_CACHE": str(transformers_cache),
        "TORCH_HOME": str(torch_home),
        "KERAS_HOME": str(cache_root / "keras"),
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
