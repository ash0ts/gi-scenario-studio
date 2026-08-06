from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from ecocorridor_core.contracts import (
    ConnectivityEvidenceClaim,
    ConnectivityRunPlan,
    EvidenceEdge,
    EvidenceNode,
    PortfolioDecisionSummary,
)
from ecocorridor_core.observability import ECOCORRIDOR_APP_PROFILE
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.review_brief import (
    REVIEW_BRIEF_ACKNOWLEDGEMENT,
    AuthorizedReviewSnapshot,
    ReviewBriefAcknowledgement,
    ReviewBriefActionKind,
    ReviewBriefAgentOutput,
    ReviewBriefAttestation,
    ReviewBriefFeedbackRecord,
    ReviewBriefFeedbackTraceReceipt,
    ReviewBriefFindingKind,
    ReviewBriefFindingSelection,
    ReviewBriefLocalFinding,
    ReviewBriefPriority,
    ReviewBriefReasonFacet,
    ReviewBriefRejectedRunnerReceipt,
    ReviewBriefRejectionAudit,
    ReviewBriefRunnerResult,
    ReviewBriefSafetyError,
    ReviewBriefSource,
    ReviewBriefSourceMode,
    ReviewBriefTopic,
    ReviewBriefTrace,
    ReviewBriefTraceReceipt,
    ReviewerDiscipline,
    build_authorized_snapshot,
    professional_review_source,
    render_review_brief,
    required_review_brief_tools,
    ridgeway_review_source,
    runner_request,
    validate_rendered_review_brief,
    validate_runner_result,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_response_contracts import RPC_RESPONSE_DATA_CONTRACTS
from ecocorridor_core.rpc_worker import (
    ReviewBriefAcknowledgeParams,
    ReviewBriefFeedbackParams,
    ReviewBriefPrepareParams,
    RPCDispatcher,
    RPCFault,
    _ReviewBriefResolution,
)
from ecocorridor_core.telemetry import CloudPolicyError, EcoTelemetryProjector
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
PROJECTION = ROOT / "benchmarks" / "ecocorridor" / "ridgeway" / "ui_projection.json"
CREATED_AT = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _source():
    projection = json.loads(PROJECTION.read_text(encoding="utf-8"))
    portfolio_id = str(projection["portfolios"][0]["id"])
    return ridgeway_review_source(
        project_id="ridgeway-project",
        model_revision_id="ridgeway-revision",
        projection=projection,
        portfolio_id=portfolio_id,
        evidence_manifest_sha256="a" * 64,
    )


def _result(source, *, trace_suffix: str = "one") -> ReviewBriefRunnerResult:
    request = runner_request(source)
    return ReviewBriefRunnerResult(
        snapshot_sha256=request.snapshot.snapshot_sha256,
        selections=[
            ReviewBriefFindingSelection(
                finding_alias=finding.finding_alias,
                action_kind=finding.permitted_actions[0],
                reviewer_disciplines=[finding.permitted_disciplines[0]],
            )
            for finding in request.snapshot.findings
        ],
        provider_call_id_sha256=("b" if trace_suffix == "one" else "c") * 64,
        provider_call_identity_kind="PROVIDER_RESPONSE_ID",
        provider_model="gpt-review-test",
        tool_calls=list(required_review_brief_tools(request.snapshot)),
        trace=ReviewBriefTrace(
            trace_id=f"trace_{trace_suffix}",
            weave_call_id=f"weave_call_{trace_suffix}",
            association_id=f"association_{trace_suffix}",
            wandb_run_id=f"run_{trace_suffix}",
            flushed=True,
            readback_verified=True,
            trace_url=f"https://wandb.ai/example/ecocorridor/weave/traces/{trace_suffix}",
        ),
    )


def _store(tmp_path: Path) -> EcoProjectStore:
    store = EcoProjectStore(tmp_path / "project")
    store.initialize(
        project_id="ridgeway-project",
        title="Ridgeway Crossing",
        mode="GUIDED",
        skeleton={"fixture": "test"},
    )
    return store


def _professional_source() -> ReviewBriefSource:
    source = _source()
    return ReviewBriefSource(
        source_mode=ReviewBriefSourceMode.PROFESSIONAL,
        project_id=source.project_id,
        project_title=source.project_title,
        analysis_id=source.analysis_id,
        portfolio_id=source.portfolio_id,
        model_revision_id=source.model_revision_id,
        decision_board_sha256=source.decision_board_sha256,
        scenario_ids=source.scenario_ids,
        disposition=source.disposition,
        next_action=source.next_action,
        controlling_scenario_id=source.controlling_scenario_id,
        safeguard_status=source.safeguard_status,
        review_mask_triggered=source.review_mask_triggered,
        authorization_sha256=source.authorization_sha256,
        evidence_authorization_decision_id=source.evidence_authorization_decision_id,
        current_authorizations_sha256=source.current_authorizations_sha256,
        findings=source.findings,
    )


def _professional_store(tmp_path: Path, source: ReviewBriefSource) -> EcoProjectStore:
    store = EcoProjectStore(tmp_path / "project")
    store.initialize(
        project_id=source.project_id,
        title="Professional connectivity project",
        mode="IMPORTED",
        skeleton={"fixture": "test"},
    )
    timestamp = CREATED_AT.isoformat()
    store._connection.execute(
        "INSERT INTO eco_analyses(analysis_id,project_id,analysis_kind,state,plan_json,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            source.analysis_id,
            source.project_id,
            "ALTERNATIVES",
            "READY",
            "{}",
            timestamp,
            timestamp,
        ),
    )
    return store


def test_authorized_snapshot_is_alias_only_and_bound_to_authorization() -> None:
    source = _source()
    snapshot = build_authorized_snapshot(source)
    serialized = snapshot.model_dump_json()

    assert isinstance(snapshot, AuthorizedReviewSnapshot)
    assert source.project_id not in serialized
    assert source.project_title not in serialized
    assert source.analysis_id not in serialized
    assert source.portfolio_id not in serialized
    assert source.model_revision_id not in serialized
    assert all(claim_id not in serialized for claim_id in source.evidence_claim_ids)
    assert all(scenario_id not in serialized for scenario_id in source.scenario_ids)
    assert snapshot.analysis_id_sha256
    assert snapshot.model_revision_id_sha256
    assert snapshot.evidence_authorization_decision_id.startswith("evidence_auth_")
    assert snapshot.current_authorizations_sha256 == "a" * 64
    mask_context = next(
        item
        for item in snapshot.findings
        if item.review_topic.value == "CURRENT_REDISTRIBUTION_MASK"
    )
    assert mask_context.reason_facets[0].value == "REVIEW_MASK_THRESHOLD_TRIGGERED"
    assert {item.evidence_class.value for item in mask_context.evidence_branches} == {
        "DIRECT_CURRENT_DIAGNOSTIC"
    }
    assert mask_context.scenario_breadth.value == "SCENARIO_SUBSET"
    assert mask_context.direct_run_count == 8


