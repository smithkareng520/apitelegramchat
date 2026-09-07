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
    # 统一图像生成入口：OpenAI Images 协议提供商（ModelScope / XXTF 等，
    # 见 media_generation.IMAGES_API_PROVIDERS）共用 _request_images_generations
    # 一个请求出口（端点 /v1/images/generations、鉴权、payload、参考图下载、
    # 任务轮询差异全部在 media_generation 内部处理）；本函数只负责调用 +
    # "响应 -> 图片字节 -> R2 -> 链接文本" 的通用后处理。
    # 其它提供商（openrouter 的 gemini 图像模型等）保留 chat/completions +
    # modalities 的原有逻辑。
    # 局部导入避免与 ai_handlers 的模块级循环依赖。
    from ai_handlers import (
        _get_images_api_display_name,
        _request_images_generations,
        _response_items_to_bytes,
        _upload_generated_images_to_r2,
        IMAGES_API_PROVIDERS,
    )
    MODEL_ALIAS_MAP = {
        "flux-schnell": "black-forest-labs/flux-schnell",
        "flux-1.1-pro": "black-forest-labs/flux-1.1-pro",
        "flux-pro": "black-forest-labs/flux-pro",
        "sd-3.5": "stabilityai/stable-diffusion-3.5-large",
    }
    if model in MODEL_ALIAS_MAP:
        model = MODEL_ALIAS_MAP[model]

    model_info = SUPPORTED_MODELS.get(model)
    provider = model_info.provider if model_info else "openrouter"
    num_images = min(max(num_images, 1), 4)

    def _format_success_links(uploaded_urls: list[str], total_count: int) -> str:
        """生成图上传 R2 后的统一成功文案（部分上传失败时如实说明）。"""
        links = "\n".join(uploaded_urls)
        if len(uploaded_urls) == total_count:
            return f"✅ 已生成 {total_count} 张图片。\n图片链接：\n{links}"
        return f"✅ 已生成 {total_count} 张图片（部分图片上传失败）。\n图片链接：\n{links}"

    # OpenAI Images 协议提供商：统一走 /v1/images/generations（不再按提供商各写一套）
    if model_info is not None and provider in IMAGES_API_PROVIDERS:
        api_display_name = _get_images_api_display_name(model_info)
        response_json, endpoint, error_detail, status_code, request_id = await _request_images_generations(
            model_info,
            prompt=prompt,
            image_urls=[image_url] if image_url else [],
            num_images=num_images,
            model=model,
            aspect_ratio=aspect_ratio,
        )
        used_endpoint = f"/v1{endpoint}"
        if response_json is None:
            return _format_image_api_error(
                api_name=f"{api_display_name} 图像接口",
                status_code=status_code,
                detail=error_detail,
                request_id=request_id,
                endpoint=used_endpoint,
                model=model,
            )
        try:
            image_bytes_list = await _response_items_to_bytes(response_json)
            if not image_bytes_list:
                return _format_image_api_error(
                    api_name=f"{api_display_name} 图像接口",
                    status_code=200,
                    detail="接口返回成功，但未找到可下载的图片数据。",
                    endpoint=used_endpoint,
                    model=model,
                )
            uploaded_urls = await _upload_generated_images_to_r2(image_bytes_list)
            if not uploaded_urls:
                return _format_image_api_error(
                    api_name=f"{api_display_name} 图像接口",
                    status_code=200,
                    detail="图片已生成，但上传 R2 全部失败。",
                    endpoint=used_endpoint,
                    model=model,
                )
            return _format_success_links(uploaded_urls, len(image_bytes_list))
        except Exception as e:
            logger.exception(f"{api_display_name} generate_image 异常: {e}")
            return _format_image_api_error(
                api_name=f"{api_display_name} 图像接口",
                status_code=getattr(e, "status", getattr(e, "status_code", 500)),
                detail=str(e),
                endpoint=used_endpoint,
                model=model,
            )

    # 其他厂商：保留原有 OpenRouter 兼容逻辑
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    if image_url:
        content_part: Any = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]
    else:
        content_part = prompt

    payload = {
        "model": model,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": content_part}],
        "image_config": {
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
        },
        "n": num_images,
        "provider": OPENROUTER_PROVIDER_PREFERENCES,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    if "not a valid model ID" in err_text and model != "google/gemini-2.5-flash-image":
                        logger.warning(f"模型 {model} 无效，回退到默认模型 google/gemini-2.5-flash-image")
                        return await execute_generate_image(
                            prompt=prompt,
                            model="google/gemini-2.5-flash-image",
                            aspect_ratio=aspect_ratio,
                            image_size=image_size,
                            num_images=num_images,
                            image_url=image_url,
                        )
                    return f"❌ 图像生成失败 (HTTP {resp.status}): {err_text[:200]}"

                data = await resp.json()
                msg = data.get("choices", [{}])[0].get("message", {})
                images = msg.get("images", [])

                if not images:
                    content = msg.get("content", "")
                    urls = re.findall(r'https?://[^\s]+\.(?:png|jpg|jpeg|gif)', content)
                    if urls:
                        images = [{"image_url": {"url": u}} for u in urls]

                if not images:
                    return "⚠️ 生成的响应中未找到图片。"

                image_bytes_list = []
                download_errors = []
                for idx, img_data in enumerate(images):
                    img_url = img_data.get("image_url", {}).get("url")
                    if not img_url:
                        continue
                    if img_url.startswith("data:image"):
                        try:
                            _, base64_data = img_url.split(",", 1)
                            img_bytes = base64.b64decode(base64_data)
                            image_bytes_list.append(img_bytes)
                            continue
                        except Exception as e:
                            logger.error(f"Base64 解码失败: {e}")
                            download_errors.append(f"图片 {idx+1} (Base64 解码失败)")
                            continue
                    elif img_url.startswith("http"):
                        max_retries = 3
                        downloaded = False
                        for attempt in range(max_retries):
                            try:
                                async with session.get(
                                    img_url,
                                    timeout=aiohttp.ClientTimeout(total=30),
                                    headers={"User-Agent": "Mozilla/5.0"}
                                ) as img_resp:
                                    if img_resp.status == 200:
                                        img_bytes = await img_resp.read()
                                        image_bytes_list.append(img_bytes)
                                        downloaded = True
                                        break
                            except Exception as e:
                                logger.warning(f"下载图片 {img_url} 异常: {e}")
                            await asyncio.sleep(1 + attempt)
                        if not downloaded:
                            download_errors.append(f"图片 {idx+1}")
                    else:
                        download_errors.append(f"图片 {idx+1} (不支持的 URL 格式)")

                if not image_bytes_list:
                    return f"⚠️ 图片生成成功，但下载全部失败。失败项: {', '.join(download_errors)}"

                # 与 Images 协议分支共用同一 R2 上传实现
                uploaded_urls = await _upload_generated_images_to_r2(image_bytes_list)

                if not uploaded_urls:
                    return "❌ 图片生成成功，但 R2 上传全部失败，请稍后重试。"

                return _format_success_links(uploaded_urls, len(image_bytes_list))

    except Exception as e:
        logger.error(f"execute_generate_image 异常: {e}", exc_info=True)
        return f"❌ 图像生成异常: {str(e)[:150]}"


# ========== 视频生成（工具版本） ==========
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
