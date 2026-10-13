from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from src.workflow.errors import WorkflowError, dependency_error
from src.workflow.maintenance import Maintenance
from src.workflow.models import AcquisitionRequest

from .conftest import BUCKET, IDENTITY, Harness
from .test_workflows import PAYLOAD, create, prepare, put


def test_worker_retry_keeps_original_source_snapshot(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = harness.register()
    request = AcquisitionRequest(
        operationKey=uuid4(),
        organizationId="org-demo",
        participantCode="00001",
        insuranceYear=2026,
        businessType="DEMO",
    )
    _, body = harness.service.create_acquisition(request, IDENTITY, uuid4())
    from uuid import UUID

    id = UUID(body["acquisitionId"])
    publish = harness.service.objects.publish

    def fail(*args: object, **kwargs: object) -> None:
        raise dependency_error()

    monkeypatch.setattr(harness.service.objects, "publish", fail)
    with pytest.raises(WorkflowError):
        harness.worker.run(id, uuid4())
    assert harness.service.get_acquisition(id, IDENTITY)["status"] == "QUEUED"
    harness.clock.at += timedelta(seconds=31)
    harness.register(
        b"organizationId,participantCode,insuranceYear,businessType,id,name,quantity\n"
        b"org-demo,00001,2026,DEMO,9,New data,1\n"
    )
    monkeypatch.setattr(harness.service.objects, "publish", publish)
    harness.worker.run(id, uuid4())
    with harness.service.database.transaction() as repo:
        job = repo.acquisition(id)
        assert job and job.sources[0].versionId == source.versionId
        file = repo.file(job.fileIds[0])
        assert file and file.versionId
    response = harness.client.get_object(Bucket=BUCKET, Key=file.key, VersionId=file.versionId)
    assert b"Apple" in response["Body"].read()
    response["Body"].close()


def test_zero_matching_records_produces_header_only_file(harness: Harness) -> None:
    harness.register(
        b"organizationId,participantCode,insuranceYear,businessType,id,name,quantity\norg-demo,00002,2026,DEMO,1,Other,1\n"
    )
    id = create(harness, ready=False)
    harness.worker.run(id, uuid4())
    with harness.service.database.transaction() as repo:
        job = repo.acquisition(id)
        assert job and job.status == "READY"
        file = repo.file(job.fileIds[0])
        assert file and file.versionId
    response = harness.client.get_object(Bucket=BUCKET, Key=file.key, VersionId=file.versionId)
    assert response["Body"].read().decode("utf-8-sig") == "id,name,quantity\r\n"
    response["Body"].close()


def test_stale_worker_lease_is_requeued(harness: Harness) -> None:
    id = create(harness, ready=False)
    with harness.service.database.transaction() as repo:
        job = repo.acquisition(id)
        assert job
        job.status = "RUNNING"
        job.leaseToken = uuid4()
        job.leaseUntil = harness.clock() - timedelta(seconds=1)
        job.version += 1
        repo.save_acquisition(job)
    assert Maintenance(harness.service).recover()["recovered"] == 1
    assert harness.service.get_acquisition(id, IDENTITY)["status"] == "QUEUED"


def test_source_layout_and_worker_retry_limit_fail_cleanly(harness: Harness) -> None:
    harness.register(b"wrong,header\n1,2\n")
    id = create(harness, ready=False)
    harness.worker.run(id, uuid4())
    assert harness.service.get_acquisition(id, IDENTITY)["error"]["code"] == "E4003"
    another = create(harness, ready=False)
    with harness.service.database.transaction() as repo:
        job = repo.acquisition(another)
        assert job
        job.attempts = harness.service.settings.max_worker_attempts
        job.version += 1
        repo.save_acquisition(job)
    harness.worker.run(another, uuid4())
    assert harness.service.get_acquisition(another, IDENTITY)["status"] == "FAILED"


def test_completion_pins_upload_version_across_dependency_retry(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    id = prepare(harness)
    first = put(harness, id)
    validate = harness.service.objects.validate_upload

    def fail(*args: object, **kwargs: object) -> None:
        raise dependency_error()

    monkeypatch.setattr(harness.service.objects, "validate_upload", fail)
    with pytest.raises(WorkflowError):
        harness.service.complete(id, IDENTITY, uuid4())
    put(harness, id, payload=PAYLOAD.replace(b"Apple", b"Grape"))
    monkeypatch.setattr(harness.service.objects, "validate_upload", validate)
    assert harness.service.complete(id, IDENTITY, uuid4())["status"] == "TRANSFERRED"
    with harness.service.database.transaction() as repo:
        job = repo.transfer(id)
        assert job and job.inputVersion == first
        file = repo.file(job.fileId)
        assert file and file.versionId
    response = harness.client.get_object(Bucket=BUCKET, Key=file.key, VersionId=file.versionId)
    assert b"Apple" in response["Body"].read()
    response["Body"].close()


def test_cleanup_marks_expired_prepared_reservation_failed(harness: Harness) -> None:
    id = prepare(harness)
    harness.clock.at += timedelta(days=2)
    assert Maintenance(harness.service).cleanup()["cleaned"] == 1
    assert harness.service.get_transfer(id, IDENTITY)["status"] == "FAILED"


def test_sqs_duplicate_batch_and_poison_message(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from src.handlers import acquisition_worker
    from src.workflow.runtime import Runtime

    id = create(harness, ready=False)
    harness.register()
    monkeypatch.setattr(
        acquisition_worker,
        "get_runtime",
        lambda: Runtime(harness.app, harness.worker, Maintenance(harness.service)),
    )
    event = {
        "Records": [
            {
                "messageId": "one",
                "body": json.dumps({"acquisitionId": str(id), "requestId": str(uuid4())}),
            },
            {"messageId": "invalid", "body": "not-json"},
        ]
    }
    assert acquisition_worker.handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "invalid"}]
    }
    assert harness.service.get_acquisition(id, IDENTITY)["status"] == "READY"