def test_runner_can_only_select_closed_authorized_values() -> None:
    source = _source()
    snapshot = build_authorized_snapshot(source)
    result = _result(source)
    validate_runner_result(result, snapshot)

    first = result.selections[0]
    unauthorized = result.model_copy(
        update={
            "selections": [
                first.model_copy(
                    update={"action_kind": ReviewBriefActionKind.RESOLVE_AUTHORIZATION}
                ),
                *result.selections[1:],
            ]
        }
    )
    with pytest.raises(ReviewBriefSafetyError, match="ACTION_INAPPROPRIATE"):
        validate_runner_result(unauthorized, snapshot)

    with pytest.raises(ValidationError):
        ReviewBriefFindingSelection.model_validate(
            {
                "finding_alias": "F1",
                "action_kind": "WRITE_RECOMMENDATION",
                "reviewer_disciplines": ["ECOLOGY"],
            }
        )


def test_ridgeway_source_groups_only_actionable_review_topics() -> None:
    source = _source()
    by_topic = {item.review_topic.value: item for item in source.findings}

    assert set(by_topic) == {
        "CURRENT_REDISTRIBUTION_MASK",
        "ECOLOGICAL_ASSUMPTION",
    }
    assert source.safeguard_status == "PASS"
    assert len(by_topic["CURRENT_REDISTRIBUTION_MASK"].scenario_ids) == 4
    assert len(by_topic["CURRENT_REDISTRIBUTION_MASK"].evidence_claim_ids) == 4
    assert all(
        claim_id.startswith(f"ECO-MAP-{source.portfolio_id}-")
        for claim_id in by_topic["CURRENT_REDISTRIBUTION_MASK"].evidence_claim_ids
    )
    assert len(by_topic["CURRENT_REDISTRIBUTION_MASK"].direct_run_ids) == 8
    assert all(
        run_id.startswith("map_")
        for run_id in by_topic["CURRENT_REDISTRIBUTION_MASK"].direct_run_ids
    )
    assert by_topic["ECOLOGICAL_ASSUMPTION"].evidence_claim_ids == ["ridgeway_assumption_ledger_v1"]
    assert by_topic["ECOLOGICAL_ASSUMPTION"].direct_run_ids == []
    assert source.model_fitness_evidence_ids == ("ridgeway_assumption_ledger_v1",)
    assert "ridgeway_assumption_ledger_v1" not in source.connectivity_claim_ids
    assert all(item.finding_kind.value != "MODEL_FITNESS" for item in source.findings)


def test_ridgeway_teaching_roles_add_only_their_observed_actionable_gaps() -> None:
    projection = json.loads(PROJECTION.read_text(encoding="utf-8"))
    by_role = {str(item["role"]): item for item in projection["portfolios"]}

    def source_for(role: str):
        return ridgeway_review_source(
            project_id="ridgeway-project",
            model_revision_id="ridgeway-revision",
            projection=projection,
            portfolio_id=str(by_role[role]["id"]),
            evidence_manifest_sha256="a" * 64,
        )

    caution = source_for("caution")
    carry = source_for("carry-forward")
    over_budget = source_for("over-budget")
    brittle = source_for("brittle")

    assert all(
        source.safeguard_status == "PASS" for source in (caution, carry, over_budget, brittle)
    )
    assert all(
        "SAFEGUARD_REVIEW" not in {item.review_topic.value for item in source.findings}
        for source in (caution, carry, over_budget, brittle)
    )
    assert "COST_BASIS" not in {item.review_topic.value for item in caution.findings}
    assert "COST_BASIS" not in {item.review_topic.value for item in carry.findings}
    assert "COST_BASIS" in {item.review_topic.value for item in over_budget.findings}
    assert "COST_BASIS" in {item.review_topic.value for item in brittle.findings}
    assert over_budget.decision_record_evidence_ids == (f"ECO-COST-{over_budget.portfolio_id}",)
    assert set(brittle.decision_record_evidence_ids) == {
        f"ECO-COST-{brittle.portfolio_id}",
        f"ECO-HYPOTHESIS-{brittle.portfolio_id}",
    }
    assumption = next(
        item for item in brittle.findings if item.review_topic.value == "ECOLOGICAL_ASSUMPTION"
    )
    assert set(assumption.evidence_claim_ids) == {
        "ridgeway_assumption_ledger_v1",
        f"ECO-HYPOTHESIS-{brittle.portfolio_id}",
    }
    assert len(assumption.scenario_ids) == 8
    assert all(
        claim_id.startswith("ECO-MAP-")
        for source in (caution, carry, over_budget, brittle)
        for finding in source.findings
        if finding.review_topic.value == "CURRENT_REDISTRIBUTION_MASK"
        for claim_id in finding.evidence_claim_ids
    )


def test_ridgeway_source_claim_boundary_checks_narrative_not_opaque_ids() -> None:
    projection = json.loads(PROJECTION.read_text(encoding="utf-8"))
    portfolio = projection["portfolios"][0]
    projection["title"] = "Unapproved portfolio review workspace"
    original_id = portfolio["id"]
    portfolio["id"] = f"unapproved-{original_id}"
    for scenario in portfolio["scenarioResults"]:
        scenario_id = scenario["scenarioId"]
        scenario["portfolioMapRunId"] = f"map_{portfolio['id']}_{scenario_id}"
        for pair in scenario["pairResults"]:
            pair["claimId"] = pair["claimId"].replace(original_id, portfolio["id"])
            pair["portfolioRunId"] = pair["portfolioRunId"].replace(original_id, portfolio["id"])
    source = ridgeway_review_source(
        project_id="unapproved-project-id",
        model_revision_id="unapproved-revision-id",
        projection=projection,
        portfolio_id=str(portfolio["id"]),
        evidence_manifest_sha256="a" * 64,
    )
    assert source.portfolio_id.startswith("unapproved-")
    assert source.project_title == "Unapproved portfolio review workspace"

    portfolio["nextAction"] = "This portfolio is approved."
    with pytest.raises(ReviewBriefSafetyError, match="SOURCE_PROHIBITED_CLAIM"):
        ridgeway_review_source(
            project_id="ridgeway-project",
            model_revision_id="ridgeway-revision",
            projection=projection,
            portfolio_id=str(portfolio["id"]),
            evidence_manifest_sha256="a" * 64,
        )


