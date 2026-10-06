from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_NAMESPACE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_STATE_DIR_NAME = os.getenv("APITELEGRAMCHAT_STATE_DIR_NAME", "state").strip() or "state"
# 运行时缓存层（pip/ccache/HF/tmp/bin/...）是家目录内的隐藏目录：
#   - 点前缀让普通 `ls` 看不见，模型视角的家目录只剩用户文件；
#   - 位于家目录内部（家目录即 Landlock 放行边界），缓存天然可写，
#     无需为放行缓存而扩大边界。
_RUNTIME_DIR_NAME = os.getenv("APITELEGRAMCHAT_RUNTIME_DIR_NAME", ".runtime").strip() or ".runtime"
_SKILLS_DIR_NAME = os.getenv("APITELEGRAMCHAT_SKILLS_DIR_NAME", "skills").strip() or "skills"
_UPLOAD_DIR_NAME = os.getenv("APITELEGRAMCHAT_UPLOAD_DIR_NAME", "upload").strip() or "upload"
_DOWNLOAD_DIR_NAME = os.getenv("APITELEGRAMCHAT_DOWNLOAD_DIR_NAME", "download").strip() or "download"

def _resolved_namespace(chat_id: object, namespace: object | None = None) -> str:
    if namespace is not None:
        return sanitize_namespace(namespace)
    try:
        from state import get_current_user_namespace

        current = get_current_user_namespace()
        if current:
            return sanitize_namespace(current)
    except Exception:
        # 用户 namespace 解析失败时静默回退 per-chat namespace 会改变
        # 工作区归属，留 debug 线索（回退本身是安全的既有行为）。
        logger.debug("get_current_user_namespace 失败，回退 chat namespace", exc_info=True)
    return sanitize_namespace(chat_id)


def _secure_directory(path: Path) -> Path:
    """Create a private runtime directory without accepting a final symlink."""
    expanded = path.expanduser()
    if expanded.exists() and expanded.is_symlink():
        raise RuntimeError(f"Refusing symlinked runtime directory: {expanded}")
    expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = expanded.resolve()
    if resolved.is_symlink() or not resolved.is_dir():
        raise RuntimeError(f"Invalid runtime directory: {expanded}")
    try:
        os.chmod(resolved, 0o700)
    except OSError as exc:
        raise RuntimeError(f"Unable to protect runtime directory {resolved}: {exc}") from exc
    return resolved


@lru_cache(maxsize=1)
def data_root() -> Path:
    """Return the private root for internal runtime state.

    承担 state/（todos/memories 等会话状态）、白名单缓存、R2 本地缓存等
    内部状态；agent 家目录/工作空间不在这里（见 :func:`workspaces_root`），
    对沙箱完全不可见。
    """
    base = os.getenv("APITELEGRAMCHAT_DATA_DIR", "/tmp/apitelegramchat_data")
    return _secure_directory(Path(base))


@lru_cache(maxsize=1)
def workspaces_root() -> Path:
    """Return the parent directory that holds every per-chat agent home.

    默认 ``/home``：家目录即 ``/home/<ns>``，bash 里 ``pwd`` 直接是
    ``/home/<ns>``，符合 Linux 习惯，不再携带 data_root 前缀。data_root
    仍承担内部状态（state/ 等），两者彻底分离。可用
    ``APITELEGRAMCHAT_WORKSPACES_DIR`` 覆盖（例如受限环境写不了 /home）。

    父目录只创建、不改权限、不做 0700 收紧（/home 是标准系统目录，本
    函数不应改变系统目录的语义）；隐私边界在每户家目录的 0700 +
    Landlock（家目录才是放行边界）。若部署镜像里运行用户写不了该目录，
    这里报出带修复提示的错误，而不是在更深路径上莫名失败。
    """
    base = os.getenv("APITELEGRAMCHAT_WORKSPACES_DIR", "/home").strip() or "/home"
    path = Path(base)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create workspaces root {path}: {exc} — "
            "the runtime user must be able to write it; set "
            "APITELEGRAMCHAT_WORKSPACES_DIR to a writable directory to override"
        ) from exc
    return path


def sanitize_namespace(value: object) -> str:
    raw = "default" if value is None else str(value).strip()
    raw = raw or "default"
    safe = _NAMESPACE_RE.sub("_", raw)
    return safe.strip("._") or "default"


def workspace_root(chat_id: object, namespace: object | None = None) -> Path:
    """Return the workspace root for this chat/scope — the agent home itself.

    布局：workspace 根即 agent 家目录（$HOME、bash 起始 cwd 与
    Landlock 唯一放行边界三者重合）。根下只存放模型可见的用户文件层
    （download/ upload/ skills/）与隐藏缓存层 ``.runtime/``；家目录之外
    的一切路径（/home 下其他家目录、data_root、系统目录）对沙箱完全
    不可见。
    """
    ns = _resolved_namespace(chat_id, namespace)
    parent = workspaces_root()
    return _secure_directory(parent / ns)


