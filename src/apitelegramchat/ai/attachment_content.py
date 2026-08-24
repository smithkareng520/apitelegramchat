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

from apitelegramchat.config import ModelConfig, TELEGRAM_BOT_TOKEN, R2_PUBLIC_URL, PROVIDERS
from apitelegramchat.utils import get_logger, transcribe_audio_with_groq
from apitelegramchat.file_handlers import get_file_path
from apitelegramchat.s3_utils import (
    upload_bytes_to_r2,
    file_exists_in_r2,
    download_from_r2,
    public_url_for_existing_key,
)
import apitelegramchat.state as state

logger = get_logger(__name__)

from cachetools import TTLCache
from apitelegramchat.config import CACHE_TTL

_image_cache = TTLCache(maxsize=1000, ttl=CACHE_TTL)
_audio_cache = TTLCache(maxsize=500, ttl=CACHE_TTL)
_document_cache = TTLCache(maxsize=300, ttl=CACHE_TTL)

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
    """Resolve image bytes for a Telegram file_id.

    Resolution order:
      1. In-memory TTLCache (~5 min) — hot path for repeat requests within a turn.
      2. Permanent-failure marker (`state.is_r2_attempted`) — if set, bail out
         early because we've already determined that neither R2 nor Telegram
         can serve this file. (NOTE: this marker must ONLY be set on actual
         *hard* failures — see below.)
      3. R2 / local cache — if the file has been previously uploaded, download
         it and re-populate the in-memory cache. This is the recovery path
         that keeps historical images visible after the TTLCache expires.
      4. Telegram — first-time fetch; on success, kick off a background R2
         upload so the next cache miss can be served from R2 alone.

    IMPORTANT: `state.mark_r2_attempted` is a *permanent* failure marker that
    short-circuits all future resolution attempts for that file_id. It must
    therefore be set ONLY on hard, non-transient failures (404/403/410 from
    Telegram, or R2 reporting that the object is unreadable). Setting it on
    transient errors (429 rate-limit, 5xx, network blip, or any ``Exception``)
    permanently blacklists the file across ALL future turns — even after the
    transient condition clears — causing historical images to silently
    disappear from the conversation. We now only mark on hard status codes.
    """
    cache_key = file_id
    if cache_key in _image_cache:
        return _image_cache[cache_key]

    if await state.is_r2_attempted(file_id):
        # Permanent failure — neither R2 nor Telegram can serve this file.
        return None

    r2_key = _get_r2_key(file_id)
    if await file_exists_in_r2(r2_key):
        data = await download_from_r2(r2_key)
        if data:
            _image_cache[cache_key] = data
            return data
        else:
            # R2 reports the object exists but the body is unreadable; treat
            # as a permanent failure so we don't loop on every request.
            await state.mark_r2_attempted(file_id)

    tg_path = await get_file_path(file_id)
    if not tg_path:
        # Telegram reports the file_path itself is missing — this is a hard
        # failure (file deleted on Telegram's side). Safe to mark permanently.
        await state.mark_r2_attempted(file_id)
        return None
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    _image_cache[cache_key] = data
                    _track_task(_upload_and_mark(file_id, data, r2_key))
                    return data
                elif resp.status in (404, 403, 410):
                    # Hard failure — file is gone or access permanently denied.
                    # Safe to mark permanently so future turns skip the round-trip.
                    logger.warning(
                        f"图片下载永久失败 {file_id}: HTTP {resp.status}"
                    )
                    await state.mark_r2_attempted(file_id)
                else:
                    # Transient (429/5xx/etc.) — DO NOT mark. Next turn may succeed.
                    logger.warning(
                        f"图片下载临时失败 {file_id}: HTTP {resp.status} (will retry next turn)"
                    )
    except asyncio.TimeoutError as e:
        # Network blip — do not permanently blacklist the file.
        logger.warning(f"图片下载超时 {file_id}: {e} (will retry next turn)")
    except aiohttp.ClientError as e:
        # Network / connection-level error — also transient.
        logger.warning(f"图片下载网络异常 {file_id}: {e} (will retry next turn)")
    except Exception as e:
        # Unknown error — log full traceback but DO NOT permanently mark.
        # Earlier code did `mark_r2_attempted` here, which permanently
        # blacklisted files on any unexpected exception (e.g. a brief DNS
        # hiccup), causing silent image loss across turns.
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
        if await file_exists_in_r2(r2_key) and R2_PUBLIC_URL:
            return f"{R2_PUBLIC_URL.rstrip('/')}/{r2_key}"
    except Exception as e:
        logger.debug(f"解析 R2 文件 URL 失败 {fid[:12]}: {e}")

    return ""


