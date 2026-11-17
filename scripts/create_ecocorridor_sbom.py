#!/usr/bin/env python3
"""Create a deterministic CycloneDX inventory from every committed lockfile."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
NATIVE_RUNTIME_PACKAGES = frozenset(
    {
        "geopandas",
        "numpy",
        "pydantic",
        "pyogrio",
        "pyinstaller",
        "rasterio",
        "scikit-learn",
        "scipy",
        "shapely",
        "wandb",
        "weave",
    }
)


@dataclass(frozen=True, order=True, slots=True)
class Component:
    ecosystem: str
    name: str
    version: str

    @property
    def purl(self) -> str:
        encoded_name = "/".join(quote(part, safe="") for part in self.name.split("/"))
        return f"pkg:{self.ecosystem}/{encoded_name}@{quote(self.version, safe='.+-')}"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def toml_packages(path: Path, ecosystem: str) -> set[Component]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        Component(ecosystem, item["name"], item["version"])
        for item in data.get("package", [])
        if item.get("name") and item.get("version")
    }


def julia_packages(path: Path) -> set[Component]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    components: set[Component] = set()
    for name, records in data.get("deps", {}).items():
        if isinstance(records, dict):
            records = [records]
        for record in records:
            version = record.get("version")
            if version:
                components.add(Component("julia", name, version))
    components.add(Component("generic", "Julia", data["julia_version"]))
    return components


def pnpm_packages(path: Path) -> set[Component]:
    text = path.read_text(encoding="utf-8")
    packages = text.split("\npackages:\n", 1)[1].split("\nsnapshots:\n", 1)[0]
    components: set[Component] = set()
    for match in re.finditer(r"^  ('.+'|\".+\"):\s*$", packages, re.MULTILINE):
        key = ast.literal_eval(match.group(1))
        name, separator, version = key.rpartition("@")
        version = version.split("(", 1)[0]
        if separator and name and version and not version.startswith(("workspace:", "link:")):
            components.add(Component("npm", name, version))
    return components


def native_inventory_components(directory: Path) -> tuple[set[Component], list[Path]]:
    """Load the three signed target inventories included in a full release."""

    if directory.is_symlink() or not directory.is_dir():
        raise SystemExit("Native inventory directory must be a regular non-symlink directory.")
    paths = sorted(directory.glob("*.dependency-inventory.json"))
    targets: set[str] = set()
    components: set[Component] = set()
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise SystemExit("Native dependency inventories must be regular non-symlink files.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        target = payload.get("target")
        packages = payload.get("python_packages")
        if (
            payload.get("schema_version") != "1.0"
            or payload.get("application_id") != "org.ecocorridor.scenariostudio"
            or target not in {"macos-arm64", "macos-x64", "windows-x64"}
            or target in targets
            or not isinstance(packages, dict)
            or set(packages) != NATIVE_RUNTIME_PACKAGES
        ):
            raise SystemExit(f"Invalid or duplicate native dependency inventory: {path.name}")
        targets.add(target)
        for name, version in packages.items():
            if not isinstance(version, str) or not version:
                raise SystemExit(f"Native dependency inventory omits a {name} version: {path.name}")
            components.add(Component("pypi", name, version))
        for name, version in (
            ("Python", payload.get("python_version")),
            ("GDAL", payload.get("gdal_version")),
            ("GDAL", payload.get("pyogrio_gdal_version")),
            ("PROJ", payload.get("proj_version")),
            ("GEOS", payload.get("geos_version")),
            ("PyInstaller", packages.get("pyinstaller")),
        ):
            if not isinstance(version, str) or not version:
                raise SystemExit(f"Native dependency inventory omits {name}: {path.name}")
            components.add(Component("generic", name, version))
    if targets != {"macos-arm64", "macos-x64", "windows-x64"}:
        raise SystemExit("A full release SBOM requires all three target dependency inventories.")
    return components, paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--native-inventory-directory", type=Path)
    arguments = parser.parse_args()
    lockfiles = [
        ROOT / "uv.lock",
        ROOT / "pnpm-lock.yaml",
        ROOT / "crates" / "scenario-host" / "Cargo.lock",
        ROOT / "apps" / "ecocorridor-desktop" / "src-tauri" / "Cargo.lock",
        ROOT / "julia" / "EcoCorridorEngine" / "Manifest.toml",
    ]
    if any(not path.is_file() for path in lockfiles):
        raise SystemExit("Every EcoCorridor lockfile is required to create the release SBOM.")
    components = set()
    components.update(toml_packages(lockfiles[0], "pypi"))
    components.update(pnpm_packages(lockfiles[1]))
    components.update(toml_packages(lockfiles[2], "cargo"))
    components.update(toml_packages(lockfiles[3], "cargo"))
    components.update(julia_packages(lockfiles[4]))
    inventory_paths: list[Path] = []
    if arguments.native_inventory_directory is not None:
        native, inventory_paths = native_inventory_components(
            arguments.native_inventory_directory.resolve(strict=True)
        )
        components.update(native)
    namespace_inputs = [*lockfiles, *inventory_paths]
    namespace_seed = "|".join(
        f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name}:{digest(path)}"
        for path in namespace_inputs
    )
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, namespace_seed)}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": f"pkg:generic/ecocorridor-scenario-studio@{arguments.version}",
                "name": "EcoCorridor Scenario Studio",
                "version": arguments.version,
            },
            "properties": [
                {
                    "name": "org.ecocorridor.lockfile.sha256",
                    "value": (
                        f"{path.relative_to(ROOT).as_posix()}={digest(path)}"
                        if path.is_relative_to(ROOT)
                        else f"{path.name}={digest(path)}"
                    ),
                }
                for path in namespace_inputs
            ],
        },
        "components": [
            {
                "type": "library",
                "bom-ref": item.purl,
                "name": item.name,
                "version": item.version,
                "purl": item.purl,
                "properties": [
                    {"name": "org.ecocorridor.package.ecosystem", "value": item.ecosystem}
                ],
            }
            for item in sorted(components)
        ],
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(components)} locked components to the CycloneDX SBOM.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
