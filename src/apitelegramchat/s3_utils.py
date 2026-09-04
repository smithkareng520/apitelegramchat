from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

try:
    import aioboto3  # type: ignore
    from botocore.exceptions import ClientError  # type: ignore
    from botocore.config import Config  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    aioboto3 = None
    ClientError = Exception
    Config = None

from apitelegramchat.config import (
    R2_ENDPOINT,
    R2_ACCESS_KEY,
    R2_SECRET_KEY,
    R2_BUCKET_NAME,
    R2_PUBLIC_URL,
    R2_REGION,
)
from apitelegramchat.workspace_paths import data_root

logger = logging.getLogger(__name__)

try:
    from cachetools import TTLCache
except Exception:  # pragma: no cover - cachetools 是硬依赖，仅为防御性回退
    TTLCache = None  # type: ignore

# =====================================================================
# 预签名 URL 记忆化（prompt cache 关键路径）
# ---------------------------------------------------------------------
# 预签名 URL 含签名时间戳（X-Amz-Date / X-Amz-Expires），每次重签都是
# 不同的字符串。若每次解析附件都重新签名，历史消息里的多模态 content
# 块（image_url / video_url）字节会变，直接打碎 LLM 的前缀缓存——
# 从第一条含附件 URL 的历史消息起，后面的全部内容都要重新计费/计算。
# 这里把同一 key 的预签名 URL 缓存到过期前 5 分钟，窗口内字节级稳定，
# 同时也避免了每轮重复签名的开销。
# =====================================================================
_PRESIGN_DEFAULT_EXPIRES = 3600
_PRESIGN_SAFETY_MARGIN = 300  # 提前 5 分钟失效，避免返回临期/过期 URL
_presigned_url_cache = TTLCache(maxsize=512, ttl=_PRESIGN_DEFAULT_EXPIRES - _PRESIGN_SAFETY_MARGIN) if TTLCache is not None else None
_presign_lock = asyncio.Lock()


session = aioboto3.Session() if aioboto3 is not None else None
_LOCAL_R2_ROOT = data_root() / "r2_cache"

# R2 超时配置：connect 3s，read 5s，0 次重试（1 次尝试，失败即放弃）。
# 默认 botocore 配置是 connect 60s / read 60s / 3 retries，冷启动时一次
# 挂掉的 R2 调用会卡 60s+60s*3 = 240s。这里把每次调用限制在 3+5=8s 内，
# 配合 init 的 30s 全局超时，确保 init 最多跑 30s 就放弃。
_R2_CONFIG = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={"max_attempts": 0, "mode": "standard"},
    max_pool_connections=10,
) if Config is not None else None


def _safe_local_key_path(key: str) -> Path:
    rel = Path(str(key).replace("\\", "/"))
    parts = []
    for part in rel.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError(f"unsafe R2 key: {key!r}")
        parts.append(part)
    return _LOCAL_R2_ROOT.joinpath(*parts)


def _public_delivery_base_url() -> str | None:
    """返回可由 Telegram 等外部抓取器访问的公开媒体基地址。

    ``<account>.r2.cloudflarestorage.com`` 是 R2 的 S3 API 端点，不是公开
    下载域名；不带签名直接拼接对象 key 会得到 AccessDenied，继而导致 Telegram
    返回 RICH_MESSAGE_VIDEO_INVALID 或 RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND。
    遇到该端点（或无效 URL）时应使用预签名 URL。真正可公开访问的 r2.dev
    域名和自定义域名则保留为无查询参数的稳定媒体 URL。
    """
    base = (R2_PUBLIC_URL or "").strip().rstrip("/")
    if not base:
        return None

    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or parsed.query or parsed.fragment:
        logger.warning("忽略无效 R2_PUBLIC_URL，改用预签名 URL: %r", base[:160])
        return None
    if host.endswith(".r2.cloudflarestorage.com"):
        logger.warning(
            "R2_PUBLIC_URL 指向私有 S3 API 端点（%s），改用预签名 URL 供 Telegram 抓取",
            host,
        )
        return None
    return base


def _local_public_url(key: str) -> str:
    base = _public_delivery_base_url()
    if base:
        return f"{base}/{key}"
    return f"file://{_safe_local_key_path(key).resolve()}"


def is_r2_configured() -> bool:
    """是否配置了远程 R2（含 endpoint / access key / secret / bucket）。

    公开化：附件层需要据此决定走 R2 公开 URL 路径还是降级 base64，
    并据此早退避免"拉字节→写本地 file://→发现不可公开访问→降级"的
    无谓链路。
    """
    return bool(aioboto3 and R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET_NAME)


