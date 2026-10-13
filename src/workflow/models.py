"""Request and persisted models. Public DTOs are built explicitly by the service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)


def utc_timestamp(value: datetime) -> datetime:
    return value.astimezone(UTC)


Timestamp = Annotated[AwareDatetime, AfterValidator(utc_timestamp)]


NonEmpty = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=128)
]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False)


class AcquisitionRequest(Request):
    operationKey: UUID
    organizationId: NonEmpty
    participantCode: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=64)
    ]
    insuranceYear: Annotated[int, Field(strict=True, ge=1900, le=9999)]
    businessType: NonEmpty


class DownloadRequest(Request):
    fileId: UUID


class TransferRequest(Request):
    operationKey: UUID
    acquisitionId: UUID
    outputType: NonEmpty
    layoutVersion: NonEmpty
    contentLength: Annotated[int, Field(strict=True, gt=0)]
    contentDigest: Digest


class EmptyRequest(Request):
    pass


class Organization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    organizationId: NonEmpty
    displayName: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class AuthContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    userId: NonEmpty
    allowedOrganizations: list[Organization] = Field(default_factory=list)

    @field_validator("allowedOrganizations")
    @classmethod
    def distinct_organizations(cls, value: list[Organization]) -> list[Organization]:
        if len({item.organizationId for item in value}) != len(value):
            raise ValueError("Duplicate organization")
        return value


class Persisted(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceVersion(Persisted):
    id: UUID
    organizationId: NonEmpty
    participantCode: str
    insuranceYear: int
    businessType: NonEmpty
    sourceSystem: Literal["OCR", "TISI"]
    bucket: str
    key: str
    versionId: str
    layoutVersion: str
    capturedAt: Timestamp


class Acquisition(Persisted):
    id: UUID
    owner: str
    organizationId: str
    request: AcquisitionRequest
    contract: dict[str, object]
    status: Literal["QUEUED", "RUNNING", "READY", "FAILED"] = "QUEUED"
    version: int = 0
    createdAt: Timestamp
    updatedAt: Timestamp
    attempts: int = 0
    leaseToken: UUID | None = None
    leaseUntil: Timestamp | None = None
    sources: list[SourceVersion] = Field(default_factory=list)
    fileIds: list[UUID] = Field(default_factory=list)
    error: dict[str, object] | None = None


class ManagedFile(Persisted):
    id: UUID
    organizationId: str
    acquisitionId: UUID
    transferId: UUID | None = None
    kind: Literal["GENERATED", "OUTPUT"]
    status: Literal["RESERVED", "AVAILABLE", "FAILED"]
    bucket: str
    key: str
    versionId: str | None = None
    layoutVersion: str
    contentLength: int
    contentDigest: str
    createdAt: Timestamp


class Transfer(Persisted):
    id: UUID
    owner: str
    organizationId: str
    request: TransferRequest
    status: Literal["PREPARED", "TRANSFERRED", "FAILED"] = "PREPARED"
    version: int = 0
    fileId: UUID
    temporaryKey: str
    finalKey: str
    layoutContract: dict[str, object]
    cleanupAt: Timestamp
    cleanedAt: Timestamp | None = None
    uploadExpiresAt: Timestamp | None = None
    inputVersion: str | None = None
    finalVersion: str | None = None
    leaseToken: UUID | None = None
    leaseUntil: Timestamp | None = None
    createdAt: Timestamp
    completedAt: Timestamp | None = None
    error: dict[str, object] | None = None
