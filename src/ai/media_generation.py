"""原生图片/视频生成模型的请求与响应解析。

从 ai_handlers.py 拆分而来，逻辑未做改动。

图像生成统一入口（OpenAI Images 协议）：
所有走 OpenAI Images 协议的提供商（ModelScope / XXTF 等）共用
:func:`_request_images_generations` 一个请求出口——端点选择、鉴权、
请求头、payload 构造、参考图下载、响应解析全部在此合并；厂商差异
（ModelScope 的异步任务轮询、xxtf 的官方 /images/edits multipart 形状）
作为该出口内部的分支处理，调用方（agentic 原生图像循环 / 工具图像
生成）不再按提供商各写一套。
"""
import asyncio
import json
import io
import aiohttp
import base64
import hashlib
from urllib.parse import urlsplit
from openai import AsyncOpenAI
import re
import mimetypes
import time
import uuid
from typing import Optional, Any, cast
from PIL import Image, UnidentifiedImageError
from collections.abc import Callable

from config import (
    OPENROUTER_API_KEY,
    AGNES_API_KEY,
    MODELSCOPE_API_KEY,
    ModelConfig,
    PROVIDERS,
    SUPPORTED_MODELS,
    get_effective_endpoint,
    get_sampling_params,
)
from utils import get_logger, strip_html_tags
from ai._constants import OPENROUTER_PROVIDER_PREFERENCES
from ai.error_formatting import _extract_error_details
from core.images import ImageRequestError, ImageTask, ImageTaskResult
from core.messages import ImageBlock, Message, TextBlock

logger = get_logger(__name__)

# =============================================================================
# 统一图像请求（OpenAI Images 协议 /v1/images/{generations,edits}）
# -----------------------------------------------------------------------------
# 走统一 Images 协议出口的提供商集合。判定依据是"该提供商的图像模型
# 支持 OpenAI Images 兼容端点"，而不是按模型逐个判断：
#   - modelscope: 文生图与图生图共用 /images/generations（用
#     X-ModelScope-Task-Type 头区分；ModelScope 无 /images/edits 端点）
#   - xxtf:       中转站标准 OpenAI Images 端点（gpt-image-2 等），按官方
#     语义分端点：文生图 -> JSON /images/generations；带参考图的编辑 ->
#     官方规范 /images/edits（multipart/form-data，image[] 字段，见
#     developers.openai.com "Create image edit"）。编辑请求绝不回退
#     /images/generations——官方 generations 端点不接受 image 参数，
#     中转站忽略该字段后编辑就变成纯文生图，HTTP 200 "假成功"但产出
#     一张与原图无关的新图（2026-09-08 生产事故）。edits 失败时明确
#     报错，绝不降级。另有生产鲁棒性：超大参考图先降采样再上传、
#     "请求体未完整/请重试"类瞬态 400 同形状自动重试
#     （详见 _request_openai_compat_image）
# 其它提供商（如 openrouter 的 gemini 图像模型）继续走
# chat/completions + modalities 路径，行为不变。
# =============================================================================


# =============================================================================
# 配置驱动的图像端点解析（公共出口）
# -----------------------------------------------------------------------------
# "这个图像模型到底 POST 到哪个 URL、参考图怎么传"由模型配置的 endpoint
# 字段决定，不再按提供商写 if-else 分支：
#   - 模型配置声明 endpoint="https://apihub.agnes-ai.com/v1/images/generations"
#     （URL 指向 /images/generations|edits）-> 视为完整图像端点：生成与
#     编辑（参考图内联 extra_body.image）共用该 URL（Agnes 形状）；
#   - endpoint 指向 API 根（未声明图像子路径）-> 按 OpenAI 官方形状推导
#     {endpoint}/images/{generations,edits}，编辑走独立 multipart
#     /images/edits（XXTF 等标准 OpenAI Images 中转行为完全不变）。
# =============================================================================
_INLINE_IMAGE_SIZE_TIERS = frozenset({"1K", "2K", "3K", "4K"})
# Agnes 官方支持的宽高比集合（size 档位 + ratio 配合使用；不在集合内的
# aspect_ratio 不发送，走网关默认 1:1）。
_INLINE_IMAGE_RATIOS = frozenset({"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"})
_EXACT_SIZE_PATTERN = re.compile(r"^\d{3,4}[xX]\d{3,4}$")
# 视频任务提交端点判定：URL 路径指向 /videos（子路径可带 query/尾斜）。
# endpoint 现为唯一端点字段（API 根也经由它表达），只有指向视频子路径的
# 声明才会被 _request_agnes_video 用作提交 URL，否则回退内置默认。
_VIDEO_SUBMIT_PATH_PATTERN = re.compile(r"/videos(?:[/?#]|$)")


class ImagesEndpointShape:
    """图像请求的端点与参考图形状（配置驱动解析结果，见
    :func:`resolve_images_endpoint_shape`）。"""

    __slots__ = ("generate_url", "edits_url", "edit_inline", "style")

    def __init__(self, *, generate_url: str, edits_url: str, edit_inline: bool, style: str) -> None:
        self.generate_url = generate_url
        self.edits_url = edits_url
        self.edit_inline = edit_inline
        self.style = style

    def __repr__(self) -> str:  # 调试便利
        return (
            f"ImagesEndpointShape(generate_url={self.generate_url!r}, "
            f"edits_url={self.edits_url!r}, edit_inline={self.edit_inline!r}, "
            f"style={self.style!r})"
        )


def resolve_images_endpoint_shape(model_info: Optional[ModelConfig]):
    """按模型配置的 endpoint 解析图像请求的端点与参考图形状（公共出口）。

    端点路由完全由唯一的 endpoint 字段支撑（URL 自身即形状声明）：
      - endpoint 指向 /images/generations|edits -> 视为完整图像端点：
        generate_url = edits_url = 该 URL，参考图内联 JSON（Agnes 形状，
        extra_body.image），生成/编辑/多图合成共用同一端点；
      - endpoint 为 API 根（不指向图像子路径）-> 按 OpenAI 官方形状推导：
        generate_url = {endpoint}/images/generations（JSON），
        edits_url    = {endpoint}/images/edits（multipart）。

    返回 :class:`ImagesEndpointShape`：
      - generate_url: 文生图（及内联编辑）应 POST 的完整 URL；
      - edits_url:    multipart 编辑应 POST 的完整 URL（inline 形状下不用）；
      - edit_inline:  True = 参考图内联进 generate_url 的 JSON 请求体
        （Agnes extra_body.image 风格）；
      - style:        展示用风格标签（"inline_images" / "multipart_edits"）。
    model_info 为 None 时按未声明处理（官方形状 + 无端点覆盖）。
    """
    ep = None
    if model_info is not None:
        try:
            ep = get_effective_endpoint(model_info)
        except Exception:
            ep = None
    ep_url = ((getattr(ep, "endpoint", "") or "").rstrip("/") if ep else "")

    # 完整图像端点判定：URL 路径指向 /images/generations|edits（与
    # protocols.images._IMAGES_ENDPOINT_PATH_PATTERN 同义；惰性导入
    # 避免模块级循环依赖）。
    is_full_images_endpoint = False
    if ep_url:
        try:
            from protocols.images import _IMAGES_ENDPOINT_PATH_PATTERN
            is_full_images_endpoint = bool(_IMAGES_ENDPOINT_PATH_PATTERN.search(ep_url))
        except Exception:
            is_full_images_endpoint = False

    if is_full_images_endpoint:
        # 形状 A：完整图像端点 -> 参考图内联 JSON，生成/编辑共用同一 URL。
        generate_url = ep_url
        edits_url = ""
        edit_inline = True
    elif ep_url:
        # 形状 B：API 根 -> OpenAI 官方形状推导，编辑走 multipart /images/edits。
        generate_url = f"{ep_url}/images/generations"
        edits_url = f"{ep_url}/images/edits"
        edit_inline = False
    else:
        generate_url = ""
        edits_url = ""
        edit_inline = False
    return ImagesEndpointShape(
        generate_url=generate_url,
        edits_url=edits_url,
        edit_inline=edit_inline,
        style=("inline_images" if edit_inline else "multipart_edits"),
    )


def _normalize_inline_size(image_size: str | None) -> str | None:
    """归一化 inline 风格的 size 参数（Agnes 档位 / 历史精确尺寸）。

    - 档位（1K/2K/3K/4K，大小写不敏感）-> 原样大写发送；
    - 历史精确尺寸（如 1024x768）-> 原样发送（网关自动标准化到最近档位）；
    - 其他/缺省 -> None（不发送 size，走网关默认）。
    """
    value = str(image_size or "").strip().upper()
    if value in _INLINE_IMAGE_SIZE_TIERS:
        return value
    if _EXACT_SIZE_PATTERN.match(value):
        # 历史精确尺寸按文档示例小写 x 归一（1024X768 -> 1024x768）
        return value.replace("X", "x")
    return None


def _normalize_inline_ratio(aspect_ratio: str | None) -> str | None:
    """归一化 inline 风格的 ratio 参数（仅在官方支持集合内才发送）。"""
    value = str(aspect_ratio or "").strip()
    return value if value in _INLINE_IMAGE_RATIOS else None



# ---- extra_params 安全合并（通用工具，两条图像链路共用）----
# 这些键由任务的结构化字段（model/prompt/参考图/尺寸/输出格式/modalities
# 等）派生，一旦被 extra_params 覆盖会破坏已有的正确性保证（如
# extra_body.image 被覆盖 = 参考图丢失、modalities 被覆盖 = 图片输出
# 请求变成纯文本输出）。调用方按各自请求形状传入相应的保留键集合，
# 命中的键丢弃并记录 warning，其余键原样透传——覆盖不了的官方参数一律
# 不拦，未来新增的可选参数（无需等代码发版）可以直接通过这个口子递进去。
def _merge_extra_params(
        target: dict[str, Any],
        extra_params: dict[str, Any] | None,
        *,
        reserved_top_keys: frozenset[str],
        nested_key: str = "",
        reserved_nested_keys: frozenset[str] = frozenset(),
        log_prefix: str = "[NativeImage]",
) -> dict[str, Any]:
    """把 extra_params 安全合并进 target（就地修改并返回）。

    - extra_params 顶层命中 reserved_top_keys 的键丢弃；
    - 若声明了 nested_key（如 "extra_body"），extra_params 顶层同名键
      （必须是 dict）与 target 已有的同名字典做浅合并（extra_params 一侧
      优先），其中命中 reserved_nested_keys 的子键同样丢弃；
    - 其余顶层键直接并入 target 顶层；
    - 非 dict / 空值原样跳过，不抛错——工具层传参异常不应打断整次生图。
    """
    if not extra_params or not isinstance(extra_params, dict):
        return target
    dropped: list[str] = []
    for key, value in extra_params.items():
        if nested_key and key == nested_key:
            if not isinstance(value, dict):
                continue
            merged_nested = dict(target.get(nested_key) or {})
            for nk, nv in value.items():
                if nk in reserved_nested_keys:
                    dropped.append(f"{nested_key}.{nk}")
                    continue
                merged_nested[nk] = nv
            if merged_nested:
                target[nested_key] = merged_nested
            continue
        if key in reserved_top_keys:
            dropped.append(key)
            continue
        target[key] = value
    if dropped:
        logger.warning(
            "%s extra_params 中的保留键已被忽略（会破坏任务已有的结构化字段，"
            "禁止覆盖）：%s",
            log_prefix, ", ".join(dropped),
        )
    return target


# inline_images（Agnes 式）形状的保留键：见 build_inline_images_payload。
_INLINE_PAYLOAD_RESERVED_TOP_KEYS = frozenset({"model", "prompt", "extra_body"})
_INLINE_PAYLOAD_RESERVED_EXTRA_BODY_KEYS = frozenset({"image"})


def _merge_extra_params_into_inline_payload(
        payload: dict[str, Any], extra_params: dict[str, Any] | None,
) -> dict[str, Any]:
    """把 extra_params 安全合并进 inline 形状 payload（_merge_extra_params 的薄封装）。

    保留键黑名单：顶层 model/prompt/extra_body（extra_body 走嵌套合并，
    不会被整体丢弃）；extra_body 内的 image（参考图，必须来自 image_url
    工具参数，不接受由 extra_params 篡改）。
    """
    return _merge_extra_params(
        payload, extra_params,
        reserved_top_keys=_INLINE_PAYLOAD_RESERVED_TOP_KEYS,
        nested_key="extra_body",
        reserved_nested_keys=_INLINE_PAYLOAD_RESERVED_EXTRA_BODY_KEYS,
    )


