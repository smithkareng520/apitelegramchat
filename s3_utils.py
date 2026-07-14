# s3_utils.py

import asyncio
import logging
from typing import List

import aioboto3
from botocore.exceptions import ClientError

from config import (
    R2_ENDPOINT,
    R2_ACCESS_KEY,
    R2_SECRET_KEY,
    R2_BUCKET_NAME,
    R2_PUBLIC_URL,
    R2_REGION,
)

logger = logging.getLogger(__name__)

session = aioboto3.Session()


async def upload_bytes_to_r2(
    data: bytes,
    key: str,
    content_type: str = "application/octet-stream",
) -> str | None:
    """
    上传字节到 R2。

    返回：
        成功：公开 URL（或预签名 URL）
        失败：None

    自动重试：
        第1次失败 -> 等1秒
        第2次失败 -> 等2秒
        第3次失败 -> 返回None
    """

    if not R2_ENDPOINT or not R2_ACCESS_KEY or not R2_SECRET_KEY:
        logger.error("R2 配置缺失")
        return None

    max_attempts = 3

    for attempt in range(max_attempts):
        try:
            async with session.client(
                "s3",
                endpoint_url=R2_ENDPOINT,
                aws_access_key_id=R2_ACCESS_KEY,
                aws_secret_access_key=R2_SECRET_KEY,
                region_name=R2_REGION,
            ) as s3:

                # R2 默认禁用 ACL，所以不再传 ACL 参数；公开性由 R2 bucket 设置或 presigned URL 决定
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
            logger.exception(
                "R2 上传失败（第 %d/%d 次）：%s",
                attempt + 1,
                max_attempts,
                key,
            )

            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)

    logger.error("R2 上传最终失败：%s", key)
    return None


async def generate_presigned_url(
    key: str,
    expires_in: int = 3600,
) -> str:
    """生成预签名 URL（私有桶）"""

    async with session.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name=R2_REGION,
    ) as s3:

        return await s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET_NAME,
                "Key": key,
            },
            ExpiresIn=expires_in,
        )


async def file_exists_in_r2(key: str) -> bool:
    """检查对象是否存在"""

    if not R2_ENDPOINT or not R2_ACCESS_KEY or not R2_SECRET_KEY:
        return False

    try:
        async with session.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name=R2_REGION,
        ) as s3:

            await s3.head_object(
                Bucket=R2_BUCKET_NAME,
                Key=key,
            )

            return True

    except ClientError as e:
        # botocore 可能返回 "404"、"NotFound" 或 "NoSuchKey" 之一
        err_code = e.response.get("Error", {}).get("Code", "")
        if err_code in ("404", "NotFound", "NoSuchKey"):
            return False

        logger.error(f"检查文件存在失败: {e}")
        return False

    except Exception:
        logger.exception("检查 R2 文件存在异常：%s", key)
        return False


async def download_from_r2(key: str) -> bytes | None:
    """下载对象"""

    if not R2_ENDPOINT or not R2_ACCESS_KEY or not R2_SECRET_KEY:
        return None

    try:
        async with session.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name=R2_REGION,
        ) as s3:

            resp = await s3.get_object(
                Bucket=R2_BUCKET_NAME,
                Key=key,
            )

            return await resp["Body"].read()

    except Exception:
        logger.exception("从 R2 下载失败：%s", key)
        return None


async def list_r2_objects(prefix: str) -> List[str]:
    """列出指定前缀所有对象"""

    if not R2_ENDPOINT or not R2_ACCESS_KEY or not R2_SECRET_KEY:
        return []

    keys = []

    async with session.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name=R2_REGION,
    ) as s3:

        paginator = s3.get_paginator("list_objects_v2")

        async for page in paginator.paginate(
            Bucket=R2_BUCKET_NAME,
            Prefix=prefix,
        ):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])

    return keys


async def delete_r2_object(key: str) -> bool:
    """删除对象"""

    if not R2_ENDPOINT or not R2_ACCESS_KEY or not R2_SECRET_KEY:
        return False

    try:
        async with session.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name=R2_REGION,
        ) as s3:

            await s3.delete_object(
                Bucket=R2_BUCKET_NAME,
                Key=key,
            )

            return True

    except Exception:
        logger.exception("删除 R2 对象失败：%s", key)
        return False
