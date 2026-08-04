from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_response_contracts import (
    RPC_RESPONSE_DATA_CONTRACTS,
    JsonRpcErrorFrame,
    JsonRpcSuccessFrame,
    RpcPayloadResult,
)
from ecocorridor_core.rpc_worker import RPCDispatcher
from pydantic import ValidationError
from scenario_platform.contracts import canonical_json


def _hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _scope_evidence() -> dict[str, Any]:
    return {
        "entity": "eco-team",
        "project": "restricted-project",
        "external_project_id": "external-project-1",
        "deployment": "DEDICATED",
        "api_base_url": "https://api.wandb.example",
        "scope": "RESTRICTED",
        "evidence_kind": "ADMIN_API_DOCUMENTED_EXACT",
        "provider_field": "admin.project.scope",
    }


def _binding(*, state: str = "CONNECTED") -> dict[str, Any]:
    return {
        "state": state,
        "binding_id": "binding-1",
        "entity": "eco-team",
        "project": "restricted-project",
        "deployment": "DEDICATED",
        "api_base_url": "https://api.wandb.example",
        "web_base_url": "https://wandb.example",
        "profile_namespace": "ecocorridor",
        "scope_evidence": _scope_evidence(),
        "verified": True,
        "external_project_id": "external-project-1",
    }


def _detailed_preview(
    *,
    approval_status: str = "NOT_REQUIRED",
    approval_id: str | None = None,
    allowed: bool = True,
) -> dict[str, Any]:
    return {
        "destination": "DEDICATED",
        "entity": "eco-team",
        "project": "restricted-project",
        "external_project_id": "external-project-1",
        "projections": [],
        "model_revision_id": "revision-1",
        "model_sha256": "b" * 64,
        "input_bytes": 10,
        "estimated_current_raster_bytes": 20,
        "telemetry_inventory_sha256": "a" * 64,
        "telemetry_projected_bytes": 70_000,
        "telemetry_artifact_count": 1,
        "approval_audit_reservation_bytes": 0,
        "approval_audit_reservation_count": 0,
        "operational_metadata_reservation_bytes": 65_536,
        "operational_metadata_reservation_count": 1,
        "estimated_total_bytes": 70_030,
        "current_raster_estimate_status": "PRE_RUN_ESTIMATE",
        "actual_retained_current_raster_bytes": 0,
        "pending_estimated_current_raster_bytes": 20,
        "expected_current_raster_count": 1,
        "retained_current_raster_count": 0,
        "current_raster_estimate_basis": "One reviewed current-raster estimate.",
        "sync_plan_sha256": "f" * 64,
        "approval_status": approval_status,
        "approval_id": approval_id,
        "blockers": [] if allowed else ["OVERSIZE_SYNC_APPROVAL_REQUIRED"],
        "warnings": [],
        "allowed": allowed,
    }


def _unavailable_preview() -> dict[str, Any]:
    return {
        "approval_status": "UNAVAILABLE",
        "blockers": ["PLANNING_MODEL_INCOMPLETE"],
        "warnings": [],
        "allowed": False,
        "estimated_current_raster_bytes": None,
        "estimated_total_bytes": None,
        "current_raster_estimate_status": "UNAVAILABLE",
        "actual_retained_current_raster_bytes": 0,
        "pending_estimated_current_raster_bytes": None,
        "expected_current_raster_count": None,
        "retained_current_raster_count": 0,
        "current_raster_estimate_basis": "Planning model incomplete.",
        "sync_plan_sha256": None,
        "approval_id": None,
        "message": "Complete the planning model.",
        "technical_type": "ValueError",
    }


def _detailed_unavailable_preview() -> dict[str, Any]:
    return {
        **_detailed_preview(allowed=False),
        "approval_status": "UNAVAILABLE",
        "approval_id": None,
        "blockers": ["PLANNING_MODEL_INCOMPLETE"],
        "estimated_current_raster_bytes": None,
        "estimated_total_bytes": None,
        "current_raster_estimate_status": "UNAVAILABLE",
        "pending_estimated_current_raster_bytes": None,
        "expected_current_raster_count": None,
        "current_raster_estimate_basis": "Planning model incomplete.",
    }


