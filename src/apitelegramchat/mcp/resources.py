from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apitelegramchat.skills import discover_skill_roots, get_skill_catalog, load_skill_records, read_skill_text
from .registry import _chat_id
from apitelegramchat.workspace_paths import (
    workspace_root,
    workspace_upload_root, workspace_download_root,
    memory_state_file, todo_state_file,
)


def _resource_paths(root: Path) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for uri, path in [
        ("workspace://current/todos", todo_state_file(_chat_id())),
        ("workspace://current/memories", memory_state_file(_chat_id())),
    ]:
        if path.exists():
            items.append((uri, path.name))

    # Skill discovery chain
    items.append(("workspace://current/skills", "skills.json"))
    for rec in load_skill_records():
        items.append((f"workspace://current/skills/{rec.skill_id}", f"{rec.skill_id}.json"))

    items.append(("workspace://current/files", "files"))
    items.append(("workspace://current/upload", "upload"))
    items.append(("workspace://current/download", "download"))
    items.append(("workspace://current/manifest", "manifest.json"))
    return items


async def list_resources() -> list[dict[str, Any]]:
    root = workspace_root(_chat_id())
    items = []
    for uri, rel in _resource_paths(root):
        mime = "application/json" if rel in {"files", "upload", "download", "manifest.json", "skills.json"} or rel.endswith(".json") else "text/plain"
        items.append({"uri": uri, "name": rel, "mimeType": mime})
    return items


def _list_tree(root: Path) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    if not root.exists():
        return payload
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                payload.append({"path": str(path.relative_to(root)), "size": path.stat().st_size})
            except OSError:
                continue
    return payload


async def read_resource(uri: str) -> dict[str, Any]:
    root = workspace_root(_chat_id())
    if uri == "workspace://current/files":
        payload = _list_tree(root)
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload, ensure_ascii=False)}]}

    if uri == "workspace://current/upload":
        payload = _list_tree(workspace_upload_root(_chat_id()))
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload, ensure_ascii=False)}]}

    if uri == "workspace://current/download":
        payload = _list_tree(workspace_download_root(_chat_id()))
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload, ensure_ascii=False)}]}

    if uri == "workspace://current/manifest":
        payload = {
            "workspace": str(root),
            "files": [str(p.relative_to(root)) for p in sorted(root.rglob("*")) if p.is_file()],
            "upload": [str(p) for p in sorted(workspace_upload_root(_chat_id()).rglob("*")) if p.is_file()],
            "download": [str(p) for p in sorted(workspace_download_root(_chat_id()).rglob("*")) if p.is_file()],
        }
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload, ensure_ascii=False)}]}

    if uri == "workspace://current/skills":
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(get_skill_catalog(), ensure_ascii=False, indent=2),
            }]
        }

    if uri.startswith("workspace://current/skills/"):
        skill_id = uri.rsplit("/", 1)[-1]
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": read_skill_text(skill_id)}]}

    mapping = {"workspace://current/todos": todo_state_file(_chat_id()), "workspace://current/memories": memory_state_file(_chat_id())}
    path = mapping.get(uri)
    if path is None:
        return {"error": {"code": -32602, "message": f"Unknown resource: {uri}"}}
    text = path.read_text(encoding="utf-8") if path.exists() else "{}"
    return {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}
