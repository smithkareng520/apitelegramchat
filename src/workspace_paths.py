from __future__ import annotations

import logging
import os
import re
import shutil
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

# v2.2（及更早）遗留布局留在 workspace 根下的缓存目录名 → 新布局目标名。
# download/upload/skills/.skills_initialized 在新布局里本来就归属根，无需动；
# 只有 runtime/ 更名为隐藏层 .runtime/，runtime.json 随迁进 .runtime/。
# 迁移用原子 rename（同一文件系统），只移动不合并，幂等可重入。
_LEGACY_RUNTIME_DIR = "runtime"
_LEGACY_RUNTIME_STATE = "runtime.json"

# 过渡布局（v2.3.0 草案，未曾正式发布）：家目录位于根下 claude/ 子目录。
# 防御性兼容：若某环境短暂部署过该草案，首次访问时把 claude/ 下的条目
# 逐个折叠回根，再删除空的 claude/ 目录。目标名相对 workspace 根。
_INTERIM_HOME_DIR_NAME = "claude"
_INTERIM_HOME_ENTRIES: tuple[tuple[str, str], ...] = (
    ("download", _DOWNLOAD_DIR_NAME),
    ("upload", _UPLOAD_DIR_NAME),
    ("skills", _SKILLS_DIR_NAME),
    (".runtime", _RUNTIME_DIR_NAME),
    (".skills_initialized", ".skills_initialized"),
    ("runtime", _LEGACY_RUNTIME_DIR),
    ("runtime.json", _LEGACY_RUNTIME_STATE),
)

