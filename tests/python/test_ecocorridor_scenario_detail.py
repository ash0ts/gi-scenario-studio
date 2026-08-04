from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ecocorridor_core import rpc_worker as rpc_worker_module
from ecocorridor_core.contracts import (
    ConnectivityEvidenceClaim,
    ConnectivityRunPlan,
    DirectConnectivityRun,
    DirectRunState,
    EvidenceEdge,
    EvidenceNode,
    PairResistanceMetric,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    QAStatus,
)
from ecocorridor_core.grid import InMemoryRaster
from ecocorridor_core.metrics import CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION
from ecocorridor_core.raster_io import render_esri_ascii
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_worker import RPCDispatcher, RPCFault


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _claim(
    *,
    claim_id: str,
    kind: str,
    portfolio_id: str,
    scenario_id: str,
    direct_run_ids: list[str],
    value: float,
    pair_id: str | None = None,
    rule_id: str | None = None,
) -> ConnectivityEvidenceClaim:
    run_node = f"run:{direct_run_ids[0]}"
    return ConnectivityEvidenceClaim(
        claim_id=claim_id,
        claim_kind=kind,
        text="Direct Circuitscape result under documented assumptions.",
        value=value,
        units="percent",
        portfolio_id=portfolio_id,
        scenario_id=scenario_id,
        pair_id=pair_id,
        rule_id=rule_id,
        direct_run_ids=direct_run_ids,
        root_node_id=claim_id,
        nodes=[
            EvidenceNode(node_id=claim_id, node_type="CLAIM", label=claim_id),
            EvidenceNode(node_id=run_node, node_type="DIRECT_RUN", label=run_node),
        ],
        edges=[EvidenceEdge(from_node_id=claim_id, to_node_id=run_node, relation="reports")],
        limitations=[CONNECTIVITY_LIMITATION],
    )


def _run(
    *,
    analysis_id: str,
    revision_id: str,
    portfolio_id: str,
    scenario_id: str,
    pair_ids: list[str],
    values: list[float],
    baseline_values: list[float],
    matrix_sha256: str,
    current_map_sha256: str | None,
    identity_suffix: str,
) -> DirectConnectivityRun:
    metrics = [
        PairResistanceMetric(
            pair_id=pair_id,
            effective_resistance=value,
            baseline_effective_resistance=baseline,
            baseline_relative_change_percent=(baseline - value) / baseline * 100.0,
        )
        for pair_id, value, baseline in zip(pair_ids, values, baseline_values, strict=True)
    ]
    return DirectConnectivityRun(
        direct_run_id=f"run-{identity_suffix}",
        analysis_id=analysis_id,
        revision_id=revision_id,
        portfolio_id=portfolio_id,
        scenario_id=scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256="a" * 64,
        model_run_cache_key=hashlib.sha256(f"cache-{identity_suffix}".encode()).hexdigest(),
        input_sha256=hashlib.sha256(f"input-{identity_suffix}".encode()).hexdigest(),
        result_sha256=hashlib.sha256(f"result-{identity_suffix}".encode()).hexdigest(),
        matrix_sha256=matrix_sha256,
        current_map_sha256=current_map_sha256,
        pair_metrics=metrics,
        qa_status=QAStatus.ACCEPTED,
        completed_at=datetime(2026, 8, 4, tzinfo=UTC),
    )


