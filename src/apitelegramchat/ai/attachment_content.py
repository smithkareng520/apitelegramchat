"""多模态附件处理：图片/音频/文档的缓存获取与降级文本构造。

从 ai_handlers.py 拆分而来，逻辑未做改动。
"""
import asyncio
import base64
import mimetypes
import io
from pathlib import Path
from PIL import Image
from typing import Optional
import aiohttp

from apitelegramchat.config import ModelConfig, TELEGRAM_BOT_TOKEN, PROVIDERS
from apitelegramchat.utils import get_logger, transcribe_audio_with_groq
from apitelegramchat.file_handlers import get_file_path
from apitelegramchat.s3_utils import (
    upload_bytes_to_r2,
    file_exists_in_r2,
    download_from_r2,
    public_url_for_existing_key,
    generate_presigned_url,
    is_r2_configured,
)
import apitelegramchat.state as state

logger = get_logger(__name__)

from cachetools import TTLCache
from apitelegramchat.config import CACHE_TTL

_image_cache = TTLCache(maxsize=1000, ttl=CACHE_TTL)
_audio_cache = TTLCache(maxsize=500, ttl=CACHE_TTL)
_document_cache = TTLCache(maxsize=300, ttl=CACHE_TTL)
# 视频体积大（Telegram bot 下载上限 20MB），缓存条数比图片少，
# 避免内存被少数大文件占满。
_video_cache = TTLCache(maxsize=50, ttl=CACHE_TTL)

# ---------- 后台任务引用集合（防止 asyncio.create_task 创建的任务被 GC 提前回收）----------
_background_tasks: set = set()

_ATTACHMENT_KIND_LABELS = {
    "photo": "图片",
    "image": "图片",
    "document": "文档",
    "audio": "音频",
    "voice": "语音",
    "video": "视频",
}


def _get_r2_key(file_id: str) -> str:
    return f"telegram/{file_id}"


async def get_cached_image_data(chat_id: int | None, file_id: str) -> Optional[bytes]:
    """取图片字节，不触发任何 R2 上传。

    解析顺序：
      1. In-memory TTLCache (~5 min) —— 同轮内重复访问的热路径。
      2. Permanent-failure marker (``state.is_r2_attempted``) —— 若已标记，
         直接返回 None，避免对已知无法恢复的 file_id 反复重试。
      3. R2 / local cache —— 之前上传过的对象，下载并重新填充内存缓存。
         这是 TTLCache 过期后的恢复路径，让历史图片不依赖 Telegram API。
      4. Telegram getFile —— 首次拉取；拉到后只填内存缓存，**不**触发
         R2 上传。上传由调用方按需显式触发（见 ``_upload_and_mark`` 与
         ``_resolve_r2_public_url_for_vision``）。

    IMPORTANT: ``state.mark_r2_attempted`` 是**永久失败标记**，必须在
    ``get_cached_image_data`` 看到 hard failure（404/403/410 from Telegram，
    或 R2 报告对象存在但 body 不可读）时才设置。设置在临时错误
    (429 rate-limit / 5xx / 网络抖动 / 任何 Exception) 上会永久拉黑该
    file_id，即使临时条件消除后下一轮也无法恢复，导致历史图片"静默
    消失"。

    设计取舍：旧版本在此函数末尾调用 ``_track_task(_upload_and_mark(...))``
    做后台上传。这把"取字节"和"预防性 R2 上传"两个职责耦合在一起，导致
    Agnes 路径（``_resolve_r2_public_url_for_vision``）首次访问时
    同一张图被 ``put_object`` 两次（一次后台 + 一次同步）。重构后此函数
    职责单一，Agnes 路径自己负责唯一的同步上传，Gemini 路径在
    ``process_one`` 内显式触发后台上传。
    """
    cache_key = file_id
    if cache_key in _image_cache:
        return _image_cache[cache_key]

    if await state.is_r2_attempted(file_id):
        return None

    r2_key = _get_r2_key(file_id)
    if await file_exists_in_r2(r2_key):
        data = await download_from_r2(r2_key)
        if data:
            _image_cache[cache_key] = data
            return data
        # R2 报告对象存在但 body 不可读：永久失败，避免每轮重复 download
        await state.mark_r2_attempted(file_id)
        return None

    # R2 没该 key → 从 Telegram getFile 拉
    return await _fetch_from_telegram_and_cache(file_id)


async def _fetch_from_telegram_and_cache(file_id: str) -> Optional[bytes]:
    """从 Telegram getFile API 拉图片字节并缓存到内存。不触发 R2 上传。

    供两条路径共用：
      * ``get_cached_image_data`` —— 内存缓存 + R2 download miss 后的兜底
      * ``_resolve_r2_public_url_for_vision`` —— R2 已知 miss 时直接调本
        函数拉字节，避免重复 HEAD 检查

    临时失败（429/5xx/网络抖动）只 WARNING 日志，**不**永久标记，
    下一轮可重试；hard failure（404/403/410）才永久标记。
    """
    tg_path = await get_file_path(file_id)
    if not tg_path:
        # Telegram 报告 file_path 本身丢失 —— 永久失败
        await state.mark_r2_attempted(file_id)
        return None

    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    _image_cache[file_id] = data
                    return data
                if resp.status in (404, 403, 410):
                    logger.warning(
                        f"图片下载永久失败 {file_id}: HTTP {resp.status}"
                    )
                    await state.mark_r2_attempted(file_id)
                else:
                    # 临时失败（429/5xx/etc.）—— 不标记，下一轮可重试
                    logger.warning(
                        f"图片下载临时失败 {file_id}: HTTP {resp.status} (will retry next turn)"
                    )
    except asyncio.TimeoutError as e:
        logger.warning(f"图片下载超时 {file_id}: {e} (will retry next turn)")
    except aiohttp.ClientError as e:
        logger.warning(f"图片下载网络异常 {file_id}: {e} (will retry next turn)")
    except Exception as e:
        # 未分类异常 —— 完整 traceback 但不永久标记，避免误拉黑
        logger.exception(f"图片下载未分类异常 {file_id}: {e} (will retry next turn)")
    return None


