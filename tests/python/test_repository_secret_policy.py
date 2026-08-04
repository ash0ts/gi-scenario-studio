from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_repository_secrets.py"
    spec = importlib.util.spec_from_file_location("repository_secret_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tar_fixture_cannot_hide_a_credential(checker: ModuleType, tmp_path: Path) -> None:
    archive_path = tmp_path / "fixture.tar.gz"
    secret = b"token=" + b"github" + b"_pat_" + b"abcdefghijklmnopqrstuvwxyz1234567890\n"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("evidence/result.json")
        member.size = len(secret)
        archive.addfile(member, io.BytesIO(secret))

    findings = checker.scan_archive(archive_path, "fixture.tar.gz")

    assert any(
        identifier == "fixture.tar.gz!evidence/result.json" and rule == "github-token"
        for identifier, _, rule in findings
    )


def test_tar_fixture_rejects_links_and_unsafe_paths(checker: ModuleType, tmp_path: Path) -> None:
    archive_path = tmp_path / "fixture.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        link = tarfile.TarInfo("evidence/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)
        payload = b"safe\n"
        escaped = tarfile.TarInfo("../outside.txt")
        escaped.size = len(payload)
        archive.addfile(escaped, io.BytesIO(payload))

    findings = checker.scan_archive(archive_path, "fixture.tar.gz")

    assert sum(rule == "unsafe-archive-member" for _, _, rule in findings) == 2


def test_zip_fixture_cannot_hide_private_key_container(checker: ModuleType, tmp_path: Path) -> None:
    archive_path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("credentials/release.pem", "not-even-a-valid-key")

    findings = checker.scan_archive(archive_path, "fixture.zip")

    assert findings == [("fixture.zip!credentials/release.pem", 1, "credential-container")]
