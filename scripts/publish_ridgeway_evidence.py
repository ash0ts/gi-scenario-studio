#!/usr/bin/env python3
"""Safely publish a verified Ridgeway package without deleting history.

The evidence packager is an immutable report dependency retained inside every
Ridgeway archive.  It intentionally runs in an empty staging directory here.
This publisher then installs only the staged, verified outputs and removes at
most the checksum-valid archive named by the previously active manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import package_ridgeway_evidence as packager

MANIFEST_LIMIT_BYTES = 1024 * 1024
ARCHIVE_NAME = re.compile(r"ridgeway-direct-evidence-([0-9a-f]{12})\.tar\.gz")


class PublicationError(ValueError):
    """Raised when verified outputs cannot be installed without ambiguity."""


@dataclass(frozen=True)
class ActiveArchive:
    path: Path
    sha256: str
    byte_size: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_from_manifest(directory: Path, manifest: object) -> ActiveArchive | None:
    if not isinstance(manifest, dict) or manifest.get("release_replay_eligible") is not True:
        return None
    bundle = manifest.get("evidence_bundle")
    if not isinstance(bundle, dict) or bundle.get("format") != "tar+gzip":
        return None
    filename = bundle.get("filename")
    expected_sha256 = bundle.get("sha256")
    expected_size = bundle.get("byte_size")
    if (
        not isinstance(filename, str)
        or ARCHIVE_NAME.fullmatch(filename) is None
        or PurePosixPath(filename).name != filename
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or filename != f"ridgeway-direct-evidence-{expected_sha256[:12]}.tar.gz"
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size <= 0
    ):
        return None
    archive = directory / filename
    if (
        archive.is_symlink()
        or not archive.is_file()
        or archive.stat().st_size != expected_size
        or sha256_file(archive) != expected_sha256
    ):
        return None
    return ActiveArchive(path=archive, sha256=expected_sha256, byte_size=expected_size)


def prior_active_archive(publish_directory: Path) -> ActiveArchive | None:
    """Resolve only the checksum-valid archive named by the active manifest."""

    manifest_path = publish_directory / "direct-evidence-manifest.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.stat().st_size > MANIFEST_LIMIT_BYTES
    ):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _archive_from_manifest(publish_directory, manifest)


def remove_replaced_active_archive(
    prior: ActiveArchive | None,
    *,
    current_archive: Path,
) -> bool:
    """Remove one replaced active archive; never glob historical evidence."""

    if prior is None or prior.path == current_archive:
        return False
    if (
        prior.path.is_symlink()
        or not prior.path.is_file()
        or prior.path.stat().st_size != prior.byte_size
        or sha256_file(prior.path) != prior.sha256
    ):
        raise PublicationError("prior active Ridgeway archive changed; refusing cleanup")
    prior.path.unlink()
    return True


def _atomic_install(source: Path, target: Path, *, immutable: bool = False) -> None:
    if source.is_symlink() or not source.is_file():
        raise PublicationError(f"staged publication source is unsafe: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise PublicationError(f"publication target is unsafe: {target.name}")
    source_sha256 = sha256_file(source)
    if immutable and target.exists():
        if target.stat().st_size != source.stat().st_size or sha256_file(target) != source_sha256:
            raise PublicationError(f"content-addressed publication target differs: {target.name}")
        return
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if sha256_file(temporary) != source_sha256:
            raise PublicationError(f"publication copy failed checksum verification: {target.name}")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(
    evidence_root: Path,
    publish_directory: Path,
    desktop_fixture: Path,
) -> dict[str, object]:
    """Stage immutable packaging, install fail-closed outputs, then prune one active archive."""

    evidence_root = evidence_root.resolve(strict=True)
    publish_directory.mkdir(parents=True, exist_ok=True)
    publish_directory = publish_directory.resolve(strict=True)
    desktop_fixture.parent.mkdir(parents=True, exist_ok=True)
    desktop_fixture = desktop_fixture.resolve(strict=False)
    previous = prior_active_archive(publish_directory)

    with tempfile.TemporaryDirectory(
        prefix=".ridgeway-publication-",
        dir=publish_directory.parent,
    ) as temporary_directory:
        staging_root = Path(temporary_directory)
        staged_publish = staging_root / "publish"
        staged_fixture = staging_root / "desktop" / desktop_fixture.name
        result = packager.package(evidence_root, staged_publish, staged_fixture)
        staged_manifest_path = staged_publish / "direct-evidence-manifest.json"
        staged_manifest = json.loads(staged_manifest_path.read_text(encoding="utf-8"))
        current = _archive_from_manifest(staged_publish, staged_manifest)
        if current is None:
            raise PublicationError("staged Ridgeway manifest does not bind a verified archive")

        target_archive = publish_directory / current.path.name
        _atomic_install(current.path, target_archive, immutable=True)
        _atomic_install(
            staged_publish / "ui_projection.json", publish_directory / "ui_projection.json"
        )
        _atomic_install(staged_fixture, desktop_fixture)
        staged_trust = staged_fixture.with_name("ridgewayReleaseTrust.json")
        target_trust = desktop_fixture.with_name("ridgewayReleaseTrust.json")
        _atomic_install(staged_trust, target_trust)
        # The manifest is the publication authority and is installed last. A
        # crash before this point leaves a detectable fail-closed mismatch.
        _atomic_install(staged_manifest_path, publish_directory / "direct-evidence-manifest.json")

        expected = {
            target_archive: current.path,
            publish_directory / "ui_projection.json": staged_publish / "ui_projection.json",
            desktop_fixture: staged_fixture,
            target_trust: staged_trust,
            publish_directory / "direct-evidence-manifest.json": staged_manifest_path,
        }
        for target, source in expected.items():
            if target.stat().st_size != source.stat().st_size or sha256_file(target) != sha256_file(
                source
            ):
                raise PublicationError(f"installed Ridgeway output differs: {target.name}")
        _fsync_directory(publish_directory)
        if desktop_fixture.parent != publish_directory:
            _fsync_directory(desktop_fixture.parent)
        remove_replaced_active_archive(previous, current_archive=target_archive)
        _fsync_directory(publish_directory)
        return {
            **result,
            "archive": str(target_archive),
            "direct_evidence_manifest": str(publish_directory / "direct-evidence-manifest.json"),
            "ui_projection": str(publish_directory / "ui_projection.json"),
            "desktop_fixture": str(desktop_fixture),
            "release_trust": str(target_trust),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--publish-directory", required=True, type=Path)
    parser.add_argument("--desktop-fixture", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        result = publish(
            arguments.evidence_root,
            arguments.publish_directory,
            arguments.desktop_fixture,
        )
    except (OSError, TypeError, ValueError, packager.PackagingError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
