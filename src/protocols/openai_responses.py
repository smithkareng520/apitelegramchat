# -*- coding: utf-8 -*-
"""OpenAI 原生 Responses API 协议适配器（/v1/responses）。

覆盖：显式声明 protocol="openai_responses" 的模型（如 XXTF 中转平台标注
"OpenAI 协议，入口 /v1/responses" 的 gpt-5.6-sol）。与 openai_chat
（Chat Completions，/chat/completions）是两个互斥的协议——同一厂商壳
（provider="xxtf"）下的不同模型可以各自声明不同协议，互不影响
（见 config.py 模型定义处的端点覆盖说明）。

实际循环实现在 ai/responses_bridge._agentic_loop_openai_responses（原生
Responses SSE 流式事件 + function_call item 累积 + 内部消息 <-> Responses
input item 转换），本适配器只负责：
  - 从 api_client 取该模型缓存的 AsyncOpenAI 客户端（Responses API 复用
    同一个 OpenAI SDK 客户端，无需新增原生 SDK 依赖）；
  - 把调用转发进循环（api_label 沿用 provider key，与其它协议适配器
    一致，供日志 / usage 归一化等厂商级逻辑继续工作）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from api_client import api_client
from protocols.base import ChatProtocolAdapter

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager
    from config import ModelConfig


class OpenAIResponsesAdapter(ChatProtocolAdapter):
    name = "openai_responses"

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
        from ai.responses_bridge import _agentic_loop_openai_responses

        client = api_client.get_client_for_model(model_info)
        api_label = model_info.provider
        return await _agentic_loop_openai_responses(
            client, current_model, messages, builder, api_label=api_label,
            tools=tools, supports_tools=supports_tools, journal=journal,
        )


__all__ = ["OpenAIResponsesAdapter"]