def build_inline_images_payload(
        *,
        model: str,
        prompt: str,
        size: str | None,
        ratio: str | None,
        image_data_urls: list[str] | tuple[str, ...] = (),
        extra_params: dict[str, Any] | None = None,
) -> dict:
    """构造 Agnes 式 inline 图像请求 payload（文生图 / 图生图 / 多图合成共用）。

    严格对齐 Agnes Images API 文档：
      - 顶层只放 model / prompt / size / ratio（+ return_base64）；
      - response_format 绝不放顶层：文生图要 Base64 用 return_base64=true，
        图生图/多图合成的参考图与输出格式统一进 extra_body；
      - 参考图（公共 URL 或 Data URI）放 extra_body.image 数组，多图合成
        传多张即得；
      - 不传 tags（图生图无需 tags: ["img2img"]）。

    image_data_urls 为空 = 文生图（return_base64=true 直取 b64_json，
    免一次签名 URL 下载往返）；非空 = 图生图/多图合成
    （extra_body.response_format="b64_json" 同理）。

    extra_params（可选）：模型通过 generate_image 工具显式传入的厂商专属
    附加参数，构造完标准字段后安全合并（见
    _merge_extra_params_into_inline_payload）——用于覆盖/补充官方文档中
    本函数未单列专属字段的可选参数（如显式要求 extra_body.response_format
    = "url"），不会影响未传该参数时的既有行为。
    """
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
    }
    if size:
        payload["size"] = size
    if ratio:
        payload["ratio"] = ratio
    if image_data_urls:
        payload["extra_body"] = {
            "image": list(image_data_urls),
            "response_format": "b64_json",
        }
    else:
        payload["return_base64"] = True
    return _merge_extra_params_into_inline_payload(payload, extra_params)


def _get_images_api_display_name(model_info: Optional[ModelConfig]) -> str:
    """返回提供商展示名（如 "ModelScope" / "XXTF"），用于错误提示文案。"""
    provider_key = getattr(model_info, "provider", "") or ""
    base = PROVIDERS.get(provider_key)
    return (getattr(base, "name", "") or provider_key or "图像").strip()


def _resolve_provider_api_key(api_key_env: str) -> str:
    """从 config 模块解析提供商 API Key（与 api_client._get_api_key 同语义）。

    从 config 模块变量（而非 os.environ）读取：scrub_environment() 清洗
    环境变量后应用仍能拿到 key。
    """
    if not api_key_env:
        return ""
    import config as app_config
    return str(getattr(app_config, api_key_env, "") or "")


def _normalize_image_url(image_url: str) -> str:
    return str(image_url or '').strip()


async def _download_reference_image_bytes(session: aiohttp.ClientSession, image_url: str) -> bytes | None:
    """下载并验证参考图字节；非真实图片（HTML 错误页等）绝不进入后续请求。

    - data: URL 解码后必须通过 Pillow 校验才返回。
    - http(s) 下载后检查 Content-Type 并用 Pillow 验证 magic bytes：
      HTTP 200 + text/html 的错误页曾经会伪装成 image/jpeg 进入编辑
      请求（参考图从未真正送达模型），这里从源头堵住。
    """
    if not image_url:
        return None
    if image_url.startswith("data:image"):
        try:
            _, base64_data = image_url.split(",", 1)
            raw = base64.b64decode(base64_data, validate=True)
            if _detect_valid_image(raw) is None:
                logger.warning("[NativeImage] data URL 内容不是可解析图片")
                return None
            return raw
        except Exception as e:
            logger.warning(f"[NativeImage] data URL 解码/校验失败: {e}")
            return None
    try:
        async with session.get(image_url, timeout=30) as resp:
            if resp.status != 200:
                logger.warning(f"[NativeImage] 下载参考图失败 {resp.status}: {image_url[:120]}")
                return None
            content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            raw = await resp.read()
            detected = _detect_valid_image(raw)
            if detected is None:
                logger.warning(
                    "[NativeImage] 参考 URL 返回的内容不是可解析图片: "
                    "content_type=%s bytes=%s url=%s",
                    content_type or "-", len(raw), image_url[:120],
                )
                return None
            detected_mime, _ext = detected
            if content_type and not content_type.startswith("image/"):
                # 内容能解析成图片但 header 不符：放行但留痕（部分对象存储
                # 会给 application/octet-stream，内容正确即可用）。
                logger.warning(
                    "[NativeImage] 参考 URL Content-Type 异常但内容可解析为 %s: "
                    "header=%s url=%s",
                    detected_mime, content_type, image_url[:120],
                )
            logger.debug(
                "[NativeImage] reference downloaded: bytes=%s mime=%s url=%s",
                len(raw), detected_mime, image_url[:120],
            )
            return raw
    except Exception as e:
        logger.warning(f"[NativeImage] 下载参考图异常: {e}")
    return None


def _bytes_to_data_url(img_bytes: bytes, source_url: str = '') -> str:
    """把真实图片字节转换成 data URL，MIME 以实际图片格式（magic bytes）为准。

    Pillow 能识别格式时以识别结果为准；识别失败才按 source_url 扩展名
    猜测（缺省 image/jpeg）。避免把 PNG 字节标成 image/jpeg 之类错配。
    """
    detected = _detect_valid_image(img_bytes)
    if detected is not None:
        mime, _ext = detected
    else:
        mime = 'image/jpeg'
        if source_url:
            guess, _ = mimetypes.guess_type(source_url.split('?', 1)[0])
            if guess and guess.startswith('image/'):
                mime = guess
    b64 = base64.b64encode(img_bytes).decode('ascii')
    return f"data:{mime};base64,{b64}"


async def _image_urls_to_data_urls(session: aiohttp.ClientSession, image_urls: list[str]) -> list[str]:
    """把调用方给的参考图（data URL / 公网 URL）统一转成已验证的 data URL 列表。

    ModelScope 与通用 OpenAI 兼容图像端点的参考图预处理共用本函数：
    下载失败 / 内容不是真实图片的参考图一律跳过（全部失败时由调用方
    返回 400），绝不把 HTML 错误页之类伪装成参考图送进编辑请求。
    """
    image_data_urls: list[str] = []
    for image_url in image_urls or []:
        normalized = _normalize_image_url(image_url)
        if not normalized:
            continue
        if normalized.startswith('data:image/'):
            try:
                decoded = _data_url_to_bytes(normalized)
                if not decoded:
                    logger.warning("[NativeImage] data URL 解码失败，跳过")
                    continue
                if _detect_valid_image(decoded[0]) is None:
                    logger.warning("[NativeImage] data URL 内容不是有效图片，跳过")
                    continue
                image_data_urls.append(normalized)
            except Exception as e:
                logger.warning("[NativeImage] data URL 验证失败: %s", e)
        elif normalized.startswith(('http://', 'https://')):
            img_bytes = await _download_reference_image_bytes(session, normalized)
            if img_bytes:
                image_data_urls.append(_bytes_to_data_url(img_bytes, normalized))
            else:
                logger.warning("[NativeImage] 下载参考图失败，跳过: %s", normalized[:80])
        else:
            logger.warning("[NativeImage] 无法识别的 image_url 格式，跳过: %s", normalized[:80])
    return image_data_urls


