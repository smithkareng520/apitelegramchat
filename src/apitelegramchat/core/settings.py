from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# The legacy project imports config.py directly from many modules.
# This package provides a safe, import-friendly view for the MCP-native layout.

@dataclass(frozen=True)
class RuntimeScope:
    name: str

    @property
    def workspace_namespace(self) -> str:
        return f"mcp_{self.name}"


def get_mcp_scope() -> RuntimeScope:
    raw = (os.getenv("APITELEGRAMCHAT_MCP_SCOPE") or os.getenv("MCP_SCOPE") or "default").strip()
    return RuntimeScope(raw or "default")


def get_data_dir() -> str:
    return os.getenv("APITELEGRAMCHAT_DATA_DIR", ".apitelegramchat_data")


def validate_telegram_runtime(strict: bool = False) -> None:
    if not strict:
        return
    missing = []
    for key in ("TELEGRAM_BOT_TOKEN", "WEBHOOK_TOKEN", "WEBHOOK_URL", "OPENROUTER_API_KEY"):
        if not os.getenv(key):
            missing.append(key)
    if missing:
        raise RuntimeError(f"缺少必需的环境变量: {', '.join(missing)}")
