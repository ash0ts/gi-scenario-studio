#!/usr/bin/env python3
"""Generate the target-specific Tauri release overlay from public metadata."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
TAURI_ROOT = (ROOT / "apps" / "ecocorridor-desktop" / "src-tauri").resolve()
THUMBPRINT = re.compile(r"[0-9A-F]{40,128}\Z")


def require_https(value: str, *, label: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise SystemExit(f"{label} must be an HTTPS URL without embedded credentials.")
    return value


def require_minisign_public_key(value: str) -> str:
    container = value.strip()
    if not container or any(character.isspace() for character in container):
        raise SystemExit("The updater key must be Tauri's canonical base64 minisign container.")
    try:
        envelope = base64.b64decode(container, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(
            "The updater key must be Tauri's canonical base64 minisign container."
        ) from exc
    if base64.b64encode(envelope.encode("utf-8")).decode("ascii") != container:
        raise SystemExit("The updater public-key container is not canonically encoded.")
    lines = [line.strip() for line in envelope.splitlines() if line.strip()]
    if len(lines) != 2 or not lines[0].casefold().startswith(
        "untrusted comment: minisign public key"
    ):
        raise SystemExit("The updater key must be one complete minisign public key.")
    try:
        decoded = base64.b64decode(lines[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SystemExit("The updater minisign public key is not valid base64.") from exc
    if len(decoded) != 42 or decoded[:2] != b"Ed":
        raise SystemExit("The updater minisign public key has an invalid Ed25519 payload.")
    return container


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--updater-public-key", required=True)
    parser.add_argument("--updater-endpoint", required=True)
    parser.add_argument("--windows-thumbprint")
    parser.add_argument("--windows-timestamp-url")
    arguments = parser.parse_args()

    output = arguments.output.resolve()
    if output.parent != TAURI_ROOT or not output.name.endswith(".generated.conf.json"):
        raise SystemExit("Generated Tauri release overlays must stay in the Eco src-tauri root.")
    public_key = require_minisign_public_key(arguments.updater_public_key)
    config: dict[str, object] = {
        "bundle": {
            "createUpdaterArtifacts": True,
            "resources": {"release-generated": "release"},
        },
        "plugins": {
            "updater": {
                "pubkey": public_key,
                "endpoints": [require_https(arguments.updater_endpoint, label="Updater endpoint")],
            }
        },
    }
    if bool(arguments.windows_thumbprint) != bool(arguments.windows_timestamp_url):
        raise SystemExit("Windows signing thumbprint and timestamp URL are required together.")
    if arguments.windows_thumbprint:
        thumbprint = arguments.windows_thumbprint.replace(" ", "").upper()
        if not THUMBPRINT.fullmatch(thumbprint):
            raise SystemExit("Windows signing thumbprint is invalid.")
        config["bundle"]["windows"] = {  # type: ignore[index]
            "certificateThumbprint": thumbprint,
            "digestAlgorithm": "sha256",
            "timestampUrl": require_https(
                arguments.windows_timestamp_url or "", label="Windows timestamp URL"
            ),
        }
    atomic_write(
        output,
        (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(f"Generated target-specific Tauri release overlay: {output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