def _safe_json_parse_dict(body_text: str) -> dict | None:
    """把响应体解析为 dict；非 JSON / 非 dict 返回 None（debug 日志留痕）。"""
    try:
        parsed = json.loads(body_text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError as e:
        logger.debug(
            "[NativeImage] JSON parse failed: %s; body_preview=%r",
            e,
            (body_text or "")[:200],
        )
        return None
    except Exception:
        logger.debug("_safe_json_parse_dict 内部忽略的异常", exc_info=True)
        return None


# OpenAI Images 的 size 参数只认固定档位；把工具侧宽高比映射过去，
# 映射不到的（21:9 / 4:5 / 5:4）返回 None = 不发送，走提供商默认（auto）。
_OPENAI_SIZE_BY_ASPECT_RATIO = {
    "1:1": "1024x1024",
    "3:2": "1536x1024",
    "4:3": "1536x1024",
    "16:9": "1536x1024",
    "2:3": "1024x1536",
    "3:4": "1024x1536",
    "9:16": "1024x1536",
}


def _aspect_ratio_to_openai_size(aspect_ratio: str | None) -> str | None:
    if not aspect_ratio:
        return None
    return _OPENAI_SIZE_BY_ASPECT_RATIO.get(str(aspect_ratio).strip())


async def _upload_generated_images_to_r2(image_bytes_list: list[bytes]) -> list[str]:
    """统一后处理：验证真实图片后上传 R2，并使用正确的扩展名/MIME。"""
    from s3_utils import upload_bytes_to_r2
    uploaded_urls: list[str] = []
    for idx, img_bytes in enumerate(image_bytes_list or []):
        validated = _validate_image_bytes(img_bytes, source=f'r2_upload[{idx}]')
        if validated is None:
            continue
        mime_ext = _detect_valid_image(validated)
        if mime_ext is None:
            continue
        mime, ext = mime_ext
        key = f"generated/{uuid.uuid4().hex}_{idx}.{ext}"
        url = await upload_bytes_to_r2(validated, key, mime)
        if url:
            uploaded_urls.append(url)
        else:
            logger.warning("[NativeImage] 一张图片上传 R2 失败")
    return uploaded_urls


def _clean_prompt_for_image_model(prompt: str) -> str:
    """Strip UI metadata from prompts before image generation."""
    if not prompt:
        return ""
    text = prompt
    text = re.sub(r"^\s*📎\s*用户上传了图片「[^」]*」\s*[\r\n]*", "", text)
    text = re.sub(r"^\s*用户上传了图片「[^」]*」\s*[\r\n]*", "", text)
    return text.strip()


async def _request_modelscope_native_image(
        *,
        prompt: str,
        image_urls: list[str],
        num_images: int = 1,
        model: str = "",
) -> tuple[dict | None, str, str, int, str]:
    """
    ModelScope 专用分支（由统一出口 _request_images_generations 调度）：
    与通用 OpenAI 兼容实现共享端点形状（/images/generations）、参考图
    预处理（_image_urls_to_data_urls）与 JSON 解析（_safe_json_parse_dict），
    仅保留 ModelScope 特有的异步任务头与 /tasks/{task_id} 轮询。

    返回: (response_json, endpoint, error_detail, status_code, request_id)
    - 若服务直接返回图片结果，则 response_json 为最终结果。
    - 若先返回 task_id，则会自动轮询任务结果后再返回最终 JSON。
    """
    api_root = "https://api-inference.modelscope.cn/v1"
    # 注意：ModelScope 的图生图（image-to-image）同样走 /images/generations 端点，
    # /images/edits 在 ModelScope API-Inference 上不存在（返回 404 page not found）。
    # 区分文生图与图生图的是 X-ModelScope-Task-Type 头部，而非 URL 路径。
    # 参考实现: https://github.com/hujuying/ComfyUI-ModelScope-API/blob/main/modelscope_image_node.py
    endpoint = "/images/generations"
    request_url = f"{api_root}{endpoint}"

    # 注意：ModelScope 异步图像接口要求在 POST 与轮询 GET 上分别附带
    # X-ModelScope-Async-Mode / X-ModelScope-Task-Type 头部，否则任务虽然
    # 在 POST 时返回 task_id=SUCCEED，但 GET /tasks/{task_id} 会立即返回
    # {"errors":{"code":500,"message":"task not found"}, "task_status":"FAILED"}。
    task_type_post = "image-to-image-generation" if image_urls else "text-to-image-generation"
    base_headers = {
        "Authorization": f"Bearer {MODELSCOPE_API_KEY}",
    }
    post_headers = {
        **base_headers,
        "X-ModelScope-Async-Mode": "true",
        "X-ModelScope-Task-Type": task_type_post,
    }
    # 轮询接口使用的 Task-Type 与 POST 不同（参考实现均为 image_generation）
    poll_headers = {
        **base_headers,
        "X-ModelScope-Task-Type": "image_generation",
    }
    # 默认 headers 保留向后兼容（_post_or_get_json 内部已切换为显式传 headers）
    headers = base_headers
    timeout = aiohttp.ClientTimeout(total=300, connect=10, sock_read=180)

    # 预先清理 prompt，用于日志和后续 payload 构造
    pre_clean_prompt = _clean_prompt_for_image_model(prompt)
    logger.debug(
        "[NativeImage/ModelScope] request prepared: endpoint=%s model=%s prompt_len=%s(cleaned, raw=%s) image_count=%s prompt_preview=%r",
        endpoint,
        model,
        len(pre_clean_prompt or ""),
        len(prompt or ""),
        len(image_urls or []),
        (pre_clean_prompt or "")[:240],
    )

    def _body_preview(body_text: str, limit: int = 3000) -> str:
        body_text = body_text or ""
        return body_text[:limit]

    async def _post_or_get_json(
            session: aiohttp.ClientSession,
            method: str,
            url: str,
            *,
            data: Any = None,
            json_payload: dict | None = None,
            request_headers: dict | None = None,
            quiet: bool = False,
    ) -> tuple[dict | None, int, str, str]:
        effective_headers = request_headers if request_headers is not None else headers
        # aiohttp 不允许同时给 data 与 json：两者行为未定义，且若 json_payload
        # 不为 None，data 通常会被忽略。这里显式二选一，避免歧义。
        if json_payload is not None:
            async with session.request(method, url, headers=effective_headers, json=json_payload) as resp:
                return await _finalize_response(resp, method, url, quiet)
        else:
            async with session.request(method, url, headers=effective_headers, data=data) as resp:
                return await _finalize_response(resp, method, url, quiet)

    async def _finalize_response(resp: aiohttp.ClientResponse, method: str, url: str, quiet: bool) -> tuple[dict | None, int, str, str]:
        """Common response handling extracted from _post_or_get_json for clarity."""
        body_text = await resp.text()
        if not quiet:
            logger.debug(
                "[NativeImage/ModelScope] %s %s response: status=%s content_type=%s body_preview=%r",
                method,
                url.replace(api_root, ''),
                resp.status,
                resp.headers.get("Content-Type", ""),
                _body_preview(body_text),
            )
        request_id = ''
        parsed = _safe_json_parse_dict(body_text)
        if parsed and isinstance(parsed, dict):
            request_id = str(parsed.get('request_id') or parsed.get('requestId') or '').strip()
        if resp.status != 200:
            detail, req_id = _extract_error_details(body_text)
            return None, resp.status, detail or body_text, req_id or request_id
        if parsed is not None:
            if not quiet:
                logger.debug(
                    "[NativeImage/ModelScope] parsed JSON keys=%s",
                    list(parsed.keys())[:40],
                )
            return parsed, resp.status, '', request_id
        if not quiet:
            logger.debug(
                "[NativeImage/ModelScope] JSON parse failed; body_preview=%r",
                _body_preview(body_text),
            )
        return None, resp.status, body_text[:500], request_id

    def _extract_request_meta(payload: dict | None) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return '', ''
        request_id = str(payload.get('request_id') or payload.get('requestId') or '').strip()
        task_id = str(payload.get('task_id') or payload.get('taskId') or '').strip()
        return request_id, task_id

    async with aiohttp.ClientSession(timeout=timeout) as session:
        clean_prompt = pre_clean_prompt
        if image_urls:
            # 参考图预处理与通用 OpenAI 兼容端点共用同一实现
            # （data URL 直通，http(s) 下载后转 data URL，失败跳过）
            image_data_urls = await _image_urls_to_data_urls(session, image_urls)
            if not image_data_urls:
                return None, endpoint, "未能读取参考图片", 400, ""

            payload = {
                "model": model,
                "prompt": clean_prompt or "请根据参考图进行编辑。",
                "image_url": image_data_urls,
                "n": max(1, min(num_images, 4)),
            }
            response_json, status_code, error_detail, request_id = await _post_or_get_json(
                session, "POST", request_url, json_payload=payload, request_headers=post_headers, quiet=True
            )
            if response_json is None:
                logger.warning(
                    "[NativeImage/ModelScope] POST failed: status=%s detail=%s",
                    status_code, (error_detail or '')[:300],
                )
                return None, endpoint, error_detail, status_code, request_id
            _post_task_id = str(response_json.get('task_id') or '').strip()
            _post_task_status = str(response_json.get('task_status') or response_json.get('status') or '').upper()
            logger.debug(
                "[NativeImage/ModelScope] POST ok: status=200 task_status=%s task_id=%s",
                _post_task_status or 'UNKNOWN', _post_task_id or '-',
            )
        else:
            payload = {
                "model": model,
                "prompt": clean_prompt or "请生成一张图片。",
                "n": max(1, min(num_images, 4)),
            }
            response_json, status_code, error_detail, request_id = await _post_or_get_json(
                session,
                "POST",
                request_url,
                json_payload=payload,
                request_headers=post_headers,
                quiet=True,
            )
            if response_json is None:
                logger.warning(
                    "[NativeImage/ModelScope] POST failed: status=%s detail=%s",
                    status_code, (error_detail or '')[:300],
                )
                return None, endpoint, error_detail, status_code, request_id
            _post_task_id = str(response_json.get('task_id') or '').strip()
            _post_task_status = str(response_json.get('task_status') or response_json.get('status') or '').upper()
            logger.debug(
                "[NativeImage/ModelScope] POST ok: status=200 task_status=%s task_id=%s",
                _post_task_status or 'UNKNOWN', _post_task_id or '-',
            )

        direct_items = _extract_image_items(response_json)
        if direct_items:
            logger.debug("[NativeImage/ModelScope] direct image payload found, item_count=%s", len(direct_items))
            return response_json, endpoint, '', 200, _extract_request_meta(response_json)[0] or request_id

        task_status = str(response_json.get('task_status') or response_json.get('status') or '').upper()
        task_id = str(response_json.get('task_id') or response_json.get('taskId') or '').strip()
        request_id = _extract_request_meta(response_json)[0] or request_id
        if task_id:
            logger.debug(
                "[NativeImage/ModelScope] task response detected: task_status=%s task_id=%s",
                task_status or 'UNKNOWN',
                task_id,
            )
            # SSRF 防御：task_id 来自上游 API 响应，必须严格白名单后再拼到
            # URL：若 task_id 包含 `../` / `?` / host 注入字符串，会改写
            # 最终的 poll_url，把 bot 引导到任意主机。这里要求 task_id
            # 仅包含 `[A-Za-z0-9_-]`，长度 1-128，其他一律拒绝并直接返回失败。
            if not re.match(r'^[A-Za-z0-9_-]{1,128}$', task_id):
                logger.warning(
                    "[NativeImage/ModelScope] rejected suspicious task_id=%r",
                    task_id[:32],
                )
                return None, endpoint, "上游返回了非法的 task_id", 200, request_id
            poll_url = f"{api_root}/tasks/{task_id}"
            poll_deadline = time.monotonic() + 240
            poll_interval = 3.0
            poll_max_interval = 5.0
            poll_start = time.monotonic()
            last_poll_json = response_json
            not_found_count = 0
            poll_iter = 0
            await asyncio.sleep(1.5)
            IN_PROGRESS_STATES = {'PENDING', 'PROCESSING', 'RUNNING', 'QUEUED', 'QUEUING', 'STARTED'}

            while time.monotonic() < poll_deadline:
                poll_iter += 1
                poll_json, poll_status, poll_error, poll_request_id = await _post_or_get_json(
                    session, "GET", poll_url, request_headers=poll_headers, quiet=True
                )

                if poll_json is None:
                    if poll_status not in (404, 405):
                        logger.debug(
                            "[NativeImage/ModelScope] polling iter=%s failed: status=%s detail=%s",
                            poll_iter, poll_status, (poll_error or '')[:200],
                        )
                    await asyncio.sleep(poll_interval)
                    poll_interval = min(poll_max_interval, poll_interval + 0.5)
                    continue

                last_poll_json = poll_json
                request_id = poll_request_id or request_id
                poll_task_status = str(poll_json.get('task_status') or poll_json.get('status') or '').upper()
                elapsed = time.monotonic() - poll_start

                errors_obj = poll_json.get('errors')
                err_message = ''
                if isinstance(errors_obj, dict):
                    err_message = str(errors_obj.get('message') or '').strip()
                if not err_message:
                    err_message = str(
                        poll_json.get('message') or poll_json.get('error') or poll_json.get('detail') or ''
                    ).strip()

                if poll_task_status in IN_PROGRESS_STATES or not poll_task_status:
                    logger.debug(
                        "[NativeImage/ModelScope] polling iter=%s status=%s elapsed=%.1fs (next poll in %.1fs)",
                        poll_iter, poll_task_status or 'UNKNOWN', elapsed, poll_interval,
                    )
                    await asyncio.sleep(poll_interval)
                    poll_interval = min(poll_max_interval, poll_interval + 0.5)
                    continue

                poll_items = _extract_image_items(poll_json)
                if poll_items:
                    logger.debug(
                        "[NativeImage/ModelScope] polling succeeded: iter=%s status=%s item_count=%s elapsed=%.1fs",
                        poll_iter, poll_task_status, len(poll_items), elapsed,
                    )
                    return poll_json, endpoint, '', 200, request_id

                is_not_found = (
                        'task not found' in err_message.lower()
                        or err_message.lower().find('not found') >= 0
                        or poll_status == 404
                )

                if poll_task_status in {'FAILED', 'ERROR', 'CANCELLED', 'CANCELED'}:
                    if is_not_found and elapsed < 30:
                        not_found_count += 1
                        logger.debug(
                            "[NativeImage/ModelScope] transient 'task not found' (count=%s elapsed=%.1fs), retrying",
                            not_found_count, elapsed,
                        )
                        await asyncio.sleep(poll_interval)
                        poll_interval = min(poll_max_interval, poll_interval + 0.5)
                        continue
                    detail = err_message or '任务执行失败'
                    return None, endpoint, detail, 200, request_id

                logger.debug(
                    "[NativeImage/ModelScope] polling iter=%s status=%s but no images extracted, elapsed=%.1fs",
                    poll_iter, poll_task_status, elapsed,
                )
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_max_interval, poll_interval + 0.5)

            logger.debug(
                "[NativeImage/ModelScope] task polling timed out after %.1fs (iters=%s); returning last task JSON keys=%s",
                time.monotonic() - poll_start, poll_iter,
                list(last_poll_json.keys())[:40] if isinstance(last_poll_json, dict) else type(last_poll_json).__name__,
            )
            return last_poll_json, endpoint, '', 200, request_id

        return response_json, endpoint, '', 200, request_id


# ---- 通用 OpenAI 兼容实现的共享辅助（edits multipart / generations JSON 两路共用）----

# ---- 生产鲁棒性辅助：瞬态 400 重试 / 超大参考图压缩 ----
#
# 历史教训（2026-09-08 生产事故）：本文件曾有"/images/edits 失败后回退
# /images/generations + image 字段""路由级 404/405 按 base_url 缓存 TTL"
# 的兼容逻辑。官方 generations 端点不接受 image 参数，中转站忽略该字段
# 后编辑请求被当纯文生图执行——HTTP 200 "成功"，实际产出一张与原图
# 无关的全新图片（用户要求"移除行人"，结果场景/风格整体重绘）。该
# fallback 及其路由能力缓存已彻底删除：编辑请求宁可明确失败，也绝不
# 假成功。


# 瞬态 400：中转站明确提示"请求体未完整/请重试"类传输层错误（生产实测
# xxtf 返回 400 INCOMPLETE_REQUEST_BODY "请求体未完整到达，请重试"，同一
# 请求稍后重试即成功）。这类错误属于 invalid_request_error 且请求未被
# 服务端受理（无计费），同形状重试一次是安全的；内容安全/鉴权类 400
# 不在此列。
_TRANSIENT_400_HINTS = (
    "incomplete_request_body",
    "请求体未完整",
    "请重试",
    "please retry",
    "please try again",
    "try again later",
)
_TRANSIENT_RETRY_BACKOFF_SECONDS = 1.5


def _is_transient_400(status_code: int, error_detail: str) -> bool:
    """判定是否为"中转站明确要求重试"类瞬态 400（非参数/安全语义错误）。"""
    if status_code != 400:
        return False
    body = (error_detail or "").lower()
    return any(hint in body for hint in _TRANSIENT_400_HINTS)


async def _post_images_with_retry(
        session: aiohttp.ClientSession,
        url: str,
        *,
        endpoint: str,
        log_prefix: str,
        headers: dict | None = None,
        json_payload: dict | None = None,
        form_factory: Callable[[], aiohttp.FormData] | None = None,
) -> tuple[dict | None, str, str, int, str]:
    """Images 协议 POST 共用出口：响应处理 + 瞬态 400 同形状重试一次。

    json_payload（JSON 形状）与 form_factory（multipart 形状，传入返回
    新建 :class:`aiohttp.FormData` 的可调用对象）二选一。multipart 重试
    必须重建 FormData——bytes 字段的流游标消费后不可复用。
    返回形状与 :func:`_finalize_images_response` 一致。
    """
    result: tuple[dict | None, str, str, int, str] = (None, endpoint, "", 0, "")
    form = form_factory() if form_factory is not None else None
    for attempt in (1, 2):
        async with session.post(
            url, headers=headers, json=json_payload, data=form
        ) as resp:
            result = await _finalize_images_response(resp, endpoint, log_prefix)
        parsed, _, detail, status_code, _req_id = result
        if parsed is not None or not _is_transient_400(status_code, detail):
            return result
        if attempt == 1:
            logger.warning(
                "%s %s 瞬态失败 (status=%s detail=%r)，%.1fs 后同形状重试一次",
                log_prefix, endpoint, status_code, (detail or "")[:120],
                _TRANSIENT_RETRY_BACKOFF_SECONDS,
            )
            await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_SECONDS)
            form = form_factory() if form_factory is not None else None
    return result


# 超大参考图压缩：参考图塞进 multipart（edits）或 JSON（回退形状）时，
# 数 MB 的原图会造出巨大请求体，部分中转站读不满直接 400"请求体未完整
# 到达"。参考图对 gpt-image 系列只是编辑依据，长边 <=2048 / ~3MB 已足够
# （官方输出分辨率本身集中在 1024/1536 档）。
_REF_IMAGE_MAX_BYTES = 3 * 1024 * 1024
_REF_IMAGE_MAX_SIDE = 2048
# JPEG 降档兜底：极端高熵图（如噪声/密集纹理）在 2048/q88 下仍可能超阈值，
# 依次降质量，仍未达标则长边降到 1536 再压。
_REF_IMAGE_JPEG_QUALITIES = (88, 72, 56)
_REF_IMAGE_FALLBACK_SIDE = 1536