class _ScenarioDetailStore:
    def __init__(
        self,
        *,
        root: Path,
        plan: ConnectivityRunPlan,
        bundle: Any,
        runs: list[DirectConnectivityRun],
        summaries: list[PortfolioDecisionSummary],
        claims: list[ConnectivityEvidenceClaim],
        artifacts: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.root = root
        self._plan = plan
        self._bundle = bundle
        self._runs = runs
        self._summaries = summaries
        self._claims = claims
        self._artifacts = artifacts
        self.verification_calls: list[str] = []
        self.invalid_run_id: str | None = None

    def analysis(self, analysis_id: str) -> dict[str, Any]:
        assert analysis_id == self._plan.analysis_id
        return {"state": "COMPLETED", "plan": self._plan.model_dump(mode="json")}

    def analysis_bundle(self, analysis_id: str) -> Any:
        assert analysis_id == self._plan.analysis_id
        return self._bundle

    def analysis_results(self, analysis_id: str) -> tuple[Any, Any, Any]:
        assert analysis_id == self._plan.analysis_id
        return self._runs, self._summaries, self._claims

    def run_artifacts(self, direct_run_id: str) -> list[dict[str, Any]]:
        return self._artifacts[direct_run_id]

    def verify_authoritative_direct_run(
        self, direct_run_id: str, *, expected_analysis_id: str
    ) -> Any:
        self.verification_calls.append(direct_run_id)
        if direct_run_id == self.invalid_run_id:
            raise ValueError("retained result protocol failed its checksum")
        run = next(item for item in self._runs if item.direct_run_id == direct_run_id)
        if run.analysis_id != expected_analysis_id:
            raise ValueError("direct evidence belongs to a different analysis")
        return SimpleNamespace(run=run)

    def close(self) -> None:
        return None


def _authoritative_store(
    tmp_path: Path, *, retain_maps: bool
) -> tuple[_ScenarioDetailStore, dict[str, str]]:
    fixture = build_ridgeway_fixture()
    scenario_a, scenario_b = fixture.bundle.scenarios[:2]
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    selected = next(
        item for item in fixture.portfolios if item.portfolio_id != baseline.portfolio_id
    )
    analysis_id = "analysis-scenario-detail"
    plan = ConnectivityRunPlan(
        analysis_id=analysis_id,
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[scenario_a.scenario_id, scenario_b.scenario_id],
        portfolios=[baseline, selected],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=4,
        solver_job_cap=4,
        search_method="ENUMERATION",
        seed=7,
        current_maps_for_portfolio_ids=(
            [baseline.portfolio_id, selected.portfolio_id] if retain_maps else []
        ),
        engine_manifest_sha256="a" * 64,
    )
    root = tmp_path / "project"
    artifacts_root = root / "blobs"
    artifacts_root.mkdir(parents=True)
    artifacts: dict[str, list[dict[str, Any]]] = {}

    hypothesis = next(
        item
        for item in fixture.bundle.resistance_hypotheses
        if item.hypothesis_id == scenario_a.resistance_hypothesis_id
    )
    resistance_asset = next(
        item for item in fixture.bundle.assets if item.asset_id == hypothesis.resistance_asset_id
    )
    assert resistance_asset.grid is not None
    grid = resistance_asset.grid
    baseline_map = render_esri_ascii(
        InMemoryRaster(
            asset_id="baseline-current",
            grid=grid,
            values=tuple(
                tuple(float(row + column + 1) for column in range(grid.width))
                for row in range(grid.height)
            ),
            source_sha256="b" * 64,
        )
    )
    selected_map = render_esri_ascii(
        InMemoryRaster(
            asset_id="portfolio-current",
            grid=grid,
            values=tuple(
                tuple(float((row + column + 1) * 1.1) for column in range(grid.width))
                for row in range(grid.height)
            ),
            source_sha256="c" * 64,
        )
    )

    def retained(run_key: str, role: str, payload: bytes) -> tuple[str, dict[str, Any]]:
        digest = _sha256(payload)
        path = artifacts_root / f"{run_key}-{role}"
        path.write_bytes(payload)
        return digest, {
            "role": role,
            "sha256": digest,
            "byte_size": len(payload),
            "cas_relative_path": path.relative_to(root).as_posix(),
            "media_type": "text/plain",
        }

    created_runs: list[DirectConnectivityRun] = []
    run_ids: dict[str, str] = {}
    for scenario_index, scenario in enumerate((scenario_a, scenario_b)):
        baseline_values = [
            10.0 + index + scenario_index * 100
            for index, _ in enumerate(scenario.included_pair_ids)
        ]
        for portfolio_name, portfolio, multiplier in (
            ("baseline", baseline, 1.0),
            ("selected", selected, 0.9 if scenario_index == 0 else 1.4),
        ):
            key = f"{portfolio_name}-{scenario.scenario_id}"
            matrix_payload = "\n".join(
                f"{index + 1} {index + 2} {value * multiplier}"
                for index, value in enumerate(baseline_values)
            ).encode()
            matrix_sha, matrix_record = retained(key, "matrix", matrix_payload)
            records = [matrix_record]
            current_sha = None
            if retain_maps and scenario.scenario_id == scenario_a.scenario_id:
                current_payload = baseline_map if portfolio_name == "baseline" else selected_map
                current_sha, current_record = retained(key, "current_map", current_payload)
                records.append(current_record)
            run = _run(
                analysis_id=analysis_id,
                revision_id=plan.revision_id,
                portfolio_id=portfolio.portfolio_id,
                scenario_id=scenario.scenario_id,
                pair_ids=scenario.included_pair_ids,
                values=[item * multiplier for item in baseline_values],
                baseline_values=baseline_values,
                matrix_sha256=matrix_sha,
                current_map_sha256=current_sha,
                identity_suffix=key,
            )
            created_runs.append(run)
            artifacts[run.direct_run_id] = records
            run_ids[key] = run.direct_run_id

    summaries = [
        PortfolioDecisionSummary(
            portfolio_id=portfolio.portfolio_id,
            disposition=PortfolioDisposition.PASSES,
            cost=0,
            currency="USD",
            rule_dispositions=[],
            controlling_scenario_id=scenario_a.scenario_id,
            controlling_pair_id=scenario_a.included_pair_ids[0],
            worst_resistance_reduction_percent=0,
            next_action="Candidate to carry forward for ecological, field, and engineering feasibility review.",
            direct_run_ids=sorted(
                item.direct_run_id
                for item in created_runs
                if item.portfolio_id == portfolio.portfolio_id
            ),
        )
        for portfolio in (baseline, selected)
    ]
    claims = [
        _claim(
            claim_id=f"claim-{pair_id}",
            kind="DIRECT_PAIR_METRIC",
            portfolio_id=selected.portfolio_id,
            scenario_id=scenario_a.scenario_id,
            pair_id=pair_id,
            rule_id="evidence:within-scenario-resistance-change",
            direct_run_ids=[
                run_ids[f"baseline-{scenario_a.scenario_id}"],
                run_ids[f"selected-{scenario_a.scenario_id}"],
            ],
            value=10.0,
        )
        for pair_id in scenario_a.included_pair_ids
    ]
    if retain_maps:
        claims.append(
            _claim(
                claim_id="claim-map-diagnostic",
                kind="DIRECT_MAP_DIAGNOSTIC",
                portfolio_id=selected.portfolio_id,
                scenario_id=scenario_a.scenario_id,
                direct_run_ids=[
                    run_ids[f"baseline-{scenario_a.scenario_id}"],
                    run_ids[f"selected-{scenario_a.scenario_id}"],
                ],
                value=10.0,
            )
        )
    return (
        _ScenarioDetailStore(
            root=root,
            plan=plan,
            bundle=fixture.bundle,
            runs=created_runs,
            summaries=summaries,
            claims=claims,
            artifacts=artifacts,
        ),
        {
            "analysis_id": analysis_id,
            "portfolio_id": selected.portfolio_id,
            "scenario_id": scenario_a.scenario_id,
            "other_scenario_id": scenario_b.scenario_id,
        },
    )


def _accept_minimal_fake_decision_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rpc_worker_module,
        "validate_portfolio_disposition_support",
        lambda **_kwargs: None,
    )