def test_professional_source_uses_passport_evidence_not_a_direct_metric_proxy() -> None:
    fixture = build_ridgeway_fixture()
    portfolio = fixture.portfolios[0]
    scenario_id = fixture.bundle.scenarios[0].scenario_id
    plan = ConnectivityRunPlan(
        analysis_id="analysis-fitness-evidence",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[scenario_id],
        portfolios=[portfolio],
        baseline_portfolio_id=portfolio.portfolio_id,
        direct_job_count=1,
        solver_job_cap=1,
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256="e" * 64,
    )
    claim = ConnectivityEvidenceClaim(
        claim_id="claim-normal-passing-metric",
        claim_kind="DIRECT_PAIR_METRIC",
        text="Direct Circuitscape result under documented assumptions.",
        value=4.0,
        units="percent",
        portfolio_id=portfolio.portfolio_id,
        scenario_id=scenario_id,
        pair_id="pair-one",
        direct_run_ids=["run-direct-one"],
        root_node_id="claim-normal-passing-metric",
        nodes=[
            EvidenceNode(
                node_id="claim-normal-passing-metric",
                node_type="CLAIM",
                label="Passing direct metric",
            ),
            EvidenceNode(
                node_id="run:run-direct-one",
                node_type="DIRECT_RUN",
                label="Direct run",
            ),
        ],
        edges=[
            EvidenceEdge(
                from_node_id="claim-normal-passing-metric",
                to_node_id="run:run-direct-one",
                relation="supported_by",
            )
        ],
        limitations=["Connectivity result only."],
    )
    summary = PortfolioDecisionSummary(
        portfolio_id=portfolio.portfolio_id,
        disposition="PASSES",
        cost=0,
        currency="USD",
        rule_dispositions=[],
        safeguard_passed=True,
        next_action="Continue qualified review.",
        direct_run_ids=["run-direct-one"],
    )
    source = professional_review_source(
        bundle=fixture.bundle,
        plan=plan,
        summary=summary,
        claims=[claim],
        active_model_revision_id=fixture.bundle.revision.revision_id,
        verified_direct_run_sha256={"run-direct-one": "f" * 64},
        authorization_sha256="a" * 64,
        evidence_authorization_decision_id="evidence_auth_" + "b" * 24,
        current_authorizations_sha256="c" * 64,
    )

    assert len(source.findings) == 1
    assert source.findings[0].review_topic.value == "ECOLOGICAL_ASSUMPTION"
    assert source.findings[0].evidence_claim_ids == ["ridgeway_assumption_ledger_v1"]
    assert source.findings[0].direct_run_ids == []
    assert "claim-normal-passing-metric" not in source.evidence_claim_ids


def test_rejection_audit_accepts_only_a_bounded_verified_runner_receipt() -> None:
    receipt = ReviewBriefRejectedRunnerReceipt(
        provider_call_id_sha256="b" * 64,
        provider_model="review-model-1",
        tool_calls=["review_brief_list_findings"],
        trace=ReviewBriefTrace(
            trace_id="trace_rejected",
            weave_call_id="weave_rejected",
            association_id="association_rejected",
            wandb_run_id="run_rejected",
            trace_url="https://wandb.ai/example/eco/weave/traces/rejected",
        ),
    )
    payload = {
        "call_id": "call-rejected",
        "project_id": "project-one",
        "analysis_id": "analysis-one",
        "portfolio_id": "portfolio-one",
        "error_code": "REVIEW_BRIEF_AGENT_OUTPUT_INVALID",
        "failure_stage": "OUTPUT_VALIDATION",
        "runner_invoked": True,
        "runner_receipt": receipt,
        "created_at": CREATED_AT,
    }
    audit = ReviewBriefRejectionAudit.model_validate(payload)
    assert audit.runner_receipt == receipt
    assert "selections" not in audit.model_dump_json()
    with pytest.raises(ValidationError, match="invoked runner"):
        ReviewBriefRejectionAudit.model_validate({**payload, "runner_invoked": False})


def test_deterministic_renderer_rejects_modified_prose() -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="call-one",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    changed_item = draft.items[0].model_copy(update={"rendered_text": "Approve this corridor."})
    changed = draft.model_copy(update={"items": [changed_item, *draft.items[1:]]})

    with pytest.raises(ReviewBriefSafetyError, match="RENDER_NOT_DETERMINISTIC"):
        validate_rendered_review_brief(changed, source=source, result=result)


