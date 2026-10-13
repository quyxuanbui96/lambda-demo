from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from src.workflow.database import Repository, acquisitions, audits, operations, transfers
from src.workflow.errors import WorkflowError, dependency_error
from src.workflow.maintenance import Maintenance
from src.workflow.models import AcquisitionRequest, AuthContext, Organization, TransferRequest

from .conftest import BUCKET, IDENTITY, Harness


def create(harness: Harness, *, ready: bool = True) -> UUID:
    if ready:
        harness.register()
    request = AcquisitionRequest(
        operationKey=uuid4(),
        organizationId="org-demo",
        participantCode="00001",
        insuranceYear=2026,
        businessType="DEMO",
    )
    status, body = harness.service.create_acquisition(request, IDENTITY, uuid4())
    assert status == 202
    id = UUID(str(body["acquisitionId"]))
    if ready:
        harness.worker.run(id, uuid4())
        assert harness.service.get_acquisition(id, IDENTITY)["status"] == "READY"
    return id


PAYLOAD = b"id,name,quantity\r\n1,Apple,10\r\n2,Orange,5\r\n"


def prepare(harness: Harness, *, payload: bytes = PAYLOAD) -> UUID:
    id = create(harness)
    request = TransferRequest(
        operationKey=uuid4(),
        acquisitionId=id,
        outputType="DEMO_OUTPUT",
        layoutVersion="demo-v1",
        contentLength=len(payload),
        contentDigest=hashlib.sha256(payload).hexdigest(),
    )
    status, body = harness.service.create_transfer(request, IDENTITY, uuid4())
    assert status == 201
    return UUID(str(body["transferId"]))


def put(harness: Harness, id: UUID, *, payload: bytes = PAYLOAD) -> str:
    with harness.service.database.transaction() as repo:
        job = repo.transfer(id)
        assert job
    result = harness.client.put_object(
        Bucket=BUCKET,
        Key=job.temporaryKey,
        Body=payload,
        Metadata={"transfer-id": str(id), "layout-version": "demo-v1"},
    )
    return result["VersionId"]


def test_acquisition_filters_scope_and_exposes_only_generated_file(harness: Harness) -> None:
    id = create(harness)
    with harness.service.database.transaction() as repo:
        job = repo.acquisition(id)
        assert job and len(job.fileIds) == 1
        file = repo.file(job.fileIds[0])
        assert file and file.versionId
    result = harness.client.get_object(Bucket=BUCKET, Key=file.key, VersionId=file.versionId)
    data = result["Body"].read()
    result["Body"].close()
    assert b"Excluded" not in data and b"Apple" in data and b"Orange" in data
    body = harness.service.get_acquisition(id, IDENTITY)
    assert "bucket" not in json.dumps(body) and "workflow/source" not in json.dumps(body)
    response = harness.service.download(id, file.id, IDENTITY, uuid4())
    assert "versionId=" in str(response["downloadUrl"])
    assert "workflow/generated" in str(response["downloadUrl"])
    with pytest.raises(WorkflowError) as error:
        harness.service.download(id, job.sources[0].id, IDENTITY, uuid4())
    assert error.value.status == 404


def test_same_operation_is_idempotent_and_different_content_conflicts(harness: Harness) -> None:
    request = AcquisitionRequest(
        operationKey=uuid4(),
        organizationId="org-demo",
        participantCode="00001",
        insuranceYear=2026,
        businessType="DEMO",
    )
    first = harness.service.create_acquisition(request, IDENTITY, uuid4())
    second = harness.service.create_acquisition(request, IDENTITY, uuid4())
    assert (first[0], second[0]) == (202, 200)
    assert first[1] == second[1] and len(harness.dispatcher.sent) == 1
    with pytest.raises(WorkflowError) as error:
        harness.service.create_acquisition(
            request.model_copy(update={"participantCode": "00002"}), IDENTITY, uuid4()
        )
    assert (error.value.status, error.value.code) == (409, "E3002")
    with harness.service.database.transaction() as repo:
        assert (
            repo.connection.execute(select(func.count()).select_from(acquisitions)).scalar_one()
            == 1
        )


