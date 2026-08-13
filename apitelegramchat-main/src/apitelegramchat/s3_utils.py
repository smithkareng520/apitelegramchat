from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List

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


def _local_public_url(key: str) -> str:
    base = (R2_PUBLIC_URL or "").rstrip("/")
    if base:
        return f"{base}/{key}"
    return f"file://{_safe_local_key_path(key).resolve()}"


def _use_remote_r2() -> bool:
    return bool(aioboto3 and R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET_NAME)


async def upload_bytes_to_r2(
    data: bytes,
    key: str,
    content_type: str = "application/octet-stream",
) -> str | None:
    """Upload bytes to R2, or fall back to a local cache when R2 is unavailable."""
    if not _use_remote_r2():
        try:
            path = _safe_local_key_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            logger.info("Local R2 cache saved: %s", key)
            return _local_public_url(key)
        except Exception as e:
            logger.exception("Local R2 cache write failed: %s", e)
            return None

    max_attempts = 1
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
            if R2_PUBLIC_URL:
                return f"{R2_PUBLIC_URL.rstrip('/')}/{key}"
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
    if not _use_remote_r2():
        return _local_public_url(key)

    async with session.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name=R2_REGION,
        config=_R2_CONFIG,
    ) as s3:
        return await s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": key},
            ExpiresIn=expires_in,
        )


async def file_exists_in_r2(key: str) -> bool:
    if not _use_remote_r2():
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
    if not _use_remote_r2():
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


async def list_r2_objects(prefix: str) -> List[str]:
    if not _use_remote_r2():
        root = _LOCAL_R2_ROOT / prefix
        if not root.exists():
            return []
        items: List[str] = []
        for path in root.rglob("*"):
            if path.is_file():
                rel = path.relative_to(_LOCAL_R2_ROOT).as_posix()
                items.append(rel)
        return items

    keys: List[str] = []
    continuation = None
    while True:
        kwargs = {"Bucket": R2_BUCKET_NAME, "Prefix": prefix}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        async with session.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name=R2_REGION,
            config=_R2_CONFIG,
        ) as s3:
            resp = await s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
    return keys


async def delete_r2_object(key: str) -> bool:
    if not _use_remote_r2():
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
