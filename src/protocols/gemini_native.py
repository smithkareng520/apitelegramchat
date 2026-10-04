# -*- coding: utf-8 -*-
"""Gemini 原生协议适配器（streamGenerateContent SSE）。

覆盖：Gemini 官方 API（provider="gemini" 厂商默认协议）。

实际循环实现在 ai/gemini_bridge._agentic_loop_gemini_native（aiohttp
直连 v1beta streamGenerateContent?alt=sse + 原生 function calling，
内部消息 -> Gemini contents 转换），本适配器只做转发——该协议不经过
SDK 客户端。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from protocols.base import ChatProtocolAdapter

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager
    from config import ModelConfig
    from responses_state import TurnState


class GeminiNativeAdapter(ChatProtocolAdapter):
    name = "gemini_native"

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
        from ai.gemini_bridge import _agentic_loop_gemini_native

        from protocols.base import invalidate_responses_chain_for

        # 协议路由到了 Gemini 原生（无服务端会话）：显式作废 Responses
        # 链头，防止下次切回 Responses 协议时续旧链丢失本回合上下文。
        invalidate_responses_chain_for(builder)

        return await _agentic_loop_gemini_native(
            current_model, messages, builder,
            tools=tools, supports_tools=supports_tools, journal=journal,
            workspace_namespace=workspace_namespace,
        )


__all__ = ["GeminiNativeAdapter"]
