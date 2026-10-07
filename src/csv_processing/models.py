"""Data models for CSV rows and import results.

``SampleRecord`` is a SAMPLE schema (id, name, quantity) used to wire the whole pipeline end to
end. Replace it with the real business columns once the CSV specification is fixed.
Column names in the CSV header map to field names, or to ``Field(alias=...)`` when set
(e.g. ``product_name: str = Field(alias="商品名")``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SampleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    quantity: int = Field(ge=0)


class FieldError(BaseModel):
    field: str
    message: str


class RowError(BaseModel):
    # Physical line number in the file (header = line 1) so users can locate the row.
    line_number: int = Field(ge=1)
    errors: list[FieldError] = Field(min_length=1)


class CsvImportSummary(BaseModel):
    bucket: str
    key: str
    total_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    invalid_rows: int = Field(ge=0)
    errors: list[RowError]
    # True when more row errors exist than CSV_MAX_REPORTED_ERRORS allows to return.
    errors_truncated: bool
