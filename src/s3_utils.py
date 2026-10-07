from __future__ import annotations

import asyncio
import logging
from pathlib import Path

try:
    import aioboto3
    from botocore.exceptions import ClientError
    from botocore.config import Config
except Exception:  # pragma: no cover - optional dependency fallback
    aioboto3 = None
    ClientError = Exception
    Config = None

from config import (
    R2_ENDPOINT,
    R2_ACCESS_KEY,
    R2_SECRET_KEY,
    R2_BUCKET_NAME,
    R2_REGION,
)
from workspace_paths import data_root

logger = logging.getLogger(__name__)

try:
    from cachetools import TTLCache
except Exception:  # pragma: no cover - cachetools 是硬依赖，仅为防御性回退
    TTLCache = None

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
# 预签名有效期：24 小时。媒体输入 URL 与对外交付 URL（生成结果、文件
# 下载等 Telegram 渲染）全部由预签名承担，长有效期同时拉长历史消息
# content 块的字节稳定窗口（LLM 前缀缓存友好）与交付链接的可抓取窗口。
# R2 SigV4 预签名的硬上限是 7 天，如需更长可调到 604800。
_PRESIGN_DEFAULT_EXPIRES = 86400
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


def _local_file_url(key: str) -> str:
    """本地缓存模式的对象地址（file://）。

    仅作日志/调试用途：``file://`` 地址既不能交给模型（媒体输入），
    也不能被 Telegram 抓取（对外交付）。调用方按各自协议降级
    （base64 内联 / 文本占位 / 本地文件直读）。
    """
    return f"file://{_safe_local_key_path(key).resolve()}"


