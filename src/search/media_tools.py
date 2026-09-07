"""媒体生成工具：generate_image_from_text / edit_image_with_reference / generate_video（自 search_engine.py 拆出）。"""

import asyncio
import base64
import re
import uuid
from typing import Any, Optional, cast

import aiohttp

from config import OPENROUTER_API_KEY, SUPPORTED_MODELS, get_openrouter_provider_preferences
from s3_utils import upload_bytes_to_r2
from chat_actions import chat_action_scope

OPENROUTER_PROVIDER_PREFERENCES = get_openrouter_provider_preferences()

from core.images import ImageTask, ImageRequestError
from protocols.images import dispatch_image_task

import logging

logger = logging.getLogger(__name__)


# --------------------- image API helpers ---------------------
# 图片响应解析 / 下载 / 转字节（_extract_image_items、_response_items_to_bytes）
# 与生成图上传 R2（_upload_generated_images_to_r2）已统一收敛到
# ai.media_generation，供 agentic 原生图像循环与本文件的
# execute_generate_image 共用，此处不再保留各写一套的副本。


def _format_image_api_error(api_name: str, status_code: int, detail: str = "", request_id: str = "", endpoint: str = "", model: str = "") -> str:
    parts = [f"❌ {api_name} 请求失败"]
    if status_code:
        parts.append(f"HTTP 状态：{status_code}")
    if model:
        parts.append(f"模型：{model}")
    if request_id:
        parts.append(f"Request ID：{request_id}")
    if detail:
        clean = detail.strip().replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in clean.split("\n") if line.strip()]
        clean = "<br/>".join(line for line in lines)
        if len(clean) > 800:
            clean = clean[:800] + "…"
        parts.append(f"详情：{clean}")
    return "<br/>".join(parts)


async def execute_generate_image(
    prompt: str,
    model: str,   # 移除默认值，让模型必须传
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
    num_images: int = 1,
    image_url: Optional[str] = None,
) -> str:
    """图像生成工具的统一入口（ImageTask 驱动）。

    重构说明（ImageTask）：本函数只负责**显式构造任务**与后处理——
      - 无参考图  -> ImageTask.generate（文生图）
      - 带参考图  -> ImageTask.edit（图生图/编辑；不再由请求层"看图猜端点"）
    请求经 protocols.images.dispatch_image_task 按模型协议分发：
      openai_images -> /images/{generations,edits}（ModelScope/XXTF 等）
      openai_chat   -> chat.completions + modalities（OpenRouter 图像模型；
                       未注册的 flux 等别名按 OpenRouter 兼容直连）
    """
    MODEL_ALIAS_MAP = {
        "flux-schnell": "black-forest-labs/flux-schnell",
        "flux-1.1-pro": "black-forest-labs/flux-1.1-pro",
        "flux-pro": "black-forest-labs/flux-pro",
        "sd-3.5": "stabilityai/stable-diffusion-3.5-large",
    }
    if model in MODEL_ALIAS_MAP:
        model = MODEL_ALIAS_MAP[model]

    model_info = SUPPORTED_MODELS.get(model)
    num_images = min(max(num_images, 1), 4)
    _protocol = _effective_image_protocol(model_info)
    used_endpoint = (
        ("/v1/images/edits" if image_url else "/v1/images/generations")
        if _protocol == "openai_images" else "/v1/chat/completions"
    )

    def _format_success_links(uploaded_urls: list[str], total_count: int) -> str:
        """生成图上传 R2 后的统一成功文案（部分上传失败时如实说明）。"""
        links = "\n".join(uploaded_urls)
        if len(uploaded_urls) == total_count:
            return f"✅ 已生成 {total_count} 张图片。\n图片链接：\n{links}"
        return f"✅ 已生成 {total_count} 张图片（部分图片上传失败）。\n图片链接：\n{links}"

    # ---- 显式构造 ImageTask：操作类型由调用入参决定，不再隐式推断 ----
    if image_url:
        task = ImageTask.edit(prompt, [image_url], model=model,
                              num_images=1, aspect_ratio=aspect_ratio, image_size=image_size,
                              meta={"image_config": {"aspect_ratio": aspect_ratio, "image_size": image_size}})
    else:
        task = ImageTask.generate(prompt, model=model,
                                  num_images=num_images, aspect_ratio=aspect_ratio, image_size=image_size,
                                  meta={"image_config": {"aspect_ratio": aspect_ratio, "image_size": image_size}})

    _api_name = f"{_get_images_api_display_name(model_info)} 图像接口" if model_info else "图像接口"
    try:
        result = await dispatch_image_task(task)
    except ImageRequestError as exc:
        return _format_image_api_error(
            api_name=_api_name,
            status_code=exc.status_code,
            detail=exc.detail,
            request_id=exc.request_id,
            endpoint=exc.endpoint or used_endpoint,
            model=model,
        )
    except Exception as e:
        logger.exception(f"execute_generate_image 异常: {e}")
        return _format_image_api_error(
            api_name=_api_name,
            status_code=getattr(e, "status", getattr(e, "status_code", 500)),
            detail=str(e),
            endpoint=used_endpoint,
            model=model,
        )

    if not result.images:
        if result.text or result.refusal:
            detail = result.refusal or result.text
            return f"⚠️ 模型未返回图片：{str(detail)[:200]}"
        return _format_image_api_error(
            api_name=_api_name,
            status_code=200,
            detail="接口返回成功，但未找到可下载的图片数据。",
            endpoint=result.endpoint or used_endpoint,
            model=model,
        )

    uploaded_urls = await _upload_generated_images_to_r2(result.images[:num_images])
    if not uploaded_urls:
        return "❌ 图片生成成功，但 R2 上传全部失败，请稍后重试。"
    return _format_success_links(uploaded_urls, len(result.images[:num_images]))


