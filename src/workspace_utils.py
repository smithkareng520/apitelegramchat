# workspace_utils.py
import asyncio
import hashlib
import json
import os
import logging
from pathlib import Path
from workspace_paths import (
    agent_home, workspace_root, workspace_namespace,
    workspace_skills_root,
    workspace_upload_root, workspace_download_root,
)

from s3_utils import (
    upload_bytes_to_r2,
    download_from_r2,
    delete_r2_object,
    list_r2_objects,
    is_r2_configured,
)

logger = logging.getLogger(__name__)

# R2 持久化只发生在明确选择的用户文件上；运行时树永不进行全量同步。


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
    It MUST NOT synchronize packaged skills: ``workspace/skills`` is runtime
    state and may contain files created or edited by the agent/user.

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


# ========== 用户 skills/ 目录的 R2 快照持久化 ==========
# R2 只保存一个压缩快照：skills/{namespace}/skills.tar.gz。
# 不再逐文件上传、删除，也不维护 sha256 manifest。workspace/skills 是
# 唯一的运行时目录；发生变化后重新打包整个目录并覆盖 R2 快照。
_SKILLS_R2_PREFIX = "skills"
_SKILLS_ARCHIVE_NAME = "skills.tar.gz"


def _skills_r2_prefix(namespace: str) -> str:
    return f"{_SKILLS_R2_PREFIX}/{namespace}"


def _skills_archive_key(namespace: str) -> str:
    return f"{_skills_r2_prefix(namespace)}/{_SKILLS_ARCHIVE_NAME}"


def _is_safe_skill_relpath(rel: str) -> bool:
    """远端归档内的相对路径必须落在 skills/ 内部。"""
    if not rel or rel.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in Path(rel).parts)


