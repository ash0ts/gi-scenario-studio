from __future__ import annotations

from pathlib import Path

import pytest
from ecocorridor_core.analysis_executor import DurableAnalysisExecutor
from ecocorridor_core.contracts import ConnectivityCostBasis
from ecocorridor_core.cost_basis import (
    cost_basis_requirements,
    with_reviewer_confirmation,
)
from ecocorridor_core.export import compute_export_eligibility
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_worker import RPCDispatcher, RPCFault


def _reviewed_basis_payload() -> dict[str, object]:
    fixture = build_ridgeway_fixture()
    basis = fixture.bundle.cost_basis
    assert basis is not None
    return basis.model_copy(
        update={
            "assumptions": [
                "Analysis horizon years: 30",
                "Real discount rate percent: 3.0",
                "CAPEX basis: Reviewer-entered conceptual capital screening values.",
                "O&M assumptions: Annual inspection and maintenance are included in each reviewed option value.",
                "Replacement assumptions: Replacement timing and residual value are included in each reviewed option value.",
            ]
        }
    ).model_dump(mode="json")


def _project_without_cost_basis(tmp_path: Path) -> tuple[Path, RPCDispatcher]:
    fixture = build_ridgeway_fixture()
    bundle = fixture.bundle.model_copy(update={"cost_basis": None})
    project = tmp_path / "professional-cost-project"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=bundle.project_id,
            title=bundle.title,
            mode="IMPORTED",
            skeleton={"source": "cost-basis-test"},
        )
        store.store_bundle(bundle)
    finally:
        store.close()
    return project, RPCDispatcher(data_root=tmp_path)


def test_reviewed_cost_basis_ledger_requires_each_lifecycle_field_and_confirmation() -> None:
    fixture = build_ridgeway_fixture()
    basis = fixture.bundle.cost_basis
    assert basis is not None
    assert {
        item.code for item in cost_basis_requirements(basis, fixture.bundle.intervention_options)
    } == {"COST_BASIS_LEDGER_FIELD_MISSING"}

    reviewed = with_reviewer_confirmation(
        ConnectivityCostBasis.model_validate(_reviewed_basis_payload()),
        "I reviewed the complete lifecycle screening cost basis for this decision.",
    )
    assert cost_basis_requirements(reviewed, fixture.bundle.intervention_options) == []

    bundle = fixture.bundle.model_copy(update={"cost_basis": reviewed})
    portfolio = fixture.portfolios[0]
    cost = sum(reviewed.option_costs[item] for item in portfolio.canonical_option_ids)
    claim = DurableAnalysisExecutor._cost_claim(
        claim_id="cost-claim-ledger-test",
        bundle=bundle,
        portfolio=portfolio,
        cost=cost,
        currency=reviewed.currency,
    )
    basis_node = next(item for item in claim.nodes if item.node_type == "COST_BASIS")
    assert basis_node.sha256 == reviewed.sha256
    assert basis_node.attributes["analysis_horizon_years"] == 30
    assert basis_node.attributes["real_discount_rate_percent"] == 3.0
    assert basis_node.attributes["reviewer_confirmation"].startswith("I reviewed")


