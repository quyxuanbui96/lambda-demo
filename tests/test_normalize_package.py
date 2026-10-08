from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

from scripts.normalize_package import normalize_package


def test_windows_dependency_paths_extract_as_linux_package(tmp_path: Path) -> None:
    archive = tmp_path / "lambda.zip"
    with ZipFile(archive, "w") as source:
        info = ZipInfo("dependency/__init__.py")
        info.filename = "dependency\\__init__.py"
        source.writestr(info, "VALUE = 42\n")

    normalize_package(archive)

    with ZipFile(archive) as result:
        assert result.infolist()[0].orig_filename == "dependency/__init__.py"
        assert result.read("dependency/__init__.py") == b"VALUE = 42\n"
        result.extractall(tmp_path / "extracted")
    assert (tmp_path / "extracted/dependency/__init__.py").read_text() == "VALUE = 42\n"


def test_health_package_removes_injected_dependencies(tmp_path: Path) -> None:
    archive = tmp_path / "health.zip"
    health_files = {"src/__init__.py", "src/handlers/__init__.py", "src/handlers/health.py"}
    with ZipFile(archive, "w") as source:
        for name in health_files:
            source.writestr(name, "# health source\n")
        source.writestr("pydantic_core/_pydantic_core.cpython-314-x86_64-linux-gnu.so", b"native")
        source.writestr("boto3/__init__.py", "# dependency\n")

    normalize_package(archive, include=health_files)

    with ZipFile(archive) as result:
        assert set(result.namelist()) == health_files
        assert all(result.read(name) == b"# health source\n" for name in health_files)


def test_missing_health_source_preserves_original_archive(tmp_path: Path) -> None:
    archive = tmp_path / "health.zip"
    with ZipFile(archive, "w") as source:
        source.writestr("boto3/__init__.py", "# dependency\n")
    original = archive.read_bytes()

    with pytest.raises(ValueError, match="Missing required package files"):
        normalize_package(archive, include={"src/handlers/health.py"})

    assert archive.read_bytes() == original
    assert not archive.with_suffix(".normalized.zip").exists()
