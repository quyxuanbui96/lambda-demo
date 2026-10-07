from pathlib import Path
from zipfile import ZipFile, ZipInfo

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