def test_scenario_detail_returns_one_exact_ecological_scope_and_rehashed_maps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identity = _authoritative_store(tmp_path, retain_maps=True)
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)
    result = dispatcher.dispatch(
        "connectivity.results.scenario_detail",
        {
            "project_directory": str(store.root),
            "analysis_id": identity["analysis_id"],
            "portfolio_id": identity["portfolio_id"],
            "scenario_id": identity["scenario_id"],
            "cell_offset": 0,
            "cell_limit": 7,
        },
    )["data"]

    assert result["scenario"]["scenario_id"] == identity["scenario_id"]
    assert result["scenario"]["scenario_id"] != identity["other_scenario_id"]
    assert result["same_scenario_only"] is True
    assert [item["pair_id"] for item in result["pair_evidence"]] == result["scenario"][
        "included_pair_ids"
    ]
    assert all(
        item["baseline_relative_reduction_percent"] == pytest.approx(10.0)
        for item in result["pair_evidence"]
    )
    assert all(
        identity["other_scenario_id"] not in item["portfolio_direct_run_id"]
        for item in result["pair_evidence"]
    )
    assert result["map_evidence"]["status"] == "AVAILABLE"
    assert result["map_evidence"]["preview"]["selection_method"] == "SYSTEMATIC_EXACT_CELL_SAMPLE"
    assert len(result["map_evidence"]["exact_cell_page"]["cells"]) == 7
    assert result["map_evidence"]["evidence_claim_ids"] == ["claim-map-diagnostic"]
    assert result["baseline_run"]["matrix_sha256"] != result["portfolio_run"]["matrix_sha256"]
    assert CURRENT_MAP_LIMITATION in result["limitations"]
    assert len(store.verification_calls) == 2
    assert len(set(store.verification_calls)) == 2


