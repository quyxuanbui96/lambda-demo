from __future__ import annotations

import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import boto3
import pytest
from openapi_spec_validator import validate
from pydantic import ValidationError
from sqlalchemy import select

from scripts.workflow_openapi import document
from src.workflow.config import load_workflow_settings
from src.workflow.contracts import BusinessContract, ContractRegistry, Layout
from src.workflow.database import Repository, audits
from src.workflow.errors import WorkflowError
from src.workflow.models import AcquisitionRequest, AuthContext
from src.workflow.runtime import SecretCache, SqsDispatcher, create_database, get_runtime
from src.workflow.storage import csv_rows

from .conftest import IDENTITY, ROOT, Harness
from .test_workflows import create, prepare, put


def test_configuration_and_production_bypass_guard(tmp_path: Path) -> None:
    raw = {
        "STAGE": "local",
        "WF_BUCKET": "example-bucket",
        "WF_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "WF_AUTH_MODE": "bypass",
        "WF_DEV_IDENTITY": IDENTITY.model_dump_json(),
        "WF_CONTRACTS_PATH": str(ROOT / "examples/workflow/contracts.demo.json"),
    }
    settings = load_workflow_settings(raw)
    assert settings.stage == "local" and settings.dev_identity == IDENTITY
    for updates in [
        {"STAGE": "prod"},
        {"WF_URL_TTL_SECONDS": "0"},
        {"WF_DEV_IDENTITY": "bad-json"},
        {"WF_SOURCE_PREFIX": "../source/"},
        {"WF_FINAL_PREFIX": "workflow/temporary/"},
        {"WF_DATABASE_URL": ""},
        {"WF_GENERATED_PREFIX": "/bad"},
        {"WF_TEMPORARY_RETENTION_SECONDS": "100"},
    ]:
        with pytest.raises(WorkflowError):
            load_workflow_settings({**raw, **updates})
    with pytest.raises(WorkflowError):
        create_database(
            settings.model_copy(update={"database_url": None, "database_secret_arn": "secret"}),
            SecretCache(boto3.client("secretsmanager"), 10),
        )


def test_secrets_are_cached_rotated_and_never_exposed(harness: Harness, tmp_path: Path) -> None:
    client = boto3.client("secretsmanager")
    raw = {
        "host": "db.invalid",
        "port": 5432,
        "username": "user",
        "password": "password-one",
        "dbname": "workflow",
    }
    arn = client.create_secret(Name="workflow-db", SecretString=json.dumps(raw))["ARN"]
    cache = SecretCache(client, 300)
    assert cache.get(arn)["port"] == "5432"
    client.put_secret_value(
        SecretId=arn, SecretString=json.dumps({**raw, "password": "password-two"})
    )
    assert cache.get(arn)["password"] == "password-one"
    cache.values[arn] = (0, {})
    assert cache.get(arn)["password"] == "password-two"
    ca = tmp_path / "ca.pem"
    ca.write_text("test-ca")
    settings = harness.service.settings.model_copy(
        update={"database_url": None, "database_secret_arn": arn, "database_ssl_root_cert": ca}
    )
    db = create_database(settings, cache)
    assert db.engine.url.drivername == "postgresql+psycopg" and db.engine.url.host == "db.invalid"
    parameters = {}
    callbacks = db.engine.dialect.dispatch.do_connect
    for callback in callbacks:
        callback(db.engine.dialect, None, [], parameters)
    assert parameters["password"] == "password-two"
    db.engine.dispose()
    client.create_secret(Name="invalid", SecretString='{"password": {"nested": "secret"}}')
    with pytest.raises(WorkflowError) as error:
        cache.get("invalid")
    assert error.value.code == "E5002" and "secret" not in str(error.value)
    with pytest.raises(WorkflowError):
        cache.get("missing")


def test_runtime_initialization_does_not_create_database_schema(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.workflow import runtime

    get_runtime.cache_clear()
    monkeypatch.setattr(runtime, "load_workflow_settings", lambda: harness.service.settings)
    # Initialization creates clients/engine only; an unmigrated local database remains empty.
    context = get_runtime()
    assert context.app.authenticator.authenticate({}).userId == IDENTITY.userId
    with pytest.raises(WorkflowError), context.app.service.database.transaction() as repo:
        repo.acquisition(uuid4())
    context.app.service.database.engine.dispose()
    get_runtime.cache_clear()


def test_sqs_dispatcher_sends_only_job_and_request_ids(harness: Harness) -> None:
    sqs = boto3.client("sqs")
    url = sqs.create_queue(QueueName="workflow-test")["QueueUrl"]
    id, correlation = uuid4(), uuid4()
    SqsDispatcher(sqs, url).send(id, correlation)
    message = sqs.receive_message(QueueUrl=url)["Messages"][0]
    assert json.loads(message["Body"]) == {"acquisitionId": str(id), "requestId": str(correlation)}
    with pytest.raises(WorkflowError):
        SqsDispatcher(sqs, None).send(id, correlation)


def test_concurrent_same_operation_creates_one_job(harness: Harness) -> None:
    request = AcquisitionRequest(
        operationKey=uuid4(),
        organizationId="org-demo",
        participantCode="00001",
        insuranceYear=2026,
        businessType="DEMO",
    )

    def send(_: int) -> tuple[int, dict[str, object]]:
        return harness.service.create_acquisition(request, IDENTITY, uuid4())

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(send, range(6)))
    assert len({body["acquisitionId"] for _, body in responses}) == 1
    assert sorted(status for status, _ in responses) == [200, 200, 200, 200, 200, 202]