def test_confirmed_cost_basis_round_trips_as_one_semantic_revision(tmp_path: Path) -> None:
    project, dispatcher = _project_without_cost_basis(tmp_path)
    before_store = EcoProjectStore(project)
    try:
        before_revision = before_store.effective_bundle().revision.revision_id
    finally:
        before_store.close()

    response = dispatcher.dispatch(
        "connectivity.cost_basis.save",
        {
            "project_directory": str(project),
            "cost_basis": _reviewed_basis_payload(),
            "confirmed": True,
            "confirmation_statement": (
                "I reviewed the complete screening cost ledger for this decision use."
            ),
        },
    )["data"]

    assert response["confirmed"] is True
    assert response["active_revision_id"] != before_revision
    assert (
        response["cost_basis_sha256"]
        == ConnectivityCostBasis.model_validate(response["cost_basis"]).sha256
    )
    assert any(
        item.startswith("Reviewer confirmation:") for item in response["cost_basis"]["assumptions"]
    )

    fetched = dispatcher.dispatch(
        "connectivity.cost_basis.get", {"project_directory": str(project)}
    )["data"]
    assert fetched["confirmed"] is True
    assert fetched["missing_requirements"] == []
    assert fetched["cost_basis"] == response["cost_basis"]
    assert fetched["active_revision_id"] == response["active_revision_id"]

    repeated = dispatcher.dispatch(
        "connectivity.cost_basis.save",
        {
            "project_directory": str(project),
            "cost_basis": _reviewed_basis_payload(),
            "confirmed": True,
            "confirmation_statement": (
                "I reviewed the complete screening cost ledger for this decision use."
            ),
        },
    )["data"]
    assert repeated["active_revision_id"] == response["active_revision_id"]
    assert repeated["revision"] == response["revision"]

    store = EcoProjectStore(project)
    try:
        effective = store.effective_bundle()
        assert effective.cost_basis is not None
        assert effective.cost_basis.sha256 == response["cost_basis_sha256"]
        assert store.get_record("cost_basis", "current") == response["cost_basis"]
        revision = effective.revision
        assert revision.revision_id == response["active_revision_id"]
        assert revision.diff_id is not None
        diff = store.get_record("semantic_diff", revision.diff_id)
        assert any(
            item["object_type"] == "cost_basis" and item["object_id"] == "current"
            for item in diff["changes"]
        )
    finally:
        store.close()


def test_cost_basis_save_requires_explicit_confirmation_and_exact_coverage(tmp_path: Path) -> None:
    project, dispatcher = _project_without_cost_basis(tmp_path)
    payload = _reviewed_basis_payload()

    with pytest.raises(RPCFault) as unconfirmed:
        dispatcher.dispatch(
            "connectivity.cost_basis.save",
            {
                "project_directory": str(project),
                "cost_basis": payload,
                "confirmed": False,
                "confirmation_statement": "I reviewed this complete cost screening basis.",
            },
        )
    assert unconfirmed.value.error.code == "RPC_CONTRACT_REJECTED"

    option_costs = dict(payload["option_costs"])  # type: ignore[arg-type]
    option_costs.pop(next(iter(option_costs)))
    with pytest.raises(RPCFault) as incomplete:
        dispatcher.dispatch(
            "connectivity.cost_basis.save",
            {
                "project_directory": str(project),
                "cost_basis": {**payload, "option_costs": option_costs},
                "confirmed": True,
                "confirmation_statement": "I reviewed this complete cost screening basis.",
            },
        )
    assert incomplete.value.error.code == "COST_BASIS_REVIEW_INCOMPLETE"

    with pytest.raises(RPCFault) as unsafe_confirmation:
        dispatcher.dispatch(
            "connectivity.cost_basis.save",
            {
                "project_directory": str(project),
                "cost_basis": payload,
                "confirmed": True,
                "confirmation_statement": (
                    "I reviewed this cost basis and it guarantees collision reduction."
                ),
            },
        )
    assert unsafe_confirmation.value.error.code == "COST_BASIS_REVIEW_INCOMPLETE"


def test_professional_validation_and_export_fail_closed_without_confirmed_ledger(
    tmp_path: Path,
) -> None:
    project, dispatcher = _project_without_cost_basis(tmp_path)
    validation = dispatcher.dispatch(
        "connectivity.model.validate", {"project_directory": str(project)}
    )["data"]
    assert "COST_BASIS_MISSING" in {item["code"] for item in validation["findings"]}

    fixture = build_ridgeway_fixture()
    eligibility = compute_export_eligibility(
        bundle=fixture.bundle,
        current_authorization_bundle=fixture.bundle,
        summaries=[],
        runs=[],
        deployment="LOCAL_ONLY",
        sync_state="CONNECTED",
        professional=True,
    )
    assert eligibility.eligible is False
    assert any("Analysis horizon years:" in item for item in eligibility.authorization_blockers)
