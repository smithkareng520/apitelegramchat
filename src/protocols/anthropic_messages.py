# -*- coding: utf-8 -*-
"""Anthropic 原生 Messages 协议适配器。

覆盖：Anthropic 官方 API（provider="anthropic"）以及声明
protocol="anthropic_messages" 的中转模型（如 XXTF 的 claude-opus-5）。

实际循环实现在 ai/anthropic_bridge._agentic_loop_anthropic（/v1/messages
流式 + 原生 tool_use + 内部消息 -> Anthropic 块转换），本适配器只负责
取该模型缓存的 AsyncAnthropic 客户端并转发。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, cast

from api_client import api_client
from protocols.base import ChatProtocolAdapter

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager
    from config import ModelConfig
    from responses_state import TurnState
    from anthropic import AsyncAnthropic


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
        turn: Optional["TurnState"] = None,
        workspace_namespace: Optional[str] = None,
    ) -> tuple[str | None, Any, list]:
        from ai.anthropic_bridge import _agentic_loop_anthropic

        from protocols.base import invalidate_responses_chain_for

        # 协议路由到了 Anthropic（传统 Messages 协议，无服务端会话）：
        # 本回合的问答不在 Responses 服务端 response chain 里，显式作废
        # previous_response_id 链头——下次切回 Responses 协议时以本地
        # 全量上下文重新 bootstrap。canonical history 不受影响。
        invalidate_responses_chain_for(builder)
        client = cast("AsyncAnthropic", api_client.get_client_for_model(model_info))
        return await _agentic_loop_anthropic(
            client, current_model, messages, builder,
            tools=tools, supports_tools=supports_tools, journal=journal,
            workspace_namespace=workspace_namespace,
        )


__all__ = ["AnthropicMessagesAdapter"]
