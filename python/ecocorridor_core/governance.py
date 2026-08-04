"""Fail-closed ecological asset authorization and resource planning."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from scenario_platform.observability import ProjectScope, ProjectScopeEvidence

from .contracts import (
    CloudDisposition,
    ConnectivityAsset,
    ConnectivityProjectBundle,
    SensitivityClass,
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
class CloudSyncPlan:
    destination: SyncDestination
    projections: tuple[AssetProjection, ...]
    input_bytes: int
    estimated_current_raster_bytes: int | None
    estimated_total_bytes: int | None
    current_raster_estimate_status: CurrentRasterEstimateStatus
    actual_retained_current_raster_bytes: int
    pending_estimated_current_raster_bytes: int | None
    expected_current_raster_count: int | None
    retained_current_raster_count: int
    current_raster_estimate_basis: str
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return not self.blockers


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
    oversize_approved_by: str | None = None,
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
    input_bytes = sum(item.bytes for item in unique_uploaded.values())
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
        estimated_total = input_bytes + estimate.estimated_bytes
    exceeds_current = (
        estimate.estimated_bytes is not None
        and estimate.estimated_bytes > policy.max_current_raster_bytes
    )
    exceeds_materialized = (
        estimated_total is not None and estimated_total > policy.max_materialized_bytes
    )
    if exceeds_current:
        if policy.require_explicit_oversize_approval and not (oversize_approved_by or "").strip():
            blockers.append(
                "estimated current-raster evidence exceeds policy and has no explicit approval"
            )
        else:
            warnings.append(
                f"{oversize_approved_by or 'project policy'} approved oversized current-raster evidence"
            )
    if exceeds_materialized:
        if policy.require_explicit_oversize_approval and not (oversize_approved_by or "").strip():
            blockers.append(
                f"estimated synchronized evidence is {estimated_total} bytes; materialized policy "
                f"permits {policy.max_materialized_bytes} and no explicit approval is recorded"
            )
        else:
            warnings.append(
                f"{oversize_approved_by or 'project policy'} approved oversized materialized evidence"
            )
    return CloudSyncPlan(
        destination=destination,
        projections=tuple(projections),
        input_bytes=input_bytes,
        estimated_current_raster_bytes=estimate.estimated_bytes,
        estimated_total_bytes=estimated_total,
        current_raster_estimate_status=estimate.status,
        actual_retained_current_raster_bytes=estimate.actual_retained_bytes,
        pending_estimated_current_raster_bytes=estimate.pending_estimated_bytes,
        expected_current_raster_count=estimate.expected_raster_count,
        retained_current_raster_count=estimate.retained_raster_count,
        current_raster_estimate_basis=estimate.basis,
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