def test_optimistic_lock_rejects_stale_update(harness: Harness) -> None:
    id = create(harness, ready=False)
    with harness.service.database.transaction() as repo:
        stale = repo.acquisition(id)
        assert stale
    with harness.service.database.transaction() as repo:
        current = repo.acquisition(id)
        assert current
        current.version += 1
        repo.save_acquisition(current)
    stale.version += 1
    with pytest.raises(WorkflowError), harness.service.database.transaction() as repo:
        repo.save_acquisition(stale)


def test_duplicate_org_and_invalid_layout_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AuthContext(userId="u", allowedOrganizations=IDENTITY.allowedOrganizations * 2)
    for kwargs in [
        dict(columns=["a", "a"]),
        dict(columns=["a"], nonnegative_integer_columns=["b"]),
        dict(columns=["a"], encoding="unknown"),
    ]:
        with pytest.raises(ValidationError):
            Layout(version="v1", **kwargs)
    with pytest.raises(WorkflowError):
        ContractRegistry.load(Path("missing-contract.json"))
    contract = ContractRegistry.load(ROOT / "examples/workflow/contracts.demo.json").acquisition(
        "DEMO"
    )
    with pytest.raises(ValueError):
        ContractRegistry([contract, contract])
    with pytest.raises(WorkflowError):
        ContractRegistry([contract]).acquisition("unknown")
    with pytest.raises(ValidationError):
        BusinessContract.model_validate({**contract.model_dump(), "organization_column": "missing"})


@pytest.mark.parametrize(
    "data",
    [
        b"id,name,quantity\n1,Apple,not-int\n",
        b'id,name,quantity\n1,"unterminated\n',
        b"id,name,quantity\n1,Apple\n",
        b"id,name,quantity\n1,\xff,2\n",
    ],
)
def test_malformed_streaming_csv_is_rejected(data: bytes) -> None:
    layout = Layout(
        version="v1", columns=["id", "name", "quantity"], nonnegative_integer_columns=["quantity"]
    )
    with pytest.raises(WorkflowError):
        list(csv_rows(io.BytesIO(data), layout, 1024))


def test_quoted_multiline_row_is_bounded() -> None:
    data = b'id,name,quantity\n1,"' + b"large\n" * 100 + b'",1\n'
    layout = Layout(version="v1", columns=["id", "name", "quantity"])
    with pytest.raises(WorkflowError) as error:
        list(csv_rows(io.BytesIO(data), layout, 128))
    assert error.value.code == "E4003"


def test_openapi_is_valid_and_contains_exactly_eight_operations() -> None:
    spec = document()
    validate(spec)
    assert sum(len(methods) for methods in spec["paths"].values()) == 8
    assert "/auth/login" not in spec["paths"]
    assert (
        spec["components"]["schemas"]["AcquisitionRequest"]["properties"]["participantCode"]["type"]
        == "string"
    )


def test_base64_request_and_permission_change(harness: Harness) -> None:
    request = {
        "operationKey": str(uuid4()),
        "organizationId": "org-demo",
        "participantCode": "00001",
        "insuranceYear": 2026,
        "businessType": "DEMO",
    }
    result = harness.app.handle(
        {
            "httpMethod": "POST",
            "path": "/acquisitions",
            "body": base64.b64encode(json.dumps(request).encode()).decode(),
            "isBase64Encoded": True,
        }
    )
    assert result["statusCode"] == 202
    harness.auth.context = AuthContext(userId=IDENTITY.userId)
    id = json.loads(result["body"])["acquisitionId"]
    assert (
        harness.app.handle({"httpMethod": "GET", "path": f"/acquisitions/{id}"})["statusCode"]
        == 403
    )


def test_expired_final_copy_is_reconciled_before_cleanup(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    id = prepare(harness)
    put(harness, id)
    save = Repository.save_transfer

    def fail(self: Repository, value: object, *, new: bool = False) -> None:
        if getattr(value, "status", None) == "TRANSFERRED":
            raise WorkflowError("E5002", "DB failed", 503)
        save(self, value, new=new)

    monkeypatch.setattr(Repository, "save_transfer", fail)
    with pytest.raises(WorkflowError):
        harness.service.complete(id, IDENTITY, uuid4())
    monkeypatch.setattr(Repository, "save_transfer", save)
    from src.workflow.maintenance import Maintenance

    harness.clock.at += timedelta(days=2)
    Maintenance(harness.service).cleanup()
    result = harness.service.get_transfer(id, IDENTITY)
    assert result["status"] == "TRANSFERRED" and result["completedAt"]
    with harness.service.database.transaction() as repo:
        assert repo.connection.execute(
            select(audits).where(audits.c.action == "transfer.recover")
        ).one()