async def _resolve_r2_public_url_for_vision(file_id: str) -> str:
    """Resolve a publicly-accessible HTTP URL for an image, suitable for
    OpenAI-compatible vision APIs that reject data: base64 URLs (e.g. Agnes
    2.5 Flash — per its docs: "Image inputs must use publicly accessible
    image_url values").

    Returns the R2 public URL **or** an R2 presigned URL — NEVER the
    Telegram direct URL (which would leak the bot token to third-party APIs).

    The function ensures the file is uploaded to R2 first (synchronously),
    so the returned URL is immediately usable. Three failure modes return
    "" so the caller can fall back to base64:
      - R2 is not configured at all (local-cache fallback: file:// is not
        publicly fetchable).
      - The image bytes can't be retrieved from cache / Telegram.
      - The R2 upload itself fails.

    Note: presigned URLs expire (default 1h). For multi-turn sessions the
    same file_id will be re-resolved on each turn, so a fresh presigned URL
    is issued when needed — historical URLs in already-stored messages are
    re-built every turn via ``_append_history_async``.
    """
    fid = str(file_id or "").strip()
    if not fid:
        return ""

    r2_key = _get_r2_key(fid)
    try:
        if await file_exists_in_r2(r2_key):
            # Already in R2 — return whatever public URL form is available
            # (custom domain, r2.dev, or presigned). Returns None if R2
            # isn't configured remotely.
            url = await public_url_for_existing_key(r2_key)
            if url:
                return url
            # Fall through to attempt a synchronous upload — covers the
            # edge case where file_exists_in_r2 returned True for the
            # local-cache path but we still can't serve a public URL.
            return ""

        # Not in R2 yet — synchronously fetch from Telegram and upload so
        # that we have a public URL for THIS turn's request.
        img_bytes = await get_cached_image_data(None, fid)
        if not img_bytes:
            return ""
        # upload_bytes_to_r2 returns the public URL (custom domain, r2.dev,
        # or presigned) on success, or None on failure.
        result = await upload_bytes_to_r2(img_bytes, r2_key, "image/jpeg")
        if result is None:
            return ""
        # If upload_bytes_to_r2 fell back to a local file:// URL, that's not
        # publicly fetchable — signal the caller to use base64 instead.
        if result.startswith("file://"):
            return ""
        return result
    except Exception as e:
        logger.debug(f"为 vision 解析 R2 公开 URL 失败 {fid[:12]}: {e}")
        return ""


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

    for idx, fid in enumerate(file_ids, start=1):
        fname = ""
        if file_names and idx - 1 < len(file_names):
            fname = str(file_names[idx - 1] or "").strip()
        mime = ""
        if mime_types and idx - 1 < len(mime_types):
            mime = str(mime_types[idx - 1] or "").strip()
        url = await _resolve_public_attachment_url(fid) if fid else ""
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
        lines.append("说明：原始附件已保留；若当前模型不支持直接读取该类型内容，请基于上述链接调用工具或进行文本降级处理。")

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


