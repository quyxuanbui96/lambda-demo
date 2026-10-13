"""Version-pinned S3 operations and bounded, streaming CSV verification."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryFile
from typing import TYPE_CHECKING, BinaryIO

from botocore.exceptions import BotoCoreError, ClientError

from src.workflow.config import WorkflowSettings
from src.workflow.contracts import Layout
from src.workflow.errors import WorkflowError, dependency_error
from src.workflow.models import Acquisition, SourceVersion, Transfer

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


# Each reader separately enforces its smaller record budget, including quoted multi-line rows.
csv.field_size_limit(4 * 1024 * 1024)


class BoundedLines:
    def __init__(self, text: io.TextIOWrapper, limit: int) -> None:
        self.text, self.limit, self.record_chars = text, limit, 0

    def __iter__(self) -> BoundedLines:
        return self

    def __next__(self) -> str:
        line = self.text.readline(self.limit + 1)
        if not line:
            raise StopIteration
        self.record_chars += len(line)
        if self.record_chars > self.limit:
            raise WorkflowError("E4003", "CSV record exceeds the configured limit", 422)
        return line


def csv_rows(file: BinaryIO, layout: Layout, max_record_chars: int) -> Iterator[dict[str, str]]:
    text = io.TextIOWrapper(file, encoding=layout.encoding, newline="")
    lines = BoundedLines(text, max_record_chars)
    reader = csv.reader(lines, strict=True)
    try:
        header = next(reader, None)
        lines.record_chars = 0
        if header != layout.columns:
            raise WorkflowError("E4003", "CSV header does not match the registered layout", 422)
        for row in reader:
            lines.record_chars = 0
            if not row:
                continue
            if len(row) != len(layout.columns):
                raise WorkflowError("E4003", "CSV column count does not match the layout", 422)
            value = dict(zip(layout.columns, row, strict=True))
            for column in layout.nonnegative_integer_columns:
                if not value[column].isascii() or not value[column].isdigit():
                    raise WorkflowError(
                        "E4003", "Invalid numeric value for the registered layout", 422
                    )
            yield value
    except UnicodeError, csv.Error:
        raise WorkflowError("E4003", "Malformed CSV or invalid file encoding", 422) from None
    finally:
        # The caller retains ownership of the file/S3 body.
        text.detach()


class ObjectStore:
    def __init__(self, client: S3Client, settings: WorkflowSettings) -> None:
        self.client, self.settings = client, settings

    def _version(self, value: str | None) -> str:
        if not value or value == "null":
            raise WorkflowError("E5001", "Workflow S3 bucket versioning must be enabled", 503)
        return value

    def head(
        self, key: str, *, version: str | None = None
    ) -> tuple[int, str, dict[str, str], datetime] | None:
        try:
            if version:
                result = self.client.head_object(
                    Bucket=self.settings.bucket, Key=key, VersionId=version
                )
            else:
                result = self.client.head_object(Bucket=self.settings.bucket, Key=key)
            return (
                result["ContentLength"],
                self._version(result.get("VersionId")),
                result.get("Metadata", {}),
                result["LastModified"],
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "404",
                "NoSuchKey",
                "NotFound",
                "NoSuchVersion",
            }:
                return None
            raise dependency_error() from None
        except BotoCoreError:
            raise dependency_error() from None

    @contextmanager
    def source(self, value: SourceVersion) -> Iterator[BinaryIO]:
        if value.bucket != self.settings.bucket or not value.key.startswith(
            self.settings.source_prefix
        ):
            raise WorkflowError("E4004", "Source location is outside the registered scope", 422)
        self._version(value.versionId)
        body = None
        try:
            result = self.client.get_object(
                Bucket=value.bucket, Key=value.key, VersionId=value.versionId
            )
            body = result["Body"]
            if result["ContentLength"] > self.settings.max_source_bytes:
                raise WorkflowError("E4002", "Source file exceeds the configured limit", 422)
            # Spool to disk to bound memory and verify actual length even if a stream is truncated.
            with TemporaryFile() as file:
                total = 0
                while chunk := body.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.settings.max_source_bytes:
                        raise WorkflowError(
                            "E4002", "Source file exceeds the configured limit", 422
                        )
                    file.write(chunk)
                if total != result["ContentLength"]:
                    raise dependency_error()
                file.seek(0)
                yield file
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "NoSuchVersion", "404"}:
                raise WorkflowError(
                    "E4004", "Registered source version is unavailable", 422
                ) from None
            raise dependency_error() from None
        except BotoCoreError:
            raise dependency_error() from None
        finally:
            if body is not None:
                body.close()

    def download_url(self, key: str, version: str, at: datetime) -> dict[str, object]:
        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.settings.bucket,
                    "Key": key,
                    "VersionId": self._version(version),
                },
                ExpiresIn=self.settings.url_ttl_seconds,
                HttpMethod="GET",
            )
        except ClientError, BotoCoreError:
            raise dependency_error() from None
        return {
            "downloadUrl": url,
            "method": "GET",
            "expiresAt": (at + timedelta(seconds=self.settings.url_ttl_seconds))
            .astimezone(UTC)
            .isoformat(),
        }

    def upload_url(self, transfer: Transfer, at: datetime) -> dict[str, object]:
        digest = base64.b64encode(bytes.fromhex(transfer.request.contentDigest)).decode()
        metadata = {
            "transfer-id": str(transfer.id),
            "layout-version": transfer.request.layoutVersion,
        }
        headers = {
            "Content-Type": "text/csv",
            "x-amz-checksum-sha256": digest,
            "x-amz-meta-transfer-id": str(transfer.id),
            "x-amz-meta-layout-version": transfer.request.layoutVersion,
            "If-None-Match": "*",
        }
        try:
            url = self.client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.settings.bucket,
                    "Key": transfer.temporaryKey,
                    "ContentType": "text/csv",
                    "ChecksumSHA256": digest,
                    "Metadata": metadata,
                    "IfNoneMatch": "*",
                },
                ExpiresIn=self.settings.url_ttl_seconds,
                HttpMethod="PUT",
            )
        except ClientError, BotoCoreError:
            raise dependency_error() from None
        return {
            "uploadUrl": url,
            "method": "PUT",
            "requiredHeaders": headers,
            "expiresAt": (at + timedelta(seconds=self.settings.url_ttl_seconds)).isoformat(),
        }

    def validate_upload(self, transfer: Transfer, version: str, layout: Layout) -> None:
        head = self.head(transfer.temporaryKey, version=version)
        if head is None:
            raise WorkflowError("E4001", "Upload has not completed", 409)
        length, _, metadata, _ = head
        if length != transfer.request.contentLength:
            raise WorkflowError("E4002", "Uploaded file size differs from the registered size", 422)
        if (
            metadata.get("transfer-id") != str(transfer.id)
            or metadata.get("layout-version") != layout.version
        ):
            raise WorkflowError("E4003", "Uploaded file metadata does not match the transfer", 422)
        body = None
        try:
            result = self.client.get_object(
                Bucket=self.settings.bucket, Key=transfer.temporaryKey, VersionId=version
            )
            body = result["Body"]
            with TemporaryFile() as file:
                digest, total = hashlib.sha256(), 0
                while chunk := body.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.settings.max_file_bytes or total > length:
                        raise WorkflowError(
                            "E4002", "Uploaded file exceeds the registered size", 422
                        )
                    digest.update(chunk)
                    file.write(chunk)
                if total != length:
                    raise dependency_error()
                if digest.hexdigest() != transfer.request.contentDigest:
                    raise WorkflowError("E4005", "Uploaded file digest does not match", 422)
                file.seek(0)
                for _ in csv_rows(file, layout, self.settings.max_record_chars):
                    pass
        except ClientError, BotoCoreError:
            raise dependency_error() from None
        finally:
            if body is not None:
                body.close()

    def existing_output(self, transfer: Transfer, version: str) -> tuple[str, datetime] | None:
        metadata = {
            "transfer-id": str(transfer.id),
            "content-digest": transfer.request.contentDigest,
            "layout-version": transfer.request.layoutVersion,
            "source-version": version,
        }
        existing = self.head(transfer.finalKey)
        if existing is None:
            return None
        if existing[0] != transfer.request.contentLength or existing[2] != metadata:
            raise WorkflowError("E5003", "Final object recovery requires investigation", 503)
        return existing[1], existing[3]

    def copy_output(self, transfer: Transfer, version: str) -> tuple[str, datetime]:
        recovered = self.existing_output(transfer, version)
        if recovered is not None:
            return recovered
        metadata = {
            "transfer-id": str(transfer.id),
            "content-digest": transfer.request.contentDigest,
            "layout-version": transfer.request.layoutVersion,
            "source-version": version,
        }
        try:
            self.client.copy_object(
                Bucket=self.settings.bucket,
                Key=transfer.finalKey,
                CopySource={
                    "Bucket": self.settings.bucket,
                    "Key": transfer.temporaryKey,
                    "VersionId": version,
                },
                Metadata=metadata,
                MetadataDirective="REPLACE",
                ContentType="text/csv",
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                raise dependency_error() from None
        except BotoCoreError:
            raise dependency_error() from None
        recovered = self.existing_output(transfer, version)
        if recovered is None:
            raise dependency_error()
        return recovered

    def publish(
        self, file: BinaryIO, key: str, job: Acquisition, size: int, digest: str, layout: str
    ) -> str:
        expected = {
            "acquisition-id": str(job.id),
            "content-digest": digest,
            "layout-version": layout,
        }
        existing = self.head(key)
        if existing is None:
            file.seek(0)
            try:
                result = self.client.put_object(
                    Bucket=self.settings.bucket,
                    Key=key,
                    Body=file,
                    ContentType="text/csv",
                    Metadata=expected,
                    ContentLength=size,
                    IfNoneMatch="*",
                )
                return self._version(result.get("VersionId"))
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                    raise dependency_error() from None
                existing = self.head(key)
            except BotoCoreError:
                raise dependency_error() from None
        if existing is None or existing[0] != size or existing[2] != expected:
            raise WorkflowError("E5003", "Generated object differs from the reserved output", 503)
        return existing[1]

    def delete_temporary(self, key: str) -> None:
        if not key.startswith(self.settings.temporary_prefix):
            raise ValueError("Cleanup may only delete temporary objects")
        try:
            # Delete every version: a delete marker alone does not release versioned storage.
            paginator = self.client.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=self.settings.bucket, Prefix=key):
                for entry in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
                    if entry["Key"] == key:
                        self.client.delete_object(
                            Bucket=self.settings.bucket, Key=key, VersionId=entry["VersionId"]
                        )
        except ClientError, BotoCoreError:
            raise dependency_error() from None
