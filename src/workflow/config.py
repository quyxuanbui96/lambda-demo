"""Explicit workflow configuration; development identity is never enabled in production."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from src.workflow.errors import WorkflowError
from src.workflow.models import AuthContext


class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: str = "dev"
    bucket: str = Field(min_length=3, max_length=63)
    source_prefix: str = "workflow/source/"
    generated_prefix: str = "workflow/generated/"
    temporary_prefix: str = "workflow/temporary/"
    final_prefix: str = "workflow/output/"
    database_url: SecretStr | None = None
    database_secret_arn: str | None = None
    database_ssl_root_cert: Path | None = None
    database_connect_timeout: int = Field(default=5, ge=1, le=30)
    database_query_timeout: int = Field(default=10, ge=1, le=20)
    secret_cache_seconds: int = Field(default=300, ge=1, le=3600)
    auth_mode: Literal["bypass", "authorizer"] = "authorizer"
    dev_identity: AuthContext | None = None
    contracts_path: Path
    queue_url: str | None = None
    url_ttl_seconds: int = Field(default=300, ge=60, le=900)
    max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    max_source_bytes: int = Field(default=100 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)
    max_record_chars: int = Field(default=1024 * 1024, ge=128, le=4 * 1024 * 1024)
    worker_lease_seconds: int = Field(default=420, ge=360, le=900)
    completion_lease_seconds: int = Field(default=45, ge=30, le=120)
    max_worker_attempts: int = Field(default=3, ge=1, le=10)
    temporary_retention_seconds: int = Field(default=86400, ge=3600)
    poll_interval_seconds: int = Field(default=3, ge=1, le=60)

    @model_validator(mode="after")
    def validate_configuration(self) -> WorkflowSettings:
        if not self.database_url and not self.database_secret_arn:
            raise ValueError("Database URL or Secrets Manager ARN is required")
        if (
            self.database_url
            and self.database_url.get_secret_value().startswith("sqlite")
            and self.stage != "local"
        ):
            raise ValueError("SQLite is only supported for local development/tests")
        if self.auth_mode == "bypass" and (
            self.stage not in {"local", "dev"} or self.dev_identity is None
        ):
            raise ValueError("Auth bypass needs an explicit identity and local/dev stage")
        prefixes = [
            self.source_prefix,
            self.generated_prefix,
            self.temporary_prefix,
            self.final_prefix,
        ]
        for prefix in prefixes:
            if not prefix.endswith("/") or prefix.startswith("/") or ".." in prefix.split("/"):
                raise ValueError("Invalid object prefix")
        if (
            any(a.startswith(b) for a in prefixes for b in prefixes if a != b)
            or len(set(prefixes)) != 4
        ):
            raise ValueError("Object prefixes must not overlap")
        if self.temporary_retention_seconds <= self.url_ttl_seconds + self.completion_lease_seconds:
            raise ValueError("Retention must outlive upload URLs and completion leases")
        return self


def load_workflow_settings(environ: Mapping[str, str] | None = None) -> WorkflowSettings:
    env = os.environ if environ is None else environ
    raw: dict[str, object] = {"stage": env.get("STAGE", "dev")}
    for name in WorkflowSettings.model_fields:
        key = "WF_" + name.upper()
        if name != "stage" and key in env:
            raw[name] = env[key]
    if "WF_DEV_IDENTITY" in env:
        try:
            raw["dev_identity"] = AuthContext.model_validate_json(env["WF_DEV_IDENTITY"])
        except ValidationError:
            raise WorkflowError(
                "E5001", "Invalid development identity configuration", 503
            ) from None
    try:
        return WorkflowSettings.model_validate(raw)
    except ValidationError:
        raise WorkflowError(
            "E5001", "Workflow configuration is incomplete or invalid", 503
        ) from None
