# -*- coding: utf-8 -*-
"""OpenAI Chat Completions 协议适配器（缺省协议）。

覆盖 99% 的 OpenAI 兼容模型：没有特殊声明 protocol 的模型一律落到
本适配器（config.DEFAULT_PROTOCOL = "openai_chat"）。

实际循环实现在 ai/agentic_loops._agentic_loop_openai_compat（流式 +
工具执行 + 历史追加），本适配器只负责：
  - 从 api_client 取该模型缓存的 AsyncOpenAI 客户端；
  - 把调用转发进循环（api_label 沿用 provider key，供日志 / 会话
    亲和 / OpenRouter 偏好等厂商级逻辑继续工作）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from api_client import api_client
from protocols.base import ChatProtocolAdapter

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager
    from config import ModelConfig


class OpenAIChatAdapter(ChatProtocolAdapter):
    name = "openai_chat"

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
        from ai.agentic_loops import _agentic_loop_openai_compat

        client = api_client.get_client_for_model(model_info)
        api_label = model_info.provider
        return await _agentic_loop_openai_compat(
            client, current_model, messages, api_label, builder,
            tools=tools, supports_tools=supports_tools, journal=journal,
        )


__all__ = ["OpenAIChatAdapter"]