async def _upload_and_mark(file_id: str, data: bytes, r2_key: str):
    """Upload image bytes to R2 / local cache in the background.

    IMPORTANT: `state.mark_r2_attempted` is a *permanent failure* marker used by
    `get_cached_image_data` to short-circuit retries once we've determined that
    a file is unrecoverable from both R2 and Telegram. It must therefore be set
    ONLY on upload failure. If we mark it after a SUCCESSFUL upload, then once
    the in-memory TTLCache entry expires (~5 min later), every subsequent
    request will:

        1. miss the cache
        2. see is_r2_attempted == True and return None immediately
        3. never re-fetch the bytes from R2 (where they now live)

    The result is that historical images silently vanish from the conversation
    history — only the most recently uploaded image (whose TTLCache entry is
    still warm) remains visible to the model. This is exactly the symptom
    "after a couple of turns, Gemini can only see the latest image".

    Setting the flag only on failure fixes this: after the TTLCache entry
    expires, `get_cached_image_data` will fall through to `file_exists_in_r2`,
    find the previously-uploaded object, and re-populate the cache.
    """
    try:
        result = await upload_bytes_to_r2(data, r2_key, "image/jpeg")
        if result is None:
            # upload_bytes_to_r2 returns None on failure (it already logged)
            await state.mark_r2_attempted(file_id)
    except Exception as e:
        logger.warning(f"R2后台上传失败 {file_id}: {e}")
        await state.mark_r2_attempted(file_id)


# =====================================================================
# 视频输入模态：缓存获取 / R2 持久化 / 公开 URL 解析
# 与图片路径（get_cached_image_data / _upload_and_mark /
# _resolve_r2_public_url_for_vision）完全对称，但有两个关键差异：
#
#   1. **只走 URL，不走 base64**。视频体积远大于图片（Telegram bot
#      下载上限 20MB），base64 后请求体会膨胀 ~33%，极易触发网关的
#      请求体上限；且 OpenRouter 官方文档也建议大文件优先用 URL。
#      因此 video_url 解析失败（R2 不可用）时直接降级为文本占位，
#      不做 base64 内联。
#
#   2. **持久化时机更早**。Telegram getFile 直链约 1 小时过期；为了让
#      "先发给不支持视频的模型，再切换到支持视频的模型"这条路径不丢
#      信息，视频在首次进入 fallback（模型不支持视频）路径时也会
#      fire-and-forget 地后台上传 R2（图片的 fallback 路径不做上传，
#      因为图片场景下 supports_vision 的模型占比高，且图片字节便宜）。
# =====================================================================


def _normalize_video_mime_type(mime_type: str = "") -> str:
    """归一化视频 mime type，只保留 OpenRouter 官方支持的四种容器。

    OpenRouter video_url 支持的格式：video/mp4、video/mpeg、video/mov、
    video/webm。未知/缺失的 mime 一律归为 video/mp4（Telegram 发送的
    视频绝大多是 H.264/mp4，video_note 也是 mp4）。
    """
    mime = (mime_type or "").strip().lower()
    if mime in ("video/mp4", "video/mpeg", "video/quicktime", "video/mov", "video/webm"):
        # OpenRouter 用 video/mov 表示 mov 容器（标准 MIME 是 video/quicktime）
        if mime == "video/quicktime":
            return "video/mov"
        return mime
    return "video/mp4"


async def get_cached_video_data(chat_id: int | None, file_id: str) -> Optional[bytes]:
    """取视频字节，不触发任何 R2 上传（与 get_cached_image_data 对称）。

    解析顺序：内存 TTLCache → 永久失败标记 → R2 下载 → Telegram getFile。
    上传由调用方按需触发（``_resolve_r2_public_url_for_video`` 同步上传，
    ``_ensure_video_persisted`` 后台上传）。
    """
    cache_key = file_id
    if cache_key in _video_cache:
        return _video_cache[cache_key]

    if await state.is_r2_attempted(file_id):
        return None

    r2_key = _get_r2_key(file_id)
    if await file_exists_in_r2(r2_key):
        data = await download_from_r2(r2_key)
        if data:
            _video_cache[cache_key] = data
            return data
        await state.mark_r2_attempted(file_id)
        return None

    return await _fetch_video_from_telegram_and_cache(file_id)