def _encode_jpeg_bytes(img: Any, quality: int) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _shrink_reference_bytes(img_bytes: bytes, mime: str) -> tuple[bytes, str]:
    """超过 3MB 的参考图降采样（长边 2048；有 alpha 先保 PNG，仍过大则白底转 JPEG）。

    JPEG 质量降档 88 -> 72 -> 56，仍未达标则长边再降到 1536。
    Pillow 不可用 / 图片解码失败 / 压缩无收益时原样返回，绝不阻断主流程。
    """
    if len(img_bytes) <= _REF_IMAGE_MAX_BYTES:
        return img_bytes, mime
    try:
        from io import BytesIO
        from PIL import Image  # Pillow 已在 requirements.txt（Pillow==11.1.0）
        img: Image.Image = Image.open(BytesIO(img_bytes))
        img.load()
        width, height = img.size
        scale = max(width, height) / _REF_IMAGE_MAX_SIDE
        if scale > 1:
            img = img.resize(
                (max(1, round(width / scale)), max(1, round(height / scale))),
                Image.LANCZOS,  # type: ignore[attr-defined]  # Pillow 以 setattr 动态挂载的兼容别名，运行时存在
            )
        has_alpha = img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        )
        if has_alpha:
            png_buf = BytesIO()
            img.save(png_buf, format="PNG", optimize=True)
            if png_buf.tell() <= _REF_IMAGE_MAX_BYTES:
                out_bytes = png_buf.getvalue()
                if len(out_bytes) < len(img_bytes):
                    logger.info(
                        "[NativeImage/OpenAICompat] 超大参考图已压缩: %d -> %d bytes (%s)",
                        len(img_bytes), len(out_bytes), "image/png",
                    )
                    return out_bytes, "image/png"
            # PNG 仍过大：白底合成 alpha 后转入 JPEG 降档
            rgba = img.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.split()[-1])
            img = flattened
        out_bytes = b""
        for quality in _REF_IMAGE_JPEG_QUALITIES:
            out_bytes = _encode_jpeg_bytes(img, quality)
            if len(out_bytes) <= _REF_IMAGE_MAX_BYTES:
                break
        if len(out_bytes) > _REF_IMAGE_MAX_BYTES and max(img.size) > _REF_IMAGE_FALLBACK_SIDE:
            w, h = img.size
            scale2 = max(w, h) / _REF_IMAGE_FALLBACK_SIDE
            img2 = img.resize(
                (max(1, round(w / scale2)), max(1, round(h / scale2))),
                Image.LANCZOS,  # type: ignore[attr-defined]  # Pillow 以 setattr 动态挂载的兼容别名，运行时存在
            )
            out_bytes = _encode_jpeg_bytes(img2, _REF_IMAGE_JPEG_QUALITIES[-1])
        if out_bytes and len(out_bytes) < len(img_bytes):
            logger.info(
                "[NativeImage/OpenAICompat] 超大参考图已压缩: %d -> %d bytes (%s)",
                len(img_bytes), len(out_bytes), "image/jpeg",
            )
            return out_bytes, "image/jpeg"
    except Exception as e:  # Pillow 缺失 / 图片损坏等：原样返回，不阻断主流程
        logger.debug("[NativeImage/OpenAICompat] 参考图压缩跳过: %s", e)
    return img_bytes, mime


def _shrink_data_url(data_url: str) -> str:
    """对单张 data URL 参考图应用 :func:`_shrink_reference_bytes`。"""
    decoded = _data_url_to_bytes(data_url)
    if not decoded:
        return data_url
    img_bytes, mime = decoded
    new_bytes, new_mime = _shrink_reference_bytes(img_bytes, mime)
    if new_bytes is img_bytes:
        return data_url
    return "data:{};base64,{}".format(
        new_mime, base64.b64encode(new_bytes).decode("ascii")
    )


def _data_url_to_bytes(data_url: str) -> tuple[bytes, str] | None:
    """把 data:image/... URL 解码为 (字节, mime)；失败返回 None。"""
    if not data_url or not data_url.startswith("data:image/"):
        return None
    try:
        header, b64_data = data_url.split(",", 1)
        mime = "image/png"
        m = re.match(r"data:([^;,]+)", header)
        if m and m.group(1).strip():
            mime = m.group(1).strip().lower()
        return base64.b64decode(b64_data), mime
    except Exception as e:
        logger.warning("[NativeImage/OpenAICompat] data URL 解码失败: %s", e)
        return None


async def _finalize_images_response(
        resp: aiohttp.ClientResponse,
        endpoint: str,
        log_prefix: str,
) -> tuple[dict | None, str, str, int, str]:
    """Images 协议两种 POST（JSON / multipart）共用的响应处理。

    返回: (response_json, endpoint, error_detail, status_code, request_id)
    """
    body_text = await resp.text()
    logger.debug(
        "%s POST response: status=%s content_type=%s body_preview=%r",
        log_prefix,
        resp.status,
        resp.headers.get("Content-Type", ""),
        body_text[:800],
    )
    if resp.status != 200:
        detail, req_id = _extract_error_details(body_text)
        candidate = detail or body_text
        if candidate.lstrip().startswith("<"):
            # 网关/中转的 HTML 错误页（如 Python http.server 默认 404 页）：
            # 提取纯文本摘要，避免整页 HTML 进入错误提示与日志
            detail = re.sub(r"\s+", " ", strip_html_tags(candidate)).strip()[:300]
        logger.warning(
            "%s POST failed: status=%s detail=%s",
            log_prefix, resp.status, (detail or body_text)[:300],
        )
        return None, endpoint, detail or body_text[:500], resp.status, req_id or ''
    parsed = _safe_json_parse_dict(body_text)
    if parsed is None:
        return None, endpoint, body_text[:500], resp.status, ''
    return parsed, endpoint, '', resp.status, ''


async def _request_openai_compat_image(
        model_info: Optional[ModelConfig],
        *,
        prompt: str,
        image_urls: list[str],
        num_images: int = 1,
        model: str = "",
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        extra_params: dict[str, Any] | None = None,
) -> tuple[dict | None, str, str, int, str]:
    """通用 OpenAI Images 兼容实现（端点与形状由 endpoint 驱动，公共出口）。

    端点与参考图形状经 :func:`resolve_images_endpoint_shape` 从模型配置
    的 endpoint 解析，支持两种 API 形状。新增一个走不同子端点/形状的模型
    只需在 config 里声明 endpoint（完整图像端点或 API 根），无需在本函数
    新建分支：

    形状 A —— inline_images（Agnes 式：生成/编辑/多图合成共用同一端点）：
      - 全部请求 POST JSON 到配置声明的 generate_url（如
        ``https://apihub.agnes-ai.com/v1/images/generations``）；
      - payload 由 :func:`build_inline_images_payload` 构造：顶层
        model/prompt/size(档位 1K-4K 或历史精确尺寸)/ratio(官方 8 种)，
        参考图内联 ``extra_body.image``（多图合成传多张即得），输出格式
        ``extra_body.response_format`` / ``return_base64``——绝不在顶层放
        response_format、绝不传 tags（严格对齐 Agnes 文档要求）；
      - 文生图 return_base64=true 直取 b64_json；图生图/多图合成
        extra_body.response_format="b64_json"，省一次签名 URL 下载往返。

    形状 B —— multipart_edits（OpenAI 官方语义，endpoint 为 API 根时推导，
    XXTF 等标准中转，行为不变）：
    - 无参考图（文生图）: POST JSON ``{endpoint}/images/generations``
      （官方 "Create image"，仅接受文本 prompt，无 image 参数）。
    - 有参考图（图生图/编辑）: 官方独立端点 "Create image edit"
      ``{endpoint}/images/edits``，且必须是 multipart/form-data——参考图以
      重复的 ``image[]`` 文件字段逐张上传（gpt-image 系列支持多张），
      文本字段为 model / prompt / n [/ size]：

          curl https://api.openai.com/v1/images/edits \
            -F "model=gpt-image-1.5" -F "image[]=@a.png" -F "image[]=@b.png" \
            -F 'prompt=...'

      ⚠️ 编辑请求绝不回退 ``/images/generations``：官方 generations 端点
      并不接受 image 参数，把参考图塞进 JSON 发给 generations 只会被部分
      中转站静默忽略——上游返回 200，实际执行的是纯文生图，用户看到的
      "编辑结果"与原图毫无关系（2026-09-08 生产事故：要求"移除行人"，
      结果场景/风格整体重绘）。因此：
        1. 参考图先经真实图片校验（Pillow magic bytes），不合法直接 400；
        2. 严格 POST multipart /images/edits；
        3. edits 失败（含 404/405 路由未实现）→ 明确报错，绝不降级。

    extra_params：模型通过 generate_image 工具显式传入的厂商专属附加参数
    （详见 core.images.ImageTask.extra_params）。仅 inline_images 形状
    （Agnes 式）会安全合并进请求体（见 build_inline_images_payload /
    _merge_extra_params_into_inline_payload）；multipart_edits 形状
    （XXTF 等标准 OpenAI Images 中转）严格对齐官方 multipart 字段集
    （model/prompt/n/size/image[]），不接受任意透传字段，该参数在此形状
    下被忽略。

    共同鲁棒性："请求体未完整/请重试"类瞬态 400 同形状自动重试一次；
    超大参考图（>3MB）先降采样再上传，避免中转站读不满请求体。
    URL 解析：模型声明的完整图像端点（endpoint 指向
    /images/generations|edits）原样使用（参考图内联，生成/编辑共用）；
    否则按 API 根推导 {endpoint}/images/{generations,edits} 官方形状。
    鉴权：``Bearer {api_key_env 解析出的 key}``；provider 级
    default_headers（如 XXTF 的浏览器 UA）一并下发。multipart 请求
    不手动设置 Content-Type（aiohttp 自动生成带 boundary 的头）。
    响应：两类形状同为 ``{data: [{url} | {b64_json}]}``，图片提取复用
    :func:`_extract_image_items`（gpt-image 系列固定回 b64_json，同样覆盖）。

    返回: (response_json, endpoint, error_detail, status_code, request_id)
    endpoint 为实际使用的相对路径（"/images/edits" 或 "/images/generations"），
    调用方拼 /v1 前缀用于展示。
    """
    gen_endpoint = "/images/generations"
    edits_path = "/images/edits"
    log_prefix = "[NativeImage/OpenAICompat]"
    ep = get_effective_endpoint(model_info)
    shape = resolve_images_endpoint_shape(model_info)
    api_key_env = getattr(ep, "api_key_env", "") or ""
    api_key = _resolve_provider_api_key(api_key_env)
    if not shape.generate_url:
        return None, gen_endpoint, f"提供商 {ep.name!r} 未配置 endpoint", 400, ""
    if not api_key:
        return None, gen_endpoint, f"缺少 API Key: {api_key_env}，请设置环境变量", 401, ""

    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        **(getattr(ep, "default_headers", None) or {}),
    }
    json_headers = {**auth_headers, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=300, connect=10, sock_read=180)

    clean_prompt = _clean_prompt_for_image_model(prompt)
    n = max(1, min(num_images, 4))
    # endpoint 变量贯穿 except 分支用于错误展示，先按即将使用的路径初始化。
    endpoint = gen_endpoint

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # ============ 形状 A：inline_images（Agnes 式，同一端点）============
            if shape.edit_inline:
                inline_size = _normalize_inline_size(image_size)
                inline_ratio = _normalize_inline_ratio(aspect_ratio)
                if not image_urls:
                    # 文生图：JSON，return_base64=true 直取 b64_json
                    payload = build_inline_images_payload(
                        model=model,
                        prompt=clean_prompt or "请生成一张图片。",
                        size=inline_size,
                        ratio=inline_ratio,
                        extra_params=extra_params,
                    )
                    logger.debug(
                        "%s [inline] request prepared: provider=%s url=%s model=%s "
                        "size=%s ratio=%s prompt_len=%s(cleaned, raw=%s) prompt_preview=%r",
                        log_prefix, ep.name, shape.generate_url, model,
                        inline_size, inline_ratio,
                        len(clean_prompt or ""), len(prompt or ""),
                        (clean_prompt or "")[:240],
                    )
                    return await _post_images_with_retry(
                        session, shape.generate_url,
                        endpoint=gen_endpoint, log_prefix=log_prefix,
                        headers=json_headers, json_payload=payload,
                    )

                # 图生图 / 多图合成：参考图下载校验后内联进 extra_body.image
                image_data_urls = await _image_urls_to_data_urls(session, image_urls)
                if not image_data_urls:
                    return None, gen_endpoint, "未能读取参考图片", 400, ""
                # 超大参考图先降采样（与 multipart 形状同一套压缩鲁棒性）
                image_data_urls = [_shrink_data_url(u) for u in image_data_urls]
                payload = build_inline_images_payload(
                    model=model,
                    prompt=clean_prompt or "请根据参考图进行编辑。",
                    size=inline_size,
                    ratio=inline_ratio,
                    image_data_urls=image_data_urls,
                    extra_params=extra_params,
                )
                logger.debug(
                    "%s [inline] edit prepared: provider=%s url=%s model=%s size=%s ratio=%s "
                    "prompt_len=%s(cleaned, raw=%s) image_count=%s prompt_preview=%r",
                    log_prefix, ep.name, shape.generate_url, model,
                    inline_size, inline_ratio,
                    len(clean_prompt or ""), len(prompt or ""),
                    len(image_data_urls), (clean_prompt or "")[:240],
                )
                return await _post_images_with_retry(
                    session, shape.generate_url,
                    endpoint=gen_endpoint, log_prefix=log_prefix,
                    headers=json_headers, json_payload=payload,
                )

            # ========= 形状 B：multipart_edits（OpenAI 官方语义，不变）=========
            # ---------------- 文生图：JSON /images/generations ----------------
            if not image_urls:
                endpoint = gen_endpoint
                size = _aspect_ratio_to_openai_size(aspect_ratio)
                payload: dict[str, Any] = {
                    "model": model,
                    "prompt": clean_prompt or "请生成一张图片。",
                    "n": n,
                }
                if size:
                    payload["size"] = size
                logger.debug(
                    "%s request prepared: provider=%s endpoint=%s model=%s prompt_len=%s(cleaned, raw=%s) prompt_preview=%r",
                    log_prefix, ep.name, endpoint, model,
                    len(clean_prompt or ""), len(prompt or ""),
                    (clean_prompt or "")[:240],
                )
                return await _post_images_with_retry(
                    session, shape.generate_url,
                    endpoint=endpoint, log_prefix=log_prefix,
                    headers=json_headers, json_payload=payload,
                )

            # ------------- 图生图/编辑：严格使用官方 /images/edits -------------
            #
            # 重要：/images/generations + {"image": ...} 并不等价于官方
            # /images/edits 协议。中转站很可能直接忽略未知字段，把编辑请求
            # 当成纯文生图执行——用户要求"只删行人"，结果场景/风格整体
            # 重绘（2026-09-08 生产事故：HTTP 200 "假成功"，原图从未送达
            # 模型）。因此：
            #   有参考图 -> 只能 POST /images/edits multipart；
            #   edits 失败 -> 明确报错；
            #   绝不 fallback 到 /images/generations（宁可失败，不假成功）。
            endpoint = edits_path
            image_data_urls = await _image_urls_to_data_urls(session, image_urls)
            if not image_data_urls:
                return None, endpoint, "未能读取参考图片", 400, ""
            # 超大参考图先降采样：数 MB 的 body 塞进 multipart 会被部分
            # 中转站截断（生产实测 xxtf 400 INCOMPLETE_REQUEST_BODY）
            image_data_urls = [_shrink_data_url(u) for u in image_data_urls]

            logger.debug(
                "%s request prepared: provider=%s endpoint=%s model=%s prompt_len=%s(cleaned, raw=%s) image_count=%s prompt_preview=%r",
                log_prefix, ep.name, endpoint, model,
                len(clean_prompt or ""), len(prompt or ""),
                len(image_data_urls), (clean_prompt or "")[:240],
            )
            # 预解码参考图（一次解码，multipart 构造可重复执行——瞬态重试
            # 时需重建 FormData，bytes 字段的流游标不可复用）；每张都过
            # Pillow 校验，非真实图片拒绝发送。
            decoded_refs: list[tuple[int, bytes, str, str]] = []
            for idx, data_url in enumerate(image_data_urls):
                decoded = _data_url_to_bytes(data_url)
                if not decoded:
                    continue
                img_bytes, mime = decoded
                detected = _detect_valid_image(img_bytes)
                if detected is None:
                    logger.warning(
                        "%s reference[%s] 不是有效图片，拒绝发送",
                        log_prefix, idx,
                    )
                    continue
                detected_mime, ext = detected
                decoded_refs.append((idx, img_bytes, detected_mime, ext))
            if not decoded_refs:
                return None, endpoint, "未能读取参考图片", 400, ""

            def _build_edits_form() -> aiohttp.FormData:
                """严格构造 OpenAI Images Edit multipart 请求（官方形状）。"""
                form = aiohttp.FormData()
                form.add_field("model", str(model))
                form.add_field("prompt", clean_prompt or "请根据参考图进行编辑。")
                form.add_field("n", str(n))
                size = _aspect_ratio_to_openai_size(aspect_ratio)
                if size:
                    form.add_field("size", str(size))
                for idx, img_bytes, mime, ext in decoded_refs:
                    form.add_field(
                        "image[]", img_bytes,
                        filename=f"reference_{idx}.{ext}", content_type=mime,
                    )
                # 不泄露图片内容的调试留痕：证明"HTTP 请求 body 里真的有
                # 图"，而不只是"程序准备了一张图"（bytes/sha256 可逐张核对）。
                logger.debug(
                    "%s multipart prepared: endpoint=%s model=%s prompt_len=%s "
                    "reference_count=%s refs=%s",
                    log_prefix, edits_path, model,
                    len(clean_prompt or ""),
                    len(decoded_refs),
                    [
                        {
                            "index": idx,
                            "bytes": len(img_bytes),
                            "mime": mime,
                            "sha256": hashlib.sha256(img_bytes).hexdigest()[:16],
                        }
                        for idx, img_bytes, mime, _ext in decoded_refs
                    ],
                )
                return form

            parsed, used_endpoint, detail, status_code, req_id = (
                await _post_images_with_retry(
                    session, shape.edits_url,
                    endpoint=endpoint, log_prefix=log_prefix,
                    headers=auth_headers, form_factory=_build_edits_form,
                )
            )
            if parsed is not None:
                logger.info(
                    "%s edit accepted: endpoint=%s model=%s reference_count=%s",
                    log_prefix, used_endpoint, model, len(decoded_refs),
                )
                return parsed, used_endpoint, '', status_code, req_id

            # 编辑失败绝不能伪装成文生图成功。特别是 404/405/501 只代表
            # 上游没有实现 /images/edits，并不代表 /images/generations 可以
            # 完成编辑——把编辑降级成文生图会产生最恶劣的"假成功"。
            logger.error(
                "%s edit rejected: endpoint=%s status=%s detail=%r; NO fallback to /images/generations",
                log_prefix, used_endpoint, status_code, (detail or "")[:300],
            )
            return (
                None,
                used_endpoint,
                (
                    f"图像编辑端点 {shape.edits_url} 不可用或拒绝请求；"
                    "为避免把编辑误降级成全新图片生成，程序不会回退到 "
                    f"/images/generations。上游状态={status_code}，"
                    f"详情={(detail or '无').strip()[:240]}"
                ),
                status_code,
                req_id,
            )
    except asyncio.TimeoutError:
        logger.warning("%s request timeout after %ss", log_prefix, timeout.total)
        return None, endpoint, "图像请求超时", 504, ""
    except Exception as e:
        logger.exception("%s request exception", log_prefix)
        return None, endpoint, str(e)[:500], 500, ""


