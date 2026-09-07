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
    ) -> tuple[str | None, Any, list]:
        from ai.gemini_bridge import _agentic_loop_gemini_native

        return await _agentic_loop_gemini_native(
            current_model, messages, builder,
            tools=tools, supports_tools=supports_tools, journal=journal,
        )


__all__ = ["GeminiNativeAdapter"]
