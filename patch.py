from pathlib import Path
import re

root = Path('/mnt/data/fixwork2')

# ---- ai_handlers.py ----
p = root / 'src/apitelegramchat/ai_handlers.py'
text = p.read_text()
text = text.replace(
'''async def _get_cached_audio_data(chat_id: int, file_id: str) -> Optional[bytes]:
    cache_key = file_id
    if cache_key in _audio_cache:
        return _audio_cache[cache_key]

    r2_key = _get_r2_key(file_id)
    if await file_exists_in_r2(r2_key):
        data = await download_from_r2(r2_key)
        if data:
            _audio_cache[cache_key] = data
            return data

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
                    _track_task(upload_bytes_to_r2(data, r2_key, "audio/ogg"))
                    return data
    except Exception as e:
        logger.exception(f"音频下载失败 {file_id}: {e}")
    return None
''',
'''async def _get_cached_audio_data(chat_id: int, file_id: str) -> Optional[bytes]:
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
''',
1)
text = text.replace(
'''                    return [
                        {"type": "input_audio", "input_audio": {"data": b64_data, "format": "ogg"}},
                        {"type": "text", "text": user_text or "请分析这段音频"}
                    ]
''',
'''                    audio_format = (Path(file_name).suffix.lstrip(".") or "ogg").lower()
                    if audio_format == "oga":
                        audio_format = "ogg"
                    return [
                        {"type": "input_audio", "input_audio": {"data": b64_data, "format": audio_format}},
                        {"type": "text", "text": user_text or "请分析这段音频"}
                    ]
''',
1)
p.write_text(text)

# ---- app.py ----
p = root / 'src/apitelegramchat/app.py'
text = p.read_text()
start = text.index('                    elif media_type in ("audio", "voice"):\n')
end = text.index('                    else:\n                        content_text = f"📎 用户引用了媒体「{file_name}」\\n\\n{user_input}" if user_input else f"📎 用户引用了媒体「{file_name}」"\n                        user_message = {"role": "user", "content": content_text}\n', start)
new_block = '''                    elif media_type in ("audio", "voice"):
                        if supports_audio:
                            content_text = f"📎 用户引用了音频「{file_name}」"
                            if user_input:
                                content_text += f"\\n\\n{user_input}"
                            else:
                                content_text += "\\n\\n请分析这段音频"
                            user_message = {
                                "role": "user",
                                "content": content_text,
                                "file_id": reply_media["file_id"],
                                "file_name": file_name,
                                "type": media_type,
                                "attachments": [
                                    {
                                        "kind": media_type,
                                        "file_id": reply_media["file_id"],
                                        "file_name": file_name,
                                    }
                                ],
                            }
                        else:
                            content_text_parts = []
                            if user_input:
                                content_text_parts.append(user_input)
                            if GROQ_API_KEY:
                                audio_bytes = await _get_cached_audio_data(chat_id, reply_media["file_id"])
                                if audio_bytes:
                                    ext = os.path.splitext(file_name)[1] or ".ogg"
                                    try:
                                        transcribed_text = await transcribe_audio_with_groq(audio_bytes, ext)
                                        if transcribed_text:
                                            content_text_parts.append(transcribed_text)
                                    except Exception as e:
                                        logger.error(f"Groq 转录失败: {e}")
                            if not content_text_parts:
                                content_text_parts.append("请分析这段音频")
                            content_text = "\\n\\n".join(content_text_parts)
                            user_message = {
                                "role": "user",
                                "content": content_text,
                                "file_id": reply_media["file_id"],
                                "file_name": file_name,
                                "type": media_type,
                                "attachments": [
                                    {
                                        "kind": media_type,
                                        "file_id": reply_media["file_id"],
                                        "file_name": file_name,
                                    }
                                ],
                            }
                    elif media_type == "video":
                        content_text = f"📎 用户引用了视频「{file_name}」"
                        if user_input:
                            content_text += f"\\n\\n{user_input}"
                        else:
                            content_text += "\\n\\n请分析这个视频"
                        user_message = {
                            "role": "user",
                            "content": content_text,
                            "file_id": reply_media["file_id"],
                            "file_name": file_name,
                            "type": "video",
                            "attachments": [
                                {
                                    "kind": "video",
                                    "file_id": reply_media["file_id"],
                                    "file_name": file_name,
                                }
                            ],
                        }
'''
text = text[:start] + new_block + text[end:]
p.write_text(text)
