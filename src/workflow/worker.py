"""Lease-based acquisition worker with immutable source/layout snapshots."""

from __future__ import annotations

import codecs
import csv
import hashlib
from datetime import timedelta
from tempfile import TemporaryFile
from typing import BinaryIO
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.workflow.contracts import BusinessContract
from src.workflow.errors import WorkflowError, conflict
from src.workflow.models import Acquisition, ManagedFile
from src.workflow.service import WorkflowService
from src.workflow.storage import csv_rows
from src.workflow.telemetry import count


class CsvSink:
    def __init__(self, file: BinaryIO, encoding: str, limit: int) -> None:
        self.file, self.limit = file, limit
        self.encoder = codecs.getincrementalencoder(encoding)()
        self.length = 0
        self.digest = hashlib.sha256()

    def write(self, text: str) -> int:
        try:
            data = self.encoder.encode(text)
        except UnicodeError:
            raise WorkflowError(
                "E4003", "Output cannot be represented in the registered encoding", 422
            ) from None
        self.length += len(data)
        if self.length > self.limit:
            raise WorkflowError("E4002", "Generated file exceeds the configured limit", 422)
        self.digest.update(data)
        self.file.write(data)
        return len(text)


class AcquisitionWorker:
    def __init__(self, service: WorkflowService) -> None:
        self.service = service

    def run(self, id: UUID, request: UUID) -> None:
        service, at, token = self.service, self.service.clock(), uuid4()
        with service.database.transaction() as repo:
            job = repo.acquisition(id, lock=True)
            if job is None or job.status in {"READY", "FAILED"}:
                return
            if job.leaseUntil is not None and job.leaseUntil > at:
                return
            if job.attempts >= service.settings.max_worker_attempts:
                job.status = "FAILED"
                job.error = {"code": "E5002", "message": "Acquisition retry limit exceeded"}
                job.leaseToken, job.leaseUntil = None, None
                job.version += 1
                job.updatedAt = at
                repo.save_acquisition(job)
                service._audit(
                    repo, job.owner, job.organizationId, id, request, "acquisition.worker", "FAILED"
                )
                return
            job.status, job.leaseToken = "RUNNING", token
            job.leaseUntil = at + timedelta(seconds=service.settings.worker_lease_seconds)
            job.attempts += 1
            job.version += 1
            job.updatedAt = at
            if not job.sources:
                job.sources = [
                    value.model_copy(update={"capturedAt": at}) for value in repo.discover(job)
                ]
            repo.save_acquisition(job)
        try:
            if not job.sources:
                raise WorkflowError("E4004", "No matching source version is registered", 422)
            contract = BusinessContract.model_validate(job.contract)
            file_id = uuid5(
                NAMESPACE_URL, f"acquisition:{job.id}:generated:{contract.generated_layout.version}"
            )
            key = f"{service.settings.generated_prefix}{job.id}/{file_id}.csv"
            with TemporaryFile() as output:
                sink = CsvSink(
                    output, contract.generated_layout.encoding, service.settings.max_file_bytes
                )
                writer = csv.writer(sink, lineterminator="\r\n")
                writer.writerow(contract.generated_layout.columns)
                for source in job.sources:
                    if source.layoutVersion != contract.source_layout.version:
                        raise WorkflowError(
                            "E4003",
                            "Source layout version is not registered for this acquisition",
                            422,
                        )
                    with service.objects.source(source) as input_file:
                        for row in csv_rows(
                            input_file, contract.source_layout, service.settings.max_record_chars
                        ):
                            if not self._matches(row, job, contract):
                                continue
                            writer.writerow(
                                [row[column] for column in contract.generated_layout.columns]
                            )
                version = service.objects.publish(
                    output,
                    key,
                    job,
                    sink.length,
                    sink.digest.hexdigest(),
                    contract.generated_layout.version,
                )
            file = ManagedFile(
                id=file_id,
                organizationId=job.organizationId,
                acquisitionId=id,
                kind="GENERATED",
                status="AVAILABLE",
                bucket=service.settings.bucket,
                key=key,
                versionId=version,
                layoutVersion=contract.generated_layout.version,
                contentLength=sink.length,
                contentDigest=sink.digest.hexdigest(),
                createdAt=at,
            )
            with service.database.transaction() as repo:
                current = repo.acquisition(id, lock=True)
                if (
                    current is None
                    or current.leaseToken != token
                    or current.leaseUntil is None
                    or current.leaseUntil <= service.clock()
                ):
                    raise conflict("Acquisition lease expired")
                repo.save_file(file, new=True)
                current.status, current.fileIds = "READY", [file_id]
                current.error, current.leaseToken, current.leaseUntil = None, None, None
                current.updatedAt = service.clock()
                current.version += 1
                repo.save_acquisition(current)
                service._audit(
                    repo,
                    current.owner,
                    current.organizationId,
                    id,
                    request,
                    "acquisition.worker",
                    "READY",
                )
            count("AcquisitionsReady", service.settings.stage)
        except WorkflowError as error:
            self._failed(id, token, request, error)
            if error.status >= 500:
                raise
        except Exception:
            unexpected = WorkflowError(
                "E5002", "Acquisition processing is temporarily unavailable", 503
            )
            self._failed(id, token, request, unexpected)
            raise unexpected from None

    @staticmethod
    def _matches(row: dict[str, str], job: Acquisition, contract: BusinessContract) -> bool:
        return (
            row[contract.organization_column] == job.organizationId
            and row[contract.participant_column] == job.request.participantCode
            and row[contract.year_column] == str(job.request.insuranceYear)
            and row[contract.business_column] == job.request.businessType
        )

    def _failed(self, id: UUID, token: UUID, request: UUID, error: WorkflowError) -> None:
        service = self.service
        with service.database.transaction() as repo:
            job = repo.acquisition(id, lock=True)
            if job is None or job.leaseToken != token:
                return
            retry = error.code == "E5002" and job.attempts < service.settings.max_worker_attempts
            job.status = "QUEUED" if retry else "FAILED"
            job.error = None if retry else error.public()
            job.leaseToken, job.leaseUntil = None, None
            job.version += 1
            job.updatedAt = service.clock()
            repo.save_acquisition(job)
            if retry:
                repo.save_dispatch(
                    id,
                    "PENDING",
                    {
                        "requestId": str(request),
                        "attempts": 0,
                        "nextAttemptAt": (
                            service.clock() + timedelta(seconds=30 * job.attempts)
                        ).isoformat(),
                    },
                )
            service._audit(
                repo,
                job.owner,
                job.organizationId,
                id,
                request,
                "acquisition.worker",
                "RETRY" if retry else "FAILED",
            )
        count("AcquisitionRetries" if retry else "AcquisitionFailures", service.settings.stage)
