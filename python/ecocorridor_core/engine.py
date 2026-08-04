"""Pinned Circuitscape process boundary and a quarantined test solver."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import (
    ComputationalResourceObservation,
    ConnectivityScenario,
    ContractModel,
    DiagnosticMapSummary,
    DirectConnectivityRun,
    DirectRunState,
    EngineManifest,
    EngineMode,
    PairResistanceMetric,
    QAStatus,
    RasterSemantics,
    canonical_sha256,
)
from .raster_io import RasterIOError, current_map_values_and_nodata
from .resource_observability import (
    ObservedProcessCancelled,
    ObservedProcessTimeout,
    run_observed_process,
)


class UnsupportedConnectivityFeature(ValueError):
    pass


class EngineManifestGateError(RuntimeError):
    pass


class EngineProtocolError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_tree(path: Path) -> str:
    if path.is_file():
        return sha256_file(path)
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        file_hash = bytes.fromhex(sha256_file(item))
        digest.update(file_hash)
    return digest.hexdigest()


def assert_supported_settings(mode: EngineMode | str, semantics: RasterSemantics | str) -> None:
    if EngineMode(mode) != EngineMode.RASTER_PAIRWISE:
        raise UnsupportedConnectivityFeature("v0.1 supports only raster pairwise mode")
    if RasterSemantics(semantics) != RasterSemantics.RESISTANCE:
        raise UnsupportedConnectivityFeature("v0.1 supports resistance rasters only")


def build_engine_manifest(
    *,
    manifest_id: str,
    executable: Path,
    julia_runtime: Path,
    julia_project_directory: Path,
    native_artifacts: Path,
    wrapper: Path,
    parser: Path,
    metric_definitions: Path,
    neighbor_rule: str,
    resistance_average: str,
    platform: str,
) -> EngineManifest:
    """Build the release manifest only from real, present packaging inputs."""

    julia_manifest = julia_project_directory / "Manifest.toml"
    if not julia_manifest.is_file():
        raise EngineManifestGateError(
            "Julia Manifest.toml is absent; instantiate with Julia 1.12.6 and verify "
            "Circuitscape 5.17.1 before packaging"
        )
    manifest_text = julia_manifest.read_text(encoding="utf-8")
    if 'version = "5.17.1"' not in manifest_text:
        raise EngineManifestGateError("Julia manifest does not pin Circuitscape 5.17.1")
    required = [executable, julia_runtime, native_artifacts, wrapper, parser, metric_definitions]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise EngineManifestGateError(f"engine packaging inputs are absent: {missing}")
    return EngineManifest(
        manifest_id=manifest_id,
        executable_sha256=sha256_file(executable),
        julia_runtime_sha256=sha256_tree(julia_runtime),
        julia_manifest_sha256=sha256_file(julia_manifest),
        native_artifacts_sha256=sha256_tree(native_artifacts),
        wrapper_sha256=sha256_file(wrapper),
        parser_sha256=sha256_file(parser),
        metric_definitions_sha256=sha256_file(metric_definitions),
        neighbor_rule=neighbor_rule,
        resistance_average=resistance_average,
        platform=platform,
        created_at=datetime.now(UTC),
    )


class PreparedResource(ContractModel):
    logical_name: Literal["resistance", "focal", "included_pairs", "review_mask"]
    source_path: Path
    sha256: str
    target_filename: str

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("resource digest must be lowercase SHA-256")
        return value

    @field_validator("target_filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if value in {"", ".", ".."} or Path(value).name != value:
            raise ValueError("engine resource target must be a plain filename")
        return value


class PreparedPair(ContractModel):
    pair_id: str
    from_focal_id: int = Field(gt=0)
    to_focal_id: int = Field(gt=0)


class PreparedConnectivityRun(ContractModel):
    prepared_run_id: str
    analysis_id: str
    revision_id: str
    portfolio_id: str
    scenario: ConnectivityScenario
    pairs: list[PreparedPair] = Field(min_length=1)
    resources: list[PreparedResource] = Field(min_length=3)
    action_raster_sha256: str
    action_raster_diff_path: Path
    prepared_resistance_sha256: str
    footprint_proof_sha256: str
    engine_manifest_sha256: str
    model_run_cache_key: str
    emit_current_map: bool = False

    @field_validator(
        "action_raster_sha256",
        "prepared_resistance_sha256",
        "footprint_proof_sha256",
        "engine_manifest_sha256",
        "model_run_cache_key",
    )
    @classmethod
    def validate_evidence_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("prepared evidence digests must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_resources(self) -> PreparedConnectivityRun:
        names = [item.logical_name for item in self.resources]
        required = {"resistance", "focal", "included_pairs"}
        if not required.issubset(names) or len(names) != len(set(names)):
            raise ValueError("prepared resources must contain unique resistance, focal, and pairs")
        pair_ids = [item.pair_id for item in self.pairs]
        if set(pair_ids) != set(self.scenario.included_pair_ids):
            raise ValueError("prepared pairs must exactly match the scenario")
        return self


class ProtocolPairMetric(ContractModel):
    pair_id: str
    from_focal_id: int = Field(gt=0)
    to_focal_id: int = Field(gt=0)
    effective_resistance: float = Field(gt=0)


class EngineProtocolResult(ContractModel):
    protocol_version: Literal["1.0"]
    status: Literal["SUCCEEDED", "FAILED"]
    engine_name: Literal["Circuitscape"]
    circuitscape_version: Literal["5.17.1"]
    pair_metrics: list[ProtocolPairMetric] = Field(default_factory=list)
    matrix_path: str | None = None
    current_map_path: str | None = None
    review_mask_summaries: list[DiagnosticMapSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    failure_message: str | None = None

    @field_validator("matrix_path", "current_map_path")
    @classmethod
    def safe_result_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("protocol artifact paths must be contained relative paths")
        return path.as_posix()

    @model_validator(mode="after")
    def disposition_is_consistent(self) -> EngineProtocolResult:
        if self.status == "SUCCEEDED":
            if not self.pair_metrics or not self.matrix_path or self.failure_message:
                raise ValueError("successful engine results require metrics and a matrix artifact")
        elif not self.failure_message:
            raise ValueError("failed engine results require a failure message")
        ids = [item.pair_id for item in self.pair_metrics]
        if len(ids) != len(set(ids)):
            raise ValueError("engine returned duplicate focal-pair metrics")
        if self.review_mask_summaries:
            raise ValueError("wrapper-supplied review-mask summaries are not authoritative")
        return self


def parse_circuitscape_matrix(
    path: Path,
    pairs: list[PreparedPair],
) -> list[ProtocolPairMetric]:
    """Fail closed while reparsing a retained Circuitscape three-column matrix.

    Circuitscape writes every focal-ID combination and uses exactly ``-1`` for
    pairs excluded by the included-pairs file.  A reportable pair must appear
    once with a finite, strictly positive resistance; every other row must be
    the documented sentinel.  Treating malformed or unexpected positive rows
    as ignorable would let a damaged matrix still appear complete.
    """

    if not path.is_file() or path.is_symlink():
        raise EngineProtocolError("Circuitscape matrix is absent or unsafe")
    requested: dict[tuple[int, int], PreparedPair] = {}
    pair_ids: set[str] = set()
    for pair in pairs:
        key = tuple(sorted((pair.from_focal_id, pair.to_focal_id)))
        if pair.from_focal_id == pair.to_focal_id:
            raise EngineProtocolError("requested focal pairs must use two distinct IDs")
        if pair.pair_id in pair_ids or key in requested:
            raise EngineProtocolError("requested focal-pair identities are ambiguous")
        pair_ids.add(pair.pair_id)
        requested[key] = pair
    values: dict[tuple[int, int], float] = {}
    observed_keys: set[tuple[int, int]] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise EngineProtocolError("Circuitscape matrix is not UTF-8 text") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        fields = line.strip().split()
        if len(fields) != 3:
            raise EngineProtocolError(
                f"Circuitscape matrix row {line_number} is not exactly three columns"
            )
        try:
            left_value, right_value, resistance = map(float, fields)
        except ValueError as exc:
            raise EngineProtocolError(
                f"Circuitscape matrix row {line_number} is not numeric"
            ) from exc
        if not all(math.isfinite(value) for value in (left_value, right_value, resistance)):
            raise EngineProtocolError(
                f"Circuitscape matrix row {line_number} contains a nonfinite value"
            )
        if (
            not left_value.is_integer()
            or not right_value.is_integer()
            or left_value <= 0
            or right_value <= 0
            or left_value == right_value
        ):
            raise EngineProtocolError(
                f"Circuitscape matrix row {line_number} has invalid focal IDs"
            )
        key = tuple(sorted((int(left_value), int(right_value))))
        if key in observed_keys:
            raise EngineProtocolError("Circuitscape matrix contains a duplicate focal pair")
        observed_keys.add(key)
        if key in requested:
            if resistance <= 0:
                raise EngineProtocolError(
                    f"Circuitscape matrix has no positive result for requested pair "
                    f"{requested[key].pair_id}"
                )
            values[key] = resistance
        elif resistance != -1.0:
            raise EngineProtocolError(
                "Circuitscape matrix contains an unexpected positive or undocumented result"
            )
    output: list[ProtocolPairMetric] = []
    for pair in pairs:
        key = tuple(sorted((pair.from_focal_id, pair.to_focal_id)))
        if key not in values:
            raise EngineProtocolError(f"Circuitscape matrix omits requested pair {pair.pair_id}")
        output.append(
            ProtocolPairMetric(
                pair_id=pair.pair_id,
                from_focal_id=pair.from_focal_id,
                to_focal_id=pair.to_focal_id,
                effective_resistance=values[key],
            )
        )
    return output


class EngineExecution(ContractModel):
    work_directory: Path
    request_path: Path
    result_path: Path
    configuration_path: Path
    protocol_result: EngineProtocolResult
    stdout_sha256: str
    stderr_sha256: str
    return_code: int
    resource_observation: ComputationalResourceObservation
    process_exit_evidence: dict[str, Any]


def effective_circuitscape_configuration_text(
    *,
    resource_paths: dict[str, str],
    output_directory: str,
    neighbor_rule: str,
    resistance_average: str,
    emit_current_map: bool,
) -> str:
    """Return the only accepted v0.1 Circuitscape effective configuration."""

    current = "True" if emit_current_map else "False"
    four = "True" if neighbor_rule == "FOUR" else "False"
    arithmetic = "True" if resistance_average == "ARITHMETIC" else "False"
    # This is deliberately byte-for-byte, rather than a loose key lookup.  An
    # INI key under the wrong section can be ignored by Circuitscape and cause
    # its default to be used even though a section-blind validator appears to
    # see the requested value.  The wrapper owns this file, so exact canonical
    # bytes are both practical and the strongest evidence of consumed config.
    expected_lines = [
        "[Options for advanced mode]",
        "ground_file_is_resistances = True",
        "source_file = (Browse for a valid file)",
        "ground_file = (Browse for a valid file)",
        "use_unit_currents = False",
        "use_direct_grounds = False",
        "remove_src_or_gnd = keepall",
        "[Mask file]",
        "use_mask = False",
        "mask_file = (Browse for a valid file)",
        "[Calculation options]",
        "low_memory_mode = False",
        "parallelize = False",
        "max_parallel = 1",
        "solver = cg+amg",
        "print_timings = False",
        "preemptive_memory_release = True",
        "print_rusages = False",
        "scenario = pairwise",
        "[Short circuit regions (aka polygons)]",
        "use_polygons = False",
        "polygon_file = (Browse for a valid file)",
        "[Options for one-to-all and all-to-one modes]",
        "use_variable_source_strengths = False",
        "variable_source_file = (Browse for a valid file)",
        "[Output options]",
        "set_null_currents_to_nodata = False",
        "set_null_voltages_to_nodata = False",
        "compress_grids = True",
        f"write_cur_maps = {current}",
        f"write_cum_cur_map_only = {current}",
        "log_transform_maps = False",
        "write_volt_maps = False",
        f"output_file = {output_directory.rstrip('/')}/connectivity",
        "[Options for pairwise and one-to-all and all-to-one modes]",
        f"included_pairs_file = {resource_paths['included_pairs']}",
        "use_included_pairs = True",
        f"point_file = {resource_paths['focal']}",
        "[Connection scheme for raster habitat data]",
        f"connect_four_neighbors_only = {four}",
        f"connect_using_avg_resistances = {arithmetic}",
        "[Habitat raster or graph]",
        "habitat_map_is_resistances = True",
        f"habitat_file = {resource_paths['resistance']}",
    ]
    return "\n".join(expected_lines) + "\n"


def validate_effective_circuitscape_configuration(
    path: Path,
    *,
    resource_paths: dict[str, str],
    output_directory: str,
    neighbor_rule: str,
    resistance_average: str,
    emit_current_map: bool,
) -> None:
    if not path.is_file() or path.is_symlink():
        raise EngineProtocolError("Circuitscape effective configuration is absent or unsafe")
    expected = effective_circuitscape_configuration_text(
        resource_paths=resource_paths,
        output_directory=output_directory,
        neighbor_rule=neighbor_rule,
        resistance_average=resistance_average,
        emit_current_map=emit_current_map,
    )
    try:
        actual = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise EngineProtocolError("Circuitscape effective configuration is not UTF-8") from exc
    if actual != expected:
        raise EngineProtocolError("Circuitscape effective configuration differs from request")


def contained_regular_file(root: Path, relative: str) -> Path:
    """Resolve a regular file without accepting a symlink in any path component."""

    root = root.resolve(strict=True)
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
        or "\\" in relative
        or ":" in relative
    ):
        raise EngineProtocolError(f"engine artifact path is unsafe: {relative}")
    cursor = root
    for part in relative_path.parts:
        cursor = cursor / part
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError as exc:
            raise EngineProtocolError(f"engine artifact is absent: {relative}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise EngineProtocolError(f"symlinked engine artifact is forbidden: {relative}")
    if not stat.S_ISREG(os.lstat(cursor).st_mode):
        raise EngineProtocolError(f"engine artifact is not a regular file: {relative}")
    resolved = cursor.resolve(strict=True)
    if root not in resolved.parents:
        raise EngineProtocolError(f"engine artifact escapes isolated run directory: {relative}")
    return resolved


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def engine_request_payload(
    prepared: PreparedConnectivityRun, *, prepared_run_id: str | None = None
) -> dict[str, object]:
    resources = {item.logical_name: f"inputs/{item.target_filename}" for item in prepared.resources}
    return {
        "protocol_version": "1.0",
        "prepared_run_id": prepared_run_id or prepared.prepared_run_id,
        "mode": "raster_pairwise",
        "raster_semantics": "resistance",
        "resources": resources,
        "pairs": [item.model_dump(mode="json") for item in prepared.pairs],
        "settings": {
            "precision": "double",
            "neighbor_rule": prepared.scenario.neighbor_rule.value,
            "resistance_average": prepared.scenario.resistance_average.value,
            "julia_threads": 1,
            "blas_threads": 1,
            "omp_threads": 1,
            "emit_current_map": prepared.emit_current_map,
        },
        "output_directory": "outputs",
    }


class CircuitscapeAdapter:
    """Execute one portfolio × scenario as an isolated direct engine job."""

    def __init__(self, executable: Path, manifest: EngineManifest) -> None:
        if not executable.is_file() or executable.is_symlink():
            raise EngineManifestGateError("Circuitscape application executable is absent or unsafe")
        self.executable = executable.resolve()
        self.manifest = manifest
        if sha256_file(self.executable) != manifest.executable_sha256:
            raise EngineManifestGateError("Circuitscape executable differs from EngineManifest")

    def prepare(
        self,
        *,
        prepared_run_id: str,
        analysis_id: str,
        revision_id: str,
        portfolio_id: str,
        scenario: ConnectivityScenario,
        pairs: list[PreparedPair],
        resources: list[PreparedResource],
        action_raster_sha256: str,
        action_raster_diff_path: Path,
        prepared_resistance_sha256: str,
        footprint_proof_sha256: str,
        emit_current_map: bool = False,
    ) -> PreparedConnectivityRun:
        assert_supported_settings(scenario.engine_mode, scenario.raster_semantics)
        for resource in resources:
            if not resource.source_path.is_file() or resource.source_path.is_symlink():
                raise EngineProtocolError(
                    f"prepared resource is absent or unsafe: {resource.logical_name}"
                )
            if sha256_file(resource.source_path) != resource.sha256:
                raise EngineProtocolError(
                    f"prepared resource hash changed: {resource.logical_name}"
                )
        if (
            not action_raster_diff_path.is_file()
            or action_raster_diff_path.is_symlink()
            or sha256_file(action_raster_diff_path) != action_raster_sha256
        ):
            raise EngineProtocolError("prepared semantic raster diff is absent or changed")
        resistance_resource = next(
            (item for item in resources if item.logical_name == "resistance"), None
        )
        if resistance_resource is None or resistance_resource.sha256 != prepared_resistance_sha256:
            raise EngineProtocolError("prepared resistance identity differs from its resource")
        cache_key = canonical_sha256(
            {
                # Human-facing revision and portfolio IDs are provenance, not
                # physics. The hashes below fully identify the prepared model,
                # allowing NO_ACTION and SURVEY_FIRST records (or equivalent
                # revisions) to reuse an immutable direct result safely.
                "engine_settings": {
                    "mode": scenario.engine_mode.value,
                    "raster_semantics": scenario.raster_semantics.value,
                    "neighbor_rule": scenario.neighbor_rule.value,
                    "resistance_average": scenario.resistance_average.value,
                },
                "pairs": [item.model_dump(mode="json") for item in pairs],
                "resources": [
                    {"logical_name": item.logical_name, "sha256": item.sha256}
                    for item in sorted(resources, key=lambda item: item.logical_name)
                ],
                "prepared_resistance_sha256": prepared_resistance_sha256,
                "engine_manifest_sha256": self.manifest.sha256,
                "emit_current_map": emit_current_map,
            }
        )
        return PreparedConnectivityRun(
            prepared_run_id=prepared_run_id,
            analysis_id=analysis_id,
            revision_id=revision_id,
            portfolio_id=portfolio_id,
            scenario=scenario,
            pairs=pairs,
            resources=resources,
            action_raster_sha256=action_raster_sha256,
            action_raster_diff_path=action_raster_diff_path,
            prepared_resistance_sha256=prepared_resistance_sha256,
            footprint_proof_sha256=footprint_proof_sha256,
            engine_manifest_sha256=self.manifest.sha256,
            model_run_cache_key=cache_key,
            emit_current_map=emit_current_map,
        )

    def execute(
        self,
        prepared: PreparedConnectivityRun,
        isolated_directory: Path,
        *,
        timeout_seconds: int,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> EngineExecution:
        assert_supported_settings(prepared.scenario.engine_mode, prepared.scenario.raster_semantics)
        if prepared.engine_manifest_sha256 != self.manifest.sha256:
            raise EngineManifestGateError("prepared job references a different engine manifest")
        isolated_directory.mkdir(parents=True, exist_ok=False)
        inputs = isolated_directory / "inputs"
        outputs = isolated_directory / "outputs"
        inputs.mkdir()
        outputs.mkdir()
        resource_payload: dict[str, str] = {}
        for resource in prepared.resources:
            if sha256_file(resource.source_path) != resource.sha256:
                raise EngineProtocolError(
                    f"resource changed after prepare: {resource.logical_name}"
                )
            target = inputs / resource.target_filename
            shutil.copyfile(resource.source_path, target)
            if sha256_file(target) != resource.sha256:
                raise EngineProtocolError(
                    f"materialized resource hash mismatch: {resource.logical_name}"
                )
            resource_payload[resource.logical_name] = target.relative_to(
                isolated_directory
            ).as_posix()
        request_path = isolated_directory / "request.json"
        result_path = isolated_directory / "result.json"
        request = engine_request_payload(prepared)
        if request["resources"] != resource_payload:
            raise EngineProtocolError("prepared resource target mapping is inconsistent")
        _atomic_json(request_path, request)
        request_sha256 = sha256_file(contained_regular_file(isolated_directory, request_path.name))
        environment = {
            key: os.environ[key]
            for key in ("SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP")
            if key in os.environ
        }
        environment.update(
            {
                "JULIA_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
            }
        )
        stdout_path = isolated_directory / "stdout.log"
        stderr_path = isolated_directory / "stderr.log"
        try:
            if (
                not self.executable.is_file()
                or self.executable.is_symlink()
                or sha256_file(self.executable) != self.manifest.executable_sha256
            ):
                raise EngineManifestGateError(
                    "Circuitscape executable changed immediately before process launch"
                )
            observed = run_observed_process(
                [
                    str(self.executable),
                    "--request",
                    request_path.name,
                    "--result",
                    result_path.name,
                ],
                cwd=isolated_directory,
                env=environment,
                timeout_seconds=timeout_seconds,
                observation_id=f"resource:{prepared.prepared_run_id}",
                boundary_description=(
                    "One isolated Circuitscape raster-pairwise process, including runtime "
                    "startup, input parsing, solve, and result serialization."
                ),
                cancel_requested=cancel_requested,
            )
        except (ObservedProcessCancelled, ObservedProcessTimeout) as error:
            stdout_path.write_bytes(error.output or b"")
            stderr_path.write_bytes(error.stderr or b"")
            raise
        completed = observed.completed
        stdout_path.write_bytes(completed.stdout)
        stderr_path.write_bytes(completed.stderr)
        try:
            request_path = contained_regular_file(isolated_directory, "request.json")
            if sha256_file(request_path) != request_sha256:
                raise EngineProtocolError("engine request bytes changed during execution")
            for resource in prepared.resources:
                materialized = contained_regular_file(
                    isolated_directory, f"inputs/{resource.target_filename}"
                )
                if sha256_file(materialized) != resource.sha256:
                    raise EngineProtocolError(
                        f"materialized {resource.logical_name} input changed during execution"
                    )
                if (
                    not resource.source_path.is_file()
                    or resource.source_path.is_symlink()
                    or sha256_file(resource.source_path) != resource.sha256
                ):
                    raise EngineProtocolError(
                        f"prepared {resource.logical_name} source changed during execution"
                    )
            if (
                not prepared.action_raster_diff_path.is_file()
                or prepared.action_raster_diff_path.is_symlink()
                or sha256_file(prepared.action_raster_diff_path) != prepared.action_raster_sha256
            ):
                raise EngineProtocolError("prepared semantic raster diff changed during execution")
            result_path = contained_regular_file(isolated_directory, "result.json")
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            protocol = EngineProtocolResult.model_validate(payload)
            if completed.returncode != 0 and protocol.status != "FAILED":
                raise EngineProtocolError("nonzero process exit cannot report SUCCEEDED")
            configuration_path = contained_regular_file(isolated_directory, "circuitscape.ini")
            validate_effective_circuitscape_configuration(
                configuration_path,
                resource_paths=resource_payload,
                output_directory="outputs",
                neighbor_rule=prepared.scenario.neighbor_rule.value,
                resistance_average=prepared.scenario.resistance_average.value,
                emit_current_map=prepared.emit_current_map,
            )
            return EngineExecution(
                work_directory=isolated_directory,
                request_path=request_path,
                result_path=result_path,
                configuration_path=configuration_path,
                protocol_result=protocol,
                stdout_sha256=sha256_file(stdout_path),
                stderr_sha256=sha256_file(stderr_path),
                return_code=completed.returncode,
                resource_observation=observed.observation,
                process_exit_evidence=observed.process_exit.as_dict(),
            )
        except (json.JSONDecodeError, ValueError) as error:
            wrapped = EngineProtocolError(f"invalid Circuitscape result protocol: {error}")
            wrapped.resource_observation = observed.observation
            wrapped.process_exit_evidence = observed.process_exit.as_dict()
            raise wrapped from error
        except Exception as error:
            # Preserve measured process evidence even when post-execution protocol,
            # configuration, or artifact validation fails.  The failed job remains
            # non-reportable, but its resource boundary and passive diagnostics are
            # still authoritative and useful for remediation.
            error.resource_observation = observed.observation
            error.process_exit_evidence = observed.process_exit.as_dict()
            raise

    def extract(
        self,
        prepared: PreparedConnectivityRun,
        execution: EngineExecution,
        *,
        direct_run_id: str,
        baseline_metrics: dict[str, float] | None = None,
    ) -> DirectConnectivityRun:
        protocol = execution.protocol_result
        if protocol.status != "SUCCEEDED" or execution.return_code != 0:
            raise EngineProtocolError(protocol.failure_message or "Circuitscape execution failed")
        if protocol.warnings:
            raise EngineProtocolError(
                "Circuitscape warnings require an explicit reviewer exception; automatic "
                "acceptance is forbidden"
            )
        expected_pairs = {item.pair_id for item in prepared.pairs}
        observed_pairs = {item.pair_id for item in protocol.pair_metrics}
        if observed_pairs != expected_pairs:
            raise EngineProtocolError(
                f"result pair set differs from requested pairs: expected={sorted(expected_pairs)}, "
                f"observed={sorted(observed_pairs)}"
            )
        matrix_path = contained_regular_file(execution.work_directory, protocol.matrix_path or "")
        reparsed = parse_circuitscape_matrix(matrix_path, prepared.pairs)
        if [
            item.model_dump(mode="json") for item in sorted(reparsed, key=lambda item: item.pair_id)
        ] != [
            item.model_dump(mode="json")
            for item in sorted(protocol.pair_metrics, key=lambda item: item.pair_id)
        ]:
            raise EngineProtocolError(
                "result protocol pair metrics differ from the retained raw matrix"
            )
        current_hash: str | None = None
        if prepared.emit_current_map:
            if not protocol.current_map_path:
                raise EngineProtocolError("requested current map was not produced")
            current_path = contained_regular_file(
                execution.work_directory, protocol.current_map_path
            )
            try:
                current_values, current_nodata = current_map_values_and_nodata(current_path)
            except RasterIOError as exc:
                raise EngineProtocolError(f"current-map artifact is malformed: {exc}") from exc
            valid_count = 0
            for row in current_values:
                for value in row:
                    is_nodata = (math.isnan(current_nodata) and math.isnan(value)) or math.isclose(
                        value, current_nodata, rel_tol=0.0, abs_tol=1e-9
                    )
                    if is_nodata:
                        continue
                    if not math.isfinite(value) or value < 0:
                        raise EngineProtocolError(
                            "current-map artifact contains nonfinite or negative current"
                        )
                    valid_count += 1
            if valid_count == 0:
                raise EngineProtocolError("current-map artifact has no valid current cells")
            current_hash = sha256_file(current_path)
        elif protocol.current_map_path:
            raise EngineProtocolError("wrapper returned an unrequested current map")
        pair_metrics: list[PairResistanceMetric] = []
        for item in sorted(reparsed, key=lambda metric: metric.pair_id):
            baseline = baseline_metrics.get(item.pair_id) if baseline_metrics else None
            relative = None
            if baseline is not None:
                if baseline <= 0:
                    raise EngineProtocolError("baseline effective resistance must be positive")
                relative = (baseline - item.effective_resistance) / baseline * 100.0
            pair_metrics.append(
                PairResistanceMetric(
                    pair_id=item.pair_id,
                    effective_resistance=item.effective_resistance,
                    baseline_effective_resistance=baseline,
                    baseline_relative_change_percent=relative,
                )
            )
        return DirectConnectivityRun(
            direct_run_id=direct_run_id,
            analysis_id=prepared.analysis_id,
            revision_id=prepared.revision_id,
            portfolio_id=prepared.portfolio_id,
            scenario_id=prepared.scenario.scenario_id,
            state=DirectRunState.SUCCEEDED,
            exact=True,
            engine_name="Circuitscape",
            engine_manifest_sha256=prepared.engine_manifest_sha256,
            model_run_cache_key=prepared.model_run_cache_key,
            input_sha256=sha256_file(execution.request_path),
            result_sha256=sha256_file(execution.result_path),
            matrix_sha256=sha256_file(matrix_path),
            current_map_sha256=current_hash,
            pair_metrics=pair_metrics,
            diagnostic_maps=[],
            qa_status=QAStatus.ACCEPTED,
            qa_findings=[],
            completed_at=datetime.now(UTC),
            test_only=False,
        )


class TestOnlyPairResult(ContractModel):
    pair_id: str
    effective_resistance: float = Field(gt=0)


class TestOnlyCircuitResult(ContractModel):
    engine_name: Literal["TEST_ONLY_HAND_CALCULATED_CIRCUIT"] = "TEST_ONLY_HAND_CALCULATED_CIRCUIT"
    test_only: Literal[True] = True
    pair_results: list[TestOnlyPairResult]
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Not a Circuitscape execution.",
            "Cannot be used in practitioner results, Evidence Rail claims, or exports.",
        ]
    )


class TestOnlyCircuitEngine:
    """Small nodal solver for hand-calculated series/parallel acceptance tests.

    It deliberately returns a type that cannot validate as DirectConnectivityRun.
    """

    @staticmethod
    def solve(
        edges: list[tuple[int, int, float]],
        pairs: dict[str, tuple[int, int]],
        *,
        allow_test_engine: bool = False,
    ) -> TestOnlyCircuitResult:
        if not allow_test_engine:
            raise PermissionError("the pure-Python circuit engine is test-only and opt-in")
        nodes = sorted({node for edge in edges for node in edge[:2]})
        if len(nodes) < 2:
            raise ValueError("a test circuit needs at least two nodes")
        if any(resistance <= 0 for _, _, resistance in edges):
            raise ValueError("edge resistance must be positive")
        results = [
            TestOnlyPairResult(
                pair_id=pair_id,
                effective_resistance=_effective_resistance(edges, source, sink),
            )
            for pair_id, (source, sink) in sorted(pairs.items())
        ]
        return TestOnlyCircuitResult(pair_results=results)


def _effective_resistance(edges: list[tuple[int, int, float]], source: int, sink: int) -> float:
    nodes = sorted({node for edge in edges for node in edge[:2]})
    if source == sink or source not in nodes or sink not in nodes:
        raise ValueError("test focal pair must contain two present, distinct nodes")
    unknown = [node for node in nodes if node != sink]
    index = {node: position for position, node in enumerate(unknown)}
    matrix = [[0.0 for _ in unknown] for _ in unknown]
    rhs = [0.0 for _ in unknown]
    rhs[index[source]] = 1.0
    for left, right, resistance in edges:
        conductance = 1.0 / resistance
        if left != sink:
            matrix[index[left]][index[left]] += conductance
        if right != sink:
            matrix[index[right]][index[right]] += conductance
        if left != sink and right != sink:
            matrix[index[left]][index[right]] -= conductance
            matrix[index[right]][index[left]] -= conductance
    voltage = _solve_linear_system(matrix, rhs)
    result = voltage[index[source]]
    if not result > 0:
        raise ValueError("test circuit is disconnected or singular")
    return result


def _solve_linear_system(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    size = len(rhs)
    augmented = [row[:] + [rhs[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-14:
            raise ValueError("test circuit is disconnected or singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[index][-1] for index in range(size)]
