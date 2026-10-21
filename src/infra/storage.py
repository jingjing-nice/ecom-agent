"""对象存储（MinIO / S3 兼容）：私有桶、短效签名、租户路径。

对象路径一律包含租户与归属者：evidence/{tenant_id}/{owner_id}/{uuid}.{ext}
minio SDK 为同步客户端，异步场景使用 asyncio.to_thread 包装。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from minio import Minio
from minio.error import S3Error

from src.core.config import get_settings
from src.core.exceptions import AppError, PermissionDenied

_client: Minio | None = None

# 上传限制（方案 §5：检查扩展名、真实 MIME、大小、解码安全）
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
PRESIGN_EXPIRE_SECONDS = 600


def get_minio_client() -> Minio:
    """返回 MinIO 客户端单例。"""
    global _client
    if _client is None:
        settings = get_settings()
        _client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
    return _client


def ensure_bucket_exists() -> None:
    """确保私有桶存在（启动时调用）。"""
    settings = get_settings()
    client = get_minio_client()
    if not client.bucket_exists(settings.MINIO_BUCKET):
        client.make_bucket(settings.MINIO_BUCKET)


def build_object_key(tenant_id: str, owner_id: str, file_name: str) -> str:
    """生成含租户标识的对象路径。"""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin"
    return f"evidence/{tenant_id}/{owner_id}/{uuid.uuid4().hex}.{ext}"


async def presign_put(object_key: str) -> str:
    """生成短效上传凭证（PUT）。"""
    settings = get_settings()
    client = Minio(settings.MINIO_PUBLIC_ENDPOINT, access_key=settings.MINIO_ACCESS_KEY,
                   secret_key=settings.MINIO_SECRET_KEY, secure=settings.MINIO_SECURE, region="us-east-1")

    def _presign() -> str:
        return client.presigned_put_object(
            settings.MINIO_BUCKET,
            object_key,
            expires=timedelta(seconds=PRESIGN_EXPIRE_SECONDS),
        )

    return await asyncio.to_thread(_presign)


async def presign_get(object_key: str) -> str:
    """生成短效下载凭证（GET）。"""
    settings = get_settings()
    client = Minio(settings.MINIO_PUBLIC_ENDPOINT, access_key=settings.MINIO_ACCESS_KEY,
                   secret_key=settings.MINIO_SECRET_KEY, secure=settings.MINIO_SECURE, region="us-east-1")

    def _presign() -> str:
        return client.presigned_get_object(
            settings.MINIO_BUCKET,
            object_key,
            expires=timedelta(seconds=PRESIGN_EXPIRE_SECONDS),
        )

    return await asyncio.to_thread(_presign)


async def stat_object(object_key: str):
    """获取对象元信息（校验存在性与大小）。"""
    settings = get_settings()
    client = get_minio_client()

    def _stat():
        return client.stat_object(settings.MINIO_BUCKET, object_key)

    try:
        return await asyncio.to_thread(_stat)
    except S3Error as exc:
        raise AppError(f"对象不存在或不可访问：{object_key}") from exc


async def download_object(object_key: str) -> bytes:
    """下载对象内容（OCR 与校验使用）。"""
    settings = get_settings()
    client = get_minio_client()

    def _download() -> bytes:
        response = client.get_object(settings.MINIO_BUCKET, object_key)
        try:
            data = response.read(MAX_FILE_SIZE_BYTES + 1)
            if len(data) > MAX_FILE_SIZE_BYTES:
                raise AppError("对象超过 10MB 限制")
            return data
        finally:
            response.close()
            response.release_conn()

    try:
        return await asyncio.to_thread(_download)
    except S3Error as exc:
        raise AppError(f"对象读取失败：{object_key}") from exc


async def delete_object(object_key: str) -> None:
    """删除对象（保留与删除策略，方案 §8）。"""
    settings = get_settings()
    client = get_minio_client()

    def _delete() -> None:
        client.remove_object(settings.MINIO_BUCKET, object_key)

    await asyncio.to_thread(_delete)


def ensure_object_ownership(object_key: str, tenant_id: str, owner_id: str) -> None:
    """校验对象归属（上传证据必须验证对象归属，方案 §7）。"""
    expected_prefix = f"evidence/{tenant_id}/{owner_id}/"
    if not object_key.startswith(expected_prefix):
        raise PermissionDenied("对象归属校验失败：非本人或非本租户对象")
