#!/usr/bin/env python3
"""Collect uniquely named signed installer and updater artifacts for a target."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

VERSION = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")


def one(root: Path, pattern: str) -> Path:
    matches = sorted(path for path in root.rglob(pattern) if path.is_file())
    if len(matches) != 1:
        raise SystemExit(f"Expected one {pattern} release artifact, found {len(matches)}.")
    return matches[0]


def copy(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise SystemExit(f"Release outputs may not be symlinks: {source.name}")
    if target.exists() or target.is_symlink():
        raise SystemExit(f"Release output already exists: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


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
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--public-resource-key", type=Path, required=True)
    parser.add_argument("--smoke-report", type=Path, required=True)
    parser.add_argument("--installed-health-receipt", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--target", choices=["macos-arm64", "macos-x64", "windows-x64"], required=True
    )
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    if not VERSION.fullmatch(arguments.version):
        raise SystemExit("Release asset version must be vMAJOR.MINOR.PATCH.")
    if arguments.bundle_root.is_symlink() or arguments.release_root.is_symlink():
        raise SystemExit("Release input roots may not be symlinks.")
    bundle_root = arguments.bundle_root.resolve(strict=True)
    release_root = arguments.release_root.resolve(strict=True)
    if not bundle_root.is_dir() or not release_root.is_dir():
        raise SystemExit("Release input roots must be directories.")
    if arguments.output.is_symlink():
        raise SystemExit("Release output directory may not be a symlink.")
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise SystemExit("Target release output directory must be empty.")
    prefix = f"EcoCorridor-Scenario-Studio_{arguments.version}_{arguments.target}"

    copied: list[dict[str, object]] = []

    def collect(source: Path, filename: str, role: str) -> None:
        destination = output / filename
        copy(source, destination)
        copied.append(
            {
                "file": destination.name,
                "role": role,
                "bytes": destination.stat().st_size,
                "sha256": digest(destination),
            }
        )

    if arguments.target.startswith("macos"):
        installer = one(bundle_root, "*.dmg")
        updater = one(bundle_root, "*.app.tar.gz")
        updater_signature = Path(f"{updater}.sig")
        if not updater_signature.is_file():
            raise SystemExit("The signed macOS updater artifact is incomplete.")
        collect(installer, f"{prefix}.dmg", "installer")
        collect(updater, f"{prefix}.app.tar.gz", "updater")
        collect(updater_signature, f"{prefix}.app.tar.gz.sig", "updater-signature")
    else:
        installer_msi = one(bundle_root, "*.msi")
        installer_nsis = one(bundle_root, "*-setup.exe")
        for installer, suffix, role in (
            (installer_msi, ".msi", "installer-msi"),
            (installer_nsis, "-setup.exe", "installer-nsis"),
        ):
            signature = Path(f"{installer}.sig")
            if not signature.is_file():
                raise SystemExit(f"Signed updater output is missing for {installer.name}.")
            collect(installer, f"{prefix}{suffix}", role)
            collect(signature, f"{prefix}{suffix}.sig", f"{role}-signature")

    for filename, role in (
        ("release-manifest.json", "resource-manifest"),
        ("release-manifest.sig", "resource-manifest-signature"),
        ("engine-manifest.json", "engine-manifest"),
        ("dependency-inventory.json", "target-dependency-inventory"),
    ):
        collect(release_root / filename, f"{prefix}.{filename}", role)
    if (
        arguments.public_resource_key.is_symlink()
        or arguments.smoke_report.is_symlink()
        or len(arguments.installed_health_receipt) != 2
        or any(path.is_symlink() for path in arguments.installed_health_receipt)
    ):
        raise SystemExit("Release evidence inputs may not be symlinks.")
    public_resource_key = arguments.public_resource_key.resolve(strict=True)
    if not re.fullmatch(r"[0-9a-f]{64}\n?", public_resource_key.read_text(encoding="ascii")):
        raise SystemExit("Resource public key must be one raw Ed25519 key in lowercase hex.")
    smoke_report = arguments.smoke_report.resolve(strict=True)
    if "EcoCorridor staged worker and direct Circuitscape engine smoke passed." not in (
        smoke_report.read_text(encoding="utf-8")
    ):
        raise SystemExit("Offline smoke report does not contain its success disposition.")
    collect(
        public_resource_key,
        f"{prefix}.resource-public-key.hex",
        "resource-public-key",
    )
    collect(
        smoke_report,
        f"{prefix}.offline-smoke.txt",
        "offline-smoke",
    )
    for index, receipt in enumerate(arguments.installed_health_receipt, start=1):
        collect(
            receipt.resolve(strict=True),
            f"{prefix}.installed-host-health-{index}.json",
            "installed-host-health",
        )
    inventory = {
        "schema_version": "1.0",
        "target": arguments.target,
        "version": arguments.version,
        "assets": copied,
    }
    atomic_write(
        output / f"{prefix}.asset-manifest.json",
        (json.dumps(inventory, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(f"Collected {len(copied)} release assets for {arguments.target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
