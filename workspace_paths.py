from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

_NAMESPACE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@lru_cache(maxsize=1)
def data_root() -> Path:
    base = os.getenv("APITELEGRAMCHAT_DATA_DIR", ".apitelegramchat_data")
    root = Path(base).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def sanitize_namespace(value: object) -> str:
    raw = "default" if value is None else str(value).strip()
    raw = raw or "default"
    safe = _NAMESPACE_RE.sub("_", raw)
    return safe.strip("._") or "default"


def workspace_root(chat_id: object, namespace: object | None = None) -> Path:
    ns = namespace if namespace is not None else f"chat_{chat_id}"
    root = data_root() / "workspaces" / sanitize_namespace(ns)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def workspace_file(chat_id: object, filename: str, namespace: object | None = None) -> Path:
    return workspace_root(chat_id, namespace) / filename