def is_r2_configured() -> bool:
    """是否配置了远程 R2（含 endpoint / access key / secret / bucket）。

    公开化：附件层需要据此决定走 R2 预签名 URL 路径还是降级 base64，
    并据此早退避免"拉字节→写本地 file://→发现不可交付→降级"的
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
            return _local_file_url(key)
        except Exception:
            # logger.exception 自带 traceback，不必再传 e。
            logger.exception("Local R2 cache write failed")
            return None

    # is_r2_configured() 为真 ⇒ aioboto3 已加载 ⇒ 模块级 session 必非 None
    # （mypy 无法跨函数沿 is_r2_configured 收窄，这里显式声明该既有不变量）。
    assert session is not None
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
            # R2 S3 API endpoint 并非公开 URL，带签名才能匿名读取。对外交付
            #（生成结果、文件下载等 Telegram 渲染）与媒体输入一样统一返回
            # 预签名 URL，使 Telegram 的媒体抓取器无需 R2 凭据也能读取刚
            # 上传的对象；调用方会在 HTML 属性中将查询参数的 & 幂等转义为
            # &amp;。不依赖任何公开域名配置。
            return await generate_presigned_url(key)
        except Exception:
            logger.exception("R2 上传失败（第 %d/%d 次）：%s", attempt + 1, max_attempts, key)
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)

    logger.error("R2 上传最终失败：%s", key)
    return None


async def generate_presigned_url(
    key: str,
    expires_in: int = _PRESIGN_DEFAULT_EXPIRES,
) -> str:
    if not is_r2_configured():
        return _local_file_url(key)

    # is_r2_configured() 为真 ⇒ session 必非 None（同 upload_bytes_to_r2 的不变量）
    assert session is not None
    # 仅对默认 24h 有效期做记忆化：TTLCache 的 ttl 是 cache 级参数，
    # 自定义 expires_in 走原路径直接签名。TTLCache 不可用时禁用记忆化，
    # 避免无过期时间的普通 dict 越积越多。
    memoizable = expires_in == _PRESIGN_DEFAULT_EXPIRES and TTLCache is not None
    if memoizable:
        # memoizable 为真 ⇒ TTLCache 可用 ⇒ _presigned_url_cache 必已初始化（非 None）
        assert _presigned_url_cache is not None
        cached_url = _presigned_url_cache.get(key)
        if cached_url:
            return cached_url

    async with _presign_lock:
        if memoizable:
            # double-check：等锁期间可能已有并发请求完成签名
            assert _presigned_url_cache is not None
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
            assert _presigned_url_cache is not None
            _presigned_url_cache[key] = url
        return url


async def presigned_url_for_existing_key(key: str) -> str | None:
    """媒体输入统一出口：为已存在于 R2 的对象签发预签名 URL。

    媒体输入（image_url / video_url / document url source）统一走 R2
    预签名 URL，不再区分"公开域名优先 / 预签名兜底"两条路径：

      * 预签名 URL 由 TTLCache 记忆化至过期前 5 分钟（见
        ``generate_presigned_url``），窗口内字节级稳定——历史消息里的
        多模态 content 块不会因重签而变字节，前缀缓存得以保全；
      * 签名访问不依赖任何公开域名配置（无需自定义域 / r2.dev），
        也不把对象内容暴露给无凭证的匿名抓取。

    返回值约定：
      1. R2 已配置：返回预签名 URL（默认 24h 有效，过期前 5 分钟内
         的缓存条目已提前失效，长会话下一轮会自动签发新 URL）。
      2. R2 未配置（本地缓存模式）：返回 None——``file://`` 地址不可
         作为模型输入，调用方按各自协议降级（base64 内联 / 文本占位）。
    """
    if not is_r2_configured():
        return None

    try:
        return await generate_presigned_url(key)
    except Exception as e:
        logger.warning("presigned_url_for_existing_key presign 失败 %s: %s", key, e)
        return None


async def file_exists_in_r2(key: str) -> bool:
    if not is_r2_configured():
        return _safe_local_key_path(key).exists()

    # is_r2_configured() 为真 ⇒ session 必非 None（同 upload_bytes_to_r2 的不变量）
    assert session is not None
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


async def list_r2_objects(prefix: str) -> list[str]:
    """List object keys under ``prefix`` (or the local cache when R2 is unavailable).

    用于目录级持久化（如用户 skills/ 备份）的枚举入口：与既有函数一样，
    R2 未配置时回退本地 r2_cache 目录；远端失败返回空列表（调用方按
    "无备份"处理，绝不抛出阻断主流程）。分页循环覆盖超过单页 1000 条
    的前缀（skills 目录远小于此，仅为完备性保留）。
    """
    clean_prefix = str(prefix or "").strip().strip("/")
    if not clean_prefix:
        return []

    if not is_r2_configured():
        root = _safe_local_key_path(clean_prefix)
        if not root.is_dir():
            return []
        try:
            return sorted(
                p.relative_to(_LOCAL_R2_ROOT).as_posix()
                for p in root.rglob("*")
                if p.is_file()
            )
        except Exception as e:
            logger.warning("Local R2 cache list failed: %s", e)
            return []

    # is_r2_configured() 为真 ⇒ session 必非 None（同 upload_bytes_to_r2 的不变量）
    assert session is not None
    keys: list[str] = []
    try:
        async with session.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name=R2_REGION,
            config=_R2_CONFIG,
        ) as s3:
            token: str | None = None
            while True:
                kwargs: dict = {
                    "Bucket": R2_BUCKET_NAME,
                    "Prefix": f"{clean_prefix}/",
                    "MaxKeys": 1000,
                }
                if token:
                    kwargs["ContinuationToken"] = token
                resp = await s3.list_objects_v2(**kwargs)
                for obj in resp.get("Contents", []) or []:
                    key = obj.get("Key")
                    if key:
                        keys.append(str(key))
                if not resp.get("IsTruncated"):
                    break
                token = resp.get("NextContinuationToken")
                if not token:
                    break
        return keys
    except Exception as e:
        logger.warning("R2 list failed: %s", e)
        return []


async def download_from_r2(key: str) -> bytes | None:
    if not is_r2_configured():
        path = _safe_local_key_path(key)
        if path.exists() and path.is_file():
            try:
                return path.read_bytes()
            except Exception as e:
                logger.warning("Local R2 cache read failed: %s", e)
        return None

    # is_r2_configured() 为真 ⇒ session 必非 None（同 upload_bytes_to_r2 的不变量）
    assert session is not None
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

    # is_r2_configured() 为真 ⇒ session 必非 None（同 upload_bytes_to_r2 的不变量）
    assert session is not None
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
