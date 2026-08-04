"""Secure ZIP validation and extraction for portable scenario bundles."""

from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .storage import PathSecurityError, contained_path


class ArchiveSecurityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_files: int = 2_000
    max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    max_single_file_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: float = 500.0
    forbidden_extensions: tuple[str, ...] = (
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
    )


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    path: str
    uncompressed_bytes: int
    compressed_bytes: int
    sha256: str


def normalize_archive_member(name: str) -> str:
    if not name or "\x00" in name:
        raise ArchiveSecurityError("archive member path is empty or contains a null byte")
    candidate = name.replace("\\", "/")
    if any(part in {"", ".", ".."} for part in candidate.split("/")):
        raise ArchiveSecurityError(f"unsafe archive path is not allowed: {name!r}")
    path = PurePosixPath(candidate)
    if path.is_absolute() or candidate.startswith("/"):
        raise ArchiveSecurityError(f"absolute archive path is not allowed: {name!r}")
    if path.parts and ":" in path.parts[0]:
        raise ArchiveSecurityError(f"drive-qualified archive path is not allowed: {name!r}")
    return path.as_posix()


def _validate_info(info: zipfile.ZipInfo, limits: ArchiveLimits) -> str | None:
    name = info.filename.rstrip("/")
    normalized = normalize_archive_member(name) if name else None
    unix_mode = (info.external_attr >> 16) & 0o177777
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise ArchiveSecurityError(f"symbolic links are not allowed: {info.filename!r}")
    file_type = stat.S_IFMT(unix_mode)
    if file_type and not (stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode)):
        raise ArchiveSecurityError(f"special files are not allowed: {info.filename!r}")
    if info.file_size > limits.max_single_file_bytes:
        raise ArchiveSecurityError(f"archive member exceeds size limit: {info.filename!r}")
    if info.file_size and info.compress_size == 0:
        raise ArchiveSecurityError(f"invalid compressed size: {info.filename!r}")
    if info.compress_size and info.file_size / info.compress_size > limits.max_compression_ratio:
        raise ArchiveSecurityError(f"suspicious compression ratio: {info.filename!r}")
    if normalized and PurePosixPath(normalized).suffix.casefold() in limits.forbidden_extensions:
        raise ArchiveSecurityError(f"executable content is not allowed: {info.filename!r}")
    return normalized


@contextmanager
def _open_zip(archive: str | Path) -> Iterator[BinaryIO]:
    path = Path(archive)
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ArchiveSecurityError("archive must be a readable regular file") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ArchiveSecurityError("archive must be a regular file, not a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArchiveSecurityError("archive could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise ArchiveSecurityError("archive changed while it was being opened")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _inspect_handle(handle: zipfile.ZipFile, limits: ArchiveLimits) -> list[ArchiveMember]:
    members: list[ArchiveMember] = []
    seen: set[str] = set()
    total = 0
    regular_count = sum(not info.is_dir() for info in handle.infolist())
    if regular_count > limits.max_files:
        raise ArchiveSecurityError("archive contains too many files")
    for info in handle.infolist():
        normalized = _validate_info(info, limits)
        if info.is_dir():
            continue
        assert normalized is not None
        key = normalized.casefold()
        if key in seen:
            raise ArchiveSecurityError(f"duplicate archive member: {normalized!r}")
        seen.add(key)
        total += info.file_size
        if total > limits.max_uncompressed_bytes:
            raise ArchiveSecurityError("archive exceeds total uncompressed size limit")
        digest = hashlib.sha256()
        observed_bytes = 0
        with handle.open(info, "r") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                observed_bytes += len(block)
                if observed_bytes > info.file_size:
                    raise ArchiveSecurityError("archive member expanded beyond declared size")
                digest.update(block)
        if observed_bytes != info.file_size:
            raise ArchiveSecurityError("archive member size changed during inspection")
        members.append(
            ArchiveMember(normalized, info.file_size, info.compress_size, digest.hexdigest())
        )
    return members


def inspect_zip(archive: str | Path, *, limits: ArchiveLimits | None = None) -> list[ArchiveMember]:
    limits = limits or ArchiveLimits()
    try:
        with _open_zip(archive) as stream, zipfile.ZipFile(stream, "r") as handle:
            return _inspect_handle(handle, limits)
    except zipfile.BadZipFile as exc:
        raise ArchiveSecurityError("bundle is not a readable ZIP archive") from exc


def read_zip_member(
    archive: str | Path,
    member_path: str,
    *,
    limits: ArchiveLimits | None = None,
    max_bytes: int = 8 * 1024 * 1024,
) -> bytes:
    """Read one member only after validating and hashing the complete archive."""

    limits = limits or ArchiveLimits()
    normalized = normalize_archive_member(member_path)
    if max_bytes <= 0:
        raise ValueError("member read limit must be positive")
    try:
        with _open_zip(archive) as stream, zipfile.ZipFile(stream, "r") as handle:
            members = {item.path: item for item in _inspect_handle(handle, limits)}
            member = members.get(normalized)
            if member is None:
                raise KeyError(normalized)
            if member.uncompressed_bytes > max_bytes:
                raise ArchiveSecurityError("requested archive member exceeds read limit")
            with handle.open(normalized, "r") as source:
                payload = source.read(max_bytes + 1)
            if len(payload) != member.uncompressed_bytes or len(payload) > max_bytes:
                raise ArchiveSecurityError("requested archive member size changed")
            if hashlib.sha256(payload).hexdigest() != member.sha256:
                raise ArchiveSecurityError("requested archive member hash changed")
            return payload
    except zipfile.BadZipFile as exc:
        raise ArchiveSecurityError("bundle is not a readable ZIP archive") from exc


def extract_zip(
    archive: str | Path,
    destination: str | Path,
    *,
    limits: ArchiveLimits | None = None,
) -> list[Path]:
    """Validate the complete archive before extracting any bytes."""

    limits = limits or ArchiveLimits()
    destination_path = Path(destination)
    destination_path.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    try:
        with _open_zip(archive) as stream, zipfile.ZipFile(stream, "r") as handle:
            members = _inspect_handle(handle, limits)
            by_name = {member.path: member for member in members}
            for info in handle.infolist():
                normalized = _validate_info(info, limits)
                if info.is_dir() or normalized is None:
                    continue
                member = by_name[normalized]
                try:
                    target = contained_path(destination_path, normalized)
                except PathSecurityError as exc:  # defensive against extraction races
                    raise ArchiveSecurityError(str(exc)) from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                # Recheck after creating parent directories so a pre-existing
                # symlink can never become an extraction parent.
                target = contained_path(destination_path, normalized)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(target, flags, 0o600)
                except OSError as exc:
                    raise ArchiveSecurityError(
                        f"archive target cannot be created: {normalized!r}"
                    ) from exc
                written = 0
                try:
                    with handle.open(info, "r") as source, os.fdopen(descriptor, "wb") as output:
                        descriptor = -1
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            written += len(block)
                            if written > member.uncompressed_bytes:
                                raise ArchiveSecurityError(
                                    "archive member expanded beyond declared size"
                                )
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if written != member.uncompressed_bytes:
                        raise ArchiveSecurityError("archive member size changed during extraction")
                except BaseException:
                    target.unlink(missing_ok=True)
                    raise
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                extracted.append(target)
    except zipfile.BadZipFile as exc:
        for target in reversed(extracted):
            target.unlink(missing_ok=True)
        raise ArchiveSecurityError("bundle is not a readable ZIP archive") from exc
    except BaseException:
        for target in reversed(extracted):
            target.unlink(missing_ok=True)
        raise
    return extracted
