# workspace_utils.py
import asyncio
import os
import logging
from pathlib import Path
from apitelegramchat.workspace_paths import workspace_root, workspace_workdir, workspace_namespace

from apitelegramchat.s3_utils import (
    upload_bytes_to_r2,
    download_from_r2,
    list_r2_objects,
    delete_r2_object,
)

logger = logging.getLogger(__name__)

# workspace 根目录下，这些顶层子目录名不参与 R2 全量同步（既不上传，也不因为
# 远程没有就被删除）。.skills/ 是本地 skill 资源；.runtime_cache/ 是沙箱运行时
# 缓存（pip/ccache/tmp/pycache 等），不属于用户文件，也不应该进入 R2。
_LOCAL_ONLY_TOPLEVEL_DIRS = {".skills", ".runtime_cache"}


def _is_local_only_rel(rel: str) -> bool:
    """判断相对路径的顶层目录是否属于本地专用（跳过 R2 同步）的目录。"""
    top = rel.split(os.sep, 1)[0]
    return top in _LOCAL_ONLY_TOPLEVEL_DIRS

# workspace 访问锁：保护同一聊天的本地文件操作，避免并发修改。
_workspace_locks = {}
_workspace_locks_lock = asyncio.Lock()

# 每个进程生命周期内，每个 workspace 只做一次 R2 全量初始化。
# 后续 text_editor / bash / present_files 直接使用本地 workspace，避免每次工具调用
# 都重新 list/download R2 而触发超时。
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


async def _ensure_workspace_initialized(chat_id: int, namespace: str | None = None) -> None:
    """首次访问 workspace 时从 R2 初始化一次；之后本地 workspace 即为工作副本。"""
    key = workspace_namespace(chat_id, namespace)
    if key in _workspace_initialized:
        return

    init_lock = await _get_workspace_init_lock(key)
    async with init_lock:
        if key in _workspace_initialized:
            return
        await _sync_workspace_from_r2(chat_id)
        _workspace_initialized.add(key)
        logger.debug("Workspace initialized once: chat_id=%s namespace=%s", chat_id, key)


def _mark_workspace_initialized(chat_id: int, namespace: str | None = None) -> None:
    """在外部已完成可靠初始化后标记 workspace，避免再次全量同步。"""
    _workspace_initialized.add(workspace_namespace(chat_id, namespace))


# ========== workspace 全量同步（editor/ 域） ==========
# 这些函数同步 workspace 根目录下的所有文件，用 R2 的 editor/{ns}/ prefix。
# state 文件（todos.json / memories.json）用另一组单文件同步函数 + state/{ns}/ prefix，
# 两个 prefix 天然隔离，不需要文件名黑名单。

async def _sync_workspace_from_r2(chat_id: int):
    """
    从 R2 拉取 editor/{ns}/ 下所有文件到本地 workspace，并删除本地多余文件。
    这是一次性初始化原语；调用方应优先使用 _ensure_workspace_initialized()。
    """
    workspace = workspace_root(chat_id)
    workspace.mkdir(parents=True, exist_ok=True)
    workdir = workspace_workdir(chat_id)
    prefix = f"editor/{workspace_namespace(chat_id)}/"
    keys = await list_r2_objects(prefix)
    remote_rels = set()
    # 防止路径遍历：在写入前对每个 rel 做严格校验
    for key in keys:
        rel = key[len(prefix):]
        if not rel:
            continue
        # 拒绝包含 .. 或绝对路径的 rel，防止通过 R2 key 注入路径遍历
        if "\x00" in rel:
            logger.warning(f"拒绝含 null 字节的 R2 key: {key!r}")
            continue
        norm_rel = os.path.normpath(rel)
        if norm_rel == "." or norm_rel.startswith("..") or os.path.isabs(norm_rel):
            logger.warning(f"拒绝路径遍历的 R2 key: {key!r} -> rel={rel!r}")
            continue
        local_path = workspace / norm_rel
        # resolve() 解析符号链接后再校验仍在 workspace 之下
        try:
            resolved = local_path.resolve()
        except Exception:
            logger.warning(f"resolve 失败: {local_path}")
            continue
        if resolved != workspace and workspace not in resolved.parents:
            logger.warning(f"拒绝越界路径: {key!r} -> {resolved}")
            continue
        remote_rels.add(norm_rel)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        data = await download_from_r2(key)
        if data is not None:
            with open(local_path, "wb") as f:
                f.write(data)
    # 删除本地多余文件（远程没有的）—— 跳过本地专用目录（如 .skills/）
    for root, dirs, files in os.walk(workspace):
        # 就地过滤 dirs，防止 os.walk 继续深入本地专用目录
        dirs[:] = [
            d for d in dirs
            if not _is_local_only_rel(os.path.relpath(os.path.join(root, d), workspace))
        ]
        for file in files:
            rel = os.path.relpath(os.path.join(root, file), workspace)
            if _is_local_only_rel(rel):
                continue
            if rel not in remote_rels:
                os.remove(os.path.join(root, file))
        # 删除空目录（可选）
        for dir_name in dirs:
            dir_path = os.path.join(root, dir_name)
            if os.path.abspath(dir_path) == os.path.abspath(workdir):
                continue
            if not os.listdir(dir_path):
                os.rmdir(dir_path)


