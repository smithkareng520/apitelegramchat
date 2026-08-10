from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from apitelegramchat.workspace_paths import workspace_root

logger = logging.getLogger(__name__)

_SKILL_MD_NAMES = ("SKILL.md", "skill.md")
_FRONTMATTER_RE = re.compile(r"^---\s*$")


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    body: str
    path: Path
    source: str
    frontmatter: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "allowed_tools": list(self.allowed_tools),
            "path": str(self.path),
            "source": self.source,
        }

    def detail(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "frontmatter": self.frontmatter,
            "content": self.body,
        }

    def prompt_text(self) -> str:
        text = self.body.strip()
        if not text:
            return ""
        return text


@dataclass(frozen=True)
class _ParsedSkill:
    frontmatter: dict[str, Any]
    body: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _skill_roots(chat_id: int) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    workspace_skill_root = workspace_root(chat_id) / ".claude" / "skills"
    roots.append(("workspace", workspace_skill_root))
    roots.append(("repo", _repo_root() / ".claude" / "skills"))
    return roots


def _iter_skill_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        for candidate in _SKILL_MD_NAMES:
            path = child / candidate
            if path.is_file():
                files.append(path)
                break
    return files


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if raw in {"true", "True"}:
        return True
    if raw in {"false", "False"}:
        return False
    if raw in {"null", "None", "~"}:
        return None
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            pass
    if re.fullmatch(r"-?\d+\.\d+", raw):
        try:
            return float(raw)
        except ValueError:
            pass
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    return raw


def _parse_frontmatter(text: str) -> _ParsedSkill:
    lines = text.splitlines()
    if not lines or not _FRONTMATTER_RE.match(lines[0].strip()):
        return _ParsedSkill({}, text.strip())

    fm_lines: list[str] = []
    body_start = len(lines)
    for idx in range(1, len(lines)):
        if _FRONTMATTER_RE.match(lines[idx].strip()):
            body_start = idx + 1
            break
        fm_lines.append(lines[idx])

    fm: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[Any] | None = None
    for raw_line in fm_lines:
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") or line.startswith("- "):
            if current_key is None:
                continue
            if current_list is None:
                current_list = []
                fm[current_key] = current_list
            current_list.append(_parse_scalar(line.split("- ", 1)[1]))
            continue
        if ":" in line:
            key, rest = line.split(":", 1)
            key = key.strip().lower().replace("-", "_")
            value = rest.strip()
            current_key = key
            if value == "":
                current_list = []
                fm[key] = current_list
            else:
                current_list = None
                fm[key] = _parse_scalar(value)
        else:
            # Ignore unexpected frontmatter fragments rather than failing the whole skill.
            continue

    body = "\n".join(lines[body_start:]).strip()
    return _ParsedSkill(fm, body)


def _normalize_allowed_tools(frontmatter: dict[str, Any]) -> tuple[str, ...]:
    for key in ("allowed_tools", "tools", "allowedtools"):
        value = frontmatter.get(key)
        if isinstance(value, list):
            return tuple(str(v).strip() for v in value if str(v).strip())
        if isinstance(value, str) and value.strip():
            parts = [p.strip() for p in re.split(r"[;,]", value) if p.strip()]
            if parts:
                return tuple(parts)
    return ()


def _skill_from_file(path: Path, source: str) -> SkillRecord | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to read skill file %s: %s", path, exc)
        return None

    parsed = _parse_frontmatter(raw)
    fm = dict(parsed.frontmatter)
    name = str(fm.get("name") or path.parent.name).strip()
    if not name:
        return None
    description = str(fm.get("description") or fm.get("summary") or "").strip()
    allowed_tools = _normalize_allowed_tools(fm)
    return SkillRecord(
        name=name,
        description=description,
        allowed_tools=allowed_tools,
        body=parsed.body,
        path=path,
        source=source,
        frontmatter=fm,
    )


@lru_cache(maxsize=8)
def _discover_cached(chat_id: int) -> tuple[SkillRecord, ...]:
    records: list[SkillRecord] = []
    seen: set[str] = set()
    for source, root in _skill_roots(chat_id):
        for path in _iter_skill_files(root):
            record = _skill_from_file(path, source)
            if not record:
                continue
            key = record.name.lower()
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
    return tuple(records)


def _invalidate_cache() -> None:
    _discover_cached.cache_clear()


def discover_skills(chat_id: int) -> list[dict[str, Any]]:
    return [record.summary() for record in _discover_cached(chat_id)]


def _find_skill(chat_id: int, name: str) -> SkillRecord | None:
    target = (name or "").strip().lower()
    if not target:
        return None
    for record in _discover_cached(chat_id):
        if record.name.lower() == target:
            return record
    return None


def _error(action: str, message: str, code: str, **extra: Any) -> str:
    payload = {"ok": False, "action": action, "error": message, "code": code}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _success(action: str, **extra: Any) -> str:
    payload = {"ok": True, "action": action}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _list_payload(chat_id: int) -> str:
    skills = discover_skills(chat_id)
    return _success(
        "list",
        skills=skills,
        total=len(skills),
        repo_root=str(_repo_root()),
        workspace_root=str(workspace_root(chat_id)),
    )


