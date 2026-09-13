"""四种 agentic 循环实现：OpenAI 兼容流式 / Gemini 原生流式 / 原生图片 / 原生视频。

从 ai_handlers.py 拆分而来。

v2.6：Gemini 从 OpenAI 兼容层（v1beta/openai/）非流式专用循环切换为
原生 API 流式桥接（ai/gemini_bridge.py，streamGenerateContent SSE +
原生 function calling），架构与 anthropic_bridge.py 同构的“边界转换”
模式：loop_messages 全程 OpenAI 形状，仅请求前后做协议转换，复用同一
套 _run_tool_calls_and_append 工具执行咽喉与草稿流式 UI。旧的
_agentic_loop_gemini_openai_compat 已移除。
"""
import asyncio
import json
import aiohttp
import httpx
import base64
import re
import uuid
from typing import TYPE_CHECKING, Any, Optional
from openai import AsyncOpenAI, BadRequestError

from config import (
    SUPPORTED_MODELS,
    get_sampling_params,
    get_reasoning_request_fields,
    get_effective_endpoint,
    ModelConfig,
)
from state import get_llm_session_key
from utils import get_logger, send_rich_html_message, escape_media_url_attr
from markdown_converter import convert_markdown_to_telegram_html
from chat_actions import (
    chat_action_scope,
    start_chat_action,
    stop_chat_action,
)
from s3_utils import upload_bytes_to_r2

from ai._constants import (
    MAX_TOOL_CALLS,
    MAX_PLAIN_TEXT_TOOL_CALL_RETRIES,
    OPENROUTER_PROVIDER_PREFERENCES,
    TIMEOUT,
)
from ai.error_formatting import (
    _format_api_error_notice,
    _format_image_metadata_caption,
    _format_image_safety_notice,
    _format_video_metadata_caption,
    _is_content_safety_error,
    extract_error_body_text,
    get_error_notification_message,
)
from ai.media_generation import (
    _clean_prompt_for_image_model,
    _extract_image_items,
    _extract_native_message_text,
    _extract_native_refusal_text,
    _format_native_image_notice,
    _get_images_api_display_name,
    _request_agnes_video,
    _request_images_generations,
    _request_openrouter_video,
    _response_items_to_bytes,
    _upload_generated_images_to_r2,
    _validate_image_bytes,
)
from ai.tool_summary import (
    _contains_textual_tool_call,
    _generate_action_description,
    _generate_initial_tool_summary,
    _generate_pending_tool_summary,
    _normalize_tool_call_arguments,
    _safe_parse_args,
    _strip_textual_tool_calls,
    _tool_limit_summary,
)
from ai.attachment_content import _apply_cache_control
# bridge_common 骨架：switch_stream 状态机 / 工具批次 / over-limit 合成 /
# 终局兜底 / assistant 组装 —— 与两条原生 bridge 循环共用同一份实现，
# 消除本循环内逐字重复的内联版本。
from ai.bridge_common import (
    LiveAssistantSlot,
    MediaProgressSlot,
    ensure_final_content,
    finish_open_tool_group,
    make_switch_stream,
    over_limit_final_summary,
    run_tool_batch,
)
from ai.strict_tools import (
    looks_like_strict_tool_rejection,
    mark_strict_tools_rejected,
    strict_tools_for_request,
)
from core.images import ImageTask
from core.messages import Message, TextBlock, ImageBlock, VideoBlock, render_openai_messages
from protocols.images import dispatch_image_task

# Anthropic 原生 Messages API 专用循环：独立实现，位于 anthropic_bridge.py
# （职责分离 + 避免本已很大的文件继续膨胀）。此处重导出保持调用方
# "from ai.agentic_loops import _agentic_loop_anthropic"
# 这一路径可用，与 _agentic_loop_openai_compat / _agentic_loop_gemini_native
# 并列，风格一致。
from ai.anthropic_bridge import _agentic_loop_anthropic  # noqa: F401

# Gemini 原生 API（streamGenerateContent SSE + 原生 function calling）专用
# 循环：独立实现位于 gemini_bridge.py，与 anthropic_bridge 同构的边界转换
# 模式。重导出保持调用方旧导入路径可用。
from ai.gemini_bridge import _agentic_loop_gemini_native  # noqa: F401

# Prompt cache 命中观测（三循环共用）：拆至独立模块 cache_usage，避免
# gemini_bridge -> agentic_loops 的循环导入。此处重导出保持旧路径可用。
from ai.cache_usage import (  # noqa: F401
    _cached_from_usage,
    _extract_cache_usage,
    _log_cache_usage,
)

if TYPE_CHECKING:
    # 仅供类型注解使用；运行时由调用方传入，避免运行时循环导入。
    from ai.draft_manager import DraftManager

logger = get_logger(__name__)

def _merge_tool_call_delta(accumulator: dict, index: int, delta_tc: dict) -> None:
    if index not in accumulator:
        accumulator[index] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    entry = accumulator[index]
    if delta_tc.get("id"):
        entry["id"] = delta_tc["id"]
    fn = delta_tc.get("function", {})
    if fn.get("name"):
        entry["function"]["name"] += fn["name"]
    if fn.get("arguments"):
        entry["function"]["arguments"] += fn["arguments"]


def _openrouter_session_id(chat_id: Optional[int]) -> str:
    """返回当前会话的 LLM 网关亲和键（≤256 字符，按 chat 稳定、可轮换）。

    键格式：tg-chat-{chat_id}-{纪元 token}（见 state.get_llm_session_key）：
    - 同一对话窗口内的所有任务（主循环、子 agent、TIMER 回合）共用同一键：
      OpenRouter 的默认会话识别靠"首条 system + 首条非 system 消息"哈希，
      本项目的上下文窗口虽然只在自动压缩事件中收缩（事件之间前缀字节
      稳定，见 context_window.py），但事件发生时历史仍会被重写（滚动摘要
      合并、工具负载归档为指针）。显式传 session_id 后：粘性路由从第一次
      请求就生效（无需先观察到缓存命中），且不随压缩事件漂移；对经
      OpenRouter 转发的 Z.AI/GLM 还会作为会话亲和键下发，进一步提升缓存
      命中。
    - 用户清空对话（/clear）后纪元 token 轮换，产生全新 session_id，
      避免旧会话的路由亲和性与旧前缀缓存干扰新对话（见
      state.safe_clear_history）。
    """
    if chat_id is None:
        return ""
    return get_llm_session_key(chat_id)


def _openrouter_extra_body(
    chat_id: Optional[int] = None,
    supports_prompt_cache: bool = False,
    session_key: Optional[str] = None,
) -> dict:
    body: dict = {"provider": OPENROUTER_PROVIDER_PREFERENCES.copy()}
    # session_key 由调用方在 loop 开始时解析一次传入（保证同一 loop 内
    # 全部轮次共用同一键）；未传入时按 chat_id 现场解析（子 agent 等
    # 调用方兼容路径）。
    key = session_key or _openrouter_session_id(chat_id)
    if key:
        body["session_id"] = key
    # Anthropic 系模型的"自动缓存"：顶层 cache_control 由网关翻译成打在
    # 最后一个可缓存块上的断点，并随对话增长自动前移。这让 agentic loop
    # 的每一轮（含 tool 结果之后的内容）都能作为缓存前缀被下一轮命中，
    # 下一轮用户请求也能直接命中上一轮的完整前缀（含工具调用中段）。
    # 与 _apply_cache_control 的显式断点叠加后总数不超过 Anthropic 的
    # 4 断点上限（3 显式 + 1 自动）。
    if supports_prompt_cache:
        body["cache_control"] = {"type": "ephemeral"}
    return body


# =====================================================================
# 会话亲和（session affinity）：agnes 等聚合网关的多副本缓存隔离缓解。
# 背景（2026-09 排查结论，agnes 缓存命中率偏低的根因）：
#   apihub.agnes-ai.com 实测链路为 Cloudflare -> new-api -> LiteLLM ->
#   多个上游推理副本（响应头 x-litellm-model-name / x-new-api-version），
#   按请求随机分发且各副本前缀缓存互相隔离。直接 API 探针证实：同一
#   逐字节稳定前缀的连续请求，命中率随落在哪个副本在 0%~100% 间随机
#   波动；插入另一会话的噪声请求后，原会话甚至共享 system prompt 头
#   都可能瞬间清零。客户端侧 prompt 构建已逐字节稳定（system prompt
#   当日内稳定、历史只追加、R2 预签名 URL 55 分钟内记忆化），非根因。
#   OpenRouter 同样的问题靠 body.session_id 粘性路由解决；agnes 未见
#   官方文档，这里 best-effort 同时下发 body.session_id 与
#   X-Session-Id 请求头（键格式与 OpenRouter 完全一致）：网关任何一层
#   支持任一形式即可从第一个请求起粘住同一副本；都不支持时未知字段
#   /请求头被安全忽略，零副作用（已实测带 session_id 的请求 HTTP 200）。
# =====================================================================
def _session_affinity_key(chat_id: Optional[int] = None, session_key: Optional[str] = None) -> str:
    """会话亲和键：与 OpenRouter 的 session_id 同源同格式（可轮换）。"""
    return session_key or _openrouter_session_id(chat_id)


def _session_affinity_body(
    api_label: str,
    chat_id: Optional[int] = None,
    session_key: Optional[str] = None,
    model_info: Optional[ModelConfig] = None,
) -> dict:
    """声明了 session_affinity 的网关返回 body 级亲和键，否则空 dict。

    优先读取 model_info 的"有效端点"（含模型级覆盖）；未传 model_info 时
    退回按 api_label 直接查 PROVIDERS（旧调用路径兼容，此时看不到模型级
    session_affinity 覆盖）。
    """
    session_affinity = False
    if model_info is not None:
        from config import get_effective_endpoint
        try:
            session_affinity = get_effective_endpoint(model_info).session_affinity
        except ValueError:
            session_affinity = False
    else:
        from config import PROVIDERS
        cfg = PROVIDERS.get(api_label)
        session_affinity = bool(cfg and getattr(cfg, "session_affinity", False))

    if not session_affinity:
        return {}
    session = _session_affinity_key(chat_id, session_key)
    if not session:
        return {}
    return {"session_id": session}


def _session_affinity_headers(
    api_label: str,
    chat_id: Optional[int] = None,
    session_key: Optional[str] = None,
    model_info: Optional[ModelConfig] = None,
) -> Optional[dict]:
    """声明了 session_affinity 的网关返回请求头级亲和键，否则 None。

    OpenAI SDK 的 create(**params) 接受 extra_headers 逐请求透传，
    不影响按 model_id 缓存的 AsyncOpenAI 客户端实例。
    """
    body = _session_affinity_body(api_label, chat_id, session_key, model_info=model_info)
    session = body.get("session_id")
    if not session:
        return None
    return {"X-Session-Id": session}


