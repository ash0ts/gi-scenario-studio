from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path

import pytest
from scenario_platform.archive import (
    ArchiveLimits,
    ArchiveSecurityError,
    extract_zip,
    inspect_zip,
    normalize_archive_member,
)
from scenario_platform.storage import (
    ContentAddressedStore,
    PathSecurityError,
    atomic_write_bytes,
    contained_path,
)


@pytest.mark.parametrize(
    "name",
    ["../escape", "/absolute", "C:\\escape", "folder/../escape", "folder//file"],
)
def test_archive_member_normalization_rejects_unsafe_paths(name: str) -> None:
    with pytest.raises(ArchiveSecurityError):
        normalize_archive_member(name)


def test_archive_is_fully_validated_then_contained(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("model/config.json", "{}")
        handle.writestr("model/grid.tif", b"synthetic")

    members = inspect_zip(archive)
    assert [member.path for member in members] == ["model/config.json", "model/grid.tif"]
    output = tmp_path / "output"
    extracted = extract_zip(archive, output)
    assert {path.relative_to(output).as_posix() for path in extracted} == {
        "model/config.json",
        "model/grid.tif",
    }


def test_archive_rejects_symlinks_duplicates_and_limits(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        symlink = zipfile.ZipInfo("link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        handle.writestr(symlink, "target")
    with pytest.raises(ArchiveSecurityError, match="symbolic links"):
        inspect_zip(archive)

    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as handle:
        handle.writestr("Grid.tif", b"one")
        handle.writestr("grid.TIF", b"two")
    with pytest.raises(ArchiveSecurityError, match="duplicate"):
        inspect_zip(duplicate)

    limited = tmp_path / "limited.zip"
    with zipfile.ZipFile(limited, "w") as handle:
        handle.writestr("large.bin", b"12345")
    with pytest.raises(ArchiveSecurityError, match="size limit"):
        inspect_zip(limited, limits=ArchiveLimits(max_single_file_bytes=4))

    executable = tmp_path / "executable.zip"
    with zipfile.ZipFile(executable, "w") as handle:
        handle.writestr("plugin/run.sh", "#!/bin/sh")
    with pytest.raises(ArchiveSecurityError, match="executable content"):
        inspect_zip(executable)


def test_archive_snapshot_applies_a_raw_staging_byte_limit(tmp_path: Path) -> None:
    archive = tmp_path / "raw-limit.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("model/input.txt", b"tiny")

    with pytest.raises(ArchiveSecurityError, match="compressed-byte staging limit"):
        inspect_zip(archive, limits=ArchiveLimits(max_archive_bytes=archive.stat().st_size - 1))


def test_contained_paths_atomic_files_and_cas_detect_tampering(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert contained_path(root, "nested/file") == root / "nested/file"
    with pytest.raises(PathSecurityError):
        contained_path(root, "../escape")

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSecurityError, match="symbolic link"):
        contained_path(root, "linked/file")

    destination = atomic_write_bytes(root / "result.json", b"first")
    atomic_write_bytes(destination, b"second")
    assert destination.read_bytes() == b"second"

    store = ContentAddressedStore(root / "blobs")
    digest = store.put_bytes(b"immutable result")
    assert store.put_bytes(b"immutable result") == digest
    assert store.read_bytes(digest) == b"immutable result"
    blob = store.path_for(digest)
    os.chmod(blob, 0o600)
    blob.write_bytes(b"tampered")
    with pytest.raises(IOError, match="hash verification"):
        store.verify(digest)

    source = root / "source-grid.tif"
    source.write_bytes(b"synthetic raster bytes")
    file_digest = store.put_file(source)
    assert store.read_bytes(file_digest) == b"synthetic raster bytes"