def _assert_no_absolute_local_path(value: Any, *, trail: tuple[str, ...] = ()) -> None:
    forbidden_keys = {
        "project_directory",
        "export_directory",
        "engine_executable",
        "source_path",
        "work_directory",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in forbidden_keys, f"renderer path key at {'.'.join((*trail, key))}"
            _assert_no_absolute_local_path(child, trail=(*trail, key))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_absolute_local_path(child, trail=(*trail, str(index)))
        return
    if not isinstance(value, str) or value.startswith(("https://", "http://")):
        return
    assert not Path(value).is_absolute(), f"absolute path at {'.'.join(trail)}"
    assert re.match(r"^[A-Za-z]:[\\/]", value) is None, f"Windows path at {'.'.join(trail)}"


def test_every_registered_eco_method_has_an_exact_response_contract(tmp_path: Path) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    registered = set(dispatcher.methods) - {"system.ping"}
    assert registered <= set(RPC_RESPONSE_DATA_CONTRACTS)


def test_method_specific_response_rejects_extra_missing_and_wrong_method_data(
    tmp_path: Path,
) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    valid = {
        "project_id": "project-1",
        "archive_sha256": "a" * 64,
        "state": "IMPORTED",
    }
    result = dispatcher._result("connectivity.bundle.import", valid)
    assert result.data.model_dump(mode="json") == valid

    with pytest.raises(ValidationError, match="Extra inputs"):
        dispatcher._result("connectivity.bundle.import", {**valid, "project_directory": "/tmp"})
    with pytest.raises(ValidationError, match="Field required"):
        dispatcher._result("connectivity.bundle.import", {"project_id": "project-1"})
    with pytest.raises(ValidationError):
        dispatcher._result(
            "connectivity.bundle.import",
            {"project_id": "project-1", "state": "PLANNING_DRAFT", "next_action": "Continue"},
        )


def test_cloud_sync_preview_requires_exact_actionable_identity() -> None:
    contract = RPC_RESPONSE_DATA_CONTRACTS["observability.preview_upload"]
    with pytest.raises(ValidationError, match="exact identity fields"):
        contract.model_validate(
            {
                "approval_status": "REQUIRED",
                "blockers": ["OVERSIZE_SYNC_APPROVAL_REQUIRED"],
                "warnings": [],
                "allowed": False,
            }
        )

    unavailable = contract.model_validate(_unavailable_preview())
    assert unavailable.sync_plan_sha256 is None
    with pytest.raises(ValidationError, match="blocked with a finding"):
        contract.model_validate({**_unavailable_preview(), "blockers": []})
    with pytest.raises(ValidationError, match="partial exact plan"):
        contract.model_validate({**_unavailable_preview(), "entity": "partial-team"})
    with pytest.raises(ValidationError, match="resolved approval status"):
        contract.model_validate(
            _detailed_preview(
                approval_status="REQUIRED",
                allowed=True,
            )
        )
    with pytest.raises(ValidationError, match="exact byte inventory"):
        contract.model_validate({**_detailed_preview(), "estimated_total_bytes": 70_031})


def test_observability_binding_connect_and_sync_are_exactly_cross_validated() -> None:
    binding_contract = RPC_RESPONSE_DATA_CONTRACTS["observability.get_status"]
    assert binding_contract.model_validate(_binding()).verified is True
    for update in (
        {"verified": False},
        {"profile_namespace": "gi-v2"},
        {"external_project_id": ""},
    ):
        with pytest.raises(ValidationError):
            binding_contract.model_validate({**_binding(), **update})

    connect_contract = RPC_RESPONSE_DATA_CONTRACTS["observability.connect"]
    connect_contract.model_validate({"binding": _binding(), "upload_preview": _detailed_preview()})
    connect_contract.model_validate(
        {"binding": _binding(), "upload_preview": _detailed_unavailable_preview()}
    )
    connect_contract.model_validate(
        {"binding": _binding(), "upload_preview": _unavailable_preview()}
    )
    with pytest.raises(ValidationError, match="differs from its exact project binding"):
        connect_contract.model_validate(
            {
                "binding": _binding(),
                "upload_preview": {**_detailed_preview(), "entity": "another-team"},
            }
        )
    with pytest.raises(ValidationError, match="differs from its exact project binding"):
        connect_contract.model_validate(
            {
                "binding": _binding(),
                "upload_preview": {
                    **_detailed_unavailable_preview(),
                    "external_project_id": "another-project-id",
                },
            }
        )

    receipt = {
        "online": True,
        "claimed": 1,
        "synced": 1,
        "failed": 0,
        "blocked": 0,
        "queued_offline": False,
        "remaining": 0,
        "state_counts": {"SYNCED": 1},
        "sync_state": "CONNECTED",
    }
    sync_contract = RPC_RESPONSE_DATA_CONTRACTS["observability.sync"]
    sync_contract.model_validate({"binding": _binding(), "receipt": receipt})
    with pytest.raises(ValidationError, match="remaining count"):
        sync_contract.model_validate(
            {"binding": _binding(), "receipt": {**receipt, "remaining": 1}}
        )
    with pytest.raises(ValidationError, match="binding state differs"):
        sync_contract.model_validate(
            {
                "binding": _binding(state="SYNC_QUEUED_OFFLINE"),
                "receipt": receipt,
            }
        )


def test_oversize_approval_response_must_match_every_exact_plan_field() -> None:
    approval = {
        "approval_id": f"oversize_approval_{'f' * 64}",
        "project_id": "project-1",
        "binding_id": "binding-1",
        "destination": "DEDICATED",
        "entity": "eco-team",
        "project": "restricted-project",
        "external_project_id": "external-project-1",
        "model_revision_id": "revision-1",
        "model_sha256": "b" * 64,
        "bundle_sha256": "c" * 64,
        "resource_policy_sha256": "d" * 64,
        "projection_sha256": "e" * 64,
        "current_raster_estimate_sha256": "1" * 64,
        "scope_evidence_sha256": "2" * 64,
        "telemetry_inventory_sha256": "a" * 64,
        "sync_plan_sha256": "f" * 64,
        "approved_input_bytes": 10,
        "approved_current_raster_bytes": 20,
        "approved_pending_current_raster_bytes": 20,
        "approved_telemetry_projected_bytes": 70_000,
        "approved_telemetry_artifact_count": 1,
        "approved_approval_audit_reservation_bytes": 0,
        "approved_approval_audit_reservation_count": 0,
        "approved_operational_metadata_reservation_bytes": 65_536,
        "approved_operational_metadata_reservation_count": 1,
        "approved_total_bytes": 70_030,
        "reviewed_by": "Ecology reviewer",
        "approved_at": "2026-08-04T12:00:00Z",
    }
    preview = _detailed_preview(
        approval_status="APPROVED",
        approval_id=approval["approval_id"],
        allowed=True,
    )
    contract = RPC_RESPONSE_DATA_CONTRACTS["observability.approve_oversize"]
    contract.model_validate({"approval": approval, "upload_preview": preview})
    with pytest.raises(ValidationError, match="differs from returned exact plan"):
        contract.model_validate(
            {
                "approval": approval,
                "upload_preview": {**preview, "entity": "another-team"},
            }
        )


def test_payload_hash_and_json_rpc_frames_are_validated_fail_closed(tmp_path: Path) -> None:
    payload = {"project_id": "project-1", "state": "PLANNING_DRAFT", "next_action": "Continue"}
    with pytest.raises(ValidationError, match="hash does not match"):
        RpcPayloadResult[Any](
            method="connectivity.planning.create", data=payload, data_sha256="0" * 64
        )

    dispatcher = RPCDispatcher(data_root=tmp_path)
    result = dispatcher._result("connectivity.planning.create", payload)
    frame = JsonRpcSuccessFrame(id="request-1", result=result)
    assert frame.result.data_sha256 == _hash(payload)
    with pytest.raises(ValidationError, match="Extra inputs"):
        JsonRpcSuccessFrame.model_validate(
            {**frame.model_dump(mode="json"), "local_debug_path": "/tmp/project"}
        )
    with pytest.raises(ValidationError, match="cannot be boolean"):
        JsonRpcSuccessFrame.model_validate({**frame.model_dump(mode="json"), "id": True})

    malformed = json.loads(
        dispatcher.handle_frame(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "bad-request",
                    "method": "system.ping",
                    "params": {"unexpected": True},
                }
            )
        )
    )
    error = JsonRpcErrorFrame.model_validate(malformed)
    assert error.error.data.code == "RPC_PROTOCOL_ERROR"
    with pytest.raises(ValidationError, match="Extra inputs"):
        JsonRpcErrorFrame.model_validate({**malformed, "traceback": "secret local path"})
    with pytest.raises(ValidationError, match="cannot be boolean"):
        JsonRpcErrorFrame.model_validate({**malformed, "id": False})


