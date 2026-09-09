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
from typing import Any, Optional

from workspace_paths import workspace_workdir, runtime_cache_root
from net_shims import ensure_network_shims

logger = logging.getLogger(__name__)

# ---------- 沙箱配置（环境变量可调） ----------
SANDBOX_MAX_PROCS = int(os.getenv("SANDBOX_MAX_PROCS", "50"))
SANDBOX_MAX_CPU_SEC = int(os.getenv("SANDBOX_MAX_CPU_SEC", "300"))   # 5 分钟 CPU
SANDBOX_MAX_FILE_SIZE = int(os.getenv("SANDBOX_MAX_FILE_SIZE", str(100 * 1024 * 1024)))  # 100MB/文件
SANDBOX_MAX_OPEN_FILES = int(os.getenv("SANDBOX_MAX_OPEN_FILES", "256"))
SANDBOX_TIMEOUT_SEC = int(os.getenv("SANDBOX_TIMEOUT_SEC", "300"))

# ---------- 无输出空闲超时（v2.4 bash 防卡死） ----------
# 命令持续无输出超过该秒数即判定为卡死（典型：网络不可达时 connect 静默
# 挂起、交互提示等待、无输出死循环），提前 kill 并向模型返回可操作的
# 错误消息，而不是等满 SANDBOX_TIMEOUT_SEC（默认 300s）才超时。模型可
# 通过 bash 工具的 timeout 参数为已知的长静默命令禁用本保护。0 = 禁用。
SANDBOX_IDLE_TIMEOUT_SEC = int(os.getenv("SANDBOX_IDLE_TIMEOUT_SEC", "60"))
# bash 工具 timeout 参数允许的硬上限（秒）；外层工具超时（BASH_TOOL_CALL_TIMEOUT）
# 据此联动放大。
SANDBOX_TIMEOUT_HARD_MAX = int(os.getenv("SANDBOX_TIMEOUT_HARD_MAX", "600"))
# 沙箱内 Python 进程的默认 socket 超时（秒）。通过 sitecustomize.py 注入
# socket.setdefaulttimeout()，让忘记设超时的脚本（smtplib / urllib /
# requests / socket.create_connection）在网络不可达时快速失败，而不是按
# 内核默认 TCP 重试挂起约 2 分钟/次。0 = 不注入。该值会写入子进程环境
# （SANDBOX_SOCKET_TIMEOUT_SEC），由沙箱内 sitecustomize.py 读取。
SANDBOX_SOCKET_TIMEOUT_SEC = int(os.getenv("SANDBOX_SOCKET_TIMEOUT_SEC", "15"))

# 沙盒内固定身份（whoami / $USER / $LOGNAME / ls 属主列全部一致）。
# 必须与镜像内 passwd 用户名同步（见 Dockerfile 的 useradd claude 行），
# 否则 $USER 会与真实 uid 解析结果不一致。历史版本的 chat{chat_id} 已移除：
# chat id 属于路由/计费元数据，不应以环境变量形式暴露给模型可读的 shell ——
# 模型需要知道自己在哪个工作区时，读 $WORKSPACE 路径即可，且那是必要信息。
SANDBOX_USER = os.getenv("APITELEGRAMCHAT_SANDBOX_USER", "claude").strip() or "claude"

# ---------- libc ----------
# Any：_libc 加载失败时为 None，各调用点各自判空；若声明为 ctypes.CDLL | None，
# _apply_landlock 内未判空直接 syscall 的既有调用点会级联报错，Any 最小且不失真。
_libc: Any
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:
    _libc = None

PR_SET_NO_NEW_PRIVS = 38
PR_SET_DUMPABLE = 11


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


