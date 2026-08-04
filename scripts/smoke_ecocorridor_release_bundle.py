#!/usr/bin/env python3
"""Exercise a staged worker and packaged Circuitscape application offline.

The release workflow runs this before Tauri bundling on every target.  It uses
only synthetic files created in a temporary directory and passes no cloud or
model-provider credentials to either process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

APPLICATION_ID = "org.ecocorridor.scenariostudio"
RELEASE_MANIFEST_SCHEMA = "1.1"
ECOLOGICAL_DATA_MANIFEST_SCHEMA = "1.1"
SMOKE_DISPOSITION_SCHEMA = "ecocorridor-packaged-boundary-smoke/v1.0"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
TARGETS = {"linux-x64", "macos-arm64", "macos-x64", "windows-x64"}
MAX_SIGNED_RESOURCES = 100_000
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
PACKAGED_RUNTIME_PACKAGES = frozenset(
    {
        "geopandas",
        "numpy",
        "pydantic",
        "pyogrio",
        "rasterio",
        "scikit-learn",
        "scipy",
        "shapely",
        "wandb",
        "weave",
    }
)
STAGED_RUNTIME_PACKAGES = PACKAGED_RUNTIME_PACKAGES | {"pyinstaller"}
PACKAGED_RUNTIME_CAPABILITIES = frozenset(
    {
        "pydantic_contracts",
        "scipy_sobol",
        "sklearn_extratrees",
        "wandb_offline_table_artifact",
        "weave_entrypoint_redaction",
    }
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def digest_tree(root: Path) -> str:
    """Match the public EngineManifest tree digest over final staged bytes."""

    value = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if path.is_symlink():
            raise SystemExit("The staged Circuitscape engine tree contains a symlink.")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        value.update(len(relative).to_bytes(8, "big"))
        value.update(relative)
        value.update(bytes.fromhex(digest(path)))
    return value.hexdigest()


def _safe_relative(value: object) -> str:
    if not isinstance(value, str):
        raise SystemExit("Signed resource paths must be strings.")
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise SystemExit(f"Signed resource path is unsafe: {value!r}")
    return value


def _read_public_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SystemExit("Resource verification key must be a regular non-symlink file.")
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit("Resource verification key is unreadable.") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SystemExit("Resource verification key must be one raw Ed25519 key in lowercase hex.")
    return bytes.fromhex(value)


def verify_signed_release_inventory(
    release_root: Path,
    public_key: Path,
    *,
    expected_version: str,
    expected_source_commit: str,
    expected_target: str,
) -> dict[str, object]:
    """Verify signature, release identity, and every staged byte before execution."""

    if (
        not SEMVER.fullmatch(expected_version)
        or not SOURCE_COMMIT.fullmatch(expected_source_commit)
        or expected_target not in TARGETS
    ):
        raise SystemExit("Expected release identity is incomplete or malformed.")
    manifest_path = release_root / "release-manifest.json"
    signature_path = release_root / "release-manifest.sig"
    for path in (manifest_path, signature_path):
        if path.is_symlink() or not path.is_file():
            raise SystemExit("The detached signed resource inventory is incomplete.")
    manifest_bytes = manifest_path.read_bytes()
    if not manifest_bytes or len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise SystemExit("The signed resource inventory exceeds its byte policy.")
    signature = signature_path.read_bytes()
    if len(signature) != 64:
        raise SystemExit("The detached resource signature has an invalid length.")
    try:
        VerifyKey(_read_public_key(public_key)).verify(manifest_bytes, signature)
    except (BadSignatureError, ValueError) as exc:
        raise SystemExit("The detached resource signature is invalid.") from exc
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("The signed resource inventory is invalid JSON.") from exc
    if not isinstance(manifest, dict):
        raise SystemExit("The signed resource inventory must be a JSON object.")
    if (
        manifest.get("schema_version") != RELEASE_MANIFEST_SCHEMA
        or manifest.get("application_id") != APPLICATION_ID
        or manifest.get("application_version") != expected_version
        or manifest.get("source_commit_sha") != expected_source_commit
        or manifest.get("target") != expected_target
    ):
        raise SystemExit("The signed resource inventory belongs to another release identity.")
    records = manifest.get("resources")
    if not isinstance(records, list) or not 0 < len(records) <= MAX_SIGNED_RESOURCES:
        raise SystemExit("The signed resource inventory has an invalid resource count.")
    expected: dict[str, tuple[int, str, bool]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise SystemExit("The signed resource inventory contains a malformed record.")
        relative = _safe_relative(record.get("path"))
        size = record.get("bytes")
        sha256 = record.get("sha256")
        executable = record.get("executable")
        if (
            relative in expected
            or not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or not SHA256.fullmatch(sha256)
            or not isinstance(executable, bool)
        ):
            raise SystemExit(f"The signed resource inventory record is invalid: {relative}")
        expected[relative] = (size, sha256, executable)

    actual: set[str] = set()
    for directory, names, filenames in os.walk(release_root, followlinks=False):
        for name in names:
            candidate = Path(directory) / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise SystemExit(f"Signed resource tree has an unsafe directory: {name}")
        for name in filenames:
            candidate = Path(directory) / name
            relative = candidate.relative_to(release_root).as_posix()
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise SystemExit(f"Signed resource tree has an unsafe file: {relative}")
            if relative in {"release-manifest.json", "release-manifest.sig"}:
                continue
            actual.add(relative)
            if relative not in expected:
                raise SystemExit(f"Signed resource tree contains an unlisted file: {relative}")
            size, sha256, _ = expected[relative]
            if candidate.stat().st_size != size or digest(candidate) != sha256:
                raise SystemExit(f"Signed resource differs from its inventory: {relative}")
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        raise SystemExit(f"Signed resource tree is incomplete: {missing}")

    suffix = ".exe" if expected_target == "windows-x64" else ""
    required_executables = {
        f"bin/ecocorridor-worker{suffix}",
        f"engine/bin/ecocorridor-engine{suffix}",
    }
    declared_executables = {path for path, facts in expected.items() if facts[2]}
    if declared_executables != required_executables:
        raise SystemExit("The signed inventory has an unexpected executable boundary.")
    return manifest


def validate_dependency_inventory(
    path: Path,
    *,
    expected_version: str,
    expected_source_commit: str,
    expected_target: str,
) -> None:
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("The target dependency inventory is unreadable.") from exc
    packages = inventory.get("python_packages") if isinstance(inventory, dict) else None
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema_version") != "1.0"
        or inventory.get("application_id") != APPLICATION_ID
        or inventory.get("application_version") != expected_version
        or inventory.get("source_commit_sha") != expected_source_commit
        or inventory.get("target") != expected_target
        or inventory.get("julia_version") != "1.12.6"
        or inventory.get("circuitscape_version") != "5.17.1"
        or inventory.get("connectivity_mode") != "raster_pairwise_resistance"
        or not isinstance(packages, dict)
        or set(packages) != STAGED_RUNTIME_PACKAGES
        or any(not isinstance(value, str) or not value for value in packages.values())
        or any(
            not isinstance(inventory.get(name), str) or not inventory[name]
            for name in (
                "gdal_version",
                "pyogrio_gdal_version",
                "proj_version",
                "geos_version",
                "python_version",
            )
        )
    ):
        raise SystemExit(
            "The target dependency inventory is incomplete or belongs to another build."
        )


def validate_packaged_runtime_inventory(
    packaged: object,
    declared: dict[str, object],
) -> None:
    if not isinstance(packaged, dict) or packaged.get("schema_version") != "1.0":
        raise SystemExit("The packaged worker returned an invalid runtime inventory.")
    packaged_packages = packaged.get("python_packages")
    declared_packages = declared.get("python_packages")
    capabilities = packaged.get("capabilities")
    if not isinstance(packaged_packages, dict) or not isinstance(declared_packages, dict):
        raise SystemExit("The packaged worker runtime inventory omits Python packages.")
    if set(packaged_packages) != PACKAGED_RUNTIME_PACKAGES:
        raise SystemExit("The packaged worker runtime inventory has an invalid package set.")
    if set(declared_packages) != STAGED_RUNTIME_PACKAGES:
        raise SystemExit("The staged worker runtime inventory has an invalid package set.")
    for package in sorted(PACKAGED_RUNTIME_PACKAGES):
        if packaged_packages.get(package) != declared_packages.get(package):
            raise SystemExit(f"The packaged worker dependency differs from staging: {package}")
    if (
        not isinstance(capabilities, dict)
        or set(capabilities) != PACKAGED_RUNTIME_CAPABILITIES
        or any(value is not True for value in capabilities.values())
    ):
        raise SystemExit("The packaged worker did not prove every professional runtime capability.")
    for field in (
        "python_version",
        "gdal_version",
        "pyogrio_gdal_version",
        "proj_version",
        "geos_version",
    ):
        if packaged.get(field) != declared.get(field):
            raise SystemExit(f"The packaged worker native runtime differs from staging: {field}")


def run_packaged_runtime_inventory(worker: Path, dependency_inventory: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="ecocorridor-professional-runtime-") as temporary:
        root = Path(temporary)
        environment = clean_environment()
        environment.update(
            {
                "TMPDIR": str(root),
                "TEMP": str(root),
                "TMP": str(root),
                "WANDB_ARTIFACT_DIR": str(root / "wandb-artifacts"),
                "WANDB_CACHE_DIR": str(root / "wandb-cache"),
                "WANDB_CONSOLE": "off",
                "WANDB_DATA_DIR": str(root / "wandb-data"),
                "WANDB_DIR": str(root / "wandb-runs"),
                "WANDB_DISABLE_CODE": "true",
                "WANDB_DISABLE_GIT": "true",
                "WANDB_ERROR_REPORTING": "false",
                "WANDB_SHOW_RUN": "false",
                "WANDB_SILENT": "true",
            }
        )
        for directory in (
            root / "wandb-artifacts",
            root / "wandb-cache",
            root / "wandb-data",
            root / "wandb-runs",
        ):
            directory.mkdir()
        completed = subprocess.run(
            [str(worker), "--packaged-runtime-inventory"],
            env=environment,
            capture_output=True,
            timeout=60,
            check=False,
        )
    if completed.returncode or completed.stderr.strip():
        raise SystemExit(
            "The packaged worker could not prove its professional runtime capabilities."
        )
    lines = [line for line in completed.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise SystemExit("The packaged worker runtime inventory is not one JSON record.")
    try:
        packaged = json.loads(lines[0])
        declared = json.loads(dependency_inventory.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("The packaged worker runtime inventory is invalid JSON.") from exc
    validate_packaged_runtime_inventory(packaged, declared)


def validate_staged_data_classification(
    data_manifest: Path,
    projection: Path,
    trust_record: Path,
    direct_manifest: Path,
    archive: Path,
) -> None:
    try:
        payload = json.loads(data_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("The staged ecological data manifest is unreadable.") from exc
    records = payload.get("files") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("manifest_version") != ECOLOGICAL_DATA_MANIFEST_SCHEMA
        or not isinstance(records, list)
    ):
        raise SystemExit("The staged ecological data manifest is invalid.")
    by_path = {
        item.get("path"): item
        for item in records
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    archive_source = f"benchmarks/ecocorridor/ridgeway/{archive.name}"
    expected_authorized = {
        "apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json",
        "benchmarks/ecocorridor/ridgeway/ui_projection.json",
        "benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json",
        archive_source,
    }
    authorized = {
        path
        for path, record in by_path.items()
        if record.get("signed_desktop_bundle_allowed") is True
    }
    if authorized != expected_authorized:
        raise SystemExit(
            "The staged ecological manifest does not authorize exactly the current trusted "
            "desktop evidence set."
        )
    for source_path, staged, public_replay_allowed in (
        ("benchmarks/ecocorridor/ridgeway/ui_projection.json", projection, True),
        (
            "apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json",
            trust_record,
            True,
        ),
        (
            "benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json",
            direct_manifest,
            False,
        ),
        (archive_source, archive, False),
    ):
        record = by_path.get(source_path)
        if (
            not isinstance(record, dict)
            or record.get("synthetic") is not True
            or record.get("signed_desktop_bundle_allowed") is not True
            or record.get("public_replay_allowed") is not public_replay_allowed
            or record.get("contains_real_locations") is not False
            or record.get("contains_species_occurrences") is not False
            or record.get("sha256") != digest(staged)
            or record.get("byte_size") != staged.stat().st_size
        ):
            raise SystemExit(
                f"Staged ecological evidence is not authorized for the signed desktop bundle: "
                f"{source_path}"
            )


def write_success_disposition(
    output: Path,
    *,
    release_root: Path,
    public_key: Path,
    expected_version: str,
    expected_source_commit: str,
    expected_target: str,
    signed_manifest: dict[str, object],
) -> None:
    """Write a path-free CI success receipt after every smoke assertion passes."""

    destination = output.resolve()
    if output.is_symlink() or destination.exists():
        raise SystemExit("Packaged-boundary smoke disposition must be a new regular file.")
    if destination == release_root or release_root in destination.parents:
        raise SystemExit("The CI smoke disposition must remain outside the signed resource tree.")
    worker_suffix = ".exe" if expected_target == "windows-x64" else ""
    receipt = {
        "schema_version": SMOKE_DISPOSITION_SCHEMA,
        "evidence_kind": "UNSIGNED_CI_SUCCESS_RECEIPT",
        "status": "PASSED",
        "application_id": APPLICATION_ID,
        "application_version": expected_version,
        "source_commit_sha": expected_source_commit,
        "target": expected_target,
        "signed_resource_count": len(signed_manifest.get("resources", [])),
        "release_manifest_sha256": digest(release_root / "release-manifest.json"),
        "release_signature_sha256": digest(release_root / "release-manifest.sig"),
        "resource_public_key_sha256": digest(public_key),
        "engine_manifest_sha256": digest(release_root / "engine-manifest.json"),
        "dependency_inventory_sha256": digest(release_root / "dependency-inventory.json"),
        "ecological_data_manifest_sha256": digest(release_root / "guided" / "data-manifest.json"),
        "ridgeway_release_trust_sha256": digest(
            release_root / "guided" / "ridgeway-release-trust.json"
        ),
        "ridgeway_direct_evidence_manifest_sha256": digest(
            release_root / "guided" / "direct-evidence-manifest.json"
        ),
        "worker_sha256": digest(release_root / "bin" / f"ecocorridor-worker{worker_suffix}"),
        "engine_executable_sha256": digest(
            release_root / "engine" / "bin" / f"ecocorridor-engine{worker_suffix}"
        ),
        "checks": [
            "SIGNED_RESOURCE_INVENTORY_VERIFIED",
            "PACKAGED_RUNTIME_INVENTORY_MATCHED",
            "PACKAGED_PROFESSIONAL_RUNTIME_CAPABILITIES_VERIFIED",
            "SIGNED_DESKTOP_BUNDLE_AUTHORIZATION_VERIFIED",
            "ENGINE_MANIFEST_TREE_MATCHED",
            "HAND_CALCULATED_CIRCUITSCAPE_RESULT_REPRODUCED",
            "WORKER_JSON_RPC_RESTART_AND_EVIDENCE_VERIFIED",
        ],
    }
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


EXPECTED_SERIES_RESISTANCE = 4.0
SERIES_ABSOLUTE_TOLERANCE = 1.0e-8


def write_ascii(
    path: Path,
    values: list[list[int | float]],
    *,
    nodata: float,
) -> None:
    rows = [
        f"ncols {len(values[0])}",
        f"nrows {len(values)}",
        "xllcorner 0",
        "yllcorner 0",
        "cellsize 1",
        f"NODATA_value {nodata}",
        *(" ".join(str(value) for value in row) for row in values),
    ]
    path.write_text("\n".join(rows) + "\n", encoding="ascii")


def clean_environment() -> dict[str, str]:
    allowed = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE"}
    }
    allowed.update(
        {
            "JULIA_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "WANDB_MODE": "offline",
        }
    )
    return allowed


def validate_hand_calculated_engine_result(result: object) -> None:
    if not isinstance(result, dict):
        raise SystemExit("Packaged Circuitscape engine returned malformed direct evidence.")
    metrics = result.get("pair_metrics")
    if not isinstance(metrics, list) or len(metrics) != 1 or not isinstance(metrics[0], dict):
        raise SystemExit("Packaged Circuitscape engine returned malformed direct evidence.")
    try:
        effective_resistance = float(metrics[0]["effective_resistance"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            "Packaged Circuitscape engine returned malformed direct evidence."
        ) from exc
    if (
        result.get("status") != "SUCCEEDED"
        or result.get("engine_name") != "Circuitscape"
        or result.get("circuitscape_version") != "5.17.1"
        or not math.isfinite(effective_resistance)
        or not math.isclose(
            effective_resistance,
            EXPECTED_SERIES_RESISTANCE,
            rel_tol=0.0,
            abs_tol=SERIES_ABSOLUTE_TOLERANCE,
        )
    ):
        raise SystemExit(
            "Packaged Circuitscape engine did not reproduce the hand-calculated "
            "four-ohm series landscape."
        )


def run_engine(engine: Path, root: Path) -> None:
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    # Three two-ohm cells form two series edges between the focal endpoints.
    # This is the same independently hand-calculated acceptance landscape used
    # by the Julia source tests; the installed engine must reproduce 4.0 ohms.
    resistance = [
        [-9999, -9999, -9999],
        [2, 2, 2],
        [-9999, -9999, -9999],
    ]
    focal = [[0, 0, 0], [1, 0, 2], [0, 0, 0]]
    write_ascii(inputs / "resistance.asc", resistance, nodata=-9999)
    write_ascii(inputs / "focal.asc", focal, nodata=0)
    (inputs / "pairs.tsv").write_text("mode\tinclude\n1\t2\n", encoding="ascii")
    request = {
        "protocol_version": "1.0",
        "prepared_run_id": "release_smoke_pair_1_2",
        "mode": "raster_pairwise",
        "raster_semantics": "resistance",
        "resources": {
            "resistance": "inputs/resistance.asc",
            "focal": "inputs/focal.asc",
            "included_pairs": "inputs/pairs.tsv",
        },
        "pairs": [{"pair_id": "pair_1_2", "from_focal_id": 1, "to_focal_id": 2}],
        "settings": {
            "precision": "double",
            "julia_threads": 1,
            "blas_threads": 1,
            "omp_threads": 1,
            "neighbor_rule": "FOUR",
            "resistance_average": "ARITHMETIC",
            "emit_current_map": False,
        },
        "output_directory": "outputs",
    }
    (root / "request.json").write_text(
        json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    completed = subprocess.run(
        [str(engine), "--request", "request.json", "--result", "result.json"],
        cwd=root,
        env=clean_environment(),
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode:
        raise SystemExit("Packaged Circuitscape engine smoke run failed.")
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    validate_hand_calculated_engine_result(result)


def validate_engine_manifest(engine_root: Path, engine_manifest: Path) -> None:
    try:
        manifest = json.loads(engine_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("The staged EngineManifest is unreadable.") from exc
    staged_digest = digest_tree(engine_root)
    if (
        manifest.get("engine_name") != "Circuitscape"
        or manifest.get("julia_version") != "1.12.6"
        or manifest.get("circuitscape_version") != "5.17.1"
        or manifest.get("julia_runtime_sha256") != staged_digest
        or manifest.get("native_artifacts_sha256") != staged_digest
    ):
        raise SystemExit("The staged Circuitscape tree differs from its typed EngineManifest.")


def worker_command(
    *,
    worker: Path,
    engine: Path,
    engine_manifest: Path,
    guided_projection: Path,
    guided_manifest: Path,
    guided_archive: Path,
    app_data: Path,
) -> list[str]:
    return [
        str(worker),
        "--transport",
        "jsonl-stdio",
        "--api-version",
        "1.0",
        "--app-data-root",
        str(app_data),
        "--engine-executable",
        str(engine),
        "--engine-sha256",
        digest(engine),
        "--engine-manifest",
        str(engine_manifest),
        "--engine-manifest-file-sha256",
        digest(engine_manifest),
        "--guided-projection",
        str(guided_projection),
        "--guided-projection-sha256",
        digest(guided_projection),
        "--guided-manifest",
        str(guided_manifest),
        "--guided-manifest-sha256",
        digest(guided_manifest),
        "--guided-evidence-archive",
        str(guided_archive),
        "--guided-evidence-archive-sha256",
        digest(guided_archive),
    ]


def invoke_worker(command: list[str], requests: list[dict[str, object]]) -> dict[object, object]:
    completed = subprocess.run(
        command,
        input=("".join(json.dumps(item, separators=(",", ":")) + "\n" for item in requests)).encode(
            "utf-8"
        ),
        env=clean_environment(),
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode:
        raise SystemExit("Packaged scientific worker smoke run failed.")
    responses = [
        json.loads(line) for line in completed.stdout.decode("utf-8").splitlines() if line.strip()
    ]
    return {item.get("id"): item for item in responses}


def run_worker(
    worker: Path,
    engine: Path,
    engine_manifest: Path,
    guided_projection: Path,
    guided_manifest: Path,
    guided_archive: Path,
    root: Path,
) -> None:
    app_data = root / "app-data"
    app_data.mkdir(parents=True)
    command = worker_command(
        worker=worker,
        engine=engine,
        engine_manifest=engine_manifest,
        guided_projection=guided_projection,
        guided_manifest=guided_manifest,
        guided_archive=guided_archive,
        app_data=app_data,
    )
    projection = json.loads(guided_projection.read_text(encoding="utf-8"))
    portfolio = projection["portfolios"][0]
    scenario = portfolio["scenarioResults"][0]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "system.ping", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "connectivity.guided.open", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "connectivity.evidence.resolve",
            "params": {
                "project_directory": str(app_data / "guided" / "ridgeway"),
                "claim_id": f"ECO-CLM-{portfolio['id']}-{scenario['scenarioId']}",
            },
        },
    ]
    by_id = invoke_worker(command, requests)
    if any(
        request_id not in by_id
        or "error" in by_id[request_id]
        or not isinstance(by_id[request_id].get("result"), dict)
        for request_id in (1, 2, 3)
    ):
        raise SystemExit("Packaged scientific worker did not complete strict JSON-RPC ping.")
    guided = by_id[2]["result"]["data"]
    evidence = by_id[3]["result"]["data"]
    if (
        guided.get("state") != "VERIFIED_FIXTURE_REPLAY"
        or guided.get("evidence_manifest_sha256") != digest(guided_manifest)
        or len(evidence.get("claim", {}).get("direct_run_ids", [])) != 2
    ):
        raise SystemExit("Packaged worker did not resolve verified Ridgeway direct evidence.")

    # Launch a fresh worker process against the same isolated app-data root.
    # This proves that the authoritative guided session survives process restart
    # and that startup recovery does not replace its pinned fixture projection.
    restarted = invoke_worker(
        command,
        [
            {"jsonrpc": "2.0", "id": 4, "method": "connectivity.guided.open", "params": {}},
        ],
    )
    if "error" in restarted.get(4, {}):
        raise SystemExit("Packaged worker could not restore the guided project after restart.")
    reopened = restarted[4]["result"]["data"]
    if (
        reopened.get("session_id") != "guided_ridgeway_v1"
        or reopened.get("state") != "VERIFIED_FIXTURE_REPLAY"
        or reopened.get("projection") != guided.get("projection")
    ):
        raise SystemExit("Packaged worker restart changed the verified Ridgeway session.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--disposition-output", type=Path)
    arguments = parser.parse_args()
    release_root = arguments.release_root.resolve(strict=True)
    if arguments.release_root.is_symlink() or not release_root.is_dir():
        raise SystemExit("Release root must be a regular non-symlink directory.")
    signed_manifest = verify_signed_release_inventory(
        release_root,
        arguments.public_key,
        expected_version=arguments.expected_version,
        expected_source_commit=arguments.expected_source_commit,
        expected_target=arguments.expected_target,
    )
    suffix = ".exe" if arguments.expected_target == "windows-x64" else ""
    worker = release_root / "bin" / f"ecocorridor-worker{suffix}"
    engine = release_root / "engine" / "bin" / f"ecocorridor-engine{suffix}"
    engine_manifest = release_root / "engine-manifest.json"
    dependency_inventory = release_root / "dependency-inventory.json"
    data_manifest = release_root / "guided" / "data-manifest.json"
    guided_projection = release_root / "guided" / "ridgeway-ui-projection.json"
    guided_trust = release_root / "guided" / "ridgeway-release-trust.json"
    guided_manifest = release_root / "guided" / "direct-evidence-manifest.json"
    try:
        archive_name = json.loads(guided_manifest.read_text(encoding="utf-8"))["evidence_bundle"][
            "filename"
        ]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit("Staged guided evidence manifest is invalid.") from exc
    if Path(str(archive_name)).name != archive_name:
        raise SystemExit("Staged guided evidence archive name is unsafe.")
    guided_archive = release_root / "guided" / archive_name
    for required in (
        worker,
        engine,
        engine_manifest,
        dependency_inventory,
        data_manifest,
        guided_projection,
        guided_trust,
        guided_manifest,
        guided_archive,
    ):
        if not required.is_file() or required.is_symlink():
            raise SystemExit(f"Required staged resource is absent or unsafe: {required.name}")
    validate_dependency_inventory(
        dependency_inventory,
        expected_version=arguments.expected_version,
        expected_source_commit=arguments.expected_source_commit,
        expected_target=arguments.expected_target,
    )
    run_packaged_runtime_inventory(worker, dependency_inventory)
    validate_staged_data_classification(
        data_manifest,
        guided_projection,
        guided_trust,
        guided_manifest,
        guided_archive,
    )
    validate_engine_manifest(release_root / "engine", engine_manifest)
    with tempfile.TemporaryDirectory(prefix="ecocorridor-release-smoke-") as temporary:
        smoke_root = Path(temporary)
        run_engine(engine, smoke_root / "engine-run")
        run_worker(
            worker,
            engine,
            engine_manifest,
            guided_projection,
            guided_manifest,
            guided_archive,
            smoke_root / "worker-run",
        )
    if arguments.disposition_output is not None:
        write_success_disposition(
            arguments.disposition_output,
            release_root=release_root,
            public_key=arguments.public_key.resolve(strict=True),
            expected_version=arguments.expected_version,
            expected_source_commit=arguments.expected_source_commit,
            expected_target=arguments.expected_target,
            signed_manifest=signed_manifest,
        )
    print("EcoCorridor staged worker and direct Circuitscape engine smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
