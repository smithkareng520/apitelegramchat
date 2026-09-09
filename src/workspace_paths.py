from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_NAMESPACE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_STATE_DIR_NAME = os.getenv("APITELEGRAMCHAT_STATE_DIR_NAME", "state").strip() or "state"
# agent 家目录名：workspace 容器根下唯一对模型可见的一层，$HOME、bash
# 起始 cwd 与 Landlock 放行边界都指向它。默认 claude（与镜像内沙箱
# 用户名一致），可用 APITELEGRAMCHAT_HOME_DIR_NAME 覆盖。
_HOME_DIR_NAME = os.getenv("APITELEGRAMCHAT_HOME_DIR_NAME", "claude").strip() or "claude"
# 运行时缓存层（pip/ccache/HF/tmp/bin/...）是家目录内的隐藏目录：
#   - 点前缀让普通 `ls` 看不见，模型视角的家目录只剩用户文件；
#   - 必须位于家目录内部（而非容器根下的兄弟目录），否则 Landlock
#     为了放行缓存就得放行容器根，"沙箱边界 = 家目录"的收紧就落空。
_RUNTIME_DIR_NAME = os.getenv("APITELEGRAMCHAT_RUNTIME_DIR_NAME", ".runtime").strip() or ".runtime"
_SKILLS_DIR_NAME = os.getenv("APITELEGRAMCHAT_SKILLS_DIR_NAME", "skills").strip() or "skills"
_UPLOAD_DIR_NAME = os.getenv("APITELEGRAMCHAT_UPLOAD_DIR_NAME", "upload").strip() or "upload"
_DOWNLOAD_DIR_NAME = os.getenv("APITELEGRAMCHAT_DOWNLOAD_DIR_NAME", "download").strip() or "download"

# 旧布局（v2.2 及之前）留在容器根下的条目名 → 新布局中家目录下的目标名。
# runtime.json 是 bash 会话的工具链清单缓存，随缓存层一起进 .runtime/。
# 迁移用原子 rename（同一文件系统），只移动不合并，幂等可重入。
_LEGACY_HOME_ENTRIES: tuple[tuple[str, str], ...] = (
    ("download", _DOWNLOAD_DIR_NAME),
    ("upload", _UPLOAD_DIR_NAME),
    ("skills", _SKILLS_DIR_NAME),
    ("runtime", _RUNTIME_DIR_NAME),
    (".skills_initialized", ".skills_initialized"),
)
_LEGACY_RUNTIME_STATE = "runtime.json"

# 已完成过迁移检查的 (容器根, namespace)（进程内缓存；重启后靠 exists()
# 快路径兜底——旧布局条目在新代码下不再产生，首次迁移后即恒为空操作）。
_home_migrated: set[tuple[str, str]] = set()


def _resolved_namespace(chat_id: object, namespace: object | None = None) -> str:
    if namespace is not None:
        return sanitize_namespace(namespace)
    try:
        from state import get_current_user_namespace

        current = get_current_user_namespace()
        if current:
            return sanitize_namespace(current)
    except Exception:
        pass
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
    """Return the private root for runtime state and workspaces."""
    base = os.getenv("APITELEGRAMCHAT_DATA_DIR", "/tmp/apitelegramchat_data")
    return _secure_directory(Path(base))


def sanitize_namespace(value: object) -> str:
    raw = "default" if value is None else str(value).strip()
    raw = raw or "default"
    safe = _NAMESPACE_RE.sub("_", raw)
    return safe.strip("._") or "default"


def workspace_root(chat_id: object, namespace: object | None = None) -> Path:
    """Return the private container root for this chat/scope.

    自 v2.3 起该目录只是 bot 自有的存储容器，不再对沙箱放行：模型可见的
    世界收敛到 :func:`agent_home`（容器根下的 ``claude/`` 家目录）。
    容器根本身对 bash 沙箱不可读不可写（Landlock 只放行家目录子树）。
    """
    ns = _resolved_namespace(chat_id, namespace)
    parent = _secure_directory(data_root() / "workspaces")
    return _secure_directory(parent / ns)


