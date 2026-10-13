"""Transactional orchestration for acquisition and transfer APIs."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from src.workflow.config import WorkflowSettings
from src.workflow.contracts import ContractRegistry, Layout
from src.workflow.database import Database, Repository
from src.workflow.errors import WorkflowError, conflict, forbidden, not_found
from src.workflow.models import (
    Acquisition,
    AcquisitionRequest,
    AuthContext,
    ManagedFile,
    Transfer,
    TransferRequest,
)
from src.workflow.storage import ObjectStore
from src.workflow.telemetry import count


def utcnow() -> datetime:
    return datetime.now(UTC)


class Dispatcher(Protocol):
    def send(self, acquisition: UUID, request: UUID) -> None: ...


def fingerprint(value: AcquisitionRequest | TransferRequest) -> str:
    return hashlib.sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def authorize(context: AuthContext, owner: str, organization: str) -> None:
    if context.userId != owner or organization not in {
        o.organizationId for o in context.allowedOrganizations
    }:
        raise forbidden()


def acquisition_dto(job: Acquisition, repo: Repository) -> dict[str, object]:
    result: dict[str, object] = {
        "acquisitionId": str(job.id),
        "status": job.status,
        "createdAt": job.createdAt.isoformat(),
        "updatedAt": job.updatedAt.isoformat(),
    }
    if job.status == "FAILED":
        result["error"] = job.error
    if job.status == "READY":
        result["sourceVersions"] = [
            {
                "fileId": str(s.id),
                "sourceSystem": s.sourceSystem,
                "version": s.versionId,
                "businessYear": s.insuranceYear,
                "capturedAt": s.capturedAt.isoformat(),
            }
            for s in job.sources
        ]
        generated = [repo.file(id) for id in job.fileIds]
        if any(value is None or value.status != "AVAILABLE" for value in generated):
            raise WorkflowError("E5000", "Acquisition file metadata is inconsistent", 503)
        result["files"] = [
            {
                "fileId": str(value.id),
                "layoutVersion": value.layoutVersion,
                "contentLength": value.contentLength,
                "contentDigest": value.contentDigest,
            }
            for value in generated
            if value is not None
        ]
    return result


def transfer_dto(job: Transfer) -> dict[str, object]:
    result: dict[str, object] = {
        "transferId": str(job.id),
        "acquisitionId": str(job.request.acquisitionId),
        "fileId": str(job.fileId),
        "status": job.status,
        "createdAt": job.createdAt.isoformat(),
        "completedAt": job.completedAt.isoformat() if job.completedAt else None,
    }
    if job.status == "FAILED":
        result["error"] = job.error
    return result


class WorkflowService:
    def __init__(
        self,
        database: Database,
        objects: ObjectStore,
        contracts: ContractRegistry,
        dispatcher: Dispatcher,
        settings: WorkflowSettings,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.database, self.objects, self.contracts = database, objects, contracts
        self.dispatcher, self.settings, self.clock = dispatcher, settings, clock

    def _acquisition(
        self, repo: Repository, id: UUID, context: AuthContext, *, lock: bool = False
    ) -> Acquisition:
        job = repo.acquisition(id, lock=lock)
        if job is None:
            raise not_found()
        authorize(context, job.owner, job.organizationId)
        return job

    def _transfer(
        self, repo: Repository, id: UUID, context: AuthContext, *, lock: bool = False
    ) -> Transfer:
        job = repo.transfer(id, lock=lock)
        if job is None:
            raise not_found()
        authorize(context, job.owner, job.organizationId)
        return job

    def create_acquisition(
        self, request: AcquisitionRequest, context: AuthContext, correlation: UUID
    ) -> tuple[int, dict[str, object]]:
        authorize(context, context.userId, request.organizationId)
        contract = self.contracts.acquisition(request.businessType)
        id, at = uuid4(), self.clock()
        with self.database.transaction() as repo:
            id, digest, new = repo.reserve_operation(
                context.userId, "ACQUISITION", request.operationKey, fingerprint(request), id
            )
            if digest != fingerprint(request):
                raise WorkflowError(
                    "E3002", "Operation key was already used for different content", 409
                )
            if new:
                job = Acquisition(
                    id=id,
                    owner=context.userId,
                    organizationId=request.organizationId,
                    request=request,
                    contract=contract.model_dump(mode="json"),
                    createdAt=at,
                    updatedAt=at,
                )
                repo.save_acquisition(job, new=True)
                repo.save_dispatch(
                    id,
                    "PENDING",
                    {"attempts": 0, "requestId": str(correlation), "nextAttemptAt": at.isoformat()},
                    new=True,
                )
                self._audit(
                    repo,
                    job.owner,
                    job.organizationId,
                    id,
                    correlation,
                    "acquisition.create",
                    "QUEUED",
                )
            else:
                job = self._acquisition(repo, id, context)
            result = acquisition_dto(job, repo)
        if new:
            with suppress(WorkflowError):
                self.dispatch(id)
        return (202 if new else 200), result

    def dispatch(self, id: UUID) -> None:
        # Claim dispatch before sending. A crash here is recovered when nextAttemptAt expires.
        at = self.clock()
        with self.database.transaction() as repo:
            job = repo.acquisition(id, lock=True)
            if job is None or job.status != "QUEUED":
                return
            record = repo.dispatch(id)
            if record is None or datetime.fromisoformat(str(record["nextAttemptAt"])) > at:
                return
            record["attempts"] = int(str(record.get("attempts", 0))) + 1
            record["nextAttemptAt"] = (
                at + timedelta(seconds=self.settings.worker_lease_seconds)
            ).isoformat()
            repo.save_dispatch(id, "PENDING", record)
        try:
            self.dispatcher.send(id, UUID(str(record["requestId"])))
        except Exception:
            count("DispatchFailures", self.settings.stage)
            # The already-committed job survives dependency failures and process termination.
            try:
                with self.database.transaction() as repo:
                    record["nextAttemptAt"] = (at + timedelta(seconds=30)).isoformat()
                    repo.save_dispatch(id, "PENDING", record)
            except WorkflowError:
                pass
            return
        try:
            with self.database.transaction() as repo:
                repo.save_dispatch(id, "SENT", record)
        except WorkflowError:
            pass

    def get_acquisition(self, id: UUID, context: AuthContext) -> dict[str, object]:
        with self.database.transaction() as repo:
            return acquisition_dto(self._acquisition(repo, id, context), repo)

    def download(
        self, id: UUID, file: UUID, context: AuthContext, correlation: UUID
    ) -> dict[str, object]:
        with self.database.transaction() as repo:
            job = self._acquisition(repo, id, context)
            if job.status != "READY":
                raise conflict("Acquisition is not ready")
            value = repo.file(file)
            if (
                value is None
                or file not in job.fileIds
                or value.acquisitionId != id
                or value.kind != "GENERATED"
            ):
                raise not_found()
            if value.status != "AVAILABLE" or value.versionId is None:
                raise conflict("Generated file is not available")
            self._audit(
                repo,
                job.owner,
                job.organizationId,
                id,
                correlation,
                "acquisition.download",
                "GRANTED",
            )
        return self.objects.download_url(value.key, value.versionId, self.clock())

    def create_transfer(
        self, request: TransferRequest, context: AuthContext, correlation: UUID
    ) -> tuple[int, dict[str, object]]:
        if request.contentLength > self.settings.max_file_bytes:
            raise WorkflowError("E4002", "File exceeds the configured size limit", 413)
        id, file_id, at = uuid4(), uuid4(), self.clock()
        with self.database.transaction() as repo:
            acquisition = self._acquisition(repo, request.acquisitionId, context, lock=True)
            if acquisition.status != "READY":
                raise conflict("Acquisition is not ready")
            layout = self.contracts.output(
                acquisition.request.businessType, request.outputType, request.layoutVersion
            )
            id, digest, new = repo.reserve_operation(
                context.userId, "TRANSFER", request.operationKey, fingerprint(request), id
            )
            if digest != fingerprint(request):
                raise WorkflowError(
                    "E3002", "Operation key was already used for different content", 409
                )
            if new:
                job = Transfer(
                    id=id,
                    owner=context.userId,
                    organizationId=acquisition.organizationId,
                    request=request,
                    fileId=file_id,
                    temporaryKey=f"{self.settings.temporary_prefix}{id}/{file_id}.csv",
                    finalKey=f"{self.settings.final_prefix}{id}/{file_id}.csv",
                    layoutContract=layout.model_dump(mode="json"),
                    cleanupAt=at + timedelta(seconds=self.settings.temporary_retention_seconds),
                    createdAt=at,
                )
                repo.save_transfer(job, new=True)
                file = ManagedFile(
                    id=file_id,
                    organizationId=job.organizationId,
                    acquisitionId=acquisition.id,
                    transferId=id,
                    kind="OUTPUT",
                    status="RESERVED",
                    bucket=self.settings.bucket,
                    key=job.finalKey,
                    layoutVersion=request.layoutVersion,
                    contentLength=request.contentLength,
                    contentDigest=request.contentDigest,
                    createdAt=at,
                )
                repo.save_file(file, new=True)
                self._audit(
                    repo,
                    job.owner,
                    job.organizationId,
                    id,
                    correlation,
                    "transfer.create",
                    "PREPARED",
                )
            else:
                job = self._transfer(repo, id, context)
            return (201 if new else 200), transfer_dto(job)

    def get_transfer(self, id: UUID, context: AuthContext) -> dict[str, object]:
        with self.database.transaction() as repo:
            return transfer_dto(self._transfer(repo, id, context))

    def upload(self, id: UUID, context: AuthContext, correlation: UUID) -> dict[str, object]:
        at = self.clock()
        with self.database.transaction() as repo:
            job = self._transfer(repo, id, context, lock=True)
            if (
                job.status != "PREPARED"
                or job.inputVersion is not None
                or (job.leaseUntil is not None and job.leaseUntil > at)
            ):
                raise conflict("Transfer cannot accept another upload")
            if at + timedelta(seconds=self.settings.url_ttl_seconds) >= job.cleanupAt:
                raise conflict("Transfer upload reservation has expired")
            result = self.objects.upload_url(job, at)
            job.uploadExpiresAt = at + timedelta(seconds=self.settings.url_ttl_seconds)
            job.version += 1
            repo.save_transfer(job)
            self._audit(
                repo, job.owner, job.organizationId, id, correlation, "transfer.upload", "GRANTED"
            )
            return result

    def complete(self, id: UUID, context: AuthContext, correlation: UUID) -> dict[str, object]:
        at, token = self.clock(), uuid4()
        with self.database.transaction() as repo:
            job = self._transfer(repo, id, context, lock=True)
            if job.status == "TRANSFERRED":
                return transfer_dto(job)
            if job.status != "PREPARED":
                raise conflict("Transfer cannot be completed")
            if job.cleanupAt <= at and (
                job.inputVersion is None
                or self.objects.existing_output(job, job.inputVersion) is None
            ):
                raise conflict("Transfer reservation has expired")
            if job.leaseUntil is not None and job.leaseUntil > at:
                raise conflict("Transfer completion is already running")
            job.leaseToken = token
            job.leaseUntil = at + timedelta(seconds=self.settings.completion_lease_seconds)
            job.version += 1
            repo.save_transfer(job)
        try:
            if job.inputVersion is None:
                head = self.objects.head(job.temporaryKey)
                if head is None:
                    raise WorkflowError("E4001", "Upload has not completed", 409)
                job.inputVersion = head[1]
                with self.database.transaction() as repo:
                    current = self._transfer(repo, id, context, lock=True)
                    self._check_lease(current, token)
                    current.inputVersion = job.inputVersion
                    current.version += 1
                    repo.save_transfer(current)
            if self.objects.existing_output(job, job.inputVersion) is None:
                self.objects.validate_upload(
                    job, job.inputVersion, Layout.model_validate(job.layoutContract)
                )
            final_version, completed_at = self.objects.copy_output(job, job.inputVersion)
            # If this commit fails, the next complete finds the final object and reuses its version.
            with self.database.transaction() as repo:
                current = self._transfer(repo, id, context, lock=True)
                self._check_lease(current, token)
                value = repo.file(current.fileId)
                if value is None:
                    raise WorkflowError("E5000", "Transfer file metadata is inconsistent", 503)
                current.finalVersion, current.status = final_version, "TRANSFERRED"
                current.completedAt = completed_at
                current.leaseToken, current.leaseUntil = None, None
                current.version += 1
                value.versionId, value.status = final_version, "AVAILABLE"
                repo.save_file(value)
                repo.save_transfer(current)
                self._audit(
                    repo,
                    current.owner,
                    current.organizationId,
                    id,
                    correlation,
                    "transfer.complete",
                    "TRANSFERRED",
                )
                result = transfer_dto(current)
            count("TransfersCompleted", self.settings.stage)
            return result
        except WorkflowError as error:
            self._release_completion(id, token, error, correlation)
            raise
        except Exception:
            unexpected = WorkflowError(
                "E5002", "Transfer completion is temporarily unavailable", 503
            )
            self._release_completion(id, token, unexpected, correlation)
            raise unexpected from None

    def _check_lease(self, job: Transfer, token: UUID) -> None:
        if job.leaseToken != token or job.leaseUntil is None or job.leaseUntil <= self.clock():
            raise conflict("Transfer completion lease expired")

    def _release_completion(
        self, id: UUID, token: UUID, error: WorkflowError, request: UUID
    ) -> None:
        try:
            with self.database.transaction() as repo:
                job = repo.transfer(id, lock=True)
                if job is None or job.leaseToken != token:
                    return
                if error.code in {"E4002", "E4003", "E4005"}:
                    job.status, job.error = "FAILED", error.public()
                    value = repo.file(job.fileId)
                    if value is not None:
                        value.status = "FAILED"
                        repo.save_file(value)
                    self._audit(
                        repo,
                        job.owner,
                        job.organizationId,
                        id,
                        request,
                        "transfer.complete",
                        "FAILED",
                    )
                job.leaseToken, job.leaseUntil = None, None
                job.version += 1
                repo.save_transfer(job)
            if job.status == "FAILED":
                count("TransferFailures", self.settings.stage)
        except WorkflowError:
            # If DB is unavailable, its durable lease expires and a retry can reconcile S3.
            pass

    def _audit(
        self,
        repo: Repository,
        owner: str,
        organization: str,
        resource: UUID,
        request: UUID,
        action: str,
        result: str,
    ) -> None:
        logging.getLogger("workflow").info(
            json.dumps(
                {
                    "action": action,
                    "userId": owner,
                    "organizationId": organization,
                    "resourceId": str(resource),
                    "requestId": str(request),
                    "result": result,
                }
            )
        )
        repo.audit(
            id=uuid4(),
            at=self.clock(),
            action=action,
            user=owner,
            organization=organization,
            resource=resource,
            request=request,
            result=result,
        )
