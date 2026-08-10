from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

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
    for root, skill_md in _iter_skill_files():
        meta = _read_skill_header(skill_md)
        skill_id = skill_md.parent.name
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