def _info_payload(chat_id: int, name: str) -> str:
    record = _find_skill(chat_id, name)
    if not record:
        return _error("info", f"找不到技能：{name}", "not_found")
    return _success("info", skill=record.detail())


def _use_payload(chat_id: int, name: str) -> str:
    record = _find_skill(chat_id, name)
    if not record:
        return _error("use", f"找不到技能：{name}", "not_found")
    return _success(
        "use",
        skill=record.detail(),
        instruction=record.prompt_text(),
        body=record.prompt_text(),
        allowed_tools=list(record.allowed_tools),
    )


async def execute_skill(
    chat_id: int,
    action: str = "list",
    name: Optional[str] = None,
    **extra: Any,
) -> str:
    """File-based skill manager (read-only)."""
    del extra
    action = (action or "list").strip().lower()
    if action == "list":
        _invalidate_cache()
        return _list_payload(chat_id)
    if action == "info":
        _invalidate_cache()
        return _info_payload(chat_id, name or "")
    if action == "use":
        _invalidate_cache()
        return _use_payload(chat_id, name or "")
    return _error(action, f"不支持的 action: {action}", "unsupported")


def _esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_skill_card(payload: dict, max_items: int = 30) -> str:
    if not isinstance(payload, dict):
        return f"<p>{_esc(payload)}</p>"
    if not payload.get("ok"):
        return (
            f"<p>❌ <b>技能操作失败</b></p>"
            f"<p>{_esc(payload.get('error', '未知错误'))}</p>"
        )

    action = payload.get("action", "list")
    if action == "list":
        skills = payload.get("skills", []) or []
        header = "<h3>🎯 可用技能</h3>"
        stat = f"共 <b>{payload.get('total', 0)}</b> 个"
        if not skills:
            return header + f"<p>{stat}</p><blockquote>暂无可用技能</blockquote>"
        rows = []
        for s in skills[:max_items]:
            tools = s.get("allowed_tools", []) or []
            tools_html = " · ".join(f"<code>{_esc(t)}</code>" for t in tools) if tools else "<i>无</i>"
            rows.append(
                f"<tr><td><b>{_esc(s.get('name'))}</b></td>"
                f"<td>{_esc(s.get('source', 'repo'))}</td>"
                f"<td>{_esc(s.get('description'))}</td>"
                f"<td>{tools_html}</td></tr>"
            )
        table = (
            "<table bordered striped>"
            "<tr><th>技能</th><th>来源</th><th>说明</th><th>允许工具</th></tr>"
            + "".join(rows)
            + "</table>"
        )
        extra = len(skills) - max_items
        extra_html = f"<p><i>… 还有 {extra} 个未显示</i></p>" if extra > 0 else ""
        tip = "<p><i>调用 skill use &lt;name&gt; 查看技能正文。</i></p>"
        return header + f"<p>{stat}</p>" + table + extra_html + tip

    skill = payload.get("skill", {}) or {}
    title = {
        "info": "🧩 技能详情",
        "use": "🧩 已加载技能",
    }.get(action, "🎯 技能")
    body = payload.get("instruction") or payload.get("body") or ""
    frontmatter = skill.get("frontmatter") or {}
    meta_lines: list[str] = []
    if skill.get("name"):
        meta_lines.append(f"<p><b>名称：</b> {_esc(skill.get('name'))}</p>")
    if skill.get("description"):
        meta_lines.append(f"<p><b>说明：</b> {_esc(skill.get('description'))}</p>")
    if skill.get("source"):
        meta_lines.append(f"<p><b>来源：</b> {_esc(skill.get('source'))}</p>")
    if skill.get("path"):
        meta_lines.append(f"<p><b>路径：</b> <code>{_esc(skill.get('path'))}</code></p>")
    if skill.get("allowed_tools"):
        tools = ", ".join(f"<code>{_esc(t)}</code>" for t in skill.get("allowed_tools") or [])
        meta_lines.append(f"<p><b>允许工具：</b> {tools}</p>")
    if frontmatter:
        meta_lines.append(f"<details><summary>Frontmatter</summary><pre><code>{_esc(json.dumps(frontmatter, ensure_ascii=False, indent=2))}</code></pre></details>")
    if body:
        meta_lines.append(f"<details><summary>正文</summary><pre><code>{_esc(body)}</code></pre></details>")
    return f"<h3>{title}</h3>" + "".join(meta_lines)


SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": (
            "File-based skill loader. Discover reusable capabilities from .claude/skills/*. "
            "Supported actions: list / info / use. Use 'use' to read a skill's instruction text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "_description": {
                    "type": "string",
                    "description": "简述本次操作目的（≤60字）。示例：查看 telegram-assistant 技能",
                },
                "action": {
                    "type": "string",
                    "enum": ["list", "info", "use"],
                    "description": "要执行的操作。默认 list。",
                },
                "name": {
                    "type": "string",
                    "description": "技能名称。info/use 必填。",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}
