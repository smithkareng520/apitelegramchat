# media_proxy.py — 自托管稳定媒体代理（v4，2026-09-05）
"""为什么需要这个模块：Telegram 富文本抓取器无法把 R2 S3 API 预签名 URL
解析为可用媒体，媒体必须以"无查询参数的稳定 https URL"对外交付。

2026-09-05 02:02 线上案例（chat=7162243624 trace a710fbdc）复盘：
  - 模型把 R2 预签名 URL 写进 ``<tg-document src>``；
  - v3 的 ``&amp;`` / 裸 ``&`` 双形态重试全部被
    ``RICH_MESSAGE_DOCUMENT_NO_MEDIA_FOUND`` 拒绝 —— 排除 HTML 转义因素；
  - 同一 URL 从公网直接 GET 返回 HTTP 200（把 ``%2F`` 解码成 ``/`` 也仍
    返回 200）—— 排除"URL 不可达 / 签名被破坏"；
  - 结论：问题出在抓取器对这类"长查询串 + 私有 S3 API 端点"URL 的内部
    处理（规范化时剥离/重写查询参数，或对非稳定 URL 直接判媒体不存在）。

修复：对外交付 URL 统一走 ``s3_utils.resolve_stable_delivery_url``：

  1) ``R2_PUBLIC_URL``（r2.dev / 自定义域）—— 直连对象存储；
  2) 自托管代理 ``{base}/media/<hmac>/<key>`` —— 本模块提供签名与回源。
     base 按 ``MEDIA_PROXY_BASE_URL → PUBLIC_BASE_URL → WEBHOOK_URL origin``
     推导：webhook 能收到 Telegram 发来的请求，同一域名抓取器必然可达；
  3) 都不可用才退回预签名 URL（本地开发等场景）。

代理 URL 安全性：路径 token = HMAC-SHA256(secret, key) 截断 16 hex，
与 key 绑定、不可伪造、不随时间过期；secret 默认从 bot token 派生。
URL 永久有效 —— 模型在历史消息里回显旧 URL 也不会失效，这一点是
1 小时过期的预签名 URL 做不到的。

注意：``MEDIA_PROXY_SECRET`` 等环境变量含敏感字样，会被
``config.scrub_environment`` 从 os.environ 清洗 —— 因此必须在
``config.py`` 顶层捕获为常量，本模块只读 config 常量，绝不 ``os.getenv``。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import os
from urllib.parse import quote, urlparse

from apitelegramchat.config import (
    MEDIA_PROXY_BASE_URL,
    MEDIA_PROXY_SECRET,
    PUBLIC_BASE_URL,
    PUBLIC_WEBHOOK_URL,
    TELEGRAM_BOT_TOKEN,
)

logger = logging.getLogger(__name__)

# 回源字节上限：内存保护。Telegram bot API getFile 下载上限本就是 20MB，
# 这里放宽到 64MB 以兼容生成的媒体对象，超过直接 413。
MEDIA_PROXY_MAX_BYTES = 64 * 1024 * 1024

# 路径 token 长度（hex）。64 bit 对个人 bot 的暴力枚举已足够（且 key 本身
# 是不可枚举的 file_id / 随机 key），无需完整 64 hex。
_TOKEN_HEX_LEN = 16

# Telegram 文档/媒体常见扩展名 → MIME。key 本身（telegram/<file_id>）不含
# 扩展名，所以优先信 R2 上传时记录的 ContentType，这里只做兜底推断。
_EXTRA_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}


def _media_proxy_secret() -> bytes:
    """HMAC 签名密钥：显式配置优先，否则从 bot token 派生。

    派生规则在同一部署内字节级稳定；换 bot token 会使历史代理 URL 失效
    （对象本身无损，重新生成 URL 即可），在 README 中已提示可通过设置
    ``MEDIA_PROXY_SECRET`` 固定密钥来规避。
    """
    if MEDIA_PROXY_SECRET:
        return MEDIA_PROXY_SECRET.encode("utf-8")
    material = f"apitelegramchat-media-proxy:v1:{TELEGRAM_BOT_TOKEN or ''}"
    return hashlib.sha256(material.encode("utf-8")).digest()


def sign_media_key(key: str) -> str:
    """为对象 key 计算 URL 路径签名 token（与 key 绑定，不随时间过期）。"""
    secret = _media_proxy_secret()
    return hmac.new(
        secret, str(key or "").encode("utf-8"), hashlib.sha256
    ).hexdigest()[:_TOKEN_HEX_LEN]


def verify_media_token(key: str, token: str) -> bool:
    """常量时间校验路径 token；空值一律拒绝。"""
    if not key or not token:
        return False
    return hmac.compare_digest(sign_media_key(key), str(token))


def media_proxy_base_url() -> str | None:
    """推导自托管媒体代理的公开基地址（scheme://host，不含路径与查询串）。

    优先级：``MEDIA_PROXY_BASE_URL`` → ``PUBLIC_BASE_URL`` →
    ``WEBHOOK_URL`` 的 origin。webhook URL 可能带路径与 ``?token=``，
    只取 origin 部分。
    """
    for candidate in (MEDIA_PROXY_BASE_URL, PUBLIC_BASE_URL):
        base = (candidate or "").strip().rstrip("/")
        if not base:
            continue
        parsed = urlparse(base)
        if parsed.scheme in {"http", "https"} and (parsed.hostname or "") and not parsed.query:
            return base
        logger.warning("忽略无效的媒体代理基地址候选: %r", base[:120])
    raw = (PUBLIC_WEBHOOK_URL or "").strip()
    if raw:
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"} and (parsed.hostname or ""):
            return f"{parsed.scheme}://{parsed.netloc}"
    return None


def build_media_proxy_url(key: str) -> str | None:
    """构造 ``{base}/media/<token>/<key>`` 稳定代理 URL；基地址不可得时 None。"""
    base = media_proxy_base_url()
    if not base:
        return None
    k = str(key or "").strip()
    if not k:
        return None
    # safe='/'：保留 key 内的层级结构，供 Quart <path:key> 转换器匹配。
    # file_id 属于 base64url 安全字符，quote 后不变。
    return f"{base}/media/{sign_media_key(k)}/{quote(k, safe='/')}"


def guess_content_type(key: str, provided: str | None = None) -> str:
    """推断响应 Content-Type。

    优先使用 R2 上传时记录的 ContentType（``application/octet-stream``
    视为"未记录"，因为上传端对未知扩展名会写这个默认值）；否则按
    key / 远端文件路径的扩展名推断；最后兜底 octet-stream。
    """
    ct = (provided or "").strip().lower()
    if ct and ct != "application/octet-stream":
        return ct
    ext = os.path.splitext(str(key or ""))[1].lower()
    if ext in _EXTRA_MIME_BY_EXT:
        return _EXTRA_MIME_BY_EXT[ext]
    try:
        guessed, _ = mimetypes.guess_type(str(key or ""))
        if guessed:
            return guessed
    except Exception:
        pass
    return "application/octet-stream"


async def collect_media_bytes(key: str) -> tuple[bytes, str] | None:
    """回源取媒体字节：R2 优先；``telegram/<file_id>`` 形态回退 Telegram 直下。

    返回 ``(bytes, content_type)``；彻底取不到时返回 None（路由层回 404，
    发送链路按既有降级规则处理）。

    延迟导入 s3_utils / file_handlers：s3_utils 依赖本模块构造代理 URL，
    顶层互参会成环；运行期按需导入即可。
    """
    k = str(key or "").strip()
    if not k:
        return None

    from apitelegramchat import s3_utils

    got = await s3_utils.fetch_r2_object(k)
    if got:
        data, provided_ct = got
        return data, guess_content_type(k, provided_ct)

    if not k.startswith("telegram/"):
        return None
    file_id = k.split("/", 1)[1].strip()
    if not file_id:
        return None

    # R2 miss → 从 Telegram 重新下载（download_file 自带并发锁与 R2 重试）。
    from apitelegramchat.file_handlers import download_file, get_file_path
    from apitelegramchat.workspace_paths import data_root

    tmp_dir = data_root() / "media_proxy_tmp"
    tmp_path = tmp_dir / file_id  # file_id 是 base64url 安全字符，可安全作文件名
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        if not await download_file(file_id, str(tmp_path)):
            logger.warning("[media-proxy] Telegram 回源下载失败: %s", file_id[:16])
            return None
        if not tmp_path.exists():
            return None
        data = tmp_path.read_bytes()
        remote_path = ""
        try:
            remote_path = await get_file_path(file_id) or ""
        except Exception:
            remote_path = ""
        # get_file_path 返回形如 documents/file_1.pdf 的远端路径，扩展名可信。
        return data, guess_content_type(remote_path or file_id)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
