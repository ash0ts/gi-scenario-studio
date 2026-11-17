#!/usr/bin/env python3
"""Prove that the EcoCorridor branch did not alter GI domain or release assets."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from ast import literal_eval
from collections import Counter
from collections.abc import Iterable
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
SECURITY_MIGRATION_REMOVED_REQUIREMENTS = frozenset(
    {
        "presidio-anonymizer>=2.2.359,<3",
    }
)
SECURITY_MIGRATION_REMOVED_LOCK_PACKAGES = frozenset({"presidio-anonymizer"})
EXPECTED_RELEASE_SECURITY_DEPENDENCIES = ["PyNaCl==1.6.2", "pyinstaller==6.21.0"]
EXPECTED_WEAVE_TRANSITIVE_CRYPTOGRAPHY = "48.0.1"
EXPECTED_WEAVE_CRYPTOGRAPHY_CHAIN = (
    ("weave", "polyfile-weave"),
    ("polyfile-weave", "pdfminer-six"),
    ("pdfminer-six", "cryptography"),
)
REVIEWED_GI_PNPM_OPTIONAL_PEER_ADDITIONS = {
    "vite@6.4.3": {"@types/node": "22.20.1"},
    "vitest@3.2.7": {"@types/node": "22.20.1", "jsdom": "26.1.0"},
}
REVIEWED_GI_PNPM_PEER_CONTEXT_KEYS = {
    "@vitejs/plugin-react@4.7.0": ("@vitejs/plugin-react@4.7.0(vite@6.4.3(@types/node@22.20.1))"),
    "@vitest/mocker@3.2.7": "@vitest/mocker@3.2.7(vite@6.4.3(@types/node@22.20.1))",
    "vite-node@3.2.4": "vite-node@3.2.4(@types/node@22.20.1)",
    "vite@6.4.3": "vite@6.4.3(@types/node@22.20.1)",
    "vitest@3.2.7": "vitest@3.2.7(@types/node@22.20.1)(jsdom@26.1.0)",
}
REVIEWED_GI_PNPM_ADDITIVE_ROOTS = ("@types/node@22.20.1", "jsdom@26.1.0")
# Filled from the exact package/snapshot records reachable from the reviewed
# optional-peer roots. Updating it requires an explicit shared-platform review.
REVIEWED_GI_PNPM_ADDITIVE_CLOSURE_SHA256 = (
    "bae7ebfab9957e9d88052dffb61f62e6c166cd0aa94ae91ed7cbb11927c417a3"
)
REVIEWED_PNPM_WORKSPACE = """packages:
  - apps/*
  - packages/*
allowBuilds:
  esbuild: true
"""
PNPM_LIFECYCLE_SCRIPTS = (
    "preinstall",
    "install",
    "postinstall",
    "prepare",
    "prepublish",
    "prepublishOnly",
    "prepack",
    "postpack",
)


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


def _root_field_block(text: str, key: str) -> str | None:
    """Return a root scalar or mapping block from a deterministic YAML document."""

    lines = text.splitlines(keepends=True)
    marker = f"{key}:"
    try:
        start = next(
            index
            for index, line in enumerate(lines)
            if line.rstrip("\n") == marker or line.startswith(f"{marker} ")
        )
    except StopIteration:
        return None
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip() and not lines[index].startswith(" ")
        ),
        len(lines),
    )
    return "".join(lines[start:end]).rstrip() + "\n"


def _normalize_pnpm_importer(block: str | None) -> str | None:
    """Ignore only peer-context suffixes while preserving package versions and specifiers."""

    if block is None:
        return None
    if "(patch_hash=" in block:
        return block
    return re.sub(r"^(\s+version: [^\s(]+)(?:\([^\n]+\))$", r"\1", block, flags=re.MULTILINE)


def _yaml_scalar(value: str) -> str:
    """Decode the quoted/plain scalar subset emitted by pnpm's lockfile writer."""

    value = value.strip()
    if value.startswith(("'", '"')):
        decoded = literal_eval(value)
        if not isinstance(decoded, str):
            raise ValueError(f"expected a string scalar, received {value!r}")
        return decoded
    return value


def _section_records(text: str, section: str) -> dict[str, str]:
    """Return exact two-space records from a pnpm lockfile root mapping.

    This intentionally parses only the deterministic indentation and scalar subset
    pnpm emits.  The release gate is also run before PyYAML is installed, so using a
    general YAML parser here would create an undeclared bootstrap dependency.
    """

    lines = text.splitlines(keepends=True)
    try:
        section_index = next(index for index, line in enumerate(lines) if line == f"{section}:\n")
    except StopIteration:
        return {}
    section_end = next(
        (
            index
            for index in range(section_index + 1, len(lines))
            if lines[index].strip() and not lines[index].startswith(" ")
        ),
        len(lines),
    )
    starts = [
        index
        for index in range(section_index + 1, section_end)
        if lines[index].startswith("  ")
        and not lines[index].startswith("    ")
        and (lines[index].rstrip("\n").endswith(":") or lines[index].rstrip("\n").endswith(": {}"))
    ]
    records: dict[str, str] = {}
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else section_end
        marker = lines[start].strip()
        key = _yaml_scalar(marker[: -len(": {}")] if marker.endswith(": {}") else marker[:-1])
        if key in records:
            raise ValueError(f"duplicate {section} lock record: {key}")
        records[key] = "".join(lines[start:end]).rstrip() + "\n"
    return records


def _record_fields(block: str) -> dict[str, str]:
    """Split one two-space record into its exact four-space field blocks."""

    lines = block.splitlines(keepends=True)
    starts = [
        index
        for index in range(1, len(lines))
        if lines[index].startswith("    ")
        and not lines[index].startswith("      ")
        and ":" in lines[index]
    ]
    fields: dict[str, str] = {}
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        key = _yaml_scalar(lines[start].strip().split(":", 1)[0])
        if key in fields:
            raise ValueError(f"duplicate lock-record field: {key}")
        fields[key] = "".join(lines[start:end]).rstrip() + "\n"
    return fields


def _dependency_edges(field: str | None) -> dict[str, str]:
    """Parse a pnpm dependencies/optionalDependencies field into exact edges."""

    if field is None:
        return {}
    edges: dict[str, str] = {}
    for line in field.splitlines()[1:]:
        if not line.strip():
            continue
        if not line.startswith("      ") or line.startswith("        "):
            raise ValueError(f"unsupported nested pnpm dependency record: {line.strip()!r}")
        key_text, separator, value_text = line.strip().partition(": ")
        if not separator:
            raise ValueError(f"invalid pnpm dependency edge: {line.strip()!r}")
        key = _yaml_scalar(key_text)
        value = _yaml_scalar(value_text)
        if key in edges:
            raise ValueError(f"duplicate pnpm dependency edge: {key}")
        edges[key] = value
    return edges


def _optional_peer_names(field: str | None) -> set[str]:
    """Return only peerDependenciesMeta entries explicitly marked optional."""

    if field is None:
        return set()
    optional: set[str] = set()
    current: str | None = None
    for line in field.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if line.startswith("      ") and not line.startswith("        "):
            if not stripped.endswith(":"):
                raise ValueError(f"invalid pnpm peer metadata entry: {stripped!r}")
            current = _yaml_scalar(stripped[:-1])
            continue
        if line.startswith("        optional: ") and current is not None:
            if stripped == "optional: true":
                optional.add(current)
            elif stripped != "optional: false":
                raise ValueError(f"invalid pnpm optional-peer marker: {stripped!r}")
            continue
        raise ValueError(f"unsupported pnpm peer metadata record: {stripped!r}")
    return optional


def _pnpm_importer_versions(block: str) -> list[tuple[str, str]]:
    """Read package/version roots from one pnpm importer block."""

    roots: list[tuple[str, str]] = []
    section: str | None = None
    package: str | None = None
    allowed_sections = {"dependencies", "devDependencies", "optionalDependencies"}
    for line in block.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if line.startswith("    ") and not line.startswith("      ") and stripped.endswith(":"):
            section = _yaml_scalar(stripped[:-1])
            package = None
            continue
        if section not in allowed_sections:
            continue
        if line.startswith("      ") and not line.startswith("        ") and stripped.endswith(":"):
            package = _yaml_scalar(stripped[:-1])
            continue
        if line.startswith("        version: ") and package is not None:
            roots.append((package, _yaml_scalar(stripped.removeprefix("version: "))))
    return roots


def _strip_peer_context(version_or_key: str) -> str:
    """Remove pnpm's resolved peer suffix without weakening the package version."""

    return version_or_key.split("(", 1)[0]


def _is_external_pnpm_version(version: str) -> bool:
    return not version.startswith(("link:", "workspace:", "file:"))


def _pnpm_snapshot_closure(
    roots: Iterable[str], snapshots: dict[str, str]
) -> tuple[set[str], list[str]]:
    pending = list(roots)
    reachable: set[str] = set()
    missing: list[str] = []
    while pending:
        key = pending.pop()
        if key in reachable:
            continue
        reachable.add(key)
        block = snapshots.get(key)
        if block is None:
            missing.append(key)
            continue
        fields = _record_fields(block)
        for field_name in ("dependencies", "optionalDependencies"):
            for name, version in _dependency_edges(fields.get(field_name)).items():
                if _is_external_pnpm_version(version):
                    pending.append(f"{name}@{version}")
    return reachable, sorted(missing)


def _pnpm_reachable_snapshots(
    importer: str, snapshots: dict[str, str]
) -> tuple[set[str], list[str]]:
    """Resolve the exact dependency graph reachable from a protected importer."""

    roots = [
        f"{name}@{version}"
        for name, version in _pnpm_importer_versions(importer)
        if _is_external_pnpm_version(version)
    ]
    return _pnpm_snapshot_closure(roots, snapshots)


def _pnpm_closure_sha256(
    roots: Iterable[str], packages: dict[str, str], snapshots: dict[str, str]
) -> str:
    """Hash every exact package/snapshot record reachable from approved roots."""

    reachable, missing = _pnpm_snapshot_closure(roots, snapshots)
    if missing:
        raise ValueError("approved pnpm additive closure is incomplete: " + ", ".join(missing))
    package_records: dict[str, str] = {}
    for key in reachable:
        canonical = _strip_peer_context(key)
        record = packages.get(canonical)
        if record is None:
            raise ValueError(f"approved pnpm additive package record is missing: {canonical}")
        package_records[canonical] = record
    payload = {
        "roots": sorted(roots),
        "packages": dict(sorted(package_records.items())),
        "snapshots": {key: snapshots[key] for key in sorted(reachable)},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _pnpm_gi_closure_issues(base_text: str, current_text: str) -> list[str]:
    """Protect every package and edge reachable from the immutable GI importer.

    Eco workspaces may add packages and satisfy previously absent optional peers.
    They may not remove or change a GI edge, package version, integrity, engine
    constraint, or other resolved package metadata.
    """

    issues: list[str] = []
    for root_field in ("lockfileVersion", "settings"):
        if _root_field_block(base_text, root_field) != _root_field_block(current_text, root_field):
            issues.append(f"shared pnpm lock {root_field} drifted")
    base_importer = _mapping_block(base_text, "importers", "apps/desktop")
    current_importer = _mapping_block(current_text, "importers", "apps/desktop")
    if not base_importer or not current_importer:
        return ["GI desktop pnpm importer or reachable closure is missing"]

    base_packages = _section_records(base_text, "packages")
    current_packages = _section_records(current_text, "packages")
    base_snapshots = _section_records(base_text, "snapshots")
    current_snapshots = _section_records(current_text, "snapshots")
    base_reachable, base_missing = _pnpm_reachable_snapshots(base_importer, base_snapshots)
    current_reachable, current_missing = _pnpm_reachable_snapshots(
        current_importer, current_snapshots
    )
    if "(patch_hash=" in current_importer or any(
        "(patch_hash=" in key for key in current_reachable
    ):
        issues.append("GI pnpm closure contains an unreviewed patched dependency")
    if base_missing:
        issues.append(
            "immutable GI pnpm closure is internally incomplete: " + ", ".join(base_missing)
        )
    if current_missing:
        issues.append(
            "current GI pnpm closure is internally incomplete: " + ", ".join(current_missing)
        )

    def by_canonical(keys: Iterable[str]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for key in keys:
            result.setdefault(_strip_peer_context(key), []).append(key)
        return {key: sorted(values) for key, values in result.items()}

    base_by_canonical = by_canonical(base_reachable)
    current_by_canonical = by_canonical(current_reachable)
    for canonical, base_keys in sorted(base_by_canonical.items()):
        current_keys = current_by_canonical.get(canonical, [])
        if len(base_keys) != 1 or len(current_keys) != 1:
            issues.append(
                "GI pnpm closure has missing or ambiguous peer resolution for "
                f"{canonical}: base={base_keys!r}, current={current_keys!r}"
            )
            continue
        base_key = base_keys[0]
        current_key = current_keys[0]
        expected_current_key = REVIEWED_GI_PNPM_PEER_CONTEXT_KEYS.get(canonical, base_key)
        if current_key != expected_current_key:
            issues.append(
                "GI pnpm resolved peer context drifted: "
                f"{canonical}: expected {expected_current_key}, found {current_key}"
            )
        if base_key not in base_snapshots or current_key not in current_snapshots:
            issues.append(f"GI pnpm snapshot record disappeared: {canonical}")
            continue
        base_package = base_packages.get(canonical)
        current_package = current_packages.get(canonical)
        if base_package is None or current_package is None:
            issues.append(f"GI pnpm package record disappeared: {canonical}")
        elif base_package != current_package:
            issues.append(f"GI pnpm package metadata or integrity drifted: {canonical}")

        base_fields = _record_fields(base_snapshots[base_key])
        current_fields = _record_fields(current_snapshots[current_key])
        for field_name, base_field in base_fields.items():
            if field_name in {"dependencies", "optionalDependencies"}:
                base_edges = _dependency_edges(base_field)
                current_edges = _dependency_edges(current_fields.get(field_name))
                for dependency, base_version in sorted(base_edges.items()):
                    current_version = current_edges.get(dependency)
                    if current_version is None or _strip_peer_context(
                        current_version
                    ) != _strip_peer_context(base_version):
                        issues.append(
                            "GI pnpm dependency edge drifted: "
                            f"{canonical} -> {dependency}@{base_version}"
                        )
            elif current_fields.get(field_name) != base_field:
                issues.append(f"GI pnpm snapshot metadata drifted: {canonical}.{field_name}")

        base_dependencies = _dependency_edges(base_fields.get("dependencies"))
        current_dependencies = _dependency_edges(current_fields.get("dependencies"))
        unexpected_dependencies = sorted(set(current_dependencies) - set(base_dependencies))
        for dependency in unexpected_dependencies:
            issues.append(
                "GI pnpm protected snapshot gained a dependency edge: "
                f"{canonical} -> {dependency}@{current_dependencies[dependency]}"
            )

        base_optional = _dependency_edges(base_fields.get("optionalDependencies"))
        current_optional = _dependency_edges(current_fields.get("optionalDependencies"))
        added_optional = {
            dependency: version
            for dependency, version in current_optional.items()
            if dependency not in base_optional
        }
        reviewed_optional = REVIEWED_GI_PNPM_OPTIONAL_PEER_ADDITIONS.get(canonical, {})
        if added_optional != reviewed_optional:
            issues.append(
                "GI pnpm optional-peer additions differ from the reviewed projection for "
                f"{canonical}: expected={reviewed_optional!r}, current={added_optional!r}"
            )
        if reviewed_optional:
            package_fields = _record_fields(base_package or "")
            peer_dependencies = _dependency_edges(package_fields.get("peerDependencies"))
            optional_peers = _optional_peer_names(package_fields.get("peerDependenciesMeta"))
            for dependency, version in sorted(reviewed_optional.items()):
                if dependency not in peer_dependencies or dependency not in optional_peers:
                    issues.append(
                        f"GI pnpm reviewed addition is not declared as an optional peer: "
                        f"{canonical} -> {dependency}"
                    )
                if f"({dependency}@{version})" not in current_key:
                    issues.append(
                        "GI pnpm optional peer is absent from the resolved peer context: "
                        f"{canonical} -> {dependency}@{version}"
                    )

        allowed_field_additions = {"optionalDependencies"} if reviewed_optional else set()
        for field_name in sorted(set(current_fields) - set(base_fields)):
            if field_name not in allowed_field_additions:
                issues.append(
                    f"GI pnpm protected snapshot gained metadata: {canonical}.{field_name}"
                )

    additive_reachable, additive_missing = _pnpm_snapshot_closure(
        REVIEWED_GI_PNPM_ADDITIVE_ROOTS,
        current_snapshots,
    )
    if additive_missing:
        issues.append("reviewed GI pnpm optional-peer closure is incomplete")
    else:
        unreviewed_reachable = sorted(
            key
            for key in current_reachable
            if _strip_peer_context(key) not in base_by_canonical and key not in additive_reachable
        )
        if unreviewed_reachable:
            issues.append(
                "GI pnpm closure gained unreviewed reachable records: "
                + ", ".join(unreviewed_reachable)
            )
        additive_digest = _pnpm_closure_sha256(
            REVIEWED_GI_PNPM_ADDITIVE_ROOTS,
            current_packages,
            current_snapshots,
        )
        if additive_digest != REVIEWED_GI_PNPM_ADDITIVE_CLOSURE_SHA256:
            issues.append(
                "reviewed GI pnpm optional-peer closure bytes drifted: "
                f"expected {REVIEWED_GI_PNPM_ADDITIVE_CLOSURE_SHA256}, found {additive_digest}"
            )
    return issues


def _lock_record_name(record: str) -> str:
    return str(json.loads(record).get("name", "unknown"))


def _missing_locked_base_names(
    base_records: Counter[str], current_records: Counter[str]
) -> list[str]:
    """Return drift after allowing only the named shared security migration."""

    return sorted(
        {
            _lock_record_name(record)
            for record in (base_records - current_records).elements()
            if _lock_record_name(record) not in SECURITY_MIGRATION_REMOVED_LOCK_PACKAGES
        }
    )


def dependency_isolation_issues() -> list[str]:
    """Allow Eco additions while preserving every pre-existing GI dependency record."""

    issues: list[str] = []
    try:
        base_project = tomllib.loads(_base_file("pyproject.toml"))
        current_project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        base_package = json.loads(_base_file("package.json"))
        current_package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        current_workspace = (ROOT / "pnpm-workspace.yaml").read_text(encoding="utf-8")
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
        expected_requirements = requirements
        if name == "observability":
            expected_requirements = [
                requirement
                for requirement in requirements
                if requirement not in SECURITY_MIGRATION_REMOVED_REQUIREMENTS
            ]
        if current_optional.get(name) != expected_requirements:
            issues.append(f"GI Python optional dependency group drifted: {name}")
    if current_optional.get("release") != EXPECTED_RELEASE_SECURITY_DEPENDENCIES:
        issues.append("shared release security dependency group drifted")
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
    for script_name in PNPM_LIFECYCLE_SCRIPTS:
        if current_package.get("scripts", {}).get(script_name) != base_package.get(
            "scripts", {}
        ).get(script_name):
            issues.append(f"shared root lifecycle script drifted: {script_name}")
    if current_package.get("pnpm") != base_package.get("pnpm"):
        issues.append("shared root pnpm overrides, patches, or package extensions drifted")
    if current_workspace != REVIEWED_PNPM_WORKSPACE:
        issues.append("shared pnpm workspace/install policy drifted from its reviewed projection")
    npmrc = ROOT / ".npmrc"
    if npmrc.exists() or npmrc.is_symlink():
        issues.append("repository .npmrc is not part of the reviewed shared install policy")

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
    names = _missing_locked_base_names(base_records, current_records)
    if names:
        issues.append("existing GI Python lock records drifted: " + ", ".join(names))
    current_records_by_name: dict[str, list[dict[str, object]]] = {}
    for record in current_records:
        decoded = json.loads(record)
        current_records_by_name.setdefault(str(decoded.get("name", "unknown")), []).append(decoded)
    forbidden_present = sorted(
        SECURITY_MIGRATION_REMOVED_LOCK_PACKAGES & current_records_by_name.keys()
    )
    if forbidden_present:
        issues.append(
            "removed shared security dependencies remain in the lock: "
            + ", ".join(forbidden_present)
        )
    pynacl_records = current_records_by_name.get("pynacl", [])
    if len(pynacl_records) != 1 or pynacl_records[0].get("version") != "1.6.2":
        issues.append("shared release signing dependency must lock exactly PyNaCl 1.6.2")
    cryptography_records = current_records_by_name.get("cryptography", [])
    if (
        len(cryptography_records) != 1
        or cryptography_records[0].get("version") != EXPECTED_WEAVE_TRANSITIVE_CRYPTOGRAPHY
    ):
        issues.append(
            "Weave's transitive cryptography record drifted and requires explicit security review"
        )
    for parent, child in EXPECTED_WEAVE_CRYPTOGRAPHY_CHAIN:
        records = current_records_by_name.get(parent, [])
        dependency_names = {
            str(dependency.get("name", "unknown"))
            for record in records
            for dependency in record.get("dependencies", [])
            if isinstance(dependency, dict)
        }
        if len(records) != 1 or child not in dependency_names:
            issues.append(f"reviewed Weave transitive security chain drifted: {parent} -> {child}")

    base_desktop_importer = _mapping_block(base_pnpm, "importers", "apps/desktop")
    current_desktop_importer = _mapping_block(current_pnpm, "importers", "apps/desktop")
    if not base_desktop_importer or _normalize_pnpm_importer(
        base_desktop_importer
    ) != _normalize_pnpm_importer(current_desktop_importer):
        issues.append("GI desktop pnpm importer drifted")
    try:
        issues.extend(_pnpm_gi_closure_issues(base_pnpm, current_pnpm))
    except (SyntaxError, ValueError) as exc:
        issues.append(f"GI desktop pnpm reachable closure could not be verified: {exc}")
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
