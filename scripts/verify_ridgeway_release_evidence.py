#!/usr/bin/env python3
"""Recompute the committed Ridgeway projection from every retained direct artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import package_ridgeway_evidence as packager

MAX_FILES = 50_000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Ridgeway archive has an unsafe member: {name}")
    return path


def extract_regular_archive(archive_path: Path, destination: Path) -> None:
    seen: set[str] = set()
    total_bytes = 0
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive:
            relative = safe_member(member.name)
            if member.name in seen or len(seen) >= MAX_FILES or not member.isfile():
                raise ValueError(
                    f"Ridgeway archive has a duplicate or non-file member: {member.name}"
                )
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise ValueError(f"Ridgeway archive member exceeds its size policy: {member.name}")
            total_bytes += member.size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("Ridgeway archive exceeds its uncompressed-size policy")
            seen.add(member.name)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Ridgeway archive member cannot be read: {member.name}")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    written += len(chunk)
            if written != member.size:
                raise ValueError(f"Ridgeway archive member is truncated: {member.name}")


def portable_packager_temporary_directory(*args: object, **kwargs: object):
    """Preserve the retained packager while making its scratch root portable."""

    if kwargs.get("dir") == "/private/tmp" and not Path("/private/tmp").is_dir():
        kwargs["dir"] = None
    return tempfile.TemporaryDirectory(*args, **kwargs)


def verify(
    publish_directory: Path,
    desktop_fixture: Path,
    *,
    allow_historical_blocked: bool,
) -> str:
    publish_directory = publish_directory.resolve(strict=True)
    desktop_fixture = desktop_fixture.resolve(strict=True)
    direct_path = publish_directory / "direct-evidence-manifest.json"
    direct = json.loads(direct_path.read_text(encoding="utf-8"))
    if direct.get("evidence_contract_status") == "SUPERSEDED_NON_RELEASE":
        if (
            not allow_historical_blocked
            or direct.get("release_replay_eligible") is not False
            or direct.get("historical_evidence_only") is not True
        ):
            raise ValueError("Historical Ridgeway evidence cannot satisfy a release verification")
        return "HISTORICAL_BLOCKED"
    if (
        direct.get("schema_version") != "ridgeway-direct-evidence/v1.2"
        or direct.get("release_replay_eligible") is not True
    ):
        raise ValueError("Ridgeway evidence is neither an explicit historical block nor v1.2")
    bundle = direct.get("evidence_bundle")
    if not isinstance(bundle, dict):
        raise TypeError("Ridgeway direct manifest omits its evidence archive")
    filename = bundle.get("filename")
    if not isinstance(filename, str) or PurePosixPath(filename).name != filename:
        raise ValueError("Ridgeway evidence archive filename is unsafe")
    archive_path = publish_directory / filename
    if (
        archive_path.is_symlink()
        or not archive_path.is_file()
        or digest(archive_path) != bundle.get("sha256")
        or archive_path.stat().st_size != bundle.get("byte_size")
    ):
        raise ValueError("Ridgeway evidence archive does not match its committed manifest")

    with tempfile.TemporaryDirectory(prefix="ridgeway-semantic-verification-") as temporary:
        root = Path(temporary)
        evidence_root = root / "evidence"
        recomputed_publish = root / "published"
        recomputed_desktop = root / "desktop" / "ridgewayFixture.json"
        evidence_root.mkdir()
        recomputed_publish.mkdir()
        recomputed_desktop.parent.mkdir()
        extract_regular_archive(archive_path, evidence_root)
        # The immutable report packager was captured on macOS and its exact
        # bytes are part of the evidence contract. Adapt only its scratch-root
        # lookup on other platforms; all recomputed output is still compared
        # byte-for-byte with the committed evidence.
        retained_tempfile = packager.tempfile
        packager.tempfile = SimpleNamespace(
            TemporaryDirectory=portable_packager_temporary_directory
        )
        try:
            packager.package(evidence_root, recomputed_publish, recomputed_desktop)
        finally:
            packager.tempfile = retained_tempfile
        # The immutable local archive predates publication of these two
        # top-level ledger identities, but its bundle index already pins both.
        # Accept only that exact metadata enrichment and the resulting manifest
        # digest propagation into the projection/trust records. No scientific
        # value, artifact, or archive byte is normalized here.
        bundle_index = json.loads((evidence_root / "bundle-index.json").read_text("utf-8"))
        recomputed_direct = json.loads(
            (recomputed_publish / "direct-evidence-manifest.json").read_text("utf-8")
        )
        for key in ("assumption_ledger_sha256", "cost_basis_sha256"):
            value = bundle_index.get(key)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                or direct.get(key) != value
            ):
                raise ValueError(f"Ridgeway published manifest does not pin archived {key}")
            recomputed_direct[key] = value
        expected_direct = packager.canonical_json_bytes(recomputed_direct)
        if direct_path.read_bytes() != expected_direct:
            raise ValueError(
                "Ridgeway semantic recomputation differs from direct-evidence-manifest.json"
            )
        enriched_manifest_sha = hashlib.sha256(expected_direct).hexdigest()

        recomputed_projection = json.loads(
            (recomputed_publish / "ui_projection.json").read_text("utf-8")
        )
        recomputed_projection["manifestSha256"] = enriched_manifest_sha
        expected_projection = packager.canonical_json_bytes(recomputed_projection)
        for committed in (publish_directory / "ui_projection.json", desktop_fixture):
            if committed.read_bytes() != expected_projection:
                raise ValueError(f"Ridgeway semantic recomputation differs from {committed.name}")

        recomputed_trust = json.loads(
            recomputed_desktop.with_name("ridgewayReleaseTrust.json").read_text("utf-8")
        )
        recomputed_trust["evidenceManifestSha256"] = enriched_manifest_sha
        expected_trust = packager.canonical_json_bytes(recomputed_trust)
        committed_trust = desktop_fixture.with_name("ridgewayReleaseTrust.json")
        if committed_trust.read_bytes() != expected_trust:
            raise ValueError(
                "Ridgeway semantic recomputation differs from ridgewayReleaseTrust.json"
            )
    return "VERIFIED_DIRECT_EVIDENCE"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish-directory", required=True, type=Path)
    parser.add_argument("--desktop-fixture", required=True, type=Path)
    parser.add_argument("--allow-historical-blocked", action="store_true")
    arguments = parser.parse_args()
    try:
        disposition = verify(
            arguments.publish_directory,
            arguments.desktop_fixture,
            allow_historical_blocked=arguments.allow_historical_blocked,
        )
    except (OSError, TypeError, ValueError, tarfile.TarError, packager.PackagingError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Ridgeway semantic evidence disposition: {disposition}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
