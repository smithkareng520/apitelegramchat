# file_handlers.py — 音频转录（Groq API），不再包含图片OCR
import os
import aiohttp
import asyncio
import logging
from urllib.parse import quote

from apitelegramchat.config import (
    TELEGRAM_BOT_TOKEN,
    BASE_URL,
    PARSE_TIMEOUT,
)

from apitelegramchat.s3_utils import upload_bytes_to_r2, file_exists_in_r2, download_from_r2

logger = logging.getLogger(__name__)

# ---------- 文件下载锁 ----------
_download_locks = {}
_download_locks_lock = asyncio.Lock()

# 后台任务引用集合（防止 GC 提前取消）
_file_bg_tasks: set = set()

async def _get_download_lock(file_id: str) -> asyncio.Lock:
    async with _download_locks_lock:
        if file_id not in _download_locks:
            _download_locks[file_id] = asyncio.Lock()
        return _download_locks[file_id]

# ========== 获取文件路径 ==========
async def get_file_path(file_id: str) -> str:
    """通过 Telegram API 获取文件的下载路径"""
    try:
        # 对 file_id 进行 URL 编码，防止异常字符破坏 URL
        encoded_fid = quote(file_id, safe="")
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/getFile?file_id={encoded_fid}") as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        return data["result"]["file_path"]
                    else:
                        logger.error(f"获取文件路径失败: {data.get('description')}")
                        return None
                else:
                    logger.error(f"获取文件路径失败: {await response.text()}")
                    return None
    except Exception as e:
        logger.error(f"获取文件路径失败: {str(e)}")
        return None

# ---------- R2 键生成 ----------
def _get_r2_key(file_id: str) -> str:
    return f"telegram/{file_id}"

# ---------- 从 Telegram 下载 ----------
async def _telegram_download(file_id: str, file_path: str) -> bool:
    try:
        file_real_path = await get_file_path(file_id)
        if not file_real_path:
            return False
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_real_path}") as response:
                if response.status == 200:
                    with open(file_path, "wb") as f:
                        f.write(await response.read())
                    return True
                else:
                    logger.error(f"文件下载失败: {await response.text()}")
                    return False
    except Exception as e:
        logger.error(f"文件下载失败: {str(e)}")
        return False

# ---------- 主下载函数（含 R2 缓存） ----------
async def download_file(file_id: str, file_path: str) -> bool:
    lock = await _get_download_lock(file_id)
    async with lock:
        key = _get_r2_key(file_id)
        # 尝试 R2
        try:
            if await file_exists_in_r2(key):
                data = await download_from_r2(key)
                if data:
                    with open(file_path, "wb") as f:
                        f.write(data)
                    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                        return True
                    else:
                        logger.warning(f"R2 下载文件写入失败或为空: {file_path}")
                else:
                    logger.warning(f"R2 文件存在但下载失败，回退到 Telegram: {key}")
        except Exception as e:
            logger.warning(f"R2 操作异常，回退到 Telegram 下载: {e}")

        # 回退 Telegram
        success = await _telegram_download(file_id, file_path)
        if success:
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                # 保留后台任务强引用，避免被 GC 提前取消
                _t = asyncio.create_task(_upload_to_r2_after_download(file_id, file_path))
                _file_bg_tasks.add(_t)
                _t.add_done_callback(_file_bg_tasks.discard)
                return True
            else:
                logger.warning(f"Telegram 下载文件写入失败或为空: {file_path}")
                return False
        return False

async def _upload_to_r2_after_download(file_id: str, file_path: str):
    """异步上传文件到 R2，供后台调用"""
    try:
        with open(file_path, "rb") as f:
            data = f.read()
        key = _get_r2_key(file_id)
        ext = os.path.splitext(file_path)[1].lower()
        content_type = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".pdf": "application/pdf",
            ".txt": "text/plain", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        }.get(ext, "application/octet-stream")
        url = await upload_bytes_to_r2(data, key, content_type)
        if url:
            logger.info(f"文件已上传到 R2: {url}")
        else:
            logger.warning("R2 上传失败，但本地文件已下载")
    except Exception as e:
        logger.error(f"异步上传到 R2 失败: {e}")

# ========== 音频转录（保持原样） ==========
async def _parse_audio_file(file_path: str, file_name: str) -> str:
    """解析音频文件，使用 Groq API 进行转录"""
    try:
        ext = os.path.splitext(file_name)[1] or ".ogg"
        with open(file_path, "rb") as f:
            audio_bytes = f.read()
        from apitelegramchat.utils import transcribe_audio_with_groq
        # 使用配置的 PARSE_TIMEOUT 而非硬编码 30 秒
        return await asyncio.wait_for(
            transcribe_audio_with_groq(audio_bytes, ext),
            timeout=PARSE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"音频转录超时: {file_name}")
        return "⏱️ 音频转录超时，请稍后重试。"
    except Exception as e:
        logger.error(f"音频转录失败: {str(e)}")
        return None


