# -*- coding: utf-8 -*-
"""ImageTask：图像生成的统一任务模型（显式操作语义）。

重构目标：废除"看到参考图 = edit"的隐式推断——**操作类型是任务的
一等字段**，由任务构造方（工具层 / 原生图像循环）显式声明：
    generate   文生图，无参考图输入
    edit       带参考图的编辑 / 图生图
    variation  以参考图为基础生成变体（prompt 可空，适配器补默认指令）

端点选择从"任务 + 模型协议"推导，收敛在 protocols/images.py 的
适配器里：
    ImageTask(openai_images 模型, operation=edit)
        -> /images/edits（官方 multipart；失败即报错，绝不回退
           /images/generations——该端点不接受 image 参数，回退等于
           把编辑降级成文生图"假成功"，2026-09-08 生产事故）
    ImageTask(openai_images 模型, operation=generate)
        -> /images/generations
    ImageTask(openai_chat 模型, ...)
        -> chat.completions + modalities（参考图作为消息内容输入）
    ModelScope 图像模型
        -> 一律 /images/generations（该厂商不存在 /images/edits 端点）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

# 任务操作类型：显式声明，不再从"有没有图片"反推。
ImageOperation = Literal["generate", "edit", "variation"]

VALID_OPERATIONS: tuple[str, ...] = ("generate", "edit", "variation")

# variation 操作在"仅接受编辑语义"的端点（/images/edits、chat modalities）
# 上使用的默认指令：任务构造方未给 prompt 时由适配器补上。
DEFAULT_VARIATION_PROMPT = (
    "Generate a new variation of this image, keeping the subject and "
    "composition recognizable while varying style and details."
)


@dataclass
class ImageTask:
    """一次图像生成/编辑/变体任务的全部输入。

    Attributes:
        operation:    显式操作类型（generate / edit / variation）。
        prompt:       文本提示词；variation 允许为空（适配器补默认指令），
                      edit 建议提供编辑指令。
        input_images: 参考图 URL 列表（http(s) 或 data:image/...;base64）。
                      generate 语义下应为空；edit / variation 至少一张。
        model:        模型 ID（SUPPORTED_MODELS 的 key）。
        num_images:   生成张数（1-4，适配器按厂商上限裁剪）。
        aspect_ratio: 宽高比（"1:1" / "16:9" ...；由适配器映射为厂商参数）。
        image_size:   图像尺寸档位（"1K" 等；仅 chat modalities 路径的
                      image_config 使用）。
        meta:         调用方附加工元数据（日志/UI 用，不进请求体）。
    """

    operation: ImageOperation
    prompt: str = ""
    input_images: list[str] = field(default_factory=list)
    model: str = ""
    num_images: int = 1
    aspect_ratio: str = "1:1"
    image_size: str = "1K"
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.operation not in VALID_OPERATIONS:
            raise ValueError(
                f"ImageTask.operation={self.operation!r} 无效，"
                f"合法值: {list(VALID_OPERATIONS)}——操作必须显式声明，"
                "禁止由'是否带参考图'隐式推断。"
            )
        if self.operation == "generate":
            # generate 语义下忽略（而非报错）误入的参考图，保持宽容；
            # 但 prompt 为空毫无意义，直接暴露配置错误。
            self.input_images = []
            if not str(self.prompt or "").strip():
                raise ValueError("ImageTask(operation='generate') 的 prompt 不能为空")
        else:
            if not self.input_images:
                raise ValueError(
                    f"ImageTask(operation='{self.operation}') 至少需要一张参考图"
                    "（input_images）；纯文本生图请用 operation='generate'"
                )

    @property
    def effective_prompt(self) -> str:
        """适配器实际应使用的 prompt（variation 空提示时补默认指令）。"""
        text = str(self.prompt or "").strip()
        if not text and self.operation == "variation":
            return DEFAULT_VARIATION_PROMPT
        return text

    @classmethod
    def generate(cls, prompt: str, model: str, **kwargs: Any) -> "ImageTask":
        """文生图任务（便捷构造器）。"""
        return cls(operation="generate", prompt=prompt, model=model, **kwargs)

    @classmethod
    def edit(cls, prompt: str, input_images: list[str], model: str, **kwargs: Any) -> "ImageTask":
        """带参考图的编辑任务（便捷构造器）。"""
        return cls(
            operation="edit", prompt=prompt,
            input_images=list(input_images or []), model=model, **kwargs,
        )

    @classmethod
    def variation(cls, input_images: list[str], model: str, prompt: str = "", **kwargs: Any) -> "ImageTask":
        """变体任务（便捷构造器；prompt 可空，适配器补默认指令）。"""
        return cls(
            operation="variation", prompt=prompt,
            input_images=list(input_images or []), model=model, **kwargs,
        )


class ImageRequestError(Exception):
    """图像请求失败（携带状态码/端点/详情，供上层统一格式化错误提示）。"""

    def __init__(self, detail: str, *, status_code: int = 500,
                 endpoint: str = "", request_id: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.endpoint = endpoint
        self.request_id = request_id


@dataclass
class ImageTaskResult:
    """图像任务的统一结果。

    images:  已验证的图片字节列表（空列表 = 未产出图片）。
    text:    模型随图返回的文本说明（chat modalities 路径可能有值）。
    refusal: 安全拒绝文案（如有）。
    finish_reason: 上游 finish_reason（chat modalities 路径）。
    endpoint:  实际使用的相对端点（"/images/generations" / "/images/edits"
               / "/chat/completions"），供日志与错误提示。
    usage:     上游 usage（如有）。
    diagnostics: 逐项拒绝诊断（如"图片 URL 下载下来是 HTML 错误页"）。
               images 为空时调用方应把诊断并入错误提示，替代笼统的
               "未找到可用图片数据"。
    """
    images: list[bytes] = field(default_factory=list)
    text: str = ""
    refusal: str = ""
    finish_reason: str = ""
    endpoint: str = ""
    usage: Any = None
    diagnostics: list[str] = field(default_factory=list)


__all__ = [
    "ImageOperation",
    "VALID_OPERATIONS",
    "DEFAULT_VARIATION_PROMPT",
    "ImageTask",
    "ImageTaskResult",
    "ImageRequestError",
]
