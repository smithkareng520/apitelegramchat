from __future__ import annotations

import json
import logging
import os
import re
import shutil
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path

from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Skill 资源层位于 workspace/skills，与用户文件 workspace/files 完全分离。
# R2 同步只遍历 workspace/files，因此 skill 资源天然不会被同步或删除。
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

        if line.startswith("  - ") or line.startswith("- "):
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
        roots.append(Path(__file__).resolve().parents[2] / ".claude" / "skills")
    except Exception:
        pass

    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except Exception:
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


def refresh_skill_cache() -> None:
    """清空 skill 目录缓存；在运行时新增/删除 skill 后可调用。"""
    _cached_skill_catalog_text.cache_clear()


def skill_catalog_brief() -> str:
    """给系统提示用的精简技能目录。"""
    return _cached_skill_catalog_text()


def build_skill_system_message(skill_id: str, *, include_body: bool = True) -> dict[str, Any]:
    data = read_skill(skill_id)
    if "error" in data:
        return {"error": data["error"]}

    skill = data["skill"]
    frontmatter = skill.get("frontmatter") or {}
    assets_relpath = skill_assets_workspace_relpath(skill.get("skill_id", skill_id))
    header_lines = [
        f"Active skill: {skill.get('skill_id')}",
        f"Name: {skill.get('name')}",
        f"Description: {skill.get('description')}",
        f"Skill assets path (in this workspace): {assets_relpath}/",
    ]
    if frontmatter.get("allowed_tools"):
        header_lines.append("Allowed tools: " + ", ".join(map(str, frontmatter.get("allowed_tools", []))))
    if frontmatter.get("priority"):
        header_lines.append(f"Priority: {frontmatter.get('priority')}")
    body = data.get("body", "") if include_body else ""
    content = "\n".join(header_lines)
    if body:
        content += "\n\nInstructions:\n" + body
        content += (
            "\n\nIMPORTANT — explicit skill usage:\n"
            f"1. The complete skill package is available at `{assets_relpath}/`. "
            "Read its `SKILL.md` first, then follow the instructions only when you "
            "have explicitly decided this skill is useful for the current task.\n"
            f"2. bash starts in the workspace execution directory. To run commands from "
            f"this skill, use `cd ../{assets_relpath}` first, or invoke scripts with an "
            f"explicit path such as `python ../{assets_relpath}/scripts/example.py`. "
            "After changing directory, the persistent bash session keeps that cwd. "
            "User files remain in the workspace and can be reached with `../` as usual.\n"
            f"3. text_editor paths are resolved from the workspace root, so use "
            f"`{assets_relpath}/...` for skill assets."
        )
    return {
        "role": "system",
        "name": f"skill:{skill.get('skill_id')}",
        "content": content,
        "skill": skill,
    }


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


def _iter_skill_package_files(skill_dir: Path) -> Iterable[Path]:
    """Yield every file in a trusted packaged skill, including SKILL.md."""
    for path in skill_dir.rglob("*"):
        if path.is_file():
            yield path


def skill_assets_workspace_relpath(skill_id: str) -> str:
    """该 skill 在 workspace 内的相对路径。"""
    return f"{SKILL_ASSETS_DIRNAME}/{skill_id}"


def sync_skill_assets_to_workspace(skill_id: str, workspace_root: Path) -> dict[str, Any]:
    """同步一个完整 skill 包到 workspace/skills/<skill_id>/。"""
    result: dict[str, Any] = {"skill_id": skill_id, "synced": False, "files": 0, "error": None}
    rec = next((r for r in load_skill_records() if r.skill_id == skill_id or r.name == skill_id), None)
    if rec is None:
        result["error"] = f"Unknown skill: {skill_id}"
        return result

    skill_dir = Path(rec.root) / rec.skill_id
    if not skill_dir.is_dir():
        result["error"] = f"Skill directory missing: {skill_dir}"
        return result

    dest_root = Path(workspace_root) / SKILL_ASSETS_DIRNAME / rec.skill_id
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        copied = 0
        source_files: set[str] = set()
        for src_path in _iter_skill_package_files(skill_dir):
            rel = src_path.relative_to(skill_dir)
            source_files.add(rel.as_posix())
            dst_path = dest_root / rel
            needs_copy = True
            if dst_path.exists():
                try:
                    src_stat = src_path.stat()
                    dst_stat = dst_path.stat()
                    needs_copy = (
                        src_stat.st_size != dst_stat.st_size
                        or int(src_stat.st_mtime) > int(dst_stat.st_mtime)
                    )
                except OSError:
                    needs_copy = True
            if needs_copy:
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst_path)
                copied += 1

        for existing in list(dest_root.rglob("*")):
            if existing.is_dir():
                continue
            rel = existing.relative_to(dest_root).as_posix()
            if rel not in source_files:
                try:
                    existing.unlink()
                except OSError:
                    pass

        for existing_dir in sorted((p for p in dest_root.rglob("*") if p.is_dir()), reverse=True):
            try:
                if not any(existing_dir.iterdir()):
                    existing_dir.rmdir()
            except OSError:
                pass

        result.update({"synced": True, "files": len(source_files), "copied": copied, "path": str(dest_root)})
    except Exception as exc:
        logger.error("同步 skill 资源失败 skill_id=%s: %s", skill_id, exc)
        result["error"] = str(exc)
    return result


def sync_all_skill_assets_to_workspace(workspace_root: Path) -> dict[str, Any]:
    """把所有可信 skill 包同步到 workspace/skills，供 AI 自主选择并使用。"""
    records = load_skill_records()
    summary = {"synced": 0, "files": 0, "errors": []}
    for rec in records:
        result = sync_skill_assets_to_workspace(rec.skill_id, workspace_root)
        if result.get("synced"):
            summary["synced"] += 1
            summary["files"] += int(result.get("files") or 0)
        elif result.get("error"):
            summary["errors"].append(f"{rec.skill_id}: {result['error']}")
    return summary


def catalog_text() -> str:
    """生成系统提示词用的 skill 目录，每行格式：name - description。"""
    records = load_skill_records()
    lines = []
    for rec in records:
        desc = rec.description.strip() if rec.description else "(no description)"
        lines.append(f"{rec.name} - {desc}")
    return "\n".join(lines)


def read_skill_text(skill_id: str) -> str:
    data = read_skill(skill_id)
    if "error" in data:
        return data["error"]
    payload = {
        "skill": data["skill"],
        "body": data["body"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
