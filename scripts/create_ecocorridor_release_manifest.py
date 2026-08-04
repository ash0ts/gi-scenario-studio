#!/usr/bin/env python3
"""Create the resource manifest verified by the native host at startup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from nacl.signing import SigningKey

APPLICATION_ID = "org.ecocorridor.scenariostudio"
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
TARGETS = {"linux-x64", "macos-arm64", "macos-x64", "windows-x64"}
MAX_RELEASE_RESOURCES = 100_000
SIGNING_SEED_FILE = re.compile(rb"[0-9a-f]{64}\n\Z")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def atomic_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    temporary.replace(path)


def load_signing_key(path: Path) -> SigningKey:
    """Load exactly one canonical raw Ed25519 seed without accepting PEM variants."""

    if os.name != "nt":
        try:
            if path.stat().st_mode & 0o077:
                raise SystemExit(
                    "Release manifest signing seed must have owner-only POSIX permissions."
                )
        except OSError as exc:
            raise SystemExit("Release manifest signing seed metadata is unreadable.") from exc
    try:
        seed_hex = path.read_bytes()
    except OSError as exc:
        raise SystemExit("Release manifest signing seed is unreadable.") from exc
    if not SIGNING_SEED_FILE.fullmatch(seed_hex):
        raise SystemExit(
            "Release manifest signing key must be a canonical 32-byte Ed25519 seed "
            "encoded as 64 lowercase hex characters plus one newline."
        )
    return SigningKey(bytes.fromhex(seed_hex[:-1].decode("ascii")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signature-output", type=Path, required=True)
    parser.add_argument("--public-key-output", type=Path, required=True)
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--application-version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--executable", action="append", default=[])
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    manifest_output = arguments.output.resolve()
    signature_output = arguments.signature_output.resolve()
    public_key_output = arguments.public_key_output.resolve()
    if arguments.root.is_symlink() or not root.is_dir():
        raise SystemExit("Release root must be a regular non-symlink directory.")
    if manifest_output != root / "release-manifest.json":
        raise SystemExit("Release manifest must be written at the canonical resource path.")
    if signature_output != root / "release-manifest.sig":
        raise SystemExit("Release signature must be written at the canonical resource path.")
    if public_key_output == root or root in public_key_output.parents:
        raise SystemExit(
            "The public verification key must remain outside the signed resource tree."
        )
    signing_key = arguments.signing_key.resolve(strict=True)
    if arguments.signing_key.is_symlink() or not signing_key.is_file():
        raise SystemExit("Release manifest signing key must be a regular non-symlink file.")
    if signing_key == root or root in signing_key.parents:
        raise SystemExit("Release signing material may not enter the packaged resource tree.")
    if not SEMVER.fullmatch(arguments.application_version):
        raise SystemExit("Application version must be canonical MAJOR.MINOR.PATCH SemVer.")
    if not SOURCE_COMMIT.fullmatch(arguments.source_commit):
        raise SystemExit("Release source commit must be one lowercase 40-character Git SHA.")
    excluded = {manifest_output, signature_output, public_key_output}
    executable_paths = {Path(value).as_posix() for value in arguments.executable}
    resources = []
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() and item.resolve() not in excluded
    ):
        if path.is_symlink():
            raise SystemExit(f"Packaged resources may not contain symlinks: {path}")
        relative = path.relative_to(root).as_posix()
        resources.append(
            {
                "path": relative,
                "sha256": digest(path),
                "bytes": path.stat().st_size,
                "executable": relative in executable_paths,
            }
        )
    if not resources or len(resources) > MAX_RELEASE_RESOURCES or not executable_paths:
        raise SystemExit("Release resources and executable declarations are required.")
    missing_executables = executable_paths - {item["path"] for item in resources}
    if missing_executables:
        raise SystemExit(f"Declared executables are missing: {sorted(missing_executables)}")
    payload = {
        "schema_version": "1.1",
        "application_id": APPLICATION_ID,
        "application_version": arguments.application_version,
        "source_commit_sha": arguments.source_commit,
        "target": arguments.target,
        "resources": resources,
    }
    manifest_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    private_key = load_signing_key(signing_key)
    signature = private_key.sign(manifest_bytes)
    signature = signature.signature
    public_key = private_key.verify_key.encode()
    atomic_write(manifest_output, manifest_bytes)
    atomic_write(signature_output, signature)
    atomic_write(public_key_output, public_key.hex().encode("ascii") + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
