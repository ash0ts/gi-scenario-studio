from __future__ import annotations

from copy import deepcopy

from ecocorridor_core.contracts import CloudDisposition, ConnectivityProjectBundle
from ecocorridor_core.governance import (
    CurrentRasterEstimate,
    CurrentRasterEstimateStatus,
    SyncDestination,
    plan_cloud_sync,
    scientific_asset_ids,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from scenario_platform.observability import (
    ProjectScope,
    ProjectScopeEvidence,
    ScopeEvidenceKind,
)


def _scope(deployment: str) -> ProjectScopeEvidence:
    return ProjectScopeEvidence(
        entity="wildlife-team",
        project="ridgeway",
        external_project_id="immutable-project-id",
        deployment=deployment,
        api_base_url="https://api.wandb.example",
        scope=ProjectScope.RESTRICTED,
        evidence_kind=ScopeEvidenceKind.PUBLIC_API_DOCUMENTED_EXACT,
        provider_field="visibility_scope",
    )


def _bundle_with_generalized_reference(
    reference_kind: str,
    *,
    bind_derivative: bool,
) -> tuple[ConnectivityProjectBundle, str, str]:
    """Build one reviewed reference binding and its approved cloud derivative."""

    payload = deepcopy(build_ridgeway_fixture().bundle.model_dump(mode="json"))
    scenario = payload["scenarios"][0]
    hypothesis = next(
        item
        for item in payload["resistance_hypotheses"]
        if item["hypothesis_id"] == scenario["resistance_hypothesis_id"]
    )
    resistance = next(
        item for item in payload["assets"] if item["asset_id"] == hypothesis["resistance_asset_id"]
    )

    source_id = f"reference_{reference_kind.casefold()}_raw"
    derivative_id = f"{source_id}_generalized"
    source = deepcopy(resistance)
    source.update(
        {
            "asset_id": source_id,
            "kind": reference_kind,
            "title": f"Reviewed {reference_kind}",
            "relative_path": f"reference/{source_id}.dat",
            "sha256": "a" * 64,
            "media_type": "text/plain",
        }
    )
    if reference_kind == "REFERENCE_MATRIX":
        source["grid"] = None
    source["authorization"].update(
        {
            "asset_id": source_id,
            "cloud_disposition": CloudDisposition.GENERALIZED_DERIVATIVE_ONLY,
            "generalized_asset_id": derivative_id,
        }
    )
    derivative = deepcopy(source)
    derivative.update(
        {
            "asset_id": derivative_id,
            "title": f"Approved generalized {reference_kind}",
            "relative_path": f"reference/{derivative_id}.dat",
            "sha256": "b" * 64,
        }
    )
    derivative["authorization"].update(
        {
            "asset_id": derivative_id,
            "cloud_disposition": CloudDisposition.RAW_ALLOWED,
            "generalized_asset_id": None,
        }
    )
    if bind_derivative:
        # Once the derivative is the reviewed scientific input, retain the
        # restricted source only as contextual provenance rather than a second,
        # unbound reference-evidence asset.
        source["kind"] = "DOCUMENT"
        source["grid"] = None
    payload["assets"].extend([source, derivative])

    matrix_id = derivative_id if reference_kind == "REFERENCE_MATRIX" and bind_derivative else None
    if matrix_id is None:
        if reference_kind == "REFERENCE_MATRIX":
            matrix_id = source_id
        else:
            matrix = deepcopy(source)
            matrix_id = "reference_matrix_control"
            matrix.update(
                {
                    "asset_id": matrix_id,
                    "kind": "REFERENCE_MATRIX",
                    "title": "Reviewed reference matrix",
                    "relative_path": "reference/reference-matrix-control.txt",
                    "sha256": "c" * 64,
                    "grid": None,
                }
            )
            matrix["authorization"].update(
                {
                    "asset_id": matrix_id,
                    "cloud_disposition": CloudDisposition.RAW_ALLOWED,
                    "generalized_asset_id": None,
                }
            )
            payload["assets"].append(matrix)

    current_id = None
    if reference_kind == "REFERENCE_CURRENT":
        current_id = derivative_id if bind_derivative else source_id
    binding = {
        "binding_id": f"binding_{reference_kind.casefold()}",
        "scenario_id": scenario["scenario_id"],
        "reference_matrix_asset_id": matrix_id,
        "tolerance": {
            "absolute_tolerance": 0.1,
            "relative_tolerance": 0.01,
        },
        "reference_basis": "Synthetic reviewed reference evidence.",
        "reviewed_by": "Test ecologist",
        "reviewed_at": source["authorization"]["reviewed_at"],
        "limitations": ["Synthetic governance test evidence."],
    }
    if current_id is not None:
        binding.update(
            {
                "reference_current_asset_id": current_id,
                "current_raster_disposition": ("DIAGNOSTIC_ONLY_NOT_NUMERICALLY_COMPARED_V0_1"),
            }
        )
    payload["reference_bindings"] = [binding]
    return ConnectivityProjectBundle.model_validate(payload), source_id, derivative_id


def test_cloud_sync_requires_authoritative_restricted_scope() -> None:
    bundle = build_ridgeway_fixture().bundle
    plan = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=None,
        estimated_current_raster_bytes=0,
    )
    assert not plan.allowed
    assert any("authoritative exact Restricted" in blocker for blocker in plan.blockers)


