"""Strict CSV parser: bytes -> validated Pydantic records + per-row errors.

File-level problems (encoding, empty file, bad header, malformed quoting) raise
``CsvFormatError``. Row-level problems are collected as ``RowError`` so one bad row does not
hide the others.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from src.common.config import CsvEncoding
from src.common.exceptions import CsvFormatError
from src.common.validation import format_validation_errors
from src.csv_processing.models import FieldError, RowError


@dataclass(frozen=True)
class ParsedCsv[ModelT: BaseModel]:
    header: list[str]
    records: list[ModelT]
    row_errors: list[RowError]

    @property
    def total_rows(self) -> int:
        return len(self.records) + len(self.row_errors)


def parse_csv[ModelT: BaseModel](
    data: bytes, model: type[ModelT], *, encoding: CsvEncoding
) -> ParsedCsv[ModelT]:
    if not data.strip():
        raise CsvFormatError("CSV file is empty")

    text = _decode(data, encoding)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    records: list[ModelT] = []
    row_errors: list[RowError] = []

    try:
        header_row = next(reader, None)
        if header_row is None:
            raise CsvFormatError("CSV header row is missing")
        header = _validate_header(header_row, model)

        for row in reader:
            if not row:  # blank line
                continue
            line_number = reader.line_num
            if len(row) != len(header):
                row_errors.append(
                    RowError(
                        line_number=line_number,
                        errors=[
                            FieldError(
                                field="<row>",
                                message=f"expected {len(header)} columns but got {len(row)}",
                            )
                        ],
                    )
                )
                continue
            try:
                records.append(model.model_validate(dict(zip(header, row, strict=True))))
            except ValidationError as exc:
                row_errors.append(
                    RowError(
                        line_number=line_number,
                        errors=[FieldError(**err) for err in format_validation_errors(exc)],
                    )
                )
    except csv.Error as exc:
        raise CsvFormatError(f"Malformed CSV near line {reader.line_num}: {exc}") from exc

    if not records and not row_errors:
        raise CsvFormatError("CSV has a header but no data rows")

    return ParsedCsv(header=header, records=records, row_errors=row_errors)


def _decode(data: bytes, encoding: CsvEncoding) -> str:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise CsvFormatError(
            f"CSV is not valid {encoding} text", details={"byte_offset": exc.start}
        ) from exc


def _validate_header(raw_header: list[str], model: type[BaseModel]) -> list[str]:
    header = [column.strip() for column in raw_header]
    if any(column == "" for column in header):
        raise CsvFormatError("CSV header contains an empty column name")

    duplicates = sorted({column for column in header if header.count(column) > 1})
    if duplicates:
        raise CsvFormatError(
            "CSV header contains duplicate columns", details={"duplicate_columns": duplicates}
        )

    expected = {(info.alias or name): info for name, info in model.model_fields.items()}
    required = {column for column, info in expected.items() if info.is_required()}
    missing = sorted(required - set(header))
    unknown = sorted(set(header) - set(expected))
    if missing or unknown:
        raise CsvFormatError(
            "CSV header does not match the expected columns",
            details={
                "missing_columns": missing,
                "unknown_columns": unknown,
                "expected_columns": sorted(expected),
            },
        )
    return header