async def _resolve_multimodal_content(msg: dict, model_info: ModelConfig, api_type: str, chat_id: int | None = None):
    supports_vision = model_info.vision
    supports_audio = model_info.audio
    supports_native_documents = bool(getattr(model_info, "native_document", False))
    # 部分网关（Agnes）只接受 image_url 里的公开 HTTP URL，不接受 data: base64。
    # 命中时优先用 R2 公开 URL；R2 不可用时回退 base64。
    provider_cfg = PROVIDERS.get(model_info.provider)
    vision_prefer_url = bool(getattr(provider_cfg, "vision_prefer_url", False)) if provider_cfg else False
    user_text = msg.get("content", "")
    if isinstance(user_text, str):
        user_text = _strip_reply_prefix(user_text)

    # ---------- 图片 / 图片组 ----------
    if "file_ids" in msg and msg.get("type") in ("photo", "photo_group"):
        file_ids = list(msg.get("file_ids") or [])
        if supports_vision:
            async def process_one(fid):
                # 优先用公开 HTTP URL（Agnes 等只接受公开 URL 的网关）。
                # 失败回退到 base64 data URL（OpenAI 等多数网关都支持）。
                if vision_prefer_url:
                    public_url = await _resolve_r2_public_url_for_vision(fid)
                    if public_url:
                        return {
                            "type": "image_url",
                            "image_url": {"url": public_url, "detail": "high"},
                        }
                    # R2 不可用，回退到 base64（仍然好过完全没图）。
                    logger.debug(
                        f"vision_prefer_url=True 但 R2 URL 不可用，回退 base64: {fid[:12]}"
                    )

                img_bytes = await get_cached_image_data(chat_id, fid) if chat_id else None
                if not img_bytes:
                    return None
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
                    logger.exception(f"处理图片 {fid} 失败: {e}")
                    return None

            results = await asyncio.gather(*[process_one(fid) for fid in file_ids])
            content_parts = [r for r in results if r is not None]
            if content_parts:
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
            url = await _resolve_public_attachment_url(fid)
            lines = [f"📎 用户上传了{_attachment_label(file_type)}「{file_name}」"]
            if url:
                lines.append(f"链接：{url}")
            if mime_type:
                lines.append(f"mime_type：{mime_type}")
            if user_text:
                lines.append("")
                lines.append(f"用户原始指令：{user_text}")
            else:
                lines.append("")
                lines.append("用户未附加文字，请根据附件内容和上下文处理。")
            return "\n".join(lines)

        if file_type in ("document", "document_group"):
            file_name = msg.get("file_name", f"document_{fid[:8]}.pdf")
            mime_type = msg.get("mime_type", "")
            url = await _resolve_public_attachment_url(fid)
            lines = [f"📎 用户上传了文档「{file_name}」"]
            if url:
                lines.append(f"链接：{url}")
            if mime_type:
                lines.append(f"mime_type：{mime_type}")
            if user_text:
                lines.append("")
                lines.append(f"用户原始指令：{user_text}")
            else:
                lines.append("")
                lines.append("用户未附加文字，请根据文档内容和上下文处理。")
            return "\n".join(lines)

    return user_text


async def _append_history_async(messages: list, history: list, api_type: str, model_info: ModelConfig, chat_id: int | None = None) -> None:
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
                resolved = await _resolve_multimodal_content(dict(msg), model_info, api_type, chat_id=chat_id)
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
    为系统消息和最后一条用户/助手消息添加 cache_control 标记。
    固定最多添加两个标记，无需 token 计数。

    注意：cache_control 必须打在 content block 上（见
    _mark_last_content_block_cacheable），打在消息顶层对 OpenRouter/
    OpenAI 兼容网关无效，会被静默忽略。
    """
    if not messages:
        return
    # 为系统消息添加标记
    markers_added = 0
    if messages[0].get("role") == "system":
        if _mark_last_content_block_cacheable(messages[0]):
            markers_added = 1
    # 如果还有余量，从后往前找一条 user/assistant 消息添加标记
    if markers_added < 2 and len(messages) >= 4:
        for i in range(len(messages) - 2, 0, -1):
            msg = messages[i]
            role = msg.get("role")
            if role in ("user", "assistant"):
                content = msg.get("content")
                already_marked = (
                    isinstance(content, list)
                    and content
                    and isinstance(content[-1], dict)
                    and "cache_control" in content[-1]
                )
                if not already_marked and _mark_last_content_block_cacheable(msg):
                    break