# 已完成过迁移检查的 (workspace 根, namespace)（进程内缓存；重启后靠 exists()
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

    v2.3.1 布局：workspace 根即 agent 家目录（$HOME、bash 起始 cwd 与
    Landlock 唯一放行边界三者重合）。根下只存放模型可见的用户文件层
    （download/ upload/ skills/）与隐藏缓存层 ``.runtime/``；家目录之外
    的一切路径（/home 下其他家目录、data_root、系统目录）对沙箱完全
    不可见。
    """
    ns = _resolved_namespace(chat_id, namespace)
    parent = workspaces_root()
    return _secure_directory(parent / ns)


def _move_if_absent(src: Path, dst: Path) -> None:
    """仅当源存在且目标不存在时原子改名；否则无操作（只移动不合并）。"""
    if not src.exists() or dst.exists():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        logger.info("Workspace migration: moved %s -> %s", src, dst)
    except OSError as exc:
        logger.warning("Workspace migration skipped for %s: %s", src, exc)


def _fold_legacy_workspaces_location(root: Path, ns: str) -> None:
    """一次性把旧位置 ``<data_root>/workspaces/<ns>`` 折叠进新家目录。

    v2.3.1 及之前家目录位于 ``<data_root>/workspaces/<ns>``；工作空间根
    改为 ``APITELEGRAMCHAT_WORKSPACES_DIR``（默认 /home）后，旧目录常与
    新家目录不在同一文件系统（如 Render 挂载盘 vs 容器层），无法整体
    rename。规则与既有布局迁移完全一致：只移动不合并、绝不覆盖 ——
    仅当新家目录内不存在同名条目时逐条 ``shutil.move``（自动兼容跨
    文件系统），随后尝试删除已空的旧目录（仍有残留则原地保留）；
    任何失败只记日志，绝不阻断路径解析（迁移失败只影响旧数据可见性，
    不影响新工作区可用性）。
    """
    legacy = data_root() / "workspaces" / ns
    try:
        if not legacy.is_dir() or legacy.is_symlink():
            return
        try:
            if legacy.resolve() == root.resolve():
                return  # env 指回旧位置的部署：新旧同径，无需迁移
        except OSError:
            return
        moved = 0
        for child in sorted(legacy.iterdir()):
            dst = root / child.name
            if dst.exists() or dst.is_symlink():
                continue  # 绝不覆盖新家已有内容
            shutil.move(str(child), str(dst))
            moved += 1
        if moved:
            logger.info(
                "Workspace location migration: %s -> %s (%d items)",
                legacy, root, moved,
            )
        try:
            legacy.rmdir()  # 仅当已空时成功；有残留则原地保留
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 — 迁移绝不阻断路径解析
        logger.warning("Workspace location migration skipped namespace=%s: %s", ns, exc)


def _migrate_legacy_layout(root: Path, ns: str) -> None:
    """One-time migration of legacy workspace layouts to the v2.3.1 layout.

    新布局：workspace 根即家目录，根下平铺 download/ upload/ skills/ 与
    隐藏缓存层 ``.runtime/``（runtime.json 归入其中）。两种遗留布局：

    - v2.3.0 过渡草案（根下 ``claude/`` 家目录，未正式发布）：把 claude/
      下的条目逐个折叠回根，成功后删除空的 claude/ 目录；
    - v2.2 及更早（runtime/ 与 runtime.json 平铺在根下）：仅把 runtime/
      更名为 ``.runtime/``，runtime.json 随迁进 .runtime/；
      download/upload/skills 本就归属根，原地不动。

    迁移规则：

    - 仅当目标不存在且源存在时原子移动（同一文件系统用 ``os.replace``
      改名，跨文件系统逐条 ``shutil.move``），不做合并、不覆盖任何已
      存在的文件 —— 中断后重跑安全，幂等可重入；
    - 目标已存在时保留双方不动（绝不覆盖新数据）；runtime.json 是可再生
      缓存，目标已存在时旧文件直接丢弃；
    - 全部失败仅记日志，绝不阻断路径解析（迁移失败只影响旧数据可见性，
      不影响新工作区可用性）。
    """
    try:
        # 0) 旧位置折叠：家目录原本在 data_root/workspaces/<ns>，整目录
        #    并入新家（升级首次访问时把用户旧文件带过来）。
        _fold_legacy_workspaces_location(root, ns)

        # 1) 过渡草案折叠：claude/ 下的条目回到根。
        interim = root / _INTERIM_HOME_DIR_NAME
        if interim.is_dir() and not interim.is_symlink():
            for src_name, dst_name in _INTERIM_HOME_ENTRIES:
                _move_if_absent(interim / src_name, root / dst_name)
            try:
                # 仅当目录已空时成功；仍有残留（未知文件）则原地保留。
                interim.rmdir()
                logger.info("Workspace migration: removed empty %s", interim)
            except OSError:
                pass

        # 2) v2.2 遗留：runtime/ → .runtime/（隐藏缓存层）。
        _move_if_absent(root / _LEGACY_RUNTIME_DIR, root / _RUNTIME_DIR_NAME)

        # 3) runtime.json（bash 工具链清单缓存）→ .runtime/runtime.json。
        legacy_state = root / _LEGACY_RUNTIME_STATE
        new_state = root / _RUNTIME_DIR_NAME / _LEGACY_RUNTIME_STATE
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

    v2.3.1 布局（家目录即 workspace 根，默认位于 /home 下）::

        <workspaces_root>/<ns>/   ← 家目录：$HOME = 起始 cwd = Landlock 边界
        ├── download/  upload/  skills/
        └── .runtime/                  ← 隐藏缓存层（bin/pip/ccache/HF/... + runtime.json）

    workspaces_root 默认 /home（可用 APITELEGRAMCHAT_WORKSPACES_DIR 覆盖），
    家目录之外的一切路径（其他家目录、data_root、系统目录）仍被
    Landlock 拒绝，沙箱世界收敛到家目录子树。首次访问时一次性迁移旧
    位置（data_root/workspaces/<ns>，见 :func:`_fold_legacy_workspaces_location`）
    与两种遗留布局（见 :func:`_migrate_legacy_layout`），之后每次调用
    只剩几个 ``exists()`` 快路径检查，开销可忽略。
    """
    ns = _resolved_namespace(chat_id, namespace)
    root = workspace_root(chat_id, ns)
    migrate_key = (str(root), ns)
    if migrate_key not in _home_migrated:
        _migrate_legacy_layout(root, ns)
        _home_migrated.add(migrate_key)
    return root


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