def test_renderer_responses_do_not_expose_authoritative_local_paths(tmp_path: Path) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    guided = dispatcher.dispatch("connectivity.guided.open", {})
    ping = json.loads(
        dispatcher.handle_frame(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "system.ping", "params": {}})
        )
    )
    JsonRpcSuccessFrame.model_validate(ping)
    _assert_no_absolute_local_path(guided)
    _assert_no_absolute_local_path(ping)


def test_authoritative_project_state_prepare_and_download_preview_are_worker_backed(
    tmp_path: Path,
) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    dispatcher.dispatch("connectivity.guided.open", {})
    project = tmp_path / "guided" / "ridgeway"

    state = dispatcher.dispatch(
        "connectivity.project.get_state", {"project_directory": str(project)}
    )["data"]
    assert state["project"]["mode"] == "GUIDED"
    assert state["effective_bundle"]["project_id"] == state["project"]["project_id"]
    assert state["engine"]["available"] is False
    _assert_no_absolute_local_path(state)

    prepared = dispatcher.dispatch(
        "connectivity.analysis.prepare",
        {"project_directory": str(project), "seed": 17},
    )["data"]
    assert prepared["engine_available"] is False
    assert prepared["proposed_plan"] is None
    assert any(
        item["code"] == "PINNED_CIRCUITSCAPE_ENGINE_UNAVAILABLE" for item in prepared["blockers"]
    )

    preview = dispatcher.dispatch(
        "connectivity.asset.download.preview",
        {
            "project_directory": str(project),
            "provider_id": "tiger_line_2025",
            "source_url": (
                "https://www2.census.gov/geo/tiger/TIGER2025/ROADS/tl_2025_06001_roads.zip"
            ),
            "extent_description": "Synthetic Ridgeway study context",
            "expected_bytes": 1024,
        },
    )["data"]
    assert preview["provider_id"] == "tiger_line_2025"
    assert preview["intended_use"] == "road and study-context review only"
    assert preview["resource_policy_allowed"] is True


