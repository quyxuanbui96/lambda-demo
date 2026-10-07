"""S3 access. All botocore failures are translated into application errors here."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.common.exceptions import FileTooLargeError, ObjectNotFoundError, StorageError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def create_s3_client() -> S3Client:
    # SigV4 + virtual-hosted style so presigned URLs work for buckets outside us-east-1.
    return boto3.client(
        "s3",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def read_object_bytes(client: S3Client, *, bucket: str, key: str, max_bytes: int) -> bytes:
    location = f"s3://{bucket}/{key}"
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            raise ObjectNotFoundError(f"S3 object not found: {location}") from exc
        raise StorageError(
            f"Failed to read S3 object: {location}", details={"aws_error_code": code}
        ) from exc
    except BotoCoreError as exc:
        raise StorageError(f"Failed to read S3 object: {location}") from exc

    body = response["Body"]
    try:
        size = response["ContentLength"]
        if size > max_bytes:
            raise FileTooLargeError(
                f"CSV file is too large: {location}",
                details={"size_bytes": size, "max_bytes": max_bytes},
            )
        # Read at most max_bytes + 1 so a wrong ContentLength can never blow up memory.
        data = body.read(max_bytes + 1)
    except (BotoCoreError, ClientError) as exc:
        raise StorageError(f"Failed to download S3 object: {location}") from exc
    finally:
        body.close()

    if len(data) > max_bytes:
        raise FileTooLargeError(
            f"CSV file is too large: {location}",
            details={"size_bytes": len(data), "max_bytes": max_bytes},
        )
    return data


def generate_presigned_put_url(
    client: S3Client, *, bucket: str, key: str, content_type: str, expires_in: int
) -> str:
    try:
        return client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("Failed to generate presigned upload URL") from exc
