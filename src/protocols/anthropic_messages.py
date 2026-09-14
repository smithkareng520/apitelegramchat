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
    from conversation_state import TurnState


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
    ) -> tuple[str | None, Any, list]:
        from ai.anthropic_bridge import _agentic_loop_anthropic

        # 协议路由到了 Anthropic（传统 Messages 协议，无服务端会话）：
        # 本回合的问答尚未在任何厂商的 Responses 服务端会话中登记，按
        # 多厂商状态机规则（conversation_state.py"分叉态"）作废全部厂商
        # 会话——下次切回 Responses 协议时以本地全量上下文重新自举。
        # canonical history 不受影响；厂商隔离由写入者台账双重保险。
        chat_id = getattr(builder, "chat_id", None)
        if chat_id is not None:
            try:
                from conversation_state import mark_legacy_divergence
                mark_legacy_divergence(chat_id)
            except Exception:
                pass
        client = api_client.get_client_for_model(model_info)
        return await _agentic_loop_anthropic(
            client, current_model, messages, builder,
            tools=tools, supports_tools=supports_tools, journal=journal,
        )


__all__ = ["AnthropicMessagesAdapter"]
