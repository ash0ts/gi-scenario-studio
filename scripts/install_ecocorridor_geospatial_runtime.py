"""Install EcoCorridor's GeoPandas runtime from the existing locked GI extra.

The Ridgeway producer evidence pins ``pyproject.toml`` and ``uv.lock`` exactly,
so the Eco application cannot rewrite either file merely to separate a shared
dependency.  Instead, this helper exports a hash-locked GeoPandas closure while
pruning the GI-only SWMManywhere branch, validates that boundary, and installs
the exported requirements without version or artifact drift.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PACKAGES = frozenset(
    {
        "certifi",
        "geopandas",
        "numpy",
        "packaging",
        "pandas",
        "pyogrio",
        "pyproj",
        "python-dateutil",
        "shapely",
        "six",
        "tzdata",
    }
)
FORBIDDEN_PACKAGES = frozenset({"llvmlite", "numba", "pyflwdir", "swmmanywhere"})
IMPORT_CHECKS = ("geopandas", "pandas", "pyogrio", "pyproj", "shapely")
REQUIREMENT_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", re.MULTILINE)


def normalize_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_exported_requirements(contents: str) -> dict[str, str]:
    packages = {
        normalize_package(name): version for name, version in REQUIREMENT_PATTERN.findall(contents)
    }
    package_names = set(packages)
    missing = EXPECTED_PACKAGES - package_names
    forbidden = FORBIDDEN_PACKAGES & package_names
    unexpected = package_names - EXPECTED_PACKAGES
    if missing:
        raise ValueError(
            "locked EcoCorridor geospatial export is missing: " + ", ".join(sorted(missing))
        )
    if forbidden:
        raise ValueError(
            "GI-only dependencies crossed the EcoCorridor boundary: " + ", ".join(sorted(forbidden))
        )
    if unexpected:
        raise ValueError(
            "unreviewed packages crossed the EcoCorridor dependency boundary: "
            + ", ".join(sorted(unexpected))
        )
    return dict(sorted(packages.items()))


def validate_installed_versions(locked_versions: dict[str, str]) -> None:
    expected_installed = EXPECTED_PACKAGES - {"tzdata"}
    if sys.platform in {"emscripten", "win32"}:
        expected_installed = EXPECTED_PACKAGES
    for package in sorted(expected_installed):
        installed = importlib.metadata.version(package)
        if installed != locked_versions[package]:
            raise RuntimeError(
                f"installed {package} {installed} differs from locked {locked_versions[package]}"
            )
    for module in IMPORT_CHECKS:
        importlib.import_module(module)


def export_command(requirements: Path) -> list[str]:
    return [
        "uv",
        "export",
        "--locked",
        "--no-dev",
        "--extra",
        "geo",
        "--prune",
        "swmmanywhere",
        "--prune",
        "pydantic",
        "--format",
        "requirements.txt",
        "--no-emit-project",
        "--no-emit-workspace",
        "--output-file",
        str(requirements),
    ]


def install_command(requirements: Path) -> list[str]:
    return [
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--require-hashes",
        "--strict",
        "--only-binary",
        ":all:",
        "--requirements",
        str(requirements),
    ]


def run(*, check_only: bool) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="ecocorridor-geo-") as temporary:
        requirements = Path(temporary) / "requirements.txt"
        subprocess.run(
            export_command(requirements),
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        packages = validate_exported_requirements(requirements.read_text(encoding="utf-8"))
        if not check_only:
            subprocess.run(install_command(requirements), cwd=ROOT, check=True)
            subprocess.run(
                ["uv", "pip", "check", "--python", sys.executable],
                cwd=ROOT,
                check=True,
            )
            validate_installed_versions(packages)
        return packages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate the locked dependency projection without installing it",
    )
    args = parser.parse_args()
    packages = run(check_only=args.check_only)
    print(
        json.dumps(
            {
                "forbidden_packages_absent": True,
                "mode": "check" if args.check_only else "install",
                "package_count": len(packages),
                "reviewed_packages": sorted(EXPECTED_PACKAGES),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