def test_decision_board_verifies_selected_and_baseline_direct_evidence_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept_minimal_fake_decision_claims(monkeypatch)
    store, identity = _authoritative_store(tmp_path, retain_maps=True)
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)

    result = dispatcher.dispatch(
        "connectivity.results.decision_board",
        {
            "project_directory": str(store.root),
            "analysis_id": identity["analysis_id"],
            "portfolio_ids": [identity["portfolio_id"]],
        },
    )["data"]

    expected_ids = {
        item.direct_run_id
        for item in store._runs
        if item.portfolio_id in {identity["portfolio_id"], store._plan.baseline_portfolio_id}
    }
    assert set(result["direct_run_ids"]) == expected_ids
    assert set(store.verification_calls) == expected_ids
    assert len(store.verification_calls) == len(expected_ids)


def test_decision_board_blocks_when_a_baseline_evidence_chain_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept_minimal_fake_decision_claims(monkeypatch)
    store, identity = _authoritative_store(tmp_path, retain_maps=True)
    store.invalid_run_id = next(
        item.direct_run_id
        for item in store._runs
        if item.portfolio_id == store._plan.baseline_portfolio_id
    )
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.results.decision_board",
            {
                "project_directory": str(store.root),
                "analysis_id": identity["analysis_id"],
                "portfolio_ids": [identity["portfolio_id"]],
            },
        )

    assert raised.value.error.code == "DECISION_BOARD_DIRECT_EVIDENCE_INVALID"
    assert raised.value.error.category.value == "SECURITY"


def test_decision_board_blocks_an_incomplete_verified_baseline_ensemble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accept_minimal_fake_decision_claims(monkeypatch)
    store, identity = _authoritative_store(tmp_path, retain_maps=True)
    store._runs = [
        item
        for item in store._runs
        if not (
            item.portfolio_id == store._plan.baseline_portfolio_id
            and item.scenario_id == identity["other_scenario_id"]
        )
    ]
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.results.decision_board",
            {
                "project_directory": str(store.root),
                "analysis_id": identity["analysis_id"],
                "portfolio_ids": [identity["portfolio_id"]],
            },
        )

    assert raised.value.error.code == "DECISION_BOARD_INCOMPLETE_BASELINE"
    assert raised.value.error.category.value == "MODEL_QUALITY"


