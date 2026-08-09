from __future__ import annotations

from pathlib import Path

from apitelegramchat.workspace_paths import data_root, sanitize_namespace, workspace_root, workspace_file
from .settings import get_mcp_scope


def mcp_workspace_root() -> Path:
    scope = get_mcp_scope()
    return workspace_root(chat_id=scope.name, namespace=scope.workspace_namespace)


def mcp_workspace_file(filename: str) -> Path:
    scope = get_mcp_scope()
    return workspace_file(chat_id=scope.name, filename=filename, namespace=scope.workspace_namespace)


__all__ = ["data_root", "sanitize_namespace", "workspace_root", "workspace_file", "mcp_workspace_root", "mcp_workspace_file"]
