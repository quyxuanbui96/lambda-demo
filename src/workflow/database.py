"""SQLAlchemy persistence: PostgreSQL in AWS, SQLite only for local tests.

Schema creation is deliberately absent from runtime initialization. Apply migrations explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Connection,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    create_engine,
    event,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.workflow.errors import conflict, dependency_error
from src.workflow.models import Acquisition, ManagedFile, SourceVersion, Transfer

metadata = MetaData()
acquisitions = Table(
    "acquisition_job",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("owner", String(128), nullable=False),
    Column("organization_id", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("version", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("lease_until", String(40)),
    CheckConstraint("status IN ('QUEUED','RUNNING','READY','FAILED')"),
    CheckConstraint("version >= 0"),
)
transfers = Table(
    "transfer_job",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("acquisition_id", String(36), ForeignKey("acquisition_job.id"), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("organization_id", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("version", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("cleanup_at", String(40)),
    CheckConstraint("status IN ('PREPARED','TRANSFERRED','FAILED')"),
    CheckConstraint("version >= 0"),
)
files = Table(
    "managed_file",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("acquisition_id", String(36), ForeignKey("acquisition_job.id"), nullable=False),
    Column("transfer_id", String(36), ForeignKey("transfer_job.id")),
    Column("organization_id", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("payload", JSON, nullable=False),
    CheckConstraint("status IN ('RESERVED','AVAILABLE','FAILED')"),
)
sources = Table(
    "source_file_version",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("organization_id", String(128), nullable=False),
    Column("participant_code", String(64), nullable=False),
    Column("insurance_year", Integer, nullable=False),
    Column("business_type", String(128), nullable=False),
    Column("source_system", String(8), nullable=False),
    Column("bucket", String(63), nullable=False),
    Column("object_key", String(1024), nullable=False),
    Column("version_id", String(256), nullable=False),
    Column("captured_at", String(40), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "organization_id",
        "participant_code",
        "insurance_year",
        "business_type",
        "source_system",
        "bucket",
        "object_key",
        "version_id",
        name="uq_source_version",
    ),
)
operations = Table(
    "operation_request",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("owner", String(128), nullable=False),
    Column("action", String(16), nullable=False),
    Column("operation_key", String(36), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("resource_id", String(36), nullable=False),
    UniqueConstraint("owner", "action", "operation_key", name="uq_operation_key"),
)
dispatches = Table(
    "dispatch_state",
    metadata,
    Column("acquisition_id", String(36), ForeignKey("acquisition_job.id"), primary_key=True),
    Column("status", String(16), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("next_attempt_at", String(40), nullable=False),
    CheckConstraint("status IN ('PENDING','SENT')"),
)
audits = Table(
    "audit_log",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("created_at", String(40), nullable=False),
    Column("action", String(64), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("organization_id", String(128), nullable=False),
    Column("resource_id", String(36), nullable=False),
    Column("request_id", String(36), nullable=False),
    Column("result", String(32), nullable=False),
)
# Composite lookup indexes; no index over the JSON body is needed for normal operations.
Index("ix_acquisition_scope", acquisitions.c.organization_id, acquisitions.c.owner)
Index("ix_transfer_scope", transfers.c.organization_id, transfers.c.owner)
Index(
    "ix_source_scope",
    sources.c.organization_id,
    sources.c.participant_code,
    sources.c.insurance_year,
    sources.c.business_type,
)
Index("ix_dispatch_status", dispatches.c.status)
Index("ix_audit_resource", audits.c.resource_id, audits.c.created_at)


class Database:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def local(cls, url: str) -> Database:
        engine = create_engine(url, connect_args={"timeout": 10}, pool_pre_ping=True)

        @event.listens_for(engine, "connect")
        def configure_sqlite(connection: Any, _: Any) -> None:
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(engine, "begin")
        def begin_sqlite(connection: Connection) -> None:
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        return cls(engine)

    @contextmanager
    def transaction(self) -> Iterator[Repository]:
        try:
            with self.engine.begin() as connection:
                yield Repository(connection)
        except SQLAlchemyError:
            raise dependency_error() from None


class Repository:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def acquisition(self, id: UUID, *, lock: bool = False) -> Acquisition | None:
        statement = select(acquisitions.c.payload).where(acquisitions.c.id == str(id))
        if lock:
            statement = statement.with_for_update()
        raw = self.connection.execute(statement).scalar_one_or_none()
        return None if raw is None else Acquisition.model_validate(raw)

    def transfer(self, id: UUID, *, lock: bool = False) -> Transfer | None:
        statement = select(transfers.c.payload).where(transfers.c.id == str(id))
        if lock:
            statement = statement.with_for_update()
        raw = self.connection.execute(statement).scalar_one_or_none()
        return None if raw is None else Transfer.model_validate(raw)

    def file(self, id: UUID) -> ManagedFile | None:
        raw = self.connection.execute(
            select(files.c.payload).where(files.c.id == str(id))
        ).scalar_one_or_none()
        return None if raw is None else ManagedFile.model_validate(raw)

    def save_acquisition(self, value: Acquisition, *, new: bool = False) -> None:
        values = dict(
            owner=value.owner,
            organization_id=value.organizationId,
            status=value.status,
            version=value.version,
            payload=value.model_dump(mode="json"),
            lease_until=value.leaseUntil.isoformat() if value.leaseUntil else None,
        )
        if new:
            self.connection.execute(insert(acquisitions).values(id=str(value.id), **values))
        else:
            result = self.connection.execute(
                update(acquisitions)
                .where(
                    acquisitions.c.id == str(value.id), acquisitions.c.version == value.version - 1
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise conflict("Acquisition changed concurrently")

    def save_transfer(self, value: Transfer, *, new: bool = False) -> None:
        values = dict(
            acquisition_id=str(value.request.acquisitionId),
            owner=value.owner,
            organization_id=value.organizationId,
            status=value.status,
            version=value.version,
            payload=value.model_dump(mode="json"),
            cleanup_at=None if value.cleanedAt else value.cleanupAt.isoformat(),
        )
        if new:
            self.connection.execute(insert(transfers).values(id=str(value.id), **values))
        else:
            result = self.connection.execute(
                update(transfers)
                .where(transfers.c.id == str(value.id), transfers.c.version == value.version - 1)
                .values(**values)
            )
            if result.rowcount != 1:
                raise conflict("Transfer changed concurrently")

    def save_file(self, value: ManagedFile, *, new: bool = False) -> None:
        values = dict(
            acquisition_id=str(value.acquisitionId),
            transfer_id=str(value.transferId) if value.transferId else None,
            organization_id=value.organizationId,
            status=value.status,
            payload=value.model_dump(mode="json"),
        )
        if new:
            self.connection.execute(insert(files).values(id=str(value.id), **values))
        else:
            self.connection.execute(
                update(files).where(files.c.id == str(value.id)).values(**values)
            )

    def reserve_operation(
        self,
        owner: str,
        action: Literal["ACQUISITION", "TRANSFER"],
        key: UUID,
        digest: str,
        resource: UUID,
    ) -> tuple[UUID, str, bool]:
        # A savepoint keeps PostgreSQL's outer transaction usable after a unique conflict.
        try:
            with self.connection.begin_nested():
                self.connection.execute(
                    insert(operations).values(
                        owner=owner,
                        action=action,
                        operation_key=str(key),
                        payload_hash=digest,
                        resource_id=str(resource),
                    )
                )
            return resource, digest, True
        except IntegrityError:
            row = self.connection.execute(
                select(operations.c.resource_id, operations.c.payload_hash).where(
                    operations.c.owner == owner,
                    operations.c.action == action,
                    operations.c.operation_key == str(key),
                )
            ).one()
            return UUID(row.resource_id), row.payload_hash, False

    def register_source(self, value: SourceVersion) -> None:
        if not value.versionId or value.versionId == "null":
            raise ValueError("Source must have an immutable S3 version")
        self.connection.execute(
            insert(sources).values(
                id=str(value.id),
                organization_id=value.organizationId,
                participant_code=value.participantCode,
                insurance_year=value.insuranceYear,
                business_type=value.businessType,
                source_system=value.sourceSystem,
                bucket=value.bucket,
                object_key=value.key,
                version_id=value.versionId,
                captured_at=value.capturedAt.isoformat(),
                payload=value.model_dump(mode="json"),
            )
        )

    def discover(self, job: Acquisition) -> list[SourceVersion]:
        rows = self.connection.execute(
            select(sources.c.payload)
            .where(
                sources.c.organization_id == job.organizationId,
                sources.c.participant_code == job.request.participantCode,
                sources.c.insurance_year == job.request.insuranceYear,
                sources.c.business_type == job.request.businessType,
            )
            .order_by(sources.c.captured_at.desc(), sources.c.id)
        )
        # Latest version of each logical source key, independent of subsequent registrations.
        selected: dict[tuple[str, str, str], SourceVersion] = {}
        for row in rows:
            value = SourceVersion.model_validate(row.payload)
            selected.setdefault((value.sourceSystem, value.bucket, value.key), value)
        return list(selected.values())

    def dispatch(self, id: UUID) -> dict[str, object] | None:
        raw = self.connection.execute(
            select(dispatches.c.payload).where(dispatches.c.acquisition_id == str(id))
        ).scalar_one_or_none()
        return None if raw is None else dict(raw)

    def save_dispatch(
        self, id: UUID, status: str, payload: dict[str, object], *, new: bool = False
    ) -> None:
        values = dict(status=status, payload=payload, next_attempt_at=payload["nextAttemptAt"])
        if new:
            self.connection.execute(insert(dispatches).values(acquisition_id=str(id), **values))
        else:
            self.connection.execute(
                update(dispatches).where(dispatches.c.acquisition_id == str(id)).values(**values)
            )

    def pending_acquisitions(self, at: datetime, limit: int = 100) -> list[Acquisition]:
        rows = self.connection.execute(
            select(acquisitions.c.payload)
            .join(dispatches)
            .where(
                or_(
                    and_(
                        acquisitions.c.status == "QUEUED",
                        dispatches.c.next_attempt_at <= at.isoformat(),
                    ),
                    and_(
                        acquisitions.c.status == "RUNNING",
                        acquisitions.c.lease_until <= at.isoformat(),
                    ),
                )
            )
            .order_by(dispatches.c.next_attempt_at)
            .limit(limit)
        )
        return [Acquisition.model_validate(row.payload) for row in rows]

    def expired_transfers(self, at: datetime, limit: int = 100) -> list[Transfer]:
        rows = self.connection.execute(
            select(transfers.c.payload)
            .where(transfers.c.cleanup_at <= at.isoformat())
            .order_by(transfers.c.cleanup_at)
            .limit(limit)
        )
        return [Transfer.model_validate(row.payload) for row in rows]

    def audit(
        self,
        *,
        id: UUID,
        at: datetime,
        action: str,
        user: str,
        organization: str,
        resource: UUID,
        request: UUID,
        result: str,
    ) -> None:
        self.connection.execute(
            insert(audits).values(
                id=str(id),
                created_at=at.astimezone(UTC).isoformat(),
                action=action,
                user_id=user,
                organization_id=organization,
                resource_id=str(resource),
                request_id=str(request),
                result=result,
            )
        )