async def _request_images_generations(
        model_info: Optional[ModelConfig],
        *,
        prompt: str,
        image_urls: list[str],
        num_images: int = 1,
        model: str = "",
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        extra_params: dict[str, Any] | None = None,
) -> tuple[dict | None, str, str, int, str]:
    """统一图像请求出口：所有 OpenAI Images 协议提供商共用这一个函数。

    调用方（_agentic_loop_native_image / execute_generate_image）不再按
    提供商各写一套请求逻辑，只拿到统一形状的返回值后做各自的呈现：

    - modelscope -> _request_modelscope_native_image（异步任务轮询特化，
      其协议差异是厂商级而非模型级，保留内部分支；该厂商官方接口未公开
      额外可选参数，extra_params 在此分支被忽略）
    - 其它 -> _request_openai_compat_image（端点与参考图形状完全由模型/
      厂商配置驱动：inline_images 如 Agnes 生成/编辑/多图合成共用同一
      声明端点，extra_params 在此形状下安全合并进请求体；multipart_edits
      如 XXTF 走官方 /images/{generations,edits}，失败即报错，绝不回退
      generations 形状，该形状同样不接受任意透传字段）

    返回: (response_json, endpoint, error_detail, status_code, request_id)
    endpoint 为实际使用的相对路径（"/images/generations" 或
    "/images/edits"），调用方拼 /v1 前缀用于展示。
    """
    provider = (getattr(model_info, "provider", "") or "").strip().lower()
    if provider == "modelscope":
        return await _request_modelscope_native_image(
            prompt=prompt,
            image_urls=image_urls,
            num_images=num_images,
            model=model,
        )
    return await _request_openai_compat_image(
        model_info,
        prompt=prompt,
        image_urls=image_urls,
        num_images=num_images,
        model=model,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        extra_params=extra_params,
    )


def _extract_image_items(response_json: dict, max_items: int = 4) -> list[dict]:
    """严格从图像结果字段提取图片 URL/base64，避免把普通 URL 当成图片。

    只信任显式的图片承载字段：data/output_images/images/results/choices/output、
    message.images，以及 content 中 type=image/image_url 的部分；不会再对整个
    response 做“发现 https URL 就当图片”的全量递归。
    """
    if not isinstance(response_json, dict):
        return []

    limit = max(1, min(int(max_items or 1), 20))
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _push_url(url: str) -> None:
        value = str(url or '').strip()
        if not value or not value.startswith(('http://', 'https://', 'data:image/')):
            return
        sig = ('url', value)
        if sig not in seen and len(items) < limit:
            seen.add(sig)
            items.append({'image_url': {'url': value}})

    def _push_b64(value: str) -> None:
        value = str(value or '').strip()
        if not value:
            return
        sig = ('b64', value)
        if sig not in seen and len(items) < limit:
            seen.add(sig)
            items.append({'b64_json': value})

    def _extract_explicit_image_fields(obj: Any, allow_bare_url: bool = False) -> bool:
        """处理一个 dict 自身明确声明的图片字段；返回是否命中过。"""
        if not isinstance(obj, dict) or len(items) >= limit:
            return False
        found = False

        for key in ('b64_json', 'base64', 'image_base64'):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                _push_b64(value)
                found = True

        for key in ('image_url', 'output_image', 'image_uri'):
            value = obj.get(key)
            if isinstance(value, dict):
                url = value.get('url') or value.get('href') or value.get('link')
                if isinstance(url, str) and url.strip():
                    _push_url(url)
                    found = True
            elif isinstance(value, str) and value.strip():
                _push_url(value)
                found = True

        # 某些 Images API 直接使用 {"url": "..."}。
        url_value = obj.get('url')
        if isinstance(url_value, str) and url_value.strip() and (allow_bare_url or 'b64_json' in obj or 'image_url' in obj or obj.get('type') in {'image', 'image_url'}):
            _push_url(url_value)
            found = True

        return found

    def _walk_image_container(value: Any) -> None:
        if len(items) >= limit:
            return
        if isinstance(value, list):
            for elem in value:
                if len(items) >= limit:
                    break
                if isinstance(elem, str):
                    # 仅在已知图片容器（如 data/images）内接受裸 URL。
                    _push_url(elem)
                elif isinstance(elem, dict):
                    explicit = _extract_explicit_image_fields(elem, allow_bare_url=True)
                    # 允许继续进入“消息/内容/图片数组”等已知图片容器，
                    # 但不扫描错误、元数据、debug 等任意字段。
                    for key in ('images', 'output_images', 'data', 'results', 'choices', 'output', 'message', 'content'):
                        if key not in elem or len(items) >= limit:
                            continue
                        child = elem.get(key)
                        if key == 'content' and isinstance(child, list):
                            for part in child:
                                if not isinstance(part, dict):
                                    continue
                                ptype = str(part.get('type') or '').lower()
                                if ptype in {'image', 'image_url'}:
                                    _extract_explicit_image_fields(part)
                        else:
                            _walk_image_container(child)
        elif isinstance(value, dict):
            _extract_explicit_image_fields(value, allow_bare_url=True)
            for key in ('images', 'output_images', 'data', 'results', 'choices', 'output', 'message', 'content'):
                if key in value and len(items) < limit:
                    child = value.get(key)
                    if key == 'content' and isinstance(child, list):
                        for part in child:
                            if isinstance(part, dict) and str(part.get('type') or '').lower() in {'image', 'image_url'}:
                                _extract_explicit_image_fields(part)
                    else:
                        _walk_image_container(child)

    for key in ('data', 'output_images', 'images', 'results', 'choices', 'output'):
        if key in response_json and len(items) < limit:
            _walk_image_container(response_json.get(key))

    # 兼容某些 provider 把最终图片直接放在顶层，但仍要求字段本身明确是图片字段。
    _extract_explicit_image_fields(response_json)
    return items


def _detect_valid_image(bytes_data: bytes) -> tuple[str, str] | None:
    """验证字节确实是一张可解析的栅格图片，并返回 (mime, extension)。"""
    if not isinstance(bytes_data, (bytes, bytearray)) or not bytes_data:
        return None
    try:
        with Image.open(io.BytesIO(bytes(bytes_data))) as image:
            image.verify()
            fmt = (image.format or '').upper()
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    except Exception:
        logger.debug('[NativeImage] 图片内容验证异常', exc_info=True)
        return None

    format_map = {
        'PNG': ('image/png', 'png'),
        'JPEG': ('image/jpeg', 'jpg'),
        'WEBP': ('image/webp', 'webp'),
        'GIF': ('image/gif', 'gif'),
    }
    return format_map.get(fmt)


def _sniff_payload_kind(bytes_data: bytes) -> str:
    """对被判为"非图片"的字节流做轻量嗅探，返回人类可读的内容形态描述。

    用途：中转商返回的"图片 URL"下载下来经常是防盗链错误页 / OSS XML
    错误 / JSON 报错体而非图片本体。只看 size 无法定位原因，把实际内容
    形态带进日志与用户报错，能一眼看出"链接给的是 HTML 错误页"。
    只读前 512 字节，绝不解析整个 payload。
    """
    if not isinstance(bytes_data, (bytes, bytearray)) or not bytes_data:
        return "空内容"
    head = bytes(bytes_data[:512])
    stripped = head.lstrip()
    if stripped.startswith(b"%PDF"):
        return "PDF 文档"
    if stripped.startswith((b"<!DOCTYPE html", b"<!doctype html", b"<html", b"<HTML")):
        return "HTML 页面（疑似防盗链/错误页）"
    if stripped.startswith((b"<?xml", b"<Error", b"<error")):
        return "XML 错误响应（疑似 OSS/CDN 拒绝）"
    if stripped.startswith((b"{", b"[")):
        return "JSON（疑似 API 错误响应）"
    if stripped.startswith(b"\x1f\x8b"):
        return "GZIP 压缩数据"
    if stripped.startswith((b"GIF8", b"\x89PNG", b"\xff\xd8\xff")):
        # 头部像图片但 PIL verify 失败 → 文件截断/损坏
        return "图片头部但数据损坏或截断"
    sample = head[:256]
    if sample and all(32 <= b < 127 or b in (9, 10, 13) for b in sample):
        try:
            text = sample.decode("ascii", "ignore").strip()
            snippet = re.sub(r"\s+", " ", text)[:60]
            return f"纯文本（开头: {snippet!r}）"
        except Exception:
            pass
    return f"未知二进制（magic: {bytes(bytes_data[:8]).hex(' ')}）"


