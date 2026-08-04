"""Fail-closed ecological asset authorization and resource planning."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum

from scenario_platform.observability import ProjectScope, ProjectScopeEvidence

from .contracts import (
    APPROVAL_AUDIT_RESERVATION_BYTES_PER_PROJECTION,
    OPERATIONAL_METADATA_RESERVATION_BYTES_PER_PROJECTION,
    CloudDisposition,
    ConnectivityAsset,
    ConnectivityProjectBundle,
    OversizeSyncApproval,
    SensitivityClass,
    canonical_sha256,
)


class SnapshotAssetAuthorizationError(ValueError):
    """An immutable analysis asset cannot be reconciled to current authority."""

    def __init__(self, code: str, asset_id: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.asset_id = asset_id


class SyncDestination(str, Enum):
    WANDB_CLOUD = "WANDB_CLOUD"
    DEDICATED = "DEDICATED"
    SELF_MANAGED = "SELF_MANAGED"
    LOCAL_ONLY = "LOCAL_ONLY"


class CurrentRasterEstimateStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PRE_RUN_ESTIMATE = "PRE_RUN_ESTIMATE"
    ACTUAL_RECONCILED = "ACTUAL_RECONCILED"
    PARTIALLY_RECONCILED = "PARTIALLY_RECONCILED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class CurrentRasterEstimate:
    """Worker-derived current-raster storage projection.

    A missing estimate is represented by ``estimated_bytes=None`` rather than
    zero. ``actual_retained_bytes`` is always a subset of a known estimate and
    is read from immutable retained-artifact metadata, never renderer input.
    """

    status: CurrentRasterEstimateStatus
    estimated_bytes: int | None
    actual_retained_bytes: int
    pending_estimated_bytes: int | None
    expected_raster_count: int | None
    retained_raster_count: int
    basis: str

    def __post_init__(self) -> None:
        numeric = (
            self.actual_retained_bytes,
            self.retained_raster_count,
        )
        if any(value < 0 for value in numeric):
            raise ValueError("current-raster estimate values cannot be negative")
        if self.estimated_bytes is not None and self.estimated_bytes < 0:
            raise ValueError("estimated current-raster bytes cannot be negative")
        if self.pending_estimated_bytes is not None and self.pending_estimated_bytes < 0:
            raise ValueError("pending current-raster bytes cannot be negative")
        if self.expected_raster_count is not None and self.expected_raster_count < 0:
            raise ValueError("expected current-raster count cannot be negative")
        if not self.basis.strip():
            raise ValueError("current-raster estimate basis is required")


@dataclass(frozen=True, slots=True)
class AssetProjection:
    source_asset_id: str
    uploaded_asset_id: str
    representation: str
    bytes: int


@dataclass(frozen=True, slots=True)
class TelemetryEnvelopeAuthorization:
    """Exact committed outbox member authorized by one inventory snapshot."""

    outbox_id: str
    project_id: str
    event_type: str
    subject_id: str
    stream_id: str
    stream_sequence: int
    payload_sha256: str
    stable_projection_sha256: str
    stable_projection_bytes: int
    artifacts_sha256: str
    artifact_count: int

    def __post_init__(self) -> None:
        identities = (
            self.outbox_id,
            self.project_id,
            self.event_type,
            self.subject_id,
            self.stream_id,
        )
        if any(not value for value in identities) or self.stream_sequence < 1:
            raise ValueError("telemetry authorization requires exact outbox identity")
        for label, digest in (
            ("payload", self.payload_sha256),
            ("stable projection", self.stable_projection_sha256),
            ("artifact set", self.artifacts_sha256),
        ):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"telemetry authorization {label} requires a SHA-256 digest")
        if self.stable_projection_bytes < 0 or self.artifact_count < 0:
            raise ValueError("telemetry authorization counts cannot be negative")

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TelemetryProjectionInventory:
    """Integrity-checked cloud evidence projected from committed outbox rows.

    ``projected_bytes`` excludes exact input assets already counted by
    ``CloudSyncPlan.input_bytes``.  It includes canonical telemetry envelopes
    and every other unique artifact by content hash and byte size.
    """

    sha256: str
    projected_bytes: int
    artifact_count: int
    approval_audit_reservation_bytes: int = 0
    approval_audit_reservation_count: int = 0
    operational_metadata_reservation_bytes: int = 0
    operational_metadata_reservation_count: int = 0
    authority_sha256: str = canonical_sha256([])
    authorized_envelopes: tuple[TelemetryEnvelopeAuthorization, ...] = ()

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise ValueError("telemetry inventory requires a lowercase SHA-256 digest")
        numeric = (
            self.projected_bytes,
            self.artifact_count,
            self.approval_audit_reservation_bytes,
            self.approval_audit_reservation_count,
            self.operational_metadata_reservation_bytes,
            self.operational_metadata_reservation_count,
        )
        if any(value < 0 for value in numeric):
            raise ValueError("telemetry inventory values cannot be negative")
        if (self.approval_audit_reservation_bytes == 0) != (
            self.approval_audit_reservation_count == 0
        ):
            raise ValueError("approval-audit reservation bytes and count must agree")
        if (self.operational_metadata_reservation_bytes == 0) != (
            self.operational_metadata_reservation_count == 0
        ):
            raise ValueError("operational-metadata reservation bytes and count must agree")
        if self.approval_audit_reservation_bytes != (
            self.approval_audit_reservation_count * APPROVAL_AUDIT_RESERVATION_BYTES_PER_PROJECTION
        ):
            raise ValueError("approval-audit reservation does not match its versioned bound")
        if self.operational_metadata_reservation_bytes != (
            self.operational_metadata_reservation_count
            * OPERATIONAL_METADATA_RESERVATION_BYTES_PER_PROJECTION
        ):
            raise ValueError("operational-metadata reservation does not match its versioned bound")
        if (
            self.approval_audit_reservation_bytes + self.operational_metadata_reservation_bytes
            > self.projected_bytes
        ):
            raise ValueError("telemetry reservations exceed projected telemetry bytes")
        authorization_payload = [item.as_dict() for item in self.authorized_envelopes]
        if self.authority_sha256 != canonical_sha256(authorization_payload):
            raise ValueError("telemetry authorization digest does not match its exact members")
        outbox_ids = [item.outbox_id for item in self.authorized_envelopes]
        if len(set(outbox_ids)) != len(outbox_ids):
            raise ValueError("telemetry authorization contains duplicate outbox identities")


EMPTY_TELEMETRY_INVENTORY = TelemetryProjectionInventory(
    sha256=canonical_sha256([]),
    projected_bytes=0,
    artifact_count=0,
)


def oversize_sync_approval_event_payload(approval: OversizeSyncApproval) -> dict[str, str]:
    """Return the stable, non-secret approval event projected into W&B lineage."""

    return {
        "approval_id": approval.approval_id,
        "project_id": approval.project_id,
        "binding_id_sha256": hashlib.sha256(approval.binding_id.encode("utf-8")).hexdigest(),
        "destination": approval.destination,
        "entity": approval.entity,
        "project": approval.project,
        "external_project_id": approval.external_project_id,
        "model_revision_id": approval.model_revision_id,
        "model_sha256": approval.model_sha256,
        "reviewed_sync_plan_sha256": approval.sync_plan_sha256,
        "reviewed_by": approval.reviewed_by,
        "approved_at": approval.approved_at.isoformat(),
        "acknowledgement_sha256": hashlib.sha256(
            approval.acknowledgement.encode("utf-8")
        ).hexdigest(),
    }


@dataclass(frozen=True, slots=True)
class CloudSyncApprovalSubject:
    project_id: str
    binding_id: str
    destination: SyncDestination
    entity: str
    project: str
    external_project_id: str
    model_revision_id: str
    model_sha256: str
    bundle_sha256: str
    resource_policy_sha256: str
    projection_sha256: str
    current_raster_estimate_sha256: str
    scope_evidence_sha256: str
    telemetry_inventory_sha256: str
    input_bytes: int
    estimated_current_raster_bytes: int | None
    pending_estimated_current_raster_bytes: int | None
    telemetry_projected_bytes: int
    telemetry_artifact_count: int
    approval_audit_reservation_bytes: int
    approval_audit_reservation_count: int
    operational_metadata_reservation_bytes: int
    operational_metadata_reservation_count: int
    estimated_total_bytes: int | None

    @property
    def sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_version": "ecocorridor-cloud-sync-approval-subject/v1.1",
                "project_id": self.project_id,
                "binding_id": self.binding_id,
                "destination": self.destination.value,
                "entity": self.entity,
                "project": self.project,
                "external_project_id": self.external_project_id,
                "model_revision_id": self.model_revision_id,
                "model_sha256": self.model_sha256,
                "bundle_sha256": self.bundle_sha256,
                "resource_policy_sha256": self.resource_policy_sha256,
                "projection_sha256": self.projection_sha256,
                "current_raster_estimate_sha256": self.current_raster_estimate_sha256,
                "scope_evidence_sha256": self.scope_evidence_sha256,
                "telemetry_inventory_sha256": self.telemetry_inventory_sha256,
                "input_bytes": self.input_bytes,
                "estimated_current_raster_bytes": self.estimated_current_raster_bytes,
                "pending_estimated_current_raster_bytes": (
                    self.pending_estimated_current_raster_bytes
                ),
                "telemetry_projected_bytes": self.telemetry_projected_bytes,
                "telemetry_artifact_count": self.telemetry_artifact_count,
                "approval_audit_reservation_bytes": (self.approval_audit_reservation_bytes),
                "approval_audit_reservation_count": (self.approval_audit_reservation_count),
                "operational_metadata_reservation_bytes": (
                    self.operational_metadata_reservation_bytes
                ),
                "operational_metadata_reservation_count": (
                    self.operational_metadata_reservation_count
                ),
                "estimated_total_bytes": self.estimated_total_bytes,
            }
        )


@dataclass(frozen=True, slots=True)
class CloudSyncPlan:
    destination: SyncDestination
    projections: tuple[AssetProjection, ...]
    input_bytes: int
    estimated_current_raster_bytes: int | None
    telemetry_inventory_sha256: str
    telemetry_projected_bytes: int
    telemetry_artifact_count: int
    approval_audit_reservation_bytes: int
    approval_audit_reservation_count: int
    operational_metadata_reservation_bytes: int
    operational_metadata_reservation_count: int
    telemetry_authority_sha256: str
    authorized_envelopes: tuple[TelemetryEnvelopeAuthorization, ...]
    estimated_total_bytes: int | None
    current_raster_estimate_status: CurrentRasterEstimateStatus
    actual_retained_current_raster_bytes: int
    pending_estimated_current_raster_bytes: int | None
    expected_current_raster_count: int | None
    retained_current_raster_count: int
    current_raster_estimate_basis: str
    approval_subject: CloudSyncApprovalSubject
    approval_status: str
    approval_id: str | None
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return not self.blockers


def create_oversize_sync_approval(
    plan: CloudSyncPlan,
    *,
    reviewed_by: str,
    approved_at: datetime,
) -> OversizeSyncApproval:
    """Create consent only for the exact oversized plan shown to the reviewer."""

    subject = plan.approval_subject
    if plan.approval_status not in {"REQUIRED", "STALE"}:
        raise ValueError("the sync plan does not require a new oversize approval")
    if (
        subject.destination == SyncDestination.LOCAL_ONLY
        or subject.estimated_current_raster_bytes is None
        or subject.estimated_total_bytes is None
    ):
        raise ValueError("oversize approval requires a complete cloud sync estimate")
    inventory = TelemetryProjectionInventory(
        sha256=subject.telemetry_inventory_sha256,
        projected_bytes=subject.telemetry_projected_bytes,
        artifact_count=subject.telemetry_artifact_count,
        approval_audit_reservation_bytes=subject.approval_audit_reservation_bytes,
        approval_audit_reservation_count=subject.approval_audit_reservation_count,
        operational_metadata_reservation_bytes=subject.operational_metadata_reservation_bytes,
        operational_metadata_reservation_count=subject.operational_metadata_reservation_count,
    )
    if subject.estimated_total_bytes is None:
        raise ValueError("oversize approval requires a complete synchronized-evidence estimate")
    pending_bytes = subject.pending_estimated_current_raster_bytes
    if pending_bytes is None:
        raise ValueError("oversize approval requires a complete pending-raster estimate")
    return OversizeSyncApproval(
        approval_id=f"oversize_approval_{subject.sha256}",
        project_id=subject.project_id,
        binding_id=subject.binding_id,
        destination=subject.destination.value,
        entity=subject.entity,
        project=subject.project,
        external_project_id=subject.external_project_id,
        model_revision_id=subject.model_revision_id,
        model_sha256=subject.model_sha256,
        bundle_sha256=subject.bundle_sha256,
        resource_policy_sha256=subject.resource_policy_sha256,
        projection_sha256=subject.projection_sha256,
        current_raster_estimate_sha256=subject.current_raster_estimate_sha256,
        scope_evidence_sha256=subject.scope_evidence_sha256,
        telemetry_inventory_sha256=inventory.sha256,
        sync_plan_sha256=subject.sha256,
        approved_input_bytes=subject.input_bytes,
        approved_current_raster_bytes=subject.estimated_current_raster_bytes,
        approved_pending_current_raster_bytes=pending_bytes,
        approved_telemetry_projected_bytes=inventory.projected_bytes,
        approved_telemetry_artifact_count=inventory.artifact_count,
        approved_approval_audit_reservation_bytes=(inventory.approval_audit_reservation_bytes),
        approved_approval_audit_reservation_count=(inventory.approval_audit_reservation_count),
        approved_operational_metadata_reservation_bytes=(
            inventory.operational_metadata_reservation_bytes
        ),
        approved_operational_metadata_reservation_count=(
            inventory.operational_metadata_reservation_count
        ),
        approved_total_bytes=subject.estimated_total_bytes,
        reviewed_by=reviewed_by,
        approved_at=approved_at,
    )


def scientific_asset_ids(bundle: ConnectivityProjectBundle) -> set[str]:
    """Return assets whose exact bytes are part of reportable scientific evidence."""

    return {
        *(item.resistance_asset_id for item in bundle.resistance_hypotheses),
        *(item.asset_id for item in bundle.focal_area_sets),
        *(item.asset_id for item in bundle.review_masks),
        *(
            effect.footprint_asset_id
            for option in bundle.intervention_options
            for effect in option.effects
        ),
        *(binding.reference_matrix_asset_id for binding in bundle.reference_bindings),
        *(
            binding.reference_current_asset_id
            for binding in bundle.reference_bindings
            if binding.reference_current_asset_id is not None
        ),
    }


def reconcile_snapshot_assets(
    snapshot: ConnectivityProjectBundle,
    current: ConnectivityProjectBundle,
    *,
    asset_ids: set[str] | None = None,
) -> dict[str, ConnectivityAsset]:
    """Bind immutable analysis bytes to the project's current authorization records.

    Authorization is mutable by design, but it is valid for historical evidence only
    while the current record still names the exact immutable asset identity used by
    the analysis. A same-ID replacement must never confer its authorization on old
    bytes. Callers receive the current records only after that identity check passes.
    """

    if snapshot.project_id != current.project_id:
        raise SnapshotAssetAuthorizationError(
            "ECO_ANALYSIS_ASSET_PROJECT_MISMATCH",
            snapshot.project_id,
            "The analysis snapshot and current authorization state belong to different projects.",
        )
    snapshot_assets = {asset.asset_id: asset for asset in snapshot.assets}
    current_assets = {asset.asset_id: asset for asset in current.assets}
    selected_ids = set(snapshot_assets) if asset_ids is None else set(asset_ids)
    unknown = selected_ids - set(snapshot_assets)
    if unknown:
        asset_id = min(unknown)
        raise SnapshotAssetAuthorizationError(
            "ECO_ANALYSIS_ASSET_SNAPSHOT_MISSING",
            asset_id,
            f"Asset {asset_id} is absent from the immutable analysis snapshot.",
        )

    reconciled: dict[str, ConnectivityAsset] = {}
    for asset_id in sorted(selected_ids):
        snapshot_asset = snapshot_assets[asset_id]
        current_asset = current_assets.get(asset_id)
        if current_asset is None:
            raise SnapshotAssetAuthorizationError(
                "ECO_ANALYSIS_ASSET_AUTHORIZATION_REVOKED",
                asset_id,
                f"Asset {asset_id} no longer has a current project authorization record.",
            )
        if (
            current_asset.kind != snapshot_asset.kind
            or current_asset.sha256 != snapshot_asset.sha256
            or current_asset.byte_size != snapshot_asset.byte_size
            or current_asset.media_type != snapshot_asset.media_type
        ):
            raise SnapshotAssetAuthorizationError(
                "ECO_ANALYSIS_ASSET_IDENTITY_STALE",
                asset_id,
                (
                    f"Asset {asset_id} differs from the immutable kind, hash, size, or media "
                    "type used by the analysis. Create a new analysis revision."
                ),
            )
        reconciled[asset_id] = current_asset
    return reconciled


def plan_cloud_sync(
    bundle: ConnectivityProjectBundle,
    *,
    destination: SyncDestination,
    scope_evidence: ProjectScopeEvidence | None,
    estimated_current_raster_bytes: int | None = None,
    current_raster_estimate: CurrentRasterEstimate | None = None,
    binding_id: str = "UNBOUND",
    approval: OversizeSyncApproval | None = None,
    telemetry_inventory: TelemetryProjectionInventory = EMPTY_TELEMETRY_INVENTORY,
) -> CloudSyncPlan:
    if current_raster_estimate is not None and estimated_current_raster_bytes is not None:
        raise ValueError("provide one authoritative current-raster estimate")
    estimate = current_raster_estimate
    if estimate is None:
        estimate = CurrentRasterEstimate(
            status=(
                CurrentRasterEstimateStatus.UNAVAILABLE
                if estimated_current_raster_bytes is None
                else CurrentRasterEstimateStatus.PRE_RUN_ESTIMATE
            ),
            estimated_bytes=estimated_current_raster_bytes,
            actual_retained_bytes=0,
            pending_estimated_bytes=estimated_current_raster_bytes,
            expected_raster_count=None,
            retained_raster_count=0,
            basis=(
                "No authoritative run plan is available."
                if estimated_current_raster_bytes is None
                else "Caller-supplied estimate retained for domain-service compatibility."
            ),
        )
    assets = {asset.asset_id: asset for asset in bundle.assets}
    scientific_assets = scientific_asset_ids(bundle)
    blockers: list[str] = []
    warnings: list[str] = []
    projections: list[AssetProjection] = []
    cloud_destination = destination != SyncDestination.LOCAL_ONLY
    if cloud_destination:
        if scope_evidence is None or scope_evidence.scope != ProjectScope.RESTRICTED:
            blockers.append("W&B destination has no authoritative exact Restricted-scope evidence")
        else:
            normalized_scope_deployment = scope_evidence.deployment.casefold().replace("-", "_")
            accepted_deployments = {
                SyncDestination.WANDB_CLOUD: {"wandb_cloud", "cloud"},
                SyncDestination.DEDICATED: {"dedicated", "dedicated_cloud"},
                SyncDestination.SELF_MANAGED: {"self_managed"},
            }[destination]
            if normalized_scope_deployment not in accepted_deployments:
                blockers.append("W&B scope evidence belongs to another deployment")

    for asset in bundle.assets:
        authorization = asset.authorization
        if (
            destination == SyncDestination.WANDB_CLOUD
            and authorization.sensitivity == SensitivityClass.CRITICAL_INFRASTRUCTURE
        ):
            blockers.append(
                f"asset {asset.asset_id} is critical-infrastructure restricted and requires "
                "Dedicated Cloud or Self-Managed W&B"
            )
            continue
        if destination.value not in authorization.approved_deployments:
            blockers.append(f"asset {asset.asset_id} is not authorized for {destination.value}")
            continue
        disposition = authorization.cloud_disposition
        if not cloud_destination:
            projections.append(_raw_projection(asset))
        elif disposition == CloudDisposition.PROHIBITED:
            blockers.append(f"asset {asset.asset_id} is prohibited from cloud replication")
        elif disposition == CloudDisposition.DEDICATED_OR_SELF_MANAGED_ONLY:
            if destination not in {SyncDestination.DEDICATED, SyncDestination.SELF_MANAGED}:
                blockers.append(
                    f"asset {asset.asset_id} requires Dedicated Cloud or Self-Managed W&B"
                )
            else:
                projections.append(_raw_projection(asset))
        elif disposition == CloudDisposition.GENERALIZED_DERIVATIVE_ONLY:
            derivative_id = authorization.generalized_asset_id
            derivative = assets.get(derivative_id or "")
            if derivative is None:
                blockers.append(f"asset {asset.asset_id} has no available generalized derivative")
            elif derivative.authorization.cloud_disposition != CloudDisposition.RAW_ALLOWED:
                blockers.append(
                    f"generalized derivative {derivative.asset_id} is not authorized for raw upload"
                )
            elif destination.value not in derivative.authorization.approved_deployments:
                blockers.append(
                    f"generalized derivative {derivative.asset_id} is not authorized for {destination.value}"
                )
            elif asset.asset_id in scientific_assets:
                blockers.append(
                    f"scientific model still references generalized-only asset {asset.asset_id}; "
                    f"create and approve a semantic revision that references {derivative.asset_id}"
                )
            else:
                projections.append(
                    AssetProjection(
                        source_asset_id=asset.asset_id,
                        uploaded_asset_id=derivative.asset_id,
                        representation="GENERALIZED_DERIVATIVE",
                        bytes=derivative.byte_size,
                    )
                )
        elif disposition == CloudDisposition.RAW_ALLOWED:
            projections.append(_raw_projection(asset))

    # One generalized derivative may represent its own source record and one or
    # more restricted originals. It is stored once in the artifact graph.
    unique_uploaded: dict[str, AssetProjection] = {}
    for projection in projections:
        unique_uploaded.setdefault(projection.uploaded_asset_id, projection)
    # W&B stores immutable evidence by content. Count identical uploaded bytes
    # once even when multiple reviewed asset records intentionally reference
    # the same derivative or exact file content.
    unique_input_content = {
        (assets[item.uploaded_asset_id].sha256, item.bytes) for item in unique_uploaded.values()
    }
    input_bytes = sum(byte_size for _, byte_size in unique_input_content)
    policy = bundle.resource_policy
    if input_bytes > policy.max_input_bytes:
        blockers.append(
            f"authorized input projection is {input_bytes} bytes; policy permits {policy.max_input_bytes}"
        )
    if estimate.estimated_bytes is None:
        blockers.append(
            "CURRENT_RASTER_ESTIMATE_UNAVAILABLE: create an authoritative run plan before sync"
        )
        estimated_total: int | None = None
    else:
        pending_bytes = estimate.pending_estimated_bytes
        if pending_bytes is None:
            estimated_total = None
        else:
            estimated_total = input_bytes + telemetry_inventory.projected_bytes + pending_bytes
    projection_sha256 = canonical_sha256(
        [
            {
                "source_asset_id": item.source_asset_id,
                "uploaded_asset_id": item.uploaded_asset_id,
                "representation": item.representation,
                "bytes": item.bytes,
            }
            for item in sorted(
                projections,
                key=lambda value: (
                    value.source_asset_id,
                    value.uploaded_asset_id,
                    value.representation,
                ),
            )
        ]
    )
    estimate_sha256 = canonical_sha256(
        {
            "status": estimate.status.value,
            "estimated_bytes": estimate.estimated_bytes,
            "actual_retained_bytes": estimate.actual_retained_bytes,
            "pending_estimated_bytes": estimate.pending_estimated_bytes,
            "expected_raster_count": estimate.expected_raster_count,
            "retained_raster_count": estimate.retained_raster_count,
            "basis": estimate.basis,
        }
    )
    approval_subject = CloudSyncApprovalSubject(
        project_id=bundle.project_id,
        binding_id=binding_id,
        destination=destination,
        entity=(scope_evidence.entity if scope_evidence else ""),
        project=(scope_evidence.project if scope_evidence else ""),
        external_project_id=(scope_evidence.external_project_id if scope_evidence else ""),
        model_revision_id=bundle.revision.revision_id,
        model_sha256=bundle.revision.model_sha256,
        bundle_sha256=canonical_sha256(bundle.model_dump(mode="json")),
        resource_policy_sha256=canonical_sha256(policy.model_dump(mode="json")),
        projection_sha256=projection_sha256,
        current_raster_estimate_sha256=estimate_sha256,
        scope_evidence_sha256=canonical_sha256(
            scope_evidence.model_dump(mode="json") if scope_evidence else None
        ),
        telemetry_inventory_sha256=telemetry_inventory.sha256,
        input_bytes=input_bytes,
        estimated_current_raster_bytes=estimate.estimated_bytes,
        pending_estimated_current_raster_bytes=estimate.pending_estimated_bytes,
        telemetry_projected_bytes=telemetry_inventory.projected_bytes,
        telemetry_artifact_count=telemetry_inventory.artifact_count,
        approval_audit_reservation_bytes=(telemetry_inventory.approval_audit_reservation_bytes),
        approval_audit_reservation_count=(telemetry_inventory.approval_audit_reservation_count),
        operational_metadata_reservation_bytes=(
            telemetry_inventory.operational_metadata_reservation_bytes
        ),
        operational_metadata_reservation_count=(
            telemetry_inventory.operational_metadata_reservation_count
        ),
        estimated_total_bytes=estimated_total,
    )
    exceeds_current = (
        estimate.estimated_bytes is not None
        and estimate.estimated_bytes > policy.max_current_raster_bytes
    )
    exceeds_materialized = (
        estimated_total is not None and estimated_total > policy.max_materialized_bytes
    )
    oversize = exceeds_current or exceeds_materialized
    approval_matches = bool(
        approval is not None
        and approval.project_id == approval_subject.project_id
        and approval.binding_id == approval_subject.binding_id
        and approval.destination == approval_subject.destination.value
        and approval.entity == approval_subject.entity
        and approval.project == approval_subject.project
        and approval.external_project_id == approval_subject.external_project_id
        and approval.model_revision_id == approval_subject.model_revision_id
        and approval.model_sha256 == approval_subject.model_sha256
        and approval.bundle_sha256 == approval_subject.bundle_sha256
        and approval.resource_policy_sha256 == approval_subject.resource_policy_sha256
        and approval.projection_sha256 == approval_subject.projection_sha256
        and approval.current_raster_estimate_sha256
        == approval_subject.current_raster_estimate_sha256
        and approval.scope_evidence_sha256 == approval_subject.scope_evidence_sha256
        and approval.telemetry_inventory_sha256 == approval_subject.telemetry_inventory_sha256
        and approval.sync_plan_sha256 == approval_subject.sha256
        and approval.approved_input_bytes == approval_subject.input_bytes
        and approval.approved_current_raster_bytes
        == approval_subject.estimated_current_raster_bytes
        and approval.approved_pending_current_raster_bytes
        == approval_subject.pending_estimated_current_raster_bytes
        and approval.approved_telemetry_projected_bytes
        == approval_subject.telemetry_projected_bytes
        and approval.approved_telemetry_artifact_count == approval_subject.telemetry_artifact_count
        and approval.approved_approval_audit_reservation_bytes
        == approval_subject.approval_audit_reservation_bytes
        and approval.approved_approval_audit_reservation_count
        == approval_subject.approval_audit_reservation_count
        and approval.approved_operational_metadata_reservation_bytes
        == approval_subject.operational_metadata_reservation_bytes
        and approval.approved_operational_metadata_reservation_count
        == approval_subject.operational_metadata_reservation_count
        and approval.approved_total_bytes == approval_subject.estimated_total_bytes
    )
    approval_status = "NOT_REQUIRED"
    approval_id: str | None = None
    if estimate.estimated_bytes is None or estimated_total is None:
        # A missing authoritative storage estimate is a blocked, unavailable
        # plan—not a zero-byte or approval-free plan. This distinct state lets
        # strict RPC/rendering contracts preserve ``None`` without weakening
        # the resource boundary.
        approval_status = "UNAVAILABLE"
    elif oversize and policy.require_explicit_oversize_approval:
        if approval is None:
            approval_status = "REQUIRED"
        elif not approval_matches:
            approval_status = "STALE"
        else:
            approval_status = "APPROVED"
            approval_id = approval.approval_id
    if exceeds_current:
        if policy.require_explicit_oversize_approval and approval_status == "REQUIRED":
            blockers.append(
                "OVERSIZE_SYNC_APPROVAL_REQUIRED: estimated current-raster evidence exceeds "
                "policy and has no exact-plan approval"
            )
        elif policy.require_explicit_oversize_approval and approval_status == "STALE":
            blockers.append(
                "OVERSIZE_SYNC_APPROVAL_STALE: current-raster approval does not match this exact plan"
            )
        else:
            warnings.append(
                f"{approval.reviewed_by if approval_matches and approval else 'project policy'} "
                "approved oversized current-raster evidence"
            )
    if exceeds_materialized:
        if policy.require_explicit_oversize_approval and approval_status == "REQUIRED":
            blockers.append(
                "OVERSIZE_SYNC_APPROVAL_REQUIRED: estimated synchronized evidence is "
                f"{estimated_total} bytes; materialized policy permits "
                f"{policy.max_materialized_bytes} and no exact-plan approval is recorded"
            )
        elif policy.require_explicit_oversize_approval and approval_status == "STALE":
            blockers.append(
                "OVERSIZE_SYNC_APPROVAL_STALE: materialized-evidence approval does not match "
                "this exact plan"
            )
        else:
            warnings.append(
                f"{approval.reviewed_by if approval_matches and approval else 'project policy'} "
                "approved oversized materialized evidence"
            )
    return CloudSyncPlan(
        destination=destination,
        projections=tuple(projections),
        input_bytes=input_bytes,
        estimated_current_raster_bytes=estimate.estimated_bytes,
        telemetry_inventory_sha256=telemetry_inventory.sha256,
        telemetry_projected_bytes=telemetry_inventory.projected_bytes,
        telemetry_artifact_count=telemetry_inventory.artifact_count,
        approval_audit_reservation_bytes=(telemetry_inventory.approval_audit_reservation_bytes),
        approval_audit_reservation_count=(telemetry_inventory.approval_audit_reservation_count),
        operational_metadata_reservation_bytes=(
            telemetry_inventory.operational_metadata_reservation_bytes
        ),
        operational_metadata_reservation_count=(
            telemetry_inventory.operational_metadata_reservation_count
        ),
        telemetry_authority_sha256=telemetry_inventory.authority_sha256,
        authorized_envelopes=telemetry_inventory.authorized_envelopes,
        estimated_total_bytes=estimated_total,
        current_raster_estimate_status=estimate.status,
        actual_retained_current_raster_bytes=estimate.actual_retained_bytes,
        pending_estimated_current_raster_bytes=estimate.pending_estimated_bytes,
        expected_current_raster_count=estimate.expected_raster_count,
        retained_current_raster_count=estimate.retained_raster_count,
        current_raster_estimate_basis=estimate.basis,
        approval_subject=approval_subject,
        approval_status=approval_status,
        approval_id=approval_id,
        blockers=tuple(sorted(set(blockers))),
        warnings=tuple(sorted(set(warnings))),
    )


def _raw_projection(asset: ConnectivityAsset) -> AssetProjection:
    return AssetProjection(
        source_asset_id=asset.asset_id,
        uploaded_asset_id=asset.asset_id,
        representation="RAW",
        bytes=asset.byte_size,
    )
