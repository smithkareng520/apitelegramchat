from __future__ import annotations

import json
import logging
import os
import re
import shutil
from functools import lru_cache
from dataclasses import dataclass
import asyncio
import hashlib
import json
from pathlib import Path

from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Skill 资源层位于 workspace/skills。项目内 .claude/skills 在部署时生成一个
# skills bundle ZIP 并写入 R2；workspace/skills 只是该 bundle 的运行时副本。
# R2 的 skills 域不保存逐文件副本。
SKILL_ASSETS_DIRNAME = "skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if not raw:
        return ""
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        try:
            return int(raw)
        except Exception:
            logger.debug("_parse_scalar 内部忽略的异常", exc_info=True)
            return raw
    lower = raw.lower()
    if lower in {"true", "yes"}:
        return True
    if lower in {"false", "no"}:
        return False
    return raw


def _parse_frontmatter_lines(lines: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[Any] | None = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            continue

        if line.startswith(("  - ", "- ")):
            if current_key is not None and current_list is not None:
                current_list.append(_parse_scalar(line.split("-", 1)[1].strip()))
            continue

        if ":" in line:
            key, value = line.split(":", 1)
            key = _normalize_key(key)
            value = value.strip()

            if not value:
                current_key = key
                current_list = []
                meta[key] = current_list
                continue

            current_key = key
            current_list = None
            if value.startswith("[") and value.endswith("]"):
                items = [item.strip() for item in value[1:-1].split(",") if item.strip()]
                meta[key] = [_parse_scalar(item) for item in items]
            else:
                meta[key] = _parse_scalar(value)
            continue

        # ignore malformed lines; keep parser tolerant

    return meta


def _read_skill_header(skill_md: Path) -> dict[str, Any]:
    try:
        with skill_md.open("r", encoding="utf-8") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return {}
            header_lines: list[str] = []
            for line in fh:
                if line.strip() == "---":
                    break
                header_lines.append(line.rstrip("\n"))
            return _parse_frontmatter_lines(header_lines)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("_read_skill_header 内部忽略的异常", exc_info=True)
        return {}


def _candidate_skill_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.getenv("APITELEGRAMCHAT_SKILLS_DIR", "").strip()
    if env:
        for chunk in env.split(os.pathsep):
            if chunk:
                roots.append(Path(chunk).expanduser())
    roots.append(Path.cwd() / ".claude" / "skills")
    try:
        roots.append(Path(__file__).resolve().parents[1] / ".claude" / "skills")
    except Exception:
        logger.debug("_candidate_skill_roots 内部忽略的异常", exc_info=True)
        pass

    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except Exception:
            logger.debug("_candidate_skill_roots 内部忽略的异常", exc_info=True)
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def discover_skill_roots() -> list[Path]:
    return [root for root in _candidate_skill_roots() if root.exists() and root.is_dir()]


def _iter_skill_files() -> Iterable[tuple[Path, Path]]:
    for root in discover_skill_roots():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                yield root, skill_md


@dataclass(frozen=True)
class SkillRecord:
    skill_id: str
    name: str
    description: str
    path: str
    root: str
    priority: int
    effort: str | None
    allowed_tools: list[str]

    def to_catalog_item(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "priority": self.priority,
            "effort": self.effort,
            "allowed_tools": self.allowed_tools,
        }


def load_skill_records() -> list[SkillRecord]:
    records: list[SkillRecord] = []
    seen_skill_ids: set[str] = set()
    for root, skill_md in _iter_skill_files():
        skill_id = skill_md.parent.name
        # 多个 skill root 里出现同名目录时，只取先发现的一份（roots 按优先级排列），
        # 避免同一个 skill 在目录/系统提示里重复出现。
        if skill_id in seen_skill_ids:
            continue
        seen_skill_ids.add(skill_id)
        meta = _read_skill_header(skill_md)
        name = str(meta.get("name") or skill_id)
        description = str(meta.get("description") or "").strip()
        priority_raw = meta.get("priority") or 0
        priority = int(priority_raw) if str(priority_raw).lstrip("-").isdigit() else 0
        effort = meta.get("effort")
        if effort is not None:
            effort = str(effort)
        allowed_tools_raw = meta.get("allowed_tools") or meta.get("allowed-tools") or []
        allowed_tools: list[str] = []
        if isinstance(allowed_tools_raw, list):
            allowed_tools = [str(item) for item in allowed_tools_raw if str(item).strip()]
        elif allowed_tools_raw:
            allowed_tools = [str(allowed_tools_raw)]
        records.append(
            SkillRecord(
                skill_id=skill_id,
                name=name,
                description=description,
                path=str(skill_md.relative_to(root)),
                root=str(root),
                priority=priority,
                effort=effort,
                allowed_tools=allowed_tools,
            )
        )

    records.sort(key=lambda item: (-item.priority, item.name.lower(), item.skill_id.lower()))
    return records


def _read_full_skill(skill_path: Path) -> tuple[dict[str, Any], str]:
    text = skill_path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text.replace("\r\n", "\n"))
    if not match:
        return {}, text.strip()
    header, body = match.group(1), match.group(2).strip()
    return _parse_frontmatter_lines(header.splitlines()), body



