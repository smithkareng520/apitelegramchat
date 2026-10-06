"""Trusted identity context for a locally launched MCP server.

The host provides an opaque scope through ``APITELEGRAMCHAT_MCP_SCOPE``.  The
scope doubles as the *state namespace*: the host spawns per-chat internal MCP
servers with ``scope = workspace_namespace(chat_id)`` so that files written by
the server process land in exactly the same ``state/{ns}`` and workspace
directories the host process uses.  The scope is never accepted from MCP
request arguments and is never exposed as a resource.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from state import bind_current_user_namespace, reset_current_user_namespace

# namespace 形如 chat_id 的十进制字符串（host 逐 chat 生成），外部部署可以
# 用更长的随机串；两者都只需要落在 [A-Za-z0-9_.-] 内。
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SCOPE_ENV = "APITELEGRAMCHAT_MCP_SCOPE"


class MCPConfigurationError(RuntimeError):
    """Raised when the local MCP server was started without trusted identity."""


@dataclass(frozen=True)
class MCPRequestContext:
    """Stable, process-local identity for MCP tool and resource execution."""

    scope: str
    chat_id: int

    @classmethod
    def from_environment(cls) -> "MCPRequestContext":
        raw_scope = (os.getenv(_SCOPE_ENV) or "").strip()
        if not _SCOPE_RE.fullmatch(raw_scope):
            raise MCPConfigurationError(
                f"{_SCOPE_ENV} must be a 1-128 character identifier containing "
                "only letters, digits, '.', '_' or '-'."
            )
        digest = hashlib.sha256(raw_scope.encode("utf-8")).digest()
        # Tool helpers take an integer chat_id; the ContextVar-bound
        # namespace (the scope itself) remains the authority for every path
        # helper, so per-chat state resolution is exact regardless of this key.
        return cls(scope=raw_scope, chat_id=int.from_bytes(digest[:8], "big", signed=False))

    @contextmanager
    def activate(self) -> Iterator[None]:
        """Bind this trusted scope to the path helpers for the duration of one call."""
        token = bind_current_user_namespace(self.scope)
        try:
            yield
        finally:
            reset_current_user_namespace(token)


def mutations_are_explicitly_enabled() -> bool:
    """Return whether write/cost-incurring MCP tools are intentionally exposed."""
    return os.getenv("APITELEGRAMCHAT_MCP_ENABLE_MUTATIONS", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }
