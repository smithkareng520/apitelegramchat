from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .registry import _chat_id
from apitelegramchat.workspace_paths import workspace_root, memory_state_file, todo_state_file

def _resource_paths(root: Path) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for uri, path in [
        ("workspace://current/todos", todo_state_file(_chat_id())),
        ("workspace://current/memories", memory_state_file(_chat_id())),
    ]:
        if path.exists():
            items.append((uri, path.name))
    items.append(("workspace://current/files", "files"))
    items.append(("workspace://current/manifest", "manifest.json"))
    return items

async def list_resources() -> list[dict[str, Any]]:
    root = workspace_root(_chat_id())
    items = []
    for uri, rel in _resource_paths(root):
        mime = "application/json" if rel in {"files", "manifest.json"} or rel.endswith(".json") else "text/plain"
        items.append({"uri": uri, "name": rel, "mimeType": mime})
    return items

async def read_resource(uri: str) -> dict[str, Any]:
    root = workspace_root(_chat_id())
    if uri == "workspace://current/files":
        payload = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                payload.append({"path": str(path.relative_to(root)), "size": path.stat().st_size})
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload, ensure_ascii=False)}]}
    if uri == "workspace://current/manifest":
        payload = {"workspace": str(root), "files": [str(p.relative_to(root)) for p in sorted(root.rglob("*")) if p.is_file()]}
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload, ensure_ascii=False)}]}
    mapping = {"workspace://current/todos": todo_state_file(_chat_id()), "workspace://current/memories": memory_state_file(_chat_id())}
    path = mapping.get(uri)
    if path is None:
        return {"error": {"code": -32602, "message": f"Unknown resource: {uri}"}}
    text = path.read_text(encoding="utf-8") if path.exists() else "{}"
    return {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}