def _merged_extra_body(
    api_label: str,
    reasoning_extra: Optional[dict],
    chat_id: Optional[int] = None,
    supports_prompt_cache: bool = False,
    session_key: Optional[str] = None,
    model_info: Optional[ModelConfig] = None,
) -> Optional[dict]:
    """
    合并 OpenRouter 路由偏好 / 会话亲和键与推理控制字段，返回应传给
    create() 的 extra_body；两个来源都为空时返回 None（不发送 extra_body）。

    reasoning_extra 来自 config.get_reasoning_request_fields()，例如：
      openrouter  -> {"reasoning": {"enabled": True, "effort": "high"}}
      glm         -> {"thinking": {"type": "enabled"}}
      modelscope  -> {"enable_thinking": True}
    这些字段均不会与 provider 键冲突，直接字典合并即可。

    model_info 若提供，session_affinity 判断走该模型的"有效端点"
    （含模型级覆盖）；不提供则退回按 api_label 查厂商默认。
    """
    body = None
    if api_label == "openrouter":
        body = _openrouter_extra_body(
            chat_id=chat_id,
            supports_prompt_cache=supports_prompt_cache,
            session_key=session_key,
        )
    else:
        # agnes 等声明了 session_affinity 的聚合网关：body 级会话亲和键
        # （多副本缓存隔离缓解，详见 _session_affinity_body 上方说明）。
        affinity_body = _session_affinity_body(api_label, chat_id, session_key, model_info=model_info)
        if affinity_body:
            body = affinity_body
    if reasoning_extra:
        body = {**(body or {}), **reasoning_extra}
    return body


# =============================================================================
# 网关侧媒体拉取瞬态失败判定（400 upstream_error 家族）
# -----------------------------------------------------------------------------
# 特征场景（2026-09-11 生产 [3a64f5cd]）：agnes-3.0-flash 等只接受 URL 输入
# 的网关，每次请求都要自行下载消息历史里的媒体 URL（R2 预签名地址）。
# R2 跨区域下载存在抖动——同一张图上一轮下载成功（3s），下一轮即超时：
#   400 {"message": "... An exception occurred while loading IMAGE data at
#        index 0: ... Timed out while downloading media URL: https://...
#        ?X-Amz-Expires=...", "type": "upstream_error"}
# 这类错误表明"网关拉取我们引用的媒体失败"，请求体本身没有问题——在
# 首个增量前重放是安全的（与下方 httpx.ReadTimeout 重试同语义）。
# 必须与请求形状类 400（参数错误 / schema 拒绝，需要模型自纠）严格区分，
# 后者绝不能吞掉重试。
#
# 重放梯子（2026-09-11 [5332ea8f] 之后的升级，用户指示"用 base64 通用兑底"）：
#   尝试 0（URL）→ 失败 → 1.5s 后原样重放（尝试 1，URL，抖动自愈）
#     → 再失败 → 把消息里的 http(s) 图片全部内联为 base64 data URI 后重放
#       （尝试 2，_inline_wire_images_as_data_urls，通用兑底）。
# 依据：Agnes 图像文档明确输入图像支持 Data URI Base64（chat 的
# image_url 同样接受 data:image/...;base64,...，内部 ImageBlock 早已
# 在 R2 不可用时走同形状）；图像端点链路（media_generation）更是本来就
# 自行下载参考图后以 data URI 内联。内联后网关无需再访问 R2，彻底绕开
# 其下载链路——不再受 R2 慢窗口连续覆盖的影响（[5332ea8f] 实证同一
# 慢窗口会连续击落 URL 重放）。视频/音频/文档不内联：体积可达数十 MB，
# 内联会让请求体爆炸，仍走 URL（若命中这三类媒体拉取失败，内联数为 0
# 时按无兑底可用快速抛出，见重放分支）。
# =============================================================================
_GATEWAY_MEDIA_FETCH_ERROR_MARKERS = (
    "timed out while downloading media url",
    "an exception occurred while loading image data",
    "an exception occurred while loading video data",
    "an exception occurred while loading audio data",
    "an exception occurred while loading file data",
)

# 媒体拉取失败的首增量前重放次数上限（总请求数 = 1 + _MEDIA_FETCH_MAX_REPLAYS）。
# 生产实证（2026-09-11 [5332ea8f]）：R2 慢窗口往往不止覆盖一次重试——首
# 次请求与 1.5s 后的 URL 重放都撞在同一个窗口里双双 400。第二次重放
# 不再原样重试 URL，而是改用 base64 内联通用兑底（见上方梯子说明），
# 上限仍然是硬闸门，绝不无限重试。
_MEDIA_FETCH_MAX_REPLAYS = 2

# base64 内联兑底的单图字节上限（Telegram 照片通常 ≤ 数 MB；超过上限
# 的图片保持 URL 不动，避免单个请求体失控）。
_MEDIA_INLINE_MAX_BYTES = 15 * 1024 * 1024
# 内联兑底下载阶段的总超时：兑底本身已是第三次尝试，不能在下载上久等。
_MEDIA_INLINE_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5)

