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
    from conversation_state import TurnState


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
        turn: Optional["TurnState"] = None,
    ) -> tuple[str | None, Any, list]:
        from ai.agentic_loops import _agentic_loop_openai_compat

        # Chat Completions 没有等价的服务端会话概念，协议路由到这里
        # 意味着当前回合不使用 Responses 的服务端会话；令 cursor 失效，
        # 下次切回 openai_responses 协议时重新自举（见
        # conversation_state.py"模型切换语义"）。canonical history 不受
        # 影响，Chat Completions 继续按既有行为全量重发（本协议本就是
        # "无状态 provider"，见任务指南 Task 9）。
        chat_id = getattr(builder, "chat_id", None)
        if chat_id is not None:
            try:
                from conversation_state import invalidate_responses_cursor
                invalidate_responses_cursor(chat_id)
            except Exception:
                pass
        client = api_client.get_client_for_model(model_info)
        api_label = model_info.provider
        return await _agentic_loop_openai_compat(
            client, current_model, messages, api_label, builder,
            tools=tools, supports_tools=supports_tools, journal=journal,
        )


__all__ = ["OpenAIChatAdapter"]