def agent_home(chat_id: object, namespace: object | None = None) -> Path:
    """Return the agent home directory: $HOME, bash cwd and Landlock scope.

    布局（家目录即 workspace 根，默认位于 /home 下）::

        <workspaces_root>/<ns>/   ← 家目录：$HOME = 起始 cwd = Landlock 边界
        ├── download/  upload/  skills/
        └── .runtime/                  ← 隐藏缓存层（bin/pip/ccache/HF/... + runtime.json）

    workspaces_root 默认 /home（可用 APITELEGRAMCHAT_WORKSPACES_DIR 覆盖），
    家目录之外的一切路径（其他家目录、data_root、系统目录）仍被
    Landlock 拒绝，沙箱世界收敛到家目录子树。
    """
    ns = _resolved_namespace(chat_id, namespace)
    return workspace_root(chat_id, ns)


def workspace_workdir(chat_id: object, namespace: object | None = None) -> Path:
    """Return the agent home directory used as the bash cwd (= workspace root).

    Every relative path in Bash, text_editor, staging, and file presentation is
    resolved against this directory (the agent home), which is also ``$HOME``
    and the only Landlock-permitted subtree — exactly :func:`workspace_root`.
    Everything outside the home (other homes under the workspaces root,
    data_root, the rest of the filesystem) is never exposed to the sandbox.
    The workspace is local-only and is never mirrored wholesale to R2.
    Packaged skills live under ``skills/``; runtime caches live under the
    hidden ``.runtime/`` inside the home.
    """
    home = agent_home(chat_id, namespace)
    workspace_skills_root(chat_id, namespace)
    return home.resolve()


def state_root() -> Path:
    return _secure_directory(data_root() / _STATE_DIR_NAME)


def chat_state_root(chat_id: object, namespace: object | None = None) -> Path:
    ns = _resolved_namespace(chat_id, namespace)
    return _secure_directory(state_root() / ns)


def state_file(chat_id: object, filename: str, namespace: object | None = None) -> Path:
    return chat_state_root(chat_id, namespace) / filename


def memory_state_file(chat_id: object, namespace: object | None = None) -> Path:
    return state_file(chat_id, "memories.json", namespace)


def todo_state_file(chat_id: object, namespace: object | None = None) -> Path:
    return state_file(chat_id, "todos.json", namespace)


def workspace_namespace(chat_id: object, namespace: object | None = None) -> str:
    """Return the canonical workspace namespace for this tool invocation.

    Callers that coordinate multiple tools should resolve this once and pass the
    returned value explicitly to every workspace operation. This avoids relying on
    the request ContextVar repeatedly across async tasks/subtasks.
    """
    return _resolved_namespace(chat_id, namespace)


def runtime_cache_root(chat_id: object, namespace: object | None = None) -> Path:
    """隐藏缓存层（家目录内 ``.runtime/``），完全独立于用户文件同步层。

    家目录即 Landlock 放行边界，沙箱内的 pip/TMPDIR/ccache/HF 等全部写
    这里；点前缀让普通 ``ls`` 不显示，不与用户文件混在一起。
    """
    return _secure_directory(agent_home(chat_id, namespace) / _RUNTIME_DIR_NAME)

def workspace_skills_root(chat_id: object, namespace: object | None = None) -> Path:
    """本地 skill 资源层（家目录下 ``skills/``）。

    打包技能由一次性 bootstrap 拷入；运行期用户自建/修改的技能由
    workspace_utils 按 ``skills/{ns}/`` 前缀定向同步到 R2（首次初始化
    恢复 + 每次消息 intake 增量备份），服务重启后自动找回。workspace
    其余部分仍不做全量同步。
    """
    return _secure_directory(agent_home(chat_id, namespace) / _SKILLS_DIR_NAME)


def workspace_upload_root(chat_id: object, namespace: object | None = None) -> Path:
    """Staging area for files the model wants to send to the user.

    present_files only accepts paths under this directory; stage outputs
    here first via bash (e.g. `cp out.txt upload/out.txt`), then present
    them with `present_files(["upload/out.txt"])`.

    upload/ is a subdirectory of the agent home, so bash and text_editor
    can read and write files here through relative paths like any other
    workspace directory.
    """
    return _secure_directory(agent_home(chat_id, namespace) / _UPLOAD_DIR_NAME)


def workspace_download_root(chat_id: object, namespace: object | None = None) -> Path:
    """Landing area for files the user uploaded via Telegram.

    When a user sends a document and the active model does not support
    native document input, the file is saved here (not into files/).
    download/ is a subdirectory of the agent home, so the model can
    read and edit files directly (bash `cat download/<name>`, text_editor
    `view download/<name>`, `ls download/`).
    """
    return _secure_directory(agent_home(chat_id, namespace) / _DOWNLOAD_DIR_NAME)
