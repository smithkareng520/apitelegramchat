# -*- coding: utf-8 -*-
"""模型级公共路由方法：按模型配置字段匹配 文本 / 视频 / 生图 链路。

这是"新增模型不需要新建请求分支"的顶层体现：回合调度方（ai_handlers）
与工具层只需要问一次 :func:`resolve_model_route`，就能拿到该模型应进入
的执行链路；后续的端点选择继续由协议层（registry / images）与请求层
（media_generation.resolve_images_endpoint_shape / _request_agnes_video）
按配置解析。整条链路上没有任何按模型/厂商硬编码的分支。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from config import ModelConfig

# 模型路由标签：
#   video: 原生视频生成模型（video_output=True）-> 视频任务循环
#   image: 原生图像生成模型（image_output=True）-> 图像任务循环
#   chat:  其余模型 -> 常规 agentic chat 循环（工具调用/纯文本）
ModelRoute = Literal["video", "image", "chat"]


def resolve_model_route(model_info: "ModelConfig | None") -> ModelRoute:
    """按模型配置字段返回应进入的执行链路（公共路由唯一出口）。

    判定完全基于模型自身的能力声明字段（video_output / image_output），
    与厂商无关——同一厂商下混布文本/图像/视频模型时无需任何特判：
        agnes-3.0-flash      -> "chat"
        agnes-image-2.5-flash -> "image"
        agnes-video-2.5      -> "video"

    model_info 为 None（未注册模型）时保守回落 "chat"，保持既有行为。
    """
    if model_info is None:
        return "chat"
    if bool(getattr(model_info, "video_output", False)):
        return "video"
    if bool(getattr(model_info, "image_output", False)):
        return "image"
    return "chat"


__all__ = ["ModelRoute", "resolve_model_route"]