def _migrate_legacy_layout(root: Path, home: Path, ns: str) -> None:
    """One-time move of v2.2 (and earlier) workspace entries into the agent home.

    旧布局把 download/ upload/ skills/ runtime/ 和 runtime.json 直接放在
    容器根下；新布局里它们归属家目录（缓存层更名为 ``.runtime``）。迁移
    规则：

    - 仅当目标不存在且源存在时用 ``os.replace`` 原子改名（同一文件系统），
      不做合并、不覆盖任何已存在的文件 —— 中断后重跑安全，幂等可重入；
    - 目标已存在时保留双方不动：残留的旧条目位于容器根下，对沙箱完全
      不可见（Landlock 只放行家目录），不会造成泄漏或混淆；
    - 全部失败仅记日志，绝不阻断路径解析（迁移失败只影响旧数据可见性，
      不影响新工作区可用性）。
    """
    try:
        home.mkdir(parents=True, exist_ok=True)
        for legacy_name, new_name in _LEGACY_HOME_ENTRIES:
            src = root / legacy_name
            dst = home / new_name
            if not src.exists() or dst.exists():
                continue
            try:
                os.replace(src, dst)
                logger.info("Workspace migration: moved %s -> %s", src, dst)
            except OSError as exc:
                logger.warning("Workspace migration skipped for %s: %s", src, exc)

        # runtime.json（bash 工具链清单缓存）→ <home>/.runtime/runtime.json。
        legacy_state = root / _LEGACY_RUNTIME_STATE
        new_state = home / _RUNTIME_DIR_NAME / _LEGACY_RUNTIME_STATE
        if legacy_state.is_file():
            try:
                if new_state.exists():
                    # 新位置已存在（更权威），旧缓存直接丢弃。
                    legacy_state.unlink()
                else:
                    new_state.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(legacy_state, new_state)
                logger.info("Workspace migration: runtime state -> %s", new_state)
            except OSError as exc:
                logger.warning("Workspace migration skipped for %s: %s", legacy_state, exc)
    except Exception as exc:  # noqa: BLE001 — 迁移绝不阻断路径解析
        logger.warning("Workspace migration failed namespace=%s: %s", ns, exc)


def agent_home(chat_id: object, namespace: object | None = None) -> Path:
    """Return the agent home directory: $HOME, bash cwd and Landlock scope.

    v2.3 布局::

        <data_root>/workspaces/<ns>/     ← 容器根（bot 自有，对沙箱不可见）
        └── claude/                      ← 家目录：模型唯一可见可写的世界
            ├── download/  upload/  skills/
            └── .runtime/                ← 隐藏缓存层（bin/pip/ccache/HF/...）

    首次访问时把旧布局的容器根条目一次性迁移进家目录（见
    :func:`_migrate_legacy_layout`），之后每次调用只剩几个 ``exists()``
    快路径检查，开销可忽略。
    """
    ns = _resolved_namespace(chat_id, namespace)
    root = workspace_root(chat_id, ns)
    home = _secure_directory(root / _HOME_DIR_NAME)
    migrate_key = (str(root), ns)
    if migrate_key not in _home_migrated:
        _migrate_legacy_layout(root, home, ns)
        _home_migrated.add(migrate_key)
    return home


def workspace_workdir(chat_id: object, namespace: object | None = None) -> Path:
    """Return the agent home directory used as the bash cwd.

    Every relative path in Bash, text_editor, staging, and file presentation is
    resolved against this directory (the agent home, e.g. ``.../claude``),
    which is also ``$HOME`` and the only Landlock-permitted subtree. It sits
    one level below :func:`workspace_root` (the private container root, never
    exposed to the sandbox). The workspace is local-only and is never mirrored
    wholesale to R2. Packaged skills live under ``skills/``; runtime caches
    live under the hidden ``.runtime/`` inside the home.
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

    必须位于家目录内部：Landlock 只放行家目录子树，沙箱内的 pip/TMPDIR/
    ccache/HF 等全部写这里；放在容器根下会迫使 Landlock 放行容器根。
    """
    return _secure_directory(agent_home(chat_id, namespace) / _RUNTIME_DIR_NAME)

def workspace_skills_root(chat_id: object, namespace: object | None = None) -> Path:
    """本地 skill 资源层（家目录下 ``skills/``），不参与用户文件同步。"""
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