def _set_undumpable() -> bool:
    """PR_SET_DUMPABLE=0：断开同 uid 进程对本进程 /proc/<pid> 的交叉读取。

    效果（对 uid 相同的其它进程生效，含其它 chat 的沙盒子进程）：
      - /proc/<pid>/environ 属主变为 root:root（mode 0400）→ 同 uid 沙箱
        无法再偷读本进程完整环境（历史版本里这是最大的残留风险：
        bot 主进程的 TELEGRAM_BOT_TOKEN / R2 密钥就在 os.environ 里）；
      - /proc/<pid>/maps、mem 等需要 PTRACE_MODE_READ 的文件同样被封死；
      - ptrace 本进程被拒绝；core dump 关闭。

    注意：watchdog 依赖的 /proc/<pid>/stat 是世界可读（0444），不受影响；
    killpg 的信号权限取决于进程真实凭据而非 proc 文件属主，同样不受影响。
    本层是纵深防御而非主边界（主边界是 Landlock + 环境变量白名单），
    因此 prctl 失败时选择 fail-open（记 ERROR 后继续），避免个别内核
    异常导致全部 bash 拒绝服务。
    """
    if _libc is None:
        return False
    rc = _libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    if rc != 0:
        err = ctypes.get_errno()
        logger.error("prctl(PR_SET_DUMPABLE, 0) failed: %s", os.strerror(err))
        return False
    return True


def harden_parent_process() -> None:
    """对 bot 主进程本身套用 dumpable=0（启动时调用一次）。

    沙箱子进程与 bot 主进程同 uid（镜像内单用户），若不关闭主进程的
    dumpable，沙箱可以 `cat /proc/1/environ` 直接拿到主进程的完整环境
    （包含所有平台密钥）。对子进程的同样保护在 _preexec_sandbox 内做，
    用于隔离子进程互相之间的窥探。
    """
    if os.getuid() == 0:
        # root 进程的 /proc 文件本就 root 属主，无需处理。
        return
    if _set_undumpable():
        logger.info("Parent process hardened: PR_SET_DUMPABLE=0")


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
                # O_PATH works for both directories and individual device/file nodes.
                # O_DIRECTORY would make it impossible to grant a narrowly-scoped
                # rule to /dev/null.
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
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

            # /dev is intentionally read/execute-only, but normal shell usage
            # commonly redirects output to /dev/null (for example `cmd >/dev/null`).
            # Grant write access only to that single device node instead of making
            # the whole /dev tree writable. This also keeps bash from failing when
            # it attempts to use HISTFILE=/dev/null.
            if os.path.exists("/dev/null"):
                add_path_rule(
                    "/dev/null",
                    LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE,
                )

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
def _preexec_sandbox(workspace_path: str) -> None:
    """Install all mandatory child restrictions before exec("bash")."""
    import resource

    if _libc is None:
        raise SandboxSetupError("libc is unavailable; cannot install sandbox")

    # no_new_privs is part of the sandbox contract, not a best-effort warning.
    if _set_no_new_privs() is False:
        raise SandboxSetupError("PR_SET_NO_NEW_PRIVS failed")

    # dumpable=0 是纵深防御（fail-open）：阻断同 uid 沙箱互相读 environ/maps。
    # 失败只记日志不阻断 —— 主隔离边界是下面的 Landlock。
    _set_undumpable()

    if not _apply_landlock(workspace_path):
        raise SandboxSetupError("Landlock filesystem sandbox could not be installed")

    resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_MAX_CPU_SEC, SANDBOX_MAX_CPU_SEC))
    resource.setrlimit(resource.RLIMIT_FSIZE, (SANDBOX_MAX_FILE_SIZE, SANDBOX_MAX_FILE_SIZE))
    resource.setrlimit(resource.RLIMIT_NOFILE, (SANDBOX_MAX_OPEN_FILES, SANDBOX_MAX_OPEN_FILES))


