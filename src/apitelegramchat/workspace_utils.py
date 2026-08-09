# workspace_utils.py
import asyncio
import os
import logging
from pathlib import Path
from apitelegramchat.workspace_paths import workspace_root

from apitelegramchat.s3_utils import (
    upload_bytes_to_r2,
    download_from_r2,
    file_exists_in_r2,
    list_r2_objects,
    delete_r2_object,
)

logger = logging.getLogger(__name__)

# 全局锁管理
_workspace_locks = {}
_workspace_locks_lock = asyncio.Lock()


async def _get_workspace_lock(chat_id: int) -> asyncio.Lock:
    """获取或创建该 chat 的 workspace 锁"""
    async with _workspace_locks_lock:
        if chat_id not in _workspace_locks:
            _workspace_locks[chat_id] = asyncio.Lock()
        return _workspace_locks[chat_id]


async def _sync_workspace_from_r2(chat_id: int):
    """
    从 R2 拉取所有文件到本地 workspace，并删除本地多余文件。
    全量同步，用于初始化或恢复。
    """
    workspace = workspace_root(chat_id)
    workspace.mkdir(parents=True, exist_ok=True)
    prefix = f"editor/{chat_id}/"
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
    # 删除本地多余文件（远程没有的）
    for root, dirs, files in os.walk(workspace):
        for file in files:
            rel = os.path.relpath(os.path.join(root, file), workspace)
            if rel not in remote_rels:
                os.remove(os.path.join(root, file))
        # 删除空目录（可选）
        for dir_name in dirs:
            dir_path = os.path.join(root, dir_name)
            if not os.listdir(dir_path):
                os.rmdir(dir_path)


async def _sync_workspace_to_r2(chat_id: int):
    """
    将本地所有文件上传到 R2，并删除远程多余文件。
    全量同步，用于 bash 等可能产生大量变更的场景。
    """
    workspace = workspace_root(chat_id)
    if not workspace.exists():
        return
    prefix = f"editor/{chat_id}/"
    local_rels = set()
    for root, dirs, files in os.walk(workspace):
        for file in files:
            abs_path = os.path.join(root, file)
            rel = os.path.relpath(abs_path, workspace)
            local_rels.add(rel)
            key = prefix + rel
            with open(abs_path, "rb") as f:
                data = f.read()
            await upload_bytes_to_r2(data, key, "application/octet-stream")
    # 删除远程多余文件
    remote_keys = await list_r2_objects(prefix)
    remote_rels = {key[len(prefix):] for key in remote_keys if key.startswith(prefix)}
    to_delete = remote_rels - local_rels
    for rel in to_delete:
        key = prefix + rel
        await delete_r2_object(key)


async def _async_sync_workspace_to_r2(chat_id: int):
    """
    异步全量同步（用于 bash 后），不阻塞主流程。
    """
    try:
        await _sync_workspace_to_r2(chat_id)
        logger.debug(f"异步全量同步完成: chat_id={chat_id}")
    except Exception as e:
        logger.error(f"异步全量同步失败: {e}")


# ========== 单文件定向同步（轻量级，用于 todo/memory/skill 等小 JSON 文件） ==========
# 旧的全量同步（_sync_workspace_from_r2 / _sync_workspace_to_r2）会下载/上传
# workspace 里的所有文件。当 workspace 积累了几十个文件后，每次工具调用都要
# 等 30+ 秒做全量同步——模型流式输出超时，返回空内容。
#
# 这组新函数只同步指定的单个文件，把延迟从 O(所有文件) 降到 O(1)。

async def _sync_file_from_r2(chat_id: int, filename: str) -> None:
    """
    仅从 R2 下载指定的单个文件到本地 workspace。
    如果 R2 上没有该文件，则不做什么（本地可能也没有，或本地是新建的）。
    """
    workspace = workspace_root(chat_id)
    workspace.mkdir(parents=True, exist_ok=True)
    local_path = workspace / filename

    # 路径安全校验
    safe_name = os.path.normpath(filename)
    if safe_name == "." or safe_name.startswith("..") or os.path.isabs(safe_name):
        logger.warning(f"拒绝路径遍历的 filename: {filename!r}")
        return

    key = f"editor/{chat_id}/{safe_name}"
    data = await download_from_r2(key)
    if data is not None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
    # 如果 R2 上没有该文件，本地保留现状（可能是首次创建）


async def _sync_file_to_r2(chat_id: int, filename: str) -> None:
    """
    仅将本地指定的单个文件上传到 R2。
    如果本地文件不存在，则删除 R2 上的对应文件（如果有的话）。
    """
    workspace = workspace_root(chat_id)
    local_path = workspace / filename

    safe_name = os.path.normpath(filename)
    if safe_name == "." or safe_name.startswith("..") or os.path.isabs(safe_name):
        logger.warning(f"拒绝路径遍历的 filename: {filename!r}")
        return

    key = f"editor/{chat_id}/{safe_name}"
    if local_path.is_file():
        with open(local_path, "rb") as f:
            data = f.read()
        await upload_bytes_to_r2(data, key, "application/json")
    else:
        # 本地没有此文件，清理 R2 上的残留
        await delete_r2_object(key)


# ========== 可选：初始化工作区（后台执行） ==========

async def init_workspace(chat_id: int):
    """
    后台初始化工作区，从 R2 拉取文件。可在首次消息时调用。
    """
    try:
        await _sync_workspace_from_r2(chat_id)
        logger.info(f"Workspace 初始化完成: chat_id={chat_id}")
    except Exception as e:
        logger.error(f"Workspace 初始化失败: {e}")
