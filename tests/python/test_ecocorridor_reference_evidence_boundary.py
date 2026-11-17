from __future__ import annotations

import pytest
from ecocorridor_core.contracts import (
    REFERENCE_CURRENT_DIAGNOSTIC_DISPOSITION,
    AssetKind,
    ConnectivityAsset,
    ConnectivityProjectBundle,
    ReferenceMetricTolerance,
    ReferenceScenarioBinding,
)
from ecocorridor_core.professional_validation import (
    validate_professional_bundle,
    validation_projection,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture


def _reference_asset(
    bundle: ConnectivityProjectBundle,
    kind: AssetKind,
    *,
    asset_id: str | None = None,
) -> ConnectivityAsset:
    source_asset = bundle.assets[0]
    resolved_id = asset_id or f"reference-{kind.value.lower().replace('_', '-')}"
    authorization = source_asset.authorization.model_copy(update={"asset_id": resolved_id})
    is_current = kind == AssetKind.REFERENCE_CURRENT
    return ConnectivityAsset.model_validate(
        {
            **source_asset.model_dump(mode="json"),
            "asset_id": resolved_id,
            "kind": kind,
            "title": f"Reviewed {kind.value}",
            "relative_path": f"references/{resolved_id}.bin",
            "media_type": "image/tiff" if is_current else "text/csv",
            "grid": source_asset.grid.model_dump(mode="json") if is_current else None,
            "authorization": authorization.model_dump(mode="json"),
        }
    )


def _binding(
    bundle: ConnectivityProjectBundle,
    matrix_asset_id: str,
    *,
    binding_id: str = "reference-binding-01",
    scenario_id: str | None = None,
    current_asset_id: str | None = None,
) -> ReferenceScenarioBinding:
    return ReferenceScenarioBinding(
        binding_id=binding_id,
        scenario_id=scenario_id or bundle.scenarios[0].scenario_id,
        reference_matrix_asset_id=matrix_asset_id,
        tolerance=ReferenceMetricTolerance(
            absolute_tolerance=1e-8,
            relative_tolerance=1e-6,
        ),
        reference_current_asset_id=current_asset_id,
        current_raster_disposition=(
            REFERENCE_CURRENT_DIAGNOSTIC_DISPOSITION
            if current_asset_id is not None
            else "NOT_PROVIDED"
        ),
        reference_basis="Approved external baseline result for this exact scenario.",
        reviewed_by="fixture-ecologist",
        reviewed_at=bundle.assets[0].authorization.reviewed_at,
        limitations=["Effective resistance is compared only for the declared focal pairs."],
    )


def _bundle_with_unbound_reference_asset(
    kind: AssetKind,
) -> tuple[ConnectivityProjectBundle, str]:
    bundle = build_ridgeway_fixture().bundle
    reference_asset = _reference_asset(bundle, kind)
    return ConnectivityProjectBundle.model_validate(
        {
            **bundle.model_dump(mode="json"),
            "assets": [
                *[asset.model_dump(mode="json") for asset in bundle.assets],
                reference_asset.model_dump(mode="json"),
            ],
        }
    ), reference_asset.asset_id


def _bundle_with_bound_matrix(
    *,
    include_current: bool = False,
) -> tuple[ConnectivityProjectBundle, ReferenceScenarioBinding, ConnectivityAsset | None]:
    bundle = build_ridgeway_fixture().bundle
    matrix_asset = _reference_asset(bundle, AssetKind.REFERENCE_MATRIX)
    current_asset = (
        _reference_asset(bundle, AssetKind.REFERENCE_CURRENT) if include_current else None
    )
    binding = _binding(
        bundle,
        matrix_asset.asset_id,
        current_asset_id=current_asset.asset_id if current_asset is not None else None,
    )
    return (
        ConnectivityProjectBundle.model_validate(
            {
                **bundle.model_dump(mode="json"),
                "assets": [
                    *[asset.model_dump(mode="json") for asset in bundle.assets],
                    matrix_asset.model_dump(mode="json"),
                    *([current_asset.model_dump(mode="json")] if current_asset is not None else []),
                ],
                "reference_bindings": [binding.model_dump(mode="json")],
            }
        ),
        binding,
        current_asset,
    )


@pytest.mark.parametrize(
    "kind",
    [AssetKind.REFERENCE_MATRIX, AssetKind.REFERENCE_CURRENT],
)
def test_unbound_reference_assets_block_professional_validation(kind: AssetKind) -> None:
    bundle, asset_id = _bundle_with_unbound_reference_asset(kind)

    findings = validate_professional_bundle(bundle)
    reference_findings = [item for item in findings if item.code == "REFERENCE_ASSET_UNBOUND"]

    assert len(reference_findings) == 1
    assert reference_findings[0].severity == "BLOCKING"
    assert reference_findings[0].object_reference == asset_id
    assert "Asset presence alone" in reference_findings[0].remediation
    assert validation_projection(bundle)["status"] == "BLOCKED"


def test_bound_matrix_stays_blocked_until_repository_comparison_exists() -> None:
    bundle, binding, _ = _bundle_with_bound_matrix()

    findings = validate_professional_bundle(bundle)
    pending = [item for item in findings if item.code == "REFERENCE_COMPARISON_REQUIRED"]

    assert len(pending) == 1
    assert pending[0].severity == "BLOCKING"
    assert pending[0].object_reference == binding.binding_id
    assert "repository-backed baseline matrix comparison" in pending[0].remediation
    assert not any(item.code == "REFERENCE_ASSET_UNBOUND" for item in findings)
    assert validation_projection(bundle)["status"] == "BLOCKED"


def test_reference_basis_rejects_prohibited_environmental_benefit_claims() -> None:
    bundle, binding, _ = _bundle_with_bound_matrix()
    unsafe_binding = binding.model_copy(
        update={
            "reference_basis": (
                "This reference demonstrates wildlife movement and reduces collisions."
            )
        }
    )
    unsafe_bundle = bundle.model_copy(update={"reference_bindings": [unsafe_binding]})

    prohibited = [
        item
        for item in validate_professional_bundle(unsafe_bundle)
        if item.code == "PROHIBITED_ECOLOGICAL_PURPOSE"
    ]

    assert len(prohibited) == 1
    assert prohibited[0].severity == "BLOCKING"
    assert prohibited[0].object_reference == (
        f"reference_binding[{binding.binding_id}].reference_basis"
    )


def test_partial_reference_scenario_coverage_requires_review() -> None:
    bundle, _, _ = _bundle_with_bound_matrix()

    coverage = [
        item
        for item in validate_professional_bundle(bundle)
        if item.code == "REFERENCE_BINDING_COVERAGE_INCOMPLETE"
    ]

    assert len(coverage) == 1
    assert coverage[0].severity == "REVIEW_REQUIRED"
    assert coverage[0].object_reference == "reference_bindings"
    assert bundle.scenarios[1].scenario_id in coverage[0].remediation


def test_reference_current_attachment_is_diagnostic_information_only() -> None:
    bundle, _, current_asset = _bundle_with_bound_matrix(include_current=True)
    assert current_asset is not None

    findings = validate_professional_bundle(bundle)
    diagnostic = [item for item in findings if item.code == "REFERENCE_CURRENT_DIAGNOSTIC_ONLY"]

    assert len(diagnostic) == 1
    assert diagnostic[0].severity == "INFORMATION"
    assert diagnostic[0].object_reference == current_asset.asset_id
    assert "does not numerically compare current rasters" in diagnostic[0].remediation
    assert any(item.code == "REFERENCE_COMPARISON_REQUIRED" for item in findings)


def test_reference_current_requires_exact_scenario_cell_alignment() -> None:
    bundle, binding, current_asset = _bundle_with_bound_matrix(include_current=True)
    assert current_asset is not None and current_asset.grid is not None
    transform = current_asset.grid.transform
    shifted_current = current_asset.model_copy(
        update={
            "grid": current_asset.grid.model_copy(
                update={
                    "transform": (
                        transform[0],
                        transform[1],
                        transform[2] + 1.0,
                        transform[3],
                        transform[4],
                        transform[5],
                    )
                }
            )
        }
    )
    invalid_bundle = bundle.model_copy(
        update={
            "assets": [
                shifted_current if item.asset_id == current_asset.asset_id else item
                for item in bundle.assets
            ]
        }
    )

    findings = validate_professional_bundle(invalid_bundle)

    invalid_grid = [item for item in findings if item.code == "REFERENCE_CURRENT_GRID_INVALID"]
    assert len(invalid_grid) == 1
    assert invalid_grid[0].severity == "BLOCKING"
    assert invalid_grid[0].object_reference == binding.binding_id
    assert "GridAlignmentPlan" in invalid_grid[0].remediation
    assert not any(item.code == "REFERENCE_COMPARISON_REQUIRED" for item in findings)


def test_duplicate_reference_bindings_block_even_if_contract_validation_was_bypassed() -> None:
    bundle, binding, _ = _bundle_with_bound_matrix()
    duplicate = binding.model_copy(update={"binding_id": "reference-binding-02"})
    invalid_bundle = bundle.model_copy(update={"reference_bindings": [binding, duplicate]})

    duplicates = [
        item
        for item in validate_professional_bundle(invalid_bundle)
        if item.code == "REFERENCE_BINDING_DUPLICATE"
    ]

    assert duplicates
    assert all(item.severity == "BLOCKING" for item in duplicates)
    assert {item.object_reference for item in duplicates} >= {
        binding.scenario_id,
        binding.reference_matrix_asset_id,
    }
    assert not any(
        item.code == "REFERENCE_COMPARISON_REQUIRED"
        for item in validate_professional_bundle(invalid_bundle)
    )


def test_missing_reference_scenario_blocks_if_contract_validation_was_bypassed() -> None:
    bundle, binding, _ = _bundle_with_bound_matrix()
    invalid = binding.model_copy(update={"scenario_id": "missing-scenario"})
    invalid_bundle = bundle.model_copy(update={"reference_bindings": [invalid]})

    findings = validate_professional_bundle(invalid_bundle)

    assert any(
        item.code == "REFERENCE_BINDING_SCENARIO_MISSING"
        and item.severity == "BLOCKING"
        and item.object_reference == invalid.binding_id
        for item in findings
    )
    assert not any(item.code == "REFERENCE_COMPARISON_REQUIRED" for item in findings)


def test_wrong_reference_asset_kind_blocks_if_contract_validation_was_bypassed() -> None:
    bundle = build_ridgeway_fixture().bundle
    binding = _binding(bundle, bundle.assets[0].asset_id)
    invalid_bundle = bundle.model_copy(update={"reference_bindings": [binding]})

    findings = validate_professional_bundle(invalid_bundle)

    assert any(
        item.code == "REFERENCE_BINDING_ASSET_KIND_INVALID"
        and item.severity == "BLOCKING"
        and item.object_reference == binding.binding_id
        for item in findings
    )
    assert not any(item.code == "REFERENCE_COMPARISON_REQUIRED" for item in findings)


def test_reference_asset_kinds_cannot_be_misread_as_validation_evidence() -> None:
    bundle, _ = _bundle_with_unbound_reference_asset(AssetKind.REFERENCE_MATRIX)
    validation_dimension = next(
        dimension
        for dimension in bundle.fitness.dimensions
        if dimension.key == "validation_evidence"
    )
    assert validation_dimension.status == "ACCEPTED"

    blockers = [
        item for item in validate_professional_bundle(bundle) if item.severity == "BLOCKING"
    ]

    assert [item.code for item in blockers] == ["REFERENCE_ASSET_UNBOUND"]
