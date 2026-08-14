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

from apitelegramchat.config import ModelConfig, TELEGRAM_BOT_TOKEN, R2_PUBLIC_URL
from apitelegramchat.utils import get_logger, transcribe_audio_with_groq
from apitelegramchat.file_handlers import get_file_path
from apitelegramchat.s3_utils import upload_bytes_to_r2, file_exists_in_r2, download_from_r2
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


async def get_cached_image_data(chat_id: int, file_id: str) -> Optional[bytes]:
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
        else:
            await state.mark_r2_attempted(file_id)

    tg_path = await get_file_path(file_id)
    if not tg_path:
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
                else:
                    await state.mark_r2_attempted(file_id)
    except Exception as e:
        logger.exception(f"图片下载失败 {file_id}: {e}")
        await state.mark_r2_attempted(file_id)
    return None


async def _upload_and_mark(file_id: str, data: bytes, r2_key: str):
    try:
        await upload_bytes_to_r2(data, r2_key, "image/jpeg")
    except Exception as e:
        logger.warning(f"R2后台上传失败 {file_id}: {e}")
    finally:
        await state.mark_r2_attempted(file_id)


async def _get_cached_audio_data(chat_id: int, file_id: str) -> Optional[bytes]:
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


async def _get_cached_document_data(chat_id: int, file_id: str) -> Optional[bytes]:
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
        chat_id: int,
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
    """把 Telegram file_id 解析成一个可供模型/工具继续引用的公开 URL。"""
    fid = str(file_id or "").strip()
    if not fid:
        return ""

    try:
        if TELEGRAM_BOT_TOKEN:
            tg_path = await get_file_path(fid)
            if tg_path:
                return f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_path}"
    except Exception as e:
        logger.debug(f"解析 Telegram 文件 URL 失败 {fid[:12]}: {e}")

    try:
        r2_key = _get_r2_key(fid)
        if await file_exists_in_r2(r2_key) and R2_PUBLIC_URL:
            return f"{R2_PUBLIC_URL.rstrip('/')}/{r2_key}"
    except Exception as e:
        logger.debug(f"解析 R2 文件 URL 失败 {fid[:12]}: {e}")

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
            logger.debug(f"[AudioFallback] 转录失败 {file_id[:12]}: {e}")

    parts: list[str] = []
    if user_text:
        parts.append(user_text)
    if transcript:
        parts.append(transcript)
    return "\n\n".join(parts) if parts else (user_text or "请分析这段音频")


async def _resolve_multimodal_content(msg: dict, model_info: ModelConfig, api_type: str, chat_id: int = None):
    supports_vision = model_info.vision
    supports_audio = model_info.audio
    supports_native_documents = bool(getattr(model_info, "native_document", False))
    user_text = msg.get("content", "")
    if isinstance(user_text, str):
        user_text = _strip_reply_prefix(user_text)

    # ---------- 图片 / 图片组 ----------
    if "file_ids" in msg and msg.get("type") in ("photo", "photo_group"):
        file_ids = list(msg.get("file_ids") or [])
        if supports_vision:
            async def process_one(fid):
                img_bytes = await get_cached_image_data(chat_id, fid) if chat_id else None
                if not img_bytes:
                    return None
                try:
                    img = Image.open(io.BytesIO(img_bytes))
                    fmt = img.format.lower() if img.format else "jpeg"
                    if fmt not in ("jpeg", "png"):
                        fmt = "jpeg"
                    if fmt == "png" and img.mode == "RGBA":
                        buf = io.BytesIO()
                        img.save(buf, format="PNG")
                        b64 = base64.b64encode(buf.getvalue()).decode()
                    else:
                        img_rgb = img.convert("RGB")
                        buf = io.BytesIO()
                        img_rgb.save(buf, format=fmt.upper())
                        b64 = base64.b64encode(buf.getvalue()).decode()
                    img.close()
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
    for msg in history:
        if msg.get("role") in ("user", "assistant", "tool", "system"):
            out_msg = {"role": msg["role"]}
            if msg.get("role") == "user":
                resolved = await _resolve_multimodal_content(dict(msg), model_info, api_type, chat_id=chat_id)
                out_msg["content"] = resolved
                for key in ("file_id", "file_ids", "file_name", "file_names", "mime_type", "mime_types", "type", "attachments"):
                    if key in msg:
                        out_msg[key] = msg[key]
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


def _apply_cache_control(messages: list) -> None:
    """
    为系统消息和最后一条用户/助手消息添加 cache_control 标记。
    固定最多添加两个标记，无需 token 计数。
    """
    if not messages:
        return
    # 为系统消息添加标记
    if messages[0].get("role") == "system":
        messages[0]["cache_control"] = {"type": "ephemeral"}
        markers_added = 1
    else:
        markers_added = 0
    # 如果还有余量，从后往前找一条 user/assistant 消息添加标记
    if markers_added < 2 and len(messages) >= 4:
        for i in range(len(messages) - 2, 0, -1):
            msg = messages[i]
            role = msg.get("role")
            if role in ("user", "assistant") and "cache_control" not in msg:
                msg["cache_control"] = {"type": "ephemeral"}
                break