def test_prohibited_asset_blocks_full_sync() -> None:
    payload = build_ridgeway_fixture().bundle.model_dump(mode="json")
    payload = deepcopy(payload)
    payload["assets"][0]["authorization"]["cloud_disposition"] = CloudDisposition.PROHIBITED
    payload["assets"][0]["authorization"]["approved_deployments"] = ["LOCAL_ONLY"]
    bundle = ConnectivityProjectBundle.model_validate(payload)
    plan = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("wandb_cloud"),
        estimated_current_raster_bytes=0,
    )
    assert not plan.allowed
    assert any(bundle.assets[0].asset_id in blocker for blocker in plan.blockers)


def test_current_raster_is_not_silently_omitted() -> None:
    bundle = build_ridgeway_fixture().bundle
    plan = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("wandb_cloud"),
        estimated_current_raster_bytes=bundle.resource_policy.max_current_raster_bytes + 1,
    )
    assert not plan.allowed
    assert any("current-raster" in blocker for blocker in plan.blockers)


def test_unknown_current_raster_plan_is_explicit_and_never_rendered_as_zero() -> None:
    bundle = build_ridgeway_fixture().bundle
    plan = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("wandb_cloud"),
        current_raster_estimate=CurrentRasterEstimate(
            status=CurrentRasterEstimateStatus.UNAVAILABLE,
            estimated_bytes=None,
            actual_retained_bytes=0,
            pending_estimated_bytes=None,
            expected_raster_count=None,
            retained_raster_count=0,
            basis="No immutable run plan exists.",
        ),
    )
    assert not plan.allowed
    assert plan.estimated_current_raster_bytes is None
    assert plan.estimated_total_bytes is None
    assert any("CURRENT_RASTER_ESTIMATE_UNAVAILABLE" in item for item in plan.blockers)


def test_explicit_oversize_approval_covers_current_and_total_materialized_limits() -> None:
    bundle = build_ridgeway_fixture().bundle
    estimate = (
        max(
            bundle.resource_policy.max_current_raster_bytes,
            bundle.resource_policy.max_materialized_bytes,
        )
        + 1
    )
    blocked = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("wandb_cloud"),
        estimated_current_raster_bytes=estimate,
    )
    assert not blocked.allowed
    assert any("current-raster evidence exceeds" in item for item in blocked.blockers)
    assert any("materialized policy" in item for item in blocked.blockers)

    approved = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("wandb_cloud"),
        estimated_current_raster_bytes=estimate,
        oversize_approved_by="Dr. Ecologist",
    )
    assert approved.allowed
    assert any("oversized current-raster" in item for item in approved.warnings)
    assert any("oversized materialized" in item for item in approved.warnings)


def test_scope_evidence_must_match_dedicated_or_self_managed_destination() -> None:
    plan = plan_cloud_sync(
        build_ridgeway_fixture().bundle,
        destination=SyncDestination.DEDICATED,
        scope_evidence=_scope("SELF_MANAGED"),
        estimated_current_raster_bytes=0,
    )
    assert not plan.allowed
    assert "W&B scope evidence belongs to another deployment" in plan.blockers


