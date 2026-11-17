"""Packaging-only Circuitscape manifest provenance gate.

This module is intentionally outside the archived direct-run protocol parser.
It protects new engine bundles without pretending that a packaging-policy
change regenerated the retained Ridgeway solver evidence.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .engine import EngineManifestGateError

REQUIRED_CIRCUITSCAPE_UUID = "2b7a1792-8151-5239-925d-e2b8fdfa3201"
REQUIRED_CIRCUITSCAPE_VERSION = "5.17.1"
REQUIRED_CIRCUITSCAPE_TREE_SHA1 = "d612b7cc82bc4ebc176f5d0a144a484503779c39"


def parse_circuitscape_manifest_version(manifest_path: Path) -> str:
    """Return the exact UUID- and source-tree-bound Circuitscape version."""

    try:
        with manifest_path.open("rb") as stream:
            manifest = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise EngineManifestGateError("Julia Manifest.toml is unreadable or invalid") from error
    dependencies = manifest.get("deps")
    entries = dependencies.get("Circuitscape") if isinstance(dependencies, dict) else None
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise EngineManifestGateError(
            "Julia manifest must contain exactly one [[deps.Circuitscape]] entry"
        )
    entry = entries[0]
    if entry.get("uuid") != REQUIRED_CIRCUITSCAPE_UUID:
        raise EngineManifestGateError("Julia manifest Circuitscape entry has an unexpected UUID")
    tracking_fields = sorted({"path", "repo-url", "repo-rev"} & set(entry))
    if tracking_fields:
        raise EngineManifestGateError(
            "Julia manifest Circuitscape entry must use the reviewed registry tree, not "
            f"path/repository tracking: {', '.join(tracking_fields)}"
        )
    version = entry.get("version")
    if not isinstance(version, str):
        raise EngineManifestGateError("Julia manifest Circuitscape entry has no exact version")
    if entry.get("git-tree-sha1") != REQUIRED_CIRCUITSCAPE_TREE_SHA1:
        raise EngineManifestGateError(
            "Julia manifest Circuitscape entry does not resolve the reviewed registry tree"
        )
    return version


def verify_circuitscape_manifest(manifest_path: Path) -> str:
    version = parse_circuitscape_manifest_version(manifest_path)
    if version != REQUIRED_CIRCUITSCAPE_VERSION:
        raise EngineManifestGateError(
            "Julia manifest does not pin Circuitscape "
            f"{REQUIRED_CIRCUITSCAPE_VERSION}; found {version}"
        )
    return version
