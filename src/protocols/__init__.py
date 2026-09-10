# -*- coding: utf-8 -*-
"""protocols：协议路由与适配器层（Model -> Protocol 的执行端）。

公开出口：
  - resolve_model_route：模型级公共路由（文本/视频/生图，routing）
  - get_chat_adapter / resolve_chat_adapter：聊天协议路由（registry）
  - ChatProtocolAdapter：适配器契约（base）
  - dispatch_image_task / resolve_image_adapter：图像任务路由（images）
  - 统一请求管道（pipeline）：run_preflight 一次完成 参数分层/输入组合
    鉴权/API 分支解析；build_media_request_body 按计划装配媒体请求体
"""
from protocols.base import ChatProtocolAdapter
from protocols.registry import (
    CHAT_PROTOCOLS,
    get_chat_adapter,
    resolve_chat_adapter,
)
from protocols.routing import ModelRoute, resolve_model_route
from protocols.images import (
    IMAGE_PROTOCOLS,
    dispatch_image_task,
    resolve_image_adapter,
)
from protocols.pipeline import (
    AuthVerdict,
    InputCombination,
    RequestPlan,
    TurnPreflight,
    authorize_request,
    build_media_request_body,
    build_video_request_body,
    normalize_video_seconds,
    resolve_input_combination,
    resolve_request_plan,
    run_preflight,
)

__all__ = [
    "ChatProtocolAdapter",
    "CHAT_PROTOCOLS",
    "get_chat_adapter",
    "resolve_chat_adapter",
    "ModelRoute",
    "resolve_model_route",
    "IMAGE_PROTOCOLS",
    "dispatch_image_task",
    "resolve_image_adapter",
    "AuthVerdict",
    "InputCombination",
    "RequestPlan",
    "TurnPreflight",
    "authorize_request",
    "build_media_request_body",
    "build_video_request_body",
    "normalize_video_seconds",
    "resolve_input_combination",
    "resolve_request_plan",
    "run_preflight",
]
