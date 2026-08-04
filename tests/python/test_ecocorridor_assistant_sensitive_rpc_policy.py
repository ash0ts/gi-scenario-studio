from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ecocorridor_core.assistants import SensitiveContextPolicy
from ecocorridor_core.contracts import (
    AssetKind,
    CloudDisposition,
    ConnectivityAsset,
    SensitiveDataAuthorization,
    SensitivityClass,
)
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.rpc_worker import RPCDispatcher, RPCFault


def _sensitive_asset() -> ConnectivityAsset:
    return ConnectivityAsset(
        asset_id="asset-hidden-den",
        kind=AssetKind.DOCUMENT,
        title="Hidden Den North",
        relative_path="data_assets/hidden-den.txt",
        sha256="a" * 64,
        byte_size=1,
        media_type="text/plain",
        source="Reviewed project evidence",
        vintage="2026-08-03",
        license="LicenseRef-Project-Evidence",
        authorization=SensitiveDataAuthorization(
            asset_id="asset-hidden-den",
            ownership="Project team",
            license_spdx_or_name="LicenseRef-Project-Evidence",
            sensitivity=SensitivityClass.SENSITIVE_LOCATION,
            cloud_disposition=CloudDisposition.RAW_ALLOWED,
            export_allowed=False,
            retention_policy="Retain with the restricted project.",
            spatial_redaction="Generalize named den locations.",
            approved_deployments=["SELF_MANAGED"],
            reviewer="Data steward",
            reviewed_at=datetime(2026, 8, 3, tzinfo=UTC),
        ),
    )


def test_renderer_cannot_self_authorize_exact_location_assistant_context(tmp_path) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    project = tmp_path / "project"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id="project-sensitive",
            title="Sensitive project",
            mode="IMPORTED",
            skeleton={"test": True},
        )
        with pytest.raises(RPCFault) as error:
            dispatcher._validated_evidence_sensitive_policy(
                store,
                [],
                SensitiveContextPolicy(exact_location_authorized=True),
            )
    finally:
        store.close()

    assert error.value.error.code == "ASSISTANT_EXACT_LOCATION_AUTHORIZATION_UNAVAILABLE"


def test_worker_adds_authoritative_sensitive_asset_terms_to_redaction_policy(tmp_path) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    project = tmp_path / "project"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id="project-sensitive",
            title="Sensitive project",
            mode="IMPORTED",
            skeleton={"test": True},
        )
        asset = _sensitive_asset()
        store.put_record("asset", asset.asset_id, asset.model_dump(mode="json"))
        policy = dispatcher._validated_evidence_sensitive_policy(
            store,
            [],
            SensitiveContextPolicy(prohibited_terms=["Reviewer supplied term"]),
        )
    finally:
        store.close()

    assert policy.exact_location_authorized is False
    assert set(policy.prohibited_terms) == {
        "Reviewer supplied term",
        "asset-hidden-den",
        "Hidden Den North",
    }
