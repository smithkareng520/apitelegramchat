"""Trusted identity context for a locally launched MCP server.

The host must provide an opaque scope through ``APITELEGRAMCHAT_MCP_SCOPE``.
The scope is not accepted from MCP request arguments and is never exposed as a
resource.  It is used solely to isolate the server process' workspace/state.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from apitelegramchat.state import bind_current_user_namespace, reset_current_user_namespace

_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{15,127}$")
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
                f"{_SCOPE_ENV} must be an opaque 16-128 character identifier containing "
                "only letters, digits, '.', '_' or '-'."
            )
        digest = hashlib.sha256(raw_scope.encode("utf-8")).digest()
        # Keep an integer for legacy tool APIs. Namespace remains the authority;
        # this value is only a stable in-process compatibility key.
        return cls(scope=raw_scope, chat_id=int.from_bytes(digest[:8], "big", signed=False))

    @contextmanager
    def activate(self) -> Iterator[None]:
        """Bind this trusted scope to legacy helpers for the duration of one call."""
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
