from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core.contracts import MAX_SAFE_FOCAL_ID, FocalAreaSet
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_worker import RPCDispatcher, RPCFault
from pydantic import ValidationError


def _valid_focal_set_payload() -> dict[str, Any]:
    focal_set = build_ridgeway_fixture().bundle.focal_area_sets[0]
    return focal_set.model_dump(mode="json")


def _duplicate_endpoint_payload() -> dict[str, Any]:
    payload = _valid_focal_set_payload()
    focal_set = build_ridgeway_fixture().bundle.focal_area_sets[0]
    duplicate = focal_set.pairs[0].model_copy(
        update={"pair_id": f"{focal_set.pairs[0].pair_id}_alias"}
    )
    payload["pairs"].append(duplicate.model_dump(mode="json"))
    return payload


def _payload_with_upper_focal_id(value: int) -> dict[str, Any]:
    payload = _valid_focal_set_payload()
    payload["focal_ids"][-1] = value
    payload["pairs"][-1]["pair_id"] = f"upper_focal_pair_3_{value}"
    payload["pairs"][-1]["to_focal_id"] = value
    return payload


def _initialize_project(project: Path) -> None:
    fixture = build_ridgeway_fixture()
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="PLANNING",
            skeleton=fixture.bundle.model_dump(mode="json"),
        )
    finally:
        store.close()


def test_focal_area_set_rejects_pair_ids_that_alias_canonical_endpoints() -> None:
    with pytest.raises(ValidationError, match="unique canonical endpoint pairs"):
        FocalAreaSet.model_validate(_duplicate_endpoint_payload())


def test_focal_pair_requires_the_ascending_form_of_its_unordered_identity() -> None:
    payload = _valid_focal_set_payload()
    pair = payload["pairs"][0]
    pair["from_focal_id"], pair["to_focal_id"] = (
        pair["to_focal_id"],
        pair["from_focal_id"],
    )

    with pytest.raises(ValidationError, match="ascending focal IDs"):
        FocalAreaSet.model_validate(payload)


def test_focal_pair_rejects_a_self_pair() -> None:
    payload = _valid_focal_set_payload()
    payload["pairs"][0]["to_focal_id"] = payload["pairs"][0]["from_focal_id"]

    with pytest.raises(ValidationError, match="two different focal IDs"):
        FocalAreaSet.model_validate(payload)


@pytest.mark.parametrize("invalid_id", [True, "1"], ids=["boolean", "numeric-string"])
@pytest.mark.parametrize("target", ["focal-id-list", "pair-endpoint"])
def test_focal_area_set_does_not_coerce_focal_identifiers(target: str, invalid_id: object) -> None:
    payload = _valid_focal_set_payload()
    if target == "focal-id-list":
        payload["focal_ids"][0] = invalid_id
    else:
        payload["pairs"][0]["from_focal_id"] = invalid_id

    with pytest.raises(ValidationError):
        FocalAreaSet.model_validate(payload)


def test_focal_area_set_preserves_the_maximum_safe_focal_identity() -> None:
    focal_set = FocalAreaSet.model_validate(_payload_with_upper_focal_id(MAX_SAFE_FOCAL_ID))

    assert focal_set.focal_ids[-1] == MAX_SAFE_FOCAL_ID
    assert focal_set.pairs[-1].to_focal_id == MAX_SAFE_FOCAL_ID


def test_focal_area_set_rejects_ids_above_the_renderer_safe_range() -> None:
    with pytest.raises(ValidationError) as raised:
        FocalAreaSet.model_validate(_payload_with_upper_focal_id(MAX_SAFE_FOCAL_ID + 1))

    locations = {tuple(item["loc"]) for item in raised.value.errors()}
    assert ("focal_ids", 3) in locations
    assert ("pairs", 1, "to_focal_id") in locations


@pytest.mark.parametrize("invalid_required", ["false", 0, 1], ids=["string", "zero", "one"])
def test_focal_pair_does_not_coerce_required_status(invalid_required: object) -> None:
    payload = _valid_focal_set_payload()
    payload["pairs"][0]["required"] = invalid_required

    with pytest.raises(ValidationError):
        FocalAreaSet.model_validate(payload)