def _validate_image_bytes(bytes_data: bytes, source: str = '') -> bytes | None:
    """只允许真正可解析的图片字节进入 R2。"""
    detected = _detect_valid_image(bytes_data)
    if detected is None:
        payload = bytes(bytes_data or b'')
        logger.warning(
            '[NativeImage] rejected non-image payload: source=%s size=%s kind=%s',
            source[:160], len(payload), _sniff_payload_kind(payload),
        )
        return None
    return bytes(bytes_data)


def _extract_native_message_text(content: Any) -> str:
    """尽量从模型内容中提取纯文本，兼容字符串与结构化 content。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                piece: Any = item.strip()
                if piece:
                    parts.append(piece)
                continue
            if not isinstance(item, dict):
                continue
            item_type = (item.get("type") or "").lower()
            if item_type in ("text", "output_text"):
                piece = item.get("text") or item.get("output_text") or item.get("content")
                if isinstance(piece, str) and piece.strip():
                    parts.append(piece.strip())
            elif isinstance(item.get("text"), str) and cast(str, item.get("text")).strip():
                parts.append(cast(str, item.get("text")).strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _extract_native_refusal_text(message: Any) -> str:
    """提取模型拒绝文本，兼容 refusal 字段、dump 结构与字符串化对象。"""
    if message is None:
        return ""
    refusal = getattr(message, "refusal", None)
    if refusal is None:
        try:
            msg_dump = message.model_dump()
            refusal = msg_dump.get("refusal")
        except Exception:
            logger.debug("_extract_native_refusal_text 内部忽略的异常", exc_info=True)
            refusal = None
    if isinstance(refusal, str):
        return refusal.strip()
    if isinstance(refusal, list):
        return "\n".join(str(x) for x in refusal if x is not None).strip()
    if refusal is not None:
        return str(refusal).strip()
    return ""


def _format_native_image_notice(
        *,
        content_text: str = "",
        refusal_text: str = "",
        finish_reason: str = "",
) -> str:
    """把模型拒绝/安全拦截/仅文本输出，统一转成用户可见友好提示。"""
    content_text = strip_html_tags(content_text or "").strip()
    refusal_text = strip_html_tags(refusal_text or "").strip()
    reason = (finish_reason or "").strip().lower()

    if reason in {"content_filter", "safety", "blocked", "moderation"}:
        if refusal_text:
            return f"⚠️ 这张图触发了安全限制：{refusal_text[:600]}"
        if content_text:
            return f"⚠️ 这张图触发了安全限制：{content_text[:600]}"
        return "⚠️ 这张图触发了安全限制，请修改描述后重试。"

    if refusal_text:
        return f"⚠️ {refusal_text[:600]}"

    if content_text:
        return content_text[:1200]

    return "⚠️ 图片生成失败，请稍后重试。"


async def _response_items_to_bytes(
        response_json: dict, max_images: int = 4,
) -> tuple[list[bytes], list[str]]:
    """解析响应中的图片数据为字节列表，并收集"逐项拒绝"诊断。

    返回 (image_bytes_list, diagnostics)。diagnostics 记录每个被跳过的
    图片项的真实原因（下载失败 / 内容非图片 / 体积超限…）——上游返回
    HTTP 200 但图片 URL 下载下来是防盗链错误页时（2026-09 ModelScope
    生产事故），调用方不能再笼统报"未找到可用图片数据"，而要把真实
    原因带给用户。

    诊断行面向用户展示，刻意不带完整 URL（中转 URL 常含签名参数，又长
    又敏感），只保留 host 供定位是哪个域的链接出了问题。
    """
    image_bytes_list: list[bytes] = []
    diagnostics: list[str] = []
    limit = max(1, min(int(max_images or 1), 4))
    items = _extract_image_items(response_json, max_items=limit)
    logger.debug("[NativeImage/ModelScope] extracted image item count=%s", len(items))
    # 防止恶意/失控的上游用超大 base64 串触发 OOM：
    # 单张图片的 base64 串超过 25 MB 时直接拒绝解码。
    MAX_BASE64_ENCODED_BYTES = 25 * 1024 * 1024

    def _host_of(url: str) -> str:
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc or url[:60]
        except Exception:
            return url[:60]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        for idx, img_data in enumerate(items):
            img_url = ''
            if isinstance(img_data.get('image_url'), dict):
                img_url = str(img_data['image_url'].get('url') or '').strip()
            elif isinstance(img_data.get('image_url'), str):
                img_url = str(img_data.get('image_url') or '').strip()
            img_url = img_url or str(img_data.get('url') or '').strip()
            b64_json = str(img_data.get('b64_json') or img_data.get('base64') or '').strip()

            if b64_json:
                if len(b64_json) > MAX_BASE64_ENCODED_BYTES:
                    logger.warning(
                        "[NativeImage] 跳过超大 base64 图片 (len=%s, 上限=%s)",
                        len(b64_json), MAX_BASE64_ENCODED_BYTES,
                    )
                    diagnostics.append(
                        f"图片 #{idx + 1}：base64 体积超限（{len(b64_json) // 1024 // 1024}MB），已跳过")
                    continue
                try:
                    decoded = base64.b64decode(b64_json, validate=True)
                    validated = _validate_image_bytes(decoded, source='b64_json')
                    if validated is not None:
                        image_bytes_list.append(validated)
                    else:
                        diagnostics.append(
                            f"图片 #{idx + 1}：base64 内容不是有效图片（{_sniff_payload_kind(decoded)}）")
                    if len(image_bytes_list) >= limit:
                        break
                    continue
                except Exception as e:
                    logger.warning(f"[NativeImage] Base64 图片解码失败: {e}")
                    diagnostics.append(f"图片 #{idx + 1}：base64 解码失败（{str(e)[:60]}）")

            if img_url.startswith('data:image'):
                try:
                    _, base64_data = img_url.split(',', 1)
                    if len(base64_data) > MAX_BASE64_ENCODED_BYTES:
                        logger.warning(
                            "[NativeImage] 跳过超大 data URL 图片 (len=%s, 上限=%s)",
                            len(base64_data), MAX_BASE64_ENCODED_BYTES,
                        )
                        diagnostics.append(
                            f"图片 #{idx + 1}：data URL 体积超限（{len(base64_data) // 1024 // 1024}MB），已跳过")
                        continue
                    decoded = base64.b64decode(base64_data, validate=True)
                    validated = _validate_image_bytes(decoded, source='data_url')
                    if validated is not None:
                        image_bytes_list.append(validated)
                    else:
                        diagnostics.append(
                            f"图片 #{idx + 1}：data URL 内容不是有效图片（{_sniff_payload_kind(decoded)}）")
                    if len(image_bytes_list) >= limit:
                        break
                    continue
                except Exception as e:
                    logger.warning(f"[NativeImage] data URL 解码失败: {e}")
                    diagnostics.append(f"图片 #{idx + 1}：data URL 解码失败（{str(e)[:60]}）")

            if img_url.startswith('http'):
                host = _host_of(img_url)
                try:
                    async with session.get(img_url, timeout=30) as resp:
                        if resp.status == 200:
                            # 同样限制远端下载体积，避免恶意 upstream 用
                            # 一个 100 MB 的"图片"把进程拖垮。
                            max_remote = 25 * 1024 * 1024
                            # 修复（2026-09 生产事故）：resp.content.read(n)
                            # 语义是"最多读 n 字节"——响应分块传输时立即返回
                            # buffer 中已到达的部分数据，85KB 的 PNG 被读成
                            # 半张（缺 IEND 尾部）→ PIL verify 判"损坏或截断"
                            # 误拒（浏览器直接打开同一 URL 完全正常）。
                            # 必须循环 readany() 读到 EOF，边读边限体积。
                            content_length = resp.headers.get("Content-Length") or ""
                            if content_length.isdigit() and int(content_length) > max_remote:
                                logger.warning(
                                    "[NativeImage] 远端图片体积超限 (Content-Length=%s)，跳过: %s",
                                    content_length, img_url[:120],
                                )
                                diagnostics.append(
                                    f"图片 #{idx + 1}（{host}）：体积约 "
                                    f"{int(content_length) / 1024 / 1024:.1f}MB 超过 25MB 上限，已跳过")
                                continue
                            chunks: list[bytes] = []
                            total = 0
                            while True:
                                block = await resp.content.readany()
                                if not block:
                                    break
                                chunks.append(block)
                                total += len(block)
                                if total > max_remote:
                                    break
                            if total > max_remote:
                                logger.warning(
                                    "[NativeImage] 远端图片体积超限 (>%s)，跳过: %s",
                                    max_remote, img_url[:120],
                                )
                                diagnostics.append(
                                    f"图片 #{idx + 1}（{host}）：下载体积超过 25MB 上限，已跳过")
                                continue
                            image_bytes = b"".join(chunks)
                            content_type = str(resp.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
                            if content_type and not content_type.startswith('image/'):
                                logger.warning('[NativeImage] 远端响应不是图片，跳过: content_type=%s url=%s', content_type or '-', img_url[:120])
                                diagnostics.append(
                                    f"图片 #{idx + 1}（{host}）：响应类型是 {content_type or '未知'}，不是图片")
                                continue
                            validated = _validate_image_bytes(image_bytes, source=img_url)
                            if validated is not None:
                                image_bytes_list.append(validated)
                            else:
                                diagnostics.append(
                                    f"图片 #{idx + 1}（{host}）：链接下载了 {len(image_bytes)} 字节，"
                                    f"但内容不是有效图片（{_sniff_payload_kind(image_bytes)}）")
                            if len(image_bytes_list) >= limit:
                                break
                        else:
                            logger.warning(f"[NativeImage] 下载生成图片失败 {resp.status}: {img_url[:120]}")
                            diagnostics.append(
                                f"图片 #{idx + 1}（{host}）：下载失败，HTTP {resp.status}")
                except Exception as e:
                    logger.warning(f"[NativeImage] 下载生成图片异常: {e}")
                    diagnostics.append(
                        f"图片 #{idx + 1}（{host}）：下载异常（{str(e)[:60]}）")
            elif not b64_json:
                # 既不是 base64 / data URL，也不是 http(s) 链接：识别不出
                # 任何图片数据形态。
                diagnostics.append(f"图片 #{idx + 1}：响应项里没有可识别的图片数据")
    return image_bytes_list, diagnostics


async def _request_agnes_video(
        prompt: str,
        duration: int,
        model: str,
        reference_images: tuple = (),
        reference_videos: tuple = (),
        *,
        size: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        mode: Optional[str] = None,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        seed: Optional[int] = None,
        reference_audios: tuple = (),
        video_specs: tuple = (),
) -> tuple[str | None, str | None, Optional[dict]]:
    """提交视频任务到 Agnes（OpenAI Videos 兼容）并轮询结果。

    返回 (video_url, error_message, meta)；成功时 error=None，meta 含
    seconds/size 等任务元数据（供 caption 使用）。

    请求体由统一管道构建器装配（protocols.pipeline.build_video_request_body，
    Agnes Video 2.5 文档 schema）：seconds 为字符串 "4"–"12"（发 duration
    字段会被网关 400 "duration is not an allowed request field"）、mode
    必填（text/keyframe/reference——keyframe 带首尾帧、reference 带参考
    媒体，构建器按媒体自动推断/校验）、size/画幅白名单校验。参考媒体
    URL 必须公开可访问（R2 公开 URL 解析路径产出的地址恰好满足）。

    提交端点由模型配置驱动（endpoint 字段，如
    "https://apihub.agnes-ai.com/v1/videos"）：endpoint 指向视频子路径
    （/videos）时作为提交 URL，否则（endpoint 为 API 根/其它协议端点）
    回退内置默认；轮询端点从声明提交端点的 scheme://host 推导
    （{host}/agnesapi），未声明时回退内置默认——与文档推荐的
    GET /agnesapi?video_id=<VIDEO_ID>&model_name=<MODEL> 查询方式一致
    （keyframe/reference 模式必须带 model_name，text 模式亦推荐）。
    """
    headers = {
        "Authorization": f"Bearer {AGNES_API_KEY}",
        "Content-Type": "application/json",
    }

    # 提交端点：模型声明了指向视频子路径（/videos）的 endpoint 就用声明值
    # （端点路由完全由 endpoint 支撑，无需按厂商分支）；endpoint 为 API 根
    # 或其它形状时回退内置默认。
    submit_url = "https://apihub.agnes-ai.com/v1/videos"
    declared_endpoint = ""
    try:
        model_info = SUPPORTED_MODELS.get(model)
        if model_info is not None:
            declared = str(get_effective_endpoint(model_info).endpoint or "").strip()
            if declared and _VIDEO_SUBMIT_PATH_PATTERN.search(declared):
                submit_url = declared
                declared_endpoint = declared
    except Exception:
        logger.debug("[NativeVideo/Agnes] 解析模型 endpoint 失败，回退默认提交端点", exc_info=True)

    # 请求体：统一管道构建器（文档 schema 硬约束集中在一处，可单测）。
    from protocols.pipeline import build_video_request_body
    plan = None
    try:
        from protocols.pipeline import resolve_request_plan
        plan = resolve_request_plan(SUPPORTED_MODELS.get(model))
    except Exception:
        plan = None
    if plan is None or plan.api_type != "video":
        # 未注册/非视频模型的安全兜底：按 video 分支最小形状构造。
        from protocols.pipeline import RequestPlan
        plan = RequestPlan(route="video", api_type="video", protocol="", endpoint="")
    payload = build_video_request_body(
        plan,
        model=model,
        prompt=prompt,
        seconds=duration,
        reference_images=tuple(reference_images),
        reference_videos=tuple(reference_videos),
        size=size,
        aspect_ratio=aspect_ratio,
        mode=mode,
        first_frame=first_frame,
        last_frame=last_frame,
        seed=seed,
        reference_audios=tuple(reference_audios),
        video_specs=tuple(video_specs),
    )
    if payload is None:  # 理论不可达（上方已强制 video 分支）；保守兜底
        payload = {"model": model, "prompt": (prompt or "").strip(),
                   "seconds": str(max(4, min(int(duration or 5), 12))), "mode": "text"}

    clean_prompt = str(payload.get("prompt") or "")
    logger.debug(
        "[NativeVideo/Agnes] request prepared: model=%s seconds=%s mode=%s submit_url=%s "
        "ref_images=%s ref_videos=%s prompt_len=%s prompt_preview=%r",
        model,
        payload.get("seconds"),
        payload.get("mode"),
        submit_url,
        len(payload.get("images") or ()),
        len(payload.get("videos") or ()),
        len(clean_prompt),
        clean_prompt[:240],
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(submit_url, headers=headers, json=payload, timeout=30) as resp:
                resp_text = await resp.text()
                logger.debug(
                    "[NativeVideo/Agnes] submit response: status=%s content_type=%s body_preview=%r",
                    resp.status,
                    resp.headers.get("Content-Type", ""),
                    resp_text[:500],
                )
                if resp.status != 200:
                    return None, f"Agnes 提交失败 (HTTP {resp.status}): {resp_text[:200]}", None

                try:
                    data = json.loads(resp_text)
                except Exception:
                    logger.debug("_request_agnes_video 内部忽略的异常", exc_info=True)
                    return None, f"Agnes 提交返回非 JSON: {resp_text[:200]}", None

                video_id = data.get("video_id") or data.get("id")
                if not video_id:
                    logger.warning("[NativeVideo/Agnes] submit ok but no video_id in response keys=%s",
                                   list(data.keys())[:40])
                    return None, "Agnes 响应中没有 video_id", None

                logger.debug(
                    "[NativeVideo/Agnes] submit ok: video_id=%s keys=%s",
                    str(video_id),
                    list(data.keys())[:40],
                )

    except Exception as e:
        logger.exception("[NativeVideo/Agnes] submit exception")
        return None, f"Agnes 提交异常: {str(e)[:100]}", None

    # 轮询结果：端点从声明提交端点的 host 推导（/agnesapi 在站点根路径，
    # 不在 /v1 下）；查询参数必须带 model_name（keyframe/reference 模式
    # 硬要求，text 模式亦为文档推荐写法）。
    if declared_endpoint:
        parts = urlsplit(declared_endpoint)
        poll_url = f"{parts.scheme}://{parts.netloc}/agnesapi"
    else:
        poll_url = "https://apihub.agnes-ai.com/agnesapi"
    max_wait = 300  # 5分钟
    interval = 3
    start_time = time.monotonic()
    poll_iter = 0

    logger.debug(
        "[NativeVideo/Agnes] start polling: poll_url=%s max_wait=%ss interval=%ss video_id=%s model_name=%s",
        poll_url,
        max_wait,
        interval,
        video_id,
        model,
    )

    async with aiohttp.ClientSession() as session:
        while time.monotonic() - start_time < max_wait:
            poll_iter += 1
            elapsed = time.monotonic() - start_time

            try:
                params = {"video_id": video_id, "model_name": model}
                async with session.get(poll_url, headers=headers, params=params, timeout=15) as resp:
                    body_text = await resp.text()
                    logger.debug(
                        "[NativeVideo/Agnes] polling iter=%s response: status=%s body_preview=%r",
                        poll_iter,
                        resp.status,
                        body_text[:500],
                    )

                    if resp.status != 200:
                        logger.debug(
                            "[NativeVideo/Agnes] polling iter=%s non-200 status=%s elapsed=%.1fs",
                            poll_iter,
                            resp.status,
                            elapsed,
                        )
                        await asyncio.sleep(interval)
                        continue

                    try:
                        data = json.loads(body_text)
                    except Exception:
                        logger.warning(
                            "[NativeVideo/Agnes] polling iter=%s JSON parse failed elapsed=%.1fs body_preview=%r",
                            poll_iter,
                            elapsed,
                            body_text[:300],
                        )
                        await asyncio.sleep(interval)
                        continue

                    status = str(data.get("status") or "").lower()
                    logger.debug(
                        "[NativeVideo/Agnes] polling iter=%s status=%s elapsed=%.1fs keys=%s",
                        poll_iter,
                        status or "unknown",
                        elapsed,
                        list(data.keys())[:40],
                    )

                    if status == "completed":
                        # 文档：结果地址在 metadata.url；保留旧字段回退
                        # （video_url / url / output.url）兼容历史网关响应。
                        metadata = data.get("metadata") or {}
                        if not isinstance(metadata, dict):
                            metadata = {}
                        video_url = (
                                metadata.get("url")
                                or data.get("video_url")
                                or data.get("url")
                                or (data.get("output") or {}).get("url")
                        )
                        if video_url:
                            # 元数据（用于 caption）：优先 perf_params，
                            # 补充文档响应中的 seconds / size 档位。
                            meta = data.get("perf_params") or {}
                            if not isinstance(meta, dict):
                                meta = {}
                            for _k in ("seconds", "size"):
                                if data.get(_k) and _k not in meta:
                                    meta[_k] = data.get(_k)
                            logger.info(
                                "[NativeVideo/Agnes] polling succeeded: iter=%s elapsed=%.1fs video_url=%r",
                                poll_iter,
                                elapsed,
                                str(video_url)[:240],
                            )
                            return video_url, None, meta

                        logger.warning(
                            "[NativeVideo/Agnes] completed but missing video_url: iter=%s elapsed=%.1fs keys=%s",
                            poll_iter,
                            elapsed,
                            list(data.keys())[:40],
                        )
                        return None, "Agnes 任务完成但未返回视频 URL", None

                    if status in ("failed", "error"):
                        # 文档：失败响应 error 为对象 {message: ...}；兼容
                        # 字符串形态与旧字段 message。
                        raw_error = data.get("error")
                        if isinstance(raw_error, dict):
                            error_msg = raw_error.get("message") or json.dumps(raw_error, ensure_ascii=False)
                        elif raw_error:
                            error_msg = str(raw_error)
                        else:
                            error_msg = data.get("message") or "未知错误"
                        logger.error(
                            "[NativeVideo/Agnes] polling failed: iter=%s elapsed=%.1fs error=%r",
                            poll_iter,
                            elapsed,
                            str(error_msg)[:300],
                        )
                        return None, f"Agnes 视频生成失败: {error_msg}", None

                    logger.debug(
                        "[NativeVideo/Agnes] polling iter=%s still running: status=%s elapsed=%.1fs next_poll_in=%ss",
                        poll_iter,
                        status or "unknown",
                        elapsed,
                        interval,
                    )
                    await asyncio.sleep(interval)

            except Exception as e:
                logger.warning(
                    "[NativeVideo/Agnes] polling exception: iter=%s elapsed=%.1fs err=%s",
                    poll_iter,
                    elapsed,
                    str(e)[:200],
                )
                await asyncio.sleep(interval)
                continue

    logger.warning("[NativeVideo/Agnes] polling timeout: max_wait=%ss video_id=%s", max_wait, video_id)
    return None, f"Agnes 轮询超时 ({max_wait} 秒)", None


async def _request_openrouter_video(
        prompt: str,
        duration: int,
        model: str,
) -> tuple[str | None, str | None, Optional[dict]]:
    """
    提交视频任务到 OpenRouter 并轮询结果。
    返回 (video_url, error_message, meta)；成功时 error=None，meta 通常为 None（OpenRouter 不暴露同量级的元数据）。
    """
    api_root = "https://openrouter.ai/api/v1"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    clean_prompt = (prompt or "").strip()
    logger.debug(
        "[NativeVideo/OpenRouter] request prepared: model=%s duration=%ss prompt_len=%s prompt_preview=%r",
        model,
        duration,
        len(clean_prompt),
        clean_prompt[:240],
    )

    # 提交任务
    submit_url = f"{api_root}/videos"
    payload = {
        "model": model,
        "prompt": clean_prompt,
        "duration": duration,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "provider": OPENROUTER_PROVIDER_PREFERENCES,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(submit_url, headers=headers, json=payload, timeout=30) as resp:
                resp_text = await resp.text()
                logger.debug(
                    "[NativeVideo/OpenRouter] submit response: status=%s content_type=%s body_preview=%r",
                    resp.status,
                    resp.headers.get("Content-Type", ""),
                    resp_text[:500],
                )

                if resp.status != 202:
                    return None, f"OpenRouter 提交失败 (HTTP {resp.status}): {resp_text[:200]}", None

                try:
                    data = json.loads(resp_text)
                except Exception:
                    logger.debug("_request_openrouter_video 内部忽略的异常", exc_info=True)
                    return None, f"OpenRouter 提交返回非 JSON: {resp_text[:200]}", None

                job_id = data.get("id")
                polling_url = data.get("polling_url") or data.get("status_url")

                if not job_id or not polling_url:
                    logger.warning(
                        "[NativeVideo/OpenRouter] submit ok but missing job_id/polling_url keys=%s",
                        list(data.keys())[:40],
                    )
                    return None, "OpenRouter 响应缺少 job_id 或 polling_url", None

                # SSRF 防御：polling_url 由上游 API 返回，恶意/被攻陷的
                # 上游可让 bot 去访问内网（如 169.254.169.254 metadata
                # endpoint 或 127.0.0.1）。这里强制白名单只允许
                # openrouter.ai 主机，其它一律拒绝。
                try:
                    from urllib.parse import urlparse
                    _parsed_poll = urlparse(str(polling_url))
                except Exception:
                    logger.debug("_request_openrouter_video 内部忽略的异常", exc_info=True)
                    _parsed_poll = None
                if (
                    not _parsed_poll
                    or _parsed_poll.scheme not in ("http", "https")
                    or _parsed_poll.netloc.lower() not in {"openrouter.ai", "api.openrouter.ai"}
                ):
                    logger.warning(
                        "[NativeVideo/OpenRouter] rejected polling_url outside allowlist: %r",
                        str(polling_url)[:200],
                    )
                    return None, "OpenRouter 返回了不在白名单内的轮询 URL", None

                logger.debug(
                    "[NativeVideo/OpenRouter] submit ok: job_id=%s polling_url=%s keys=%s",
                    str(job_id),
                    str(polling_url)[:240],
                    list(data.keys())[:40],
                )

    except Exception as e:
        logger.exception("[NativeVideo/OpenRouter] submit exception")
        return None, f"OpenRouter 提交异常: {str(e)[:100]}", None

    # 轮询结果
    max_wait = 300
    interval = 3
    start_time = time.monotonic()
    poll_iter = 0

    logger.debug(
        "[NativeVideo/OpenRouter] start polling: max_wait=%ss interval=%ss job_id=%s polling_url=%s",
        max_wait,
        interval,
        str(job_id),
        str(polling_url)[:240],
    )

    async with aiohttp.ClientSession() as session:
        while time.monotonic() - start_time < max_wait:
            poll_iter += 1
            elapsed = time.monotonic() - start_time

            try:
                async with session.get(polling_url, headers=headers, timeout=15) as resp:
                    body_text = await resp.text()
                    logger.debug(
                        "[NativeVideo/OpenRouter] polling iter=%s response: status=%s body_preview=%r",
                        poll_iter,
                        resp.status,
                        body_text[:500],
                    )

                    if resp.status != 200:
                        logger.debug(
                            "[NativeVideo/OpenRouter] polling iter=%s non-200 status=%s elapsed=%.1fs",
                            poll_iter,
                            resp.status,
                            elapsed,
                        )
                        await asyncio.sleep(interval)
                        continue

                    try:
                        data = json.loads(body_text)
                    except Exception:
                        logger.warning(
                            "[NativeVideo/OpenRouter] polling iter=%s JSON parse failed elapsed=%.1fs body_preview=%r",
                            poll_iter,
                            elapsed,
                            body_text[:300],
                        )
                        await asyncio.sleep(interval)
                        continue

                    status = str(data.get("status") or "").lower()
                    logger.debug(
                        "[NativeVideo/OpenRouter] polling iter=%s status=%s elapsed=%.1fs keys=%s",
                        poll_iter,
                        status or "unknown",
                        elapsed,
                        list(data.keys())[:40],
                    )

                    if status == "completed":
                        # unsigned_urls 可能是空列表 []（任务完成但 URL 列表为空）。
                        # 用 next(iter(...), None) 安全取首个元素，避免 IndexError。
                        unsigned = data.get("unsigned_urls")
                        video_url = data.get("content") or (
                            next(iter(unsigned), None)
                            if isinstance(unsigned, list) and unsigned
                            else None
                        )
                        if video_url:
                            logger.info(
                                "[NativeVideo/OpenRouter] polling succeeded: iter=%s elapsed=%.1fs video_url=%r",
                                poll_iter,
                                elapsed,
                                str(video_url)[:240],
                            )
                            return video_url, None, None

                        logger.warning(
                            "[NativeVideo/OpenRouter] completed but missing video_url: iter=%s elapsed=%.1fs keys=%s",
                            poll_iter,
                            elapsed,
                            list(data.keys())[:40],
                        )
                        return None, "OpenRouter 任务完成但未返回视频 URL", None

                    if status in ("failed", "error"):
                        error_msg = data.get("error") or data.get("message") or "未知错误"
                        logger.error(
                            "[NativeVideo/OpenRouter] polling failed: iter=%s elapsed=%.1fs error=%r",
                            poll_iter,
                            elapsed,
                            str(error_msg)[:300],
                        )
                        return None, f"OpenRouter 视频生成失败: {error_msg}", None

                    logger.debug(
                        "[NativeVideo/OpenRouter] polling iter=%s still running: status=%s elapsed=%.1fs next_poll_in=%ss",
                        poll_iter,
                        status or "unknown",
                        elapsed,
                        interval,
                    )
                    await asyncio.sleep(interval)

            except Exception as e:
                logger.warning(
                    "[NativeVideo/OpenRouter] polling exception: iter=%s elapsed=%.1fs err=%s",
                    poll_iter,
                    elapsed,
                    str(e)[:200],
                )
                await asyncio.sleep(interval)
                continue

    logger.warning("[NativeVideo/OpenRouter] polling timeout: max_wait=%ss job_id=%s", max_wait, str(job_id))
    return None, f"OpenRouter 轮询超时 ({max_wait} 秒)", None




# =============================================================================
# ImageTask 统一请求出口（protocols/images.py 的两个适配器落在这里）
# -----------------------------------------------------------------------------
# 重构说明（ImageTask）：图像任务的"操作"（generate/edit/variation）是任务的
# 一等字段（core/images.ImageTask.operation），由任务构造方显式声明；
# 以下两个出口只按任务与模型协议发请求并解析，不再做任何
# "看到参考图 = edit"式的端点猜测。
#   - _request_openai_images_task        -> /images/{generations,edits}
#     （operation=edit/variation 且带参考图 -> 官方 multipart /images/edits，
#      路由未实现时按既有鲁棒性回退 JSON /images/generations + image 字段；
#      operation=generate -> /images/generations；ModelScope 一律
#      /images/generations + 异步任务轮询，无 /images/edits 端点）
#   - _request_chat_modalities_image_task -> chat.completions + modalities
#     （OpenRouter 图像模型；参考图作为消息内容输入）
# =============================================================================


async def _request_openai_images_task(task: "ImageTask") -> "ImageTaskResult":
    """OpenAI Images 协议出口：ImageTask -> /v1/images/{generations,edits}。

    端点选择由任务操作 + 提供商能力决定：
      - generate            -> JSON /images/generations
      - edit / variation    -> multipart /images/edits（XXTF 等标准端点），
                               失败即报错；绝不回退 /images/generations
                               （该端点不接受 image 参数，回退 = 文生图假成功）
      - modelscope          -> 一律 /images/generations（X-ModelScope-Task-Type
                               头区分文生图/图生图；该厂商不存在 /images/edits）
    """
    model_info = SUPPORTED_MODELS.get(task.model)
    if model_info is None:
        raise ImageRequestError(f"未知图像模型: {task.model!r}", endpoint="/images/generations")

    response_json, endpoint, error_detail, status_code, request_id = await _request_images_generations(
        model_info,
        prompt=task.effective_prompt,
        image_urls=list(task.input_images),
        num_images=max(1, min(int(task.num_images or 1), 4)),
        model=task.model,
        aspect_ratio=task.aspect_ratio,
        image_size=task.image_size,
        extra_params=task.extra_params,
    )
    if response_json is None:
        raise ImageRequestError(
            error_detail or "图像接口请求失败",
            status_code=status_code or 500,
            endpoint=f"/v1{endpoint}",
            request_id=request_id,
        )
    image_bytes_list, diagnostics = await _response_items_to_bytes(
        response_json, max_images=task.num_images or 1)
    usage = response_json.get("usage") if isinstance(response_json, dict) else None
    if diagnostics:
        logger.warning(
            "[NativeImage] %s 响应解析出 %s 张图片，另有 %s 条拒绝诊断: %s",
            task.model, len(image_bytes_list), len(diagnostics),
            " | ".join(diagnostics)[:500],
        )
    return ImageTaskResult(
        images=image_bytes_list, endpoint=f"/v1{endpoint}", usage=usage,
        diagnostics=diagnostics,
    )


# chat modalities（OpenRouter 式）形状的保留键：modalities/provider 由
# 本函数固定管理（决定输出模态与路由偏好，覆盖会导致图片输出请求变成
# 纯文本或路由行为失控）；image_config/n 同样由 task 的结构化字段
# （meta.image_config / num_images）派生，覆盖会与工具其他参数的预期不符。
_CHAT_MODALITIES_RESERVED_TOP_KEYS = frozenset({"modalities", "provider", "image_config", "n"})


async def _request_chat_modalities_image_task(task: "ImageTask") -> "ImageTaskResult":
    """Chat Completions + modalities 图像出口（OpenRouter 图像模型等）。

    语义映射：
      - generate            -> 纯文本 prompt（modalities=["image","text"]）
      - edit / variation    -> prompt + 参考图 image_url 内容
                               （图生图；prompt 为空时 variation 补默认指令）
    网关不支持 image+text 输出时按既有行为自动降级重试 image-only。
    未注册到 SUPPORTED_MODELS 的模型按 OpenRouter 兼容直连（保持旧
    execute_generate_image 对 flux 等别名的可达性）。

    task.extra_params 同样在此路径生效（与 inline_images/Agnes 形状一致的
    "escape hatch"语义）：安全合并进 extra_body（首次请求与 image-only
    降级重试都会带上），modalities/provider 等本函数管理的字段禁止被
    覆盖（见 _CHAT_MODALITIES_RESERVED_TOP_KEYS）。上游网关（OpenRouter）
    转发的具体模型若支持额外采样参数（如某些图像模型的 seed），可通过
    这个口子传入而无需等代码发版。
    """
    model_info = SUPPORTED_MODELS.get(task.model)

    # ---------- 组装消息（内部 Message -> wire，同一套渲染出口） ----------
    content_blocks: list[Any] = []
    if task.effective_prompt:
        content_blocks.append(TextBlock(task.effective_prompt))
    for url in task.input_images:
        if url:
            content_blocks.append(ImageBlock(url=url))
    wire_messages: list[dict]
    if len(content_blocks) == 1 and isinstance(content_blocks[0], TextBlock):
        wire_messages = [{"role": "user", "content": task.effective_prompt}]
    elif content_blocks:
        wire_messages = [Message.user(content_blocks).to_openai_dict()]
    else:
        raise ImageRequestError("图像任务缺少 prompt 与参考图", endpoint="/chat/completions")

    # ---------- 客户端与鉴权 ----------
    if model_info is not None:
        from api_client import api_client as _api_client
        client = _api_client.get_client_for_model(model_info)
        sampling = get_sampling_params(model_info)
    else:
        # 未注册模型（flux 等历史别名）：按 OpenRouter 兼容直连。
        client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
            max_retries=0,
        )
        sampling = {}

    extra_body: dict[str, Any] = {"modalities": ["image", "text"],
                                  "provider": OPENROUTER_PROVIDER_PREFERENCES}
    if task.meta.get("image_config"):
        extra_body["image_config"] = task.meta["image_config"]
    if int(task.num_images or 1) > 1:
        extra_body["n"] = max(1, min(int(task.num_images), 4))
    # extra_params 透传（如上游网关文档化的采样/风格类可选字段）：
    # modalities/provider 属于本函数管理的结构化字段，禁止被覆盖，见
    # _CHAT_MODALITIES_RESERVED_TOP_KEYS。
    _merge_extra_params(
        extra_body, task.extra_params,
        reserved_top_keys=_CHAT_MODALITIES_RESERVED_TOP_KEYS,
    )

    max_tokens = (model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192)

    try:
        response = await client.chat.completions.create(
            model=task.model,
            messages=wire_messages,
            max_tokens=max_tokens,
            extra_body=extra_body,
            stream=False,
            **sampling,
        )
    except Exception as e:
        err_text = str(e)
        # "output modalities" 含子串 "modalities"，前一条件恒被后者包含。
        # 鉴权/配额/限流类错误（401/402/403/429）即使错误文本碰巧提到
        # "modalities" 也绝不是能力不支持问题——重试只会再次失败（甚至
        # 再次计费），必须直接上抛，由上层按配额/鉴权错误呈现。
        _status = getattr(e, "status_code", None) or getattr(e, "status", None)
        if "modalities" not in err_text or _status in (401, 402, 403, 429):
            raise
        logger.warning(f"Native image model does not support image+text output, retrying image-only: {e}")
        # 降级重试保持与首次请求一致的 n 参数（num_images>1 时首次请求带 n，
        # 旧代码重试时丢失 n 导致多图请求静默退化为单图）。
        retry_extra: dict[str, Any] = {"modalities": ["image"],
                                       "provider": OPENROUTER_PROVIDER_PREFERENCES}
        if int(task.num_images or 1) > 1:
            retry_extra["n"] = max(1, min(int(task.num_images), 4))
        _merge_extra_params(
            retry_extra, task.extra_params,
            reserved_top_keys=_CHAT_MODALITIES_RESERVED_TOP_KEYS,
        )
        try:
            response = await client.chat.completions.create(
                model=task.model,
                messages=wire_messages,
                max_tokens=max_tokens,
                extra_body=retry_extra,
                stream=False,
                **sampling,
            )
        except Exception as retry_err:
            # 降级重试也失败：合并两次错误抛 ImageRequestError，让上层
            # （execute_generate_image / 原生图像循环）按统一格式呈现，
            # 而不是把 SDK 原始异常裸抛（裸抛会丢失"已降级重试过"的上下文，
            # 模型无法判断该换模型还是该停止重试）。
            _retry_status = getattr(retry_err, "status_code", None) or getattr(retry_err, "status", None)
            _retry_status_code = _retry_status if isinstance(_retry_status, int) else 500
            logger.error(
                "[NativeImage] image-only 降级重试同样失败: status=%s err=%s（首次: status=%s）",
                _retry_status_code, str(retry_err)[:300], _status,
            )
            raise ImageRequestError(
                f"{err_text} ｜ 降级为 image-only 重试后仍失败: {retry_err}",
                status_code=_retry_status_code,
                endpoint="/chat/completions",
            ) from retry_err

    # ---------- 解析响应（与旧 chat modalities 路径一致） ----------
    choice = response.choices[0]
    finish_reason = str(getattr(choice, "finish_reason", "") or "")
    text = _extract_native_message_text(getattr(choice.message, "content", ""))
    refusal = _extract_native_refusal_text(choice.message)

    try:
        msg_dump = choice.message.model_dump()
        images = _extract_image_items(msg_dump)
        if not images:
            images = getattr(choice.message, "images", []) or []
    except Exception:
        logger.debug("_request_chat_modalities_image_task 内部忽略的异常", exc_info=True)
        images = []

    image_bytes_list: list[bytes] = []
    for img_data in images:
        img_url = (img_data.get("image_url", {}) or {}).get("url") if isinstance(img_data, dict) else None
        if not img_url:
            continue
        if img_url.startswith("data:image"):
            try:
                _, base64_data = img_url.split(",", 1)
                img_bytes = base64.b64decode(base64_data, validate=True)
                validated = _validate_image_bytes(img_bytes, source="modalities_data_url")
                if validated is not None:
                    image_bytes_list.append(validated)
            except Exception as e:
                logger.error(f"Base64 decode failed: {e}")
        elif img_url.startswith("http"):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_url, timeout=30) as resp:
                        if resp.status == 200:
                            content_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                            if content_type and not content_type.startswith("image/"):
                                logger.warning("[NativeImage] 远端响应不是图片 Content-Type=%s: %s", content_type or "-", img_url[:120])
                                continue
                            img_bytes = await resp.read()
                            validated = _validate_image_bytes(img_bytes, source=img_url)
                            if validated is not None:
                                image_bytes_list.append(validated)
                        else:
                            logger.warning(f"Download image {img_url} failed: {resp.status}")
            except Exception as e:
                logger.error(f"Download image {img_url} error: {e}")

    return ImageTaskResult(
        images=image_bytes_list,
        text=text,
        refusal=refusal,
        finish_reason=finish_reason,
        endpoint="/chat/completions",
        usage=getattr(response, "usage", None),
    )
