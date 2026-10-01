"""R2 persistence for runtime workspace skills.

Ordinary workspace files are local-only. This module owns the explicit
``workspace/skills`` snapshot lifecycle.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from s3_utils import (
    upload_bytes_to_r2, download_from_r2, delete_r2_object,
    list_r2_objects, is_r2_configured,
)
from workspace_paths import workspaces_root

logger = logging.getLogger(__name__)

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


async def backup_user_skills_to_r2(home: Path, namespace: str) -> None:
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


async def initialize_workspace_skills(home: Path, namespace: str) -> str:
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
    await backup_user_skills_to_r2(home, namespace)
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


async def sync_all_existing_workspace_skills_r2() -> dict[str, object]:
    """启动时为所有已有 workspace 恢复/播种 skills R2 快照。"""
    results: dict[str, object] = {"workspaces": 0, "restored": 0, "bootstrapped": 0, "errors": []}
    if not is_r2_configured():
        return results
    for home in sorted(workspace_namespace_dirs_for_skills()):
        try:
            namespace = home.name
            # 只有 namespace 目录才会进入这里；恢复逻辑本身负责创建 skills/。
            status = await initialize_workspace_skills(home, namespace)
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


def workspace_namespace_dirs_for_skills() -> list[Path]:
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
        for home in workspace_namespace_dirs_for_skills():
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
            home = next((p for p in workspace_namespace_dirs_for_skills() if p.name == namespace), None)
            if home is None:
                continue
            try:
                await backup_user_skills_to_r2(home, namespace)
                fingerprints[namespace] = _skills_tree_fingerprint(home / "skills")
            except Exception:
                logger.warning("workspace skills R2 自动同步失败 namespace=%s", namespace, exc_info=True)
        pending.clear()


