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

# workspace 内用于存放已激活 skill 资源的子目录名。
# 必须与 workspace_utils.py 中 R2 全量同步的排除规则保持一致，
# 否则同步逻辑会把这里的文件当成"远程没有的多余文件"删掉。
SKILL_ASSETS_DIRNAME = ".skills"

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


def _skill_text_for_matching(rec: SkillRecord) -> str:
    parts = [rec.skill_id, rec.name, rec.description, rec.path]
    return " ".join(part for part in parts if part).lower()


def _normalize_request_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _tokenize_request_text(text: str) -> set[str]:
    normalized = _normalize_request_text(text)
    if not normalized:
        return set()
    pieces = set(re.findall(r"[\w.-]+|[\u4e00-\u9fff]+", normalized))
    for phrase in ("word 文档", "word文档", ".docx", ".pdf", "前端设计", "界面设计", "生成图片"):
        if phrase in normalized:
            pieces.add(phrase)
    return pieces


def _score_skill_match(skill: SkillRecord, request_text: str) -> tuple[int, str]:
    text = _normalize_request_text(request_text)
    if not text:
        return 0, ""

    haystack = _skill_text_for_matching(skill)
    score = 0
    reasons: list[str] = []

    exact_hits = {
        "pdf": [".pdf", "pdf", "ocr", "扫描", "合并", "拆分", "水印", "表单", "填表", "抽取文本", "提取文本"],
        "docx": [".docx", "docx", "word", "word文档", "word 文档", "报告", "备忘录", "信函", "模板", "批注", "修订"],
        "frontend-design": [
            "frontend", "前端", "ui", "界面", "页面", "布局", "tailwind", "react", "组件", "设计",
            "登录页", "落地页", "仪表盘", "dashboard", "responsive", "样式", "配色", "排版", "动效",
        ],
    }

    for token in exact_hits.get(skill.skill_id, []):
        if token in text:
            score += 5
            reasons.append(token)

    tokens = _tokenize_request_text(text)
    skill_tokens = set(re.findall(r"[\w.-]+|[\u4e00-\u9fff]+", haystack))
    overlap = tokens & skill_tokens
    if overlap:
        score += min(6, len(overlap) * 2)
        reasons.extend(sorted(overlap)[:3])

    if skill.description:
        score += 1 if len(skill.description) < 180 else 0

    return score, ", ".join(dict.fromkeys(reasons))


@lru_cache(maxsize=1)
def _cached_skill_catalog_text() -> str:
    return catalog_text()


def refresh_skill_cache() -> None:
    """清空 skill 目录缓存；在运行时新增/删除 skill 后可调用。"""
    _cached_skill_catalog_text.cache_clear()


def skill_catalog_brief() -> str:
    """给系统提示用的精简技能目录。"""
    return _cached_skill_catalog_text()


def match_skill_for_text(
    request_text: str,
    *,
    current_skill_id: str | None = None,
    minimum_score: int = 5,
) -> dict[str, Any] | None:
    """根据用户请求自动匹配最合适的 skill。"""
    records = load_skill_records()
    if not records:
        return None

    text = _normalize_request_text(request_text)
    if not text:
        return None

    if any(phrase in text for phrase in ("取消技能", "关闭技能", "不使用技能", "不需要技能", "clear skill", "disable skill")):
        return {"skill_id": None, "reason": "用户明确要求取消技能", "score": 0}

    best: SkillRecord | None = None
    best_score = -1
    best_reason = ""
    for rec in records:
        score, reason = _score_skill_match(rec, text)
        if score > best_score:
            best = rec
            best_score = score
            best_reason = reason

    if best is None or best_score < minimum_score:
        if current_skill_id:
            for rec in records:
                if rec.skill_id == current_skill_id:
                    score, reason = _score_skill_match(rec, text)
                    if score >= max(2, minimum_score - 2):
                        return {
                            "skill_id": rec.skill_id,
                            "reason": reason or "沿用当前 skill",
                            "score": score,
                        }
        return None

    if current_skill_id and best.skill_id != current_skill_id:
        current_rec = next((r for r in records if r.skill_id == current_skill_id), None)
        if current_rec is not None:
            current_score, _ = _score_skill_match(current_rec, text)
            if best_score - current_score < 3:
                return {
                    "skill_id": current_rec.skill_id,
                    "reason": "继续沿用当前 skill",
                    "score": current_score,
                }

    return {
        "skill_id": best.skill_id,
        "reason": best_reason or "自动匹配",
        "score": best_score,
    }


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
            "\n\nIMPORTANT — how to run the commands above:\n"
            f"1. bash tool: your shell's working directory is AUTOMATICALLY set to "
            f"`{assets_relpath}/` (this skill's own folder) for every command you run while "
            "this skill is active. This means every relative path written in the "
            "instructions above — `scripts/office/unpack.py`, `REFERENCE.md`, "
            "`python scripts/accept_changes.py input.docx output.docx`, etc. — already works "
            "exactly as written. Do NOT prefix them with anything; do NOT `cd` elsewhere "
            "first. If a command needs to read/write a file the user uploaded or a file "
            f"you created earlier in this conversation, that file lives one level up, at "
            f"`../` relative to your current directory (i.e. the workspace root) — use "
            f"`../document.docx` or an absolute path from the `Cwd:` line in the bash "
            "tool's output to reach it.\n"
            f"2. text_editor tool: unlike bash, every path you pass to text_editor is "
            "resolved relative to the workspace root, NOT this skill's folder. So to open "
            f"a skill asset with text_editor you must write the full path, e.g. "
            f"`{assets_relpath}/scripts/office/unpack.py`."
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


