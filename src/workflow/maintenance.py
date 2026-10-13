"""Recover durable dispatches/stale leases and clean expired upload reservations."""

from __future__ import annotations

from uuid import uuid4

from src.workflow.models import Transfer
from src.workflow.service import WorkflowService
from src.workflow.telemetry import count


class Maintenance:
    def __init__(self, service: WorkflowService) -> None:
        self.service = service

    def recover(self) -> dict[str, int]:
        service, recovered = self.service, 0
        with service.database.transaction() as repo:
            candidates = repo.pending_acquisitions(service.clock())
        for job in candidates:
            with service.database.transaction() as repo:
                current = repo.acquisition(job.id, lock=True)
                if current is None or current.status in {"READY", "FAILED"}:
                    continue
                if current.status == "RUNNING":
                    if current.leaseUntil is not None and current.leaseUntil > service.clock():
                        continue
                    current.status = "QUEUED"
                    current.leaseToken, current.leaseUntil = None, None
                    current.version += 1
                    current.updatedAt = service.clock()
                    repo.save_acquisition(current)
                    record = repo.dispatch(current.id)
                    if record:
                        record["nextAttemptAt"] = service.clock().isoformat()
                        repo.save_dispatch(current.id, "PENDING", record)
            service.dispatch(job.id)
            recovered += 1
        if recovered:
            count("StaleJobsRecovered", service.settings.stage, recovered)
        return {"recovered": recovered}

    def cleanup(self) -> dict[str, int]:
        service, cleaned = self.service, 0
        with service.database.transaction() as repo:
            candidates = repo.expired_transfers(service.clock())
        for candidate in candidates:
            with service.database.transaction() as repo:
                job = repo.transfer(candidate.id, lock=True)
                if job is None or not self._expired(job):
                    continue
                recovered = (
                    service.objects.existing_output(job, job.inputVersion)
                    if job.status == "PREPARED" and job.inputVersion is not None
                    else None
                )
                if recovered is not None:
                    value = repo.file(job.fileId)
                    if value is None:
                        raise ValueError("Missing reserved output metadata")
                    job.finalVersion, job.completedAt = recovered
                    job.status = "TRANSFERRED"
                    job.leaseToken, job.leaseUntil = None, None
                    value.status, value.versionId = "AVAILABLE", job.finalVersion
                    repo.save_file(value)
                    service._audit(
                        repo,
                        job.owner,
                        job.organizationId,
                        job.id,
                        uuid4(),
                        "transfer.recover",
                        "TRANSFERRED",
                    )
                # Lock the row while deleting: complete/URL issuance cannot race cleanup.
                service.objects.delete_temporary(job.temporaryKey)
                if job.status == "PREPARED":
                    job.status = "FAILED"
                    job.error = {"code": "E3004", "message": "Upload reservation expired"}
                    file = repo.file(job.fileId)
                    if file is not None:
                        file.status = "FAILED"
                        repo.save_file(file)
                job.cleanedAt = service.clock()
                job.version += 1
                repo.save_transfer(job)
                cleaned += 1
        return {"cleaned": cleaned}

    def _expired(self, job: Transfer) -> bool:
        now = self.service.clock()
        return (
            job.cleanedAt is None
            and job.cleanupAt <= now
            and (job.leaseUntil is None or job.leaseUntil <= now)
            and (job.uploadExpiresAt is None or job.uploadExpiresAt <= now)
        )
