from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path


_NAMESPACE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_WORKDIR_NAME = os.getenv("APITELEGRAMCHAT_WORKDIR_NAME", "workspace").strip() or "workspace"
_STATE_DIR_NAME = os.getenv("APITELEGRAMCHAT_STATE_DIR_NAME", "state").strip() or "state"


def _resolved_namespace(chat_id: object, namespace: object | None = None) -> str:
    if namespace is not None:
        return sanitize_namespace(namespace)
    try:
        from apitelegramchat.state import get_current_user_namespace

        current = get_current_user_namespace()
        if current:
            return sanitize_namespace(current)
    except Exception:
        pass
    return sanitize_namespace(f"chat_{chat_id}")


@lru_cache(maxsize=1)
def data_root() -> Path:
    # Use a writable location by default. On managed deploys the app directory may
    # be read-only for the runtime user, so default to /tmp unless explicitly set.
    base = os.getenv("APITELEGRAMCHAT_DATA_DIR", "/tmp/apitelegramchat_data")
    root = Path(base).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        fallback = Path("/tmp/apitelegramchat_data").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        root = fallback
    return root


def sanitize_namespace(value: object) -> str:
    raw = "default" if value is None else str(value).strip()
    raw = raw or "default"
    safe = _NAMESPACE_RE.sub("_", raw)
    return safe.strip("._") or "default"


def workspace_root(chat_id: object, namespace: object | None = None) -> Path:
    ns = _resolved_namespace(chat_id, namespace)
    root = data_root() / "workspaces" / ns
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def workspace_workdir(chat_id: object, namespace: object | None = None) -> Path:
    # Keep the shell workdir at the chat root so the workspace tree stays flat.
    root = workspace_root(chat_id, namespace)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def workspace_file(chat_id: object, filename: str, namespace: object | None = None) -> Path:
    return workspace_root(chat_id, namespace) / filename


def state_root() -> Path:
    root = data_root() / _STATE_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def chat_state_root(chat_id: object, namespace: object | None = None) -> Path:
    ns = _resolved_namespace(chat_id, namespace)
    root = state_root() / ns
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def state_file(chat_id: object, filename: str, namespace: object | None = None) -> Path:
    return chat_state_root(chat_id, namespace) / filename


def memory_state_file(chat_id: object, namespace: object | None = None) -> Path:
    return state_file(chat_id, "memories.json", namespace)


def todo_state_file(chat_id: object, namespace: object | None = None) -> Path:
    return state_file(chat_id, "todos.json", namespace)


def workspace_namespace(chat_id: object, namespace: object | None = None) -> str:
    return _resolved_namespace(chat_id, namespace)


def runtime_cache_root(chat_id: object, namespace: object | None = None) -> Path:
    """持久化运行时缓存（python/pip/编译缓存），避免每次 bash 重建。"""
    root = workspace_root(chat_id, namespace) / ".runtime_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()