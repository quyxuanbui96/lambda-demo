from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import boto3
import pytest
from moto import mock_aws
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from src.workflow.api import WorkflowApplication
from src.workflow.auth import DevelopmentAuthenticator
from src.workflow.config import WorkflowSettings
from src.workflow.contracts import ContractRegistry
from src.workflow.database import Database, metadata
from src.workflow.errors import dependency_error
from src.workflow.models import AuthContext, Organization, SourceVersion
from src.workflow.service import WorkflowService
from src.workflow.storage import ObjectStore
from src.workflow.worker import AcquisitionWorker

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

ROOT = Path(__file__).resolve().parents[2]
BUCKET = "test-workflow-bucket"
IDENTITY = AuthContext(
    userId="user-one",
    allowedOrganizations=[Organization(organizationId="org-demo", displayName="Demo")],
)


@dataclass
class FakeDispatcher:
    sent: list[tuple[UUID, UUID]] = field(default_factory=list)
    fail: bool = False

    def send(self, acquisition: UUID, request: UUID) -> None:
        if self.fail:
            raise dependency_error()
        self.sent.append((acquisition, request))


@dataclass
class Clock:
    at: datetime = datetime(2026, 10, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


@dataclass
class Harness:
    service: WorkflowService
    app: WorkflowApplication
    worker: AcquisitionWorker
    client: S3Client
    dispatcher: FakeDispatcher
    auth: DevelopmentAuthenticator
    clock: Clock

    def register(
        self,
        data: bytes | None = None,
        *,
        participant: str = "00001",
        key: str = "workflow/source/example.csv",
    ) -> SourceVersion:
        content = (
            data if data is not None else (ROOT / "examples/workflow/source.demo.csv").read_bytes()
        )
        response = self.client.put_object(Bucket=BUCKET, Key=key, Body=content)
        value = SourceVersion(
            id=uuid4(),
            organizationId="org-demo",
            participantCode=participant,
            insuranceYear=2026,
            businessType="DEMO",
            sourceSystem="OCR",
            bucket=BUCKET,
            key=key,
            versionId=response["VersionId"],
            layoutVersion="demo-source-v1",
            capturedAt=self.clock(),
        )
        with self.service.database.transaction() as repo:
            repo.register_source(value)
        return value


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    with mock_aws():
        client = boto3.client("s3", region_name="ap-northeast-1")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"}
        )
        client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        database = Database.local(f"sqlite+pysqlite:///{tmp_path / 'workflow.sqlite3'}")
        postgres_url = os.environ.get("TEST_WORKFLOW_POSTGRES_URL")
        schema = "wf_test_" + uuid4().hex
        administrative = None
        if postgres_url:
            database.engine.dispose()
            administrative = create_engine(postgres_url)
            with administrative.begin() as connection:
                connection.execute(CreateSchema(schema))
            database = Database(
                create_engine(postgres_url, connect_args={"options": f"-c search_path={schema}"})
            )
        metadata.create_all(database.engine)
        settings = WorkflowSettings(
            stage="local",
            bucket=BUCKET,
            database_url=SecretStr("sqlite+pysqlite:///:memory:"),
            auth_mode="bypass",
            dev_identity=IDENTITY,
            contracts_path=ROOT / "examples/workflow/contracts.demo.json",
        )
        clock, dispatcher = Clock(), FakeDispatcher()
        auth = DevelopmentAuthenticator(IDENTITY)
        service = WorkflowService(
            database,
            ObjectStore(client, settings),
            ContractRegistry.load(settings.contracts_path),
            dispatcher,
            settings,
            clock=clock,
        )
        yield Harness(
            service,
            WorkflowApplication(service, auth),
            AcquisitionWorker(service),
            client,
            dispatcher,
            auth,
            clock,
        )
        database.engine.dispose()
        if administrative is not None:
            with administrative.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            administrative.dispose()
