# workspace_utils.py
import asyncio
import os
import logging
from pathlib import Path
import shutil
import mimetypes
from apitelegramchat.workspace_paths import (
    workspace_root, workspace_workdir, workspace_files_root, workspace_namespace,
    runtime_cache_root,
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


def _copy_persistent_tree_to_runtime(chat_id: int, namespace: str | None = None) -> None:
    """Incrementally hydrate the ephemeral shell tree from the persistent files tree.

    This is idempotent: it only copies files that don't exist in the target or
    are newer than the source. No marker file — every call is a cheap stat-check
    sweep. This ensures files downloaded by a background R2 init (which may
    complete AFTER the first tool call) eventually appear in runtime/exec/ on
    the next tool call.
    """
    source = workspace_files_root(chat_id, namespace)
    target = workspace_workdir(chat_id, namespace)
    target.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        return

    # Walk source and copy any file that's missing or outdated in target.
    for entry in source.rglob("*"):
        if entry.is_dir() or entry.is_symlink():
            continue
        rel = entry.relative_to(source)
        dst = target / rel
        try:
            src_stat = entry.stat()
            if dst.exists():
                dst_stat = dst.stat()
                # Skip if target is same age or newer (already copied).
                if dst_stat.st_mtime >= src_stat.st_mtime and dst_stat.st_size == src_stat.st_size:
                    continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, dst)
        except OSError:
            continue


async def _ensure_runtime_workspace(chat_id: int, namespace: str | None = None) -> None:
    """Ensure the execution tree contains persistent files and all packaged skills."""
    await _ensure_workspace_initialized(chat_id, namespace)
    await asyncio.to_thread(_copy_persistent_tree_to_runtime, chat_id, namespace)
    try:
        from apitelegramchat.skills import sync_all_skill_assets_to_workspace
        summary = await asyncio.to_thread(
            sync_all_skill_assets_to_workspace,
            workspace_root(chat_id, namespace),
        )
        if summary.get("errors"):
            logger.warning(
                "部分 skill 包同步失败 chat_id=%s: %s",
                chat_id,
                "; ".join(summary["errors"]),
            )
    except Exception as exc:
        logger.warning("同步 skill 包到 workspace 失败 chat_id=%s: %s", chat_id, exc)


def _safe_workspace_relpath(rel_path: str) -> str:
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("path must be a non-empty relative file path")
    if "\x00" in rel_path:
        raise ValueError("path contains a null byte")
    norm = os.path.normpath(rel_path.replace("\\", "/"))
    if norm in {".", ""} or norm.startswith("..") or os.path.isabs(norm):
        raise ValueError(f"invalid relative path: {rel_path!r}")
    return norm


async def _persist_runtime_file(
    chat_id: int,
    rel_path: str,
    *,
    delete: bool = False,
    namespace: str | None = None,
) -> dict:
    """Persist one explicit runtime file into files/ and R2.

    Only callers that explicitly name the file can cross the persistence boundary.
    """
    rel = _safe_workspace_relpath(rel_path)
    runtime_root = workspace_workdir(chat_id, namespace)
    files_root = workspace_files_root(chat_id, namespace)
    source = (runtime_root / rel).resolve()
    runtime_resolved = runtime_root.resolve()
    if source != runtime_resolved and runtime_resolved not in source.parents:
        raise ValueError("path escapes runtime workspace")

    dest = (files_root / rel).resolve()
    files_resolved = files_root.resolve()
    if dest != files_resolved and files_resolved not in dest.parents:
        raise ValueError("path escapes persistent files workspace")

    key = f"editor/{workspace_namespace(chat_id, namespace)}/{rel}"

    if delete:
        if dest.exists() and dest.is_file():
            dest.unlink()
        elif dest.exists() and dest.is_dir():
            raise ValueError("workspace_commit only accepts files, not directories")
        await delete_r2_object(key)
        return {"path": rel, "deleted": True, "key": key}

    if not source.is_file():
        raise FileNotFoundError(f"runtime file not found: {rel}")

    data = await asyncio.to_thread(source.read_bytes)
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(dest.write_bytes, data)
    content_type = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    url = await upload_bytes_to_r2(data, key, content_type)
    return {
        "path": rel,
        "deleted": False,
        "key": key,
        "bytes": len(data),
        "url": url,
    }


async def persist_workspace_file(
    chat_id: int,
    rel_path: str,
    *,
    delete: bool = False,
    namespace: str | None = None,
) -> dict:
    # ★ init 在 workspace lock 外面执行（同 bash / text_editor）。
    await _ensure_runtime_workspace(chat_id, namespace)
    lock = await _get_workspace_lock(chat_id, namespace)
    async with lock:
        return await _persist_runtime_file(
            chat_id, rel_path, delete=delete, namespace=namespace
        )