async def _sync_workspace_to_r2(chat_id: int):
    """
    将本地 workspace 所有文件上传到 R2 的 editor/{ns}/ prefix，并删除远程多余文件。
    全量同步，用于 bash 工具执行后。
    """
    workspace = workspace_root(chat_id)
    if not workspace.exists():
        return
    workdir = workspace_workdir(chat_id)
    prefix = f"editor/{workspace_namespace(chat_id)}/"
    local_rels = set()
    for root, dirs, files in os.walk(workspace):
        # 就地过滤 dirs，本地专用目录（如 .skills/）不参与遍历，不上传
        dirs[:] = [
            d for d in dirs
            if not _is_local_only_rel(os.path.relpath(os.path.join(root, d), workspace))
        ]
        for file in files:
            abs_path = os.path.join(root, file)
            rel = os.path.relpath(abs_path, workspace)
            if _is_local_only_rel(rel):
                continue
            local_rels.add(rel)
            key = prefix + rel
            with open(abs_path, "rb") as f:
                data = f.read()
            await upload_bytes_to_r2(data, key, "application/octet-stream")
    # 删除远程多余文件（本地专用目录从不产生远程 key，天然不受影响）
    remote_keys = await list_r2_objects(prefix)
    remote_rels = {key[len(prefix):] for key in remote_keys if key.startswith(prefix)}
    to_delete = remote_rels - local_rels
    for rel in to_delete:
        key = prefix + rel
        await delete_r2_object(key)


# Bash 可能连续修改 workspace；只允许每个 workspace 同时存在一个后台同步任务，
# 并用短 debounce 合并连续修改，避免每个 Bash 都启动一次并发的全量 R2 上传。
_workspace_sync_tasks: dict[str, asyncio.Task] = {}
_workspace_sync_tasks_lock = asyncio.Lock()


async def _async_sync_workspace_to_r2(chat_id: int):
    """后台同步 workspace；同步本身不属于工具调用的响应路径。"""
    try:
        # 给连续 Bash 一个很短的合并窗口；期间的修改由最后一次快照统一上传。
        await asyncio.sleep(0.75)
        lock = await _get_workspace_lock(chat_id)
        async with lock:
            await _sync_workspace_to_r2(chat_id)
        logger.debug("异步 workspace 同步完成: chat_id=%s", chat_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("异步 workspace 同步失败: %s", e)
    finally:
        key = workspace_namespace(chat_id)
        async with _workspace_sync_tasks_lock:
            task = _workspace_sync_tasks.get(key)
            if task is asyncio.current_task():
                _workspace_sync_tasks.pop(key, None)


def schedule_workspace_sync(chat_id: int) -> None:
    """调度/合并 workspace R2 同步；不会为每个 Bash 创建并发上传任务。"""
    key = workspace_namespace(chat_id)
    task = _workspace_sync_tasks.get(key)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(
        _async_sync_workspace_to_r2(chat_id),
        name=f"workspace-sync-{key}",
    )
    _workspace_sync_tasks[key] = task


# ========== 单文件定向同步（state/ 域） ==========
# 用于 todo / memory 等小 JSON 文件。这些文件的本地路径在 state/{ns}/ 目录，
# R2 key 也在 state/{ns}/ prefix 下，和 workspace 的 editor/{ns}/ 天然隔离。
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


# ========== 可选：初始化工作区（后台执行） ==========

async def init_workspace(chat_id: int):
    """后台初始化工作区；同一进程内只初始化一次。"""
    try:
        await _ensure_workspace_initialized(chat_id)
        logger.info(f"Workspace 初始化完成: chat_id={chat_id}")
    except Exception as e:
        logger.error(f"Workspace 初始化失败: {e}")