def _iter_asset_files(skill_dir: Path) -> Iterable[Path]:
    """遍历 skill 目录下除 SKILL.md 外的所有文件（资源：scripts/、*.md 参考文档等）。"""
    for path in skill_dir.rglob("*"):
        if path.is_dir():
            continue
        if path.name == "SKILL.md" and path.parent == skill_dir:
            continue
        yield path


def skill_assets_workspace_relpath(skill_id: str) -> str:
    """该 skill 的资源在 workspace 内的相对路径（bash/text_editor 可直接使用）。"""
    return f"{SKILL_ASSETS_DIRNAME}/{skill_id}"


def sync_skill_assets_to_workspace(skill_id: str, workspace_root: Path) -> dict[str, Any]:
    """
    把 skill 目录下除 SKILL.md 外的全部资源（scripts/、REFERENCE.md、FORMS.md 等）
    复制到 <workspace_root>/.skills/<skill_id>/ 下。

    这一步是必须的：沙箱的 Landlock 策略只放行 workspace_root 本身和只读系统目录，
    `.claude/skills/<id>/` 所在的应用源码树完全不在白名单里。SKILL.md 正文里写的
    `scripts/xxx.py`、`REFERENCE.md` 等相对路径，只有先把文件"物理搬"进 workspace，
    bash / text_editor 工具才有可能访问到。

    增量同步：只有当目标文件不存在，或大小/mtime 与源文件不同才重写，避免每轮对话
    都重新拷贝体积较大的资源（例如 docx skill 里的 XSD schema 集合）。
    """
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
        for src_path in _iter_asset_files(skill_dir):
            rel = src_path.relative_to(skill_dir)
            source_files.add(str(rel))
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

        # 清理目标目录里源端已经不存在的陈旧文件（skill 更新/删除资源后跟着同步）
        for existing in list(dest_root.rglob("*")):
            if existing.is_dir():
                continue
            rel = str(existing.relative_to(dest_root))
            if rel not in source_files:
                try:
                    existing.unlink()
                except OSError:
                    pass
        # 清理空目录
        for existing_dir in sorted(
            (p for p in dest_root.rglob("*") if p.is_dir()), reverse=True
        ):
            try:
                next(existing_dir.iterdir())
            except StopIteration:
                try:
                    existing_dir.rmdir()
                except OSError:
                    pass
            except OSError:
                pass

        result["synced"] = True
        result["files"] = len(source_files)
        result["copied"] = copied
        result["path"] = str(dest_root)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("同步 skill 资源失败 skill_id=%s: %s", skill_id, exc)
        result["error"] = str(exc)

    return result


def activate_skill(skill_id: str, include_body: bool = True) -> dict[str, Any]:
    result = read_skill(skill_id)
    if "error" in result:
        return result
    payload = {
        "activated": result["skill"],
        "activation_note": (
            "Use this skill as the authoritative on-demand workflow. "
            "Load only the body when the task matches, and keep execution narrow."
        ),
    }
    if include_body:
        payload["body"] = result["body"]
    return payload


def catalog_text() -> str:
    catalog = get_skill_catalog()
    lines = [
        f"Skill roots: {', '.join(catalog['roots']) if catalog['roots'] else '(none)'}",
        f"Discovered skills: {catalog['count']}",
    ]
    if catalog.get("featured"):
        lines.append(f"Featured skill: {catalog['featured']}")
    for item in catalog["skills"]:
        desc = item["description"] or "(no description)"
        lines.append(f"- {item['skill_id']}: {item['name']} — {desc}")
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