def test_decision_board_blocks_a_summary_that_no_longer_reduces_its_direct_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identity = _authoritative_store(tmp_path, retain_maps=True)

    def reject_inconsistent_derivation(**_kwargs: Any) -> None:
        raise ValueError("worst-case aggregate does not equal its direct argmin")

    monkeypatch.setattr(
        rpc_worker_module,
        "validate_portfolio_disposition_support",
        reject_inconsistent_derivation,
    )
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.results.decision_board",
            {
                "project_directory": str(store.root),
                "analysis_id": identity["analysis_id"],
                "portfolio_ids": [identity["portfolio_id"]],
            },
        )

    assert raised.value.error.code == "DECISION_BOARD_DERIVATION_EVIDENCE_INVALID"
    assert raised.value.error.category.value == "SECURITY"


def test_scenario_detail_blocks_when_the_result_protocol_chain_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identity = _authoritative_store(tmp_path, retain_maps=True)
    store.invalid_run_id = next(
        item.direct_run_id
        for item in store._runs
        if item.portfolio_id == identity["portfolio_id"]
        and item.scenario_id == identity["scenario_id"]
    )
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.results.scenario_detail",
            {
                "project_directory": str(store.root),
                "analysis_id": identity["analysis_id"],
                "portfolio_id": identity["portfolio_id"],
                "scenario_id": identity["scenario_id"],
            },
        )

    assert raised.value.error.code == "SCENARIO_DETAIL_DIRECT_EVIDENCE_INVALID"
    assert raised.value.error.category.value == "SECURITY"


def test_scenario_detail_keeps_pair_evidence_and_reports_map_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identity = _authoritative_store(tmp_path, retain_maps=False)
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)
    result = dispatcher.dispatch(
        "connectivity.results.scenario_detail",
        {
            "project_directory": str(store.root),
            "analysis_id": identity["analysis_id"],
            "portfolio_id": identity["portfolio_id"],
            "scenario_id": identity["scenario_id"],
        },
    )["data"]

    assert result["pair_evidence"]
    assert result["map_evidence"]["status"] == "ABSENT"
    assert result["map_evidence"]["absence_code"] == "CURRENT_MAPS_NOT_RETAINED"
    assert result["map_evidence"]["evidence_claim_ids"] == []
    assert result["map_evidence"]["grid"] is None
    assert result["map_evidence"]["preview"] is None
    assert "rerun" in result["map_evidence"]["absence_reason"].lower()


def test_scenario_detail_rejects_a_cell_page_outside_the_authoritative_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identity = _authoritative_store(tmp_path, retain_maps=True)
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)
    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.results.scenario_detail",
            {
                "project_directory": str(store.root),
                "analysis_id": identity["analysis_id"],
                "portfolio_id": identity["portfolio_id"],
                "scenario_id": identity["scenario_id"],
                "cell_offset": 1_000_000,
            },
        )
    assert raised.value.error.code == "SCENARIO_DETAIL_CELL_PAGE_INVALID"


def test_scenario_detail_rejects_changed_retained_matrix_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identity = _authoritative_store(tmp_path, retain_maps=True)
    baseline_run_id = next(
        item.direct_run_id
        for item in store._runs
        if item.portfolio_id == store._plan.baseline_portfolio_id
        and item.scenario_id == identity["scenario_id"]
    )
    matrix_record = next(
        item for item in store._artifacts[baseline_run_id] if item["role"] == "matrix"
    )
    (store.root / matrix_record["cas_relative_path"]).write_bytes(b"changed matrix bytes")
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)

    with pytest.raises(RPCFault) as raised:
        dispatcher.dispatch(
            "connectivity.results.scenario_detail",
            {
                "project_directory": str(store.root),
                "analysis_id": identity["analysis_id"],
                "portfolio_id": identity["portfolio_id"],
                "scenario_id": identity["scenario_id"],
            },
        )
    assert raised.value.error.code == "SCENARIO_DETAIL_ARTIFACT_INTEGRITY_FAILED"
