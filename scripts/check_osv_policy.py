"""Validate OSV lockfile coverage and the repository's license deny policy."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_LOCKFILES = (
    "uv.lock",
    "pnpm-lock.yaml",
    "apps/desktop/src-tauri/Cargo.lock",
    "crates/scenario-host/Cargo.lock",
    "apps/ecocorridor-desktop/src-tauri/Cargo.lock",
)
LICENSE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*")
DENIED_LICENSE_MARKERS = ("AGPL-", "SSPL-")


class PolicyError(ValueError):
    """Raised when an OSV report does not satisfy repository policy."""


@dataclass(frozen=True, slots=True)
class PolicySummary:
    dependency_count: int
    unresolved_license_count: int


def _matches_lockfile(source_path: str, expected: str) -> bool:
    normalized = source_path.replace("\\", "/")
    return normalized == expected or normalized.endswith(f"/{expected}")


def _package_label(package: dict[str, Any]) -> str:
    ecosystem = str(package.get("ecosystem", "unknown"))
    name = str(package.get("name", "unknown"))
    version = str(package.get("version", "unknown"))
    return f"{ecosystem}:{name}@{version}"


def evaluate_report(report: dict[str, Any]) -> PolicySummary:
    """Evaluate lockfile coverage and reject forbidden license identifiers."""
    config = report.get("experimental_config")
    if not isinstance(config, dict):
        raise PolicyError("OSV report is missing scanner configuration metadata")
    licenses = config.get("licenses")
    if not isinstance(licenses, dict) or licenses.get("summary") is not True:
        raise PolicyError("OSV license inventory was not enabled")

    results = report.get("results")
    if not isinstance(results, list):
        raise PolicyError("OSV report does not contain a results list")

    source_paths: list[str] = []
    packages: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        source = result.get("source")
        if isinstance(source, dict) and isinstance(source.get("path"), str):
            source_paths.append(source["path"])
        result_packages = result.get("packages")
        if isinstance(result_packages, list):
            packages.extend(package for package in result_packages if isinstance(package, dict))

    missing_lockfiles = [
        expected
        for expected in EXPECTED_LOCKFILES
        if not any(_matches_lockfile(path, expected) for path in source_paths)
    ]
    if missing_lockfiles:
        raise PolicyError(
            "OSV did not inventory required lockfiles: " + ", ".join(missing_lockfiles)
        )
    if not packages:
        raise PolicyError("OSV report contains no dependency packages")

    denied: list[str] = []
    unresolved = 0
    for entry in packages:
        package = entry.get("package")
        package = package if isinstance(package, dict) else {}
        expressions = entry.get("licenses")
        if not isinstance(expressions, list) or not expressions:
            unresolved += 1
            continue

        normalized_expressions = [
            expression for expression in expressions if isinstance(expression, str)
        ]
        if any(
            expression.upper() in {"UNKNOWN", "NON-STANDARD"}
            for expression in normalized_expressions
        ):
            unresolved += 1

        for expression in normalized_expressions:
            tokens = LICENSE_TOKEN.findall(expression.upper())
            forbidden = sorted(
                {
                    token
                    for token in tokens
                    if any(marker in token for marker in DENIED_LICENSE_MARKERS)
                }
            )
            if forbidden:
                denied.append(f"{_package_label(package)}: {', '.join(forbidden)} ({expression})")

    if denied:
        raise PolicyError("forbidden dependency licenses were found:\n" + "\n".join(sorted(denied)))

    return PolicySummary(
        dependency_count=len(packages),
        unresolved_license_count=unresolved,
    )


def load_report(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        report = json.load(handle)
    if not isinstance(report, dict):
        raise PolicyError("OSV report root must be an object")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="OSV JSON report to validate")
    args = parser.parse_args(argv)

    try:
        summary = evaluate_report(load_report(args.report))
    except (OSError, json.JSONDecodeError, PolicyError) as error:
        print(f"OSV policy failed: {error}", file=sys.stderr)
        return 1

    print(
        "OSV policy passed: "
        f"{summary.dependency_count} locked dependencies inventoried; "
        f"{summary.unresolved_license_count} have unresolved/non-SPDX metadata."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