def test_intervention_preview_cannot_reassign_an_option_id_from_another_site() -> None:
    bundle = build_ridgeway_fixture().bundle
    first_site, second_site = bundle.intervention_sites[:2]
    borrowed = next(
        option for option in bundle.intervention_options if option.site_id == second_site.site_id
    ).model_copy(update={"site_id": first_site.site_id})
    proposed_site = first_site.model_copy(update={"option_ids": [borrowed.option_id]})

    with pytest.raises(ValueError, match="already belong to another site"):
        RPCDispatcher._candidate_intervention_bundle(bundle, proposed_site, [borrowed])


def test_guided_status_contract_enforces_direct_job_progress_arithmetic() -> None:
    contract = RPC_RESPONSE_DATA_CONTRACTS["connectivity.guided.status"]
    valid = {
        "session_id": "guided_ridgeway_v1",
        "analysis_id": "analysis-1",
        "state": "RUNNING",
        "phase": "MAP",
        "completed_jobs": 1540,
        "total_jobs": 1596,
        "matrix_completed": 1536,
        "map_completed": 4,
        "cached_jobs": 100,
        "differential": None,
    }
    assert contract.model_validate(valid).completed_jobs == 1540
    with pytest.raises(ValidationError, match="matrix plus map"):
        contract.model_validate({**valid, "completed_jobs": 1539})
    with pytest.raises(ValidationError, match="cached jobs"):
        contract.model_validate({**valid, "cached_jobs": 1541})
