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

# 全局锁管理
_workspace_locks = {}
_workspace_locks_lock = asyncio.Lock()

# ========== R2 同步排除规则 ==========
# HOME 指向 workspace，所以包管理器（pip --user / npm / cargo ...）的产物会落到
# workspace 下的 .local/ .cache/ .npm/ node_modules/ 等目录。这些绝对不能同步到 R2：
#   1. 体积大（python-docx 一个包就 200+ 文件）
#   2. 平台相关（.so 在不同镜像间不通用）
#   3. 噪音（每次 pip install 都触发上千次 R2 PUT）
# 同步时直接跳过这些目录名（在 os.walk 的 dirs 列表里就地剪枝）。
_SYNC_SKIP_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", ".local", ".npm", ".yarn", ".pnpm-store",
    "node_modules", ".venv", "venv", ".env",
    ".git", ".hg", ".svn",
    ".ipynb_checkpoints",
})

# 也跳过常见临时/编辑器文件
_SYNC_SKIP_FILE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".swp", ".swo", ".DS_Store")


def _should_skip_dir(name: str) -> bool:
    return name in _SYNC_SKIP_DIRS


def _should_skip_file(name: str) -> bool:
    return name in _SYNC_SKIP_DIRS or name.endswith(_SYNC_SKIP_FILE_SUFFIXES)



async def _get_workspace_lock(chat_id: int, namespace: str | None = None) -> asyncio.Lock:
    """获取或创建该用户/作用域的 workspace 锁。"""
    key = workspace_namespace(chat_id, namespace)
    async with _workspace_locks_lock:
        if key not in _workspace_locks:
            _workspace_locks[key] = asyncio.Lock()
        return _workspace_locks[key]


# ========== workspace 全量同步（editor/ 域） ==========
# 这些函数同步 workspace 根目录下的所有文件，用 R2 的 editor/{ns}/ prefix。
# state 文件（todos.json / memories.json）用另一组单文件同步函数 + state/{ns}/ prefix，
# 两个 prefix 天然隔离，不需要文件名黑名单。

async def _sync_workspace_from_r2(chat_id: int):
    """
    从 R2 拉取 editor/{ns}/ 下所有文件到本地 workspace，并删除本地多余文件。
    全量同步，用于 bash 工具执行前的初始化。

    跳过 _SYNC_SKIP_DIRS（.cache / .local / __pycache__ / node_modules 等），
    因为它们是包管理器产物，不应进入 R2。
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
        # 跳过包管理器产物 —— 即使 R2 上有（历史遗留），也不同步下来
        rel_parts = norm_rel.split(os.sep)
        if any(part in _SYNC_SKIP_DIRS for part in rel_parts):
            continue
        if _should_skip_file(os.path.basename(norm_rel)):
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
    # 删除本地多余文件（远程没有的）—— 但只清理非排除目录里的文件
    for root, dirs, files in os.walk(workspace):
        # 就地剪枝：跳过 .cache/.local/__pycache__/node_modules 等
        # （workdir 本身就是 workspace 根，不需要特殊处理）
        dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
        for file in files:
            if _should_skip_file(file):
                continue
            rel = os.path.relpath(os.path.join(root, file), workspace)
            if rel not in remote_rels:
                try:
                    os.remove(os.path.join(root, file))
                except OSError:
                    pass
        # 删除空目录（可选）
        for dir_name in list(dirs):
            dir_path = os.path.join(root, dir_name)
            if os.path.abspath(dir_path) == os.path.abspath(workdir):
                continue
            if not os.listdir(dir_path):
                try:
                    os.rmdir(dir_path)
                except OSError:
                    pass


async def _sync_workspace_to_r2(chat_id: int):
    """
    将本地 workspace 所有文件上传到 R2 的 editor/{ns}/ prefix，并删除远程多余文件。
    全量同步，用于 bash 工具执行后。

    跳过 _SYNC_SKIP_DIRS（.cache / .local / __pycache__ / node_modules 等），
    避免 pip --user / npm install 的产物污染 R2。
    """
    workspace = workspace_root(chat_id)
    if not workspace.exists():
        return
    workdir = workspace_workdir(chat_id)
    prefix = f"editor/{workspace_namespace(chat_id)}/"
    local_rels = set()
    for root, dirs, files in os.walk(workspace):
        # 就地剪枝：跳过 .cache/.local/__pycache__/node_modules 等
        dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
        for file in files:
            if _should_skip_file(file):
                continue
            abs_path = os.path.join(root, file)
            rel = os.path.relpath(abs_path, workspace)
            local_rels.add(rel)
            key = prefix + rel
            with open(abs_path, "rb") as f:
                data = f.read()
            await upload_bytes_to_r2(data, key, "application/octet-stream")
    # 删除远程多余文件
    remote_keys = await list_r2_objects(prefix)
    remote_rels = set()
    for key in remote_keys:
        if not key.startswith(prefix):
            continue
        rel = key[len(prefix):]
        norm_rel = os.path.normpath(rel)
        rel_parts = norm_rel.split(os.sep)
        # 远程清理时也跳过排除目录里的文件 —— 不删它们，让 R2 lifecycle 自己回收
        if any(part in _SYNC_SKIP_DIRS for part in rel_parts):
            continue
        if _should_skip_file(os.path.basename(norm_rel)):
            continue
        remote_rels.add(norm_rel)
    to_delete = remote_rels - local_rels
    for rel in to_delete:
        key = prefix + rel
        await delete_r2_object(key)


async def _async_sync_workspace_to_r2(chat_id: int):
    """异步全量同步（用于 bash 后），不阻塞主流程。"""
    try:
        await _sync_workspace_to_r2(chat_id)
        logger.debug(f"异步全量同步完成: chat_id={chat_id}")
    except Exception as e:
        logger.error(f"异步全量同步失败: {e}")


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
    """后台初始化工作区，从 R2 拉取文件。可在首次消息时调用。"""
    try:
        await _sync_workspace_from_r2(chat_id)
        logger.info(f"Workspace 初始化完成: chat_id={chat_id}")
    except Exception as e:
        logger.error(f"Workspace 初始化失败: {e}")
