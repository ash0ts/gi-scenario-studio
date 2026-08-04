from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core.contracts import (
    ConnectivityEvidenceClaim,
    ConnectivityRunPlan,
    EvidenceEdge,
    EvidenceNode,
    PortfolioDecisionSummary,
    PortfolioSelection,
    RuleDisposition,
)
from ecocorridor_core.observability import ECOCORRIDOR_APP_PROFILE
from ecocorridor_core.telemetry import EcoTelemetryProjector
from scenario_platform.cloud_sync import CloudIntegrityError


class _Store:
    def __init__(self, root: Path, rows: list[dict[str, Any]]) -> None:
        self.root = root.resolve()
        self.rows = rows

    def project(self) -> dict[str, Any]:
        return {"project_id": "project-1"}

    def telemetry_rows(self, **_filters: Any) -> list[dict[str, Any]]:
        return self.rows


def _projector(tmp_path: Path, rows: list[dict[str, Any]]) -> EcoTelemetryProjector:
    return EcoTelemetryProjector(
        store=_Store(tmp_path, rows),  # type: ignore[arg-type]
        project_root=tmp_path,
        sync_plan={"allowed": True, "blockers": [], "projections": []},
        profile=ECOCORRIDOR_APP_PROFILE,
    )


def _plan() -> ConnectivityRunPlan:
    portfolios = [
        PortfolioSelection(
            portfolio_id=f"portfolio-{index}",
            selections={"site-1": f"option-{index}"},
            canonical_option_ids=(f"option-{index}",),
        )
        for index in range(2)
    ]
    return ConnectivityRunPlan(
        analysis_id="analysis-1",
        revision_id="revision-1",
        scenario_ids=["scenario-1", "scenario-2"],
        portfolios=portfolios,
        baseline_portfolio_id="portfolio-0",
        direct_job_count=4,
        solver_job_cap=4,
        search_method="SOBOL",
        seed=17,
        engine_manifest_sha256="a" * 64,
    )


def _claim() -> ConnectivityEvidenceClaim:
    return ConnectivityEvidenceClaim(
        claim_id="claim-adverse",
        claim_kind="DIRECT_PAIR_METRIC",
        text="Direct Circuitscape result under documented assumptions.",
        value=-2.5,
        units="percent",
        portfolio_id="portfolio-1",
        scenario_id="scenario-1",
        pair_id="core-a--core-b",
        rule_id="rule-nonregression",
        direct_run_ids=["run-baseline", "run-portfolio"],
        root_node_id="claim-adverse",
        nodes=[
            EvidenceNode(node_id="claim-adverse", node_type="CLAIM", label="Adverse result"),
            EvidenceNode(
                node_id="rule:rule-nonregression",
                node_type="RULE",
                label="Safeguard nonregression",
            ),
            EvidenceNode(node_id="run-portfolio", node_type="DIRECT_RUN", label="Direct run"),
        ],
        edges=[
            EvidenceEdge(
                from_node_id="claim-adverse",
                to_node_id="rule:rule-nonregression",
                relation="evaluates",
            ),
            EvidenceEdge(
                from_node_id="claim-adverse",
                to_node_id="run-portfolio",
                relation="supported_by",
            ),
        ],
        limitations=["Connectivity result only."],
    )


def test_sync_failures_table_contains_only_durable_failure_records(tmp_path: Path) -> None:
    rows = [
        {
            "outbox_id": "outbox-failed",
            "event_type": "analysis_completion",
            "subject_id": "analysis-1",
            "analysis_id": "analysis-1",
            "state": "UPLOADING",
            "attempt_count": 2,
            "last_error_code": "WANDB_RUN_INITIALIZATION_FAILED",
            "created_at": "2026-08-03T00:00:00+00:00",
            "updated_at": "2026-08-03T00:01:00+00:00",
        },
        {
            "outbox_id": "outbox-clean",
            "event_type": "bundle_import",
            "subject_id": "project-1",
            "analysis_id": None,
            "state": "PENDING",
            "attempt_count": 0,
            "last_error_code": None,
            "created_at": "2026-08-03T00:00:00+00:00",
            "updated_at": "2026-08-03T00:00:00+00:00",
        },
    ]
    projection = _projector(tmp_path, rows)._base(
        {"project_id": "project-1", "event_type": "guided_session"},
        run_kind="guided_session",
        run_subject_id="guided-1",
        event_sequence=1,
    )

    table = next(item for item in projection.tables if item.logical_name == "sync_failures")
    assert len(table.rows) == 1
    assert table.rows[0][0] == "outbox-failed"
    assert table.rows[0][6] == "WANDB_RUN_INITIALIZATION_FAILED"


def test_search_batch_table_is_an_exact_projection_of_the_immutable_plan() -> None:
    plan = _plan()
    table = EcoTelemetryProjector._search_batches_table(plan, state="PLANNED")

    assert table.logical_name == "search_batches"
    assert table.rows[0][0:9] == [
        "analysis-1",
        0,
        "INITIAL_IMMUTABLE_PLAN",
        "PLANNED",
        "SOBOL",
        17,
        2,
        2,
        4,
    ]
    assert len(table.rows[0][-1]) == 64


def test_adverse_changes_table_requires_failed_rule_and_direct_evidence() -> None:
    claim = _claim()
    summary = PortfolioDecisionSummary(
        portfolio_id="portfolio-1",
        disposition="FAILS",
        cost=12_000,
        currency="USD",
        rule_dispositions=[
            RuleDisposition(
                rule_id="rule-nonregression",
                scenario_id="scenario-1",
                pair_id="core-a--core-b",
                observed_value=-2.5,
                passed=False,
                evidence_claim_id=claim.claim_id,
                explanation="The directly evaluated safeguard rule failed.",
            ),
            RuleDisposition(
                rule_id="rule-cost",
                observed_value=12_000,
                passed=True,
                explanation="The cost rule passed.",
            ),
        ],
        safeguard_passed=False,
        next_action="Review the safeguard result before feasibility work.",
        direct_run_ids=["run-baseline", "run-portfolio"],
    )

    table = EcoTelemetryProjector._adverse_changes_table(
        summaries=[summary],
        claims=[claim],
        successful_direct_run_ids={"run-baseline", "run-portfolio"},
    )

    assert table is not None
    assert table.logical_name == "adverse_changes"
    assert len(table.rows) == 1
    assert table.rows[0][0:4] == [
        "portfolio-1",
        "DIRECT_PAIR_METRIC",
        "rule-nonregression",
        "scenario-1",
    ]
    assert table.rows[0][7] == "claim-adverse"

    with pytest.raises(CloudIntegrityError, match="DIRECT_RUN_MISSING"):
        EcoTelemetryProjector._adverse_changes_table(
            summaries=[summary],
            claims=[claim],
            successful_direct_run_ids={"run-baseline"},
        )
