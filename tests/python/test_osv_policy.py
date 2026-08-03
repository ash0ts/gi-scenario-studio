from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_osv_policy.py"
SPEC = importlib.util.spec_from_file_location("check_osv_policy", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
PolicyError = MODULE.PolicyError
evaluate_report = MODULE.evaluate_report


def _report(license_expression: str = "MIT") -> dict[str, object]:
    return {
        "experimental_config": {"licenses": {"summary": True, "allowlist": []}},
        "results": [
            {
                "source": {"path": "/workspace/uv.lock", "type": "lockfile"},
                "packages": [
                    {
                        "package": {
                            "ecosystem": "PyPI",
                            "name": "example",
                            "version": "1.0.0",
                        },
                        "licenses": [license_expression],
                    }
                ],
            },
            {
                "source": {
                    "path": "/workspace/pnpm-lock.yaml",
                    "type": "lockfile",
                },
                "packages": [],
            },
            {
                "source": {
                    "path": "/workspace/apps/desktop/src-tauri/Cargo.lock",
                    "type": "lockfile",
                },
                "packages": [],
            },
        ],
    }


def test_osv_policy_accepts_complete_permissive_inventory() -> None:
    summary = evaluate_report(_report())

    assert summary.dependency_count == 1
    assert summary.unresolved_license_count == 0


@pytest.mark.parametrize(
    "license_expression",
    [
        "AGPL-3.0-only",
        "MIT OR AGPL-3.0-or-later",
        "LicenseRef-SSPL-1.0",
    ],
)
def test_osv_policy_rejects_forbidden_license_identifiers(
    license_expression: str,
) -> None:
    with pytest.raises(PolicyError, match="forbidden dependency licenses"):
        evaluate_report(_report(license_expression))


def test_osv_policy_rejects_incomplete_lockfile_coverage() -> None:
    report = _report()
    results = report["results"]
    assert isinstance(results, list)
    results.pop()

    with pytest.raises(PolicyError, match="Cargo.lock"):
        evaluate_report(report)


def test_osv_policy_reports_unresolved_metadata_without_hiding_it() -> None:
    summary = evaluate_report(_report("non-standard"))

    assert summary.unresolved_license_count == 1
