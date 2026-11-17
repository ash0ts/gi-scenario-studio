"""Contained paths, atomic files, and content-addressed immutable blobs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

from .contracts import canonical_json, require_sha256


class PathSecurityError(ValueError):
    pass


def contained_path(
    root: str | Path,
    relative: str | Path,
    *,
    reject_symlinks: bool = True,
) -> Path:
    """Resolve a relative path below ``root`` without traversing symlinks."""

    root_path = Path(root).resolve(strict=True)
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative_path.parts:
        raise PathSecurityError("path must be non-empty and relative")
    if any(part in {"", ".", ".."} for part in relative_path.parts):
        raise PathSecurityError("path traversal is not allowed")
    current = root_path
    for part in relative_path.parts:
        current = current / part
        if reject_symlinks and current.is_symlink():
            raise PathSecurityError(f"symbolic link traversal is not allowed: {relative_path}")
    candidate = current.resolve(strict=False)
    if candidate != root_path and root_path not in candidate.parents:
        raise PathSecurityError("path escapes the allowed root")
    return candidate


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:  # pragma: no cover - platform filesystem edge
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    destination: str | Path,
    payload: bytes,
    *,
    mode: int = 0o600,
    overwrite: bool = True,
) -> Path:
    """Write, fsync, and atomically rename a file in its destination directory."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if not overwrite and path.exists():
            raise FileExistsError(path)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def atomic_write_json(destination: str | Path, payload: Any, *, mode: int = 0o600) -> Path:
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    return atomic_write_bytes(destination, encoded, mode=mode)


def sha256_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        byte_count += len(block)
        digest.update(block)
    return digest.hexdigest(), byte_count


class ContentAddressedStore:
    """Immutable SHA-256 store that verifies every read and convergent write."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.blob_root = self.root / "sha256"
        self.blob_root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        digest = require_sha256(sha256)
        return contained_path(self.blob_root, Path(digest[:2]) / digest[2:])

    def put_bytes(self, payload: bytes, *, expected_sha256: str | None = None) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != require_sha256(expected_sha256):
            raise ValueError("content hash does not match expected SHA-256")
        destination = self.path_for(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self.verify(digest)
            return digest
        atomic_write_bytes(destination, payload, mode=0o444, overwrite=False)
        return digest

    def put_file(self, source: str | Path, *, expected_sha256: str | None = None) -> str:
        source_path = Path(source)
        metadata = os.lstat(source_path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PathSecurityError("CAS source must be a regular file, not a symbolic link")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source_path, flags)
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".incoming.", dir=self.blob_root
        )
        temporary = Path(temporary_name)
        try:
            digest = hashlib.sha256()
            with (
                os.fdopen(descriptor, "rb", closefd=True) as stream,
                os.fdopen(temporary_descriptor, "wb", closefd=True) as output,
            ):
                descriptor = -1
                temporary_descriptor = -1
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            actual = digest.hexdigest()
            if expected_sha256 is not None and actual != require_sha256(expected_sha256):
                raise ValueError("content hash does not match expected SHA-256")
            destination = self.path_for(actual)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                self.verify(actual)
            else:
                os.chmod(temporary, 0o444)
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
            return actual
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            temporary.unlink(missing_ok=True)

    def verify(self, sha256: str) -> Path:
        expected = require_sha256(sha256)
        path = self.path_for(expected)
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PathSecurityError("CAS object must be a regular file")
        with path.open("rb") as stream:
            actual, _ = sha256_stream(stream)
        if actual != expected:
            raise OSError(f"CAS object failed hash verification: {expected}")
        return path

    def read_bytes(self, sha256: str) -> bytes:
        return self.verify(sha256).read_bytes()

    def put_json(self, payload: Any) -> str:
        return self.put_bytes(canonical_json(payload).encode("utf-8"))

    def read_json(self, sha256: str) -> Any:
        return json.loads(self.read_bytes(sha256).decode("utf-8"))