# =====================================================================
# sitecustomize 注入（沙箱内 Python 默认 socket 超时）
# =====================================================================
# 模型生成的 Python 脚本几乎从不主动设网络超时；一旦网络不可达（防火墙
# 静默丢包、SMTP 端口被墙），smtplib/urllib/requests 会按内核默认 TCP
# 重试挂起约 2 分钟/次，bash 层的空闲/总超时只能事后杀。sitecustomize.py
# 在每个 Python 进程启动时自动执行 socket.setdefaulttimeout()，从源头
# 把静默挂起变成 15s 快速失败 + 清晰报错。
# 注意：setdefaulttimeout 是「单次 socket 操作」的不活跃超时而非总时长
# ——正常的大文件下载（持续有数据流入）不受影响。
_SITECUSTOMIZE_MARKER = "sandbox-sc-v1"
_SITECUSTOMIZE_SOURCE = f'''# apitelegramchat sandbox sitecustomize (marker: {_SITECUSTOMIZE_MARKER})
# Auto-injected by sandbox.build_sandbox_env; do not edit (rewritten on
# every bash session start). Gives every Python process inside the
# sandbox a sane default socket timeout so scripts that forget to set
# one (smtplib, urllib, requests, socket.create_connection) fail fast
# on unreachable networks instead of hanging on kernel-level TCP
# retries (~2 minutes per connect attempt).
import os as _os
import socket as _socket

_t = _os.getenv("SANDBOX_SOCKET_TIMEOUT_SEC", "")
if _t:
    try:
        _v = float(_t)
        if _v > 0:
            _socket.setdefaulttimeout(_v)
    except Exception:
        pass
'''