async def upload_bytes_to_r2(
    data: bytes,
    key: str,
    content_type: str = "application/octet-stream",
) -> str | None:
    """Upload bytes to R2, or fall back to a local cache when R2 is unavailable."""
    if not is_r2_configured():
        try:
            path = _safe_local_key_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            logger.info("Local R2 cache saved: %s", key)
            return _local_public_url(key)
        except Exception:
            # logger.exception 自带 traceback，不必再传 e。
            logger.exception("Local R2 cache write failed")
            return None

    # 修复 BUG：max_attempts=1 让 for 循环只跑一次，下面的重试分支
    # （if attempt < max_attempts - 1）永远进不去。要么改成 >1 的实际重试
    # 次数，要么删掉循环结构。这里改成 3 次重试 + 指数退避，让短暂
    # 网络/服务端抖动有自愈机会。
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            async with session.client(
                "s3",
                endpoint_url=R2_ENDPOINT,
                aws_access_key_id=R2_ACCESS_KEY,
                aws_secret_access_key=R2_SECRET_KEY,
                region_name=R2_REGION,
                config=_R2_CONFIG,
            ) as s3:
                await s3.put_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                )
            logger.info("R2 上传成功：%s", key)
            public_base = _public_delivery_base_url()
            if public_base:
                return f"{public_base}/{key}"
            # R2 S3 API endpoint 并非公开 URL。使用预签名 URL，使 Telegram 的
            # 媒体抓取器无需 R2 凭据也能读取刚上传的视频；调用方会在 HTML 属性
            # 中将查询参数的 & 幂等转义为 &amp;。
            return await generate_presigned_url(key)
        except Exception:
            logger.exception("R2 上传失败（第 %d/%d 次）：%s", attempt + 1, max_attempts, key)
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)

    logger.error("R2 上传最终失败：%s", key)
    return None


async def generate_presigned_url(
    key: str,
    expires_in: int = 3600,
) -> str:
    if not is_r2_configured():
        return _local_public_url(key)

    # 仅对默认 1h 有效期做记忆化：TTLCache 的 ttl 是 cache 级参数，
    # 自定义 expires_in 走原路径直接签名。TTLCache 不可用时禁用记忆化，
    # 避免无过期时间的普通 dict 越积越多。
    memoizable = expires_in == _PRESIGN_DEFAULT_EXPIRES and TTLCache is not None
    if memoizable:
        cached_url = _presigned_url_cache.get(key)
        if cached_url:
            return cached_url

    async with _presign_lock:
        if memoizable:
            # double-check：等锁期间可能已有并发请求完成签名
            cached_url = _presigned_url_cache.get(key)
            if cached_url:
                return cached_url
        async with session.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name=R2_REGION,
            config=_R2_CONFIG,
        ) as s3:
            url = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": R2_BUCKET_NAME, "Key": key},
                ExpiresIn=expires_in,
            )
        if memoizable and url:
            _presigned_url_cache[key] = url
        return url


async def public_url_for_existing_key(key: str) -> str | None:
    """Return a publicly-accessible URL for an object that's already in R2.

    Used by vision flows (e.g. Agnes, which rejects base64 image_url) to
    satisfy the "publicly accessible image_url" requirement without
    re-uploading the same bytes every turn.

    Resolution order:
      1. If R2_PUBLIC_URL is configured (custom domain or r2.dev):
         return ``{R2_PUBLIC_URL}/{key}`` — no signature, no expiry.
      2. Otherwise, if R2 is configured remotely: return a presigned URL
         (default 1h expiry). The URL is publicly fetchable but expires;
         long-running sessions will re-issue a fresh one on the next turn.
      3. R2 is not configured at all (local-cache fallback): return None.
         ``file://`` URLs aren't publicly reachable, so the vision caller
         must fall back to base64 (or skip the image entirely).
    """
    if not is_r2_configured():
        # Local cache: file:// URLs aren't publicly accessible, so signal
        # the caller to fall back to base64.
        return None

    base = _public_delivery_base_url()
    if base:
        return f"{base}/{key}"
    # No public delivery URL — issue a presigned URL instead.
    try:
        return await generate_presigned_url(key)
    except Exception as e:
        logger.warning("public_url_for_existing_key presign 失败 %s: %s", key, e)
        return None


async def file_exists_in_r2(key: str) -> bool:
    if not is_r2_configured():
        return _safe_local_key_path(key).exists()

    try:
        async with session.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name=R2_REGION,
            config=_R2_CONFIG,
        ) as s3:
            await s3.head_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except ClientError as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        logger.warning("R2 head_object error: %s", e)
        return False
    except Exception as e:
        logger.warning("R2 head_object failed: %s", e)
        return False


async def download_from_r2(key: str) -> bytes | None:
    if not is_r2_configured():
        path = _safe_local_key_path(key)
        if path.exists() and path.is_file():
            try:
                return path.read_bytes()
            except Exception as e:
                logger.warning("Local R2 cache read failed: %s", e)
        return None

    try:
        async with session.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name=R2_REGION,
            config=_R2_CONFIG,
        ) as s3:
            resp = await s3.get_object(Bucket=R2_BUCKET_NAME, Key=key)
            return await resp["Body"].read()
    except Exception as e:
        logger.warning("R2 download failed: %s", e)
        return None


async def delete_r2_object(key: str) -> bool:
    if not is_r2_configured():
        path = _safe_local_key_path(key)
        try:
            if path.exists():
                path.unlink()
            return True
        except Exception as e:
            logger.warning("Local R2 cache delete failed: %s", e)
            return False

    try:
        async with session.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name=R2_REGION,
            config=_R2_CONFIG,
        ) as s3:
            await s3.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except Exception as e:
        logger.warning("R2 delete failed: %s", e)
        return False