@lru_cache(maxsize=1)
def _cached_skill_catalog_text() -> str:
    return catalog_text()


def skill_catalog_brief() -> str:
    """给系统提示用的精简技能目录。"""
    return _cached_skill_catalog_text()


def get_skill_catalog() -> dict[str, Any]:
    records = load_skill_records()
    featured = next((rec.skill_id for rec in records if rec.priority > 0), None)
    return {
        "roots": [str(root) for root in discover_skill_roots()],
        "count": len(records),
        "featured": featured,
        "skills": [rec.to_catalog_item() for rec in records],
    }


def read_skill(skill_id: str) -> dict[str, Any]:
    skill_id = str(skill_id or "").strip()
    if not skill_id:
        return {"error": "Missing skill_id"}
    for rec in load_skill_records():
        if rec.skill_id == skill_id or rec.name == skill_id:
            skill_path = Path(rec.root) / rec.skill_id / "SKILL.md"
            meta, body = _read_full_skill(skill_path)
            meta.setdefault("name", rec.name)
            meta.setdefault("description", rec.description)
            meta.setdefault("priority", rec.priority)
            meta.setdefault("effort", rec.effort)
            meta.setdefault("allowed_tools", rec.allowed_tools)
            return {
                "skill": {
                    **rec.to_catalog_item(),
                    "frontmatter": meta,
                },
                "body": body,
            }
    return {"error": f"Unknown skill: {skill_id}"}


def _project_skill_source_root() -> Path | None:
    """Return the project ``.claude/skills`` source tree used to build a bundle."""
    for root in _candidate_skill_roots():
        try:
            root = root.resolve()
        except Exception:
            logger.debug("_project_skill_source_root 内部忽略的异常", exc_info=True)
            continue
        if root.is_dir():
            return root
    return None


def _iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            yield path


_PACKAGED_MANIFEST_NAME = ".packaged-manifest.json"
_SKILLS_BUNDLE_KEY = os.getenv("APITELEGRAMCHAT_SKILLS_BUNDLE_KEY", "skills/skills.bundle.zip").strip().strip("/") or "skills/skills.bundle.zip"
_SYNC_INTERVAL_SECONDS = max(2, float(os.getenv("SKILLS_AUTO_SYNC_INTERVAL_SECONDS", "5")))
_sync_watcher_task: asyncio.Task[None] | None = None
_sync_watcher_stop: asyncio.Event | None = None
_bundle_lock = asyncio.Lock()
_bundle_source_root: Path | None = None
_bundle_source_hash: str = ""


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_packaged_manifest(dest_root: Path) -> dict[str, str]:
    path = dest_root / _PACKAGED_MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        files = payload.get("files")
        if isinstance(files, dict):
            return {str(k): str(v) for k, v in files.items() if _is_safe_relpath(str(k))}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass
    return {}