# URL 扩展名 → image MIME 的兑底映射（magic sniff 与 Content-Type 都拿
# 不到时用；大小写不敏感）。
_MEDIA_INLINE_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _sniff_image_mime(data: bytes) -> str:
    """按 magic bytes 嗅探常见图片 MIME；认不出返回空串。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return ""


def _media_fetch_replay_mode(failed_stream_attempt: int) -> str:
    """第 failed_stream_attempt 次尝试刚失败后，本次重放应采用的形态。

    - "url"：原样重放同一请求（首次抖动自愈；请求体逐字节不变，
      幂等性最强，对 prompt cache 最友好）。
    - "inline"：base64 内联兑底重放（把 http(s) 图片替换为 data URI，
      网关不再需要下载）。

    纯判定便于单测；调用方先用 _should_retry_media_fetch_400 过闸门，
    本函数只在闸门放行后被调用（即 failed_stream_attempt ∈ {0, 1}）。
    """
    return "url" if failed_stream_attempt <= 0 else "inline"


async def _inline_wire_images_as_data_urls(
    wire_messages: list,
    *,
    chat_id: int | None = None,
    _fetch=None,
    _resolve_file_id=None,
) -> tuple[int, int]:
    """把 wire 消息里所有 http(s) 图片内联为 base64 data URI（原地替换）。

    base64 通用兑底的核心步骤：Agnes 等网关对输入图像同时接受公共 URL
    与 Data URI Base64（图像端点文档明示；chat image_url 同形状）。当
    网关侧下载 URL 连续失败时，由我方自行下载图片字节（我方到 R2 的
    链路正常，超时只发生在网关出口），替换 ``image_url.url`` 后重放，
    网关彻底不再需要访问 R2。

    处理范围与边界：
      - 只处理 ``{"type": "image_url", "image_url": {"url": ...}}`` part；
        ``video_url`` / ``file`` / ``input_audio`` 明确不内联（视频可达
        数十 MB，内联会让请求体爆炸），保持 URL 原样；
      - 已经是 ``data:`` 的 part 天然跳过；
      - 下载失败 / 非 200 / 超过 _MEDIA_INLINE_MAX_BYTES 的图片保持 URL
        原样（部分兑底：能内联几张是几张）；
      - MIME 判定顺序：magic bytes 嗅探 → 响应 Content-Type（仅接受
        image/*）→ URL 扩展名 → image/jpeg 兑底；
      - ``image_url.detail`` 等其它字段原样保留；
      - 原地替换传入的 wire dicts，无返回值副作用面（wire 消息是本轮
        临时渲染产物，下一轮 _round 会从内部消息重新渲染，不污染历史）。

    Args:
        wire_messages: render_openai_messages 产出的 OpenAI wire dict 列表。
        _fetch: 测试注入点——``async def(url) -> tuple[bytes, str]``
            （返回响应字节与 Content-Type）；默认 None 时用 aiohttp 真实下载。

    Returns:
        (成功内联数, 失败保持 URL 数)。没有 http 图片时返回 (0, 0)。

    ``chat_id`` / ``_resolve_file_id``：优先从本轮 attachment layer 建立的
    URL->file_id 映射恢复原始附件字节，再退回直接下载 URL。这样网关
    首轮拉取失败时，不会因为“二次下载同一个预签名 URL”再丢掉其中一张图。
    """
    targets: list[tuple[dict, str]] = []
    for msg in wire_messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            inner = part.get("image_url")
            url = inner.get("url") if isinstance(inner, dict) else None
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                targets.append((part, url))
    if not targets:
        return 0, 0

    async def _default_fetch(url: str) -> tuple[bytes, str]:
        async with aiohttp.ClientSession(timeout=_MEDIA_INLINE_FETCH_TIMEOUT) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history,
                        status=resp.status, message=f"status {resp.status}",
                    )
                # OOM 防护：Content-Length 预拒绝 + 分块累积超限即中止
                # （服务器不声明长度/谎报时，逐块检查仍然生效）。
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > _MEDIA_INLINE_MAX_BYTES:
                    raise ValueError(f"image exceeds inline cap: {declared} bytes")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    total += len(chunk)
                    if total > _MEDIA_INLINE_MAX_BYTES:
                        raise ValueError(f"image exceeds inline cap: >{_MEDIA_INLINE_MAX_BYTES} bytes")
                    chunks.append(chunk)
                return b"".join(chunks), (resp.headers.get("Content-Type") or "").strip()

    fetch = _fetch or _default_fetch

    async def _inline_one(part: dict, url: str) -> bool:
        # 最优先：通过本轮附件解析阶段建立的 URL -> file_id 映射，直接
        # 复用 get_cached_image_data。这里拿到的字节与首轮 ImageBlock
        # 使用的是同一附件，避免再次依赖 R2 预签名 URL 的匿名 HTTP GET。
        if _resolve_file_id is not None and chat_id is not None:
            try:
                file_id = str(_resolve_file_id(url) or "")
            except Exception:
                file_id = ""
            if file_id:
                try:
                    from ai.attachment_content import get_cached_image_data
                    data = await get_cached_image_data(chat_id, file_id)
                    if data:
                        mime = _sniff_image_mime(data) or "image/jpeg"
                        inner = part.get("image_url")
                        if isinstance(inner, dict):
                            inner["url"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                            return True
                except Exception as e:
                    logger.warning(
                        "base64 内联兑底：按 file_id 复用图片字节失败，回退 URL 下载：%s… (%s: %s)",
                        url[:120], type(e).__name__, str(e)[:120],
                    )

        try:
            data, content_type = await fetch(url)
        except Exception as e:
            logger.warning(
                "base64 内联兑底：图片下载失败，保持 URL 原样：%s… (%s: %s)",
                url[:120], type(e).__name__, str(e)[:120],
            )
            return False
        if not data:
            return False
        mime = _sniff_image_mime(data)
        if not mime and content_type.lower().startswith("image/"):
            mime = content_type.split(";", 1)[0].strip().lower()
        if not mime:
            ext = url.split("?", 1)[0].rsplit(".", 1)
            if len(ext) == 2:
                mime = _MEDIA_INLINE_EXT_MIME.get(f".{ext[1].lower()}", "")
        if not mime:
            mime = "image/jpeg"
        inner = part.get("image_url")
        if not isinstance(inner, dict):
            return False
        inner["url"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return True

    results = await asyncio.gather(*(_inline_one(part, url) for part, url in targets))
    inlined = sum(1 for ok in results if ok)
    return inlined, len(results) - inlined


def _looks_like_transient_media_fetch_error(exc: BaseException) -> bool:
    """判断是否为网关拉取媒体 URL 的瞬态失败（可重试）。

    按错误文本的大小写不敏感子串匹配媒体拉取层标记，覆盖
    LiteLLM/OpenAIException 等网关的层层包装变体。只匹配显式的
    "加载 image/video/audio/file 数据失败" 与 "下载媒体 URL 超时"，
    不匹配宽泛的 upstream_error（避免把非媒体类的上游错误误判为
    可重试，吞掉真正需要暴露的请求问题）。
    """
    text = str(exc or "").lower()
    return any(marker in text for marker in _GATEWAY_MEDIA_FETCH_ERROR_MARKERS)


def _should_retry_media_fetch_400(
    exc: BaseException, *, received_any: bool, stream_attempt: int
) -> bool:
    """400 媒体拉取失败是否应重放（纯判定，便于单测）。

    条件三者缺一不可：
      - 尚未收到任何流式增量（重放幂等；已向用户/工具状态写入增量
        则重放会产生半个模型回合，必须直接抛出）；
      - 尚未用完重试机会（stream_attempt < _MEDIA_FETCH_MAX_REPLAYS，
        至多补试两次；重放形态见 :func:`_media_fetch_replay_mode`——
        第一次原样 URL 重放，第二次 base64 内联兑底重放）；
      - 错误形状确属网关媒体拉取失败（见
        :func:`_looks_like_transient_media_fetch_error`）。
    """
    return (
        not received_any
        and stream_attempt < _MEDIA_FETCH_MAX_REPLAYS
        and _looks_like_transient_media_fetch_error(exc)
    )


async def _agentic_loop_openai_compat(
        client: AsyncOpenAI, current_model: str, messages: list, api_label: str,
        builder: "DraftManager", tools: Optional[list[dict[str, Any]]] = None, supports_tools: bool = True,
        journal: Optional[list[dict[str, Any]]] = None,
) -> tuple[str | None, object | None, list]:
    """OpenAI 兼容 Chat Completions 流式循环（内部消息 -> 协议渲染）。

    重构说明：本循环与其它协议循环共用内部消息（core.messages.Message），
    每轮请求前统一经 render_openai_messages 渲染为 OpenAI wire JSON；
    prompt cache 断点打在渲染后的 wire dict 上（缓存标记是纯出站装饰，
    不进入内部消息）。循环内追加的 assistant / tool / 纠错消息全部为
    Message 对象。
    """
    if tools is None:
        from search_engine import SEARCH_TOOLS
        tools = SEARCH_TOOLS
    loop_messages = list(messages)
    final_content = None
    final_usage = None
    # 本轮最近一份带缓存字段的 usage 快照（流式网关可能丢弃
    # prompt_tokens_details，见 _extract_cache_usage 上方说明）。
    usage_with_cache = None
    tool_call_count_ref = [0]
    # 连续相同工具错误的熔断计数：跨本轮全部工具批次共享，见
    # bridge_common.run_tool_batch 与 tool_call_loop._run_tool_calls_and_append
    # 的 error_streak 参数说明。
    error_streak: dict = {}
    # journal：由 get_ai_response 注入的轮次日志（打断保全用，见 turn_recovery.py）。
    # 注入时循环直接往里追加，轮次被打断时已完成的消息不至于丢失。
    new_history_entries = journal if journal is not None else []
    plain_text_tool_attempts = 0
    parallel_tool_calls = True

    model_info = SUPPORTED_MODELS.get(current_model)
    max_tokens = model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192
    # 采样与推理控制统一来自 config.py（含 per-model 覆盖），禁止在此硬编码。
    sampling_params = get_sampling_params(model_info)
    reasoning_top, reasoning_extra = get_reasoning_request_fields(model_info, api_label)
    prompt_cache_enabled = bool(model_info and model_info.supports_prompt_cache)
    # 会话亲和键在整个 loop 内只解析一次：loop 内全部轮次（含工具追加轮）
    # 共用同一 session_id，保证粘性路由与前缀缓存不因中途轮换漂移；
    # 三个请求出口（主流式/非流式兑底/over-limit 合成）共用。
    loop_session_key = _openrouter_session_id(builder.chat_id)
    # 会话亲和请求头（agnes 等聚合网关多副本缓存隔离缓解）：声明了
    # session_affinity 的网关才非空。
    affinity_headers = _session_affinity_headers(
        api_label, builder.chat_id, session_key=loop_session_key, model_info=model_info
    )

    # L0 预防层（主流：OpenAI Structured Outputs）：对能安全规范化的工具
    # 注入 strict:true + 递归 schema 规范化（全部必填 + additionalProperties:false
    # + 可选字段可空）。per-tool best-effort，schema 复杂（union/anyOf）的工具
    # 原样发送；网关拒绝时运行时自动降级（见下方 except BadRequestError）。
    # 手动总开关：环境变量 DISABLE_STRICT_TOOL_SCHEMA=1。
    request_tools = strict_tools_for_request(api_label, tools)

    for _round in range(MAX_TOOL_CALLS):
        added_tool_indices = set()
        last_arg_len: dict[int, int] = {}
        # 每个 tool_call 索引当前使用的 UI 条目
        # id（占位阶段为 pending_* id）与已确认的函数名。部分网关会先流式
        # 传输参数增量、把 id/函数名拖到很晚才补发；占位条目保证工具块在
        # 第一片增量到达时就上屏，id/名称到达后再原地改绑/回填。
        stream_item_ids: dict = {}
        stream_item_names: dict = {}

        # OpenAI 兼容路径的手动缓存（OpenRouter 上 Anthropic 系模型）：
        # 每轮请求前重打显式 cache_control 断点（函数内部先回收旧标记再
        # 从尾部重新分配，总量恒 ≤3，幂等）。仅靠入口处（get_ai_response
        # 预处理）一次性打标的话，loop 内追加的 tool 结果与 assistant
        # (tool_calls) 消息会落在全部显式断点之外；每轮重打让最新的
        # 工具输出进入显式缓存覆盖，loop 内 2..N 轮直接命中到最新工具
        # 结果为止的完整前缀。与 extra_body 顶层自动断点（第 4 个）叠加。
        # 非流式兜底与 over-limit 合成路径复用同一份 loop_messages，
        # 无需重复打标。
        # 重构说明（Internal Message）：断点打在渲染后的 wire dict 上
        # （每轮重新渲染，天然无旧标记残留）；内部 Message 不携带任何
        # 缓存装饰——cache_control 是纯出站协议装饰。
        wire_messages = render_openai_messages(loop_messages)
        if prompt_cache_enabled:
            _apply_cache_control(wire_messages)

        content_acc = ""
        reasoning_acc = ""
        tool_calls_acc: dict = {}
        # 打断保全（改动点1）：本轮 assistant 消息的实时占位——流式期间
        # journal 始终持有一条与 content_acc / reasoning_acc 同步的消息，
        # 取消发生在任何 await 点上都能被 finalize_interrupted_turn 保全。
        # tool_calls 只在 finalize（流正常结束）写入（改动点2：流式中途
        # 的半截参数 JSON 整体丢弃，绝不进历史）。四条循环同构接入。
        live_slot = LiveAssistantSlot(new_history_entries)
        # 每轮重置缓存字段快照：hint 必须与本轮 token 数同源，
        # 跨轮复用会把上一轮的命中量安到本轮头上。
        usage_with_cache = None
        in_reasoning = False
        received_any = False
        # v2.5：本轮流结束原因（length / stop / tool_calls / content_filter…）。
        # 初始 None = 尚未见到任何终止事件；流被完整消费却仍为 None 时，
        # 归一化层会把 ""（断流证据）传入诊断信封——空参数/截断参数的
        # 根因自此可被准确区分（输出上限 vs 断流 vs 模型自身语法错）。
        stream_finish_reason: Optional[str] = None
        # 本轮"第一个出现的内容类型"：'tool'（先出现工具调用）或 'content'（先出现思考/文本）。
        # 只有第一次出现时才据此决定是否要关闭上一个未闭合的工具块，之后不再重复判断。
        round_leading_kind = None

        # 草稿流切换状态机（与两条原生 bridge 循环共用 make_switch_stream）：
        # 同一流类型幂等返回；切换前结束当前流，并在"此前确有流"时触发
        # 块边界换草稿检查点（非阻塞事件，由 DraftManager 容量阈值决定
        # 是否真实滚动，§8/§9）。状态存于 cell，代替本循环原先逐字相同
        # 的 nonlocal current_stream 闭包。
        current_stream_cell = [None]
        switch_stream = make_switch_stream(builder, current_stream_cell)

        try:
            # SDK create() 重载不接受 dict[str, object] 的 ** 解包；
            # 请求载荷本就是动态 JSON 形状，按 Any 标注。
            create_params: dict[str, Any] = {
                "model": current_model,
                "messages": wire_messages,
                "stream": True,
                "max_tokens": max_tokens,
                "stream_options": {"include_usage": True},
            }
            create_params.update(sampling_params)
            create_params.update(reasoning_top)
            if supports_tools and tools:
                create_params["tools"] = request_tools
                create_params["tool_choice"] = "auto"
                create_params["parallel_tool_calls"] = parallel_tool_calls
            extra_body = _merged_extra_body(
                api_label, reasoning_extra,
                chat_id=builder.chat_id,
                supports_prompt_cache=prompt_cache_enabled,
                session_key=loop_session_key,
                model_info=model_info,
            )
            if extra_body is not None:
                create_params["extra_body"] = extra_body
            if affinity_headers:
                create_params["extra_headers"] = affinity_headers

            # 某些聚合网关会在长工具链后的首个 SSE 事件前沉默较久。
            # 只有尚未收到任何增量时，重试相同请求才是幂等且安全的；一旦已经向
            # 用户或工具状态写入增量，必须直接抛出，避免重放半个模型回合。
            # attempt 上限取 1 + _MEDIA_FETCH_MAX_REPLAYS 与 ReadTimeout 旧上限
            # 的较大者：ReadTimeout 仍由自身闸门（stream_attempt >= 1 即抛）
            # 保持"只补试一次"的旧语义，不因 attempt 空间变大而多试。
            for stream_attempt in range(1 + _MEDIA_FETCH_MAX_REPLAYS):
                try:
                    comp_stream = await client.chat.completions.create(**create_params)
                    # typing 状态：仅在本轮真实消费流式增量（思考/文本字段）
                    # 期间显示——“模型正在打字”。状态每 4 秒循环重发：
                    # Telegram 的 chat action 最多持续约 5 秒，且 bot 发出的
                    # 任何消息（含草稿刷新）都会清除指示；/show on 时草稿
                    # 流式本身就是输入可视化，指示被反复清除属预期设计。
                    # 非流式回退（下方 stream=False）不触发 typing：一次性
                    # 返回完整文本等价于直接粘贴发送，没有输入过程。
                    await start_chat_action(builder.chat_id, "typing")
                    async for chunk in comp_stream:
                        received_any = True
                        if getattr(chunk, "usage", None):
                            final_usage = chunk.usage
                            if _cached_from_usage(chunk.usage) is not None:
                                usage_with_cache = chunk.usage
                        choices = chunk.choices or []
                        if not choices:
                            continue
                        fr = getattr(choices[0], "finish_reason", None)
                        if fr:
                            stream_finish_reason = str(fr)
                        delta = choices[0].delta
                        c_delta = getattr(delta, "content", None) or ""
                        if isinstance(c_delta, list):
                            c_delta = "".join(str(item) for item in c_delta)

                        r_delta = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None) or ""
                        if isinstance(r_delta, list):
                            r_delta = "".join(str(item) for item in r_delta)
                        if r_delta:
                            if round_leading_kind is None:
                                round_leading_kind = "content"
                                if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                                    builder.finish_group(len(builder._tool_groups) - 1)
                                    # ★ 强制刷新，确保总结先于思考内容显示 ★
                                    # （非阻塞：request_flush 由后台合并循环发送）
                                    builder.request_flush(force=True)
                            await switch_stream("reasoning")
                            reasoning_acc += r_delta
                            builder.append_stream_delta(r_delta)
                            live_slot.sync(content_acc, reasoning_acc)

                        if c_delta:
                            content_acc += c_delta
                            # 打断保全：增量落地后立即同步 journal 占位（
                            # 与 builder._stream_buffer 同一制律）；下方
                            # 思考标记分区后还会精化一次 reasoning 部分。
                            live_slot.sync(content_acc, reasoning_acc)
                            if round_leading_kind is None:
                                round_leading_kind = "content"
                                if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                                    builder.finish_group(len(builder._tool_groups) - 1)
                                    # ★ 强制刷新，确保总结先于文本内容显示 ★
                                    # （非阻塞：request_flush 由后台合并循环发送）
                                    builder.request_flush(force=True)
                            if round_leading_kind == "tool" and builder._tool_groups and not builder._tool_groups[-1].get(
                                    "finished", False):
                                # 本轮先出现了工具调用，这段文字是同一轮里紧跟在工具调用之后的说明文字，
                                # 归入当前（同一轮新开或合并的）工具块内部。
                                builder.append_to_current_tool_group_text(c_delta)
                            else:
                                if "<think>" in c_delta:
                                    in_reasoning = True
                                    before, _, rest = c_delta.partition("<think>")
                                    if before:
                                        await switch_stream("content")
                                        builder.append_stream_delta(before)
                                    await switch_stream("reasoning")
                                    if "</think>" in rest:
                                        think_part, _, after = rest.partition("</think>")
                                        reasoning_acc += think_part
                                        builder.append_stream_delta(think_part)
                                        in_reasoning = False
                                        if after:
                                            await switch_stream("content")
                                            builder.append_stream_delta(after)
                                        else:
                                            current_stream_cell[0] = None
                                    else:
                                        reasoning_acc += rest
                                        builder.append_stream_delta(rest)
                                elif in_reasoning:
                                    if "</think>" in c_delta:
                                        think_part, _, after = c_delta.partition("</think>")
                                        reasoning_acc += think_part
                                        builder.append_stream_delta(think_part)
                                        in_reasoning = False
                                        if after:
                                            await switch_stream("content")
                                            builder.append_stream_delta(after)
                                        else:
                                            current_stream_cell[0] = None
                                    else:
                                        reasoning_acc += c_delta
                                        builder.append_stream_delta(c_delta)
                                else:
                                    await switch_stream("content")
                                    builder.append_stream_delta(c_delta)
                            # 思考标记分区已把 reasoning_acc 补齐：再同步一次
                            # 占位，保证 journal 与两个累积器的最终状态一致
                            # （该语句无 await，与下一轮 chunk 之间不存在取消窗口）。
                            live_slot.sync(content_acc, reasoning_acc)

                        for tc_delta in (getattr(delta, "tool_calls", None) or []):
                            idx = getattr(tc_delta, "index", 0)
                            _merge_tool_call_delta(
                                tool_calls_acc, idx,
                                {"id": getattr(tc_delta, "id", "") or "",
                                 "function": {"name": getattr(tc_delta.function, "name", "") or "",
                                              "arguments": getattr(tc_delta.function, "arguments", "") or ""}}
                            )
                            tc = tool_calls_acc[idx]
                            tc_id = tc.get("id")
                            tc_name = tc.get("function", {}).get("name")

                            # 该工具调用的第一片增量到达就立即建条目，不要求
                            # id 与函数名都齐备：部分网关把 tool_call 的
                            # id/name 拖到流末尾才补发（参数增量却在正常
                            # 流式），若等 id/name 齐备再建条目，整段参数流期间
                            # （例如 text_editor create 写整个 file_text 的几十秒里）
                            # 都不会显示任何工具块，直到创建完毕才一次性出现完成态。
                            # 用占位 id 先建条目，id/name 到达后再原地改绑与回填。
                            if idx not in added_tool_indices:
                                if round_leading_kind is None:
                                    # 本轮第一个出现的就是工具调用：沿用/合并到上一个未闭合的工具块。
                                    round_leading_kind = "tool"
                                elif round_leading_kind == "content" and builder._tool_groups and not builder._tool_groups[
                                    -1].get("finished", False):
                                    # 本轮先出现了文本/思考才轮到工具调用：这段文本已经在上面把旧工具块
                                    # 关闭掉了，这里创建的会是全新的独立工具块，不需要再次关闭。
                                    pass
                                item_id = tc_id or f"pending_{_round}_{idx}"
                                args_str = tc.get("function", {}).get("arguments", "")
                                parsed_args = _safe_parse_args(args_str)
                                if tc_name:
                                    summary = _generate_initial_tool_summary(tc_name, parsed_args)
                                    action_desc = _generate_action_description(tc_name, parsed_args)
                                    stream_item_names[idx] = tc_name
                                else:
                                    # 函数名尚未到达：按参数形状推断（如 text_editor 的
                                    # command 枚举），推断不出时显示通用进行态文本。
                                    summary = _generate_pending_tool_summary(parsed_args)
                                    action_desc = None
                                builder.add_tool_item(
                                    item_id,
                                    tc_name or "",
                                    summary,
                                    action_description=action_desc,
                                    fn_args=parsed_args
                                )
                                stream_item_ids[idx] = item_id
                                added_tool_indices.add(idx)
                                builder.request_flush(force=False)
                            else:
                                # 已建条目：真实 id / 函数名到达时原地改绑与回填，
                                # 避免执行批次按真实 id 再建一个重复的工具块。
                                item_id = stream_item_ids.get(idx) or ""
                                known_name = stream_item_names.get(idx) or ""
                                need_rebind = bool(tc_id and item_id and tc_id != item_id)
                                need_name = bool(tc_name and tc_name != known_name)
                                if item_id and (need_rebind or need_name):
                                    builder.attach_stream_tool_identity(
                                        item_id,
                                        new_id=tc_id if need_rebind else None,
                                        tool_type=tc_name or None,
                                    )
                                    if need_rebind:
                                        stream_item_ids[idx] = tc_id
                                    if need_name:
                                        stream_item_names[idx] = tc_name

                            if idx in added_tool_indices:
                                # 工具调用参数在流式接收过程中不再实时渲染预览；
                                # 最终结果会在工具执行完成后按统一的 Input/Output 格式一次性展示。
                                # 但参数中一旦解析出模型提交的简短描述（description/_summary），
                                # 或完整 JSON 解析出 query/command/url 等字段，就立即更新摘要上屏，
                                # 不再等到整段参数流结束后才由工具批次补写。
                                # 更新一律按条目当前 id（占位或真实）寻址，占位条目同样
                                # 能在参数流式期间刷新摘要。
                                current_args = tc.get("function", {}).get("arguments", "")
                                current_len = len(current_args)
                                if current_len - last_arg_len.get(idx, 0) >= 20:
                                    last_arg_len[idx] = current_len
                                    parsed_args = _safe_parse_args(current_args)
                                    builder.update_tool_args(stream_item_ids[idx], parsed_args)
                    break
                except BadRequestError as exc:
                    # strict 工具 schema 被网关拒绝（聚合网关对 strict 的支持
                    # 参差不齐，OpenRouter/ModelScope/agnes 等转发厂商行为各异）：
                    # 标记该 api_label（进程内记忆），摘除 strict 后用原始
                    # schema 立即重试一次，后续轮次不再撞墙。报错文本不指向
                    # schema/strict 时按原样抛出，不误降级。
                    if (request_tools is not tools
                            and looks_like_strict_tool_rejection(str(exc))):
                        mark_strict_tools_rejected(api_label, str(exc))
                        request_tools = tools
                        create_params["tools"] = tools
                        continue
                    # 网关侧媒体拉取瞬态失败（如 Agnes 下载消息历史里的
                    # R2 预签名 URL 超时，400 + upstream_error）：请求体
                    # 本身没有问题，首个增量前重放是安全的——与下方
                    # ReadTimeout 重试同语义。重放梯子（见模块常量区说明）：
                    #   第 1 次重放（stream_attempt=0 失败后）：1.5s 后原样
                    #     URL 重放，抖动自愈；
                    #   第 2 次重放（stream_attempt=1 失败后）：base64 内联
                    #     通用兜底——把消息里的 http(s) 图片替换为 data URI
                    #     后重放，网关不再需要访问 R2（[5332ea8f] 实证同一
                    #     R2 慢窗口会连续击落 URL 重放，原样重试无效）。
                    # 内联数为 0（无 http 图片/全部下载失败）时兜底不可用，
                    # 快速抛出，避免注定失败的第三次 400 再等一轮。
                    if _should_retry_media_fetch_400(
                        exc, received_any=received_any, stream_attempt=stream_attempt
                    ):
                        if _media_fetch_replay_mode(stream_attempt) == "inline":
                            from ai.attachment_content import _file_id_for_image_url
                            image_part_count = sum(
                                1
                                for _m in create_params["messages"]
                                if isinstance(_m, dict) and isinstance(_m.get("content"), list)
                                for _p in _m["content"]
                                if isinstance(_p, dict) and _p.get("type") == "image_url"
                                and isinstance(_p.get("image_url"), dict)
                                and str(_p["image_url"].get("url") or "").startswith(("http://", "https://"))
                            )
                            inlined, failed = await _inline_wire_images_as_data_urls(
                                create_params["messages"],
                                chat_id=builder.chat_id,
                                _resolve_file_id=lambda url: _file_id_for_image_url(url),
                            )
                            logger.info(
                                "[%s] base64 兜底媒体统计: http_image_parts=%s inlined=%s failed=%s",
                                api_label, image_part_count, inlined, failed,
                            )
                            if inlined == 0:
                                logger.warning(
                                    "[%s] 第 %s 轮网关媒体拉取失败（400 upstream_error），"
                                    "base64 兜底无可内联图片（失败 %s 个或无 http 图片），放弃重放",
                                    api_label, _round + 1, failed,
                                )
                                raise
                            logger.warning(
                                "[%s] 第 %s 轮网关媒体拉取失败（400 upstream_error），"
                                "连续两次 URL 下载均超时——改用 base64 内联通用兜底重放"
                                "（已内联 %s 张图片，网关无需再下载；%s 张未能内联保持 URL）",
                                api_label, _round + 1, inlined, failed,
                            )
                            continue
                        logger.warning(
                            "[%s] 第 %s 轮网关媒体拉取失败（400 upstream_error），"
                            "1.5s 后第 1 次重放同一请求",
                            api_label, _round + 1,
                        )
                        await asyncio.sleep(1.5)
                        continue
                    raise
                except httpx.ReadTimeout:
                    if received_any or stream_attempt >= 1:
                        raise
                    logger.warning(
                        "[%s] 第 %s 轮模型流在首个增量前读取超时，等待后重试一次",
                        api_label, _round + 1,
                    )
                    await asyncio.sleep(1.0)
                finally:
                    # 无论本轮流式正常结束、读取超时重试还是异常/取消，
                    # 消费流式增量的过程已结束，typing 随之熄灭。
                    await stop_chat_action(builder.chat_id, "typing")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"{api_label} stream error: {e}")
            raise

        builder.end_stream()
        while builder.blocks and not builder.blocks[-1].strip() and builder.block_types[-1] in ("text", "reasoning"):
            builder.blocks.pop()
            builder.block_types.pop()

        if not received_any or (not content_acc and not tool_calls_acc):
            logger.warning(f"[{api_label}] 流式无有效内容，回退到非流式请求")
            try:
                # 与上方 create_params 同理：** 解包需 Any 值类型。
                fallback_params: dict[str, Any] = {
                    "model": current_model,
                    "messages": wire_messages,
                    "stream": False,
                    "max_tokens": max_tokens,
                }
                fallback_params.update(sampling_params)
                fallback_params.update(reasoning_top)
                if supports_tools and tools:
                    fallback_params["tools"] = request_tools
                    fallback_params["tool_choice"] = "auto"
                    fallback_params["parallel_tool_calls"] = parallel_tool_calls
                fallback_extra_body = _merged_extra_body(
                    api_label, reasoning_extra,
                    chat_id=builder.chat_id,
                    supports_prompt_cache=prompt_cache_enabled,
                    session_key=loop_session_key,
                    model_info=model_info,
                )
                if fallback_extra_body is not None:
                    fallback_params["extra_body"] = fallback_extra_body
                if affinity_headers:
                    fallback_params["extra_headers"] = affinity_headers

                # 非流式回退：create 带同样的 strict 降级重试（与流式路径
                # 同一套逻辑——网关拒绝 strict schema 时摘除后立即重试一次）。
                try:
                    resp = await client.chat.completions.create(**fallback_params)
                except BadRequestError as exc:
                    if (request_tools is not tools
                            and looks_like_strict_tool_rejection(str(exc))):
                        mark_strict_tools_rejected(api_label, str(exc))
                        request_tools = tools
                        fallback_params["tools"] = tools
                        resp = await client.chat.completions.create(**fallback_params)
                    else:
                        raise
                # 非流式回退同样需要捕获 usage，否则本轮缓存命中统计会丢失
                if getattr(resp, "usage", None):
                    final_usage = resp.usage
                    if _cached_from_usage(resp.usage) is not None:
                        usage_with_cache = resp.usage
                msg = resp.choices[0].message
                fr = str(getattr(resp.choices[0], "finish_reason", "") or "")
                if fr:
                    stream_finish_reason = fr
                content_acc = msg.content or ""
                if supports_tools and tools and hasattr(msg, "tool_calls") and msg.tool_calls:
                    for idx, tc in enumerate(msg.tool_calls):
                        tool_calls_acc[idx] = {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                        }
                if not content_acc and not tool_calls_acc:
                    content_acc = "（模型未返回任何内容）"
                try:
                    fallback_tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())] if tool_calls_acc else []
                    logger.info(
                        f"[{api_label}] 第 {_round + 1} 轮模型原始返回(回退): tool_calls={len(fallback_tool_calls)}, "
                        f"ids={[tc.get('id', '') or '' for tc in fallback_tool_calls]}, "
                        f"names={[tc.get('function', {}).get('name', '') or '' for tc in fallback_tool_calls]}, "
                        f"content_len={len(content_acc.strip())}"
                    )
                except Exception:
                    logger.exception(f"[{api_label}] 记录回退 tool_calls 日志失败")
            except Exception as e:
                # 流式与非流式都失败：向上抛给 get_ai_response 顶层异常路径
                # 统一处理（错误通知 + journal 保全 + mark_failed_unanswered_
                # user）。绝不能把"请求失败，请稍后重试。"当成功正文返回——
                # 那会被当成正常模型回复写入历史并持久化，失败轮既没有
                # ⚠️/❌ 前缀供失败守卫识别，也不会打失败标记，重试语义退化
                # （历史里模型"自己说过请求失败"，下一条消息与之合并）。
                logger.exception(f"非流式回退失败: {e}")
                raise

        tool_calls_list = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())] if tool_calls_acc else []
        # v2.5：流被完整消费却从未见到终止事件，且本轮确实产出了工具调用 →
        # 记为 ""（断流证据，非"无信息"），供归一化层定性空参数/截断参数。
        if stream_finish_reason is None and tool_calls_list:
            stream_finish_reason = ""
        try:
            tool_call_names = [tc.get("function", {}).get("name", "") or "" for tc in tool_calls_list]
            tool_call_ids = [tc.get("id", "") or "" for tc in tool_calls_list]
            logger.info(
                f"[{api_label}] 第 {_round + 1} 轮模型原始返回: tool_calls={len(tool_calls_list)}, "
                f"ids={tool_call_ids}, names={tool_call_names}, content_len={len(content_acc.strip())}, "
                f"reasoning_len={len(reasoning_acc.strip())}, finish_reason={stream_finish_reason!r}"
            )
        except Exception:
            logger.exception(f"[{api_label}] 记录 tool_calls 日志失败")
        # 每轮请求结束后立即打印本轮缓存命中统计：final_usage 每轮被覆盖，
        # 若只在循环外打印，多轮 agentic 循环中间轮次的命中情况不可观测。
        # final_usage 仍保留最后一轮的值供返回值（token 台账）使用。
        _log_cache_usage(api_label, final_usage, cache_hint=usage_with_cache, model_name=current_model)
        for idx, tc in enumerate(tool_calls_list):
            if not tc.get("id"):
                tc["id"] = f"call_{_round}_{idx}_{uuid.uuid4().hex[:8]}"
        # ★ 流结束：把仍处于占位 id 的条目改绑到最终 id（含上面补发的合成
        # id），并回填函数名。这样 _run_tool_calls_and_append 里的
        # add_tool_item(真实 id, ...) 会合并进已经显示的条目，而不是另建
        # 一个重复的工具块。acc 索引与 tool_calls_list 位置按排序键对齐。
        if stream_item_ids:
            for acc_idx in sorted(tool_calls_acc.keys()):
                tc_entry = tool_calls_acc[acc_idx]
                item_id = stream_item_ids.get(acc_idx)
                if item_id and item_id != tc_entry.get("id"):
                    builder.attach_stream_tool_identity(
                        item_id,
                        new_id=tc_entry.get("id") or None,
                        tool_type=tc_entry.get("function", {}).get("name") or None,
                    )
                    stream_item_ids[acc_idx] = tc_entry.get("id") or item_id
        _normalize_tool_call_arguments(
            tool_calls_list, api_label, _round + 1,
            stream_finish_reason=stream_finish_reason)

        if not tool_calls_list and not content_acc.strip():
            content_acc = "（模型未返回任何内容）"

        # 个别兼容模型会将 function calling XML 错当普通正文输出。该内容已经
        # 在流式阶段写入草稿，必须先从构建器撤回，避免最终消息泄漏 <tool_call>。
        textual_tool_call = _contains_textual_tool_call(content_acc)
        if textual_tool_call:
            raw_textual_content = content_acc
            content_acc = _strip_textual_tool_calls(content_acc)
            if not builder.replace_trailing_text(raw_textual_content, content_acc):
                logger.warning(
                    "[%s] 未能在草稿中定位伪工具调用文本，已阻止其进入最终内容",
                    api_label,
                )

        if reasoning_acc:
            builder.finalize_reasoning_block()

        # 块边界换草稿检查点①②（本轮最后一个块）：流已结束，最后一个思考块
        # 或文本块在此闭合，switch_stream 不会再被触发，故在此补一次检查。
        # 历史问题1（工具流式输出被拆到新草稿）已由安全点判定内的
        # _has_pending_tool_group 守卫兜住：本轮若已建工具条目而未收束，
        # 这里不会滚动，工具批次结束后的 tool.end 安全点仍会照常触发。
        # 终局轮修复：此处 tool_calls_list / textual_tool_call 均已定型。
        # 解耦改造：中途安全点（还会继续请求模型时）只发射事件，满容量
        # 时由 DraftManager 在后台滚动，Agent 立即继续（§8）；终局轮
        # （无后续请求）必须走 finalize_turn 同步收束——把"只永久化旧段、
        # 不创建新草稿"留给终局分支完成，避免容量预警被以
        # start_next_draft=True 抢先消费——创建一个永远无人写入、只显示
        # "Thinking..." 的幽灵草稿，随后被 get_ai_response 收尾
        # mark_dead + 删除（表现为回复交付后闪现的 Thinking 气泡）。
        will_request_again = bool(tool_calls_list) or bool(textual_tool_call)
        if will_request_again:
            # 非阻塞安全点：满容量时后台滚动；工具批次/纠错重试立即开始。
            builder.on_round_boundary()
            builder.request_flush()
        elif not await builder.finalize_turn():
            # 终局：等待旧段永久化（不开新草稿）；未滚动时保底刷一帧。
            builder.request_flush()

        # assistant 消息组装 + 双列表追加（与两条原生 bridge 循环共用骨架）。
        # 打断保全（改动点1）：改为"升级已存在的占位消息"而不是新增一条——
        # journal 里的实时占位原地补全 tool_calls / reasoning / 最终文本，
        # 同一对象追加进 loop_messages（正常路径不出现重复 assistant 消息）。
        live_slot.finalize(loop_messages, content_acc, tool_calls_list, reasoning_acc)

        # 文本伪工具调用最多纠正三次；达到次数后直接给出安全状态说明，而不是
        # 把 XML 原文返回给用户，也避免模型在不可恢复状态下无限循环。
        if not tool_calls_list:
            if textual_tool_call:
                plain_text_tool_attempts += 1
                logger.warning(
                    "[%s] 模型输出了文本格式工具调用，已清理并请求标准调用：第 %s/%s 次",
                    api_label, plain_text_tool_attempts, MAX_PLAIN_TEXT_TOOL_CALL_RETRIES,
                )
                if plain_text_tool_attempts < MAX_PLAIN_TEXT_TOOL_CALL_RETRIES:
                    loop_messages.append(Message.user_text(
                        "System: Your last response attempted a tool call as plain text. "
                        "Use the standard tool_calls API only. Do not emit  XML as user-visible text."
                    ))
                    # 这是一次完整但需要纠正的模型返回；还会重试下一请求。
                    # 解耦：非阻塞安全点，满容量时后台滚动，重试立即开始。
                    builder.on_round_boundary()
                    continue
                final_content = content_acc or (
                    "工具调用格式连续异常，未继续执行额外操作。请重新描述需求或换一个模型后重试。"
                )
                if not content_acc:
                    builder.add_text(final_content)
            else:
                final_content = content_acc
            finish_open_tool_group(builder)
            # 终局：同步收束旧段（只永久化、不创建新草稿）。
            await builder.finalize_turn()
            break
        status = await run_tool_batch(
            builder, tool_calls_list, loop_messages, new_history_entries,
            tool_call_count_ref, api_label, tools, error_streak=error_streak,
        )
        # ★ 解耦关键点（§8）：工具结果已全部进入 conversation context，
        # 下一轮 LLM 请求立即发出；满容量时草稿滚动由 DraftManager 在
        # tool.end 安全点后台执行，Agent 不再等待草稿切换。（run_tool_batch
        # 已在内部触发 tool.end 安全点。）

        # ===== FIX: 只对 over_limit 做强制总结并退出 =====
        if status == "over_limit":

            def _build_synth_request(extra_msg: Message) -> dict[str, Any]:
                # 与上方 create_params 同理：** 解包需 Any 值类型。
                synth_params: dict[str, Any] = {
                    "model": current_model,
                    "messages": render_openai_messages(loop_messages + [extra_msg]),
                    "stream": True,
                    "max_tokens": max_tokens,
                }
                synth_params.update(sampling_params)
                synth_params.update(reasoning_top)
                synth_extra_body = _merged_extra_body(
                    api_label, reasoning_extra,
                    chat_id=builder.chat_id,
                    supports_prompt_cache=prompt_cache_enabled,
                    session_key=loop_session_key,
                    model_info=model_info,
                )
                if synth_extra_body is not None:
                    synth_params["extra_body"] = synth_extra_body
                if affinity_headers:
                    synth_params["extra_headers"] = affinity_headers
                return synth_params

            async def _stream_synth(desc: dict[str, Any]) -> str:
                # 合成总结同样走流式输出；typing 状态与文本块开闭由骨架统一管理。
                synth_text = ""
                synth_stream = await client.chat.completions.create(**desc)
                async for chunk in synth_stream:
                    if chunk.choices:
                        c_delta = getattr(chunk.choices[0].delta, "content", None) or ""
                        if c_delta:
                            synth_text += c_delta
                            builder.append_stream_delta(c_delta)
                return synth_text

            # over-limit 强制总结骨架（与两条原生 bridge 循环共用）：
            # 合成指令注入 -> 流式总结 -> 空内容兜底 -> 写历史 -> 收束。
            final_content = await over_limit_final_summary(
                builder, new_history_entries,
                api_label=api_label, loop_name="_agentic_loop_openai_compat",
                build_synth_request=_build_synth_request,
                stream_synth=_stream_synth,
                postprocess=_strip_textual_tool_calls,
            )
            break
        # 如果 status == "continue"（包括之前熔断返回的），循环自然继续

    # 轮次数耗尽 / 空终局兜底（与两条原生 bridge 循环共用 bridge_common 骨架）。
    final_content = await ensure_final_content(builder, new_history_entries, final_content)
    return final_content, final_usage, new_history_entries


# =====================================================================
# 原生图像循环的 prompt / 参考图提取（Internal Message 原生）
# =====================================================================
def _extract_native_image_urls_from_user_message(msg: Message) -> list[str]:
    """从单条 user 消息（内部 Message）中提取全部参考图 URL。

    重要边界（2026-09 排查 agnes-image-2.5-flash "看不到历史图片"实锤）：
    这里读的是 msg.blocks 里已经解析好的 ImageBlock——它是否存在，完全
    取决于上游 _append_history_async -> _resolve_multimodal_content 有没有
    按当前模型的 model_info.image_input 重新解析出图片块（见 attachment_content.py）。
    若历史消息携带的 meta 信封形状异常（如 R2 下载失败、file_id 已过期、
    信封字段缺失等），_resolve_multimodal_content 会静默降级为纯文本占位，
    此处自然读不到任何 ImageBlock——这不是本函数的 bug，但排查时必须
    先确认"图片有没有进 messages"，而不是想当然地怀疑这里的提取逻辑。
    """
    urls: list[str] = []
    for block in msg.blocks:
        if isinstance(block, ImageBlock) and block.url:
            urls.append(block.url)
    return urls


def _extract_native_video_urls_from_user_message(msg: Message) -> list[str]:
    """从单条 user 消息（内部 Message）中提取全部参考视频 URL。

    视频输入模态（VideoBlock.url）在视频生成模型上对应 Agnes Video 2.5
    的 videos[].url 参考语义（动作/节奏参考）；URL 均为 R2 公开地址，
    满足文档"参考媒体必须公开可访问"的要求。
    """
    urls: list[str] = []
    for block in msg.blocks:
        if isinstance(block, VideoBlock) and block.url:
            urls.append(block.url)
    return urls


def _extract_image_prompt_and_reference_urls(msgs: list) -> tuple[str, list[str]]:
    """从（内部 Message 列表）请求消息中提取图像模型的 prompt 与参考图 URL 列表。

    prompt 一律取最后一条 user 消息（本轮最新指令）；参考图优先取同一
    条消息里的 ImageBlock。

    参考图回溯：最后一条 user 消息不带图时，向前找最近一条带图的 user
    消息沿用其参考图。

    背景（gpt-image-2 等 Images 协议模型只见"最后一条 user 消息"）：
    图像轮以"模型仅返回文本"的方式失败时（网关 200 + 纯文本回复、非
    嚗 前缀的提示文案），该提示会作为普通 assistant 消息写入历史；用户
    随后发的重试消息（往往是纯文本"再试一次"）追加在其后成为最后一
    条 user 消息——旧实现此时提取不到任何参考图，请求退化为不带参考
    图的文生图（"失败后只发了文本，没把图片发给 AI"）。回溯最近一条
    带图 user 消息即可让重试继续拿到参考图（图生图/编辑语义保持）。

    重要边界（务必与"视觉理解历史图片"区分开，这是本函数唯一职责）：
    这里只解决"编辑/变体任务该用哪张参考图"，不解决、也不可能解决
    "模型能不能针对历史图片内容做文本问答"——image_output=True 的模型
    （如 agnes-image-2.5-flash）走的是 openai_images 协议（/images/
    generations、/images/edits），响应体系里没有"针对输入图片的文字
    理解"这一产物（ImageTaskResult 只有 images/text(拒绝说明)/refusal），
    所以即使这里成功拿到了历史图片 URL，模型也不会、也不能用文字回答
    "这张图里画的是什么"之类的问题。该模型配置里的 image_input=True 仅表示
    "支持把图片当输入模态接收"（即：可以被当参考图使用），并不等价于
    "支持视觉问答"——两者是完全不同的能力，排查"看不到历史图片"类问题
    时先确认用户诉求是"想用旧图继续编辑"（本函数负责）还是"想问模型
    图片里有什么"（该模型架构上不支持，应引导用户切换到真正的多模态
    对话模型，如任意 image_input=True 且走 chat 协议的模型）。
    """
    last_user_msg: Optional[Message] = None
    for item in reversed(msgs):
        m = item if isinstance(item, Message) else Message.from_openai_dict(item)
        if m.role == "user":
            last_user_msg = m
            break

    if not last_user_msg:
        return "", []

    prompt = "\n".join(
        t for t in (b.text.strip() for b in last_user_msg.blocks
                    if isinstance(b, TextBlock)) if t
    ).strip()

    image_urls = _extract_native_image_urls_from_user_message(last_user_msg)

    if not image_urls:
        skipped_user_turns = 0
        for item in reversed(msgs):
            m = item if isinstance(item, Message) else Message.from_openai_dict(item)
            if m is last_user_msg or m.role != "user":
                continue
            carried = _extract_native_image_urls_from_user_message(m)
            if carried:
                image_urls = carried
                logger.info(
                    "[NativeImage] 本轮 user 消息未带参考图，向前跳过 %s 条无图"
                    " user 消息后，沿用最近一条带图 user 消息的 %s 张参考图"
                    "（重试/追问场景保持图生图输入）",
                    skipped_user_turns, len(carried),
                )
                break
            skipped_user_turns += 1
        else:
            if skipped_user_turns:
                logger.info(
                    "[NativeImage] 本轮及此前 %s 条 user 消息均未解析出参考图"
                    "（可能原因：历史图片的 R2 链接已过期/下载失败，或该模型的"
                    " model_info.image_input 在解析历史时被判定为不支持——见"
                    " attachment_content._resolve_multimodal_content），"
                    "本次按纯文生图处理", skipped_user_turns,
                )

    return prompt, image_urls


# =====================================================================
# 原生图像循环（ImageTask 驱动：任务显式声明操作，协议适配器决定端点）
# =====================================================================
async def _agentic_loop_native_image(
        client: AsyncOpenAI,
        current_model: str,
        messages: list,
        builder: "DraftManager",
        chat_id: int,
        journal: Optional[list[dict[str, Any]]] = None,
        media_overrides: Optional[dict[str, Any]] = None,
) -> tuple[str | None, object | None, list]:
    """原生图像模型回合：prompt/参考图提取 -> ImageTask -> 协议分发 ->
    R2 上传 -> 富媒体消息。

    重构说明（ImageTask）：旧版把参考图列表直接塞给请求函数，由其按
    "有没有图"猜端点；现在先显式构造 ImageTask（operation=edit/generate），
    经 protocols.images.dispatch_image_task 按**模型协议**分发——
    openai_images（ModelScope/XXTF）与 openai_chat modalities
    （OpenRouter 图像模型）两条链路共用同一任务模型与后处理。

    media_overrides（可选，交互参数卡片的提交产物），键（全部可省略）：
      image_size（"1K"–"4K" 档位）/ aspect_ratio（官方比例集合）/
      num_images（1-4，仅请求层实际消费时生效）/ reference_images(list[str])
    """
    overrides = media_overrides if isinstance(media_overrides, dict) else {}
    model_info = SUPPORTED_MODELS.get(current_model)

    # 采样参数由图像协议适配器按 model_info 自取（get_sampling_params）；
    # 推理控制不适用于图像生成端点，不发送。
    prompt_text, image_urls = _extract_image_prompt_and_reference_urls(messages)

    # 卡片显式给出参考图时优先于消息内提取（卡片收集的 URL 均已通过
    # 公开可访问解析）。
    if "reference_images" in overrides:
        image_urls = [str(u) for u in (overrides.get("reference_images") or []) if str(u or "").strip()]

    clean_prompt = _clean_prompt_for_image_model(prompt_text)

    # ---- 显式构造图像任务：带参考图 = edit，否则 generate（任务构造层
    # 的唯一推断点；进入适配器后不再有任何"看图猜端点"逻辑）。----
    # 卡片参数（尺寸档位/宽高比/张数）作为 ImageTask 一等字段透传，由
    # 请求层按端点形状消费（inline 形状进 size/ratio，multipart 进 size）。
    task_kwargs: dict[str, Any] = {}
    if overrides.get("image_size"):
        task_kwargs["image_size"] = str(overrides["image_size"])
    if overrides.get("aspect_ratio"):
        task_kwargs["aspect_ratio"] = str(overrides["aspect_ratio"])
    if overrides.get("num_images"):
        try:
            task_kwargs["num_images"] = max(1, min(int(overrides["num_images"]), 4))
        except (TypeError, ValueError):
            pass
    if image_urls:
        task = ImageTask.edit(clean_prompt or prompt_text, image_urls, model=current_model, **task_kwargs)
    else:
        task = ImageTask.generate(clean_prompt or prompt_text, model=current_model, **task_kwargs)

    try:
        # ---- 协议分发：openai_images -> /images/{generations,edits}；
        # openai_chat -> chat.completions + modalities。----
        model_info = SUPPORTED_MODELS.get(current_model)
        if model_info is None:
            return f"IMAGE_ERROR:未知图像模型 {current_model}", None, []

        # 打断保全（改动点4）：发起生成请求前先往 journal 放进度占位——
        # 生成是原子性调用（无“半张图”中间态），等待返回的几十秒里被打断
        # 时，占位提供“模型上一轮确实在生成图片”的上下文；请求成功后原地
        # 更新为最终结果，失败路径整体移除（保持失败轮替换语义不变）。
        media_slot = MediaProgressSlot(
            journal,
            f"[图片生成中] 指令: {clean_prompt or prompt_text or '(无)'}"[:300],
        )

        result = await dispatch_image_task(task)
        used_endpoint = result.endpoint or "/images/generations"

        if result.images:
            image_bytes_list = result.images
        else:
            # 无图片字节：chat modalities 路径可能返回纯文本/拒绝说明；
            # images 路径视为"返回成功但未找到图片数据"。
            if result.text or result.refusal:
                final_notice = _format_native_image_notice(
                    content_text=result.text,
                    refusal_text=result.refusal,
                    finish_reason=result.finish_reason,
                )
                safe_notice_html = convert_markdown_to_telegram_html(final_notice).replace("\n", "<br/>")
                # pre_rendered=True：上一行已完成唯一一次转换，发送层不再重过
                await send_rich_html_message(chat_id, safe_notice_html, pre_rendered=True)
                final_content = "IMAGE_SENT"
                new_entries = media_slot.complete(final_notice or "（已生成图片）")
                return final_content, result.usage, new_entries
            # 语义准确化（2026-09 ModelScope 生产事故）：HTTP 200 + 空 images
            # 有两种截然不同的情形——
            #   a) 响应里真的没有图片数据；
            #   b) 响应里有图片链接，但下载校验失败（防盗链/链接过期/错误页）。
            # 情形 b 会带逐项诊断，报错必须反映真实原因，否则用户会误以为
            # 接口什么都没返回（旧文案"未找到可用图片数据"就是栽在这里）。
            # detail 传纯文本多行（\n 分行）：_format_api_error_notice 内部
            # 会剥 HTML 并按行重排。
            if result.diagnostics:
                diag_body = "\n".join(f"· {line}" for line in result.diagnostics[:4])
                if len(result.diagnostics) > 4:
                    diag_body += f"\n· …等共 {len(result.diagnostics)} 项"
                detail = (
                    f"接口返回了 {len(result.diagnostics)} 个图片数据项，但全部下载/校验失败：\n"
                    f"{diag_body}\n"
                    "常见原因：中转商返回的图片链接有防盗链或已过期（下载到的是错误页而非图片），请直接重试一次。"
                )
            else:
                detail = "接口返回成功，但未找到可用图片数据。"
            error_notice = _format_api_error_notice(
                api_name=f"{_get_images_api_display_name(model_info)} 图像接口",
                error_code=200,
                endpoint=used_endpoint,
                model=current_model,
                detail=detail,
            )
            media_slot.drop()
            return f"IMAGE_ERROR:{error_notice}", None, []

        uploaded_urls = await _upload_generated_images_to_r2(image_bytes_list)

        if uploaded_urls:
            # src 属性走 URL 属性转义：R2 presigned URL 含 & 参数，
            # 不转义会被 Telegram HTML 解析器当作实体名起点截断
            img_tags = "".join(f'<img src="{escape_media_url_attr(u)}"/>' for u in uploaded_urls)
            caption_text = _format_image_metadata_caption(image_bytes_list[0],
                                                          current_model) if image_bytes_list else "Generated image"
            # 单图用 <figure>，多图用 <tg-slideshow> 轮播
            if len(uploaded_urls) == 1:
                rich_html = f'<figure>{img_tags}<figcaption>{convert_markdown_to_telegram_html(caption_text)}</figcaption></figure>'
            else:
                rich_html = f'<tg-slideshow>{img_tags}<figcaption>{convert_markdown_to_telegram_html(caption_text)}</figcaption></tg-slideshow>'
            # pre_rendered=True：figcaption 已在上方转换，发送层不重过转换器
            await send_rich_html_message(chat_id, rich_html, pre_rendered=True)
            final_notice = caption_text or (result.text[:200] if result.text else "")
        else:
            if result.text or result.refusal:
                final_notice = _format_native_image_notice(
                    content_text=result.text,
                    refusal_text=result.refusal,
                    finish_reason=result.finish_reason,
                )
            else:
                error_notice = _format_api_error_notice(
                    api_name=f"{_get_images_api_display_name(model_info)} 图像接口",
                    error_code=200,
                    endpoint=used_endpoint,
                    model=current_model,
                    detail="接口返回成功，图片已生成，但转存图片存储失败（多为对象存储临时故障），请直接重试。",
                )
                media_slot.drop()
                return f"IMAGE_ERROR:{error_notice}", None, []

        final_content = f"IMAGE_SENT:{final_notice}" if final_notice else "IMAGE_SENT"
        if uploaded_urls:
            history_content = f"[图片已生成] 指令: {clean_prompt or '(无)'} | {final_notice}".strip(" |")
        else:
            history_content = final_notice or "（已生成图片）"
        # 打断保全（改动点4）：占位原地定稿为最终历史内容（journal 中恰一条，
        # 不与占位叠加；journal=None 时 complete 仍返回有效 new_entries）。
        new_entries = media_slot.complete(history_content)
        return final_content, result.usage, new_entries

    except Exception as e:
        logger.exception(f"Native image model request failed: {e}")
        # 打断保全（改动点4）：异常路径移除进度占位——失败轮保持“历史末尾
        # 仍是 user 消息”，mark_failed_unanswered_user / 下一条消息的替换
        # 语义不变。（CancelledError 不是 Exception，不走本分支：占位留在
        # journal 由打断方保全。）
        media_slot.drop()
        # 修复：旧写法 hasattr(e, "response") and hasattr(e.response, "text")
        # 在流式响应未读取时会抛 httpx.ResponseNotRead（hasattr 只吞
        # AttributeError），且旧代码 `await e.response.text()` 对同步
        # str 属性 await 必抛 TypeError——两处都会让错误日志提取
        # 静默失效甚至二次崩溃。改用安全的 extract_error_body_text。
        _err_body = extract_error_body_text(e)
        if _err_body:
            logger.error(f"Response body: {_err_body[:1000]}")
        err_str = str(e)
        if _is_content_safety_error(err_str):
            logger.info("[NativeImage] 请求被内容安全策略拦截（异常路径）: %s", err_str[:200])
            error_notice = _format_image_safety_notice(detail=err_str, model=current_model)
        else:
            _ep_is_images = (
                SUPPORTED_MODELS.get(current_model)
                and get_effective_endpoint(SUPPORTED_MODELS[current_model]).protocol == "openai_images"
            )
            error_notice = await get_error_notification_message(
                chat_id,
                error_code=getattr(e, "status_code", getattr(e, "status", 500)),
                error_message=err_str,
                api_name="图像请求",
                exception=e,
                endpoint="/v1/images/generations" if _ep_is_images else "/v1/chat/completions",
                model=current_model,
            )
        return f"IMAGE_ERROR:{error_notice}", None, []


async def _agentic_loop_native_video(
        current_model: str,
        messages: list,
        builder: "DraftManager",
        chat_id: int,
        journal: Optional[list[dict[str, Any]]] = None,
        media_overrides: Optional[dict[str, Any]] = None,
) -> tuple[str | None, object | None, list]:
    """
    处理视频生成模型。
    目前支持 Agnes 和 OpenRouter。

    media_overrides（可选，交互参数卡片的提交产物）：显式携带卡片上选择
    的参数与收集的参考媒体，优先级高于消息内提取。键（全部可省略）：
      seconds / size / aspect_ratio / mode / seed / first_frame / last_frame /
      reference_images(list[str]) / reference_audios(list[str]) /
      video_specs(list[dict]{url,start_seconds?,require_audio?})
    """
    overrides = media_overrides if isinstance(media_overrides, dict) else {}

    # 提取 prompt（最后一条 user 消息的文本块）
    prompt = ""
    for item in reversed(messages):
        m = item if isinstance(item, Message) else Message.from_openai_dict(item)
        if m.role == "user":
            prompt = m.text().strip()
            break
    if not prompt:
        return "VIDEO_ERROR:未提供提示词", None, []

    # 可选：解析时长（文档硬约束：seconds 字符串 "4"–"12"，默认 "5"；
    # 超出范围一律钳到边界，否则网关 400）。卡片显式选择的 seconds 优先
    # 于 prompt 文本里的"N 秒"解析。
    duration = 5
    override_seconds = overrides.get("seconds")
    if override_seconds is not None:
        try:
            duration = int(str(override_seconds).strip())
        except (TypeError, ValueError):
            duration = 5
        duration = max(4, min(duration, 12))
    else:
        # 时长解析：兼容中英文（"5秒" 与 "5 seconds" / "5s"）
        # 中文"秒"与后续汉字都是 \w，末尾的 \b 对中文分支永不成立（导致
        # "生成5秒的视频" 匹配失败、时长恒为默认值）——中文分支不加边界。
        match = re.search(r'(\d+)\s*(?:秒|seconds?\b|secs?\b|s\b)', prompt, re.IGNORECASE)
        if match:
            try:
                duration = int(match.group(1))
                duration = max(4, min(duration, 12))
            except ValueError:
                duration = 5

    # 获取模型信息，确定 provider
    model_info = SUPPORTED_MODELS.get(current_model)
    if not model_info:
        return f"VIDEO_ERROR:未知模型 {current_model}", None, []

    provider = model_info.provider
    video_url = None
    error = None
    video_meta: Optional[dict] = None

    # 参考媒体提取（与图像循环同语义：最后一条 user 消息优先，缺失时
    # 回溯最近一条带媒体的消息）：图片 -> images[]，视频 -> videos[]。
    # Agnes 侧由请求体构建器自动判为 reference/keyframe 模式并追加
    # <Picture N>/<Video N> 占位符；纯文本则为 text 模式（请求层唯一推断点）。
    # 卡片 media_overrides 显式给出参考媒体时优先于消息内提取（卡片收集
    # 的 URL 已经过公开可访问解析，且视频可携带 start_seconds/require_audio）。
    _, image_ref_urls = _extract_image_prompt_and_reference_urls(messages)
    video_ref_urls: list[str] = []
    for item in reversed(messages):
        m = item if isinstance(item, Message) else Message.from_openai_dict(item)
        if m.role != "user":
            continue
        video_ref_urls = _extract_native_video_urls_from_user_message(m)
        if video_ref_urls:
            break

    if "reference_images" in overrides:
        image_ref_urls = [str(u) for u in (overrides.get("reference_images") or []) if str(u or "").strip()]
    if "reference_videos" in overrides:
        video_ref_urls = [str(u) for u in (overrides.get("reference_videos") or []) if str(u or "").strip()]

    # chat action 语义（与 chat_actions.py 的白名单约定一致）：
    # - 生成阶段（调用生视频模型的轮询/生成）-> record_video（bot 正在
    #   "录制"视频），每 4 秒循环重发，覆盖动辄数十秒到数分钟的生成过程；
    # - 发送阶段（视频下载 / R2 上传 / sendRichMessage 携带 <video>）
    #   -> upload_video（bot 正在发送视频）。
    # 打断保全（改动点4）：发起生成请求前先往 journal 放进度占位——
    # 视频生成动辄数十秒到数分钟，等待期间被打断时，占位提供“模型上一轮
    # 确实在生成视频”的上下文；成功后原地定稿为最终历史内容，失败路径
    # 整体移除（保持失败轮替换语义不变），取消路径留在 journal 由打断方
    # 保全（CancelledError 不走 except Exception）。
    media_slot = MediaProgressSlot(
        journal,
        f"[视频生成中] 提示词: {prompt[:200]}" if prompt else "[视频生成中]",
    )

    if provider == "agnes":
        async with chat_action_scope(chat_id, "record_video"):
            video_url, error, video_meta = await _request_agnes_video(
                prompt, duration, current_model,
                reference_images=tuple(image_ref_urls),
                reference_videos=tuple(video_ref_urls),
                size=overrides.get("size"),
                aspect_ratio=overrides.get("aspect_ratio"),
                mode=overrides.get("mode"),
                first_frame=overrides.get("first_frame"),
                last_frame=overrides.get("last_frame"),
                seed=overrides.get("seed"),
                reference_audios=tuple(overrides.get("reference_audios") or ()),
                video_specs=tuple(overrides.get("video_specs") or ()),
            )
    elif provider == "openrouter":
        async with chat_action_scope(chat_id, "record_video"):
            video_url, error, video_meta = await _request_openrouter_video(prompt, duration, current_model)
    else:
        media_slot.drop()
        return f"VIDEO_ERROR:不支持的视频提供商 {provider}", None, []

    if error:
        media_slot.drop()
        return f"VIDEO_ERROR:{error}", None, []

    if not video_url:
        media_slot.drop()
        return "VIDEO_ERROR:未获取到视频链接", None, []

    # ---------- 发送视频富文本消息（与图片生成路径保持一致） ----------
    # 与图片路径一样：先把视频字节下载下来，上传到 R2 并带正确的 Content-Type: video/mp4，
    # 再用 R2 URL 拼 <figure><video src=...></video><figcaption>...</figcaption></figure>
    # 通过 sendRichMessage 发送。这样可保证 Telegram 能拿到合法的 video MIME，
    # 不会触发 400 RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND。
    final_video_url = video_url
    video_bytes_len = 0
    r2_url = None
    await start_chat_action(chat_id, "upload_video")
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        # Agnes 返回的视频 URL 可能经过 CDN redirect，必须跟随跳转；
        # 否则可能把 redirect/error 页面当成 mp4 上传到 R2。
        async with aiohttp.ClientSession(timeout=timeout) as dl_session:
            async with dl_session.get(video_url, allow_redirects=True) as dl_resp:
                content_type = (dl_resp.headers.get("Content-Type") or "").lower()
                logger.info(
                    "[NativeVideo] download response: status=%s type=%s length=%s final_url=%s",
                    dl_resp.status,
                    content_type,
                    dl_resp.headers.get("Content-Length"),
                    str(dl_resp.url)[:200],
                )
                if dl_resp.status == 200:
                    # 修复 OOM 风险：限制为 200MB（足够任何合理的 720p 视频片段），
                    # 超限则拒绝并回退到原始 URL。防护必须在"读取"阶段生效：
                    # 1) Content-Length 预拒绝（服务器声明超限直接放弃）；
                    # 2) 分块流式累积，超限即中止（服务器不声明长度/谎报时，
                    #    旧实现 resp.read() 仍会把整个 body 读进内存才检查，
                    #    防护形同虚设）。
                    _MAX_VIDEO_BYTES = 200 * 1024 * 1024
                    declared_len_raw = dl_resp.headers.get("Content-Length")
                    declared_len: Optional[int] = None
                    if declared_len_raw is not None:
                        try:
                            declared_len = int(declared_len_raw)
                        except ValueError:
                            declared_len = None
                    if declared_len is not None and declared_len > _MAX_VIDEO_BYTES:
                        logger.warning(
                            "[NativeVideo] Content-Length 超限 (%s > %s)，跳过下载与 R2 上传，回退原始 URL: %s",
                            declared_len, _MAX_VIDEO_BYTES, str(video_url)[:200],
                        )
                        video_bytes = b""
                        video_bytes_len = 0
                    else:
                        chunks: list[bytes] = []
                        total = 0
                        overflow = False
                        async for chunk in dl_resp.content.iter_chunked(1024 * 1024):
                            total += len(chunk)
                            if total > _MAX_VIDEO_BYTES:
                                overflow = True
                                break
                            chunks.append(chunk)
                        if overflow:
                            logger.warning(
                                "[NativeVideo] 视频体积超限 (>%s)，跳过 R2 上传，回退原始 URL: %s",
                                _MAX_VIDEO_BYTES, str(video_url)[:200],
                            )
                            video_bytes = b""
                            video_bytes_len = 0
                        else:
                            video_bytes = b"".join(chunks)
                            video_bytes_len = len(video_bytes)

                        # 防止将 HTML/错误页/redirect body 伪装成 mp4 上传。
                        # 正常 720p 视频不应只有几 KB，且 MP4 必须包含 ftyp box。
                        # 仅在真正读到字节时校验（超限拒绝路径 video_bytes 为空）。
                        if video_bytes_len:
                            is_mp4 = b"ftyp" in video_bytes[:256]
                            if video_bytes_len < 100_000 or not is_mp4:
                                logger.error(
                                    "[NativeVideo] invalid video payload, skip R2 upload: bytes=%s content_type=%s has_ftyp=%s url=%s",
                                    video_bytes_len,
                                    content_type,
                                    is_mp4,
                                    str(video_url)[:200],
                                )
                                video_bytes = b""
                                video_bytes_len = 0
                            else:
                                logger.info(
                                    "[NativeVideo] video validated: bytes=%d content_type=%s",
                                    video_bytes_len,
                                    content_type,
                                )
                                r2_key = f"generated/{uuid.uuid4().hex}.mp4"
                                r2_url = await upload_bytes_to_r2(video_bytes, r2_key, "video/mp4")
                        if r2_url:
                            final_video_url = r2_url
                        else:
                            logger.warning("[NativeVideo] R2 上传失败，回退使用原始视频 URL")
                else:
                    logger.warning(
                        "[NativeVideo] 视频下载非 200: status=%s url=%s，回退使用原始 URL",
                        dl_resp.status, str(video_url)[:200],
                    )
    except Exception as e:
        logger.exception(
            "[NativeVideo] 视频下载/上传异常，回退使用原始 URL: url=%s err=%s",
            str(video_url)[:200], e,
        )
    finally:
        # 下载 / R2 上传阶段结束，熄灭 upload_video（发送动作由
        # send_rich_html_message 内部的 upload_video 钩子接管）。
        await stop_chat_action(chat_id, "upload_video")

    # 构造富文本：用 <figure>+<video>+<figcaption> 的文档推荐写法（视频只能作为独立 media block）
    # caption 走与图片一致的"元数据"风格（分辨率/帧率/帧数/大小/模型），不再附提示词。
    if video_bytes_len == 0 and video_meta:
        # 下载失败时退而用 Agnes 报告的 perf_output_size 作为大小估算
        out_size = video_meta.get("perf_output_size") if isinstance(video_meta, dict) else None
        video_bytes_len = int(out_size) if isinstance(out_size, (int, float)) else 0
    caption_text = _format_video_metadata_caption(
        file_size_bytes=video_bytes_len,
        model=current_model,
        meta=video_meta if isinstance(video_meta, dict) else None,
    )
    video_html = (
        f'<figure><video src="{escape_media_url_attr(final_video_url)}"></video>'
        f'<figcaption>{convert_markdown_to_telegram_html(caption_text)}</figcaption></figure>'
    )
    # pre_rendered=True：figcaption 已在上方转换，发送层不重过转换器
    send_ok = await send_rich_html_message(chat_id, video_html, pre_rendered=True)
    if not send_ok:
        logger.error(
            "视频已生成，但 sendRichMessage 发送失败 final_video_url=%s",
            str(final_video_url)[:200],
        )
        media_slot.drop()
        return "VIDEO_ERROR:视频发送失败", None, []

    # 生成历史记录（打断保全改动点4：占位原地定稿，journal 中恰一条）
    history_content = f"[视频已生成] 提示词: {prompt[:200]}" if prompt else "[视频已生成]"
    new_entries = media_slot.complete(history_content)

    final_content = f"VIDEO_SENT:{prompt[:100]}"  # 用于上游判断
    return final_content, None, new_entries
