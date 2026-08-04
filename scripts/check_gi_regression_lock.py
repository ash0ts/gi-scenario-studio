#!/usr/bin/env python3
"""Prove that the EcoCorridor branch did not alter GI domain or release assets."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "28d8631986e07fc53872706dd12b7dcdb55359aa"
LOCKED_PATHS = (
    ".github/workflows/ci.yml",
    "apps/desktop",
    "benchmarks/cc0_reference",
    "benchmarks/exhaustive_exact",
    "benchmarks/guided_exact",
    "engine",
    "python/gi_scenario_core",
    "scripts/generate_exhaustive_benchmark.py",
    "scripts/generate_guided_fixture.py",
    "scripts/generate_guided_ui_fixture.py",
    "scripts/generate_schemas.py",
    "schemas/v1.0",
    "schemas/v2.0",
)

EXPECTED_GI_IDENTIFIER = "org.giscenariostudio.desktop"
EXPECTED_ECO_IDENTIFIER = "org.ecocorridor.scenariostudio"
EXPECTED_GI_KEYCHAIN_SERVICE = "com.gi-scenario-studio.wandb"
EXPECTED_ECO_KEYCHAIN_SERVICE = "org.ecocorridor.scenariostudio.credentials"
EXPECTED_ECO_APP_DATA_ROOT = "EcoCorridorScenarioStudio"


def git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _base_file(path: str) -> str:
    result = git("show", f"{BASE_SHA}:{path}")
    if result.returncode:
        raise RuntimeError(f"immutable base does not contain {path}")
    return result.stdout


def _mapping_block(text: str, section: str, key: str) -> str | None:
    """Return one exact two-space YAML mapping block from a named root section."""

    lines = text.splitlines(keepends=True)
    try:
        section_index = next(index for index, line in enumerate(lines) if line == f"{section}:\n")
    except StopIteration:
        return None
    section_end = next(
        (
            index
            for index in range(section_index + 1, len(lines))
            if lines[index].strip() and not lines[index].startswith(" ")
        ),
        len(lines),
    )
    marker = f"  {key}:"
    try:
        start = next(
            index
            for index in range(section_index + 1, section_end)
            if lines[index].rstrip("\n") == marker
        )
    except StopIteration:
        return None
    end = next(
        (
            index
            for index in range(start + 1, section_end)
            if lines[index].startswith("  ") and not lines[index].startswith("    ")
        ),
        section_end,
    )
    return "".join(lines[start:end])


def _normalize_pnpm_importer(block: str | None) -> str | None:
    """Ignore only peer-context suffixes while preserving package versions and specifiers."""

    if block is None:
        return None
    return re.sub(r"^(\s+version: [^\s(]+)(?:\([^\n]+\))$", r"\1", block, flags=re.MULTILINE)


def dependency_isolation_issues() -> list[str]:
    """Allow Eco additions while preserving every pre-existing GI dependency record."""

    issues: list[str] = []
    try:
        base_project = tomllib.loads(_base_file("pyproject.toml"))
        current_project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        base_package = json.loads(_base_file("package.json"))
        current_package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        base_uv = tomllib.loads(_base_file("uv.lock"))
        current_uv = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        base_pnpm = _base_file("pnpm-lock.yaml")
        current_pnpm = (ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8")
    except (OSError, RuntimeError, ValueError, tomllib.TOMLDecodeError) as exc:
        return [f"GI dependency projection could not be read: {exc}"]

    for key in ("name", "version", "description", "requires-python", "license", "authors"):
        if base_project["project"].get(key) != current_project["project"].get(key):
            issues.append(f"root Python project field drifted for existing GI key: {key}")
    if base_project["project"].get("dependencies") != current_project["project"].get(
        "dependencies"
    ):
        issues.append("GI base Python dependency list drifted")
    base_optional = base_project["project"].get("optional-dependencies", {})
    current_optional = current_project["project"].get("optional-dependencies", {})
    for name, requirements in base_optional.items():
        if current_optional.get(name) != requirements:
            issues.append(f"GI Python optional dependency group drifted: {name}")
    base_scripts = base_project["project"].get("scripts", {})
    current_scripts = current_project["project"].get("scripts", {})
    for name, command in base_scripts.items():
        if current_scripts.get(name) != command:
            issues.append(f"GI Python entry point drifted: {name}")
    base_build_packages = (
        base_project.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages", [])
    )
    current_build_packages = (
        current_project.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages", [])
    )
    if any(package not in current_build_packages for package in base_build_packages):
        issues.append("GI Python wheel package disappeared from the shared build")

    for key in ("name", "private", "version", "packageManager"):
        if base_package.get(key) != current_package.get(key):
            issues.append(f"root JavaScript project field drifted for existing GI key: {key}")
    for section in ("scripts", "dependencies", "devDependencies", "peerDependencies"):
        for name, value in base_package.get(section, {}).items():
            if current_package.get(section, {}).get(name) != value:
                issues.append(f"GI root package {section} entry drifted: {name}")

    def package_records(payload: dict[str, object]) -> Counter[str]:
        records = payload.get("package", [])
        assert isinstance(records, list)
        return Counter(
            json.dumps(record, sort_keys=True, separators=(",", ":"))
            for record in records
            if isinstance(record, dict) and record.get("name") != base_project["project"]["name"]
        )

    base_records = package_records(base_uv)
    current_records = package_records(current_uv)
    missing_records = base_records - current_records
    if missing_records:
        names = sorted(
            {
                str(json.loads(record).get("name", "unknown"))
                for record in missing_records.elements()
            }
        )
        issues.append("existing GI Python lock records drifted: " + ", ".join(names))

    base_desktop_importer = _mapping_block(base_pnpm, "importers", "apps/desktop")
    current_desktop_importer = _mapping_block(current_pnpm, "importers", "apps/desktop")
    if not base_desktop_importer or _normalize_pnpm_importer(
        base_desktop_importer
    ) != _normalize_pnpm_importer(current_desktop_importer):
        issues.append("GI desktop pnpm importer drifted")
    return issues


def app_identity_issues() -> list[str]:
    """Return cross-application identity collisions or unexpected drift."""

    gi_config = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())
    eco_config = json.loads(
        (ROOT / "apps/ecocorridor-desktop/src-tauri/tauri.conf.json").read_text()
    )
    gi_credentials = (ROOT / "apps/desktop/src-tauri/src/credential.rs").read_text()
    eco_profile = (ROOT / "python/ecocorridor_core/observability.py").read_text()
    gi_observability = (ROOT / "python/gi_scenario_core/observability.py").read_text()

    issues: list[str] = []
    gi_identifier = gi_config.get("identifier")
    eco_identifier = eco_config.get("identifier")
    if gi_identifier != EXPECTED_GI_IDENTIFIER:
        issues.append("GI bundle identifier drifted from its immutable release identity")
    if eco_identifier != EXPECTED_ECO_IDENTIFIER:
        issues.append("EcoCorridor bundle identifier does not match its release identity")
    if gi_identifier == eco_identifier:
        issues.append("GI and EcoCorridor bundle identifiers collide")

    gi_url = gi_config.get("build", {}).get("devUrl")
    eco_url = eco_config.get("build", {}).get("devUrl")
    if not gi_url or not eco_url or gi_url == eco_url:
        issues.append("GI and EcoCorridor development origins must be distinct")

    if EXPECTED_GI_KEYCHAIN_SERVICE not in gi_credentials:
        issues.append("GI keychain service drifted from its immutable release identity")
    if EXPECTED_ECO_KEYCHAIN_SERVICE not in eco_profile:
        issues.append("EcoCorridor keychain service is missing from its application profile")
    if EXPECTED_GI_KEYCHAIN_SERVICE in eco_profile:
        issues.append("EcoCorridor reuses the GI keychain service")
    if EXPECTED_ECO_APP_DATA_ROOT not in eco_profile:
        issues.append("EcoCorridor app-data root is missing from its application profile")
    if 'namespace="ecocorridor"' not in eco_profile:
        issues.append("EcoCorridor W&B/Weave namespace is not isolated")
    if "gi-v2|" not in gi_observability:
        issues.append("GI observability namespace drifted from the immutable baseline")
    return issues


def main() -> int:
    if git("cat-file", "-e", f"{BASE_SHA}^{{commit}}").returncode:
        print(f"GI regression lock cannot resolve immutable base {BASE_SHA}.")
        return 2
    if git("merge-base", "--is-ancestor", BASE_SHA, "HEAD").returncode:
        print(f"GI regression lock requires HEAD to descend from {BASE_SHA[:12]}.")
        return 2

    tracked = git("diff", "--name-only", BASE_SHA, "--", *LOCKED_PATHS)
    untracked = git("ls-files", "--others", "--exclude-standard", "--", *LOCKED_PATHS)
    if tracked.returncode or untracked.returncode:
        print("GI regression lock could not inspect the repository diff.")
        return 2
    changed = sorted(
        {
            line.strip()
            for output in (tracked.stdout, untracked.stdout)
            for line in output.splitlines()
            if line.strip()
        }
    )
    if changed:
        print("EcoCorridor isolation failed; locked GI files changed:")
        for path in changed:
            print(f"  {path}")
        return 1

    identity_issues = app_identity_issues()
    if identity_issues:
        print("EcoCorridor application identity isolation failed:")
        for issue in identity_issues:
            print(f"  {issue}")
        return 1
    dependency_issues = dependency_isolation_issues()
    if dependency_issues:
        print("EcoCorridor shared dependency isolation failed:")
        for issue in dependency_issues:
            print(f"  {issue}")
        return 1
    print(f"GI regression lock passed against {BASE_SHA[:12]}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