# 跟踪 init 是否正在进行中。工具调用检查这个集合，如果 init 在跑就直接返回，
# 不等锁。这是解决"工具调用阻塞在 init_lock 上"的关键。
_workspace_init_in_progress: set[str] = set()

# init 全局超时：即使 R2 上有几百个文件，init 也最多跑这么久。
# 超时后标记为已完成，后续工具调用使用本地已有的部分状态。
_INIT_TIMEOUT_SEC = 30.0


async def _ensure_workspace_initialized(chat_id: int, namespace: str | None = None) -> None:
    """非阻塞检查：如果 init 已完成或在跑，立即返回。

    工具调用（text_editor / bash 等）调这个函数。它永远不会阻塞——
    如果 init 还没开始，会 fire-and-forget 启动一个后台 init 任务，
    然后立即返回。工具使用本地已有的文件状态（可能为空）。

    这解决了一个关键死锁：之前工具调用会等 init_lock，而 init_lock 被后台
    init 任务持有（可能跑 3+ 分钟）。工具的 45s 超时在等锁阶段就耗尽了，
    finally 块根本没机会执行。
    """
    key = workspace_namespace(chat_id, namespace)
    if key in _workspace_initialized or key in _workspace_init_in_progress:
        return
    # init 还没开始——fire and forget。工具不等它完成。
    asyncio.create_task(_do_workspace_init(chat_id, namespace))


async def _do_workspace_init(chat_id: int, namespace: str | None = None) -> None:
    """实际的 R2 同步逻辑，带 30s 全局超时。

    由后台任务调用（app.py 的 init_workspace 或 _ensure_workspace_initialized
    的 fire-and-forget）。同一个 chat 只会跑一次。
    """
    key = workspace_namespace(chat_id, namespace)
    if key in _workspace_initialized or key in _workspace_init_in_progress:
        return

    init_lock = await _get_workspace_init_lock(key)
    async with init_lock:
        if key in _workspace_initialized or key in _workspace_init_in_progress:
            return
        _workspace_init_in_progress.add(key)

    try:
        await asyncio.wait_for(
            _sync_workspace_and_upload(chat_id, namespace),
            timeout=_INIT_TIMEOUT_SEC,
        )
        logger.info("Workspace 初始化完成: chat_id=%s", chat_id)
    except asyncio.TimeoutError:
        logger.warning(
            "Workspace init timed out after %ss for chat_id=%s; "
            "proceeding with partial local state.",
            _INIT_TIMEOUT_SEC, chat_id,
        )
    except asyncio.CancelledError:
        logger.warning(
            "Workspace init cancelled for chat_id=%s; "
            "proceeding with partial local state.", chat_id,
        )
        raise
    except Exception as e:
        logger.warning(
            "Workspace init failed for chat_id=%s (%s); "
            "proceeding with local-only state.", chat_id, e,
        )
    finally:
        _workspace_init_in_progress.discard(key)
        _workspace_initialized.add(key)


async def _sync_workspace_and_upload(chat_id: int, namespace: str | None = None) -> None:
    """同步 files/ 和 upload/ 两棵子树。"""
    await _sync_workspace_from_r2(chat_id)
    await _sync_upload_from_r2(chat_id, namespace)


def _mark_workspace_initialized(chat_id: int, namespace: str | None = None) -> None:
    """在外部已完成可靠初始化后标记 workspace，避免再次全量同步。"""
    _workspace_initialized.add(workspace_namespace(chat_id, namespace))


# ========== 兼容/迁移：旧 workspace 全量同步（已禁用） ==========
# 历史版本会把 editor/{ns}/ 下整个工作区镜像到 R2；该路径现在只保留为
# 兼容桩，绝不再读取/上传运行时树。新的持久化边界是显式文件提交。

