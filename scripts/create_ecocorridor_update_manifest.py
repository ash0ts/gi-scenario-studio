#!/usr/bin/env python3
"""Create Tauri's signed static update manifest from collected release assets."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")


def _decode_tauri_container(value: str, *, label: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"{label} is not canonical Tauri base64 text") from exc
    if base64.b64encode(decoded.encode("utf-8")).decode("ascii") != value:
        raise ValueError(f"{label} is not canonically encoded")
    return decoded


def verify_tauri_minisign_signature(
    artifact: Path, *, signature_container: str, public_key_container: str
) -> None:
    """Verify Tauri's base64-wrapped, pre-hashed Minisign signature."""

    public_lines = _decode_tauri_container(
        public_key_container, label="updater public key"
    ).splitlines()
    signature_lines = _decode_tauri_container(
        signature_container, label="updater signature"
    ).splitlines()
    if len(public_lines) != 2 or not public_lines[0].startswith(
        "untrusted comment: minisign public key"
    ):
        raise ValueError("updater public key has an invalid Minisign envelope")
    if (
        len(signature_lines) != 4
        or not signature_lines[0].startswith("untrusted comment: ")
        or not signature_lines[2].startswith("trusted comment: ")
    ):
        raise ValueError("updater signature has an invalid Minisign envelope")
    try:
        public_record = base64.b64decode(public_lines[1], validate=True)
        signature_record = base64.b64decode(signature_lines[1], validate=True)
        global_signature = base64.b64decode(signature_lines[3], validate=True)
    except binascii.Error as exc:
        raise ValueError("updater Minisign record is not valid base64") from exc
    if (
        len(public_record) != 42
        or public_record[:2] not in {b"Ed", b"ED"}
        or len(signature_record) != 74
        or signature_record[:2] != b"ED"
        or len(global_signature) != 64
    ):
        raise ValueError("updater signature must use Minisign pre-hashed Ed25519 mode")
    if public_record[2:10] != signature_record[2:10]:
        raise ValueError("updater signature key ID does not match the embedded public key")
    key = Ed25519PublicKey.from_public_bytes(public_record[10:42])
    primary_signature = signature_record[10:74]
    message_digest = hashlib.blake2b(artifact.read_bytes(), digest_size=64).digest()
    trusted_comment = signature_lines[2][len("trusted comment: ") :].encode("utf-8")
    try:
        key.verify(primary_signature, message_digest)
        key.verify(global_signature, primary_signature + trusted_comment)
    except InvalidSignature as exc:
        raise ValueError(f"updater signature does not verify for {artifact.name}") from exc


def one(root: Path, pattern: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"Expected one updater artifact for {pattern}; found {len(matches)}.")
    match = matches[0]
    if match.is_symlink() or not match.is_file() or match.stat().st_size <= 0:
        raise SystemExit(f"Updater artifact is empty or unsafe: {match.name}.")
    return match


def release_url(repository: str, tag: str, filename: str) -> str:
    return (
        f"https://github.com/{repository}/releases/download/"
        f"{quote(tag, safe='.-_')}/{quote(filename, safe='.-_')}"
    )


def atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--updater-public-key", required=True)
    arguments = parser.parse_args()
    if not REPOSITORY.fullmatch(arguments.repository):
        raise SystemExit("Release repository must be an owner/name GitHub repository.")
    version = arguments.version.removeprefix("v")
    if not SEMVER.fullmatch(version) or arguments.tag != f"v{version}":
        raise SystemExit("Updater tag and semantic version must match exactly.")
    if arguments.assets.is_symlink():
        raise SystemExit("Release assets directory may not be a symlink.")
    assets = arguments.assets.resolve(strict=True)
    if not assets.is_dir():
        raise SystemExit("Release assets path must be a directory.")
    if arguments.output.is_symlink():
        raise SystemExit("Updater manifest output may not be a symlink.")
    output = arguments.output.resolve(strict=False)
    if output.parent != assets or output.name != "latest.json":
        raise SystemExit("Updater manifest must be release-assets/latest.json.")
    targets = {
        "darwin-aarch64": one(assets, "*macos-arm64.app.tar.gz"),
        "darwin-x86_64": one(assets, "*macos-x64.app.tar.gz"),
        "windows-x86_64": one(assets, "*windows-x64-setup.exe"),
    }
    platforms: dict[str, dict[str, str]] = {}
    for target, artifact in targets.items():
        signature_path = Path(f"{artifact}.sig")
        if (
            signature_path.is_symlink()
            or not signature_path.is_file()
            or not 0 < signature_path.stat().st_size <= 64 * 1024
        ):
            raise SystemExit(f"Updater signature is missing for {artifact.name}.")
        signature = signature_path.read_text(encoding="utf-8").strip()
        if not signature:
            raise SystemExit(f"Updater signature is empty for {artifact.name}.")
        try:
            verify_tauri_minisign_signature(
                artifact,
                signature_container=signature,
                public_key_container=arguments.updater_public_key.strip(),
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        platforms[target] = {
            "url": release_url(arguments.repository, arguments.tag, artifact.name),
            "signature": signature,
        }
    payload = {
        "version": version,
        "notes": "See the signed EcoCorridor release evidence attached to this release.",
        "platforms": platforms,
    }
    atomic_write(
        output,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print("Created signed updater metadata for three desktop targets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