def _effective_image_protocol(model_info) -> str:
    """模型的有效图像协议（openai_images / openai_chat），未知厂商回落 openai_chat。"""
    if model_info is None:
        return "openai_chat"
    try:
        from config import get_effective_endpoint
        return get_effective_endpoint(model_info).protocol
    except Exception:
        return "openai_chat"


def _get_images_api_display_name(model_info) -> str:
    """提供商展示名（ModelScope / XXTF ...），用于错误提示文案。"""
    provider_key = (getattr(model_info, "provider", "") or "") if model_info else ""
    return provider_key or "图像"


async def execute_generate_video(
    prompt: str,
    model: str,
    duration: int = 5,
    chat_id: Optional[int] = None,
) -> str:
    """
    视频生成工具：复用 ai_handlers 中已有的 _request_agnes_video / _request_openrouter_video
    轮询逻辑，下载视频字节并上传 R2（拿到稳定的 HTTPS URL + 正确的 video/mp4 MIME）。

    与 _agentic_loop_native_video 的区别：
    - 这条路径是由 LLM 在任意对话模型下主动调用工具触发的；
    - 视频不会单独 sendRichMessage 发出，而是把 R2 URL 以结构化文本返回给上层，
      由 format_tool_result 在工具结果卡片里以 <figure><video> 内嵌渲染
      （Telegram Rich Message 支持视频作为独立 block 与文本同消息共存，
      参见 Rich Message Formatting Options 文档）。

    返回格式（供 format_tool_result 解析）：
        ✅ 已生成视频。
        视频链接：https://...
    """
    # 局部导入避免与 ai_handlers 产生循环依赖
    from ai_handlers import (
        _request_agnes_video,
        _request_openrouter_video,
    )

    if not prompt or not prompt.strip():
        return "❌ 视频生成失败：未提供提示词。"
    if not model:
        return "❌ 视频生成失败：未指定模型。"

    # 时长范围约束（与 _agentic_loop_native_video 保持一致）
    try:
        duration = int(duration)
    except Exception:
        logger.debug("execute_generate_video 内部忽略的异常", exc_info=True)
        duration = 5
    duration = max(3, min(duration, 30))

    model_info = SUPPORTED_MODELS.get(model)
    if not model_info:
        return f"❌ 未知视频模型：{model}"
    if not model_info.native_video:
        return f"❌ 模型 {model} 不支持视频生成。"

    provider = model_info.provider
    video_url: Optional[str] = None
    error: Optional[str] = None
    video_meta: Optional[dict] = None

    # chat action 语义（与 chat_actions.py 白名单约定一致）：
    # - 生成阶段（工具调用生视频模型的轮询/生成）→ record_video；
    #   生成动辄数十秒到数分钟，4 秒循环重发保证指示不闪断。
    # - 下载/上传阶段不触发任何 chat action：工具结果（视频 URL）是
    #   「AI 收到的信息」而非 bot 在向用户发送视频，upload_video 在此处
    #   属于错误语义。真正的发送发生在 utils.send_rich_html_message
    #   携带 <video> 的永久消息（那里自带 upload_video 钩子）。
    #   对比：原生视频模型路径（_agentic_loop_native_video）的模型输出
    #   就是最终要发给用户的视频，其下载/R2 上传全程属于发送动作，
    #   因此该路径保留 upload_video。
    # chat_id 可能为 None（极端调用路径）：chat_action_scope 会静默降级。
    if provider == "agnes":
        # chat_action_scope 运行时容忍 None（_validate 静默降级），cast 仅为对齐其 int 签名
        async with chat_action_scope(cast(int, chat_id), "record_video"):
            video_url, error, video_meta = await _request_agnes_video(
                prompt=prompt, duration=duration, model=model,
            )
    elif provider == "openrouter":
        # chat_action_scope 运行时容忍 None（_validate 静默降级），cast 仅为对齐其 int 签名
        async with chat_action_scope(cast(int, chat_id), "record_video"):
            video_url, error, video_meta = await _request_openrouter_video(
                prompt=prompt, duration=duration, model=model,
            )
    else:
        return f"❌ 暂不支持的视频提供商：{provider}"

    if error:
        return f"❌ 视频生成失败：{error}"
    if not video_url:
        return "❌ 视频生成失败：未获取到视频链接。"

    # 下载并上传 R2，确保 Telegram 拿到合法 video/mp4 MIME 的稳定 HTTPS URL
    # （Rich Message 媒体 block 仅支持 HTTP/HTTPS URL）
    final_video_url = video_url
    video_bytes_len = 0
    # 此处不触发 upload_video：下载与 R2 上传完成后，视频 URL 作为工具
    # 结果返回给模型（AI 收到信息），模型再决定如何在回复中使用它；
    # 真正的「bot 发送视频」发生在最终永久消息发送（见上方语义注释）。
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as dl_session:
            async with dl_session.get(video_url) as dl_resp:
                if dl_resp.status == 200:
                    video_bytes = await dl_resp.read()
                    video_bytes_len = len(video_bytes)
                    r2_key = f"generated/{uuid.uuid4().hex}.mp4"
                    r2_url = await upload_bytes_to_r2(video_bytes, r2_key, "video/mp4")
                    if r2_url:
                        final_video_url = r2_url
                    else:
                        logger.warning("[generate_video] R2 上传失败，回退原始 URL")
                else:
                    logger.warning(
                        "[generate_video] 视频下载非 200: status=%s url=%s",
                        dl_resp.status, str(video_url)[:200],
                    )
    except Exception:
        logger.exception("[generate_video] 视频下载/上传异常，回退原始 URL: %s", str(video_url)[:200])

    if video_bytes_len == 0 and isinstance(video_meta, dict):
        out_size = video_meta.get("perf_output_size")
        if isinstance(out_size, (int, float)):
            video_bytes_len = int(out_size)

    # 结构化返回：format_tool_result 解析“视频链接：”那一行构造内嵌 <figure><video>。
    # 不附带元数据 caption —— 工具结果卡片只展示视频本体，与图片工具行为对称。
    return (
        f"✅ 已生成视频。\n"
        f"视频链接：{final_video_url}"
    )