def test_deterministic_renderer_identifies_hypothesis_sensitivity() -> None:
    projection = json.loads(PROJECTION.read_text(encoding="utf-8"))
    brittle = next(item for item in projection["portfolios"] if item["role"] == "brittle")
    source = ridgeway_review_source(
        project_id="ridgeway-project",
        model_revision_id="ridgeway-revision",
        projection=projection,
        portfolio_id=str(brittle["id"]),
        evidence_manifest_sha256="a" * 64,
    )

    draft = render_review_brief(
        call_id="call-hypothesis-sensitivity",
        source=source,
        result=_result(source),
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    assumption = next(
        item for item in draft.items if item.review_topic == ReviewBriefTopic.ECOLOGICAL_ASSUMPTION
    )

    assert "retained resistance hypotheses" in assumption.rendered_text
    assert "sensitivity affects feasibility review" in assumption.rendered_text


@pytest.mark.parametrize(
    ("topic", "reason", "action", "discipline", "expected_issue"),
    [
        (
            ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP,
            ReviewBriefReasonFacet.LAND_ACCESS_UNRESOLVED,
            ReviewBriefActionKind.RESOLVE_LAND_OR_OWNERSHIP,
            ReviewerDiscipline.LAND,
            "land-access or ownership",
        ),
        (
            ReviewBriefTopic.MAINTENANCE_RESPONSIBILITY,
            ReviewBriefReasonFacet.MAINTENANCE_RESPONSIBILITY_UNRESOLVED,
            ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
            ReviewerDiscipline.MAINTENANCE,
            "maintenance responsibility",
        ),
        (
            ReviewBriefTopic.CULTURAL_REVIEW,
            ReviewBriefReasonFacet.CULTURAL_REVIEW_UNRESOLVED,
            ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
            ReviewerDiscipline.CULTURAL,
            "cultural or Tribal review",
        ),
        (
            ReviewBriefTopic.PERMITTING_REVIEW,
            ReviewBriefReasonFacet.PERMITTING_REVIEW_UNRESOLVED,
            ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
            ReviewerDiscipline.PERMITTING,
            "permitting review",
        ),
        (
            ReviewBriefTopic.TRAFFIC_OR_STRUCTURAL_FEASIBILITY,
            ReviewBriefReasonFacet.TRAFFIC_OR_STRUCTURAL_REVIEW_UNRESOLVED,
            ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
            ReviewerDiscipline.ENGINEERING,
            "traffic and structural feasibility review",
        ),
    ],
)
def test_deterministic_renderer_names_the_exact_feasibility_issue(
    topic: ReviewBriefTopic,
    reason: ReviewBriefReasonFacet,
    action: ReviewBriefActionKind,
    discipline: ReviewerDiscipline,
    expected_issue: str,
) -> None:
    base = _source()
    finding = ReviewBriefLocalFinding(
        finding_id=f"finding-{topic.value.lower()}",
        finding_kind=ReviewBriefFindingKind.FEASIBILITY_GAP,
        review_topic=topic,
        reason_facets=[reason],
        priority=ReviewBriefPriority.BEFORE_FEASIBILITY,
        evidence_claim_ids=[f"evidence-{topic.value.lower()}"],
        permitted_actions=[action],
        permitted_disciplines=[discipline],
    )
    source = ReviewBriefSource(
        source_mode=base.source_mode,
        project_id=base.project_id,
        project_title=base.project_title,
        analysis_id=base.analysis_id,
        portfolio_id=base.portfolio_id,
        model_revision_id=base.model_revision_id,
        decision_board_sha256=base.decision_board_sha256,
        scenario_ids=base.scenario_ids,
        disposition=base.disposition,
        next_action=base.next_action,
        controlling_scenario_id=base.controlling_scenario_id,
        safeguard_status=base.safeguard_status,
        review_mask_triggered=base.review_mask_triggered,
        authorization_sha256=base.authorization_sha256,
        evidence_authorization_decision_id=base.evidence_authorization_decision_id,
        current_authorizations_sha256=base.current_authorizations_sha256,
        findings=[finding],
    )
    request = runner_request(source)
    result = ReviewBriefRunnerResult(
        snapshot_sha256=request.snapshot.snapshot_sha256,
        selections=[
            ReviewBriefFindingSelection(
                finding_alias=request.snapshot.findings[0].finding_alias,
                action_kind=action,
                reviewer_disciplines=[discipline],
            )
        ],
        provider_call_id_sha256="d" * 64,
        provider_call_identity_kind="PROVIDER_RESPONSE_ID",
        provider_model="gpt-review-test",
        tool_calls=list(required_review_brief_tools(request.snapshot)),
        trace=ReviewBriefTrace(
            trace_id=f"trace-{topic.value.lower()}",
            weave_call_id=f"weave-{topic.value.lower()}",
            association_id=f"association-{topic.value.lower()}",
            wandb_run_id=f"run-{topic.value.lower()}",
            flushed=True,
            readback_verified=True,
        ),
    )

    draft = render_review_brief(
        call_id=f"call-{topic.value.lower()}",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )

    assert draft.items[0].review_topic == topic
    assert expected_issue in draft.items[0].rendered_text


def test_public_contract_names_are_aliases_not_divergent_models() -> None:
    assert ReviewBriefAgentOutput is ReviewBriefRunnerResult
    assert ReviewBriefTraceReceipt is ReviewBriefTrace
    assert ReviewBriefAttestation is ReviewBriefAcknowledgement


def test_review_brief_store_is_immutable_idempotent_and_versioned(tmp_path: Path) -> None:
    source = _source()
    first_result = _result(source)
    first = render_review_brief(
        call_id="call-one",
        source=source,
        result=first_result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    store = _store(tmp_path)
    try:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 116
        assert (
            store.publish_review_brief_draft(
                call_id="call-one", draft=first, runner_result=first_result, source=source
            )
            == first
        )
        assert store.telemetry_rows(event_type="review_brief_draft") == []
        normalized_counts = {
            table: store._connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE draft_id=?", (first.draft_id,)
            ).fetchone()[0]
            for table in (
                "eco_review_brief_items",
                "eco_review_brief_finding_selections",
                "eco_review_brief_evidence_edges",
                "eco_review_brief_agent_executions",
                "eco_review_brief_validations",
                "eco_review_brief_trace_references",
            )
        }
        execution_id = store._connection.execute(
            "SELECT execution_id FROM eco_review_brief_agent_executions WHERE draft_id=?",
            (first.draft_id,),
        ).fetchone()[0]
        normalized_counts["eco_review_brief_tool_receipts"] = store._connection.execute(
            "SELECT COUNT(*) FROM eco_review_brief_tool_receipts WHERE execution_id=?",
            (execution_id,),
        ).fetchone()[0]
        assert normalized_counts == {
            "eco_review_brief_items": len(first.items),
            "eco_review_brief_finding_selections": len(first.items),
            "eco_review_brief_evidence_edges": sum(
                len(item.evidence_claim_ids) + len(item.direct_run_ids) + len(item.scenario_ids)
                for item in first.items
            ),
            "eco_review_brief_agent_executions": 1,
            "eco_review_brief_validations": 1,
            "eco_review_brief_trace_references": 1,
            "eco_review_brief_tool_receipts": len(first_result.tool_calls),
        }
        trace_record = store.assistant_answer_by_trace(first.trace.trace_id)
        assert trace_record is not None
        assert trace_record["answer"]["weave_call_id"] == first.trace.weave_call_id
        assert trace_record["answer"]["trace_url"] == first.trace.trace_url
        assert (
            store.publish_review_brief_draft(
                call_id="call-one", draft=first, runner_result=first_result, source=source
            )
            == first
        )
        assert store.telemetry_rows(event_type="review_brief_draft") == []
        assert store.review_brief_draft_record(first.draft_id)["draft"] == first

        second_result = _result(source, trace_suffix="two")
        second = render_review_brief(
            call_id="call-two",
            source=source,
            result=second_result,
            created_at=CREATED_AT.replace(minute=1),
            supersedes_draft_id=first.draft_id,
        )
        store.publish_review_brief_draft(
            call_id="call-two", draft=second, runner_result=second_result, source=source
        )
        assert (
            store.latest_review_brief_draft(
                analysis_id=source.analysis_id,
                portfolio_id=source.portfolio_id,
            )
            == second
        )
        assert [item.draft_id for item in store.list_review_brief_drafts()] == [
            second.draft_id,
            first.draft_id,
        ]

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._connection.execute(
                "UPDATE eco_review_brief_drafts SET payload_json='{}' WHERE draft_id=?",
                (first.draft_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._connection.execute(
                "UPDATE eco_review_brief_trace_references SET payload_json='{}' WHERE draft_id=?",
                (first.draft_id,),
            )
    finally:
        store.close()


def test_review_brief_store_rejects_tampered_evidence_and_run_lineage(tmp_path: Path) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="tampered-call",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    tampered_item = draft.items[0].model_copy(
        update={
            "evidence_claim_ids": ["claim-not-in-authoritative-source"],
            "direct_run_ids": ["run-not-in-authoritative-source"],
        }
    )
    tampered = draft.model_copy(update={"items": [tampered_item, *draft.items[1:]]})
    store = _store(tmp_path)
    try:
        with pytest.raises(ReviewBriefSafetyError, match="RENDER_NOT_DETERMINISTIC"):
            store.publish_review_brief_draft(
                call_id="tampered-call",
                draft=tampered,
                runner_result=result,
                source=source,
            )
        assert store.list_review_brief_drafts() == []
        assert store.telemetry_rows(event_type="review_brief_draft") == []
    finally:
        store.close()


def test_synthetic_review_brief_cannot_be_retagged_into_professional_sync(
    tmp_path: Path,
) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="source-mode-tamper",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    retagged = draft.model_copy(update={"source_mode": ReviewBriefSourceMode.PROFESSIONAL})
    store = _store(tmp_path)
    try:
        with pytest.raises(ReviewBriefSafetyError, match="RENDER_NOT_DETERMINISTIC"):
            store.publish_review_brief_draft(
                call_id="source-mode-tamper",
                draft=retagged,
                runner_result=result,
                source=source,
            )
        assert store.list_review_brief_drafts() == []
        assert store.telemetry_rows(event_type="review_brief_draft") == []
    finally:
        store.close()


def test_review_brief_draft_and_normalized_evidence_are_producer_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="atomic-call",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    store = _store(tmp_path)

    original_verify = EcoProjectStore._verify_review_brief_normalized

    def fail_after_normalized_insert(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected normalized-evidence failure")

    monkeypatch.setattr(
        EcoProjectStore,
        "_verify_review_brief_normalized",
        fail_after_normalized_insert,
    )
    try:
        with pytest.raises(RuntimeError, match="injected normalized-evidence failure"):
            store.publish_review_brief_draft(
                call_id="atomic-call",
                draft=draft,
                runner_result=result,
                source=source,
            )
        assert store.list_review_brief_drafts() == []
        assert store.telemetry_rows(event_type="review_brief_draft") == []
        for table in (
            "eco_review_brief_items",
            "eco_review_brief_finding_selections",
            "eco_review_brief_evidence_edges",
            "eco_review_brief_agent_executions",
            "eco_review_brief_tool_receipts",
            "eco_review_brief_validations",
            "eco_review_brief_trace_references",
        ):
            assert store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        monkeypatch.setattr(
            EcoProjectStore,
            "_verify_review_brief_normalized",
            original_verify,
        )
        store.close()


def test_professional_review_brief_outbox_and_local_evidence_are_producer_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _professional_source()
    result = _result(source)
    draft = render_review_brief(
        call_id="professional-atomic-call",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    store = _professional_store(tmp_path, source)

    def fail_outbox(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(EcoProjectStore, "_enqueue_outbox", staticmethod(fail_outbox))
    try:
        with pytest.raises(RuntimeError, match="injected outbox failure"):
            store.publish_review_brief_draft(
                call_id="professional-atomic-call",
                draft=draft,
                runner_result=result,
                source=source,
            )
        assert store.list_review_brief_drafts() == []
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM eco_review_brief_agent_executions"
            ).fetchone()[0]
            == 0
        )
        assert store.telemetry_rows(event_type="review_brief_draft") == []
    finally:
        store.close()


def test_professional_review_brief_outbox_projects_typed_wandb_evidence(
    tmp_path: Path,
) -> None:
    source = _professional_source()
    result = _result(source)
    draft = render_review_brief(
        call_id="projection-call",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    store = _professional_store(tmp_path, source)
    try:
        store.publish_review_brief_draft(
            call_id="projection-call", draft=draft, runner_result=result, source=source
        )
        row = store.telemetry_rows(event_type="review_brief_draft")[0]
        projection = EcoTelemetryProjector(
            store=store,
            project_root=store.root,
            sync_plan={"allowed": True, "blockers": [], "projections": []},
            profile=ECOCORRIDOR_APP_PROFILE,
            inventory_mode=True,
        )(row, row["payload"])

        assert projection.run_kind == "connectivity_analysis"
        assert projection.run_subject_id == source.analysis_id
        assert projection.event_type == "review_brief_draft"
        assert [table.logical_name for table in projection.tables] == ["review_brief_drafts"]
        assert projection.tables[0].rows[0][0] == draft.draft_id
        assert len(projection.artifacts) == 1
        assert projection.artifacts[0].artifact_type == "review_brief_draft"
        staged = store.root / projection.artifacts[0].source_relative_path
        retained = json.loads(staged.read_text(encoding="utf-8"))
        assert retained["draft"]["draft_id"] == draft.draft_id
        assert retained["runner_receipt"]["trace"]["weave_call_id"] == (draft.trace.weave_call_id)
    finally:
        store.close()


def test_synthetic_review_brief_never_enqueues_and_projector_rejects_forged_event(
    tmp_path: Path,
) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="synthetic-local-only",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    store = _store(tmp_path)
    try:
        store.publish_review_brief_draft(
            call_id="synthetic-local-only",
            draft=draft,
            runner_result=result,
            source=source,
        )
        assert store.telemetry_rows(event_type="review_brief_draft") == []
        forged_row = {
            "event_type": "review_brief_draft",
            "project_id": source.project_id,
            "subject_id": draft.draft_id,
        }
        forged_payload = {
            "schema_version": "ecocorridor-review-brief-draft-event/v1.0",
            "draft_id": draft.draft_id,
            "source_mode": "RIDGEWAY_SYNTHETIC",
            "analysis_id": draft.analysis_id,
            "portfolio_id": draft.portfolio_id,
            "model_revision_id": draft.model_revision_id,
            "decision_board_sha256": draft.decision_board_sha256,
            "draft_sha256": "0" * 64,
            "runner_receipt_sha256": "0" * 64,
            "trace_receipt_sha256": "0" * 64,
            "item_count": len(draft.items),
            "evidence_claim_set_sha256": "0" * 64,
            "created_at": draft.created_at.isoformat(),
        }
        projector = EcoTelemetryProjector(
            store=store,
            project_root=store.root,
            sync_plan={"allowed": True, "blockers": [], "projections": []},
            profile=ECOCORRIDOR_APP_PROFILE,
            inventory_mode=True,
        )
        with pytest.raises(CloudPolicyError, match="SYNTHETIC_REVIEW_BRIEF_SYNC_FORBIDDEN"):
            projector(forged_row, forged_payload)
    finally:
        store.close()


def test_review_brief_normalized_checksum_and_foreign_keys_are_enforced(
    tmp_path: Path,
) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="normalized-integrity",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    store = _store(tmp_path)
    try:
        store.publish_review_brief_draft(
            call_id="normalized-integrity",
            draft=draft,
            runner_result=result,
            source=source,
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store._connection.execute(
                "INSERT INTO eco_review_brief_validations(draft_id,project_id,payload_json,"
                "payload_sha256,created_at) VALUES(?,?,?,?,?)",
                ("missing-draft", source.project_id, "{}", "0" * 64, CREATED_AT.isoformat()),
            )

        store._connection.execute("DROP TRIGGER eco_review_brief_validation_immutable_update")
        store._connection.execute(
            "UPDATE eco_review_brief_validations SET payload_json='{}' WHERE draft_id=?",
            (draft.draft_id,),
        )
        with pytest.raises(ValueError, match="normalized validations failed integrity"):
            store.review_brief_draft_record(draft.draft_id)
        with pytest.raises(ValueError, match="normalized validations failed integrity"):
            store.review_brief_call_record("normalized-integrity")

        acknowledgement = ReviewBriefAcknowledgement(
            acknowledgement_id="normalized-integrity-ack",
            draft_id=draft.draft_id,
            reviewer="Qualified ecological reviewer",
            statement=REVIEW_BRIEF_ACKNOWLEDGEMENT,
            acknowledged_at=CREATED_AT,
        )
        with pytest.raises(ValueError, match="normalized validations failed integrity"):
            store.attest_review_brief(
                acknowledgement=acknowledgement,
                current_model_revision_id=draft.model_revision_id,
                current_decision_board_sha256=draft.decision_board_sha256,
            )

        feedback = ReviewBriefFeedbackRecord(
            feedback_id="normalized-integrity-feedback",
            draft_id=draft.draft_id,
            rating="HELPFUL",
            comment="",
            recorded_at=CREATED_AT,
            trace_receipt=ReviewBriefFeedbackTraceReceipt(
                weave_feedback_id="normalized-integrity-weave-feedback",
                trace_id=draft.trace.trace_id,
                weave_call_id=draft.trace.weave_call_id,
                wandb_run_id=draft.trace.wandb_run_id,
            ),
        )
        with pytest.raises(ValueError, match="normalized validations failed integrity"):
            store.record_review_brief_feedback(feedback)
    finally:
        store.close()


def test_schema_115_review_brief_blobs_backfill_normalized_evidence(tmp_path: Path) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="schema-115-backfill",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    project = tmp_path / "project"
    store = _store(tmp_path)
    store.publish_review_brief_draft(
        call_id="schema-115-backfill",
        draft=draft,
        runner_result=result,
        source=source,
    )
    store.close()

    connection = sqlite3.connect(project / "project.sqlite", isolation_level=None)
    try:
        for trigger in (
            "eco_review_brief_item_immutable_delete",
            "eco_review_brief_selection_immutable_delete",
            "eco_review_brief_evidence_edge_immutable_delete",
            "eco_review_brief_execution_immutable_delete",
            "eco_review_brief_tool_receipt_immutable_delete",
            "eco_review_brief_validation_immutable_delete",
            "eco_review_brief_trace_reference_immutable_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        for table in (
            "eco_review_brief_tool_receipts",
            "eco_review_brief_trace_references",
            "eco_review_brief_validations",
            "eco_review_brief_evidence_edges",
            "eco_review_brief_finding_selections",
            "eco_review_brief_items",
            "eco_review_brief_agent_executions",
        ):
            connection.execute(f"DELETE FROM {table}")
        connection.execute("PRAGMA user_version=115")
    finally:
        connection.close()

    migrated = EcoProjectStore(project)
    try:
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == 116
        assert migrated.review_brief_draft_record(draft.draft_id)["draft"] == draft
        assert (
            migrated._connection.execute(
                "SELECT COUNT(*) FROM eco_review_brief_agent_executions WHERE draft_id=?",
                (draft.draft_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        migrated.close()


def test_rejection_is_checksum_verified_and_excludes_a_draft(tmp_path: Path) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="rejected-call",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    store = _store(tmp_path)
    try:
        audit = ReviewBriefRejectionAudit(
            call_id="rejected-call",
            project_id="ridgeway-project",
            analysis_id=source.analysis_id,
            portfolio_id=source.portfolio_id,
            error_code="REVIEW_BRIEF_TRACE_NOT_VERIFIED",
            decision_board_sha256=source.decision_board_sha256,
            runner_invoked=True,
            created_at=CREATED_AT,
        )
        recorded = store.record_review_brief_rejection(audit)
        assert recorded.local_audit_verified is True
        assert recorded.audit_sha256
        assert store.review_brief_call_record("rejected-call")["disposition"] == "REJECTED"
        with pytest.raises(ValueError, match="rejected"):
            store.publish_review_brief_draft(
                call_id="rejected-call",
                draft=draft,
                runner_result=result,
                source=source,
            )
        assert store.list_review_brief_drafts() == []
        assert store.telemetry_rows(event_type="review_brief_draft") == []
    finally:
        store.close()


def test_acknowledgement_is_non_approval_and_stale_safe(tmp_path: Path) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="call-one",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    store = _store(tmp_path)
    try:
        store.publish_review_brief_draft(
            call_id="call-one",
            draft=draft,
            runner_result=result,
            source=source,
        )
        acknowledgement = ReviewBriefAcknowledgement(
            acknowledgement_id="ack-one",
            draft_id=draft.draft_id,
            reviewer="Qualified ecological reviewer",
            statement=REVIEW_BRIEF_ACKNOWLEDGEMENT,
            acknowledged_at=CREATED_AT,
        )
        with pytest.raises(ValueError, match="stale"):
            store.attest_review_brief(
                acknowledgement=acknowledgement,
                current_model_revision_id="changed-revision",
                current_decision_board_sha256=draft.decision_board_sha256,
            )
        recorded = store.attest_review_brief(
            acknowledgement=acknowledgement,
            current_model_revision_id=draft.model_revision_id,
            current_decision_board_sha256=draft.decision_board_sha256,
        )
        assert recorded.model_unchanged is True
        assert recorded.portfolio_selected is False
        retry = acknowledgement.model_copy(
            update={"acknowledged_at": CREATED_AT + timedelta(minutes=5)}
        )
        assert (
            store.attest_review_brief(
                acknowledgement=retry,
                current_model_revision_id=draft.model_revision_id,
                current_decision_board_sha256=draft.decision_board_sha256,
            )
            == acknowledgement
        )
        assert store.review_brief_acknowledgement(draft.draft_id) == acknowledgement

        feedback = ReviewBriefFeedbackRecord(
            feedback_id="feedback-one",
            draft_id=draft.draft_id,
            rating="HELPFUL",
            comment="",
            recorded_at=CREATED_AT,
            trace_receipt=ReviewBriefFeedbackTraceReceipt(
                weave_feedback_id="feedback-weave-one",
                trace_id=draft.trace.trace_id,
                weave_call_id=draft.trace.weave_call_id,
                wandb_run_id=draft.trace.wandb_run_id,
            ),
        )
        assert store.record_review_brief_feedback(feedback) == feedback
        mismatched = feedback.model_copy(
            update={
                "feedback_id": "feedback-mismatched",
                "trace_receipt": feedback.trace_receipt.model_copy(
                    update={"trace_id": "trace-not-the-draft"}
                ),
            }
        )
        with pytest.raises(ValueError, match="differs from the draft trace"):
            store.record_review_brief_feedback(mismatched)
    finally:
        store.close()


def test_acknowledgement_rpc_retry_returns_authoritative_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="ack-rpc-source",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    project = tmp_path / "project"
    store = _store(tmp_path)
    store.publish_review_brief_draft(
        call_id="ack-rpc-source",
        draft=draft,
        runner_result=result,
        source=source,
    )
    store.close()

    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app",
        allowed_roots=[tmp_path],
        review_brief_runner=object(),  # type: ignore[arg-type]
    )
    resolution_calls = 0

    def resolve_once(opened_store, *, analysis_id, portfolio_id):
        nonlocal resolution_calls
        del opened_store, analysis_id, portfolio_id
        resolution_calls += 1
        if resolution_calls > 1:
            raise AssertionError("an acknowledgement retry must use retained readback")
        return _ReviewBriefResolution(source=source)

    monkeypatch.setattr(
        dispatcher,
        "_resolve_review_brief_source",
        resolve_once,
    )
    params = ReviewBriefAcknowledgeParams(
        project_directory=str(project),
        draft_id=draft.draft_id,
        acknowledgement_id="ack-rpc-idempotent",
        reviewer="Qualified ecological reviewer",
        acknowledgement=REVIEW_BRIEF_ACKNOWLEDGEMENT,
    )
    first = dispatcher._review_brief_acknowledge(params).data.acknowledgement
    second = dispatcher._review_brief_acknowledge(params).data.acknowledgement
    assert second == first
    assert resolution_calls == 1

    reopened = EcoProjectStore(project)
    try:
        assert reopened.review_brief_acknowledgement(draft.draft_id) == first
        assert (
            reopened._connection.execute(
                "SELECT COUNT(*) FROM eco_review_brief_attestations WHERE draft_id=?",
                (draft.draft_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        reopened.close()


def test_review_brief_feedback_requires_verified_weave_receipt_before_local_write(
    tmp_path: Path,
) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="feedback-source-call",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    project = tmp_path / "project"
    store = _store(tmp_path)
    store.publish_review_brief_draft(
        call_id="feedback-source-call",
        draft=draft,
        runner_result=result,
        source=source,
    )
    store.close()

    runtime_calls: list[dict[str, object]] = []

    class FeedbackRuntime:
        def record_review_brief_feedback(self, **kwargs):
            runtime_calls.append(kwargs)
            assert kwargs["project_root"] == project.resolve()
            assert kwargs["draft"] == draft
            return ReviewBriefFeedbackTraceReceipt(
                weave_feedback_id="weave-feedback-verified",
                trace_id=draft.trace.trace_id,
                weave_call_id=draft.trace.weave_call_id,
                wandb_run_id=draft.trace.wandb_run_id,
            )

    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app",
        allowed_roots=[tmp_path],
        assistant_runtime=FeedbackRuntime(),  # type: ignore[arg-type]
    )
    params = ReviewBriefFeedbackParams(
        project_directory=str(project),
        draft_id=draft.draft_id,
        feedback_id="feedback-verified",
        rating="HELPFUL",
        comment="",
    )
    response = dispatcher._review_brief_feedback(params)
    assert response.data.feedback.trace_receipt.verified is True
    repeated = dispatcher._review_brief_feedback(params)
    assert repeated.data.feedback == response.data.feedback
    assert len(runtime_calls) == 1
    reopened = EcoProjectStore(project)
    try:
        row = reopened._connection.execute(
            "SELECT payload_json FROM eco_review_brief_feedback WHERE feedback_id=?",
            ("feedback-verified",),
        ).fetchone()
        assert row is not None
        assert json.loads(row["payload_json"])["trace_receipt"]["verified"] is True
    finally:
        reopened.close()


def test_review_brief_feedback_trace_failure_saves_no_local_record(tmp_path: Path) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="feedback-failure-source",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    project = tmp_path / "project"
    store = _store(tmp_path)
    store.publish_review_brief_draft(
        call_id="feedback-failure-source",
        draft=draft,
        runner_result=result,
        source=source,
    )
    store.close()

    class FailingFeedbackRuntime:
        def record_review_brief_feedback(self, **kwargs):
            del kwargs
            raise RuntimeError("NATIVE_AGENT_BRIDGE_FEEDBACK_READBACK_INVALID")

    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app",
        allowed_roots=[tmp_path],
        assistant_runtime=FailingFeedbackRuntime(),  # type: ignore[arg-type]
    )
    with pytest.raises(RPCFault):
        dispatcher._review_brief_feedback(
            ReviewBriefFeedbackParams(
                project_directory=str(project),
                draft_id=draft.draft_id,
                feedback_id="feedback-must-not-persist",
                rating="NOT_HELPFUL",
                comment="",
            )
        )
    reopened = EcoProjectStore(project)
    try:
        count = reopened._connection.execute(
            "SELECT COUNT(*) FROM eco_review_brief_feedback"
        ).fetchone()[0]
        assert count == 0
    finally:
        reopened.close()


def test_current_source_drift_marks_retained_draft_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="stale-call",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    store = _store(tmp_path)
    dispatcher = RPCDispatcher(data_root=tmp_path / "app", review_brief_runner=object())  # type: ignore[arg-type]
    try:
        store.publish_review_brief_draft(
            call_id="stale-call",
            draft=draft,
            runner_result=result,
            source=source,
        )
        source.decision_board_sha256 = "f" * 64
        monkeypatch.setattr(
            dispatcher,
            "_resolve_review_brief_source",
            lambda opened_store, *, analysis_id, portfolio_id: _ReviewBriefResolution(
                source=source
            ),
        )
        stale = dispatcher._review_brief_current_draft(store, draft)
        assert stale.status == "STALE"
        assert stale.draft_id == draft.draft_id
        assert stale.decision_board_sha256 == draft.decision_board_sha256
    finally:
        store.close()


def test_prepare_idempotency_revalidates_and_returns_stale_after_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    result = _result(source)
    draft = render_review_brief(
        call_id="retry-after-drift",
        source=source,
        result=result,
        created_at=CREATED_AT,
        supersedes_draft_id=None,
    )
    project = tmp_path / "project"
    store = _store(tmp_path)
    store.publish_review_brief_draft(
        call_id="retry-after-drift",
        draft=draft,
        runner_result=result,
        source=source,
    )
    store.close()
    source.decision_board_sha256 = "f" * 64
    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app",
        allowed_roots=[tmp_path],
        review_brief_runner=object(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        dispatcher,
        "_resolve_review_brief_source",
        lambda opened_store, *, analysis_id, portfolio_id: _ReviewBriefResolution(source=source),
    )
    response = dispatcher._review_brief_prepare(
        ReviewBriefPrepareParams(
            project_directory=str(project),
            call_id="retry-after-drift",
            analysis_id=draft.analysis_id,
            portfolio_id=draft.portfolio_id,
            supersedes_draft_id=None,
            experimental_agent_acknowledged=True,
        )
    )
    assert response.data.draft.status == "STALE"


def test_rpc_contracts_and_handlers_are_registered(tmp_path: Path) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path / "app", review_brief_runner=object())  # type: ignore[arg-type]
    expected = {
        "assistant.review_brief.preflight",
        "assistant.review_brief.prepare",
        "assistant.review_brief.get",
        "assistant.review_brief.list",
        "assistant.review_brief.acknowledge",
        "assistant.review_brief.feedback",
    }
    assert expected.issubset(dispatcher.methods)
    assert expected.issubset(RPC_RESPONSE_DATA_CONTRACTS)


def test_guided_preflight_and_rail_resolve_every_typed_review_record(
    tmp_path: Path,
) -> None:
    dispatcher = RPCDispatcher(data_root=tmp_path)
    opened = dispatcher.dispatch("connectivity.guided.open", {})["data"]
    project = tmp_path / "guided" / "ridgeway"
    projection = opened["projection"]
    over_budget = next(
        item
        for item in projection["portfolios"]
        if item["costUsd"] > projection["decisionBrief"]["budgetUsd"]
    )
    brittle = next(
        item
        for item in projection["portfolios"]
        if item["projectionKey"] == "single-hypothesis-collapse"
    )

    for portfolio in (over_budget, brittle):
        response = dispatcher.dispatch(
            "assistant.review_brief.preflight",
            {
                "project_directory": str(project),
                "analysis_id": "ridgeway-verified-replay-v1.2",
                "portfolio_id": portfolio["id"],
            },
        )["data"]
        assert response["model_revision_id"] != "unavailable"
        assert response["evidence_claim_count"] > 0
        assert response["blocked_reason"] == (
            "Configure an approved provider credential for this project."
        )

    for evidence_id, expected_units in (
        (f"ECO-COST-{over_budget['id']}", "USD"),
        (f"ECO-HYPOTHESIS-{brittle['id']}", "rank_positions"),
    ):
        resolved = dispatcher.dispatch(
            "connectivity.evidence.resolve",
            {"project_directory": str(project), "claim_id": evidence_id},
        )["data"]
        assert resolved["claim"]["claim_kind"] == "GUIDED_DECISION_RECORD"
        assert resolved["claim"]["units"] == expected_units
        assert resolved["claim"]["direct_run_ids"] == []
        assert len(resolved["claim"]["nodes"]) == 2

    fitness = dispatcher.dispatch(
        "connectivity.evidence.resolve",
        {
            "project_directory": str(project),
            "claim_id": "ridgeway_assumption_ledger_v1",
        },
    )["data"]
    assert fitness["claim"]["claim_kind"] == "MODEL_FITNESS_RECORD"
    assert fitness["claim"]["direct_run_ids"] == []


def test_failed_runner_capability_preflight_persists_rejection_without_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCapabilityRunner:
        provider_model = "gpt-review-test"

        def preflight(self, *, request):
            del request
            return SimpleNamespace(
                tool_calling_verified=True,
                strict_structured_output_verified=False,
                trace_readback_verified=True,
            )

        def prepare(self, *, request):  # pragma: no cover - safety assertion
            del request
            raise AssertionError("prepare must not run after failed capability preflight")

    source = _source()
    project = tmp_path / "project"
    store = EcoProjectStore(project)
    store.initialize(
        project_id=source.project_id,
        title=source.project_title,
        mode="GUIDED",
        skeleton={},
    )
    store.close()
    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app",
        allowed_roots=[tmp_path],
        review_brief_runner=FailingCapabilityRunner(),
    )
    monkeypatch.setattr(
        dispatcher,
        "_resolve_review_brief_source",
        lambda store, *, analysis_id, portfolio_id: _ReviewBriefResolution(source=source),
    )

    with pytest.raises(RPCFault) as rejected:
        dispatcher._review_brief_prepare(
            ReviewBriefPrepareParams(
                project_directory=str(project),
                call_id="capability-call",
                analysis_id=source.analysis_id,
                portfolio_id=source.portfolio_id,
                supersedes_draft_id=None,
                experimental_agent_acknowledged=True,
            )
        )
    assert rejected.value.error.code == "REVIEW_BRIEF_RUNNER_CAPABILITY_PREFLIGHT_FAILED"

    reopened = EcoProjectStore(project)
    try:
        record = reopened.review_brief_call_record("capability-call")
        assert record is not None and record["disposition"] == "REJECTED"
        assert record["audit"].runner_invoked is True
        assert reopened.list_review_brief_drafts() == []
    finally:
        reopened.close()


def test_native_agent_failure_keeps_typed_rpc_remediation_and_rejection_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TraceReadbackFailureRunner:
        provider_model = "gpt-review-test"

        def preflight(self, *, request):
            del request
            return SimpleNamespace(
                tool_calling_verified=True,
                strict_structured_output_verified=True,
                trace_readback_verified=True,
            )

        def prepare(self, *, request):
            del request
            raise RuntimeError("REVIEW_BRIEF_AGENT_TRACE_READBACK_FAILED")

    source = _source()
    project = tmp_path / "project"
    store = EcoProjectStore(project)
    store.initialize(
        project_id=source.project_id,
        title=source.project_title,
        mode="GUIDED",
        skeleton={},
    )
    store.close()
    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app",
        allowed_roots=[tmp_path],
        review_brief_runner=TraceReadbackFailureRunner(),
    )
    monkeypatch.setattr(
        dispatcher,
        "_resolve_review_brief_source",
        lambda store, *, analysis_id, portfolio_id: _ReviewBriefResolution(source=source),
    )

    with pytest.raises(RPCFault) as rejected:
        dispatcher._review_brief_prepare(
            ReviewBriefPrepareParams(
                project_directory=str(project),
                call_id="native-trace-readback-failure",
                analysis_id=source.analysis_id,
                portfolio_id=source.portfolio_id,
                supersedes_draft_id=None,
                experimental_agent_acknowledged=True,
            )
        )
    assert rejected.value.error.code == "REVIEW_BRIEF_AGENT_TRACE_READBACK_FAILED"
    assert rejected.value.error.category.value == "DATA_PROVIDER"
    assert "Weave connection" in (rejected.value.error.remediation or "")

    reopened = EcoProjectStore(project)
    try:
        record = reopened.review_brief_call_record("native-trace-readback-failure")
        assert record is not None and record["disposition"] == "REJECTED"
        assert record["audit"].error_code == "REVIEW_BRIEF_AGENT_TRACE_READBACK_FAILED"
        assert reopened.list_review_brief_drafts() == []
    finally:
        reopened.close()


def test_experimental_agent_requires_explicit_opt_in_before_runner_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverCalledRunner:
        provider_model = "gpt-review-test"

        def prepare(self, *, request):  # pragma: no cover - safety assertion
            del request
            raise AssertionError("experimental agent ran without explicit acknowledgement")

    source = _source()
    project = tmp_path / "project"
    store = EcoProjectStore(project)
    store.initialize(
        project_id=source.project_id,
        title=source.project_title,
        mode="GUIDED",
        skeleton={},
    )
    store.close()
    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app",
        allowed_roots=[tmp_path],
        review_brief_runner=NeverCalledRunner(),
    )
    monkeypatch.setattr(
        dispatcher,
        "_resolve_review_brief_source",
        lambda store, *, analysis_id, portfolio_id: _ReviewBriefResolution(source=source),
    )

    with pytest.raises(RPCFault) as rejected:
        dispatcher._review_brief_prepare(
            ReviewBriefPrepareParams(
                project_directory=str(project),
                call_id="experimental-without-opt-in",
                analysis_id=source.analysis_id,
                portfolio_id=source.portfolio_id,
                supersedes_draft_id=None,
            )
        )
    assert rejected.value.error.code == ("REVIEW_BRIEF_EXPERIMENTAL_ACKNOWLEDGEMENT_REQUIRED")


def test_source_drift_rejection_keeps_only_the_verified_runner_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    changed = _source()
    changed.decision_board_sha256 = "f" * 64
    result = _result(source, trace_suffix="drift")

    class TracedRunner:
        provider_model = "gpt-review-test"

        def preflight(self, *, request):
            del request
            return SimpleNamespace(
                tool_calling_verified=True,
                strict_structured_output_verified=True,
                trace_readback_verified=True,
            )

        def prepare(self, *, request):
            del request
            return result

    project = tmp_path / "project"
    store = EcoProjectStore(project)
    store.initialize(
        project_id=source.project_id,
        title=source.project_title,
        mode="GUIDED",
        skeleton={},
    )
    store.close()
    dispatcher = RPCDispatcher(
        data_root=tmp_path / "app",
        allowed_roots=[tmp_path],
        review_brief_runner=TracedRunner(),
    )
    resolutions = iter(
        (_ReviewBriefResolution(source=source), _ReviewBriefResolution(source=changed))
    )
    monkeypatch.setattr(
        dispatcher,
        "_resolve_review_brief_source",
        lambda store, *, analysis_id, portfolio_id: next(resolutions),
    )

    with pytest.raises(RPCFault) as rejected:
        dispatcher._review_brief_prepare(
            ReviewBriefPrepareParams(
                project_directory=str(project),
                call_id="source-drift-after-trace",
                analysis_id=source.analysis_id,
                portfolio_id=source.portfolio_id,
                supersedes_draft_id=None,
                experimental_agent_acknowledged=True,
            )
        )
    assert rejected.value.error.code == "REVIEW_BRIEF_SOURCE_CHANGED_DURING_PREPARE"

    reopened = EcoProjectStore(project)
    try:
        record = reopened.review_brief_call_record("source-drift-after-trace")
        assert record is not None and record["disposition"] == "REJECTED"
        assert record["audit"].failure_stage.value == "AUTHORIZATION"
        assert record["audit"].runner_receipt is not None
        assert record["audit"].runner_receipt.provider_call_id_sha256 == (
            result.provider_call_id_sha256
        )
        assert reopened.list_review_brief_drafts() == []
    finally:
        reopened.close()
