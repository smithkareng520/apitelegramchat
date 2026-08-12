"""Private, scope-bound MCP resource service."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import mcp.types as types
from mcp.server.lowlevel.helper_types import ReadResourceContents

from apitelegramchat.mcp.context import MCPRequestContext
from apitelegramchat.skills import get_skill_catalog, load_skill_records, read_skill_text
from apitelegramchat.workspace_paths import (
    chat_state_root,
    todo_state_file,
    workspace_download_root,
    workspace_root,
    workspace_upload_root,
)


class ResourceNotFoundError(ValueError):
    """Raised when a client requests a URI outside the advertised resource set."""


class ResourceService:
    """Expose metadata-only workspace resources for one trusted scope."""

    def __init__(self, context: MCPRequestContext) -> None:
        self._context = context

    @property
    def _chat_id(self) -> int:
        return self._context.chat_id

    @property
    def _namespace(self) -> str:
        return self._context.scope

    def _workspace(self) -> Path:
        return workspace_root(self._chat_id, self._namespace)

    @staticmethod
    def _tree(root: Path) -> list[dict[str, int | str]]:
        items: list[dict[str, int | str]] = []
        if not root.exists():
            return items
        for path in sorted(root.rglob("*")):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                items.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
            except OSError:
                continue
        return items

    async def list_resources(self) -> list[types.Resource]:
        resources = [
            types.Resource(uri="workspace://current/todos", name="todos.json", mimeType="application/json"),
            types.Resource(uri="workspace://current/memories", name="memories-index.json", mimeType="application/json"),
            types.Resource(uri="workspace://current/skills", name="skills.json", mimeType="application/json"),
            types.Resource(uri="workspace://current/files", name="files.json", mimeType="application/json"),
            types.Resource(uri="workspace://current/upload", name="upload.json", mimeType="application/json"),
            types.Resource(uri="workspace://current/download", name="download.json", mimeType="application/json"),
            types.Resource(uri="workspace://current/manifest", name="manifest.json", mimeType="application/json"),
        ]
        for record in load_skill_records():
            resources.append(
                types.Resource(
                    uri=f"workspace://current/skills/{record.skill_id}",
                    name=f"{record.skill_id}.json",
                    mimeType="application/json",
                )
            )
        return resources

    async def read_resource(self, uri: str) -> Iterable[ReadResourceContents]:
        with self._context.activate():
            text = self._read_text(uri)
        return [ReadResourceContents(content=text, mime_type="application/json")]

    def _read_text(self, uri: str) -> str:
        workspace = self._workspace()
        if uri == "workspace://current/todos":
            path = todo_state_file(self._chat_id, self._namespace)
            return path.read_text(encoding="utf-8") if path.exists() else "{}"
        if uri == "workspace://current/memories":
            memory_root = chat_state_root(self._chat_id, self._namespace) / "memories"
            return json.dumps(
                {"root": "/memories", "files": self._tree(memory_root)},
                ensure_ascii=False,
                indent=2,
            )
        if uri == "workspace://current/skills":
            return json.dumps(get_skill_catalog(), ensure_ascii=False, indent=2)
        if uri.startswith("workspace://current/skills/"):
            skill_id = uri.rsplit("/", 1)[-1]
            advertised = {record.skill_id for record in load_skill_records()}
            if skill_id not in advertised:
                raise ResourceNotFoundError(f"Unknown skill resource: {skill_id}")
            return read_skill_text(skill_id)
        if uri == "workspace://current/files":
            return json.dumps(self._tree(workspace), ensure_ascii=False)
        if uri == "workspace://current/upload":
            return json.dumps(self._tree(workspace_upload_root(self._chat_id, self._namespace)), ensure_ascii=False)
        if uri == "workspace://current/download":
            return json.dumps(self._tree(workspace_download_root(self._chat_id, self._namespace)), ensure_ascii=False)
        if uri == "workspace://current/manifest":
            return json.dumps(
                {
                    "files": self._tree(workspace),
                    "upload": self._tree(workspace_upload_root(self._chat_id, self._namespace)),
                    "download": self._tree(workspace_download_root(self._chat_id, self._namespace)),
                },
                ensure_ascii=False,
            )
        raise ResourceNotFoundError(f"Unknown resource: {uri}")
