# -*- coding: utf-8 -*-
"""Anthropic 原生 Messages 协议适配器。

覆盖：Anthropic 官方 API（provider="anthropic"）以及声明
protocol="anthropic_messages" 的中转模型（如 XXTF 的 claude-opus-5）。

实际循环实现在 ai/anthropic_bridge._agentic_loop_anthropic（/v1/messages
流式 + 原生 tool_use + 内部消息 -> Anthropic 块转换），本适配器只负责
取该模型缓存的 AsyncAnthropic 客户端并转发。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from api_client import api_client
from protocols.base import ChatProtocolAdapter

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager
    from config import ModelConfig


class AnthropicMessagesAdapter(ChatProtocolAdapter):
    name = "anthropic_messages"

    async def run_agent_loop(
        self,
        *,
        current_model: str,
        model_info: "ModelConfig",
        messages: list,
        builder: "DraftManager",
        tools: Optional[list[Any]] = None,
        supports_tools: bool = True,
        journal: Optional[list[Any]] = None,
    ) -> tuple[str | None, Any, list]:
        from ai.anthropic_bridge import _agentic_loop_anthropic

        client = api_client.get_client_for_model(model_info)
        return await _agentic_loop_anthropic(
            client, current_model, messages, builder,
            tools=tools, supports_tools=supports_tools, journal=journal,
        )


__all__ = ["AnthropicMessagesAdapter"]