async def _sync_workspace_from_r2(chat_id: int):
    """
    从 R2 拉取 editor/{ns}/ 下所有文件到本地 workspace，并删除本地多余文件。
    这是一次性初始化原语；调用方应优先使用 _ensure_workspace_initialized()。
    """
    workspace = workspace_root(chat_id)
    workspace.mkdir(parents=True, exist_ok=True)
    files_root = workspace_files_root(chat_id)
    prefix = f"editor/{workspace_namespace(chat_id)}/"
    keys = await list_r2_objects(prefix)
    remote_rels = set()
    # 防止路径遍历：在写入前对每个 rel 做严格校验
    for key in keys:
        rel = key[len(prefix):]
        if not rel:
            continue
        # resolve() 解析符号链接后再校验仍在 files 层之下
        # 拒绝包含 .. 或绝对路径的 rel，防止通过 R2 key 注入路径遍历
        if "\x00" in rel:
            logger.warning(f"拒绝含 null 字节的 R2 key: {key!r}")
            continue
        norm_rel = os.path.normpath(rel)
        if norm_rel == "." or norm_rel.startswith("..") or os.path.isabs(norm_rel):
            logger.warning(f"拒绝路径遍历的 R2 key: {key!r} -> rel={rel!r}")
            continue
        local_path = files_root / norm_rel
        # resolve() 解析符号链接后再校验仍在 files 层之下
        try:
            resolved = local_path.resolve()
        except Exception:
            logger.warning(f"resolve 失败: {local_path}")
            continue
        if resolved != files_root and files_root not in resolved.parents:
            logger.warning(f"拒绝越界路径: {key!r} -> {resolved}")
            continue
        remote_rels.add(norm_rel)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        data = await download_from_r2(key)
        if data is not None:
            with open(local_path, "wb") as f:
                f.write(data)
    # 远程没有的用户文件从 files 层删除；其他 workspace 层不受影响。
    if files_root.exists():
        for root, dirs, files in os.walk(files_root):
            for file in files:
                rel = os.path.relpath(os.path.join(root, file), files_root)
                if rel not in remote_rels:
                    os.remove(os.path.join(root, file))
            for dir_name in list(dirs):
                dir_path = os.path.join(root, dir_name)
                if not os.listdir(dir_path):
                    os.rmdir(dir_path)


async def _sync_workspace_to_r2(chat_id: int):
    """Deprecated compatibility shim.

    The old implementation mirrored every file under the workspace and is
    intentionally disabled. It was the source of dependency-tree uploads
    (e.g. node_modules) after package installation.
    """
    logger.warning("Ignoring legacy full workspace->R2 sync; persistence is explicit per-file now.")
    return
    files_root = workspace_files_root(chat_id)
    if not files_root.exists():
        return
    prefix = f"editor/{workspace_namespace(chat_id)}/"
    local_rels = set()
    for root, _dirs, files in os.walk(files_root):
        for file in files:
            abs_path = os.path.join(root, file)
            rel = os.path.relpath(abs_path, files_root)
            local_rels.add(rel)
            key = prefix + rel
            with open(abs_path, "rb") as f:
                data = f.read()
            await upload_bytes_to_r2(data, key, "application/octet-stream")
    # 删除远程多余文件；R2 只代表 files 层。
    remote_keys = await list_r2_objects(prefix)
    remote_rels = {key[len(prefix):] for key in remote_keys if key.startswith(prefix)}
    to_delete = remote_rels - local_rels
    for rel in to_delete:
        key = prefix + rel
        await delete_r2_object(key)


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


# ========== upload/ & download/ 持久化子树 ==========
# 这两棵子树都镜像到 R2（独立 prefix），保证进程重启后用户上传的文档与
# 模型暂存的产物不丢失。它们与 files/ (editor/{ns}/) 完全隔离，互不影响。

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
# 子树，所以模型需要这两个原语把文件搬进/搬出 runtime/exec/。

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
    """把 download/{filename} 复制到 runtime/exec/{filename}。

    返回 runtime 工作区下的相对路径，bash / text_editor 可以直接使用。
    默认不覆盖已存在的文件，避免误覆盖模型已经在编辑的同名文件。
    """
    rel = _safe_relative_name(filename)
    # ★ init 在 workspace lock 外面执行（同 bash / text_editor）。
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
    """把 runtime/exec/{rel_path} 复制到 upload/{rel_path}，并同步到 R2。

    这是模型把产物送入“即将发送给用户”暂存区的唯一显式入口。
    present_files 只从 upload/ 读取，所以模型必须先调用 stage_to_upload。
    """
    rel = _safe_relative_name(rel_path)
    # ★ init 在 workspace lock 外面执行（同 bash / text_editor）。
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

async def init_workspace(chat_id: int):
    """后台初始化工作区；同一进程内只初始化一次。

    调用 _do_workspace_init（带 30s 超时的实际同步逻辑）。
    _do_workspace_init 内部有幂等检查，重复调用是 no-op。
    """
    try:
        await _do_workspace_init(chat_id)
    except Exception as e:
        logger.error(f"Workspace 初始化失败: {e}")
