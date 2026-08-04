"""Authoritative EcoCorridor producer-event projections for W&B.

This adapter is intentionally read-only over ecological/scientific state.  It
turns a committed outbox row into bounded W&B Runs, Tables, and Artifacts only
after re-reading and hash-verifying the authoritative project repository.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any

from scenario_platform.cloud_sync import (
    ArtifactProjection,
    CloudIntegrityError,
    CloudPolicyError,
    CloudTransportError,
    TableProjection,
    TelemetryProjection,
)
from scenario_platform.contracts import canonical_json
from scenario_platform.observability import AppProfile
from scenario_platform.storage import PathSecurityError, atomic_write_json, contained_path

from .contracts import (
    CloudDisposition,
    ComputationalResourceObservation,
    ConnectivityEvidenceClaim,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    PortfolioDecisionSummary,
    canonical_sha256,
)
from .governance import (
    SnapshotAssetAuthorizationError,
    reconcile_snapshot_assets,
    scientific_asset_ids,
)
from .project_store import EcoProjectStore
from .reference_matrix_parser import REFERENCE_MATRIX_PARSER_SHA256

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_LOCAL_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|/(?:Users|home|var|private|tmp|opt|etc)/)[^\s\"']+"
)
_SUPPORTED_EVENTS = frozenset(
    {
        "bundle_import",
        "semantic_revision",
        "model_attestation",
        "analysis_plan",
        "guided_session",
        "guided_checkpoint",
        "direct_job_terminal",
        "analysis_completion",
        "reference_comparison",
        "evidence_export",
    }
)

_RESOURCE_COLUMNS = [
    "subject_id",
    "observation_id",
    "boundary",
    "solver_wall_seconds",
    "proposer_wall_seconds",
    "cache_lookup_wall_seconds",
    "peak_memory_status",
    "peak_memory_bytes",
    "peak_memory_method",
    "peak_memory_sample_count",
    "peak_memory_sampling_interval_seconds",
    "peak_memory_is_lower_bound",
    "energy_status",
    "energy_kwh",
    "energy_method",
    "energy_estimate_watts",
    "energy_estimate_lower_watts",
    "energy_estimate_upper_watts",
    "energy_estimate_lower_kwh",
    "energy_estimate_upper_kwh",
    "hardware",
    "boundary_description",
    "assumptions",
    "exclusions",
]


def _resource_row(subject_id: str, observation: ComputationalResourceObservation) -> list[Any]:
    return [
        subject_id,
        observation.observation_id,
        observation.boundary.value,
        observation.solver_wall_seconds,
        observation.proposer_wall_seconds,
        observation.cache_lookup_wall_seconds,
        observation.peak_memory_status.value,
        observation.peak_memory_bytes,
        observation.peak_memory_method,
        observation.peak_memory_sample_count,
        observation.peak_memory_sampling_interval_seconds,
        observation.peak_memory_is_lower_bound,
        observation.energy_status.value,
        observation.energy_kwh,
        observation.energy_method,
        observation.energy_estimate_watts,
        observation.energy_estimate_lower_watts,
        observation.energy_estimate_upper_watts,
        observation.energy_estimate_lower_kwh,
        observation.energy_estimate_upper_kwh,
        observation.hardware,
        observation.boundary_description,
        list(observation.assumptions),
        list(observation.exclusions),
    ]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_component(value: str, *, fallback: str) -> str:
    normalized = _SAFE_COMPONENT.sub("-", value).strip(".-")
    return (normalized or fallback)[:96]


def _assert_no_local_paths(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_local_paths(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_local_paths(item)
    elif isinstance(value, str) and _LOCAL_PATH.search(value):
        raise CloudPolicyError("ECO_TELEMETRY_CONTAINS_LOCAL_PATH")


class EcoTelemetryProjector:
    """Project Eco producer envelopes without influencing model execution."""

    def __init__(
        self,
        *,
        store: EcoProjectStore,
        project_root: str | Path,
        sync_plan: dict[str, Any],
        profile: AppProfile,
    ) -> None:
        self.store = store
        self.root = Path(project_root).resolve()
        self.profile = profile
        if store.root != self.root:
            raise ValueError("Eco telemetry store and project root differ")
        if sync_plan.get("allowed") is not True or sync_plan.get("blockers"):
            raise CloudPolicyError("BLOCKED_PROJECT_POLICY")
        projections = sync_plan.get("projections")
        if not isinstance(projections, list):
            raise CloudPolicyError("ECO_SYNC_PLAN_PROJECTIONS_REQUIRED")
        destination = str(sync_plan.get("destination", ""))
        self.sync_destination = destination if destination else None
        self.asset_projections: dict[str, list[dict[str, Any]]] = {}
        self.source_projections: dict[str, dict[str, Any]] = {}
        for item in projections:
            if not isinstance(item, dict):
                raise CloudPolicyError("ECO_SYNC_PLAN_PROJECTION_INVALID")
            uploaded = str(item.get("uploaded_asset_id", ""))
            source = str(item.get("source_asset_id", ""))
            representation = str(item.get("representation", ""))
            byte_size = int(item.get("bytes", -1))
            if (
                not uploaded
                or not source
                or representation not in {"RAW", "GENERALIZED_DERIVATIVE"}
                or byte_size < 0
            ):
                raise CloudPolicyError("ECO_SYNC_PLAN_PROJECTION_INVALID")
            normalized = {
                "source_asset_id": source,
                "uploaded_asset_id": uploaded,
                "representation": representation,
                "bytes": byte_size,
            }
            if source in self.source_projections:
                raise CloudPolicyError("ECO_SYNC_PLAN_SOURCE_DUPLICATED")
            self.source_projections[source] = normalized
            existing = self.asset_projections.setdefault(uploaded, [])
            if any(item["bytes"] != byte_size for item in existing):
                raise CloudPolicyError("ECO_SYNC_PLAN_PROJECTION_CONFLICT")
            if normalized not in existing:
                existing.append(normalized)

    @staticmethod
    def _cloud_source_reference(plan: dict[str, Any]) -> str:
        source = str(plan["source_asset_id"])
        if plan["representation"] == "RAW":
            return source
        return "generalized-source-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]

    def _validate_bundle_projections(self, bundle: ConnectivityProjectBundle) -> None:
        assets = {item.asset_id: item for item in bundle.assets}
        if set(self.source_projections) != set(assets):
            raise CloudPolicyError("ECO_SYNC_PLAN_SOURCE_COVERAGE_MISMATCH")
        scientific = scientific_asset_ids(bundle)
        for source_id, plan in self.source_projections.items():
            source = assets[source_id]
            uploaded = assets.get(str(plan["uploaded_asset_id"]))
            if uploaded is None or uploaded.byte_size != plan["bytes"]:
                raise CloudPolicyError("ECO_SYNC_PLAN_ASSET_METADATA_MISMATCH")
            if plan["representation"] == "RAW":
                if uploaded.asset_id != source.asset_id:
                    raise CloudPolicyError("ECO_SYNC_PLAN_RAW_IDENTITY_MISMATCH")
                continue
            if (
                source.authorization.cloud_disposition
                != CloudDisposition.GENERALIZED_DERIVATIVE_ONLY
                or source.authorization.generalized_asset_id != uploaded.asset_id
                or uploaded.authorization.cloud_disposition != CloudDisposition.RAW_ALLOWED
            ):
                raise CloudPolicyError("ECO_SYNC_PLAN_GENERALIZED_DERIVATIVE_MISMATCH")
            if source_id in scientific:
                raise CloudPolicyError("ECO_SYNC_PLAN_GENERALIZED_SCIENTIFIC_ASSET_BLOCKED")

    def _require_current_analysis_asset_authorization(self, analysis_id: str) -> None:
        """Reject historical analysis uploads detached from current asset authority."""

        try:
            snapshot = self.store.analysis_bundle(analysis_id)
            current = self.store.effective_bundle()
            current_assets = reconcile_snapshot_assets(snapshot, current)
        except SnapshotAssetAuthorizationError as exc:
            raise CloudPolicyError(exc.code) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise CloudPolicyError("ECO_ANALYSIS_ASSET_AUTHORIZATION_UNAVAILABLE") from exc

        scientific = scientific_asset_ids(snapshot)
        all_current_assets = {asset.asset_id: asset for asset in current.assets}
        for asset_id, current_asset in sorted(current_assets.items()):
            projection = self.source_projections.get(asset_id)
            if projection is None:
                raise CloudPolicyError("ECO_ANALYSIS_ASSET_PROJECTION_MISSING")
            if (
                self.sync_destination is not None
                and self.sync_destination not in current_asset.authorization.approved_deployments
            ):
                raise CloudPolicyError("ECO_ANALYSIS_ASSET_CURRENT_DEPLOYMENT_UNAUTHORIZED")
            representation = str(projection["representation"])
            uploaded_asset_id = str(projection["uploaded_asset_id"])
            if representation == "RAW":
                if (
                    uploaded_asset_id != asset_id
                    or int(projection["bytes"]) != current_asset.byte_size
                    or current_asset.authorization.cloud_disposition
                    in {
                        CloudDisposition.GENERALIZED_DERIVATIVE_ONLY,
                        CloudDisposition.PROHIBITED,
                    }
                ):
                    raise CloudPolicyError("ECO_ANALYSIS_ASSET_PROJECTION_STALE")
            else:
                uploaded_asset = all_current_assets.get(uploaded_asset_id)
                if (
                    current_asset.authorization.cloud_disposition
                    != CloudDisposition.GENERALIZED_DERIVATIVE_ONLY
                    or current_asset.authorization.generalized_asset_id != uploaded_asset_id
                    or uploaded_asset is None
                    or uploaded_asset.authorization.cloud_disposition
                    != CloudDisposition.RAW_ALLOWED
                    or (
                        self.sync_destination is not None
                        and self.sync_destination
                        not in uploaded_asset.authorization.approved_deployments
                    )
                    or int(projection["bytes"]) != uploaded_asset.byte_size
                ):
                    raise CloudPolicyError("ECO_ANALYSIS_ASSET_PROJECTION_STALE")
            if asset_id in scientific and representation != "RAW":
                raise CloudPolicyError("ECO_ANALYSIS_SCIENTIFIC_ASSET_RAW_SYNC_REQUIRED")

    def _cloud_bundle_payload(self, bundle: ConnectivityProjectBundle) -> dict[str, Any]:
        """Create an upload descriptor containing only authorized asset representations."""

        self._validate_bundle_projections(bundle)
        payload = bundle.model_dump(mode="json")
        assets = {item.asset_id: item for item in bundle.assets}
        uploaded_ids = sorted(self.asset_projections)
        payload["assets"] = [assets[item].model_dump(mode="json") for item in uploaded_ids]
        payload["cloud_projection"] = {
            "schema_version": "ecocorridor-cloud-project/v1.0",
            "source_bundle_sha256": hashlib.sha256(
                canonical_json(bundle.model_dump(mode="json")).encode("utf-8")
            ).hexdigest(),
            "asset_representations": [
                {
                    "source_asset_reference": self._cloud_source_reference(plan),
                    "uploaded_asset_id": plan["uploaded_asset_id"],
                    "representation": plan["representation"],
                    "bytes": plan["bytes"],
                }
                for plan in sorted(
                    self.source_projections.values(),
                    key=lambda item: self._cloud_source_reference(item),
                )
            ],
        }
        return payload

    def __call__(self, row: dict[str, Any], payload: dict[str, Any]) -> TelemetryProjection:
        event_type = str(row.get("event_type", ""))
        if event_type not in _SUPPORTED_EVENTS:
            raise CloudPolicyError("ECO_TELEMETRY_EVENT_UNSUPPORTED")
        if str(row.get("project_id", "")) != self.store.project()["project_id"]:
            raise CloudIntegrityError("ECO_TELEMETRY_PROJECT_MISMATCH")
        handlers = {
            "bundle_import": self._bundle_import,
            "semantic_revision": self._semantic_revision,
            "model_attestation": self._model_attestation,
            "analysis_plan": self._analysis_plan,
            "guided_session": self._guided_session,
            "guided_checkpoint": self._guided_checkpoint,
            "direct_job_terminal": self._direct_job_terminal,
            "analysis_completion": self._analysis_completion,
            "reference_comparison": self._reference_comparison,
            "evidence_export": self._evidence_export,
        }
        projection = handlers[event_type](row, payload)
        dumped = projection.model_dump(mode="json")
        _assert_no_local_paths(dumped)
        return projection

    def _base(
        self,
        row: dict[str, Any],
        *,
        run_kind: str,
        run_subject_id: str,
        event_sequence: int,
        config: dict[str, Any] | None = None,
        history: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
        tables: list[TableProjection] | None = None,
        input_artifact_refs: list[str] | None = None,
        artifacts: list[ArtifactProjection] | None = None,
    ) -> TelemetryProjection:
        projected_tables = list(tables or [])
        sync_failures = self._sync_failures_table()
        if sync_failures is not None:
            if any(item.logical_name == sync_failures.logical_name for item in projected_tables):
                raise CloudIntegrityError("ECO_TELEMETRY_TABLE_NAME_CONFLICT")
            projected_tables.append(sync_failures)
        return TelemetryProjection(
            profile_namespace=self.profile.namespace,
            project_id=str(row["project_id"]),
            run_kind=run_kind,
            run_subject_id=run_subject_id,
            event_type=str(row["event_type"]),
            event_sequence=event_sequence,
            config=config or {},
            history=history or {},
            summary=summary or {},
            tables=projected_tables,
            input_artifact_refs=sorted(set(input_artifact_refs or [])),
            artifacts=artifacts or [],
        )

    @staticmethod
    def _stream_sequence(row: dict[str, Any], *, run_kind: str, run_subject_id: str) -> int:
        """Return the transactionally assigned provider-run barrier sequence."""

        expected_stream = f"{run_kind}:{run_subject_id}"
        try:
            sequence = int(row["stream_sequence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CloudIntegrityError("ECO_TELEMETRY_STREAM_SEQUENCE_MISSING") from exc
        if row.get("stream_id") != expected_stream or sequence < 1:
            raise CloudIntegrityError("ECO_TELEMETRY_STREAM_SEQUENCE_MISMATCH")
        return sequence

    def _sync_failures_table(self) -> TableProjection | None:
        """Project only durable, stable outbox failures that still exist locally."""

        rows: list[list[Any]] = []
        for item in self.store.telemetry_rows():
            error_code = item.get("last_error_code")
            if not error_code:
                continue
            if not isinstance(error_code, str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]{2,127}", error_code
            ):
                raise CloudIntegrityError("ECO_SYNC_FAILURE_CODE_INVALID")
            state = str(item.get("state", ""))
            if state not in {"PENDING", "UPLOADING", "FAILED", "BLOCKED"}:
                raise CloudIntegrityError("ECO_SYNC_FAILURE_STATE_INVALID")
            rows.append(
                [
                    item["outbox_id"],
                    item["event_type"],
                    item["subject_id"],
                    item.get("analysis_id"),
                    state,
                    int(item["attempt_count"]),
                    error_code,
                    item["created_at"],
                    item["updated_at"],
                ]
            )
        if not rows:
            return None
        return TableProjection(
            logical_name="sync_failures",
            columns=[
                "outbox_id",
                "event_type",
                "subject_id",
                "analysis_id",
                "state",
                "attempt_count",
                "error_code",
                "created_at",
                "updated_at",
            ],
            rows=rows,
        )

    @staticmethod
    def _search_batches_table(
        plan: ConnectivityRunPlan,
        *,
        state: str,
        complete_portfolio_count: int | None = None,
    ) -> TableProjection:
        """Describe the one immutable initial batch represented by the v0.1 run plan."""

        if state not in {"PLANNED", "COMPLETED"}:
            raise ValueError("search-batch projection state is invalid")
        if complete_portfolio_count is not None and not 0 <= complete_portfolio_count <= len(
            plan.portfolios
        ):
            raise CloudIntegrityError("ECO_SEARCH_BATCH_COMPLETION_COUNT_INVALID")
        portfolio_ids = [item.portfolio_id for item in plan.portfolios]
        portfolio_set_sha256 = hashlib.sha256(
            canonical_json(portfolio_ids).encode("utf-8")
        ).hexdigest()
        return TableProjection(
            logical_name="search_batches",
            columns=[
                "analysis_id",
                "batch_index",
                "batch_role",
                "state",
                "method",
                "seed",
                "scenario_count",
                "portfolio_count",
                "direct_job_count",
                "complete_portfolio_count",
                "ordered_portfolio_ids_sha256",
            ],
            rows=[
                [
                    plan.analysis_id,
                    0,
                    "INITIAL_IMMUTABLE_PLAN",
                    state,
                    plan.search_method,
                    plan.seed,
                    len(plan.scenario_ids),
                    len(plan.portfolios),
                    plan.direct_job_count,
                    complete_portfolio_count,
                    portfolio_set_sha256,
                ]
            ],
        )

    @staticmethod
    def _adverse_changes_table(
        *,
        summaries: list[PortfolioDecisionSummary],
        claims: list[ConnectivityEvidenceClaim],
        successful_direct_run_ids: set[str],
    ) -> TableProjection | None:
        """Project failed rules only when an exact retained claim supports them."""

        claims_by_id = {item.claim_id: item for item in claims}
        rows: list[list[Any]] = []
        for summary in summaries:
            for disposition in summary.rule_dispositions:
                if disposition.passed or disposition.evidence_claim_id is None:
                    continue
                claim = claims_by_id.get(disposition.evidence_claim_id)
                if claim is None:
                    raise CloudIntegrityError("ECO_ADVERSE_CHANGE_EVIDENCE_MISSING")
                if (
                    claim.portfolio_id != summary.portfolio_id
                    or not any(
                        node.node_type == "RULE" and node.node_id == f"rule:{disposition.rule_id}"
                        for node in claim.nodes
                    )
                    or (
                        claim.scenario_id is not None
                        and claim.scenario_id != disposition.scenario_id
                    )
                    or (claim.pair_id is not None and claim.pair_id != disposition.pair_id)
                ):
                    raise CloudIntegrityError("ECO_ADVERSE_CHANGE_EVIDENCE_MISMATCH")
                if not claim.direct_run_ids:
                    continue
                if not set(claim.direct_run_ids).issubset(successful_direct_run_ids):
                    raise CloudIntegrityError("ECO_ADVERSE_CHANGE_DIRECT_RUN_MISSING")
                rows.append(
                    [
                        summary.portfolio_id,
                        claim.claim_kind,
                        disposition.rule_id,
                        disposition.scenario_id,
                        disposition.pair_id,
                        disposition.observed_value,
                        claim.units,
                        claim.claim_id,
                        sorted(claim.direct_run_ids),
                        summary.review_mask_triggered,
                        disposition.explanation,
                    ]
                )
        if not rows:
            return None
        return TableProjection(
            logical_name="adverse_changes",
            columns=[
                "portfolio_id",
                "claim_kind",
                "rule_id",
                "scenario_id",
                "pair_id",
                "observed_value",
                "units",
                "evidence_claim_id",
                "direct_run_ids",
                "review_mask_triggered",
                "explanation",
            ],
            rows=rows,
        )

    def _verified_artifact(
        self,
        *,
        local_artifact_id: str,
        artifact_type: str,
        logical_id: str,
        logical_name: str,
        source_relative_path: str,
        expected_sha256: str,
        expected_bytes: int,
        media_type: str,
        registry_alias: str | None = None,
    ) -> ArtifactProjection:
        try:
            path = contained_path(self.root, source_relative_path)
        except PathSecurityError as exc:
            raise CloudIntegrityError("ECO_ARTIFACT_PATH_UNSAFE") from exc
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != expected_bytes
            or _sha256_file(path) != expected_sha256
        ):
            raise CloudIntegrityError("ECO_ARTIFACT_SOURCE_HASH_MISMATCH")
        return ArtifactProjection(
            local_artifact_id=local_artifact_id,
            artifact_type=artifact_type,
            logical_id=logical_id,
            logical_name=logical_name,
            source_relative_path=PurePosixPath(source_relative_path).as_posix(),
            sha256=expected_sha256,
            bytes=expected_bytes,
            media_type=media_type,
            registry_alias=registry_alias,
        )

    def _stage_json(
        self,
        *,
        category: str,
        logical_id: str,
        value: Any,
        artifact_type: str,
        logical_name: str,
        registry_alias: str | None = None,
    ) -> ArtifactProjection:
        _assert_no_local_paths(value)
        payload = (canonical_json(value) + "\n").encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        filename = f"{_safe_component(logical_id, fallback='snapshot')}-{digest[:16]}.json"
        relative = PurePosixPath(".scenario-cloud") / self.profile.namespace / "projections"
        relative = relative / _safe_component(category, fallback="event") / filename
        path = contained_path(self.root, relative.as_posix())
        if path.exists():
            if path.is_symlink() or path.read_bytes() != payload:
                raise CloudIntegrityError("ECO_STAGED_PROJECTION_CONFLICT")
        else:
            atomic_write_json(path, value, mode=0o600)
        return self._verified_artifact(
            local_artifact_id=f"snapshot:{artifact_type}:{digest}",
            artifact_type=artifact_type,
            logical_id=logical_id,
            logical_name=logical_name,
            source_relative_path=relative.as_posix(),
            expected_sha256=digest,
            expected_bytes=len(payload),
            media_type="application/json",
            registry_alias=registry_alias,
        )

    def _asset_source(self, bundle: ConnectivityProjectBundle, asset_id: str) -> str:
        asset = next((item for item in bundle.assets if item.asset_id == asset_id), None)
        if asset is None:
            raise CloudPolicyError("ECO_SYNC_PLAN_ASSET_UNKNOWN")
        candidates = [PurePosixPath(asset.relative_path)]
        project = self.store.project()
        source_sha = str(project["skeleton"].get("source_bundle_sha256", ""))
        if source_sha:
            manifest = self.store.get_record("import_manifest", source_sha)
            materialized = PurePosixPath(str(manifest["materialized_directory"]))
            candidates.insert(0, materialized / asset.relative_path)
        for relative in candidates:
            try:
                path = contained_path(self.root, relative.as_posix())
            except PathSecurityError:
                continue
            if (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size == asset.byte_size
                and _sha256_file(path) == asset.sha256
            ):
                return relative.as_posix()
        raise CloudIntegrityError("ECO_AUTHORIZED_ASSET_BYTES_UNAVAILABLE")

    def _bundle_import(self, row: dict[str, Any], payload: dict[str, Any]) -> TelemetryProjection:
        revision = int(payload.get("record_revision", 0))
        if revision < 1:
            raise CloudIntegrityError("ECO_BUNDLE_REVISION_INVALID")
        bundle = ConnectivityProjectBundle.model_validate(
            self.store.get_record_revision("bundle", str(payload.get("project_id", "")), revision)
        )
        bundle_payload = bundle.model_dump(mode="json")
        digest = hashlib.sha256(canonical_json(bundle_payload).encode("utf-8")).hexdigest()
        if payload.get("bundle_sha256") != digest or payload.get("project_id") != bundle.project_id:
            raise CloudIntegrityError("ECO_BUNDLE_OUTBOX_MISMATCH")
        manifest_reference = payload.get("import_manifest")
        project = self.store.project()
        source_sha = str(project["skeleton"].get("source_bundle_sha256", ""))
        if source_sha:
            if not isinstance(manifest_reference, dict):
                raise CloudIntegrityError("ECO_IMPORT_MANIFEST_REFERENCE_MISSING")
            manifest_id = str(manifest_reference.get("record_id", ""))
            manifest_revision = manifest_reference.get("record_revision")
            if manifest_id != source_sha or not isinstance(manifest_revision, int):
                raise CloudIntegrityError("ECO_IMPORT_MANIFEST_REFERENCE_MISMATCH")
            manifest = self.store.get_record_revision(
                "import_manifest", manifest_id, manifest_revision
            )
            manifest_sha256 = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
            if (
                manifest_reference.get("sha256") != manifest_sha256
                or manifest.get("archive_sha256") != source_sha
            ):
                raise CloudIntegrityError("ECO_IMPORT_MANIFEST_OUTBOX_MISMATCH")
        elif manifest_reference is not None:
            raise CloudIntegrityError("ECO_UNEXPECTED_IMPORT_MANIFEST_REFERENCE")
        cloud_bundle_payload = self._cloud_bundle_payload(bundle)
        descriptor = self._stage_json(
            category="bundle",
            logical_id=f"{bundle.project_id}:{digest}",
            value=cloud_bundle_payload,
            artifact_type="connectivity_project",
            logical_name="connectivity-project.json",
        )
        assets = {item.asset_id: item for item in bundle.assets}
        artifacts = [descriptor]
        table_rows: list[list[Any]] = []
        for uploaded_id, plans in sorted(self.asset_projections.items()):
            asset = assets.get(uploaded_id)
            if asset is None or any(asset.byte_size != plan["bytes"] for plan in plans):
                raise CloudPolicyError("ECO_SYNC_PLAN_ASSET_METADATA_MISMATCH")
            relative = self._asset_source(bundle, uploaded_id)
            artifacts.append(
                self._verified_artifact(
                    local_artifact_id=f"asset:{asset.asset_id}:{asset.sha256}",
                    artifact_type="data_asset",
                    logical_id=f"{bundle.project_id}:{asset.asset_id}:{asset.sha256}",
                    logical_name=(
                        "assets/"
                        f"{_safe_component(asset.asset_id, fallback='asset')}/"
                        f"{_safe_component(Path(asset.relative_path).name, fallback='data')}"
                    ),
                    source_relative_path=relative,
                    expected_sha256=asset.sha256,
                    expected_bytes=asset.byte_size,
                    media_type=asset.media_type,
                )
            )
            for plan in sorted(
                plans, key=lambda item: (item["source_asset_id"], item["representation"])
            ):
                table_rows.append(
                    [
                        self._cloud_source_reference(plan),
                        asset.asset_id,
                        plan["representation"],
                        asset.kind.value,
                        asset.sha256,
                        asset.byte_size,
                        asset.authorization.cloud_disposition.value,
                        asset.authorization.sensitivity.value,
                    ]
                )
        uploaded_bytes = sum(assets[item].byte_size for item in self.asset_projections)
        return self._base(
            row,
            run_kind="bundle_import",
            run_subject_id=bundle.project_id,
            event_sequence=revision,
            config={
                "revision_id": bundle.revision.revision_id,
                "bundle_sha256": digest,
                "schema_version": bundle.schema_version,
            },
            history={
                "record_revision": revision,
                "authorized_source_count": len(table_rows),
            },
            summary={
                "bundle_sha256": digest,
                "authorized_asset_count": len(self.asset_projections),
                "authorized_source_count": len(table_rows),
                "authorized_asset_bytes": uploaded_bytes,
            },
            tables=[
                TableProjection(
                    logical_name="data_assets",
                    columns=[
                        "source_asset_id",
                        "uploaded_asset_id",
                        "representation",
                        "kind",
                        "sha256",
                        "bytes",
                        "cloud_disposition",
                        "sensitivity",
                    ],
                    rows=table_rows,
                )
            ],
            artifacts=artifacts,
        )

    def _analysis_plan(self, row: dict[str, Any], payload: dict[str, Any]) -> TelemetryProjection:
        """Publish the immutable model snapshot actually bound to an analysis.

        Bundle-import telemetry is not an authoritative analysis input: a
        planning project may never have one, and an imported project may have
        several revisions.  The analysis snapshot is therefore its own
        producer event.  Every subsequent direct-job event pins its W&B input
        lineage to this artifact version.
        """

        analysis_id = str(payload.get("analysis_id", ""))
        if not analysis_id or analysis_id != row.get("analysis_id"):
            raise CloudIntegrityError("ECO_ANALYSIS_PLAN_OUTBOX_MISMATCH")
        self._require_current_analysis_asset_authorization(analysis_id)
        analysis = self.store.analysis(analysis_id)
        run_plan = ConnectivityRunPlan.model_validate(analysis["plan"])
        bundle = self.store.analysis_bundle(analysis_id)
        bundle_payload = bundle.model_dump(mode="json")
        digest = hashlib.sha256(canonical_json(bundle_payload).encode("utf-8")).hexdigest()
        if (
            payload.get("bundle_sha256") != digest
            or payload.get("revision_id") != bundle.revision.revision_id
            or run_plan.revision_id != bundle.revision.revision_id
        ):
            raise CloudIntegrityError("ECO_ANALYSIS_PLAN_SNAPSHOT_MISMATCH")

        cloud_bundle_payload = self._cloud_bundle_payload(bundle)
        descriptor = self._stage_json(
            category="analysis-plan",
            logical_id=f"{analysis_id}:{digest}",
            value=cloud_bundle_payload,
            artifact_type="connectivity_project",
            logical_name="analysis/connectivity-project.json",
        )
        assets = {item.asset_id: item for item in bundle.assets}
        artifacts = [descriptor]
        table_rows: list[list[Any]] = []
        for uploaded_id, plans in sorted(self.asset_projections.items()):
            asset = assets.get(uploaded_id)
            if asset is None or any(asset.byte_size != plan["bytes"] for plan in plans):
                raise CloudPolicyError("ECO_SYNC_PLAN_ASSET_METADATA_MISMATCH")
            relative = self._asset_source(bundle, uploaded_id)
            artifacts.append(
                self._verified_artifact(
                    local_artifact_id=f"analysis-asset:{analysis_id}:{asset.asset_id}:{asset.sha256}",
                    artifact_type="data_asset",
                    logical_id=f"{analysis_id}:{asset.asset_id}:{asset.sha256}",
                    logical_name=(
                        "analysis/assets/"
                        f"{_safe_component(asset.asset_id, fallback='asset')}/"
                        f"{_safe_component(Path(asset.relative_path).name, fallback='data')}"
                    ),
                    source_relative_path=relative,
                    expected_sha256=asset.sha256,
                    expected_bytes=asset.byte_size,
                    media_type=asset.media_type,
                )
            )
            for plan in sorted(
                plans, key=lambda item: (item["source_asset_id"], item["representation"])
            ):
                table_rows.append(
                    [
                        self._cloud_source_reference(plan),
                        asset.asset_id,
                        plan["representation"],
                        asset.kind.value,
                        asset.sha256,
                        asset.byte_size,
                        asset.authorization.cloud_disposition.value,
                        asset.authorization.sensitivity.value,
                    ]
                )
        uploaded_bytes = sum(assets[item].byte_size for item in self.asset_projections)

        tables = [
            TableProjection(
                logical_name="analysis_assets",
                columns=[
                    "source_asset_id",
                    "uploaded_asset_id",
                    "representation",
                    "kind",
                    "sha256",
                    "bytes",
                    "cloud_disposition",
                    "sensitivity",
                ],
                rows=table_rows,
            ),
            self._search_batches_table(run_plan, state="PLANNED"),
        ]
        if run_plan.search_resource_observation is not None:
            tables.append(
                TableProjection(
                    logical_name="resource_observations",
                    columns=_RESOURCE_COLUMNS,
                    rows=[
                        _resource_row(
                            analysis_id,
                            run_plan.search_resource_observation,
                        )
                    ],
                )
            )

        history: dict[str, Any] = {"planned_direct_jobs": run_plan.direct_job_count}
        if run_plan.search_resource_observation is not None:
            history["proposer_wall_seconds"] = (
                run_plan.search_resource_observation.proposer_wall_seconds
            )

        return self._base(
            row,
            run_kind="connectivity_analysis",
            run_subject_id=analysis_id,
            event_sequence=self._stream_sequence(
                row,
                run_kind="connectivity_analysis",
                run_subject_id=analysis_id,
            ),
            config={
                "revision_id": bundle.revision.revision_id,
                "bundle_sha256": digest,
                "engine_manifest_sha256": run_plan.engine_manifest_sha256,
                "search_method": run_plan.search_method,
                "seed": run_plan.seed,
                "direct_job_count": run_plan.direct_job_count,
            },
            history=history,
            summary={
                "analysis_state": analysis["state"],
                "bundle_sha256": digest,
                "authorized_asset_count": len(self.asset_projections),
                "authorized_source_count": len(table_rows),
                "authorized_asset_bytes": uploaded_bytes,
            },
            tables=tables,
            input_artifact_refs=self._bundle_receipt_refs(
                bundle.revision.revision_id, required=False
            ),
            artifacts=artifacts,
        )

    def _semantic_revision(
        self, row: dict[str, Any], payload: dict[str, Any]
    ) -> TelemetryProjection:
        """Mirror one committed semantic revision without reconstructing mutable state."""

        revision = payload.get("revision")
        semantic_diff = payload.get("semantic_diff")
        record_revisions = payload.get("record_revisions")
        if (
            not isinstance(revision, dict)
            or not isinstance(semantic_diff, dict)
            or not isinstance(record_revisions, list)
        ):
            raise CloudIntegrityError("ECO_SEMANTIC_REVISION_OUTBOX_INVALID")
        revision_id = str(revision.get("revision_id", ""))
        diff_id = str(semantic_diff.get("diff_id", ""))
        if (
            not revision_id
            or row.get("subject_id") != revision_id
            or revision.get("project_id") != self.store.project()["project_id"]
            or revision.get("diff_id") != diff_id
            or semantic_diff.get("to_revision_id") != revision_id
        ):
            raise CloudIntegrityError("ECO_SEMANTIC_REVISION_OUTBOX_MISMATCH")
        retained_revision = self.store.get_record("revision", revision_id)
        retained_diff = self.store.get_record("semantic_diff", diff_id)
        if retained_revision != revision or retained_diff != semantic_diff:
            raise CloudIntegrityError("ECO_SEMANTIC_REVISION_RECORD_MISMATCH")

        records: list[dict[str, Any]] = []
        record_rows: list[list[Any]] = []
        normalized_references: list[tuple[str, str, int]] = []
        for item in record_revisions:
            if not isinstance(item, dict):
                raise CloudIntegrityError("ECO_SEMANTIC_REVISION_REFERENCE_INVALID")
            record_type = str(item.get("record_type", ""))
            record_id = str(item.get("record_id", ""))
            try:
                record_revision = int(item.get("revision", 0))
            except (TypeError, ValueError) as exc:
                raise CloudIntegrityError("ECO_SEMANTIC_REVISION_REFERENCE_INVALID") from exc
            if not record_type or not record_id or record_revision < 1:
                raise CloudIntegrityError("ECO_SEMANTIC_REVISION_REFERENCE_INVALID")
            normalized_references.append((record_type, record_id, record_revision))
        if normalized_references != sorted(set(normalized_references)):
            raise CloudIntegrityError("ECO_SEMANTIC_REVISION_REFERENCES_NOT_CANONICAL")
        for record_type, record_id, record_revision in normalized_references:
            asset_plan = self.source_projections.get(record_id)
            if (
                record_type == "asset"
                and asset_plan is not None
                and asset_plan["representation"] == "GENERALIZED_DERIVATIVE"
            ):
                raise CloudPolicyError("ECO_TELEMETRY_GENERALIZED_ASSET_REVISION_UNSUPPORTED")
            value = self.store.get_record_revision(record_type, record_id, record_revision)
            record_digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
            records.append(
                {
                    "record_type": record_type,
                    "record_id": record_id,
                    "revision": record_revision,
                    "sha256": record_digest,
                    "value": value,
                }
            )
            record_rows.append([record_type, record_id, record_revision, record_digest])

        snapshot_value = {
            "schema_version": "ecocorridor-semantic-revision/v1.0",
            "revision": revision,
            "semantic_diff": semantic_diff,
            "records": records,
        }
        snapshot = self._stage_json(
            category="semantic-revision",
            logical_id=revision_id,
            value=snapshot_value,
            artifact_type="connectivity_project",
            logical_name=f"revisions/{_safe_component(revision_id, fallback='revision')}.json",
        )
        predecessors = self._receipt_refs(
            event_type="semantic_revision",
            subject_id=str(revision.get("parent_revision_id", "")),
            required=False,
            require_each=True,
        )
        if not predecessors:
            predecessors = self._bundle_receipt_refs(
                str(revision.get("parent_revision_id", "")), required=False
            )
        rank = self._planning_rank(row)
        return self._base(
            row,
            run_kind="planning_model",
            run_subject_id=self.store.project()["project_id"],
            event_sequence=rank,
            config={
                "revision_id": revision_id,
                "parent_revision_id": revision.get("parent_revision_id"),
                "model_sha256": revision.get("model_sha256"),
                "semantic_diff_sha256": semantic_diff.get("sha256"),
            },
            history={"semantic_revision_sequence": rank},
            summary={
                "current_revision_id": revision_id,
                "changed_record_count": len(record_rows),
            },
            tables=[
                TableProjection(
                    logical_name="semantic_changes",
                    columns=["record_type", "record_id", "revision", "sha256"],
                    rows=record_rows,
                )
            ],
            input_artifact_refs=predecessors,
            artifacts=[snapshot],
        )

    def _planning_rank(self, row: dict[str, Any]) -> int:
        events = [
            event
            for event_type in ("semantic_revision", "model_attestation")
            for event in self.store.telemetry_rows(event_type=event_type)
        ]
        events.sort(key=lambda item: (str(item["created_at"]), str(item["outbox_id"])))
        for index, event in enumerate(events, start=1):
            if event["outbox_id"] == row["outbox_id"]:
                return index
        raise CloudIntegrityError("ECO_PLANNING_EVENT_NOT_AUTHORITATIVE")

    def _model_attestation(
        self, row: dict[str, Any], payload: dict[str, Any]
    ) -> TelemetryProjection:
        """Project a retained, validation-bound Model Fitness attestation."""

        attestation_id = str(payload.get("attestation_id", ""))
        passport_id = str(payload.get("passport_id", ""))
        validation_id = str(payload.get("validation_id", ""))
        revision_id = str(payload.get("revision_id", ""))
        try:
            fitness_revision = int(payload.get("fitness_record_revision", 0))
        except (TypeError, ValueError) as exc:
            raise CloudIntegrityError("ECO_MODEL_ATTESTATION_OUTBOX_INVALID") from exc
        if (
            not attestation_id
            or row.get("subject_id") != attestation_id
            or not passport_id
            or not validation_id
            or not revision_id
            or fitness_revision < 1
        ):
            raise CloudIntegrityError("ECO_MODEL_ATTESTATION_OUTBOX_INVALID")
        fitness = self.store.get_record_revision("fitness", passport_id, fitness_revision)
        validation = self.store.get_record("validation", validation_id)
        attestation = self.store.get_record("attestation", attestation_id)
        fitness_sha256 = canonical_sha256(fitness)
        if (
            fitness_sha256 != payload.get("candidate_fitness_sha256")
            or fitness.get("revision_id") != revision_id
            or validation.get("validation_id") != validation_id
            or validation.get("revision_id") != revision_id
            or validation.get("model_sha256") != payload.get("model_sha256")
            or validation.get("status") != "READY"
            or attestation.get("attestation_id") != attestation_id
            or attestation.get("validation_id") != validation_id
            or attestation.get("fitness_revision") != fitness_revision
        ):
            raise CloudIntegrityError("ECO_MODEL_ATTESTATION_RECORD_MISMATCH")
        snapshot_value = {
            "schema_version": "ecocorridor-model-attestation/v1.0",
            "revision_id": revision_id,
            "model_sha256": payload.get("model_sha256"),
            "fitness": fitness,
            "validation": validation,
            "attestation": attestation,
        }
        snapshot = self._stage_json(
            category="model-attestation",
            logical_id=attestation_id,
            value=snapshot_value,
            artifact_type="decision_evidence",
            logical_name=(
                f"fitness/{_safe_component(attestation_id, fallback='attestation')}.json"
            ),
        )
        predecessors = self._receipt_refs(
            event_type="semantic_revision",
            subject_id=revision_id,
            required=False,
            require_each=True,
        )
        if not predecessors:
            predecessors = self._bundle_receipt_refs(revision_id, required=False)
        rank = self._planning_rank(row)
        dimensions = fitness.get("dimensions", [])
        return self._base(
            row,
            run_kind="planning_model",
            run_subject_id=self.store.project()["project_id"],
            event_sequence=rank,
            config={
                "revision_id": revision_id,
                "model_sha256": payload.get("model_sha256"),
                "passport_id": passport_id,
                "validation_id": validation_id,
            },
            history={"model_fitness_attestation_sequence": rank},
            summary={
                "attestation_id": attestation_id,
                "fitness_dimension_count": len(dimensions),
                "validation_status": validation.get("status"),
                "unresolved_assumption_count": len(fitness.get("unresolved_assumptions", [])),
            },
            tables=[
                TableProjection(
                    logical_name="model_fitness",
                    columns=["dimension", "status", "reviewer", "evidence_count"],
                    rows=[
                        [
                            item.get("key"),
                            item.get("status"),
                            item.get("reviewer"),
                            len(item.get("evidence_ids", [])),
                        ]
                        for item in dimensions
                    ],
                )
            ],
            input_artifact_refs=predecessors,
            artifacts=[snapshot],
        )

    def _guided_rank(self, session_id: str, outbox_id: str) -> int:
        candidates = self._guided_events(session_id)
        for index, item in enumerate(candidates, start=1):
            if item["outbox_id"] == outbox_id:
                return index
        raise CloudIntegrityError("ECO_GUIDED_EVENT_NOT_AUTHORITATIVE")

    def _guided_events(self, session_id: str) -> list[dict[str, Any]]:
        candidates = []
        for event_type in ("guided_session", "guided_checkpoint"):
            for record in self.store.telemetry_rows(event_type=event_type):
                if str(record["payload"].get("session_id", "")) == session_id:
                    candidates.append(record)
        candidates.sort(key=lambda item: (str(item["created_at"]), str(item["outbox_id"])))
        return candidates

    def _guided_session(self, row: dict[str, Any], payload: dict[str, Any]) -> TelemetryProjection:
        session_id = str(payload.get("session_id", ""))
        session = self.store.guided(session_id)
        event_projection = payload.get("projection")
        if not isinstance(event_projection, dict):
            raise CloudIntegrityError("ECO_GUIDED_PROJECTION_MISSING")
        projection_json = canonical_json(event_projection)
        digest = hashlib.sha256(projection_json.encode("utf-8")).hexdigest()
        revision = int(payload.get("revision", 0))
        if (
            payload.get("projection_sha256") != digest
            or revision < 1
            or revision > int(session["revision"])
        ):
            raise CloudIntegrityError("ECO_GUIDED_SESSION_OUTBOX_MISMATCH")
        if revision == int(session["revision"]) and (
            payload.get("state") != session["state"] or event_projection != session["projection"]
        ):
            raise CloudIntegrityError("ECO_GUIDED_SESSION_OUTBOX_MISMATCH")
        snapshot = self._stage_json(
            category="guided",
            logical_id=f"{session_id}:{digest}",
            value=event_projection,
            artifact_type="guided_fixture",
            logical_name="guided/ridgeway-projection.json",
            registry_alias=(
                "guided" if event_projection.get("verified_fixture_replay") is True else None
            ),
        )
        event_rank = self._guided_rank(session_id, str(row["outbox_id"]))
        preceding = self._guided_events(session_id)[:event_rank]
        checkpoint_count = sum(item["event_type"] == "guided_checkpoint" for item in preceding)
        artifacts = [snapshot]
        input_artifact_refs: list[str] = []
        config: dict[str, Any] = {"fixture_projection_sha256": digest}
        history: dict[str, Any] = {
            "session_revision": revision,
            "checkpoint_count": checkpoint_count,
        }
        summary: dict[str, Any] = {
            "state": payload.get("state"),
            "checkpoint_count": checkpoint_count,
        }

        rerun = payload.get("rerun_completion")
        if rerun is not None:
            if not isinstance(rerun, dict) or payload.get("state") != "COMPLETED":
                raise CloudIntegrityError("ECO_GUIDED_RERUN_COMPLETION_INVALID")
            analysis_id = str(rerun.get("analysis_id", ""))
            map_analysis_id = str(rerun.get("map_analysis_id", ""))
            differential_sha256 = str(rerun.get("differential_sha256", ""))
            differential = rerun.get("differential")
            expected_relative = (
                PurePosixPath("blobs")
                / "sha256"
                / differential_sha256[:2]
                / differential_sha256[2:]
            ).as_posix()
            if (
                not analysis_id
                or not map_analysis_id
                or analysis_id == map_analysis_id
                or row.get("analysis_id") != analysis_id
                or int(rerun.get("total_jobs", -1)) != 1_596
                or int(rerun.get("matrix_jobs", -1)) != 1_536
                or int(rerun.get("map_jobs", -1)) != 60
                or len(differential_sha256) != 64
                or any(character not in "0123456789abcdef" for character in differential_sha256)
                or rerun.get("differential_cas_relative_path") != expected_relative
                or not isinstance(differential, dict)
            ):
                raise CloudIntegrityError("ECO_GUIDED_RERUN_COMPLETION_INVALID")
            try:
                retained = self.store.get_record("guided_differential", analysis_id)
                matrix_state = self.store.analysis(analysis_id)["state"]
                map_state = self.store.analysis(map_analysis_id)["state"]
            except KeyError as exc:
                raise CloudIntegrityError("ECO_GUIDED_RERUN_COMPLETION_MISSING") from exc
            if (
                retained != {**differential, "artifact_sha256": differential_sha256}
                or matrix_state != "COMPLETED"
                or map_state != "COMPLETED"
            ):
                raise CloudIntegrityError("ECO_GUIDED_RERUN_COMPLETION_MISMATCH")
            try:
                differential_path = contained_path(self.root, expected_relative)
            except PathSecurityError as exc:
                raise CloudIntegrityError("ECO_GUIDED_RERUN_DIFFERENTIAL_PATH_UNSAFE") from exc
            if not differential_path.is_file() or differential_path.is_symlink():
                raise CloudIntegrityError("ECO_GUIDED_RERUN_DIFFERENTIAL_MISSING")
            differential_artifact = self._verified_artifact(
                local_artifact_id=f"guided-differential:{analysis_id}:{differential_sha256}",
                artifact_type="decision_evidence",
                logical_id=f"{session_id}:{analysis_id}:{differential_sha256}",
                logical_name="guided/ridgeway-rerun-differential.json",
                source_relative_path=expected_relative,
                expected_sha256=differential_sha256,
                expected_bytes=differential_path.stat().st_size,
                media_type="application/json",
            )
            artifacts.append(differential_artifact)
            input_artifact_refs = [
                *self._receipt_refs(
                    event_type="analysis_completion",
                    analysis_id=analysis_id,
                    required=True,
                    require_each=True,
                ),
                *self._receipt_refs(
                    event_type="analysis_completion",
                    analysis_id=map_analysis_id,
                    required=True,
                    require_each=True,
                ),
            ]
            config.update(
                {
                    "matrix_analysis_id": analysis_id,
                    "map_analysis_id": map_analysis_id,
                    "differential_sha256": differential_sha256,
                }
            )
            history.update(
                {
                    "direct_jobs_completed": 1_596,
                    "matrix_jobs_completed": 1_536,
                    "map_jobs_completed": 60,
                }
            )
            summary.update(
                {
                    "differential_accepted": differential.get("accepted"),
                    "differential_report_sha256": differential.get("report_sha256"),
                    "direct_jobs_completed": 1_596,
                }
            )
        else:
            input_artifact_refs = self._receipt_refs(
                event_type="bundle_import", required=True, latest_only=True
            )
        return self._base(
            row,
            run_kind="guided_session",
            run_subject_id=session_id,
            event_sequence=event_rank,
            config=config,
            history=history,
            summary=summary,
            input_artifact_refs=input_artifact_refs,
            artifacts=artifacts,
        )

    def _guided_checkpoint(
        self, row: dict[str, Any], payload: dict[str, Any]
    ) -> TelemetryProjection:
        session_id = str(payload.get("session_id", ""))
        sequence = int(payload.get("sequence_number", 0))
        checkpoint = next(
            (
                item
                for item in self.store.guided_checkpoints(session_id)
                if item["event_id"] == payload.get("event_id")
            ),
            None,
        )
        if checkpoint is None or int(checkpoint["sequence_number"]) != sequence:
            raise CloudIntegrityError("ECO_GUIDED_CHECKPOINT_OUTBOX_MISMATCH")
        details = checkpoint["payload"]
        return self._base(
            row,
            run_kind="guided_session",
            run_subject_id=session_id,
            event_sequence=self._guided_rank(session_id, str(row["outbox_id"])),
            history={
                "checkpoint_sequence": sequence,
                "duration_ms": int(details.get("duration_ms", 0)),
                "evidence_opened": len(details.get("evidence_ids", [])),
                "alternatives_compared": len(details.get("portfolio_ids", [])),
            },
            summary={"last_checkpoint_id": checkpoint["checkpoint_id"]},
            tables=[
                TableProjection(
                    logical_name="guided_checkpoints",
                    columns=[
                        "sequence",
                        "checkpoint_id",
                        "event_kind",
                        "duration_ms",
                        "evidence_count",
                        "portfolio_count",
                    ],
                    rows=[
                        [
                            sequence,
                            checkpoint["checkpoint_id"],
                            checkpoint["event_kind"],
                            int(details.get("duration_ms", 0)),
                            len(details.get("evidence_ids", [])),
                            len(details.get("portfolio_ids", [])),
                        ]
                    ],
                )
            ],
            input_artifact_refs=self._receipt_refs(
                event_type="guided_session",
                subject_id=session_id,
                required=True,
                first_only=True,
            ),
        )

    def _direct_job_terminal(
        self, row: dict[str, Any], payload: dict[str, Any]
    ) -> TelemetryProjection:
        analysis_id = str(row.get("analysis_id", ""))
        self._require_current_analysis_asset_authorization(analysis_id)
        direct_run_id = str(payload.get("direct_job_id", ""))
        jobs = self.store.direct_jobs(analysis_id)
        job = next((item for item in jobs if item["direct_job_id"] == direct_run_id), None)
        if job is None or job["state"] != payload.get("state"):
            raise CloudIntegrityError("ECO_DIRECT_JOB_OUTBOX_MISMATCH")
        event_sequence = self._stream_sequence(
            row,
            run_kind="connectivity_analysis",
            run_subject_id=analysis_id,
        )
        artifacts: list[ArtifactProjection] = []
        qa_rows: list[list[Any]] = []
        metric_rows: list[list[Any]] = []
        run = None
        if job["state"] == "SUCCEEDED":
            runs, _, _ = self.store.analysis_results(analysis_id)
            run = next((item for item in runs if item.direct_run_id == direct_run_id), None)
            if run is None or run.result_sha256 != payload.get("result_sha256"):
                raise CloudIntegrityError("ECO_DIRECT_RUN_EVIDENCE_MISSING")
            for item in self.store.run_artifacts(direct_run_id):
                role = str(item["role"])
                if role in {"stdout", "stderr"}:
                    continue
                suffix = "json" if item["media_type"] == "application/json" else "dat"
                artifacts.append(
                    self._verified_artifact(
                        local_artifact_id=f"direct:{direct_run_id}:{role}:{item['sha256']}",
                        artifact_type="direct_run_batch",
                        logical_id=f"{analysis_id}:{direct_run_id}:{role}",
                        logical_name=(
                            f"runs/{_safe_component(direct_run_id, fallback='run')}/"
                            f"{_safe_component(role, fallback='artifact')}.{suffix}"
                        ),
                        source_relative_path=str(item["cas_relative_path"]),
                        expected_sha256=str(item["sha256"]),
                        expected_bytes=int(item["byte_size"]),
                        media_type=str(item["media_type"]),
                    )
                )
            metric_rows = [
                [
                    run.direct_run_id,
                    run.portfolio_id,
                    run.scenario_id,
                    metric.pair_id,
                    metric.effective_resistance,
                    metric.baseline_relative_change_percent,
                ]
                for metric in run.pair_metrics
            ]
            qa_rows = [
                [run.direct_run_id, run.qa_status.value if run.qa_status else None, finding]
                for finding in (run.qa_findings or [""])
            ]
        terminal_events = [
            item
            for item in self.store.telemetry_rows(
                event_type="direct_job_terminal",
                analysis_id=analysis_id,
            )
            if item.get("stream_id") == row.get("stream_id")
            and int(item.get("stream_sequence", 0)) <= event_sequence
        ]
        if not terminal_events or terminal_events[-1]["outbox_id"] != row["outbox_id"]:
            raise CloudIntegrityError("ECO_DIRECT_JOB_BARRIER_SEQUENCE_INVALID")
        counts: dict[str, int] = {}
        for item in terminal_events:
            state = str(item["payload"].get("state", ""))
            if state not in {"SUCCEEDED", "FAILED", "QA_REJECTED", "CANCELLED"}:
                raise CloudIntegrityError("ECO_DIRECT_JOB_BARRIER_STATE_INVALID")
            counts[state] = counts.get(state, 0) + 1
        plan = self.store.analysis(analysis_id)["plan"]
        observation = (
            run.resource_observation
            if run is not None
            else (
                ComputationalResourceObservation.model_validate(job["resource_observation"])
                if job.get("resource_observation") is not None
                else None
            )
        )
        tables = [
            TableProjection(
                logical_name="solver_jobs",
                columns=[
                    "direct_job_id",
                    "portfolio_id",
                    "scenario_id",
                    "terminal_sequence",
                    "planned_sequence",
                    "state",
                    "cached",
                    "attempt_count",
                    "result_sha256",
                    "resource_observation_id",
                    "resource_boundary",
                    "solver_wall_seconds",
                    "cache_lookup_wall_seconds",
                    "peak_memory_status",
                    "peak_memory_bytes",
                    "energy_status",
                    "energy_kwh",
                ],
                rows=[
                    [
                        direct_run_id,
                        job["portfolio_id"],
                        job["scenario_id"],
                        event_sequence,
                        job["sequence_number"],
                        job["state"],
                        bool(job["cached"]),
                        job["attempt_count"],
                        payload.get("result_sha256"),
                        observation.observation_id if observation else None,
                        observation.boundary.value if observation else None,
                        observation.solver_wall_seconds if observation else None,
                        observation.cache_lookup_wall_seconds if observation else None,
                        observation.peak_memory_status.value if observation else None,
                        observation.peak_memory_bytes if observation else None,
                        observation.energy_status.value if observation else None,
                        observation.energy_kwh if observation else None,
                    ]
                ],
            )
        ]
        if observation is not None:
            tables.append(
                TableProjection(
                    logical_name="resource_observations",
                    columns=_RESOURCE_COLUMNS,
                    rows=[_resource_row(direct_run_id, observation)],
                )
            )
        if metric_rows:
            tables.append(
                TableProjection(
                    logical_name="pair_metrics",
                    columns=[
                        "direct_run_id",
                        "portfolio_id",
                        "scenario_id",
                        "pair_id",
                        "effective_resistance",
                        "baseline_relative_change_percent",
                    ],
                    rows=metric_rows,
                )
            )
        if qa_rows:
            tables.append(
                TableProjection(
                    logical_name="qa_findings",
                    columns=["direct_run_id", "status", "finding"],
                    rows=qa_rows,
                )
            )
        return self._base(
            row,
            run_kind="connectivity_analysis",
            run_subject_id=analysis_id,
            event_sequence=event_sequence,
            config={
                "revision_id": plan["revision_id"],
                "engine_manifest_sha256": plan["engine_manifest_sha256"],
                "search_method": plan["search_method"],
                "seed": plan["seed"],
                "direct_job_count": plan["direct_job_count"],
            },
            history={
                "terminal_sequence": event_sequence,
                "succeeded_jobs": counts.get("SUCCEEDED", 0),
                "failed_jobs": counts.get("FAILED", 0),
                "qa_rejected_jobs": counts.get("QA_REJECTED", 0),
                "cancelled_jobs": counts.get("CANCELLED", 0),
                **(
                    {"solver_wall_seconds": observation.solver_wall_seconds}
                    if observation is not None and observation.solver_wall_seconds is not None
                    else {}
                ),
                **(
                    {"cache_lookup_wall_seconds": observation.cache_lookup_wall_seconds}
                    if observation is not None and observation.cache_lookup_wall_seconds is not None
                    else {}
                ),
            },
            summary={
                "last_terminal_job_state": job["state"],
                "planned_direct_job_count": len(jobs),
                "terminal_job_count": sum(
                    counts.get(state, 0)
                    for state in ("SUCCEEDED", "FAILED", "QA_REJECTED", "CANCELLED")
                ),
                "cache_hits": sum(
                    1 for item in terminal_events if bool(item["payload"].get("cached"))
                ),
                "resource_observation_status": (
                    "RECORDED" if observation is not None else "NOT_RECORDED"
                ),
                "energy_status": (
                    observation.energy_status.value if observation is not None else "NOT_RECORDED"
                ),
            },
            tables=tables,
            input_artifact_refs=self._receipt_refs(
                event_type="analysis_plan",
                analysis_id=analysis_id,
                required=True,
                require_each=True,
            ),
            artifacts=artifacts,
        )

    def _analysis_completion(
        self, row: dict[str, Any], payload: dict[str, Any]
    ) -> TelemetryProjection:
        analysis_id = str(payload.get("analysis_id", ""))
        self._require_current_analysis_asset_authorization(analysis_id)
        analysis = self.store.analysis(analysis_id)
        if analysis["state"] != "COMPLETED" or analysis_id != row.get("analysis_id"):
            raise CloudIntegrityError("ECO_ANALYSIS_COMPLETION_OUTBOX_MISMATCH")
        run_plan = ConnectivityRunPlan.model_validate(analysis["plan"])
        runs, summaries, claims = self.store.analysis_results(analysis_id)
        if (
            sorted(run.direct_run_id for run in runs) != payload.get("direct_run_ids")
            or sorted(item.portfolio_id for item in summaries) != payload.get("portfolio_ids")
            or sorted(item.claim_id for item in claims) != payload.get("evidence_claim_ids")
        ):
            raise CloudIntegrityError("ECO_ANALYSIS_COMPLETION_EVIDENCE_MISMATCH")
        snapshot_value = {
            "schema_version": "ecocorridor-decision-evidence/v1.0",
            "analysis_id": analysis_id,
            "plan": analysis["plan"],
            "runs": [
                {
                    "direct_run_id": item.direct_run_id,
                    "portfolio_id": item.portfolio_id,
                    "scenario_id": item.scenario_id,
                    "result_sha256": item.result_sha256,
                    "matrix_sha256": item.matrix_sha256,
                    "current_map_sha256": item.current_map_sha256,
                    "qa_status": item.qa_status.value if item.qa_status else None,
                    "resource_observation": (
                        item.resource_observation.model_dump(mode="json")
                        if item.resource_observation is not None
                        else None
                    ),
                }
                for item in runs
            ],
            "summaries": [item.model_dump(mode="json") for item in summaries],
            "claims": [item.model_dump(mode="json") for item in claims],
        }
        snapshot = self._stage_json(
            category="analysis",
            logical_id=analysis_id,
            value=snapshot_value,
            artifact_type="decision_evidence",
            logical_name="decision/decision-evidence.json",
        )
        alternative_rows = [
            [
                item.portfolio_id,
                item.disposition.value,
                item.cost,
                item.currency,
                item.controlling_scenario_id,
                item.controlling_pair_id,
                item.worst_resistance_reduction_percent,
                item.safeguard_passed,
                item.review_mask_triggered,
                len(item.unresolved_feasibility),
            ]
            for item in summaries
        ]
        claim_rows = [
            [
                item.claim_id,
                item.portfolio_id,
                item.scenario_id,
                item.pair_id,
                item.rule_id,
                item.value,
                item.units,
                len(item.direct_run_ids),
            ]
            for item in claims
        ]
        observations = [
            (item.direct_run_id, item.resource_observation)
            for item in runs
            if item.resource_observation is not None
        ]
        tables = [
            TableProjection(
                logical_name="evaluated_alternatives",
                columns=[
                    "portfolio_id",
                    "disposition",
                    "cost",
                    "currency",
                    "controlling_scenario_id",
                    "controlling_pair_id",
                    "worst_resistance_reduction_percent",
                    "safeguard_passed",
                    "review_mask_triggered",
                    "unresolved_feasibility_count",
                ],
                rows=alternative_rows,
            ),
            TableProjection(
                logical_name="evidence_claims",
                columns=[
                    "claim_id",
                    "portfolio_id",
                    "scenario_id",
                    "pair_id",
                    "rule_id",
                    "value",
                    "units",
                    "direct_run_count",
                ],
                rows=claim_rows,
            ),
            self._search_batches_table(
                run_plan,
                state="COMPLETED",
                complete_portfolio_count=len(summaries),
            ),
        ]
        adverse_changes = self._adverse_changes_table(
            summaries=summaries,
            claims=claims,
            successful_direct_run_ids={
                item.direct_run_id for item in runs if item.state.value == "SUCCEEDED"
            },
        )
        if adverse_changes is not None:
            tables.append(adverse_changes)
        if observations:
            tables.append(
                TableProjection(
                    logical_name="resource_observations",
                    columns=_RESOURCE_COLUMNS,
                    rows=[
                        _resource_row(run_id, observation) for run_id, observation in observations
                    ],
                )
            )
        measured_solver_seconds = sum(
            observation.solver_wall_seconds or 0.0 for _, observation in observations
        )
        energy_status_counts = {
            status: sum(
                observation.energy_status.value == status for _, observation in observations
            )
            for status in ("MEASURED", "ESTIMATED", "NOT_MEASURED")
        }
        summary: dict[str, Any] = {
            "analysis_state": "COMPLETED",
            "exact_direct_runs": len(runs),
            "portfolio_count": len(summaries),
            "evidence_claim_count": len(claims),
            "passing_portfolio_count": sum(
                item.disposition.value == "PASSES" for item in summaries
            ),
            "resource_observations_recorded": len(observations),
            "resource_observations_not_recorded": len(runs) - len(observations),
            "measured_solver_wall_seconds": measured_solver_seconds,
            "energy_measured_observations": energy_status_counts["MEASURED"],
            "energy_estimated_observations": energy_status_counts["ESTIMATED"],
            "energy_not_measured_observations": energy_status_counts["NOT_MEASURED"],
        }
        measured_energy = [
            observation.energy_kwh
            for _, observation in observations
            if observation.energy_status.value == "MEASURED" and observation.energy_kwh is not None
        ]
        estimated_energy = [
            observation.energy_kwh
            for _, observation in observations
            if observation.energy_status.value == "ESTIMATED" and observation.energy_kwh is not None
        ]
        if measured_energy:
            summary["measured_energy_kwh"] = sum(measured_energy)
        if estimated_energy:
            summary["estimated_energy_kwh"] = sum(estimated_energy)
            summary["estimated_energy_lower_kwh"] = sum(
                observation.energy_estimate_lower_kwh or 0.0
                for _, observation in observations
                if observation.energy_status.value == "ESTIMATED"
            )
            summary["estimated_energy_upper_kwh"] = sum(
                observation.energy_estimate_upper_kwh or 0.0
                for _, observation in observations
                if observation.energy_status.value == "ESTIMATED"
            )
            summary["estimated_energy_boundary"] = (
                "Sum of independent process-active-duration proxy estimates; not measured "
                "wall-plug energy and not a carbon estimate."
            )
        observed_peak_memory = [
            observation.peak_memory_bytes
            for _, observation in observations
            if observation.peak_memory_bytes is not None
        ]
        if observed_peak_memory:
            summary["maximum_observed_single_process_memory_bytes"] = max(observed_peak_memory)
            summary["peak_memory_is_sampled_lower_bound"] = True
        return self._base(
            row,
            run_kind="connectivity_analysis",
            run_subject_id=analysis_id,
            event_sequence=self._stream_sequence(
                row,
                run_kind="connectivity_analysis",
                run_subject_id=analysis_id,
            ),
            history={"complete_portfolios": len(summaries), "exact_direct_runs": len(runs)},
            summary=summary,
            tables=tables,
            input_artifact_refs=self._receipt_refs(
                event_type="direct_job_terminal",
                analysis_id=analysis_id,
                required=True,
                require_each=True,
            ),
            artifacts=[snapshot],
        )

    def _reference_comparison(
        self, row: dict[str, Any], payload: dict[str, Any]
    ) -> TelemetryProjection:
        """Mirror one immutable direct reference comparison and its exact lineage.

        Reference matching is intentionally a separate provider run from the
        alternatives-analysis history.  Its local outbox stream therefore has
        its own monotonic clock and cannot regress an already-completed W&B
        analysis run.  The local repository remains authoritative: the
        projection revalidates source/reference bytes, retained direct-run
        artifacts, claims, and hashes before emitting any cloud payload.
        """

        result_id = str(row.get("subject_id", ""))
        analysis_id = str(row.get("analysis_id", ""))
        if not result_id or not analysis_id:
            raise CloudIntegrityError("ECO_REFERENCE_COMPARISON_IDENTITY_MISSING")
        expected_stream_id = f"reference_comparison:{analysis_id}"
        try:
            event_sequence = int(row["stream_sequence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CloudIntegrityError("ECO_REFERENCE_COMPARISON_STREAM_SEQUENCE_MISSING") from exc
        if row.get("stream_id") != expected_stream_id or event_sequence < 1:
            raise CloudIntegrityError("ECO_REFERENCE_COMPARISON_STREAM_MISMATCH")

        retained_rows = self.store.telemetry_rows(
            event_type="reference_comparison",
            analysis_id=analysis_id,
            subject_id=result_id,
        )
        retained_row = next(
            (item for item in retained_rows if item["outbox_id"] == row.get("outbox_id")),
            None,
        )
        if (
            retained_row is None
            or retained_row["stream_id"] != expected_stream_id
            or int(retained_row["stream_sequence"]) != event_sequence
            or retained_row["payload"] != payload
        ):
            raise CloudIntegrityError("ECO_REFERENCE_COMPARISON_OUTBOX_ROW_MISMATCH")

        self._require_current_analysis_asset_authorization(analysis_id)
        try:
            reference_plan, plan_sha256 = self.store.reference_comparison_plan(
                str(payload.get("plan_id", "")),
                analysis_id=analysis_id,
            )
            result, claims = self.store.reference_comparison_result(result_id)
            analysis_run_plan = ConnectivityRunPlan.model_validate(
                self.store.analysis(analysis_id)["plan"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CloudIntegrityError("ECO_REFERENCE_COMPARISON_EVIDENCE_INVALID") from exc
        result_payload = result.model_dump(mode="json")
        result_sha256 = hashlib.sha256(canonical_json(result_payload).encode("utf-8")).hexdigest()
        comparison_ids = sorted(item.comparison_id for item in result.scenario_comparisons)
        claim_ids = sorted(item.claim_id for item in claims)
        direct_run_ids = sorted(item.direct_run_id for item in result.scenario_comparisons)
        expected_payload = {
            "schema_version": "ecocorridor/v1.0",
            "result_id": result.result_id,
            "plan_id": result.plan_id,
            "analysis_id": result.analysis_id,
            "revision_id": result.revision_id,
            "model_sha256": result.model_sha256,
            "engine_manifest_sha256": result.engine_manifest_sha256,
            "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
            "status": result.status,
            "plan_payload_sha256": plan_sha256,
            "result_payload_sha256": result_sha256,
            "comparison_ids": comparison_ids,
            "claim_ids": claim_ids,
            "direct_run_ids": direct_run_ids,
        }
        if (
            payload != expected_payload
            or result.analysis_id != analysis_id
            or result.result_id != result_id
            or reference_plan.plan_id != result.plan_id
            or reference_plan.analysis_id != result.analysis_id
            or reference_plan.revision_id != result.revision_id
            or reference_plan.model_sha256 != result.model_sha256
            or reference_plan.engine_manifest_sha256 != result.engine_manifest_sha256
            or canonical_json(reference_plan.run_plan.model_dump(mode="json"))
            != canonical_json(analysis_run_plan.model_dump(mode="json"))
            or set(reference_plan.bound_scenario_ids)
            != {item.scenario_id for item in result.scenario_comparisons}
        ):
            raise CloudIntegrityError("ECO_REFERENCE_COMPARISON_OUTBOX_MISMATCH")

        links_by_subject = {
            (item.comparison_id, item.scenario_id, item.pair_id, item.direct_run_id): item
            for item in result.evidence_claim_links
        }
        scenario_rows: list[list[Any]] = []
        pair_rows: list[list[Any]] = []
        for comparison in sorted(
            result.scenario_comparisons,
            key=lambda item: (item.scenario_id, item.comparison_id),
        ):
            scenario_rows.append(
                [
                    result.result_id,
                    comparison.comparison_id,
                    comparison.binding_id,
                    comparison.scenario_id,
                    comparison.direct_run_id,
                    comparison.status,
                    comparison.qa_status,
                    comparison.reference_matrix_asset_id,
                    comparison.reference_matrix_sha256,
                    comparison.observed_matrix_sha256,
                    comparison.tolerance.absolute_tolerance,
                    comparison.tolerance.relative_tolerance,
                    comparison.reference_current_asset_id,
                    comparison.reference_current_sha256,
                    comparison.current_raster_disposition,
                    comparison.current_raster_numeric_comparison_performed,
                    len(comparison.pair_comparisons),
                ]
            )
            for pair in sorted(comparison.pair_comparisons, key=lambda item: item.pair_id):
                subject = (
                    comparison.comparison_id,
                    comparison.scenario_id,
                    pair.pair_id,
                    comparison.direct_run_id,
                )
                link = links_by_subject.get(subject)
                if link is None:
                    raise CloudIntegrityError("ECO_REFERENCE_COMPARISON_CLAIM_LINK_MISSING")
                pair_rows.append(
                    [
                        result.result_id,
                        comparison.comparison_id,
                        comparison.scenario_id,
                        pair.pair_id,
                        comparison.direct_run_id,
                        pair.reference_effective_resistance,
                        pair.observed_effective_resistance,
                        pair.absolute_difference,
                        pair.relative_difference,
                        pair.allowed_difference,
                        pair.tolerance.absolute_tolerance,
                        pair.tolerance.relative_tolerance,
                        pair.within_tolerance,
                        link.claim_id,
                    ]
                )

        claim_rows = [
            [
                claim.claim_id,
                claim.portfolio_id,
                claim.scenario_id,
                claim.pair_id,
                claim.value,
                claim.units,
                claim.direct_run_ids[0],
                list(claim.limitations),
            ]
            for claim in sorted(claims, key=lambda item: item.claim_id)
        ]
        snapshot_value = {
            "schema_version": "ecocorridor-reference-evidence/v1.0",
            "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
            "plan_payload_sha256": plan_sha256,
            "plan": reference_plan.model_dump(mode="json"),
            "result": result_payload,
            "claims": [
                item.model_dump(mode="json")
                for item in sorted(claims, key=lambda claim: claim.claim_id)
            ],
        }
        snapshot = self._stage_json(
            category="reference-comparison",
            logical_id=f"{result.result_id}:{result_sha256}",
            value=snapshot_value,
            artifact_type="decision_evidence",
            logical_name="reference/reference-comparison.json",
        )

        input_references = self._receipt_refs(
            event_type="analysis_plan",
            analysis_id=analysis_id,
            required=True,
            require_each=True,
        )
        for direct_run_id in direct_run_ids:
            input_references.extend(
                self._receipt_refs(
                    event_type="direct_job_terminal",
                    analysis_id=analysis_id,
                    subject_id=direct_run_id,
                    required=True,
                    require_each=True,
                )
            )

        within_tolerance_count = sum(
            pair.within_tolerance
            for comparison in result.scenario_comparisons
            for pair in comparison.pair_comparisons
        )
        return self._base(
            row,
            run_kind="connectivity_analysis",
            run_subject_id=f"{analysis_id}:reference-comparison",
            event_sequence=event_sequence,
            config={
                "analysis_id": analysis_id,
                "reference_plan_id": result.plan_id,
                "reference_plan_payload_sha256": plan_sha256,
                "reference_result_id": result.result_id,
                "revision_id": result.revision_id,
                "model_sha256": result.model_sha256,
                "engine_manifest_sha256": result.engine_manifest_sha256,
                "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
                "exact_direct_evidence_only": result.exact_direct_evidence_only,
            },
            history={
                "reference_scenarios_compared": len(scenario_rows),
                "reference_pairs_compared": len(pair_rows),
                "reference_pairs_within_tolerance": within_tolerance_count,
            },
            summary={
                "reference_result_id": result.result_id,
                "reference_result_sha256": result_sha256,
                "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
                "reference_status": result.status,
                "reference_scenario_count": len(scenario_rows),
                "reference_pair_count": len(pair_rows),
                "reference_claim_count": len(claim_rows),
                "reference_scenarios_passed": sum(
                    comparison.status == "PASSED" for comparison in result.scenario_comparisons
                ),
                "reference_scenarios_failed": sum(
                    comparison.status == "FAILED" for comparison in result.scenario_comparisons
                ),
                "current_raster_numeric_comparison_performed": (
                    result.current_raster_numeric_comparison_performed
                ),
                "model_fitness_attested_by_reference_match": False,
            },
            tables=[
                TableProjection(
                    logical_name="reference_plan",
                    columns=[
                        "plan_id",
                        "analysis_id",
                        "revision_id",
                        "model_sha256",
                        "engine_manifest_sha256",
                        "reference_matrix_parser_sha256",
                        "payload_sha256",
                        "baseline_portfolio_id",
                        "bound_scenario_count",
                        "binding_count",
                        "direct_job_count",
                        "exact_direct_evidence_only",
                    ],
                    rows=[
                        [
                            reference_plan.plan_id,
                            reference_plan.analysis_id,
                            reference_plan.revision_id,
                            reference_plan.model_sha256,
                            reference_plan.engine_manifest_sha256,
                            REFERENCE_MATRIX_PARSER_SHA256,
                            plan_sha256,
                            reference_plan.run_plan.baseline_portfolio_id,
                            len(reference_plan.bound_scenario_ids),
                            len(reference_plan.binding_ids),
                            reference_plan.run_plan.direct_job_count,
                            reference_plan.exact_direct_evidence_only,
                        ]
                    ],
                ),
                TableProjection(
                    logical_name="reference_comparisons",
                    columns=[
                        "result_id",
                        "comparison_id",
                        "binding_id",
                        "scenario_id",
                        "direct_run_id",
                        "status",
                        "qa_status",
                        "reference_matrix_asset_id",
                        "reference_matrix_sha256",
                        "observed_matrix_sha256",
                        "absolute_tolerance",
                        "relative_tolerance",
                        "reference_current_asset_id",
                        "reference_current_sha256",
                        "current_raster_disposition",
                        "current_raster_numeric_comparison_performed",
                        "pair_count",
                    ],
                    rows=scenario_rows,
                ),
                TableProjection(
                    logical_name="reference_pair_comparisons",
                    columns=[
                        "result_id",
                        "comparison_id",
                        "scenario_id",
                        "pair_id",
                        "direct_run_id",
                        "reference_effective_resistance",
                        "observed_effective_resistance",
                        "absolute_difference",
                        "relative_difference",
                        "allowed_difference",
                        "absolute_tolerance",
                        "relative_tolerance",
                        "within_tolerance",
                        "evidence_claim_id",
                    ],
                    rows=pair_rows,
                ),
                TableProjection(
                    logical_name="reference_evidence_claims",
                    columns=[
                        "claim_id",
                        "portfolio_id",
                        "scenario_id",
                        "pair_id",
                        "value",
                        "units",
                        "direct_run_id",
                        "limitations",
                    ],
                    rows=claim_rows,
                ),
            ],
            input_artifact_refs=sorted(set(input_references)),
            artifacts=[snapshot],
        )

    def _evidence_export(self, row: dict[str, Any], payload: dict[str, Any]) -> TelemetryProjection:
        manifest = payload.get("manifest")
        package_files = payload.get("package_files")
        if not isinstance(manifest, dict) or not isinstance(package_files, list):
            raise CloudIntegrityError("ECO_EXPORT_OUTBOX_INVALID")
        export_id = str(manifest.get("export_id", ""))
        analysis_id = str(row.get("analysis_id", ""))
        self._require_current_analysis_asset_authorization(analysis_id)
        if export_id != row.get("subject_id") or manifest.get("project_id") != row.get(
            "project_id"
        ):
            raise CloudIntegrityError("ECO_EXPORT_OUTBOX_MISMATCH")
        artifacts = []
        file_rows = []
        expected_root = PurePosixPath("exports") / export_id
        for item in package_files:
            if not isinstance(item, dict):
                raise CloudIntegrityError("ECO_EXPORT_FILE_MANIFEST_INVALID")
            relative = str(item.get("relative_path", ""))
            path = PurePosixPath(relative)
            if (
                path.is_absolute()
                or expected_root not in path.parents
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise CloudIntegrityError("ECO_EXPORT_FILE_PATH_INVALID")
            package_relative = path.relative_to(expected_root)
            logical_suffix = "/".join(
                _safe_component(part, fallback="file") for part in package_relative.parts
            )
            artifacts.append(
                self._verified_artifact(
                    local_artifact_id=(
                        f"export:{export_id}:{package_relative.as_posix()}:{item.get('sha256')}"
                    ),
                    artifact_type="evidence_package",
                    logical_id=f"{export_id}:{package_relative.as_posix()}",
                    logical_name=f"evidence-package/{logical_suffix}",
                    source_relative_path=relative,
                    expected_sha256=str(item.get("sha256", "")),
                    expected_bytes=int(item.get("byte_size", -1)),
                    media_type=str(item.get("media_type", "")),
                )
            )
            file_rows.append(
                [
                    package_relative.as_posix(),
                    item.get("sha256"),
                    int(item.get("byte_size", -1)),
                    item.get("media_type"),
                ]
            )
        return self._base(
            row,
            run_kind="evidence_export",
            run_subject_id=export_id,
            event_sequence=1,
            config={"analysis_id": analysis_id, "revision_id": manifest.get("revision_id")},
            history={"package_file_count": len(artifacts)},
            summary={
                "export_id": export_id,
                "package_file_count": len(artifacts),
                "direct_run_count": len(manifest.get("direct_run_ids", [])),
                "evidence_claim_count": len(manifest.get("evidence_claim_ids", [])),
                "sync_state_at_creation": manifest.get("sync_state"),
            },
            tables=[
                TableProjection(
                    logical_name="export_files",
                    columns=["relative_path", "sha256", "bytes", "media_type"],
                    rows=file_rows,
                )
            ],
            input_artifact_refs=self._receipt_refs(
                event_type="analysis_completion",
                analysis_id=analysis_id,
                required=True,
                require_each=True,
            ),
            artifacts=artifacts,
        )

    def _receipt_refs(
        self,
        *,
        event_type: str,
        analysis_id: str | None = None,
        subject_id: str | None = None,
        required: bool,
        latest_only: bool = False,
        first_only: bool = False,
        require_each: bool = False,
    ) -> list[str]:
        if first_only and latest_only:
            raise ValueError("artifact lineage cannot select both first and latest events")
        references: list[str] = []
        matching = self.store.telemetry_rows(
            event_type=event_type, analysis_id=analysis_id, subject_id=subject_id
        )
        rows = matching[:1] if first_only else (matching[-1:] if latest_only else matching)
        for row in rows:
            row_references: list[str] = []
            if row["state"] != "SYNCED" or row["external_reference"] is None:
                if require_each:
                    raise CloudTransportError("ECO_REQUIRED_ARTIFACT_LINEAGE_NOT_SYNCED")
                continue
            for artifact in row["external_reference"].get("artifacts", []):
                reference = artifact.get("provider_artifact_ref")
                if reference:
                    row_references.append(str(reference))
            if require_each and not row_references:
                raise CloudTransportError("ECO_REQUIRED_ARTIFACT_LINEAGE_NOT_SYNCED")
            references.extend(row_references)
        if required and not references:
            raise CloudTransportError("ECO_REQUIRED_ARTIFACT_LINEAGE_NOT_SYNCED")
        return sorted(set(references))

    def _bundle_receipt_refs(self, revision_id: str, *, required: bool = True) -> list[str]:
        matching = [
            row
            for row in self.store.telemetry_rows(event_type="bundle_import")
            if row["payload"].get("revision_id") == revision_id
        ]
        if not matching and not required:
            return []
        if len(matching) != 1:
            raise CloudIntegrityError("ECO_ANALYSIS_BUNDLE_REVISION_AMBIGUOUS")
        row = matching[0]
        if row["state"] != "SYNCED" or row["external_reference"] is None:
            raise CloudTransportError("ECO_REQUIRED_ARTIFACT_LINEAGE_NOT_SYNCED")
        references = [
            str(item["provider_artifact_ref"])
            for item in row["external_reference"].get("artifacts", [])
            if item.get("provider_artifact_ref")
        ]
        if not references:
            raise CloudTransportError("ECO_REQUIRED_ARTIFACT_LINEAGE_NOT_SYNCED")
        return sorted(set(references))


__all__ = ["EcoTelemetryProjector"]