def _is_safe_relpath(rel: str) -> bool:
    if not rel or rel.startswith("/"):
        return False
    return all(part not in {"", ".", ".."} for part in Path(rel).parts)


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}-{id(src)}")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _source_snapshot(root: Path) -> tuple[str, dict[str, str]]:
    """Return a deterministic content hash and per-file hashes for the source tree."""
    files: dict[str, str] = {}
    for path in sorted(_iter_files(root), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if not _is_safe_relpath(rel):
            continue
        files[rel] = _file_sha256(path)
    digest_input = "\n".join(f"{rel}\0{digest}" for rel, digest in files.items()).encode()
    return hashlib.sha256(digest_input).hexdigest(), files


def _bundle_cache_paths() -> tuple[Path, Path]:
    from workspace_paths import data_root
    root = data_root() / "skills_bundle"
    root.mkdir(parents=True, exist_ok=True)
    return root / "skills.bundle.zip", root / "extracted"


def _build_bundle_bytes(source_root: Path, source_hash: str, files: dict[str, str]) -> bytes:
    import io
    import zipfile

    manifest = {
        "format": 1,
        "source_sha256": source_hash,
        "files": files,
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        info = zipfile.ZipInfo(".bundle-manifest.json")
        info.date_time = (1980, 1, 1, 0, 0, 0)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, (source_root / rel).read_bytes())
    return out.getvalue()


def _extract_bundle(bundle_bytes: bytes, extracted_root: Path, expected_hash: str) -> Path:
    import io
    import zipfile

    tmp = extracted_root.with_name(f"{extracted_root.name}.tmp-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
            raw = zf.read(".bundle-manifest.json")
            manifest = json.loads(raw.decode("utf-8"))
            if manifest.get("source_sha256") != expected_hash:
                raise ValueError("skills bundle source hash mismatch")
            names = zf.namelist()
            for name in names:
                if name == ".bundle-manifest.json":
                    continue
                if not _is_safe_relpath(name):
                    raise ValueError(f"unsafe skills bundle path: {name!r}")
                target = tmp / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        shutil.rmtree(extracted_root, ignore_errors=True)
        os.replace(tmp, extracted_root)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return extracted_root


async def _ensure_packaged_skill_bundle() -> Path | None:
    """Build/upload the project skills bundle when needed, then extract it once.

    R2 intentionally stores exactly one skills object: ``skills/skills.bundle.zip``.
    The ZIP contains its own manifest, including the source content hash. A HEAD
    metadata check avoids downloading/uploading an unchanged bundle. The extracted
    directory is process-local cache shared by every workspace.
    """
    global _bundle_source_root, _bundle_source_hash
    async with _bundle_lock:
        source_root = _project_skill_source_root()
        if source_root is None:
            return None
        source_hash, files = await asyncio.to_thread(_source_snapshot, source_root)
        zip_path, extracted_root = _bundle_cache_paths()

        from s3_utils import (
            get_r2_object_metadata, download_from_r2, is_r2_configured,
            upload_bytes_to_r2, list_r2_objects, delete_r2_object,
        )

        remote_meta = await get_r2_object_metadata(_SKILLS_BUNDLE_KEY)
        remote_hash = (remote_meta or {}).get("skills-sha256", "")
        bundle_bytes: bytes | None = None

        if remote_hash == source_hash:
            if zip_path.is_file():
                try:
                    bundle_bytes = await asyncio.to_thread(zip_path.read_bytes)
                except OSError:
                    bundle_bytes = None
            if bundle_bytes is None:
                bundle_bytes = await download_from_r2(_SKILLS_BUNDLE_KEY)
        else:
            bundle_bytes = await asyncio.to_thread(_build_bundle_bytes, source_root, source_hash, files)
            if is_r2_configured() or bundle_bytes is not None:
                await upload_bytes_to_r2(
                    bundle_bytes,
                    _SKILLS_BUNDLE_KEY,
                    "application/zip",
                    metadata={"skills-sha256": source_hash, "skills-format": "1"},
                )

        if bundle_bytes is None:
            # R2 may be unavailable. A local build is still sufficient for the
            # current process and preserves the deployment's local behavior.
            bundle_bytes = await asyncio.to_thread(_build_bundle_bytes, source_root, source_hash, files)

        # One-time compatibility cleanup: older releases stored individual skill
        # files below skills/<namespace>/. Keep the new R2 namespace strictly to
        # the single bundle object. This is intentionally best-effort.
        if is_r2_configured() and not remote_meta:
            try:
                old_keys = await list_r2_objects("skills")
                for key in old_keys:
                    if key != _SKILLS_BUNDLE_KEY:
                        await delete_r2_object(key)
            except Exception:
                logger.warning("清理旧的逐文件 Skills R2 对象失败", exc_info=True)

        await asyncio.to_thread(zip_path.write_bytes, bundle_bytes)
        await asyncio.to_thread(_extract_bundle, bundle_bytes, extracted_root, source_hash)
        _bundle_source_root = extracted_root
        _bundle_source_hash = source_hash
        return extracted_root


def _bundle_or_project_source_root() -> Path | None:
    return _bundle_source_root or _project_skill_source_root()


def sync_all_skill_assets_to_workspace(workspace_root: Path) -> dict[str, Any]:
    """Synchronize the unpacked packaged bundle into a runtime workspace safely."""
    source_root = _bundle_or_project_source_root()
    dest_root = Path(workspace_root) / SKILL_ASSETS_DIRNAME
    summary: dict[str, Any] = {
        "synced": 0, "files": 0, "copied": 0, "updated": 0,
        "preserved_user_edits": 0, "errors": [],
        "source": str(source_root) if source_root else None,
        "path": str(dest_root),
    }
    if source_root is None:
        summary["errors"].append("No packaged skill bundle/source found")
        return summary

    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        previous = _load_packaged_manifest(dest_root)
        current: dict[str, str] = {}
        source_files = list(_iter_files(source_root))
        for src_path in source_files:
            rel = src_path.relative_to(source_root)
            rel_key = rel.as_posix()
            if not _is_safe_relpath(rel_key):
                continue
            src_hash = _file_sha256(src_path)
            current[rel_key] = src_hash
            dst = dest_root / rel
            if not dst.exists():
                _atomic_copy(src_path, dst)
                summary["copied"] += 1
                continue
            previous_hash = previous.get(rel_key)
            try:
                dst_hash = _file_sha256(dst)
            except OSError as exc:
                summary["errors"].append(f"{rel_key}: {exc}")
                continue
            if dst_hash == src_hash:
                continue
            if previous_hash and dst_hash == previous_hash:
                _atomic_copy(src_path, dst)
                summary["updated"] += 1
            else:
                summary["preserved_user_edits"] += 1

        manifest_payload = json.dumps({"version": 1, "bundle_sha256": _bundle_source_hash, "files": current}, ensure_ascii=False, indent=2) + "\n"
        manifest_path = dest_root / _PACKAGED_MANIFEST_NAME
        tmp = manifest_path.with_name(f".{manifest_path.name}.tmp-{os.getpid()}")
        tmp.write_text(manifest_payload, encoding="utf-8")
        os.replace(tmp, manifest_path)
        summary.update({
            "synced": len({p.name for p in source_root.iterdir() if p.is_dir()}),
            "files": len(current),
        })
    except Exception as exc:
        logger.error("同步项目 skills 到 workspace 失败: %s", exc)
        summary["errors"].append(str(exc))
    return summary


def sync_packaged_skills_for_existing_workspaces() -> dict[str, Any]:
    """Refresh packaged skills for every existing workspace from the unpacked bundle."""
    results: dict[str, Any] = {"workspaces": 0, "copied": 0, "updated": 0, "preserved": 0, "errors": []}
    for home in _workspace_namespace_dirs():
        try:
            result = sync_all_skill_assets_to_workspace(home)
            results["workspaces"] += 1
            results["copied"] += int(result.get("copied", 0))
            results["updated"] += int(result.get("updated", 0))
            results["preserved"] += int(result.get("preserved_user_edits", 0))
            results["errors"].extend(result.get("errors", []))
        except Exception as exc:
            results["errors"].append(f"{home}: {exc}")
    return results


def _project_source_fingerprint() -> str:
    root = _project_skill_source_root()
    if root is None:
        return ""
    # Fast watcher path: metadata only. The expensive content hash is computed
    # only after this fingerprint changes.
    items: list[str] = []
    for path in _iter_files(root):
        try:
            stat = path.stat()
            items.append(f"{path.relative_to(root).as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}")
        except OSError:
            continue
    return hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()


async def start_packaged_skill_auto_sync() -> None:
    """Build/upload the bundle once, then watch the project source for changes."""
    global _sync_watcher_task, _sync_watcher_stop
    if _sync_watcher_task and not _sync_watcher_task.done():
        return

    await _ensure_packaged_skill_bundle()
    initial = await asyncio.to_thread(sync_packaged_skills_for_existing_workspaces)
    logger.info(
        "startup packaged skills refresh: workspaces=%s copied=%s updated=%s preserved=%s errors=%s",
        initial["workspaces"], initial["copied"], initial["updated"],
        initial["preserved"], len(initial["errors"]),
    )

    stop_event = asyncio.Event()
    _sync_watcher_stop = stop_event
    source_fingerprint = _project_source_fingerprint()

    async def _watch() -> None:
        last = source_fingerprint
        try:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=_SYNC_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break
                fingerprint = _project_source_fingerprint()
                if fingerprint == last:
                    continue
                await _ensure_packaged_skill_bundle()
                result = await asyncio.to_thread(sync_packaged_skills_for_existing_workspaces)
                logger.info(
                    "packaged skills auto-refresh: workspaces=%s copied=%s updated=%s preserved=%s errors=%s",
                    result["workspaces"], result["copied"], result["updated"],
                    result["preserved"], len(result["errors"]),
                )
                last = fingerprint
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("packaged skill auto-sync stopped unexpectedly", exc_info=True)

    _sync_watcher_task = asyncio.create_task(_watch(), name="packaged-skill-auto-sync")


async def stop_packaged_skill_auto_sync() -> None:
    global _sync_watcher_task, _sync_watcher_stop
    stop_event = _sync_watcher_stop
    task = _sync_watcher_task
    if stop_event is not None:
        stop_event.set()
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _sync_watcher_task = None
    _sync_watcher_stop = None
