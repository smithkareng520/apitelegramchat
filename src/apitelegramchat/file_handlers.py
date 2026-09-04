# file_handlers.py — 音频转录（Groq API），不再包含图片OCR
import mimetypes
import os
import aiohttp
import asyncio
import logging
from urllib.parse import quote
from cachetools import LRUCache

from apitelegramchat.config import (
    TELEGRAM_BOT_TOKEN,
    BASE_URL,
)

from apitelegramchat.s3_utils import upload_bytes_to_r2, file_exists_in_r2, download_from_r2

logger = logging.getLogger(__name__)

# ---------- 文件下载锁 ----------
# 用 LRUCache 避免 dict 无界增长（每个 file_id 一把锁，长期运行会累积）。
_DOWNLOAD_LOCKS_MAX = 256
_download_locks: LRUCache = LRUCache(maxsize=_DOWNLOAD_LOCKS_MAX)
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
        # 必须设置超时：此前没有 timeout，Telegram API stall 时会
        # 无限期挂起，间接阻塞所有等待该 file_id 的协程。
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
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
        # 脱敏：str(e) 可能含 BASE_URL（带 bot token），不要原样打日志
        # 到 ERROR 级别（容易被采集到外部日志系统）。但保留诊断价值。
        safe_msg = str(e)
        if BASE_URL and BASE_URL in safe_msg:
            safe_msg = "[redacted url]"
        logger.error(f"获取文件路径失败: {safe_msg}")
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
        timeout = aiohttp.ClientTimeout(total=60, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_real_path}") as response:
                if response.status == 200:
                    with open(file_path, "wb") as f:
                        f.write(await response.read())
                    return True
                else:
                    logger.error(f"文件下载失败: HTTP {response.status}")
                    return False
    except Exception as e:
        safe_msg = str(e)
        if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN in safe_msg:
            safe_msg = "[redacted token]"
        logger.error(f"文件下载失败: {safe_msg}")
        return False

# ---------- 主下载函数（含 R2 缓存） ----------
async def download_file(
    file_id: str, file_path: str, mime_type: str = ""
) -> bool:
    """下载文件到 file_path，并异步缓存到 R2（telegram/<file_id>）。

    v5：``mime_type`` 是 Telegram 消息里报告的原始 MIME（如 .htm 文件的
    text/html），透传给后台上传任务写入 R2 ContentType。此前上传端按
    本地临时文件扩展名查一张很小的映射表，查不到一律写
    application/octet-stream——R2 里的对象从此失去全部类型信息，媒体
    代理按 key 也推不出扩展名，对外交付的 URL/响应头均无文件名与类型，
    Telegram 富文本抓取器下载成功也无法建档（v5 复盘的根因之一）。
    """
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
                # v5：透传 Telegram 报告的真实 mime_type。
                _t = asyncio.create_task(
                    _upload_to_r2_after_download(file_id, file_path, mime_type)
                )
                _file_bg_tasks.add(_t)
                _t.add_done_callback(_file_bg_tasks.discard)
                return True
            else:
                logger.warning(f"Telegram 下载文件写入失败或为空: {file_path}")
                return False
        return False

# v5：上传 ContentType 解析——真实 mime 优先，扩展名映射与标准库推断兑底。
# 历史映射表只覆盖 6 种扩展名，.htm/.html/.md 等常见文档一律被写成
# octet-stream；现改为 mimetypes.guess_type 全量兑底。
_EXT_MIME_OVERRIDES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".htm": "text/html", ".html": "text/html",
}


def _resolve_upload_content_type(mime_type: str = "", file_path: str = "") -> str:
    """解析 R2 上传 ContentType：显式 mime > 扩展名映射 > 标准库推断 > octet-stream。"""
    mt = (mime_type or "").strip().lower()
    if mt and mt != "application/octet-stream":
        return mt
    ext = os.path.splitext(file_path or "")[1].lower()
    if ext in _EXT_MIME_OVERRIDES:
        return _EXT_MIME_OVERRIDES[ext]
    try:
        guessed = mimetypes.guess_type(file_path or "")[0]
        if guessed:
            return guessed
    except Exception:
        pass
    return "application/octet-stream"


async def _upload_to_r2_after_download(
    file_id: str, file_path: str, mime_type: str = ""
):
    """异步上传文件到 R2，供后台调用"""
    try:
        with open(file_path, "rb") as f:
            data = f.read()
        key = _get_r2_key(file_id)
        content_type = _resolve_upload_content_type(mime_type, file_path)
        url = await upload_bytes_to_r2(data, key, content_type)
        if url:
            logger.info(f"文件已上传到 R2: {url}")
        else:
            logger.warning("R2 上传失败，但本地文件已下载")
    except Exception as e:
        logger.error(f"异步上传到 R2 失败: {e}")