def test_focal_area_save_rpc_rejects_duplicate_canonical_endpoints(tmp_path: Path) -> None:
    project = tmp_path / "duplicate-focal-pair-project"

    with pytest.raises(RPCFault) as raised:
        RPCDispatcher(data_root=tmp_path).dispatch(
            "connectivity.focal_areas.save",
            {
                "project_directory": str(project),
                "focal_area_set": _duplicate_endpoint_payload(),
            },
        )

    assert raised.value.code == -32602
    assert raised.value.error.code == "RPC_CONTRACT_REJECTED"
    assert not project.exists()


@pytest.mark.parametrize("invalid_required", ["false", 0, 1], ids=["string", "zero", "one"])
def test_focal_area_save_rpc_does_not_coerce_required_status(
    tmp_path: Path, invalid_required: object
) -> None:
    payload = _valid_focal_set_payload()
    payload["pairs"][0]["required"] = invalid_required
    project = tmp_path / "invalid-required-status"

    with pytest.raises(RPCFault) as raised:
        RPCDispatcher(data_root=tmp_path).dispatch(
            "connectivity.focal_areas.save",
            {
                "project_directory": str(project),
                "focal_area_set": payload,
            },
        )

    assert raised.value.code == -32602
    assert raised.value.error.code == "RPC_CONTRACT_REJECTED"
    assert not project.exists()


def test_focal_area_save_rpc_preserves_the_maximum_safe_focal_identity(
    tmp_path: Path,
) -> None:
    project = tmp_path / "maximum-safe-focal-id"
    _initialize_project(project)

    response = RPCDispatcher(data_root=tmp_path).dispatch(
        "connectivity.focal_areas.save",
        {
            "project_directory": str(project),
            "focal_area_set": _payload_with_upper_focal_id(MAX_SAFE_FOCAL_ID),
        },
    )

    assert response["data"]["record_id"] == "focal_set_target_guild_a"
    store = EcoProjectStore(project)
    try:
        saved = store.list_records("focal_area_set")
    finally:
        store.close()
    assert saved[0]["focal_ids"][-1] == MAX_SAFE_FOCAL_ID
    assert saved[0]["pairs"][-1]["to_focal_id"] == MAX_SAFE_FOCAL_ID


def test_focal_area_save_rpc_rejects_ids_above_the_renderer_safe_range(
    tmp_path: Path,
) -> None:
    project = tmp_path / "above-safe-focal-id"
    _initialize_project(project)

    with pytest.raises(RPCFault) as raised:
        RPCDispatcher(data_root=tmp_path).dispatch(
            "connectivity.focal_areas.save",
            {
                "project_directory": str(project),
                "focal_area_set": _payload_with_upper_focal_id(MAX_SAFE_FOCAL_ID + 1),
            },
        )

    assert raised.value.code == -32602
    assert raised.value.error.code == "RPC_CONTRACT_REJECTED"
    store = EcoProjectStore(project)
    try:
        assert store.list_records("focal_area_set") == []
    finally:
        store.close()


@pytest.mark.parametrize("invalid_id", [True, "1"], ids=["boolean", "numeric-string"])
@pytest.mark.parametrize("target", ["focal-id-list", "pair-endpoint"])
def test_focal_area_save_rpc_does_not_coerce_focal_identifiers(
    tmp_path: Path, target: str, invalid_id: object
) -> None:
    payload = _valid_focal_set_payload()
    if target == "focal-id-list":
        payload["focal_ids"][0] = invalid_id
    else:
        payload["pairs"][0]["from_focal_id"] = invalid_id
    project = tmp_path / f"invalid-{target}"

    with pytest.raises(RPCFault) as raised:
        RPCDispatcher(data_root=tmp_path).dispatch(
            "connectivity.focal_areas.save",
            {
                "project_directory": str(project),
                "focal_area_set": payload,
            },
        )

    assert raised.value.code == -32602
    assert raised.value.error.code == "RPC_CONTRACT_REJECTED"
    assert not project.exists()