def _ensure_sitecustomize(runtime_bin: Path) -> None:
    """把 sitecustomize.py 原子写入 runtime bin（经 PYTHONPATH 生效）。

    幂等：内容一致时跳过写入。失败只记 debug——这是尽力而为的增强，
    绝不能阻断 bash 会话启动。与 net_shims 的 shim 安装同一套模式。
    """
    try:
        target = runtime_bin / "sitecustomize.py"
        try:
            if target.read_text(encoding="utf-8") == _SITECUSTOMIZE_SOURCE:
                return
        except OSError:
            pass
        tmp = runtime_bin / f".sitecustomize.{os.getpid()}.tmp"
        tmp.write_text(_SITECUSTOMIZE_SOURCE, encoding="utf-8")
        os.replace(tmp, target)
        logger.debug("sitecustomize injected at %s (socket default timeout)", target)
    except OSError as exc:
        logger.debug("sitecustomize injection skipped: %s", exc)


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

    # 旧镜像没有 curl/wget（Landlock 不拦网络，纯粹是没装）：安装纯 stdlib
    # 兜底 shim 到 runtime bin（PATH 首位）。镜像里出现真二进制后自动让位。
    # 这避免了模型执行 `curl ...` 得到 command not found、浪费一次工具调用。
    ensure_network_shims(runtime_bin)
    # 沙箱内 Python 默认 socket 超时（sitecustomize 经 PYTHONPATH 注入，
    # 见上方模块级注释）。让 smtplib/urllib/requests 等忘记设超时的脚本
    # 在网络不可达时 15s 快速失败，而不是挂起拖到 bash 层超时。
    _ensure_sitecustomize(runtime_bin)

    # Keep runtime_bin first only for local wrappers. The actual compiler remains the
    # system toolchain baked into the image; no apt/pip install happens per Bash run.
    # ★ 工作区自我描述：让模型不用"猜"自己在哪、哪里可写。生产日志显示
    #   模型习惯性 `cd /tmp` 下载文件，而 Landlock 只放行 workspace 子树，
    #   curl -o 直接 exit 23，平均浪费 5-7 轮试错才撞到正确路径。现在
    #   `echo $WORKSPACE` 一次即可拿到绝对路径；系统提示词与 bash 工具
    #   description 同步引用该变量。
    #
    # 身份说明（历史遗留问题的修复）：这里不再设置 USER=chat{chat_id}。
    #   - 旧的 chat{id} 值从未被任何代码读取，只是展示标签，却把会话路由
    #     id 泄露进模型可读的 shell 环境；
    #   - $USER 与真实 uid 解析（whoami/id/ls 属主列）不一致还会误导模型
    #     以为自己"是"某个 chat；实际身份统一为镜像内的 claude 用户，
    #     per-chat 的隔离由 Landlock 按 workspace 路径强制，不靠身份标签。
    return {
        "PATH": f"{runtime_bin}:{cache_root / 'python_user' / 'bin'}:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "HOME": str(cache_root),
        "USER": SANDBOX_USER,
        "LOGNAME": SANDBOX_USER,
        "WORKSPACE": workdir_abs,
        "WORKDIR": workdir_abs,
        # ★ PYTHONPATH 指向 runtime bin：其中的 sitecustomize.py 在沙箱内
        #   每个 Python 进程启动时自动执行（注入默认 socket 超时）。该目录
        #   只含 shim 可执行脚本与 sitecustomize.py，无可导入模块名冲突，
        #   不会遮蔽标准库或 site-packages。
        "PYTHONPATH": str(runtime_bin),
        # sitecustomize.py 读取该值设置 socket.setdefaulttimeout。
        "SANDBOX_SOCKET_TIMEOUT_SEC": str(SANDBOX_SOCKET_TIMEOUT_SEC),
        # ---------- 常见 CLI 工具的网络超时 ----------
        # 网络不可达时让命令快速失败，而不是按各自默认值挂起数分钟：
        #   - pip：连接超时 15s（官方环境变量，等价 --timeout）；
        #   - git：传输速率低于 1KB/s 持续 30s 即中止（覆盖 clone/fetch 静默卡死）；
        #   - npm：fetch 阶段 60s 超时 + 最多重试 2 次（npm 默认 300s）。
        "PIP_DEFAULT_TIMEOUT": "15",
        "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
        "GIT_HTTP_LOW_SPEED_TIME": "30",
        "npm_config_fetch_timeout": "60000",
        "npm_config_fetch_retries": "2",
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
    children_map: dict[int, list[int]] = {}
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
    """周期性检查子进程树，超过 max_procs 立即 kill。

    注意：proc 必须以 `start_new_session=True` 启动（见 verify_security.py），
    这样 proc.pid 才是它自己的 PGID，killpg 才会杀掉"这个子进程及其子孙"
    而不是误杀父进程组。如果 caller 没用 start_new_session=True，
    `os.getpgid(proc.pid)` 会返回父进程的 PGID，killpg 会误杀整个父进程组
    （包括 bot 自身）。为此本函数对 fallback 路径仅做 proc.kill()，
    不用 killpg，避免灾难性误杀。
    """
    if proc.returncode is not None:
        return
    while proc.returncode is None:
        try:
            n = _count_descendants(proc.pid)
            if n > max_procs:
                logger.warning(
                    f"🚨 Watchdog: sandbox pid={proc.pid} spawned {n} > {max_procs} procs, killing"
                )
                # 仅当 proc.pid 自身就是 PGID（即 start_new_session=True）时
                # 才 killpg；否则只杀 proc.pid 本身，不波及父进程组。
                try:
                    proc_pgid = os.getpgid(proc.pid)
                except ProcessLookupError:
                    proc_pgid = None
                if proc_pgid is not None and proc_pgid == proc.pid:
                    try:
                        os.killpg(proc_pgid, signal.SIGKILL)
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


# 在父进程（import 时）预热 Landlock ABI 探测缓存。
# preexec_fn 运行在 fork 之后、exec 之前的子进程里；若首次探测发生在
# 子进程内，其触发的 logger.info 会拿 logging 模块随 fork 继承的锁，
# 在多线程父进程（asyncio 线程池 / aiohttp）下有死锁风险。这里提前
# 填充 _landlock_abi 缓存，子进程内走纯缓存路径，不再触碰 logging。
_landlock_abi_version()
