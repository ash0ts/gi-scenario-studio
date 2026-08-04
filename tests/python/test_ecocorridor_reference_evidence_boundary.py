from __future__ import annotations

import pytest
from ecocorridor_core.contracts import AssetKind, ConnectivityAsset, ConnectivityProjectBundle
from ecocorridor_core.professional_validation import (
    validate_professional_bundle,
    validation_projection,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture


def _bundle_with_unbound_reference_asset(kind: AssetKind) -> tuple[ConnectivityProjectBundle, str]:
    bundle = build_ridgeway_fixture().bundle
    source_asset = bundle.assets[0]
    asset_id = f"unbound-{kind.value.lower().replace('_', '-')}"
    authorization = source_asset.authorization.model_copy(update={"asset_id": asset_id})
    reference_asset = ConnectivityAsset.model_validate(
        {
            **source_asset.model_dump(mode="json"),
            "asset_id": asset_id,
            "kind": kind,
            "title": f"Unbound {kind.value}",
            "relative_path": f"references/{asset_id}.bin",
            "media_type": "application/octet-stream",
            "grid": None,
            "authorization": authorization.model_dump(mode="json"),
        }
    )
    return ConnectivityProjectBundle.model_validate(
        {
            **bundle.model_dump(mode="json"),
            "assets": [
                *[asset.model_dump(mode="json") for asset in bundle.assets],
                reference_asset.model_dump(mode="json"),
            ],
        }
    ), asset_id


@pytest.mark.parametrize(
    "kind",
    [AssetKind.REFERENCE_MATRIX, AssetKind.REFERENCE_CURRENT],
)
def test_unbound_reference_assets_block_professional_validation(kind: AssetKind) -> None:
    bundle, asset_id = _bundle_with_unbound_reference_asset(kind)

    findings = validate_professional_bundle(bundle)
    reference_findings = [
        item for item in findings if item.code == "REFERENCE_EVIDENCE_BINDING_UNSUPPORTED"
    ]

    assert len(reference_findings) == 1
    assert reference_findings[0].severity == "BLOCKING"
    assert reference_findings[0].object_reference == asset_id
    assert "no explicit contract" in reference_findings[0].remediation
    assert "does not compare it" in reference_findings[0].remediation

    projection = validation_projection(bundle)
    assert projection["status"] == "BLOCKED"


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

    assert [item.code for item in blockers] == ["REFERENCE_EVIDENCE_BINDING_UNSUPPORTED"]
