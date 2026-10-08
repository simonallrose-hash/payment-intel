"""S3-compatible object storage client (MinIO) for artifacts and exports (AS-16)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from payintel.core.errors import StorageError
from payintel.core.settings import S3Settings

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


def make_s3_client(settings: S3Settings) -> S3Client:
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.access_key.get_secret_value(),
        aws_secret_access_key=settings.secret_key.get_secret_value(),
        region_name=settings.region,
        config=Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    )


class ObjectStore:
    """Thin wrapper: put/get/presign with timeouts and uniform errors."""

    def __init__(self, client: S3Client) -> None:
        self._client = client

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self._client.head_bucket(Bucket=bucket)
        except ClientError:
            try:
                self._client.create_bucket(Bucket=bucket)
            except ClientError as exc:
                raise StorageError(f"cannot create bucket {bucket}", bucket=bucket) from exc

    def put_bytes(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        try:
            self._client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        except ClientError as exc:
            raise StorageError("put_object failed", bucket=bucket, key=key) from exc

    def get_bytes(self, bucket: str, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            raise StorageError("get_object failed", bucket=bucket, key=key) from exc
        return response["Body"].read()

    def presigned_get_url(self, bucket: str, key: str, *, expires_seconds: int) -> str:
        """Signed download link with limited lifetime (FR-EX-04: 72 h)."""
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_seconds
        )

    def delete(self, bucket: str, key: str) -> None:
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            raise StorageError("delete_object failed", bucket=bucket, key=key) from exc