def _pack_skills_dir(skills_dir: Path) -> bytes:
    """把 skills/ 打成 gzip tar；只收录普通文件，避免符号链接逃逸。"""
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        if skills_dir.is_dir():
            for path in sorted(skills_dir.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(skills_dir).as_posix()
                if not _is_safe_skill_relpath(rel):
                    continue
                archive.add(path, arcname=rel, recursive=False)
    return buffer.getvalue()


def _extract_skills_archive(data: bytes, skills_dir: Path, *, replace_existing: bool = True) -> int:
    """安全解压 skills 快照，拒绝绝对路径/.. 路径和符号链接条目。"""
    import io
    import shutil
    import tarfile

    skills_dir.mkdir(parents=True, exist_ok=True)
    if replace_existing:
        for child in skills_dir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    restored = 0
    root = skills_dir.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = member.name.replace("\\", "/")
            if not _is_safe_skill_relpath(name):
                raise ValueError(f"unsafe skills archive path: {member.name!r}")
            if member.isdir():
                (skills_dir / name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                # 不恢复 symlink / hardlink / device 等特殊条目。
                continue
            dst = (skills_dir / name).resolve()
            if root != dst and root not in dst.parents:
                raise ValueError(f"skills archive path escapes workspace: {member.name!r}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            src = archive.extractfile(member)
            if src is None:
                continue
            with src, dst.open("wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            try:
                os.chmod(dst, member.mode & 0o777)
            except OSError:
                pass
            restored += 1
    return restored


async def _restore_skills_snapshot_from_r2(home: Path, namespace: str) -> bool:
    """R2 有快照则下载并恢复；返回是否命中快照。"""
    if not is_r2_configured():
        return False
    data = await download_from_r2(_skills_archive_key(namespace))
    if data is None:
        return False
    restored = await asyncio.to_thread(
        _extract_skills_archive, data, home / "skills", replace_existing=True,
    )
    logger.info(
        "从 R2 恢复 skills 快照 namespace=%s: %d 个文件", namespace, restored,
    )
    return True


async def _backup_user_skills_to_r2(home: Path, namespace: str) -> None:
    """重新打包整个 skills/ 并覆盖 R2 快照。"""
    if not is_r2_configured():
        return
    data = await asyncio.to_thread(_pack_skills_dir, home / "skills")
    archive_key = _skills_archive_key(namespace)
    await upload_bytes_to_r2(data, archive_key, "application/gzip")

    # One-time/ongoing cleanup keeps the new contract strict: R2 contains only
    # the compressed snapshot for this namespace. This also removes objects
    # left behind by the previous per-file + manifest implementation.
    try:
        legacy_keys = await list_r2_objects(_skills_r2_prefix(namespace))
        for key in legacy_keys:
            if key != archive_key:
                await delete_r2_object(key)
    except Exception:
        logger.warning("清理旧 skills R2 对象失败 namespace=%s", namespace, exc_info=True)

    logger.info(
        "用户 skills 快照已上传 R2 namespace=%s: %.1f KiB",
        namespace, len(data) / 1024,
    )


async def sync_workspace_skills_r2(chat_id: int, home: Path, namespace: str) -> str:
    """初始化 workspace skills：优先恢复 R2 快照，否则从项目 skills 初始化并上传。

    返回 ``restored`` / ``bootstrapped`` / ``disabled``，供启动扫描和测试使用。
    """
    if not is_r2_configured():
        return "disabled"
    archive_key = _skills_archive_key(namespace)
    existing = await download_from_r2(archive_key)
    if existing is not None:
        await asyncio.to_thread(
            _extract_skills_archive, existing, home / "skills", replace_existing=True,
        )
        logger.info("skills R2 快照恢复完成 namespace=%s", namespace)
        return "restored"

    from skills import sync_all_skill_assets_to_workspace

    summary = await asyncio.to_thread(sync_all_skill_assets_to_workspace, home)
    if summary.get("errors"):
        raise RuntimeError("; ".join(summary["errors"]))
    await _backup_user_skills_to_r2(home, namespace)
    logger.info("R2 无 skills 快照，已从项目 skills 初始化并上传 namespace=%s", namespace)
    return "bootstrapped"


def _skills_tree_fingerprint(skills_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """仅用路径/大小/mtime_ns 检测变化，不读取内容、不计算 sha256。"""
    if not skills_dir.is_dir():
        return ()
    items: list[tuple[str, int, int]] = []
    for path in sorted(skills_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append((path.relative_to(skills_dir).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(items)


async def _ensure_workspace_initialized(chat_id: int, namespace: str | None = None) -> None:
    """初始化 workspace skills，并保留一个轻量的首次同步标记。"""
    resolved_namespace = workspace_namespace(chat_id, namespace)
    key = resolved_namespace
    lock = await _get_workspace_init_lock(key)
    async with lock:
        home = agent_home(chat_id, resolved_namespace)
        marker = home / ".skills_initialized"

        if key not in _workspace_initialized and not marker.is_file():
            try:
                if is_r2_configured():
                    await sync_workspace_skills_r2(chat_id, home, resolved_namespace)
                else:
                    from skills import sync_all_skill_assets_to_workspace
                    summary = await asyncio.to_thread(sync_all_skill_assets_to_workspace, home)
                    if summary.get("errors"):
                        raise RuntimeError("; ".join(summary["errors"]))
                marker.write_text("initialized\n", encoding="utf-8")
                _workspace_initialized.add(key)
            except Exception as exc:
                logger.warning(
                    "初始化 skill 包到 workspace 失败 namespace=%s: %s",
                    resolved_namespace, exc,
                )
                return
        else:
            _workspace_initialized.add(key)


async def sync_all_existing_workspace_skills_r2() -> dict[str, object]:
    """启动时为所有已有 workspace 恢复/播种 skills R2 快照。"""
    results: dict[str, object] = {"workspaces": 0, "restored": 0, "bootstrapped": 0, "errors": []}
    if not is_r2_configured():
        return results
    for home in sorted(_workspace_namespace_dirs_for_skills()):
        try:
            namespace = home.name
            # 只有 namespace 目录才会进入这里；恢复逻辑本身负责创建 skills/。
            status = await sync_workspace_skills_r2(0, home, namespace)
            results["workspaces"] = int(results["workspaces"]) + 1
            if status == "restored":
                results["restored"] = int(results["restored"]) + 1
            elif status == "bootstrapped":
                results["bootstrapped"] = int(results["bootstrapped"]) + 1
        except Exception as exc:
            cast = results["errors"]
            assert isinstance(cast, list)
            cast.append(f"{home}: {exc}")
    return results


def _workspace_namespace_dirs_for_skills() -> list[Path]:
    """返回 workspace 根下的 namespace 目录，避免依赖 chat_id 反解。"""
    try:
        from workspace_paths import workspaces_root
        root = workspaces_root()
    except Exception:
        return []
    if not root.is_dir():
        return []
    return [p for p in sorted(root.iterdir()) if p.is_dir() and not p.is_symlink()]


async def watch_workspace_skills_r2(stop_event: asyncio.Event, interval: float = 2.0) -> None:
    """轮询已有 workspace 的 skills/，变化后 debounce 并覆盖上传 R2 快照。"""
    fingerprints: dict[str, tuple[tuple[str, int, int], ...]] = {}
    pending: set[str] = set()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.5, interval))
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        if not is_r2_configured():
            continue
        for home in _workspace_namespace_dirs_for_skills():
            namespace = home.name
            skills_dir = home / "skills"
            current = _skills_tree_fingerprint(skills_dir)
            previous = fingerprints.get(namespace)
            fingerprints[namespace] = current
            if previous is None:
                continue
            if current == previous:
                continue
            pending.add(namespace)

        # 同一轮发现的多个 workspace 各自只上传一次。
        for namespace in sorted(pending):
            home = next((p for p in _workspace_namespace_dirs_for_skills() if p.name == namespace), None)
            if home is None:
                continue
            try:
                await _backup_user_skills_to_r2(home, namespace)
                fingerprints[namespace] = _skills_tree_fingerprint(home / "skills")
            except Exception:
                logger.warning("workspace skills R2 自动同步失败 namespace=%s", namespace, exc_info=True)
        pending.clear()


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


# 后台 init_workspace 任务强引用集：事件循环只持有任务的弱引用，若调用方
# 不保存返回值，任务可能在执行中途被 GC 回收（CPython asyncio 官方文档
# 明确警告的坑），表现为 workspace 预初始化静默消失、首个工具调用退化为
# 同步 no-op 初始化。此集合保证任务存活到自然结束。
_workspace_init_tasks: set = set()


def schedule_workspace_init(chat_id: int, namespace: str | None = None) -> asyncio.Task:
    """后台调度 init_workspace 并保留强引用（fire-and-forget 的安全封装）。

    所有"预初始化工作区"的调用点都应使用本函数而非裸
    ``asyncio.create_task(init_workspace(chat_id))``。
    """
    task = asyncio.create_task(init_workspace(chat_id, namespace))
    _workspace_init_tasks.add(task)
    task.add_done_callback(_workspace_init_tasks.discard)
    return task
