"""Runtime skill-context helpers shared by agentic protocol loops."""
from __future__ import annotations

from typing import TYPE_CHECKING

from core.messages import Message, TextBlock

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager


def refresh_skill_catalog(
    loop_messages: list,
    builder: "DraftManager",
    workspace_namespace: str | None = None,
) -> None:
    """Refresh the runtime skill catalog before every model call.

    A tool call may create, edit, or delete a skill during an agent run. The
    next model invocation must therefore see the current workspace catalog,
    while the surrounding system prompt stays byte-stable.
    """
    try:
        from skills import skill_catalog_brief

        chat_id = getattr(builder, "chat_id", None)
        catalog = skill_catalog_brief(chat_id, workspace_namespace)
        for msg in loop_messages:
            if not isinstance(msg, Message) or msg.role != "system":
                continue
            meta = msg.meta or {}
            if not meta.get("skill_catalog_refresh"):
                continue
            prefix = meta.get("skill_catalog_prefix", "")
            suffix = meta.get("skill_catalog_suffix", "")
            if msg.blocks and isinstance(msg.blocks[0], TextBlock):
                msg.blocks[0].text = prefix + catalog + suffix
            break
    except Exception:
        # Never make a stale/temporarily unreadable skill catalog prevent the
        # model call; retain the last known catalog for this invocation.
        import logging
        logging.getLogger(__name__).warning(
            "agent run 内刷新 skills catalog 失败，继续使用上一版本",
            exc_info=True,
        )
