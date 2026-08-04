#!/usr/bin/env python3
"""Stage audited EcoCorridor resources for one target-specific Tauri build."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_STAGE = (
    ROOT / "apps" / "ecocorridor-desktop" / "src-tauri" / "release-generated"
).resolve()
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
TARGETS = {"linux-x64", "macos-arm64", "macos-x64", "windows-x64"}


def require_regular(path: Path, *, executable: bool = False) -> Path:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file():
        raise SystemExit(f"Release input must be a regular non-symlink file: {path}")
    if executable and os.name != "nt" and not os.access(resolved, os.X_OK):
        raise SystemExit(f"Release executable is not executable: {path}")
    return resolved


def require_safe_tree(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_dir():
        raise SystemExit(f"Release tree must be a regular non-symlink directory: {path}")
    for directory, names, files in os.walk(resolved, followlinks=False):
        for name in [*names, *files]:
            candidate = Path(directory) / name
            if candidate.is_symlink():
                try:
                    target = candidate.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise SystemExit(
                        f"Release resource contains a dangling or cyclic symlink: {candidate}"
                    ) from exc
                if (
                    resolved not in target.parents
                    or not (target.is_file() or target.is_dir())
                    or (
                        target.is_dir()
                        and (target == candidate.parent or target in candidate.parents)
                    )
                ):
                    raise SystemExit(
                        "Release symlinks must resolve to non-cyclic regular files or directories "
                        "inside the engine "
                        f"tree: {candidate}"
                    )
                continue
            mode = candidate.lstat().st_mode
            if name in names:
                if not stat.S_ISDIR(mode):
                    raise SystemExit(f"Release tree contains a special directory: {candidate}")
            elif not stat.S_ISREG(mode):
                raise SystemExit(f"Release tree contains a special file: {candidate}")
    return resolved


def require_symlink_free_tree(path: Path) -> Path:
    """Verify a staged tree contains only real directories and regular files."""

    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_dir():
        raise SystemExit(f"Staged release tree is not a regular directory: {path}")
    for directory, names, files in os.walk(resolved, followlinks=False):
        for name in [*names, *files]:
            candidate = Path(directory) / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise SystemExit(f"Staged release tree retained a symlink: {candidate}")
            if name in names and not stat.S_ISDIR(mode):
                raise SystemExit(f"Staged release tree contains a special directory: {candidate}")
            if name in files and not stat.S_ISREG(mode):
                raise SystemExit(f"Staged release tree contains a special file: {candidate}")
    return resolved


def copy_safe_tree(source: Path, destination: Path) -> None:
    """Dereference reviewed internal links into a plain, runnable release tree.

    PackageCompiler applications use versioned Julia library aliases.  Copying
    those aliases as independent files makes Darwin's dynamic loader treat the
    same library image as distinct loads and the packaged sysimage then fails
    its consistency check.  Preserve alias identity with hard links while
    still eliminating symbolic links from the signed resource tree.  Directory
    aliases (currently ICU's ``current`` directory) are expanded normally.
    """

    source = require_safe_tree(source)
    file_aliases: list[tuple[Path, Path]] = []
    for directory, names, files in os.walk(source, followlinks=False):
        for name in [*names, *files]:
            candidate = Path(directory) / name
            if not candidate.is_symlink():
                continue
            target = candidate.resolve(strict=True)
            if target.is_file():
                file_aliases.append((candidate.relative_to(source), target.relative_to(source)))
    shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)
    for alias_relative, target_relative in file_aliases:
        alias = destination / alias_relative
        target = destination / target_relative
        alias.unlink()
        try:
            os.link(target, alias)
        except OSError as exc:
            raise SystemExit(
                "Release staging must preserve PackageCompiler file aliases as hard links: "
                f"{alias_relative}"
            ) from exc
    require_symlink_free_tree(destination)


def copy_file(source: Path, destination: Path, *, executable: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(require_regular(source, executable=executable), destination)
    if executable and os.name != "nt":
        destination.chmod(destination.stat().st_mode | 0o755)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--engine-app", type=Path, required=True)
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--application-version", required=True)
    parser.add_argument("--source-commit", required=True)
    arguments = parser.parse_args()

    if not SEMVER.fullmatch(arguments.application_version):
        raise SystemExit("Application version must be canonical MAJOR.MINOR.PATCH SemVer.")
    if not SOURCE_COMMIT.fullmatch(arguments.source_commit):
        raise SystemExit("Release source commit must be one lowercase 40-character Git SHA.")

    stage = arguments.stage.resolve()
    if stage != EXPECTED_STAGE:
        raise SystemExit(f"Release stage must be the isolated Eco Tauri stage: {EXPECTED_STAGE}")
    worker = require_regular(arguments.worker, executable=True)
    engine_app = require_safe_tree(arguments.engine_app)

    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    copy_file(worker, stage / "bin" / f"ecocorridor-worker{suffix}", executable=True)
    copy_safe_tree(engine_app, stage / "engine")
    engine_executable = stage / "engine" / "bin" / f"ecocorridor-engine{suffix}"
    require_regular(engine_executable, executable=True)
    copy_file(ROOT / "NOTICE", stage / "notices" / "NOTICE")
    copy_file(
        ROOT / "THIRD_PARTY_NOTICES.md",
        stage / "notices" / "THIRD_PARTY_NOTICES.md",
    )
    copy_file(
        ROOT / "benchmarks" / "ecocorridor" / "data-manifest.json",
        stage / "guided" / "data-manifest.json",
    )
    guided_source = ROOT / "benchmarks" / "ecocorridor" / "ridgeway"
    guided_manifest = require_regular(guided_source / "direct-evidence-manifest.json")
    guided_projection = require_regular(guided_source / "ui_projection.json")
    guided_trust = require_regular(
        ROOT / "apps" / "ecocorridor-desktop" / "src" / "data" / "ridgewayReleaseTrust.json"
    )
    try:
        guided_payload = json.loads(guided_manifest.read_text(encoding="utf-8"))
        projection_payload = json.loads(guided_projection.read_text(encoding="utf-8"))
        trust_payload = json.loads(guided_trust.read_text(encoding="utf-8"))
        bundle = guided_payload["evidence_bundle"]
        archive_name = str(bundle["filename"])
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("Ridgeway direct-evidence manifest is invalid.") from exc
    if (
        Path(archive_name).name != archive_name
        or "/" in archive_name
        or "\\" in archive_name
        or guided_payload.get("schema_version") != "ridgeway-direct-evidence/v1.2"
        or guided_payload.get("evidence_contract_status") == "SUPERSEDED_NON_RELEASE"
        or guided_payload.get("historical_evidence_only") is True
        or guided_payload.get("release_replay_eligible") is not True
        or int(guided_payload.get("matrix_job_count", -1)) != 1_536
        or int(guided_payload.get("map_job_count", -1)) != 60
    ):
        raise SystemExit("Ridgeway direct evidence is not release eligible.")
    guided_archive = require_regular(guided_source / archive_name)
    if guided_archive.stat().st_size != int(bundle.get("byte_size", -1)) or sha256_file(
        guided_archive
    ) != bundle.get("sha256"):
        raise SystemExit("Ridgeway evidence archive differs from its public manifest.")
    manifest_digest = sha256_file(guided_manifest)
    if (
        projection_payload.get("schemaVersion") != "ecocorridor-replay/v1.2"
        or projection_payload.get("evidenceState") != "VERIFIED_DIRECT_EVIDENCE"
        or projection_payload.get("releaseReplayEligible") is not True
        or projection_payload.get("manifestSha256") != manifest_digest
        or trust_payload.get("schemaVersion") != "ecocorridor-release-trust/v1.0"
        or trust_payload.get("trusted") is not True
        or trust_payload.get("evidenceManifestSha256") != manifest_digest
    ):
        raise SystemExit("Ridgeway replay is not bound to independently trusted v1.2 evidence.")
    copy_file(guided_projection, stage / "guided" / "ridgeway-ui-projection.json")
    copy_file(guided_trust, stage / "guided" / "ridgeway-release-trust.json")
    copy_file(guided_manifest, stage / "guided" / "direct-evidence-manifest.json")
    copy_file(guided_archive, stage / "guided" / archive_name)
    try:
        import pyogrio
        import rasterio
        import shapely
    except ImportError as exc:
        raise SystemExit("Release staging requires the locked geospatial runtime.") from exc
    python_packages = {
        name: importlib.metadata.version(name)
        for name in (
            "geopandas",
            "numpy",
            "pyogrio",
            "pyinstaller",
            "rasterio",
            "shapely",
        )
    }
    inventory = {
        "schema_version": "1.0",
        "application_id": "org.ecocorridor.scenariostudio",
        "application_version": arguments.application_version,
        "source_commit_sha": arguments.source_commit,
        "target": arguments.target,
        "build_platform": platform.platform(),
        "julia_version": "1.12.6",
        "circuitscape_version": "5.17.1",
        "python_version": platform.python_version(),
        "python_worker": "PyInstaller one-file",
        "python_packages": python_packages,
        "gdal_version": rasterio.__gdal_version__,
        "pyogrio_gdal_version": pyogrio.__gdal_version_string__,
        "proj_version": rasterio.__proj_version__,
        "geos_version": shapely.geos_version_string,
        "connectivity_mode": "raster_pairwise_resistance",
    }
    (stage / "dependency-inventory.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Staged EcoCorridor release resources for {arguments.target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
