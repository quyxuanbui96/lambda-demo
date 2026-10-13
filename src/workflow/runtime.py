"""AWS adapters and cached connection/runtime factories."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from typing import TYPE_CHECKING, Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL

from src.workflow.api import WorkflowApplication
from src.workflow.auth import DevelopmentAuthenticator, GatewayAuthenticator
from src.workflow.config import WorkflowSettings, load_workflow_settings
from src.workflow.contracts import ContractRegistry
from src.workflow.database import Database
from src.workflow.errors import WorkflowError, dependency_error
from src.workflow.maintenance import Maintenance
from src.workflow.service import WorkflowService
from src.workflow.storage import ObjectStore
from src.workflow.worker import AcquisitionWorker

if TYPE_CHECKING:
    from mypy_boto3_secretsmanager import SecretsManagerClient
    from mypy_boto3_sqs import SQSClient

CLIENT_CONFIG = Config(
    connect_timeout=3, read_timeout=10, retries={"mode": "standard", "max_attempts": 2}
)


class SecretCache:
    def __init__(self, client: SecretsManagerClient, ttl: int) -> None:
        self.client, self.ttl = client, ttl
        self.values: dict[str, tuple[float, dict[str, str]]] = {}
        self.lock = Lock()

    def get(self, arn: str) -> dict[str, str]:
        with self.lock:
            value = self.values.get(arn)
            if value and value[0] > time.monotonic():
                return value[1].copy()
            try:
                result = self.client.get_secret_value(SecretId=arn)
                raw = json.loads(result["SecretString"])
                if not isinstance(raw, dict) or not all(
                    isinstance(k, str) and (isinstance(v, str) or (k == "port" and type(v) is int))
                    for k, v in raw.items()
                ):
                    raise ValueError("Invalid secret")
                payload = {str(k): str(v) for k, v in raw.items()}
                self.values[arn] = (time.monotonic() + self.ttl, payload)
                return payload.copy()
            except ClientError, BotoCoreError, ValueError, KeyError:
                raise dependency_error() from None


class SqsDispatcher:
    def __init__(self, client: SQSClient, queue_url: str | None) -> None:
        self.client, self.queue_url = client, queue_url

    def send(self, acquisition: UUID, request: UUID) -> None:
        if not self.queue_url:
            raise WorkflowError("E5001", "Acquisition queue is not configured", 503)
        try:
            self.client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps(
                    {"acquisitionId": str(acquisition), "requestId": str(request)}
                ),
            )
        except ClientError, BotoCoreError:
            raise dependency_error() from None


def create_database(settings: WorkflowSettings, cache: SecretCache) -> Database:
    url = settings.database_url.get_secret_value() if settings.database_url else None
    if url and url.startswith("sqlite"):
        return Database.local(url)
    ca = settings.database_ssl_root_cert
    if ca is None or not ca.is_file():
        raise WorkflowError("E5001", "PostgreSQL TLS CA certificate is required", 503)
    if settings.database_secret_arn:
        secret = cache.get(settings.database_secret_arn)
        try:
            url_value: str | URL = URL.create(
                "postgresql+psycopg",
                username=secret["username"],
                password=secret["password"],
                host=secret["host"],
                port=int(secret.get("port", "5432")),
                database=secret["dbname"],
            )
        except KeyError, ValueError:
            raise WorkflowError("E5001", "Invalid database secret format", 503) from None
    elif url and url.startswith("postgresql+psycopg://"):
        url_value = url
    else:
        raise WorkflowError("E5001", "PostgreSQL configuration is required", 503)
    engine = create_engine(
        url_value,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=settings.database_connect_timeout,
        pool_recycle=settings.secret_cache_seconds,
        connect_args={
            "connect_timeout": settings.database_connect_timeout,
            "options": (
                f"-c statement_timeout={settings.database_query_timeout * 1000} "
                f"-c lock_timeout={settings.database_query_timeout * 1000}"
            ),
            "sslmode": "verify-full",
            "sslrootcert": str(ca),
        },
    )
    if settings.database_secret_arn:
        arn = settings.database_secret_arn

        @event.listens_for(engine, "do_connect")
        def refresh_credentials(_: Any, __: Any, ___: Any, parameters: dict[str, Any]) -> None:
            current = cache.get(arn)
            parameters["user"], parameters["password"] = current["username"], current["password"]

    return Database(engine)


@dataclass
class Runtime:
    app: WorkflowApplication
    worker: AcquisitionWorker
    maintenance: Maintenance


@lru_cache(maxsize=1)
def get_runtime() -> Runtime:
    settings = load_workflow_settings()
    cache = SecretCache(
        boto3.client("secretsmanager", config=CLIENT_CONFIG), settings.secret_cache_seconds
    )
    database = create_database(settings, cache)
    objects = ObjectStore(
        boto3.client("s3", config=CLIENT_CONFIG.merge(Config(signature_version="s3v4"))), settings
    )
    dispatcher = SqsDispatcher(boto3.client("sqs", config=CLIENT_CONFIG), settings.queue_url)
    service = WorkflowService(
        database, objects, ContractRegistry.load(settings.contracts_path), dispatcher, settings
    )
    authenticator = (
        DevelopmentAuthenticator(settings.dev_identity)
        if settings.auth_mode == "bypass" and settings.dev_identity is not None
        else GatewayAuthenticator()
    )
    return Runtime(
        WorkflowApplication(service, authenticator),
        AcquisitionWorker(service),
        Maintenance(service),
    )
