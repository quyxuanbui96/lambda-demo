from __future__ import annotations

import pytest

from src.common.config import CsvEncoding
from src.common.exceptions import CsvFormatError
from src.csv_processing.models import SampleRecord
from src.csv_processing.parser import ParsedCsv, parse_csv


def _parse(text: str, encoding: CsvEncoding = "utf-8-sig") -> ParsedCsv[SampleRecord]:
    return parse_csv(text.encode("utf-8"), SampleRecord, encoding=encoding)


def test_valid_csv() -> None:
    parsed = _parse("id,name,quantity\n1,りんご,3\n2,Banana,0\n")
    assert parsed.total_rows == 2
    assert parsed.row_errors == []
    assert parsed.records[0] == SampleRecord(id="1", name="りんご", quantity=3)


def test_header_and_values_are_trimmed() -> None:
    parsed = _parse(" id , name ,quantity\n 1 , Apple ,5\n")
    assert parsed.records[0] == SampleRecord(id="1", name="Apple", quantity=5)


def test_crlf_and_blank_lines_are_accepted() -> None:
    parsed = _parse("id,name,quantity\r\n1,A,1\r\n\r\n2,B,2\r\n")
    assert [r.id for r in parsed.records] == ["1", "2"]


def test_quoted_field_with_comma_and_newline() -> None:
    parsed = _parse('id,name,quantity\n1,"Apple, red\nlarge",1\n')
    assert parsed.records[0].name == "Apple, red\nlarge"


def test_utf8_bom_is_stripped_with_utf8_sig() -> None:
    data = "\ufeffid,name,quantity\n1,A,1\n".encode()
    parsed = parse_csv(data, SampleRecord, encoding="utf-8-sig")
    assert parsed.header == ["id", "name", "quantity"]


def test_utf8_bom_with_plain_utf8_is_rejected_as_unknown_column() -> None:
    data = "\ufeffid,name,quantity\n1,A,1\n".encode()
    with pytest.raises(CsvFormatError) as exc_info:
        parse_csv(data, SampleRecord, encoding="utf-8")
    details = exc_info.value.details
    assert isinstance(details, dict)
    assert "\ufeffid" in details["unknown_columns"]


def test_cp932_encoding() -> None:
    data = "id,name,quantity\n1,商品,2\n".encode("cp932")
    parsed = parse_csv(data, SampleRecord, encoding="cp932")
    assert parsed.records[0].name == "商品"


def test_wrong_encoding_fails_clearly() -> None:
    data = "id,name,quantity\n1,商品,2\n".encode("cp932")
    with pytest.raises(CsvFormatError, match="not valid utf-8-sig"):
        parse_csv(data, SampleRecord, encoding="utf-8-sig")


@pytest.mark.parametrize("data", [b"", b"   \n\n"])
def test_empty_file(data: bytes) -> None:
    with pytest.raises(CsvFormatError, match="empty"):
        parse_csv(data, SampleRecord, encoding="utf-8-sig")


def test_header_only() -> None:
    with pytest.raises(CsvFormatError, match="no data rows"):
        _parse("id,name,quantity\n")


def test_missing_and_unknown_columns() -> None:
    with pytest.raises(CsvFormatError) as exc_info:
        _parse("id,title\n1,A\n")
    details = exc_info.value.details
    assert details == {
        "missing_columns": ["name", "quantity"],
        "unknown_columns": ["title"],
        "expected_columns": ["id", "name", "quantity"],
    }


def test_duplicate_columns() -> None:
    with pytest.raises(CsvFormatError, match="duplicate"):
        _parse("id,name,name,quantity\n1,A,B,1\n")


def test_empty_header_column() -> None:
    with pytest.raises(CsvFormatError, match="empty column name"):
        _parse("id,,quantity\n1,A,1\n")


def test_row_errors_are_collected_with_line_numbers() -> None:
    parsed = _parse("id,name,quantity\n1,A,1\n2,,-1\n3,C,abc\n4,D,4\n")
    assert [r.id for r in parsed.records] == ["1", "4"]
    assert [e.line_number for e in parsed.row_errors] == [3, 4]
    fields_line3 = {err.field for err in parsed.row_errors[0].errors}
    assert fields_line3 == {"name", "quantity"}
    assert parsed.total_rows == 4


def test_column_count_mismatch_is_row_error() -> None:
    parsed = _parse("id,name,quantity\n1,A\n2,B,2,extra\n3,C,3\n")
    assert len(parsed.records) == 1
    assert [e.line_number for e in parsed.row_errors] == [2, 3]
    assert parsed.row_errors[0].errors[0].field == "<row>"


def test_all_rows_invalid_returns_errors_not_exception() -> None:
    parsed = _parse("id,name,quantity\n,,\n")
    assert parsed.records == []
    assert len(parsed.row_errors) == 1


def test_malformed_quoting() -> None:
    with pytest.raises(CsvFormatError, match="Malformed CSV"):
        _parse('id,name,quantity\n1,"A"B,1\n')
