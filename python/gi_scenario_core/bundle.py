"""Secure, read-only inspection and deliberate extraction of portable projects."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .swmm import SwmmDocument


class BundleSecurityError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class BundleLimits:
    max_files: int = 2_000
    max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    max_single_file_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: float = 500.0


@dataclass(slots=True)
class BundleFile:
    path: str
    size: int
    compressed_size: int
    sha256: str
    role: str


@dataclass(slots=True)
class ModelDependencies:
    model_path: str
    references: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BundleInspection:
    archive_path: str
    archive_sha256: str
    archive_bytes: int
    files: list[BundleFile]
    models: list[ModelDependencies]
    total_uncompressed_bytes: int
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return bool(self.models) and not any(model.missing for model in self.models)

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "valid": self.valid,
        }


FORBIDDEN_EXTENSIONS = {
    ".app",
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".dylib",
    ".exe",
    ".jar",
    ".msi",
    ".ps1",
    ".scr",
    ".sh",
    ".so",
}

FILE_EXTENSIONS = {
    ".dat",
    ".txt",
    ".tsv",
    ".csv",
    ".rpt",
    ".out",
    ".hsf",
    ".hif",
    ".ifc",
    ".clm",
    ".rain",
}


def _normalize_member(name: str) -> str:
    if "\x00" in name:
        raise BundleSecurityError("archive member contains a null byte")
    candidate = name.replace("\\", "/")
    path = PurePosixPath(candidate)
    if path.is_absolute() or candidate.startswith("/"):
        raise BundleSecurityError(f"absolute archive path is not allowed: {name!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise BundleSecurityError(f"unsafe archive path is not allowed: {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise BundleSecurityError(f"drive-qualified archive path is not allowed: {name!r}")
    return path.as_posix()


def _check_info(info: zipfile.ZipInfo, limits: BundleLimits) -> str:
    normalized = _normalize_member(info.filename.rstrip("/")) if not info.is_dir() else ""
    unix_mode = (info.external_attr >> 16) & 0o177777
    file_type = stat.S_IFMT(unix_mode)
    if file_type and stat.S_ISLNK(unix_mode):
        raise BundleSecurityError(f"symbolic links are not allowed: {info.filename!r}")
    if file_type and not (stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode)):
        raise BundleSecurityError(f"special files are not allowed: {info.filename!r}")
    if info.file_size > limits.max_single_file_bytes:
        raise BundleSecurityError(f"archive member exceeds size limit: {info.filename!r}")
    if info.file_size and info.compress_size == 0:
        raise BundleSecurityError(f"invalid compressed size for {info.filename!r}")
    if info.compress_size and info.file_size / info.compress_size > limits.max_compression_ratio:
        raise BundleSecurityError(f"suspicious compression ratio for {info.filename!r}")
    if normalized and Path(normalized).suffix.casefold() in FORBIDDEN_EXTENSIONS:
        raise BundleSecurityError(f"executable content is not accepted: {info.filename!r}")
    return normalized


def _hash_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


@contextmanager
def _verified_archive(
    archive: str | Path,
    *,
    limits: BundleLimits,
    expected_sha256: str | None,
    expected_bytes: int | None,
) -> Iterator[tuple[Path, BinaryIO, str, int]]:
    """Hash one regular file descriptor, then keep that descriptor open for ZIP use."""

    archive_path = Path(archive)
    try:
        path_metadata = os.lstat(archive_path)
    except OSError as exc:
        raise BundleSecurityError("project bundle must be a readable ZIP archive") from exc
    if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(path_metadata.st_mode):
        raise BundleSecurityError("project bundle must be a regular file, not a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(archive_path, flags)
    except OSError as exc:
        raise BundleSecurityError("project bundle could not be opened safely") from exc
    try:
        descriptor_metadata = os.fstat(descriptor)
        archive_bytes = descriptor_metadata.st_size
        if not stat.S_ISREG(descriptor_metadata.st_mode):
            raise BundleSecurityError("project bundle must be a regular file")
        if archive_bytes <= 0 or archive_bytes > limits.max_uncompressed_bytes:
            raise BundleSecurityError("project bundle is empty or exceeds the 2 GiB limit")
        if expected_bytes is not None and archive_bytes != expected_bytes:
            raise BundleSecurityError("project bundle byte count changed after native staging")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            archive_sha256 = _hash_stream(stream)
            if expected_sha256 is not None and archive_sha256 != expected_sha256:
                raise BundleSecurityError("project bundle hash changed after native staging")
            stream.seek(0)
            if not zipfile.is_zipfile(stream):
                raise BundleSecurityError("project bundle must be a readable ZIP archive")
            stream.seek(0)
            yield archive_path, stream, archive_sha256, archive_bytes
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _inspect_open_bundle(
    handle: zipfile.ZipFile,
    *,
    archive_path: Path,
    archive_sha256: str,
    archive_bytes: int,
    limits: BundleLimits,
) -> BundleInspection:
    files: list[BundleFile] = []
    model_text: dict[str, str] = {}
    total = 0
    infos = handle.infolist()
    regular = [info for info in infos if not info.is_dir()]
    if len(regular) > limits.max_files:
        raise BundleSecurityError("archive contains too many files")
    seen: set[str] = set()
    for info in infos:
        normalized = _check_info(info, limits)
        if info.is_dir():
            continue
        key = normalized.casefold()
        if key in seen:
            raise BundleSecurityError(f"duplicate archive member: {normalized!r}")
        seen.add(key)
        total += info.file_size
        if total > limits.max_uncompressed_bytes:
            raise BundleSecurityError("archive exceeds total uncompressed size limit")
        with handle.open(info, "r") as stream:
            sha256 = _hash_stream(stream)
        suffix = Path(normalized).suffix.casefold()
        role = "swmm_model" if suffix == ".inp" else "dependency"
        files.append(BundleFile(normalized, info.file_size, info.compress_size, sha256, role))
        if suffix == ".inp":
            with handle.open(info, "r") as stream:
                raw = stream.read()
            try:
                model_text[normalized] = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                model_text[normalized] = raw.decode("cp1252")
    archive_names = {item.path.casefold(): item.path for item in files}
    dependencies: list[ModelDependencies] = []
    for path, text in model_text.items():
        references = discover_external_references(SwmmDocument.parse(text))
        parent = PurePosixPath(path).parent
        resolved: list[str] = []
        missing: list[str] = []
        for reference in references:
            reference_path = PurePosixPath(reference.replace("\\", "/"))
            if reference_path.is_absolute() or any(part == ".." for part in reference_path.parts):
                missing.append(reference)
                continue
            candidate = (parent / reference_path).as_posix()
            actual = archive_names.get(candidate.casefold())
            if actual is None:
                missing.append(reference)
            else:
                resolved.append(actual)
        dependencies.append(
            ModelDependencies(path, sorted(references), sorted(set(resolved)), sorted(set(missing)))
        )
    warnings = []
    if not dependencies:
        warnings.append("No SWMM .inp model was found.")
    if len(dependencies) > 1:
        warnings.append("Multiple SWMM models were found; select the base and storm variants.")
    return BundleInspection(
        str(archive_path),
        archive_sha256,
        archive_bytes,
        sorted(files, key=lambda item: item.path),
        dependencies,
        total,
        warnings,
    )


def inspect_bundle(
    archive: str | Path,
    *,
    limits: BundleLimits | None = None,
    expected_sha256: str | None = None,
    expected_bytes: int | None = None,
) -> BundleInspection:
    """Inspect an archive without writing any content to disk."""

    limits = limits or BundleLimits()
    with (
        _verified_archive(
            archive,
            limits=limits,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
        ) as (archive_path, stream, archive_sha256, archive_bytes),
        zipfile.ZipFile(stream) as handle,
    ):
        return _inspect_open_bundle(
            handle,
            archive_path=archive_path,
            archive_sha256=archive_sha256,
            archive_bytes=archive_bytes,
            limits=limits,
        )


def safe_extract_bundle(
    archive: str | Path,
    destination: str | Path,
    *,
    limits: BundleLimits | None = None,
    expected_sha256: str | None = None,
    expected_bytes: int | None = None,
) -> BundleInspection:
    """Extract a previously inspectable bundle under an explicit project directory."""

    limits = limits or BundleLimits()
    destination_path = Path(destination).resolve()
    with (
        _verified_archive(
            archive,
            limits=limits,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
        ) as (archive_path, stream, archive_sha256, archive_bytes),
        zipfile.ZipFile(stream) as handle,
    ):
        inspection = _inspect_open_bundle(
            handle,
            archive_path=archive_path,
            archive_sha256=archive_sha256,
            archive_bytes=archive_bytes,
            limits=limits,
        )
        destination_path.mkdir(parents=True, exist_ok=True)
        for info in handle.infolist():
            if info.is_dir():
                continue
            normalized = _check_info(info, limits)
            target = (destination_path / normalized).resolve()
            if destination_path not in target.parents:
                raise BundleSecurityError("archive member escapes the destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.partial")
            with handle.open(info, "r") as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            os.replace(temporary, target)
    return inspection


def discover_external_references(document: SwmmDocument) -> set[str]:
    """Return explicit file references from supported SWMM input sections."""

    references: set[str] = set()
    for section_name in ("RAINGAGES", "TIMESERIES"):
        for row in document.data_rows(section_name):
            for index, token in enumerate(row.tokens[:-1]):
                if token.upper() == "FILE":
                    references.add(row.tokens[index + 1])
    for row in document.data_rows("OPTIONS"):
        if len(row.tokens) >= 3 and row.tokens[0].upper() in {"USE", "SAVE"}:
            if row.tokens[1].upper() == "HOTSTART":
                references.add(row.tokens[2])
        elif len(row.tokens) >= 2 and row.tokens[0].upper() in {
            "USE_HOTSTART",
            "SAVE_HOTSTART",
        }:
            references.add(row.tokens[1])
    for section_name in ("TEMPERATURE", "EVAPORATION", "ADJUSTMENTS"):
        for row in document.data_rows(section_name):
            for index, token in enumerate(row.tokens):
                if token.upper() == "FILE" and index + 1 < len(row.tokens):
                    references.add(row.tokens[index + 1])
                elif Path(token).suffix.casefold() in FILE_EXTENSIONS:
                    references.add(token)
    return {item for item in references if item not in {"*", "NONE", "NO"}}


def hash_files(paths: Iterable[str | Path]) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path_like in paths:
        path = Path(path_like)
        with path.open("rb") as handle:
            manifest[str(path)] = _hash_stream(handle)
    return manifest