async def _fetch_video_from_telegram_and_cache(file_id: str) -> Optional[bytes]:
    """从 Telegram getFile API 拉视频字节并缓存到内存。不触发 R2 上传。

    与图片版的差异：视频体积大，超时给到 120s（图片用默认值）；
    hard failure（404/403/410）才永久标记，临时失败可重试。
    """
    tg_path = await get_file_path(file_id)
    if not tg_path:
        await state.mark_r2_attempted(file_id)
        return None

    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_path}"
    try:
        timeout = aiohttp.ClientTimeout(total=120, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    _video_cache[file_id] = data
                    return data
                if resp.status in (404, 403, 410):
                    logger.warning(f"视频下载永久失败 {file_id}: HTTP {resp.status}")
                    await state.mark_r2_attempted(file_id)
                else:
                    logger.warning(
                        f"视频下载临时失败 {file_id}: HTTP {resp.status} (will retry next turn)"
                    )
    except asyncio.TimeoutError as e:
        logger.warning(f"视频下载超时 {file_id}: {e} (will retry next turn)")
    except aiohttp.ClientError as e:
        logger.warning(f"视频下载网络异常 {file_id}: {e} (will retry next turn)")
    except Exception as e:
        logger.exception(f"视频下载未分类异常 {file_id}: {e} (will retry next turn)")
    return None


async def _upload_video_and_mark(file_id: str, data: bytes, r2_key: str, mime_type: str = "video/mp4"):
    """后台上传视频字节到 R2（与 _upload_and_mark 对称，但用真实 mime）。

    同样遵守"只在失败时 mark_r2_attempted"的约束，防止上传成功后
    TTLCache 过期导致历史视频被永久拉黑静默消失。
    """
    try:
        result = await upload_bytes_to_r2(data, r2_key, _normalize_video_mime_type(mime_type))
        if result is None:
            await state.mark_r2_attempted(file_id)
    except Exception as e:
        logger.warning(f"视频 R2 后台上传失败 {file_id}: {e}")
        await state.mark_r2_attempted(file_id)


async def _resolve_r2_public_url_for_video(file_id: str, mime_type: str = "video/mp4") -> str:
    """为视频输入模态解析公开可访问的 HTTP URL（对称图片版 _resolve_r2_public_url_for_vision）。

    video_url content part（OpenRouter / vLLM / LiteLLM 等的事实标准）只
    接受可公开抓取的 URL 或 data: URL；这里统一走 URL：

      1. R2 未配置 → 空串，调用方降级为文本占位（视频不走 base64，
         避免请求体膨胀触发网关上限）。
      2. R2 已有对象 → 直接返回公开 URL（自定义域 / r2.dev）或预签名
         URL（1h 有效，每轮重解析时重新签发，与图片 Agnes 路径一致）。
      3. R2 未有对象 → 同步从 Telegram 拉字节 → 同步上传 R2 → 返回 URL。

    返回值绝不包含 bot token：Telegram 直链会泄露 token 给第三方 API。
    """
    fid = str(file_id or "").strip()
    if not fid:
        return ""

    if not is_r2_configured():
        return ""

    r2_key = _get_r2_key(fid)

    if await file_exists_in_r2(r2_key):
        url = await public_url_for_existing_key(r2_key)
        if url:
            return url
        return ""

    # 冷路径：同步拉取 + 同步上传（首次访问支持视频的模型时触发一次，
    # 之后切换模型直接命中路径 2）。
    video_bytes = await _fetch_video_from_telegram_and_cache(fid)
    if not video_bytes:
        return ""

    result = await upload_bytes_to_r2(video_bytes, r2_key, _normalize_video_mime_type(mime_type))
    if result is None or result.startswith("file://"):
        return ""
    return result


async def _ensure_video_persisted(file_id: str, mime_type: str = "video/mp4"):
    """后台把视频持久化到 R2（fire-and-forget，不阻塞响应）。

    使用场景：当前模型不支持视频输入，走了文本降级路径。此时仍要把
    字节存到 R2，因为 Telegram getFile 直链约 1 小时过期——若不持久化，
    之后切换到支持视频的模型（如 stealth/ox-alpha / Gemini）时，历史里
    这条视频消息将既拿不到 URL 也拉不到字节，信息就此丢失。
    """
    fid = str(file_id or "").strip()
    if not fid or not is_r2_configured():
        return

    r2_key = _get_r2_key(fid)
    if await file_exists_in_r2(r2_key):
        return

    video_bytes = await get_cached_video_data(None, fid)
    if not video_bytes:
        logger.warning(f"[VideoPersist] 无法获取视频字节，放弃后台上传: {fid[:12]}")
        return

    await _upload_video_and_mark(fid, video_bytes, r2_key, mime_type)


async def _get_cached_audio_data(chat_id: int | None, file_id: str) -> Optional[bytes]:
    """仅在内存中缓存音频字节，不做磁盘或 R2 持久化。"""
    cache_key = file_id
    if cache_key in _audio_cache:
        return _audio_cache[cache_key]

    tg_path = await get_file_path(file_id)
    if not tg_path:
        return None
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    _audio_cache[cache_key] = data
                    return data
    except Exception as e:
        logger.exception(f"音频下载失败 {file_id}: {e}")
    return None


async def _get_cached_document_data(chat_id: int | None, file_id: str) -> Optional[bytes]:
    cache_key = file_id
    if cache_key in _document_cache:
        return _document_cache[cache_key]

    tg_path = await get_file_path(file_id)
    if not tg_path:
        return None

    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    _document_cache[cache_key] = data
                    return data
                logger.warning(f"文档下载失败 {file_id}: {resp.status}")
    except Exception as e:
        logger.exception(f"文档下载失败 {file_id}: {e}")
    return None


def _guess_document_mime_type(file_name: str = "", explicit_mime: str = "") -> str:
    mime = (explicit_mime or "").strip()
    if mime:
        return mime
    guessed, _ = mimetypes.guess_type(file_name or "")
    return guessed or "application/pdf"


async def _build_native_document_part(
        chat_id: int | None,
        file_id: str,
        file_name: str = "",
        mime_type: str = "",
):
    data = await _get_cached_document_data(chat_id, file_id)
    if not data:
        return None

    safe_name = file_name or f"document_{file_id[:8]}.pdf"
    safe_mime = _guess_document_mime_type(safe_name, mime_type)
    b64_data = base64.b64encode(data).decode()
    return {
        "type": "file",
        "file": {
            "filename": safe_name,
            "file_data": f"data:{safe_mime};base64,{b64_data}",
        },
    }


def _attachment_label(kind: str) -> str:
    return _ATTACHMENT_KIND_LABELS.get(str(kind or "").lower(), str(kind or "附件"))


async def _resolve_public_attachment_url(file_id: str) -> str:
    """把 Telegram file_id 解析成一个可供模型/工具继续引用的公开 URL。

    安全约束：此函数的返回值会被嵌入到发送给 LLM 的 fallback 文本里
    （见 ``_build_attachment_fallback_text``），因此**绝对不能**返回
    Telegram 直链 —— 那会暴露 ``bot{TELEGRAM_BOT_TOKEN}/`` 给第三方模型
    API。优先返回 R2 公开 URL；若 R2 未配置则返回空串，由调用方降级为
    file_id 文本。
    """
    fid = str(file_id or "").strip()
    if not fid:
        return ""

    # 不再返回 Telegram 直链（避免把 bot token 暴露给第三方模型 API）。
    # 仅 R2 公开 URL 是安全的：它要么是自定义域，要么是 r2.dev。
    try:
        r2_key = _get_r2_key(fid)
        if await file_exists_in_r2(r2_key):
            # fallback 与多模态注入统一使用上传后的临时访问 URL。
            # 不依赖永久公开域名，避免切换模型时丢失可访问地址。
            return await generate_presigned_url(r2_key)
    except Exception as e:
        logger.debug(f"解析 R2 文件 URL 失败 {fid[:12]}: {e}")

    return ""


async def _resolve_r2_public_url_for_vision(file_id: str) -> str:
    """为 vision API 解析公开可访问的 HTTP URL（Agnes 等只接受公开 URL 的网关）。

    三条路径，按开销从低到高：

      1. **R2 未配置 → 立即返回空串**。让调用方降级 base64，避免"拉字节
         → 上传本地 file:// → 检测不可公开访问 → 降级 base64"的无谓链路
         （浪费一次本地磁盘 IO 和一次 download_from_r2 调用）。
      2. **R2 已有该对象 → 直接拿公开 URL**。无上传开销，无 Telegram API
         调用。这是切换模型场景下的热路径（Gemini 那轮已上传过）。
      3. **R2 有配置但对象不存在 → 同步从 Telegram 拉字节 → 同步上传到
         R2 → 返回 URL**。这是首次访问 Agnes 的冷路径。

    关键设计：本函数负责**唯一的** R2 上传调用，不通过
    ``get_cached_image_data`` 触发后台上传。旧版本调 ``get_cached_image_data``
    导致同一张图被 ``put_object`` 两次（后台 fire-and-forget + 本函数同步），
    浪费一次 R2 PUT 和一次同图上行带宽。

    返回值绝不包含 bot token：Telegram 直链会泄露 token 给第三方 API。
    """
    fid = str(file_id or "").strip()
    if not fid:
        return ""

    # 路径 1：R2 完全没配置 → 立即降级，避免无谓的本地文件写入
    if not is_r2_configured():
        return ""

    r2_key = _get_r2_key(fid)

    # 路径 2：R2 已有 → 直接拿公开 URL（custom domain / r2.dev / presigned）
    if await file_exists_in_r2(r2_key):
        url = await public_url_for_existing_key(r2_key)
        if url:
            return url
        # R2 已有对象但拿不到公开 URL（罕见：custom domain 未配 + presign 失败）
        # → 让调用方降级 base64
        return ""

    # 路径 3：R2 未有 → 同步从 Telegram 拉字节 + 同步上传
    # 不调 get_cached_image_data：它会再做一次 file_exists_in_r2（外层刚做过）
    # 是纯浪费 HEAD 请求。
    img_bytes = await _fetch_from_telegram_and_cache(fid)
    if not img_bytes:
        return ""

    result = await upload_bytes_to_r2(img_bytes, r2_key, "image/jpeg")
    if result is None or result.startswith("file://"):
        # upload_bytes_to_r2 返回 file:// 说明 is_r2_configured() 其实是 False，
        # 但路径 1 已早退，这里理论上不该走到；保险起见仍降级。
        return ""
    return result


async def _build_attachment_fallback_text(
    *,
    kind: str,
    file_ids: list[str],
    user_text: str,
    chat_id: int | None,
    file_names: list[str] | None = None,
    mime_types: list[str] | None = None,
) -> str:
    """为不支持原生多模态的模型构造保留可用信息的文本占位。"""
    safe_kind = _attachment_label(kind)
    lines: list[str] = []
    total = len(file_ids)
    if total <= 0:
        return user_text

    if total == 1:
        header = f"📎 用户上传了{safe_kind}「{(file_names or [''])[0] or file_ids[0][:8]}」"
    else:
        header = f"📎 用户上传了{safe_kind}组（共 {total} 个）"
    lines.append(header)

    any_url = False
    for idx, fid in enumerate(file_ids, start=1):
        fname = ""
        if file_names and idx - 1 < len(file_names):
            fname = str(file_names[idx - 1] or "").strip()
        mime = ""
        if mime_types and idx - 1 < len(mime_types):
            mime = str(mime_types[idx - 1] or "").strip()
        url = await _resolve_public_attachment_url(fid) if fid else ""
        any_url = any_url or bool(url)
        parts = [f"{safe_kind}{idx if total > 1 else ''}"]
        if fname:
            parts.append(f"文件名：{fname}")
        parts.append(f"file_id：{fid}")
        if mime:
            parts.append(f"mime_type：{mime}")
        if url:
            parts.append(f"链接：{url}")
            if safe_kind == "图片":
                parts.append("可直接把该链接传给 edit_image_with_reference.image_url")
        lines.append(" | ".join(parts))

    if user_text:
        lines.append("")
        lines.append(f"用户原始指令：{user_text}")
    else:
        lines.append("")
        lines.append("用户未附加文字，请根据附件本身和用户上下文推断任务。")

    if chat_id is not None:
        lines.append("")
        lines.append(
            "说明：原始附件已保留；若当前模型不支持直接读取该类型内容，"
            "请基于上述链接调用工具或进行文本降级处理。"
        )
        # 显式提醒模型：file_name / file_id 都不是 URL，禁止拼到 <img src> 里。
        # 这条提示针对「链接」字段为空（R2 未配置）时的降级路径，避免 LLM
        # 自行编造伪 URL 写入 <img src>，导致 Telegram 返回
        # RICH_MESSAGE_PHOTO_URL_INVALID 整条消息发送失败。
        # 判定基于全部文件而不是单个文件：多文件部分成功部分失败时
        # 不会误加/漏加该警告。
        if not any_url:
            lines.append(
                "⚠️ 上面的「文件名」和「file_id」仅是元数据，不是合法 URL，"
                "禁止把它们写入 <img src>/<video src>/<a href>；"
                "若需展示图片，请直接用文字描述。"
            )

    return "\n".join(lines)


async def _build_audio_fallback_text(
    *,
    chat_id: int | None,
    file_id: str,
    file_name: str,
    user_text: str,
) -> str:
    """为不支持音频的模型生成纯文本降级内容。"""
    safe_name = str(file_name or f"audio_{file_id[:8]}.ogg").strip() or f"audio_{file_id[:8]}.ogg"

    audio_bytes = await _get_cached_audio_data(chat_id, file_id) if chat_id is not None else None
    transcript = ""
    if audio_bytes:
        try:
            ext = Path(safe_name).suffix or ".ogg"
            transcript = await transcribe_audio_with_groq(audio_bytes, ext) or ""
        except Exception as e:
            # 提升到 warning：转录失败对用户可见（用户会拿到空文本占位），
            # debug 级别在实际生产环境几乎不会被打开，会让问题被静默吞掉。
            logger.warning(f"[AudioFallback] 转录失败 {file_id[:12]}: {e}")
            transcript = "[转录失败，请稍后重试或检查 Groq 配置]"

    parts: list[str] = []
    if user_text:
        parts.append(user_text)
    if transcript:
        parts.append(transcript)
    return "\n\n".join(parts) if parts else (user_text or "请分析这段音频")


async def _build_image_content_part(
    chat_id: int | None, file_id: str, vision_prefer_url: bool
) -> Optional[dict]:
    """把单个图片 file_id 解析为 image_url content part（失败返回 None）。

    从 photo_group 分支抽出的公共逻辑，混合附件分支复用：
    vision_prefer_url 网关（Agnes）优先 R2 公开 URL，失败回退 base64；
    其余网关直接 base64 内联，并顺带做 R2 预上传与 TTL 缓存。
    """
    # Agnes 等 vision_prefer_url 网关：走公开 URL 路径。
    # 失败回退到 base64（OpenAI 等多数网关都支持）。
    if vision_prefer_url:
        public_url = await _resolve_r2_public_url_for_vision(file_id)
        if public_url:
            return {
                "type": "image_url",
                "image_url": {"url": public_url, "detail": "high"},
            }
        # R2 不可用，回退到 base64（仍然好过完全没图）。
        logger.debug(
            f"vision_prefer_url=True 但 R2 URL 不可用，回退 base64: {file_id[:12]}"
        )

    img_bytes = await get_cached_image_data(chat_id, file_id) if chat_id else None
    if not img_bytes:
        return None

    # 预防性后台上传到 R2：fire-and-forget，不阻塞 base64 编码。
    # 目的：
    #   1. 让内存 TTLCache (~5min) 过期后能从 R2 拉取，避免再调
    #      Telegram getFile API（Telegram bot getFile 有 rate limit）。
    #   2. 让未来切换到 Agnes (vision_prefer_url=True) 的轮次能
    #      零延迟拿 R2 公开 URL，不必再走同步上传路径。
    # Agnes 路径不经过这里（vision_prefer_url=True 时早已 return），
    # 所以同一张图不会被 put_object 两次。
    r2_key = _get_r2_key(file_id)
    _track_task(_upload_and_mark(file_id, img_bytes, r2_key))

    try:
        # 用 with 语句确保 PIL Image 在异常路径上也会被 close，
        # 避免大量并发图片处理时文件描述符泄露。
        with Image.open(io.BytesIO(img_bytes)) as img:
            fmt = img.format.lower() if img.format else "jpeg"
            if fmt not in ("jpeg", "png"):
                fmt = "jpeg"
            if fmt == "png" and img.mode == "RGBA":
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode()
            else:
                # convert() 返回的是新 Image 对象，同样需要 close。
                with img.convert("RGB") as img_rgb:
                    buf = io.BytesIO()
                    img_rgb.save(buf, format=fmt.upper())
                    b64 = base64.b64encode(buf.getvalue()).decode()
            return {
                "type": "image_url",
                "image_url": {"url": f"data:image/{fmt};base64,{b64}", "detail": "high"}
            }
    except Exception as e:
        logger.exception(f"处理图片 {file_id} 失败: {e}")
        return None


async def _resolve_mixed_attachments(
    entries: list[dict],
    model_info: ModelConfig,
    chat_id: int | None,
    user_text: str,
    vision_prefer_url: bool,
):
    """逐条解析混合 kind / 多音频附件（打断合并产物，无单一 type 可路由）。

    每个附件独立判断当前模型能力：支持的模态生成对应原生 content part
    （image_url / video_url / input_audio / file），不支持或解析失败的
    降级为文本占位（音频走转录降级，视频顺带后台持久化，保证切换模型
    后可恢复）。返回 content parts 列表；全部失败时返回占位文本。
    """
    supports_vision = model_info.vision
    supports_audio = model_info.audio
    supports_video = bool(getattr(model_info, "video", False))
    supports_native_documents = bool(getattr(model_info, "native_document", False))

    content_parts: list[dict] = []
    fallback_texts: list[str] = []

    for entry in entries:
        kind = str(entry.get("kind") or "").strip().lower()
        fid = str(entry.get("file_id") or "").strip()
        if not fid:
            continue
        fname = str(entry.get("file_name") or "").strip()
        mime = str(entry.get("mime_type") or "").strip()

        resolved_part = None
        if kind == "photo" and supports_vision:
            resolved_part = await _build_image_content_part(chat_id, fid, vision_prefer_url)
        elif kind == "video" and supports_video:
            public_url = await _resolve_r2_public_url_for_video(fid, mime or "video/mp4")
            if public_url:
                resolved_part = {"type": "video_url", "video_url": {"url": public_url}}
            else:
                # URL 不可用（R2 未配置/上传失败）：后台持久化，
                # 万一 R2 稍后恢复，下一轮可重新解析为原生视频。
                _track_task(_ensure_video_persisted(fid, mime or "video/mp4"))
        elif kind in ("audio", "voice"):
            if supports_audio:
                audio_bytes = await _get_cached_audio_data(chat_id, fid)
                if audio_bytes:
                    b64_data = base64.b64encode(audio_bytes).decode()
                    audio_format = (Path(fname).suffix.lstrip(".") or "ogg").lower()
                    if audio_format == "oga":
                        audio_format = "ogg"
                    resolved_part = {
                        "type": "input_audio",
                        "input_audio": {"data": b64_data, "format": audio_format},
                    }
            if resolved_part is None:
                # 模型不支持音频 / 字节获取失败：转录降级（与单音频路径一致）。
                fallback_texts.append(await _build_audio_fallback_text(
                    chat_id=chat_id,
                    file_id=fid,
                    file_name=fname or f"audio_{fid[:8]}.ogg",
                    user_text="",
                ))
                continue
        elif kind == "document" and supports_native_documents:
            resolved_part = await _build_native_document_part(
                chat_id,
                fid,
                file_name=fname or f"document_{fid[:8]}.pdf",
                mime_type=mime,
            )

        if resolved_part is not None:
            content_parts.append(resolved_part)
            continue

        # 该附件模态不被当前模型支持 / 解析失败：文本占位；视频顺带
        # 后台持久化，保证之后切换到支持的模型时不丢信息。
        if kind == "video":
            _track_task(_ensure_video_persisted(fid, mime or "video/mp4"))
        fallback_kind = kind if kind in ("photo", "video", "document") else "document"
        fallback_texts.append(await _build_attachment_fallback_text(
            kind=fallback_kind,
            file_ids=[fid],
            user_text="",
            chat_id=chat_id,
            file_names=[fname] if fname else [],
            mime_types=[mime] if mime else [],
        ))

    text_bits = [t for t in fallback_texts if t and str(t).strip()]
    if user_text and str(user_text).strip():
        text_bits.append(str(user_text))
    if text_bits:
        content_parts.append({"type": "text", "text": "\n\n".join(text_bits)})
    if content_parts:
        return content_parts
    return user_text


async def _resolve_multimodal_content(msg: dict, model_info: ModelConfig, chat_id: int | None = None):
    supports_vision = model_info.vision
    supports_audio = model_info.audio
    # 视频输入模态：默认由 provider 能力决定，模型必须显式设置 video=True 才开启。
    # 与 vision/audio 等参数保持一致：provider 只提供默认能力，模型配置负责覆盖。
    # 例如 OpenRouter 默认 video=False，但某个模型经过验证支持后可以手动 video=True。
    # 这样不会因为免费模型 metadata 声明支持视频而误发送 video_url。
    supports_video = bool(getattr(model_info, "video", False))

    supports_native_documents = bool(getattr(model_info, "native_document", False))
    # 部分网关（Agnes）只接受 image_url 里的公开 HTTP URL，不接受 data: base64。
    # 命中时优先用 R2 公开 URL；R2 不可用时回退 base64。
    provider_cfg = PROVIDERS.get(model_info.provider)
    vision_prefer_url = bool(getattr(provider_cfg, "vision_prefer_url", False)) if provider_cfg else False
    user_text = msg.get("content", "")
    if isinstance(user_text, str):
        user_text = _strip_reply_prefix(user_text)

    # ---------- 混合类型 / 多音频附件（打断合并产物） ----------
    # 打断合并（turn_recovery._merge_user_message）可能产生一条携带多种
    # kind 附件（如图片+视频）或多个音频的用户消息：这类消息没有单一
    # type 可供下方分支路由，这里按 attachments 逐条解析——支持该模态
    # 的附件生成对应 content part，不支持的降级为文本占位。普通消息
    # 不会进入本分支：所有生产者写入的 attachments 均为单一 kind；同类
    # 多附件场景在合并时已归一为 photo_group / video_group /
    # document_group 数组形态，由下方各分支按数组原生处理。
    atts = msg.get("attachments")
    if isinstance(atts, list) and len(atts) >= 2:
        entries = [a for a in atts if isinstance(a, dict) and a.get("file_id")]
        att_kinds = {str(a.get("kind") or "").strip().lower() for a in entries}
        if entries and (len(att_kinds) > 1 or att_kinds <= {"audio", "voice"}):
            return await _resolve_mixed_attachments(
                entries, model_info, chat_id, user_text, vision_prefer_url
            )

    # ---------- 图片 / 图片组 ----------
    if "file_ids" in msg and msg.get("type") in ("photo", "photo_group"):
        file_ids = list(msg.get("file_ids") or [])
        if supports_vision:
            results = await asyncio.gather(
                *[_build_image_content_part(chat_id, fid, vision_prefer_url) for fid in file_ids]
            )
            content_parts = [r for r in results if r is not None]
            if content_parts:
                # 即使当前模型支持视觉输入，也额外注入附件临时 URL。
                # 该 URL 与非多模态 fallback 使用同一套解析逻辑，
                # 便于图片编辑工具调用，以及后续模型切换后继续复用。
                url_lines = []
                for fid in file_ids:
                    try:
                        temp_url = await _resolve_public_attachment_url(fid)
                    except Exception:
                        logger.debug("_resolve_multimodal_content 内部忽略的异常", exc_info=True)
                        temp_url = ""
                    if temp_url:
                        url_lines.append(f"原始图片 URL: {temp_url}")
                if url_lines:
                    content_parts.append({
                        "type": "text",
                        "text": "\n".join(url_lines),
                    })
                content_parts.append({"type": "text", "text": user_text})
                return content_parts
            return user_text

        file_names = list(msg.get("file_names") or [])
        mime_types = list(msg.get("mime_types") or [])
        return await _build_attachment_fallback_text(
            kind="photo",
            file_ids=file_ids,
            user_text=user_text,
            chat_id=chat_id,
            file_names=file_names,
            mime_types=mime_types,
        )

    # ---------- 视频组（video_group，对称 photo_group） ----------
    if "file_ids" in msg and msg.get("type") == "video_group":
        vg_file_ids = list(msg.get("file_ids") or [])
        vg_file_names = list(msg.get("file_names") or [])
        vg_mime_types = list(msg.get("mime_types") or [])
        if vg_file_ids and supports_video:
            async def process_video_one(idx: int, fid: str):
                mime = ""
                if idx < len(vg_mime_types):
                    mime = str(vg_mime_types[idx] or "").strip()
                public_url = await _resolve_r2_public_url_for_video(fid, mime or "video/mp4")
                if public_url:
                    return {
                        "type": "video_url",
                        "video_url": {"url": public_url},
                    }
                return None

            results = await asyncio.gather(
                *[process_video_one(i, fid) for i, fid in enumerate(vg_file_ids)]
            )
            content_parts = [r for r in results if r is not None]
            # 无论整体走原生还是降级，解析失败的视频都触发后台持久化，
            # 保证之后切换模型/下一轮重试时仍有机会恢复。
            failed_indices = [i for i, r in enumerate(results) if r is None]
            for i in failed_indices:
                mime = vg_mime_types[i] if i < len(vg_mime_types) else ""
                _track_task(_ensure_video_persisted(vg_file_ids[i], mime or "video/mp4"))
            if content_parts:
                content_parts.append({"type": "text", "text": user_text})
                return content_parts
        elif vg_file_ids:
            # 模型不支持视频输入：后台持久化全部，保证切换模型不丢信息
            for i, fid in enumerate(vg_file_ids):
                mime = vg_mime_types[i] if i < len(vg_mime_types) else ""
                _track_task(_ensure_video_persisted(fid, mime or "video/mp4"))

        return await _build_attachment_fallback_text(
            kind="video",
            file_ids=vg_file_ids,
            user_text=user_text,
            chat_id=chat_id,
            file_names=vg_file_names,
            mime_types=vg_mime_types,
        )

    # ---------- 原生文档 / 文档组 ----------
    doc_file_ids = []
    doc_file_names = []
    doc_mime_types = []
    if ("file_id" in msg or "file_ids" in msg) and msg.get("type") in ("document", "document_group"):
        if "file_id" in msg:
            doc_file_ids = [msg["file_id"]]
            doc_file_names = [msg.get("file_name", "document.pdf")]
            doc_mime_types = [msg.get("mime_type", "")]
        else:
            doc_file_ids = list(msg.get("file_ids") or [])
            doc_file_names = list(msg.get("file_names") or [])
            doc_mime_types = list(msg.get("mime_types") or [])

        if supports_native_documents:
            if doc_file_ids:
                content_parts = []
                if user_text:
                    content_parts.append({"type": "text", "text": user_text})
                else:
                    content_parts.append(
                        {"type": "text", "text": "请分析这些文档。" if len(doc_file_ids) > 1 else "请分析这个文档。"})

                for idx, fid in enumerate(doc_file_ids):
                    file_name = doc_file_names[idx] if idx < len(doc_file_names) else f"document_{fid[:8]}.pdf"
                    mime_type = doc_mime_types[idx] if idx < len(doc_mime_types) else ""
                    part = await _build_native_document_part(chat_id, fid, file_name=file_name, mime_type=mime_type)
                    if part:
                        content_parts.append(part)

                if len(content_parts) > 1:
                    return content_parts
                return user_text
        elif doc_file_ids:
            return await _build_attachment_fallback_text(
                kind="document",
                file_ids=doc_file_ids,
                user_text=user_text,
                chat_id=chat_id,
                file_names=doc_file_names,
                mime_types=doc_mime_types,
            )

    # ---------- 单文件回退（音频 / 其他） ----------
    if "file_id" in msg:
        fid = msg["file_id"]
        file_type = str(msg.get("type") or "").lower()

        if file_type in ("audio", "voice"):
            file_name = msg.get("file_name", f"{file_type}_{fid[:8]}.ogg")
            if supports_audio:
                audio_bytes = await _get_cached_audio_data(chat_id, fid)
                if audio_bytes:
                    b64_data = base64.b64encode(audio_bytes).decode()
                    audio_format = (Path(file_name).suffix.lstrip(".") or "ogg").lower()
                    if audio_format == "oga":
                        audio_format = "ogg"
                    return [
                        {"type": "input_audio", "input_audio": {"data": b64_data, "format": audio_format}},
                        {"type": "text", "text": user_text or "请分析这段音频"}
                    ]
            return await _build_audio_fallback_text(
                chat_id=chat_id,
                file_id=fid,
                file_name=file_name,
                user_text=user_text,
            )

        if file_type == "video":
            file_name = msg.get("file_name", f"{file_type}_{fid[:8]}")
            mime_type = msg.get("mime_type", "")

            # ---------- 视频输入模态（与图片 supports_vision 路径对称） ----------
            # OpenRouter / vLLM / LiteLLM 等的事实标准：
            #   {"type": "video_url", "video_url": {"url": "<公开 URL>"}}
            # 视频统一走 URL（R2 公开域名 / 预签名），不走 base64 内联。
            if supports_video:
                public_url = await _resolve_r2_public_url_for_video(fid, mime_type or "video/mp4")
                if public_url:
                    return [
                        {
                            "type": "video_url",
                            "video_url": {"url": public_url},
                        },
                        {"type": "text", "text": user_text or "请分析这段视频。"},
                    ]
                # URL 不可用（R2 未配置 / 上传失败）：降级为文本占位。
                # 同时后台尝试持久化，万一 R2 稍后恢复/配置上，下一轮可
                # 重新解析为原生视频。
                _track_task(_ensure_video_persisted(fid, mime_type or "video/mp4"))
                logger.warning(
                    f"模型 {getattr(model_info, 'model_id', '?')} 支持视频但无法解析公开 URL，"
                    f"降级为文本占位: {fid[:12]}（检查 R2 配置）"
                )
            else:
                # 当前模型不支持视频输入：后台把字节持久化到 R2，
                # 保证后续切换到支持视频的模型时不丢信息。
                _track_task(_ensure_video_persisted(fid, mime_type or "video/mp4"))

            url = await _resolve_public_attachment_url(fid)
            lines = [f"📎 用户上传了{_attachment_label(file_type)}「{file_name}」"]
            if url:
                lines.append(f"链接：{url}")
            if mime_type:
                lines.append(f"mime_type：{mime_type}")
            if fid:
                lines.append(f"file_id：{fid}")
            if user_text:
                lines.append("")
                lines.append(f"用户原始指令：{user_text}")
            else:
                lines.append("")
                lines.append("用户未附加文字，请根据附件内容和上下文处理。")
            if supports_video:
                lines.append("")
                lines.append(
                    "说明：当前模型支持视频输入，但视频 URL 不可用（R2 未配置或上传失败），"
                    "已降级为文本占位；配置 R2 后新上传的视频可直接解析。"
                )
            return "\n".join(lines)

        # 说明：document / document_group 在上方"原生文档"分支已全路径
        # 处理（supports_native_documents 与降级文本均返回），此处不可能
        # 再收到该类型，无需再降级。

    return user_text


async def _append_history_async(messages: list, history: list, model_info: ModelConfig, chat_id: int | None = None) -> None:
    """把历史消息格式化后追加到 ``messages``。

    重要：发给模型 API 的消息体只允许包含 OpenAI 兼容协议认可的字段
    （``role``、``content``、``tool_calls``、``tool_call_id``、``name``、
    ``reasoning_content``）。Telegram 侧的附件元数据（``file_id``、
    ``file_ids``、``file_name`` 等）属于内部存储字段，**不能**写到
    出站消息里——否则部分网关（OpenAI / Anthropic / Gemini）会因未声
    明字段直接 400。此前版本在这里把附件元数据一起拷进了 out_msg，
    是一个静默导致请求失败的 BUG。
    """
    for msg in history:
        if msg.get("role") in ("user", "assistant", "tool", "system"):
            out_msg = {"role": msg["role"]}
            if msg.get("role") == "user":
                resolved = await _resolve_multimodal_content(dict(msg), model_info, chat_id=chat_id)
                out_msg["content"] = resolved
                # 注意：不要把 file_id / file_ids / file_name / mime_type /
                # type / attachments 等附件元数据写到出站消息里，部分
                # 模型 API 会因此返回 400。
            else:
                if "content" in msg:
                    content = msg["content"]
                    if isinstance(content, str):
                        out_msg["content"] = _strip_reply_prefix(content)
                    else:
                        out_msg["content"] = content
            for key in ["tool_calls", "tool_call_id", "name", "reasoning_content"]:
                if key in msg:
                    out_msg[key] = msg[key]
            messages.append(out_msg)


def _strip_reply_prefix(content):
    if isinstance(content, str) and "💡 引用回复:" in content:
        return content.split("💡 引用回复:")[-1].strip()
    return content


def _track_task(coro):
    """启动一个后台任务并保留强引用，避免被 GC 提前回收。"""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


def _mark_last_content_block_cacheable(msg: dict) -> bool:
    """
    在 OpenAI 兼容协议（OpenRouter 等网关）下，cache_control 必须挂在
    content 数组里"某一个具体 content block"上，而不是消息对象本身；
    网关会忽略挂在消息顶层的 cache_control 字段，导致标记静默失效。

    若 content 是字符串，先转成单元素的 text block 数组再打标记；
    若已经是数组（多模态消息），直接给最后一个 block 打标记。
    返回是否成功打上标记。
    """
    content = msg.get("content")
    if content is None:
        return False
    if isinstance(content, str):
        if not content:
            return False
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
        return True
    if isinstance(content, list) and content:
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block["cache_control"] = {"type": "ephemeral"}
            return True
    return False


def _apply_cache_control(messages: list) -> None:
    """
    为系统消息、上一轮对话末尾、最后一条消息添加 cache_control 标记。
    最多添加三个显式标记（Anthropic 单请求上限 4 个断点，剩余 1 个额度
    留给 agentic loop 的顶层自动缓存，见 agentic_loops._openrouter_extra_body）。

    注意：cache_control 必须打在 content block 上（见
    _mark_last_content_block_cacheable），打在消息顶层对 OpenRouter/
    OpenAI 兼容网关无效，会被静默忽略。

    断点策略（Anthropic 前缀缓存最佳实践）：
      1. system 消息末尾 —— 稳定不变的巨型系统提示（含技能目录）在每一轮
         都能命中，这是收益最大、最稳定的缓存段；
      2. 上一轮对话的最后一条可标记消息（倒数第二条区域）—— 把上一轮的
         完整内容（含最终 assistant 回复）纳入缓存前缀。没有这个断点时，
         下一轮请求最多命中到"上一轮的 user 消息"，上一轮的工具调用
         中段（往往占一轮 token 的大头）全部按原价重算；
      3. 最后一条消息（通常是本轮新 user 消息）—— 断点越靠后，缓存覆盖
         的前缀越长；agentic loop 的第 2..N 轮请求（追加了 tool 结果）
         可以直接命中到这里。
    本函数必须在"全部消息（含本轮新 user 消息）就位之后"调用，
    供 agentic loop 的每一轮请求复用：loop 内追加的 tool 消息位于
    断点之后，不影响断点之前的前缀命中。
    """
    if not messages:
        return
    # 为系统消息添加标记（断点 1）
    if messages[0].get("role") == "system":
        _mark_last_content_block_cacheable(messages[0])
    # 断点 2 + 3：从最后一条消息往前找两条 user/assistant 消息。
    # 最末一条覆盖"本轮新 user 消息"（loop 内多轮复用）；再往前一条
    # 覆盖"上一轮对话末尾"（跨轮命中）。已带标记的消息同样占用断点
    # 额度，因此统一计数，保证总数不超过 3。
    remaining_markers = 2
    for i in range(len(messages) - 1, 0, -1):
        if remaining_markers <= 0:
            break
        msg = messages[i]
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        already_marked = (
            isinstance(content, list)
            and content
            and isinstance(content[-1], dict)
            and "cache_control" in content[-1]
        )
        if already_marked:
            remaining_markers -= 1
            continue
        if _mark_last_content_block_cacheable(msg):
            remaining_markers -= 1


