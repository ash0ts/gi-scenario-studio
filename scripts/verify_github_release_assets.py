"""Create and verify an exact GitHub Release asset manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ecocorridor-github-release-assets/v1.0"
_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_MANIFEST_KEYS = frozenset({"schema_version", "assets"})
_ASSET_KEYS = frozenset({"name", "size_in_bytes", "digest"})
_WINDOWS_RESERVED = frozenset(
    {"aux", "con", "nul", "prn"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _safe_name(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not _ASSET_NAME.fullmatch(value)
        or value.endswith(".")
        or ".." in value
        or value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
    ):
        raise ValueError(f"{label} has an unsafe filename")
    return value


def _positive_size(value: Any, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must have a positive byte size")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def create_manifest(assets_directory: Path) -> dict[str, object]:
    """Describe every immediate regular release asset without following links."""

    if assets_directory.is_symlink():
        raise ValueError("Release asset directory may not be a symlink")
    root = assets_directory.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Release asset root must be a directory")

    assets: list[dict[str, object]] = []
    names: set[str] = set()
    with os.scandir(root) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            name = _safe_name(entry.name, label="release asset")
            collision_key = name.casefold()
            if collision_key in names:
                raise ValueError(f"Release asset name is duplicated: {name}")
            names.add(collision_key)
            if entry.is_symlink():
                raise ValueError(f"Release assets may not contain symlinks: {name}")
            if not entry.is_file(follow_symlinks=False):
                raise ValueError(
                    f"Release assets must be immediate regular files; rejected: {name}"
                )
            path = Path(entry.path)
            size = _positive_size(entry.stat(follow_symlinks=False).st_size, label=name)
            assets.append(
                {
                    "name": name,
                    "size_in_bytes": size,
                    "digest": _sha256(path),
                }
            )
    if not assets:
        raise ValueError("Release asset directory must contain at least one file")
    return {"schema_version": SCHEMA_VERSION, "assets": assets}


def validate_manifest(payload: object) -> dict[str, dict[str, object]]:
    """Return a strict name-indexed representation of an expected manifest."""

    manifest = _object(payload, label="release asset manifest")
    if set(manifest) != _MANIFEST_KEYS or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Release asset manifest has an unsupported shape or schema")
    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ValueError("Release asset manifest must contain assets")

    indexed: dict[str, dict[str, object]] = {}
    collision_keys: set[str] = set()
    previous_name: str | None = None
    for index, raw_asset in enumerate(raw_assets):
        asset = _object(raw_asset, label=f"manifest asset {index}")
        if set(asset) != _ASSET_KEYS:
            raise ValueError("Manifest assets must contain exactly name, size, and digest")
        name = _safe_name(asset.get("name"), label="manifest asset")
        collision_key = name.casefold()
        if collision_key in collision_keys:
            raise ValueError(f"Manifest asset name is duplicated: {name}")
        collision_keys.add(collision_key)
        if previous_name is not None and name <= previous_name:
            raise ValueError("Manifest assets must be in strict filename order")
        previous_name = name
        size = _positive_size(asset.get("size_in_bytes"), label=name)
        digest = asset.get("digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError(f"Manifest asset has no valid SHA-256 digest: {name}")
        indexed[name] = {"name": name, "size_in_bytes": size, "digest": digest}
    return indexed


def verify_release(
    manifest_payload: object,
    release_payload: object,
    *,
    expected_tag: str,
    expected_draft: bool,
) -> int:
    """Verify one REST release response against every expected asset byte identity."""

    if not isinstance(expected_tag, str) or not _TAG.fullmatch(expected_tag):
        raise ValueError("Expected release tag is unsafe")
    if type(expected_draft) is not bool:
        raise TypeError("Expected draft state must be a boolean")
    expected = validate_manifest(manifest_payload)
    release = _object(release_payload, label="GitHub release")
    if release.get("tag_name") != expected_tag:
        raise ValueError("GitHub release tag does not match the requested tag")
    if release.get("draft") is not expected_draft:
        raise ValueError("GitHub release draft state does not match the requested state")
    if release.get("prerelease") is not False:
        raise ValueError("GitHub release must not be a prerelease")
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        raise TypeError("GitHub release assets must be a list")

    uploaded: dict[str, dict[str, object]] = {}
    collision_keys: set[str] = set()
    for index, raw_asset in enumerate(raw_assets):
        asset = _object(raw_asset, label=f"GitHub release asset {index}")
        name = _safe_name(asset.get("name"), label="GitHub release asset")
        collision_key = name.casefold()
        if collision_key in collision_keys:
            raise ValueError(f"GitHub release asset name is duplicated: {name}")
        collision_keys.add(collision_key)
        if asset.get("state") != "uploaded":
            raise ValueError(f"GitHub release asset is not uploaded: {name}")
        size = _positive_size(asset.get("size"), label=name)
        digest = asset.get("digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError(f"GitHub release asset has no server SHA-256 digest: {name}")
        uploaded[name] = {"name": name, "size_in_bytes": size, "digest": digest}

    missing = sorted(set(expected) - set(uploaded))
    extra = sorted(set(uploaded) - set(expected))
    if missing or extra:
        raise ValueError(f"GitHub release asset set differs: missing={missing}, extra={extra}")
    mismatched = sorted(name for name in expected if expected[name] != uploaded[name])
    if mismatched:
        raise ValueError(
            "GitHub release asset size or server digest differs: " + ", ".join(mismatched)
        )
    return len(expected)


def _write_json_atomic(path: Path, payload: object) -> None:
    if path.is_symlink():
        raise ValueError("Manifest output may not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"JSON input must be a regular non-symlink file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="manifest immediate local release assets")
    create.add_argument("--assets-directory", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)

    verify = subparsers.add_parser("verify", help="verify one GitHub REST release response")
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--release-json", required=True, type=Path)
    verify.add_argument("--expected-tag", required=True)
    verify.add_argument("--expected-draft", required=True, choices=("true", "false"))

    arguments = parser.parse_args()
    try:
        if arguments.command == "create":
            root = arguments.assets_directory.resolve(strict=True)
            output = arguments.output.resolve(strict=False)
            if output == root or root in output.parents:
                raise ValueError(
                    "Expected manifest must remain outside the release asset directory"
                )
            manifest = create_manifest(arguments.assets_directory)
            _write_json_atomic(arguments.output, manifest)
            count = len(validate_manifest(manifest))
            action = "Manifested"
        else:
            count = verify_release(
                _read_json(arguments.manifest),
                _read_json(arguments.release_json),
                expected_tag=arguments.expected_tag,
                expected_draft=arguments.expected_draft == "true",
            )
            action = "Verified"
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"{action} {count} exact GitHub Release asset(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
