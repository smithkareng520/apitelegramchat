# workspace_utils.py
import asyncio
import os
import logging
from pathlib import Path
from workspace_paths import (
    agent_home, workspace_root, workspace_namespace,
    workspace_upload_root, workspace_download_root,
)

from s3_utils import (
    upload_bytes_to_r2,
    download_from_r2,
    delete_r2_object,
    list_r2_keys,
)

logger = logging.getLogger(__name__)

# R2 持久化只发生在明确选择的用户文件上；运行时缓存树永不进行全量同步。
# skills/ 是例外：它是用户可编辑的资源层，按用户 namespace 独立保存。


class _LockRegistry:
    """按需创建、按 key 复用的 asyncio.Lock 注册表。

    把此前暴露为模块级全局变量的两组 "dict + 注册表锁 + get-or-create"
    （_workspace_locks/_workspace_locks_lock、_workspace_init_locks/
    _workspace_init_locks_lock）收拢进类：可变 dict 不再可被任意导入方
    绕过锁直接改写，get_or_create 在注册表锁内原子完成。

    并发模型说明（为什么不用 contextvars）：这里的锁按 workspace key
    全局共享——同一个 workspace 的所有并发协程必须互斥，属于「跨任务
    共享」状态；contextvars 是「每任务/每请求隔离」，语义正好相反。
    锁实例的使用（async with）发生在注册表锁之外，不存在嵌套持锁，
    因此不会死锁。锁数量与 workspace 数量同阶（每 namespace 一把），
    有界且生命周期与进程相同，无需淘汰。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def get_or_create(self, key: str) -> asyncio.Lock:
        """原子地取 key 对应的锁，不存在则创建。"""
        async with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock


# workspace 访问锁：保护同一聊天的本地文件操作，避免并发修改。
_workspace_file_locks = _LockRegistry()

_workspace_initialized: set[str] = set()
# workspace 初始化专用锁；与文件操作锁分离，避免嵌套死锁。
_workspace_init_lock_registry = _LockRegistry()


async def _get_workspace_lock(chat_id: int, namespace: str | None = None) -> asyncio.Lock:
    """获取或创建该用户/作用域的 workspace 锁。"""
    return await _workspace_file_locks.get_or_create(workspace_namespace(chat_id, namespace))


async def _get_workspace_init_lock(key: str) -> asyncio.Lock:
    """获取 workspace 初始化专用锁；与文件操作锁分离，避免嵌套死锁。"""
    return await _workspace_init_lock_registry.get_or_create(key)


async def _ensure_runtime_workspace(chat_id: int, namespace: str | None = None) -> None:
    """Ensure the runtime workspace tree (agent home + upload/ + download/) exists.

    This function is intentionally safe to call before every tool invocation.
    The ``workspace/skills`` resource layer is initialized separately and is
    persisted per user namespace; other runtime files are not mirrored.

    upload/ and download/ are pre-created here (rather than lazily on first
    use) because bash starts with cwd=agent home and the model almost
    immediately tries `cp out.txt upload/out.txt` or `cat download/x.pdf`.
    Without pre-creating these subtrees, the very first such command fails
    with "No such file or directory" — forcing the model to spend an extra
    `mkdir -p upload/` round before doing the real work. This is the
    initialization boundary, not a per-tool concern.

    v2.3.1：upload/ 与 download/ 挂在 agent 家目录（即 workspace 根本身）
    下；首次访问家目录时会自动把遗留布局条目迁移到位（见
    workspace_paths.agent_home）。
    """
    workspace = workspace_root(chat_id, namespace)
    workspace.mkdir(parents=True, exist_ok=True)
    # 显式预创建 upload/ 与 download/：两者都位于 agent 家目录下，
    # workspace_upload_root / workspace_download_root 是幂等的（mkdir
    # exist_ok + chmod 0o700），重复调用不会出错；首次调用就把这两棵
    # 子树准备好（并顺带触发家目录的一次性迁移）。
    workspace_upload_root(chat_id, namespace)
    workspace_download_root(chat_id, namespace)


async def _ensure_workspace_initialized(chat_id: int, namespace: str | None = None) -> None:
    """Restore, bootstrap and persist the per-user skill resource layer.

    The marker is deliberately checked only after the first startup recovery.
    This matters on ephemeral deployments: the local workspace and marker can
    disappear together while the user's skills remain in R2.
    """
    resolved_namespace = workspace_namespace(chat_id, namespace)
    key = resolved_namespace
    lock = await _get_workspace_init_lock(key)
    async with lock:
        # workspace 根（agent 家目录本身）才是 skills/ 与初始化标记的归属；
        # agent_home() 首次访问时会自动迁移遗留布局条目（含 v2.3.0 过渡
        # 草案的 claude/ 折叠回根）。
        home = agent_home(chat_id, resolved_namespace)
        marker = home / ".skills_initialized"

        if key in _workspace_initialized:
            _workspace_initialized.add(key)
            return

        try:
            from skills import sync_all_skill_assets_to_workspace

            restore = await _restore_user_skills_from_r2(home, resolved_namespace)
            summary = await asyncio.to_thread(
                sync_all_skill_assets_to_workspace,
                home,
            )
            if summary.get("errors"):
                logger.warning(
                    "部分 skill 包初始化失败 namespace=%s: %s",
                    resolved_namespace,
                    "; ".join(summary["errors"]),
                )
                return

            persist = await _persist_user_skills_to_r2(home, resolved_namespace)
            if persist.get("errors"):
                logger.warning(
                    "部分用户 skills 持久化失败 namespace=%s: %s",
                    resolved_namespace,
                    "; ".join(persist["errors"]),
                )
                return

            marker.write_text("initialized\n", encoding="utf-8")
            _workspace_initialized.add(key)
            logger.info(
                "用户 skills 已恢复/持久化 namespace=%s restored=%s copied=%s uploaded=%s",
                resolved_namespace,
                restore.get("restored", 0),
                summary.get("copied", 0),
                persist.get("uploaded", 0),
            )
        except Exception as exc:
            logger.warning(
                "初始化 skill 包到 workspace 失败 namespace=%s: %s",
                resolved_namespace,
                exc,
            )


def _skill_r2_prefix(namespace: str) -> str:
    return f"skills/{workspace_namespace(0, namespace)}"


async def _restore_user_skills_from_r2(home: Path, namespace: str) -> dict[str, object]:
    """Restore missing files from the namespace's R2 skill prefix."""
    result: dict[str, object] = {"restored": 0, "errors": []}
    prefix = _skill_r2_prefix(namespace)
    try:
        keys = await list_r2_keys(prefix)
        skills_root = home / "skills"
        for key in keys:
            rel = key[len(prefix):].lstrip("/")
            if not rel:
                continue
            target = (skills_root / rel).resolve()
            if skills_root.resolve() not in target.parents:
                result["errors"].append(f"unsafe skill key: {key}")
                continue
            if target.exists():
                continue
            data = await download_from_r2(key)
            if data is None:
                result["errors"].append(f"download failed: {key}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            result["restored"] += 1
    except Exception as exc:
        result["errors"].append(str(exc))
    return result


async def _persist_user_skills_to_r2(home: Path, namespace: str) -> dict[str, object]:
    """Upload the complete local skill tree under the user's namespace."""
    result: dict[str, object] = {"uploaded": 0, "errors": []}
    skills_root = home / "skills"
    if not skills_root.is_dir():
        return result
    prefix = _skill_r2_prefix(namespace)
    try:
        for path in skills_root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(skills_root).as_posix()
            remote_key = f"{prefix}/{rel}"
            content_type = "text/plain" if path.suffix.lower() in {".md", ".txt"} else "application/octet-stream"
            uploaded = await upload_bytes_to_r2(path.read_bytes(), remote_key, content_type)
            if uploaded is None:
                result["errors"].append(f"upload failed: {remote_key}")
            else:
                result["uploaded"] += 1
    except Exception as exc:
        result["errors"].append(str(exc))
    return result




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
#   - upload/：待发送产物的暂存区（present_files 只接受 upload/ 下的文件）。
# bash 本就能直接读写这两棵子树（相对路径即可），无需跨边界原语：
# 模型直接使用 bash：`cat download/x.pdf`、`cp out.txt upload/out.txt`。
#
# 注意：download/ 不做 R2 同步，持久化由 file_handlers.py 的
# `telegram/{file_id}` R2 缓存负责；upload/ 也不做 R2 镜像同步——
# 恢复方向从未被调用且会造成重复存储。


# ========== 可选：初始化工作区（后台执行） ==========

async def init_workspace(chat_id: int, namespace: str | None = None) -> None:
    """Initialize the workspace and packaged skills once for this workspace."""
    try:
        await _ensure_workspace_initialized(chat_id, namespace)
    except Exception as e:
        logger.error(f"Workspace 初始化失败: {e}")
