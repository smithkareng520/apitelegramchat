# -*- coding: utf-8 -*-
"""图像协议适配器：ImageTask -> 各图像链路的统一分发。

图像模型不再按 provider if-else：模型声明什么协议，任务就分发进哪个
图像适配器——
  - openai_images  （ModelScope / XXTF / Agnes 等标准 OpenAI Images 端点；
                     端点 URL 与参考图形状由模型/厂商配置驱动，见
                     media_generation.resolve_images_endpoint_shape）
  - openai_chat    （OpenRouter 等经 chat.completions + modalities 出图
                     的图像模型，如 gemini-image / seedream 系列）

适配器内部只做"任务 -> 请求 -> 解析"，图片上传 R2、富媒体消息渲染等
后处理仍由调用方（原生图像循环 / 工具层）统一执行，与旧职责划分一致。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from core.images import ImageTask, ImageTaskResult

if TYPE_CHECKING:
    from config import ModelConfig

# 图像端点 URL 特征：路径以 /images/generations 或 /images/edits 结尾（可带
# query/尾斜）。用于"模型未显式声明图像协议，但声明了图像端点"时的配置
# 驱动回退路由（新增一个图像模型只需在 config 里写 endpoint，不需要在
# 本文件新建分支）。
_IMAGES_ENDPOINT_PATH_PATTERN = re.compile(r"/images/(?:generations|edits)(?:[/?#]|$)")


class ImageProtocolAdapter:
    """图像协议适配器契约。"""

    #: 协议标签
    name: str = ""

    async def run_image_task(self, task: ImageTask) -> ImageTaskResult:
        raise NotImplementedError


class OpenAIImagesAdapter(ImageProtocolAdapter):
    """OpenAI Images 协议（/images/generations、/images/edits）。

    请求出口复用 media_generation 的统一实现（鉴权、payload、参考图
    下载/真实图片校验/降采样、ModelScope 异步任务轮询、瞬态 400 重试
    等鲁棒性逻辑全部留在原处）。端点选择按 ImageTask.operation 显式映射：
        generate   -> /images/generations（JSON）
        edit       -> /images/edits（multipart），失败即报错；
                      绝不回退 /images/generations（该端点不接受 image
                      参数，回退等于把编辑降级成文生图"假成功"）
        variation  -> 同 edit（ModelScope / XXTF 均无 /variations 端点）
    """

    name = "openai_images"

    async def run_image_task(self, task: ImageTask) -> ImageTaskResult:
        from ai.media_generation import _request_openai_images_task

        return await _request_openai_images_task(task)


class ChatModalitiesImageAdapter(ImageProtocolAdapter):
    """Chat Completions + modalities 图像链路（OpenRouter 等）。

    图像模型经 chat.completions（modalities=["image","text"]）出图，
    参考图作为消息内容输入（等价"编辑/变体"语义）；纯文本 prompt 即
    文生图。响应解析复用 media_generation 的统一实现。
    """

    name = "openai_chat"

    async def run_image_task(self, task: ImageTask) -> ImageTaskResult:
        from ai.media_generation import _request_chat_modalities_image_task

        return await _request_chat_modalities_image_task(task)


# 协议 -> 图像适配器（单例）。
IMAGE_PROTOCOLS: dict[str, ImageProtocolAdapter] = {
    "openai_images": OpenAIImagesAdapter(),
    "openai_chat": ChatModalitiesImageAdapter(),
}


def _endpoint_shape_is_images(ep) -> bool:
    """合并后的有效端点是否声明了 OpenAI Images 形状的完整 URL。"""
    for attr in ("endpoint", "edits_endpoint"):
        url = str(getattr(ep, attr, None) or "").strip()
        if url and _IMAGES_ENDPOINT_PATH_PATTERN.search(url):
            return True
    return False


def resolve_image_adapter(model_info: "ModelConfig") -> ImageProtocolAdapter:
    """按模型配置取图像适配器（配置驱动 + 端点特征优先回退）。

    路由顺序：
      1. 模型（含厂商级默认合并后）声明了 OpenAI Images 形状的完整端点
         （endpoint="https://.../v1/images/generations" 等）-> 路由到
         openai_images 适配器。端点是比协议更具体的信号——特别是协议
         继承到 openai_chat（如 Agnes 厂商默认）时，openai_chat 同时也是
         IMAGE_PROTOCOLS 里的合法图像链路（chat modalities），但声明了
         images 端点的模型绝不该再走 chat.completions（网关会 400
         "is an image model. Use /v1/images/generations"）。若此时协议
         不是 openai_images，留 warning 提示显式补上（回退是为了让
         "只写端点"的最小配置也能工作，不是鼓励省略 protocol）。
      2. 有效协议是图像链路协议（openai_images / openai_chat）-> 直接用
         （OpenRouter 的 gemini-image / seedream 等无端点声明模型走此路）。
      3. 其它（anthropic_messages / gemini_native 等无图像链路协议且无
         图像端点声明）-> 直接报错而不是静默回落。
    """
    from config import get_effective_endpoint

    ep = get_effective_endpoint(model_info)
    protocol = ep.protocol

    if _endpoint_shape_is_images(ep):
        if protocol != "openai_images":
            import logging
            logging.getLogger(__name__).warning(
                "模型 %s 的协议是 %r，但声明了 OpenAI Images 端点，已按端点"
                "路由到 openai_images 适配器；建议在配置中显式声明 "
                "protocol=\"openai_images\"",
                getattr(model_info, "model_id", "?"), protocol,
            )
        return IMAGE_PROTOCOLS["openai_images"]

    adapter = IMAGE_PROTOCOLS.get(protocol)
    if adapter is not None:
        return adapter

    raise ValueError(
        f"模型 {getattr(model_info, 'model_id', '?')} 的协议 {protocol!r} "
        "不支持图像任务（可用: openai_images / openai_chat，或声明指向 "
        "/images/generations|edits 的 endpoint）"
    )


async def dispatch_image_task(task: ImageTask) -> ImageTaskResult:
    """ImageTask 的统一分发入口。

    用法（调用方只构造任务，不关心端点）：
        task = ImageTask.edit("变成油画", [img_url], model="gpt-image-2")
        result = await dispatch_image_task(task)
    """
    from config import SUPPORTED_MODELS

    model_info = SUPPORTED_MODELS.get(task.model)
    if model_info is None:
        raise ValueError(f"未知图像模型: {task.model!r}，请检查 SUPPORTED_MODELS")
    adapter = resolve_image_adapter(model_info)
    return await adapter.run_image_task(task)


__all__ = [
    "ImageProtocolAdapter",
    "OpenAIImagesAdapter",
    "ChatModalitiesImageAdapter",
    "IMAGE_PROTOCOLS",
    "resolve_image_adapter",
    "dispatch_image_task",
]