"""Explicit layout registry. Real OCR/TISI mappings must be registered, not inferred."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.workflow.errors import WorkflowError


class Layout(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = Field(min_length=1)
    columns: list[str] = Field(min_length=1)
    nonnegative_integer_columns: list[str] = Field(default_factory=list)
    encoding: str = "utf-8-sig"

    @model_validator(mode="after")
    def validate_columns(self) -> Layout:
        if len(set(self.columns)) != len(self.columns) or any(not c for c in self.columns):
            raise ValueError("Invalid layout columns")
        if not set(self.nonnegative_integer_columns) <= set(self.columns):
            raise ValueError("Invalid numeric columns")
        if self.encoding not in {"utf-8", "utf-8-sig", "cp932", "shift_jis"}:
            raise ValueError("Unsupported encoding")
        return self


class BusinessContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    business_type: str
    source_layout: Layout
    generated_layout: Layout
    outputs: dict[str, Layout]
    # The source must expose these columns so filtering cannot leak another participant.
    organization_column: str = "organizationId"
    participant_column: str = "participantCode"
    year_column: str = "insuranceYear"
    business_column: str = "businessType"

    @model_validator(mode="after")
    def validate_mapping(self) -> BusinessContract:
        required = {
            self.organization_column,
            self.participant_column,
            self.year_column,
            self.business_column,
        }
        if not required <= set(self.source_layout.columns):
            raise ValueError("Source layout needs all four scope columns")
        if not set(self.generated_layout.columns) <= set(self.source_layout.columns):
            raise ValueError("Generated layout must map registered source columns")
        if not self.outputs:
            raise ValueError("No output layouts registered")
        return self


class ContractRegistry:
    def __init__(self, contracts: list[BusinessContract]) -> None:
        self.contracts = {c.business_type: c for c in contracts}
        if len(self.contracts) != len(contracts):
            raise ValueError("Duplicate business contract")

    @classmethod
    def load(cls, path: Path) -> ContractRegistry:
        try:
            raw = json.loads(path.read_text())
            return cls([BusinessContract.model_validate(value) for value in raw])
        except OSError, ValueError, TypeError:
            raise WorkflowError("E5001", "Invalid business contract configuration", 503) from None

    def acquisition(self, business_type: str) -> BusinessContract:
        value = self.contracts.get(business_type)
        if value is None:
            raise WorkflowError("E1002", "Unregistered business type", 422)
        return value

    def output(self, business_type: str, output_type: str, version: str) -> Layout:
        value = self.acquisition(business_type).outputs.get(output_type)
        if value is None or value.version != version:
            raise WorkflowError("E1002", "Unregistered output type or layout", 422)
        return value
