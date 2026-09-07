# -*- coding: utf-8 -*-
"""protocols：协议路由与适配器层（Model -> Protocol 的执行端）。

公开出口：
  - get_chat_adapter / resolve_chat_adapter：聊天协议路由（registry）
  - ChatProtocolAdapter：适配器契约（base）
  - dispatch_image_task / resolve_image_adapter：图像任务路由（images）
"""
from protocols.base import ChatProtocolAdapter
from protocols.registry import (
    CHAT_PROTOCOLS,
    get_chat_adapter,
    resolve_chat_adapter,
)
from protocols.images import (
    IMAGE_PROTOCOLS,
    dispatch_image_task,
    resolve_image_adapter,
)

__all__ = [
    "ChatProtocolAdapter",
    "CHAT_PROTOCOLS",
    "get_chat_adapter",
    "resolve_chat_adapter",
    "IMAGE_PROTOCOLS",
    "dispatch_image_task",
    "resolve_image_adapter",
]
