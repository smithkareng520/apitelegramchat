# workspace_utils.py
import asyncio
import os
import logging
from pathlib import Path
from apitelegramchat.workspace_paths import (
    workspace_root, workspace_namespace,
    workspace_upload_root, workspace_download_root,
)

from apitelegramchat.s3_utils import (
    upload_bytes_to_r2,
    download_from_r2,
    delete_r2_object,
)

logger = logging.getLogger(__name__)

# R2 持久化只发生在明确选择的用户文件上；运行时树永不进行全量同步。

# workspace 访问锁：保护同一聊天的本地文件操作，避免并发修改。
_workspace_locks = {}
_workspace_locks_lock = asyncio.Lock()

_workspace_initialized: set[str] = set()
_workspace_init_locks = {}
_workspace_init_locks_lock = asyncio.Lock()


async def _get_workspace_lock(chat_id: int, namespace: str | None = None) -> asyncio.Lock:
    """获取或创建该用户/作用域的 workspace 锁。"""
    key = workspace_namespace(chat_id, namespace)
    async with _workspace_locks_lock:
        if key not in _workspace_locks:
            _workspace_locks[key] = asyncio.Lock()
        return _workspace_locks[key]


async def _get_workspace_init_lock(key: str) -> asyncio.Lock:
    """获取 workspace 初始化专用锁；与文件操作锁分离，避免嵌套死锁。"""
    async with _workspace_init_locks_lock:
        if key not in _workspace_init_locks:
            _workspace_init_locks[key] = asyncio.Lock()
        return _workspace_init_locks[key]


async def _ensure_runtime_workspace(chat_id: int, namespace: str | None = None) -> None:
    """Ensure the runtime workspace tree (root + upload/ + download/) exists.

    This function is intentionally safe to call before every tool invocation.
    It MUST NOT synchronize packaged skills: ``workspace/skills`` is runtime
    state and may contain files created or edited by the agent/user.

    upload/ and download/ are pre-created here (rather than lazily on first
    use) because bash starts with cwd=workspace root and the model almost
    immediately tries `cp out.txt upload/out.txt` or `cat download/x.pdf`.
    Without pre-creating these subtrees, the very first such command fails
    with "No such file or directory" — forcing the model to spend an extra
    `mkdir -p upload/` round before doing the real work. This is the
    initialization boundary, not a per-tool concern.
    """
    workspace = workspace_root(chat_id, namespace)
    workspace.mkdir(parents=True, exist_ok=True)
    # 显式预创建 upload/ 与 download/：workspace_upload_root /
    # workspace_download_root 是幂等的（mkdir exist_ok + chmod 0o700），
    # 重复调用不会出错；首次调用就把这两棵子树准备好。
    workspace_upload_root(chat_id, namespace)
    workspace_download_root(chat_id, namespace)


async def _ensure_workspace_initialized(chat_id: int, namespace: str | None = None) -> None:
    """Run one-time workspace skill initialization.

    Initialization is protected by a per-workspace lock and a persistent marker
    so repeated calls never re-run the packaged-skill sync.
    """
    resolved_namespace = workspace_namespace(chat_id, namespace)
    key = resolved_namespace
    lock = await _get_workspace_init_lock(key)
    async with lock:
        workspace = workspace_root(chat_id, resolved_namespace)
        workspace.mkdir(parents=True, exist_ok=True)
        marker = workspace / ".skills_initialized"

        if key in _workspace_initialized or marker.is_file():
            _workspace_initialized.add(key)
            return

        try:
            from apitelegramchat.skills import sync_all_skill_assets_to_workspace

            summary = await asyncio.to_thread(
                sync_all_skill_assets_to_workspace,
                workspace,
            )
            if summary.get("errors"):
                logger.warning(
                    "部分 skill 包初始化失败 namespace=%s: %s",
                    resolved_namespace,
                    "; ".join(summary["errors"]),
                )
                return

            marker.write_text("initialized\n", encoding="utf-8")
            _workspace_initialized.add(key)
        except Exception as exc:
            logger.warning(
                "初始化 skill 包到 workspace 失败 namespace=%s: %s",
                resolved_namespace,
                exc,
            )




# ========== 单文件定向同步（state/ 域） ==========
# 用于 todo / memory 等小 JSON 文件。这些文件的本地路径在 state/{ns}/ 目录，
# R2 key 也在 state/{ns}/ prefix 下，和本地 workspace 隔离。
# 不做文件名黑名单 —— 用户在 workspace 里放同名文件也互不影响。

async def _sync_named_file_from_r2(chat_id: int, local_path: Path, remote_name: str) -> None:
    """
    从 R2 的 state/{ns}/{remote_name} 下载到 local_path。
    如果 R2 上没有该文件，本地保留现状（可能是首次创建）。
    """
    safe_name = os.path.normpath(remote_name)
    if safe_name == "." or safe_name.startswith("..") or os.path.isabs(safe_name):
        logger.warning(f"拒绝路径遍历的 remote_name: {remote_name!r}")
        return

    local_path.parent.mkdir(parents=True, exist_ok=True)
    key = f"state/{workspace_namespace(chat_id)}/{safe_name}"
    data = await download_from_r2(key)
    if data is not None:
        with open(local_path, "wb") as f:
            f.write(data)


async def _sync_named_file_to_r2(chat_id: int, local_path: Path, remote_name: str) -> None:
    """
    将 local_path 上传到 R2 的 state/{ns}/{remote_name}。
    如果本地文件不存在，则删除 R2 上的对应文件。
    """
    safe_name = os.path.normpath(remote_name)
    if safe_name == "." or safe_name.startswith("..") or os.path.isabs(safe_name):
        logger.warning(f"拒绝路径遍历的 remote_name: {remote_name!r}")
        return

    key = f"state/{workspace_namespace(chat_id)}/{safe_name}"
    if local_path.is_file():
        with open(local_path, "rb") as f:
            data = f.read()
        await upload_bytes_to_r2(data, key, "application/json")
    else:
        await delete_r2_object(key)


# ========== upload/ & download/ 说明 ==========
# upload/ 与 download/ 是 workspace 根目录的两棵子树：
#   - download/：用户上传文档的本地落地缓冲（Telegram → R2 缓存 → 本地）；
#   - upload/：待发送产物的暂存区（present_files 只从这里读取）。
# bash 本就能直接读写这两棵子树（相对路径即可），无需跨边界原语：
# 模型直接使用 bash：`cat download/x.pdf`、`cp out.txt upload/out.txt`。
#
# 注意：download/ 不做 R2 同步，持久化由 file_handlers.py 的
# `telegram/{file_id}` R2 缓存负责；upload/ 也不做 R2 镜像同步——
# 恢复方向从未被调用且会造成重复存储。


# ========== 可选：初始化工作区（后台执行） ==========

async def init_workspace(chat_id: int, namespace: str | None = None):
    """Initialize the workspace and packaged skills once for this workspace."""
    try:
        await _ensure_workspace_initialized(chat_id, namespace)
    except Exception as e:
        logger.error(f"Workspace 初始化失败: {e}")
