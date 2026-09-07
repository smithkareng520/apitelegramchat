# -*- coding: utf-8 -*-
"""协议路由注册表（Protocol Router）。

把 "protocol 标签 -> 协议适配器" 的映射收敛到唯一一张表：
  - CHAT_PROTOCOLS：聊天 agentic 循环的协议适配器；
  - get_chat_adapter(protocol)：唯一合法的聊天协议取用出口。

原 ai_handlers._call_api / api_client._build_client 里的
if anthropic / else openai 分支判断全部由本表替代——新增协议时只需
注册一行，调用方零改动。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict

from config import _VALID_PROTOCOLS
from protocols.base import ChatProtocolAdapter
from protocols.anthropic_messages import AnthropicMessagesAdapter
from protocols.gemini_native import GeminiNativeAdapter
from protocols.openai_chat import OpenAIChatAdapter

if TYPE_CHECKING:
    from config import ModelConfig

# 协议 -> 聊天适配器（单例；适配器本身无状态，可安全全局复用）。
CHAT_PROTOCOLS: Dict[str, ChatProtocolAdapter] = {
    "openai_chat": OpenAIChatAdapter(),
    "anthropic_messages": AnthropicMessagesAdapter(),
    "gemini_native": GeminiNativeAdapter(),
}


def get_chat_adapter(protocol: str) -> ChatProtocolAdapter:
    """按协议标签取聊天适配器（协议路由唯一出口）。

    未注册的协议直接抛 ValueError——与 config._VALID_PROTOCOLS 校验
    形成双保险（配置期 + 路由期），杜绝"配置了个合法但没实现的协议"
    被静默回落到错误循环的旧风险。
    """
    adapter = CHAT_PROTOCOLS.get(protocol)
    if adapter is None:
        raise ValueError(
            f"未注册的聊天协议: {protocol!r}，"
            f"合法值: {sorted(_VALID_PROTOCOLS)}（已实现: {sorted(CHAT_PROTOCOLS)}）"
        )
    return adapter


def resolve_chat_adapter(model_info: "ModelConfig") -> ChatProtocolAdapter:
    """按 ModelConfig 的有效端点协议解析聊天适配器（便捷封装）。"""
    from config import get_effective_endpoint

    return get_chat_adapter(get_effective_endpoint(model_info).protocol)


__all__ = ["CHAT_PROTOCOLS", "get_chat_adapter", "resolve_chat_adapter"]
