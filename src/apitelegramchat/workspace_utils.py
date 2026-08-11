# workspace_utils.py
import asyncio
import os
import logging
from pathlib import Path
import mimetypes
from apitelegramchat.workspace_paths import (
    workspace_root, workspace_workdir, workspace_namespace,
    workspace_upload_root, workspace_download_root,
)

from apitelegramchat.s3_utils import (
    upload_bytes_to_r2,
    download_from_r2,
    list_r2_objects,
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
    """Ensure only the runtime workspace directory exists.

    This function is intentionally safe to call before every tool invocation.
    It MUST NOT synchronize packaged skills: ``workspace/skills`` is runtime
    state and may contain files created or edited by the agent/user.
    """
    workspace = workspace_root(chat_id, namespace)
    workspace.mkdir(parents=True, exist_ok=True)


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


# ========== upload/ & download/ 持久化子树 ==========
# 这两棵子树都镜像到 R2（独立 prefix），保证进程重启后用户上传的文档与
# 模型暂存的产物不丢失。它们与 the local workspace 完全隔离，互不影响。

async def _sync_generic_tree_from_r2(
    chat_id: int,
    local_root: Path,
    r2_prefix: str,
    *,
    namespace: str | None = None,
) -> None:
    """通用：把 R2 上某个 prefix 下的所有对象拉到 local_root，并删除本地多余文件。"""
    local_root.mkdir(parents=True, exist_ok=True)
    keys = await list_r2_objects(r2_prefix)
    remote_rels: set[str] = set()
    for key in keys:
        rel = key[len(r2_prefix):]
        if not rel:
            continue
        if "\x00" in rel:
            logger.warning(f"拒绝含 null 字节的 R2 key: {key!r}")
            continue
        norm_rel = os.path.normpath(rel)
        if norm_rel == "." or norm_rel.startswith("..") or os.path.isabs(norm_rel):
            logger.warning(f"拒绝路径遍历的 R2 key: {key!r} -> rel={rel!r}")
            continue
        local_path = local_root / norm_rel
        try:
            resolved = local_path.resolve()
        except Exception:
            logger.warning(f"resolve 失败: {local_path}")
            continue
        local_resolved = local_root.resolve()
        if resolved != local_resolved and local_resolved not in resolved.parents:
            logger.warning(f"拒绝越界路径: {key!r} -> {resolved}")
            continue
        remote_rels.add(norm_rel)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        data = await download_from_r2(key)
        if data is not None:
            with open(local_path, "wb") as f:
                f.write(data)
    # 删除本地多余的文件（远程已经没有的）
    for root, dirs, files in os.walk(local_root):
        for file in files:
            rel = os.path.relpath(os.path.join(root, file), local_root)
            if rel not in remote_rels:
                try:
                    os.remove(os.path.join(root, file))
                except OSError:
                    pass
        for dir_name in list(dirs):
            dir_path = os.path.join(root, dir_name)
            if not os.listdir(dir_path):
                try:
                    os.rmdir(dir_path)
                except OSError:
                    pass


async def _sync_upload_from_r2(chat_id: int, namespace: str | None = None) -> None:
    """从 R2 的 upload/{ns}/ 拉取暂存产物到本地 upload/ 目录。"""
    ns = workspace_namespace(chat_id, namespace)
    local_root = workspace_upload_root(chat_id, namespace)
    await _sync_generic_tree_from_r2(chat_id, local_root, f"upload/{ns}/", namespace=namespace)


# 注意：download/ 没有 R2 同步函数。用户上传文档的持久化由 file_handlers.py
# 的 `telegram/{file_id}` R2 缓存负责，download/ 只是本地落地缓冲。
# 历史上这里曾有过 _sync_download_from_r2 / _persist_download_file_to_r2，
# 但它们会导致：(1) 同一份字节在 R2 上存两份；(2) init 时删除本地 download/
# 里"不在 R2 download/{ns}/ 中"的文件——如果 R2 上传失败就会删掉用户刚上传
# 的本地副本，造成数据丢失。已移除。


async def _persist_upload_file_to_r2(
    chat_id: int,
    local_path: Path,
    rel_name: str,
    *,
    namespace: str | None = None,
) -> str | None:
    """把 upload/ 下的某个文件上传到 R2 的 upload/{ns}/{rel_name}。"""
    safe = os.path.normpath(rel_name)
    if safe == "." or safe.startswith("..") or os.path.isabs(safe):
        raise ValueError(f"invalid upload rel name: {rel_name!r}")
    if not local_path.is_file():
        return None
    data = await asyncio.to_thread(local_path.read_bytes)
    key = f"upload/{workspace_namespace(chat_id, namespace)}/{safe}"
    content_type = mimetypes.guess_type(rel_name)[0] or "application/octet-stream"
    url = await upload_bytes_to_r2(data, key, content_type)
    return url


# ========== fetch_from_download / stage_to_upload ==========
# 模型与 download/、upload/ 之间的显式跨边界操作。bash 不能 cd 进这两棵
# 子树，所以模型需要这两个原语把文件搬进/搬出 workspace/。

def _safe_relative_name(name: str) -> str:
    """归一化并校验一个 *单文件* 相对路径（禁止 ..、绝对路径、null 字节）。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty relative file path")
    if "\x00" in name:
        raise ValueError("name contains a null byte")
    norm = os.path.normpath(name.replace("\\", "/"))
    if norm in {".", ""} or norm.startswith("..") or os.path.isabs(norm):
        raise ValueError(f"invalid relative path: {name!r}")
    return norm


async def fetch_from_download(
    chat_id: int,
    filename: str,
    *,
    overwrite: bool = False,
    namespace: str | None = None,
) -> dict:
    """把 download/{filename} 复制到 workspace/{filename}。

    返回 runtime 工作区下的相对路径，bash / file_editor 可以直接使用。
    默认不覆盖已存在的文件，避免误覆盖模型已经在编辑的同名文件。
    """
    rel = _safe_relative_name(filename)
    # ★ init 在 workspace lock 外面执行（同 bash / file_editor）。
    await _ensure_runtime_workspace(chat_id, namespace)
    lock = await _get_workspace_lock(chat_id, namespace)
    async with lock:
        download_root = workspace_download_root(chat_id, namespace)
        runtime_root = workspace_workdir(chat_id, namespace)
        src = (download_root / rel).resolve()
        if src != download_root and download_root not in src.parents:
            raise ValueError("path escapes download root")
        if not src.is_file():
            raise FileNotFoundError(f"download file not found: {rel}")
        dst = (runtime_root / rel).resolve()
        if dst != runtime_root and runtime_root not in dst.parents:
            raise ValueError("path escapes runtime workspace")
        if dst.exists() and not overwrite:
            return {
                "path": rel,
                "fetched": False,
                "skipped": True,
                "reason": "destination already exists; pass overwrite=true to replace",
                "bytes": dst.stat().st_size if dst.is_file() else 0,
            }
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = await asyncio.to_thread(src.read_bytes)
        await asyncio.to_thread(dst.write_bytes, data)
        return {
            "path": rel,
            "fetched": True,
            "skipped": False,
            "bytes": len(data),
        }


async def stage_to_upload(
    chat_id: int,
    rel_path: str,
    *,
    namespace: str | None = None,
) -> dict:
    """把 workspace/{rel_path} 复制到 upload/{rel_path}，并同步到 R2。

    这是模型把产物送入“即将发送给用户”暂存区的唯一显式入口。
    present_files 只从 upload/ 读取，所以模型必须先调用 stage_to_upload。
    """
    rel = _safe_relative_name(rel_path)
    # ★ init 在 workspace lock 外面执行（同 bash / file_editor）。
    await _ensure_runtime_workspace(chat_id, namespace)
    lock = await _get_workspace_lock(chat_id, namespace)
    async with lock:
        runtime_root = workspace_workdir(chat_id, namespace)
        upload_root = workspace_upload_root(chat_id, namespace)
        src = (runtime_root / rel).resolve()
        if src != runtime_root and runtime_root not in src.parents:
            raise ValueError("path escapes runtime workspace")
        if not src.is_file():
            raise FileNotFoundError(f"runtime file not found: {rel}")
        dst = (upload_root / rel).resolve()
        if dst != upload_root and upload_root not in dst.parents:
            raise ValueError("path escapes upload root")
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = await asyncio.to_thread(src.read_bytes)
        await asyncio.to_thread(dst.write_bytes, data)
        # 同步到 R2 以便跨重启存活
        try:
            await _persist_upload_file_to_r2(chat_id, dst, rel, namespace=namespace)
        except Exception as exc:
            logger.warning("upload R2 sync failed for %s: %s", rel, exc)
        return {
            "path": rel,
            "staged": True,
            "bytes": len(data),
        }


async def list_download_files(chat_id: int, *, namespace: str | None = None) -> list[dict]:
    """列出 download/ 下的所有文件（相对路径 + 大小）。"""
    root = workspace_download_root(chat_id, namespace)
    out: list[dict] = []
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                out.append({"path": str(p.relative_to(root)), "size": p.stat().st_size})
            except OSError:
                continue
    return out


async def list_upload_files(chat_id: int, *, namespace: str | None = None) -> list[dict]:
    """列出 upload/ 下的所有文件（相对路径 + 大小）。"""
    root = workspace_upload_root(chat_id, namespace)
    out: list[dict] = []
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                out.append({"path": str(p.relative_to(root)), "size": p.stat().st_size})
            except OSError:
                continue
    return out


# ========== 可选：初始化工作区（后台执行） ==========

async def init_workspace(chat_id: int, namespace: str | None = None):
    """Initialize the workspace and packaged skills once for this workspace."""
    try:
        await _ensure_workspace_initialized(chat_id, namespace)
    except Exception as e:
        logger.error(f"Workspace 初始化失败: {e}")
