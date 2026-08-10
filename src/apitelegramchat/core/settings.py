from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MCPScope:
    name: str


def get_mcp_scope() -> MCPScope:
    raw = os.getenv("APITELEGRAMCHAT_MCP_SCOPE", "default").strip()
    return MCPScope(name=raw or "default")