def test_critical_infrastructure_assets_require_non_multitenant_destination() -> None:
    payload = deepcopy(build_ridgeway_fixture().bundle.model_dump(mode="json"))
    payload["assets"][0]["authorization"]["sensitivity"] = "CRITICAL_INFRASTRUCTURE"
    bundle = ConnectivityProjectBundle.model_validate(payload)
    plan = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("WANDB_CLOUD"),
        estimated_current_raster_bytes=0,
    )
    assert not plan.allowed
    assert any("critical-infrastructure restricted" in blocker for blocker in plan.blockers)


def test_generalized_projection_cannot_mask_raw_scientific_model_use() -> None:
    payload = deepcopy(build_ridgeway_fixture().bundle.model_dump(mode="json"))
    source = payload["assets"][0]
    derivative_id = f"{source['asset_id']}_generalized"
    source["authorization"].update(
        {
            "cloud_disposition": CloudDisposition.GENERALIZED_DERIVATIVE_ONLY,
            "generalized_asset_id": derivative_id,
        }
    )
    derivative = deepcopy(source)
    derivative.update(
        {
            "asset_id": derivative_id,
            "title": f"{source['title']} — generalized",
            "relative_path": "assets/generalized-resistance-h1.asc",
        }
    )
    derivative["authorization"].update(
        {
            "asset_id": derivative_id,
            "cloud_disposition": CloudDisposition.RAW_ALLOWED,
            "generalized_asset_id": None,
        }
    )
    payload["assets"].append(derivative)
    bundle = ConnectivityProjectBundle.model_validate(payload)

    blocked = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("wandb_cloud"),
        estimated_current_raster_bytes=0,
    )
    assert not blocked.allowed
    assert any("scientific model still references" in item for item in blocked.blockers)

    payload["resistance_hypotheses"][0]["resistance_asset_id"] = derivative_id
    revised = ConnectivityProjectBundle.model_validate(payload)
    allowed = plan_cloud_sync(
        revised,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("wandb_cloud"),
        estimated_current_raster_bytes=0,
    )
    assert allowed.allowed
    derivative_projections = [
        item for item in allowed.projections if item.uploaded_asset_id == derivative_id
    ]
    assert {item.source_asset_id for item in derivative_projections} == {
        source["asset_id"],
        derivative_id,
    }


def test_bound_reference_matrix_requires_exact_raw_cloud_evidence() -> None:
    bundle, source_id, _ = _bundle_with_generalized_reference(
        "REFERENCE_MATRIX",
        bind_derivative=False,
    )

    assert source_id in scientific_asset_ids(bundle)
    plan = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("wandb_cloud"),
        estimated_current_raster_bytes=0,
    )

    assert not plan.allowed
    assert any(
        f"scientific model still references generalized-only asset {source_id}" in blocker
        for blocker in plan.blockers
    )


def test_bound_reference_current_requires_exact_raw_cloud_evidence() -> None:
    bundle, source_id, _ = _bundle_with_generalized_reference(
        "REFERENCE_CURRENT",
        bind_derivative=False,
    )

    assert source_id in scientific_asset_ids(bundle)
    plan = plan_cloud_sync(
        bundle,
        destination=SyncDestination.WANDB_CLOUD,
        scope_evidence=_scope("wandb_cloud"),
        estimated_current_raster_bytes=0,
    )

    assert not plan.allowed
    assert any(
        f"scientific model still references generalized-only asset {source_id}" in blocker
        for blocker in plan.blockers
    )


def test_approved_derivative_must_be_the_reviewed_reference_binding() -> None:
    for reference_kind in ("REFERENCE_MATRIX", "REFERENCE_CURRENT"):
        bundle, source_id, derivative_id = _bundle_with_generalized_reference(
            reference_kind,
            bind_derivative=True,
        )

        scientific = scientific_asset_ids(bundle)
        assert derivative_id in scientific
        assert source_id not in scientific
        plan = plan_cloud_sync(
            bundle,
            destination=SyncDestination.WANDB_CLOUD,
            scope_evidence=_scope("wandb_cloud"),
            estimated_current_raster_bytes=0,
        )

        assert plan.allowed
        projected = {
            (item.source_asset_id, item.uploaded_asset_id, item.representation)
            for item in plan.projections
        }
        assert (source_id, derivative_id, "GENERALIZED_DERIVATIVE") in projected
        assert (derivative_id, derivative_id, "RAW") in projected