def test_creation_rolls_back_job_operation_and_dispatch_before_any_send(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise dependency_error()

    monkeypatch.setattr(Repository, "audit", fail)
    with pytest.raises(WorkflowError):
        create(harness, ready=False)
    assert not harness.dispatcher.sent
    with harness.service.database.transaction() as repo:
        assert (
            repo.connection.execute(select(func.count()).select_from(acquisitions)).scalar_one()
            == 0
        )
        assert (
            repo.connection.execute(select(func.count()).select_from(operations)).scalar_one() == 0
        )


def test_dispatch_failure_is_durable_and_scheduler_recovers(harness: Harness) -> None:
    harness.dispatcher.fail = True
    id = create(harness, ready=False)
    assert harness.service.get_acquisition(id, IDENTITY)["status"] == "QUEUED"
    harness.clock.at += timedelta(seconds=31)
    harness.dispatcher.fail = False
    assert Maintenance(harness.service).recover()["recovered"] == 1
    assert harness.dispatcher.sent[0][0] == id


def test_missing_source_is_failed_business_status(harness: Harness) -> None:
    id = create(harness, ready=False)
    harness.worker.run(id, uuid4())
    body = harness.service.get_acquisition(id, IDENTITY)
    assert body["status"] == "FAILED" and body["error"]["code"] == "E4004"
    assert "files" not in body and "sourceVersions" not in body


def test_duplicate_worker_does_not_create_duplicate_output(harness: Harness) -> None:
    id = create(harness)
    before = harness.service.get_acquisition(id, IDENTITY)
    harness.worker.run(id, uuid4())
    assert before == harness.service.get_acquisition(id, IDENTITY)
    response = harness.client.list_object_versions(
        Bucket=BUCKET, Prefix=f"workflow/generated/{id}/"
    )
    assert len(response["Versions"]) == 1


def test_full_transfer_and_repeated_complete_are_identical(harness: Harness) -> None:
    id = prepare(harness)
    response = harness.service.upload(id, IDENTITY, uuid4())
    assert response["method"] == "PUT" and response["requiredHeaders"]["If-None-Match"] == "*"
    assert "x-amz-checksum-sha256" in response["requiredHeaders"]
    assert "workflow/temporary" in response["uploadUrl"]
    put(harness, id)
    first = harness.service.complete(id, IDENTITY, uuid4())
    second = harness.service.complete(id, IDENTITY, uuid4())
    assert first == second and first["status"] == "TRANSFERRED" and first["completedAt"]
    with harness.service.database.transaction() as repo:
        job = repo.transfer(id)
        assert job
        file = repo.file(job.fileId)
        assert file and file.status == "AVAILABLE"
        events = repo.connection.execute(
            select(audits).where(audits.c.action == "transfer.complete")
        ).all()
        assert len(events) == 1
    versions = harness.client.list_object_versions(Bucket=BUCKET, Prefix=f"workflow/output/{id}/")
    assert len(versions["Versions"]) == 1
    with pytest.raises(WorkflowError):
        harness.service.upload(id, IDENTITY, uuid4())


@pytest.mark.parametrize("failure", ["size", "digest", "layout", "metadata"])
def test_invalid_upload_fails_and_never_publishes(harness: Harness, failure: str) -> None:
    data = b"wrong,header\n1,2\n" if failure == "layout" else PAYLOAD
    id = prepare(harness, payload=data)
    actual = data + b"extra" if failure == "size" else data
    if failure == "digest":
        actual = actual.replace(b"Apple", b"Grape")
    put(harness, id, payload=actual)
    if failure == "metadata":
        with harness.service.database.transaction() as repo:
            job = repo.transfer(id)
            assert job
        harness.client.put_object(Bucket=BUCKET, Key=job.temporaryKey, Body=data, Metadata={})
    with pytest.raises(WorkflowError) as error:
        harness.service.complete(id, IDENTITY, uuid4())
    assert error.value.status == 422
    result = harness.service.get_transfer(id, IDENTITY)
    assert result["status"] == "FAILED" and result["error"]
    assert not harness.client.list_objects_v2(Bucket=BUCKET, Prefix=f"workflow/output/{id}/").get(
        "Contents"
    )


def test_missing_upload_is_retryable(harness: Harness) -> None:
    id = prepare(harness)
    with pytest.raises(WorkflowError) as error:
        harness.service.complete(id, IDENTITY, uuid4())
    assert error.value.code == "E4001"
    assert harness.service.get_transfer(id, IDENTITY)["status"] == "PREPARED"
    put(harness, id)
    assert harness.service.complete(id, IDENTITY, uuid4())["status"] == "TRANSFERRED"


def test_copy_success_database_failure_reuses_final_version(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    id = prepare(harness)
    put(harness, id)
    original = Repository.save_transfer
    failed = False

    def save(self: Repository, value: object, *, new: bool = False) -> None:
        nonlocal failed
        if getattr(value, "status", None) == "TRANSFERRED" and not failed:
            failed = True
            raise dependency_error()
        original(self, value, new=new)

    monkeypatch.setattr(Repository, "save_transfer", save)
    with pytest.raises(WorkflowError):
        harness.service.complete(id, IDENTITY, uuid4())
    assert harness.service.get_transfer(id, IDENTITY)["status"] == "PREPARED"
    assert harness.service.complete(id, IDENTITY, uuid4())["status"] == "TRANSFERRED"
    versions = harness.client.list_object_versions(Bucket=BUCKET, Prefix=f"workflow/output/{id}/")
    assert len(versions["Versions"]) == 1


def test_permission_revocation_and_other_owner_are_blocked(harness: Harness) -> None:
    id = create(harness)
    for identity in [
        AuthContext(userId=IDENTITY.userId),
        AuthContext(userId="other-user", allowedOrganizations=IDENTITY.allowedOrganizations),
        AuthContext(
            userId=IDENTITY.userId,
            allowedOrganizations=[Organization(organizationId="other", displayName="Other")],
        ),
    ]:
        with pytest.raises(WorkflowError) as error:
            harness.service.get_acquisition(id, identity)
        assert error.value.status == 403


def test_cleanup_removes_all_versions_and_preserves_final_object(harness: Harness) -> None:
    id = prepare(harness)
    put(harness, id)
    harness.service.complete(id, IDENTITY, uuid4())
    harness.clock.at += timedelta(days=2)
    assert Maintenance(harness.service).cleanup()["cleaned"] == 1
    assert Maintenance(harness.service).cleanup()["cleaned"] == 0
    assert not harness.client.list_object_versions(
        Bucket=BUCKET, Prefix=f"workflow/temporary/{id}/"
    ).get("Versions")
    assert (
        len(
            harness.client.list_object_versions(Bucket=BUCKET, Prefix=f"workflow/output/{id}/")[
                "Versions"
            ]
        )
        == 1
    )


def test_transfer_idempotency_and_contract_validation(harness: Harness) -> None:
    id = create(harness)
    request = TransferRequest(
        operationKey=uuid4(),
        acquisitionId=id,
        outputType="DEMO_OUTPUT",
        layoutVersion="demo-v1",
        contentLength=len(PAYLOAD),
        contentDigest=hashlib.sha256(PAYLOAD).hexdigest(),
    )
    first = harness.service.create_transfer(request, IDENTITY, uuid4())
    second = harness.service.create_transfer(request, IDENTITY, uuid4())
    assert (first[0], second[0]) == (201, 200) and first[1] == second[1]
    with pytest.raises(WorkflowError) as error:
        harness.service.create_transfer(
            request.model_copy(update={"contentLength": 1}), IDENTITY, uuid4()
        )
    assert error.value.code == "E3002"
    with pytest.raises(WorkflowError):
        harness.service.create_transfer(
            request.model_copy(update={"layoutVersion": "unknown"}), IDENTITY, uuid4()
        )
    with harness.service.database.transaction() as repo:
        assert (
            repo.connection.execute(select(func.count()).select_from(transfers)).scalar_one() == 1
        )
