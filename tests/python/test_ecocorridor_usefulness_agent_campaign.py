from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.usefulness_agent_campaign import (
    ADAPTIVE_ARM_ID,
    DETERMINISTIC_ARM_ID,
    FORCED_EXHAUSTIVE_ARM_ID,
    FULL_CONTEXT_ARM_ID,
    ORACLE_ARM_ID,
    PROVIDER_ARM_IDS,
    AgentComplexityScoringTruth,
    AgentModelPreflight,
    CampaignStopReason,
    FindingEvidenceAliasPair,
    InvocationDisposition,
    InvocationTraceReceipt,
    ModelSlot,
    PromotionDecision,
    agent_complexity_invocation_id,
    build_agent_complexity_campaign_plan,
    create_fair_input_receipt,
    evaluate_model_promotion,
    expected_execution_keys,
    seal_invocation_record,
    summarize_completed_agent_complexity_campaign,
    validate_invocation_record_truth,
)
from ecocorridor_core.usefulness_study import (
    REQUIRED_FROZEN_INPUTS,
    AgentRetrievalObservation,
    RetrievalStopReason,
    agent_complexity_task_matrix,
    build_default_synthetic_protocol,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
MODEL_IDS = (
    "openai/gpt-oss-120b",
    "gateway/frontier-tool-model",
    "gateway/efficient-tool-model",
)


def _tiny_truth(task_id: str = "task-1") -> AgentComplexityScoringTruth:
    return AgentComplexityScoringTruth.create(
        task_id=task_id,
        adjudicated_outcome_sha256=canonical_sha256({"task": task_id, "truth": "frozen"}),
        available_finding_aliases=("F1", "F2"),
        required_finding_aliases=("F1",),
        critical_required_finding_aliases=("F1",),
        relevant_evidence_aliases=("E1",),
        irrelevant_evidence_aliases=("E2",),
        supporting_evidence_pairs=(("F1", "E1"),),
        expected_fail_closed=False,
    )


def _protocol():
    return build_default_synthetic_protocol(
        source_commit="a" * 40,
        model_ids=MODEL_IDS,
        frozen_input_sha256s={
            name: canonical_sha256({"frozen": name}) for name in REQUIRED_FROZEN_INPUTS
        },
        preregistered_at=NOW,
        frozen_at=NOW + timedelta(minutes=1),
    )


def _plan():
    protocol = _protocol()
    provider_envelope = {
        "type": "object",
        "required": ["authorized_alias_catalog", "task"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "required": ["selected_grounding_pairs", "stop"],
        "additionalProperties": False,
    }
    preflights = tuple(
        AgentModelPreflight.create(
            slot=slot,
            model_id=model_id,
            provider_visible_evidence_contract_sha256=canonical_sha256(provider_envelope),
            output_schema_sha256=canonical_sha256(output_schema),
            prompt_sha256=canonical_sha256({"prompt": "v1"}),
            tool_contract_sha256=canonical_sha256({"tools": "v1"}),
            validator_sha256=canonical_sha256({"validator": "v1"}),
            cost_basis_sha256=canonical_sha256({"rates": "frozen-v1"}),
            wandb_run_id=f"preflight-{slot.value.lower()}",
            weave_trace_id=f"trace-preflight-{slot.value.lower()}",
            weave_trace_readback_sha256=canonical_sha256(
                {"trace": slot.value, "readback": True}
            ),
            completed_at=NOW,
        )
        for slot, model_id in zip(ModelSlot, MODEL_IDS, strict=True)
    )
    receipts = []
    scoring_truths = []
    for row in agent_complexity_task_matrix(protocol):
        finding_count = int(row["finding_count"])
        available_findings = tuple(f"F{index}" for index in range(1, finding_count + 1))
        required_findings = available_findings[: min(5, finding_count)]
        relevant_evidence = tuple(f"E{index}" for index in range(1, 6))
        irrelevant_evidence = tuple(f"I{index}" for index in range(1, 11))
        truth = AgentComplexityScoringTruth.create(
            task_id=row["task_id"],
            adjudicated_outcome_sha256=canonical_sha256(
                {"task": row["task_id"], "adjudication": "frozen"}
            ),
            available_finding_aliases=available_findings,
            required_finding_aliases=required_findings,
            critical_required_finding_aliases=required_findings[:2],
            relevant_evidence_aliases=relevant_evidence,
            irrelevant_evidence_aliases=irrelevant_evidence,
            supporting_evidence_pairs=tuple(
                zip(required_findings, relevant_evidence, strict=True)
            ),
            expected_fail_closed=False,
        )
        scoring_truths.append(truth)
        evidence = {
            "task_id": row["task_id"],
            "authorized_items": [
                {"alias": alias}
                for alias in (*available_findings, *relevant_evidence, *irrelevant_evidence)
            ],
            "limitations": ["connectivity screening only"],
        }
        receipts.append(
            create_fair_input_receipt(
                task_id=row["task_id"],
                evidence_payloads_by_arm={arm: evidence for arm in PROVIDER_ARM_IDS},
                output_schema_payloads_by_arm={arm: output_schema for arm in PROVIDER_ARM_IDS},
                provider_envelope_contract=provider_envelope,
                prompt_payloads_by_arm={
                    arm: {"instruction": "select evidence safely", "method": arm}
                    for arm in PROVIDER_ARM_IDS
                },
                provider_envelopes_by_arm={
                    arm: {"task": "bounded-review", "method": arm}
                    for arm in PROVIDER_ARM_IDS
                },
                scoring_truth=truth,
            )
        )
    return build_agent_complexity_campaign_plan(
        protocol=protocol,
        campaign_id="fair-agent-complexity-v1",
        wandb_campaign_run_id="usefulness-agent-complexity-a1b2c3d4",
        model_preflights=preflights,
        fair_input_receipts=receipts,
        scoring_truths=scoring_truths,
        frozen_at=NOW + timedelta(minutes=2),
    )


def _trace(*, provider: bool, invocation_id: str) -> InvocationTraceReceipt:
    return InvocationTraceReceipt(
        wandb_run_id="usefulness-agent-complexity-a1b2c3d4",
        provider_invoked=provider,
        weave_trace_id=f"trace-{invocation_id}" if provider else None,
        weave_trace_readback_sha256=(
            canonical_sha256({"trace": invocation_id}) if provider else None
        ),
        trace_readback_verified=provider,
        wandb_association_verified=provider,
    )


def _retrieval(
    *,
    invocation_id: str,
    task_id: str,
    arm_id: str,
    model_id: str,
    relevant_aliases: tuple[str, ...],
    irrelevant_aliases: tuple[str, ...],
    available_aliases: tuple[str, ...],
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
    cost_usd: float,
):
    returned = relevant_aliases + irrelevant_aliases
    return AgentRetrievalObservation(
        invocation_id=invocation_id,
        case_id=task_id,
        arm_id=arm_id,
        model_id=model_id,
        turn_index=1,
        tool_call_index=1,
        tool_name="review_brief_get_findings",
        available_aliases=available_aliases,
        requested_aliases=returned,
        returned_aliases=returned,
        relevant_aliases=relevant_aliases,
        irrelevant_aliases=irrelevant_aliases,
        context_bytes_before=100,
        context_bytes_after=300,
        tool_result_bytes=200,
        provider_input_tokens=input_tokens,
        provider_output_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        stop_reason=RetrievalStopReason.SUFFICIENT_EVIDENCE,
        weave_trace_id=f"trace-{invocation_id}",
    )


def _record(
    *,
    plan,
    task,
    replicate_index: int,
    arm_id: str,
    model_id: str | None,
    input_tokens: int,
    irrelevant_count: int,
    include_irrelevant_selection: bool = False,
    selected_grounding_pairs_override: tuple[FindingEvidenceAliasPair, ...] | None = None,
    reject_selection: bool = False,
    prohibited_count: int = 0,
):
    invocation_id = agent_complexity_invocation_id(
        plan_sha256=plan.plan_sha256,
        task_id=task.task_id,
        replicate_index=replicate_index,
        arm_id=arm_id,
        model_id=model_id,
    )
    provider = model_id is not None
    agent = arm_id in {ADAPTIVE_ARM_ID, FORCED_EXHAUSTIVE_ARM_ID}
    output_tokens = 10 if provider else 0
    latency_ms = 10.0 if provider else 1.0
    cost_usd = 0.001 if provider else 0.0
    truth = next(item for item in plan.scoring_truths if item.task_id == task.task_id)
    relevant_aliases = tuple(truth.relevant_evidence_aliases)
    irrelevant_aliases = tuple(truth.irrelevant_evidence_aliases[:irrelevant_count])
    available_evidence = (
        tuple(truth.relevant_evidence_aliases) + tuple(truth.irrelevant_evidence_aliases)
    )
    accessed_evidence = (
        relevant_aliases + irrelevant_aliases
        if agent
        else available_evidence
        if arm_id in {DETERMINISTIC_ARM_ID, FULL_CONTEXT_ARM_ID}
        else relevant_aliases
    )
    selected_grounding_pairs = (
        selected_grounding_pairs_override
        if selected_grounding_pairs_override is not None
        else truth.supporting_evidence_pairs
    )
    if include_irrelevant_selection:
        selected_grounding_pairs = (
            *selected_grounding_pairs,
            FindingEvidenceAliasPair(
                finding_alias=truth.required_finding_aliases[0],
                evidence_alias=truth.irrelevant_evidence_aliases[0],
            ),
        )
    observations = (
        (
            _retrieval(
                invocation_id=invocation_id,
                task_id=task.task_id,
                arm_id=arm_id,
                model_id=model_id,
                relevant_aliases=relevant_aliases,
                irrelevant_aliases=irrelevant_aliases,
                available_aliases=available_evidence,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
            ),
        )
        if agent and model_id is not None
        else ()
    )
    stop_reason = {
        DETERMINISTIC_ARM_ID: CampaignStopReason.NOT_APPLICABLE,
        FULL_CONTEXT_ARM_ID: CampaignStopReason.SINGLE_SHOT_COMPLETE,
        ADAPTIVE_ARM_ID: CampaignStopReason.SELECTION_READY,
        FORCED_EXHAUSTIVE_ARM_ID: CampaignStopReason.FORCED_EXHAUSTIVE_COMPLETE,
        ORACLE_ARM_ID: CampaignStopReason.ORACLE_CONTROL_COMPLETE,
    }[arm_id]
    rejected_selection = include_irrelevant_selection or reject_selection
    disposition = (
        InvocationDisposition.VALIDATION_FAILED
        if rejected_selection
        else InvocationDisposition.RELEASED
    )
    if rejected_selection:
        stop_reason = CampaignStopReason.FAIL_CLOSED
    return seal_invocation_record(
        scoring_truth=truth,
        invocation_id=invocation_id,
        plan_sha256=plan.plan_sha256,
        task_id=task.task_id,
        task_sha256=task.task_sha256,
        fair_input_receipt_sha256=task.fair_input_receipt_sha256,
        arm_id=arm_id,
        model_id=model_id,
        replicate_index=replicate_index,
        disposition=disposition,
        expected_fail_closed=False,
        trace=_trace(provider=provider, invocation_id=invocation_id),
        retrieval_observations=observations,
        selected_grounding_pairs=selected_grounding_pairs,
        accessed_evidence_aliases=accessed_evidence,
        stop_reason=stop_reason,
        provider_context_bytes=(300 if agent else (1200 if provider else 0)),
        provider_input_tokens=input_tokens,
        provider_output_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        duplicate_retrieval_count=0,
        tool_call_count=len(observations),
        prohibited_claim_count=prohibited_count,
        sensitive_location_leak_count=0,
        unauthorized_tool_attempt_count=0,
        unauthorized_action_executed_count=0,
        deterministic_validation_passed=not rejected_selection,
        draft_saved=not rejected_selection,
    )


def _scored_rows(
    plan, *, negative: bool = False, relational_mismatch: bool = False
):
    rows = []
    model_id = MODEL_IDS[0]
    for task in plan.tasks[:6]:
        truth = next(item for item in plan.scoring_truths if item.task_id == task.task_id)
        wrong_related_pairs = (
            (
                FindingEvidenceAliasPair(
                    finding_alias="F1",
                    evidence_alias="E2",
                ),
                *truth.supporting_evidence_pairs[1:],
            )
            if relational_mismatch
            else None
        )
        rows.extend(
            (
                _record(
                    plan=plan,
                    task=task,
                    replicate_index=0,
                    arm_id=DETERMINISTIC_ARM_ID,
                    model_id=None,
                    input_tokens=0,
                    irrelevant_count=10,
                ),
                _record(
                    plan=plan,
                    task=task,
                    replicate_index=0,
                    arm_id=FULL_CONTEXT_ARM_ID,
                    model_id=model_id,
                    input_tokens=100,
                    irrelevant_count=10,
                ),
                _record(
                    plan=plan,
                    task=task,
                    replicate_index=0,
                    arm_id=ADAPTIVE_ARM_ID,
                    model_id=model_id,
                    input_tokens=98 if negative else 60,
                    irrelevant_count=8 if negative else 3,
                    include_irrelevant_selection=negative,
                    selected_grounding_pairs_override=wrong_related_pairs,
                    reject_selection=relational_mismatch,
                    prohibited_count=1 if negative else 0,
                ),
                _record(
                    plan=plan,
                    task=task,
                    replicate_index=0,
                    arm_id=FORCED_EXHAUSTIVE_ARM_ID,
                    model_id=model_id,
                    input_tokens=90,
                    irrelevant_count=10,
                ),
                _record(
                    plan=plan,
                    task=task,
                    replicate_index=0,
                    arm_id=ORACLE_ARM_ID,
                    model_id=model_id,
                    input_tokens=35,
                    irrelevant_count=0,
                ),
            )
        )
    return tuple(rows)


def test_plan_has_exact_300_cells_60_repeat_cells_and_five_fair_arms() -> None:
    plan = _plan()
    assert len(plan.tasks) == 300
    assert sum(task.repeat_cell for task in plan.tasks) == 60
    assert len(expected_execution_keys(plan)) == 7_800
    assert sum(key[3] is not None for key in expected_execution_keys(plan)) == 7_200
    assert len(set(plan.model_ids)) == 3
    assert len(plan.fair_input_receipts) == 300
    receipt = plan.fair_input_receipts[0]
    assert len(receipt.provider_visible_artifacts) == 4
    assert len({item.evidence_bytes_sha256 for item in receipt.provider_visible_artifacts}) == 1
    assert len({item.output_schema_bytes_sha256 for item in receipt.provider_visible_artifacts}) == 1
    assert len({item.prompt_bytes_sha256 for item in receipt.provider_visible_artifacts}) == 4


def test_fair_receipt_rejects_target_leakage_and_different_provider_evidence() -> None:
    schema = {"type": "object", "required": ["selected_grounding_pairs"]}
    evidence = {"authorized_aliases": ["F1", "F2"]}
    with pytest.raises(ValueError, match="target leakage"):
        create_fair_input_receipt(
            task_id="task-1",
            evidence_payloads_by_arm={
                arm: {"authorized_answer_contract": ["F1"]} for arm in PROVIDER_ARM_IDS
            },
            output_schema_payloads_by_arm={arm: schema for arm in PROVIDER_ARM_IDS},
            provider_envelope_contract={"type": "object"},
            prompt_payloads_by_arm={arm: {"prompt": "safe"} for arm in PROVIDER_ARM_IDS},
            provider_envelopes_by_arm={
                arm: {"method": arm, "task": "bounded"} for arm in PROVIDER_ARM_IDS
            },
            scoring_truth=_tiny_truth(),
        )
    unequal = {arm: evidence for arm in PROVIDER_ARM_IDS}
    unequal[ADAPTIVE_ARM_ID] = {"authorized_aliases": ["F1"]}
    with pytest.raises(ValueError, match="different evidence"):
        create_fair_input_receipt(
            task_id="task-1",
            evidence_payloads_by_arm=unequal,
            output_schema_payloads_by_arm={arm: schema for arm in PROVIDER_ARM_IDS},
            provider_envelope_contract={"type": "object"},
            prompt_payloads_by_arm={arm: {"prompt": "safe"} for arm in PROVIDER_ARM_IDS},
            provider_envelopes_by_arm={
                arm: {"method": arm, "task": "bounded"} for arm in PROVIDER_ARM_IDS
            },
            scoring_truth=_tiny_truth(),
        )


def test_fair_receipt_rejects_exact_truth_alias_in_prompt_even_without_banned_key() -> None:
    schema = {"type": "object", "required": ["selected_grounding_pairs"]}
    evidence = {"authorized_items": [{"alias": "F1"}, {"alias": "F2"}]}
    prompts = {arm: {"prompt": "safe"} for arm in PROVIDER_ARM_IDS}
    prompts[ADAPTIVE_ARM_ID] = {"prompt": "focus on F1"}
    with pytest.raises(ValueError, match="exact scoring truth"):
        create_fair_input_receipt(
            task_id="task-1",
            evidence_payloads_by_arm={arm: evidence for arm in PROVIDER_ARM_IDS},
            output_schema_payloads_by_arm={arm: schema for arm in PROVIDER_ARM_IDS},
            provider_envelope_contract={"type": "object"},
            prompt_payloads_by_arm=prompts,
            provider_envelopes_by_arm={
                arm: {"method": arm, "task": "bounded"} for arm in PROVIDER_ARM_IDS
            },
            scoring_truth=_tiny_truth(),
        )


def test_known_paired_rows_promote_only_when_every_gate_passes() -> None:
    plan = _plan()
    assessment = evaluate_model_promotion(
        _scored_rows(plan),
        model_id=MODEL_IDS[0],
        bootstrap_seed=71,
        bootstrap_resamples=500,
    )
    assert assessment.decision == PromotionDecision.PROMOTE_AGENT
    assert assessment.critical_required_finding_recall == 1
    assert assessment.evidence_precision == 1
    assert assessment.input_token_reduction.estimate == pytest.approx(40)
    assert assessment.irrelevant_evidence_reduction.estimate == pytest.approx(70)
    assert assessment.irrelevant_evidence_reduction.confidence_interval_low > 0
    assert assessment.forced_exhaustive_efficiency_gate is True

    negative = evaluate_model_promotion(
        _scored_rows(plan, negative=True),
        model_id=MODEL_IDS[0],
        bootstrap_seed=71,
        bootstrap_resamples=500,
    )
    assert negative.decision == PromotionDecision.KEEP_DETERMINISTIC
    assert negative.evidence_precision_gate is False
    assert negative.safety_gate is False
    assert negative.input_token_gate is False
    assert negative.forced_exhaustive_efficiency_gate is False

    relational_mismatch = evaluate_model_promotion(
        _scored_rows(plan, relational_mismatch=True),
        model_id=MODEL_IDS[0],
        bootstrap_seed=71,
        bootstrap_resamples=500,
    )
    assert relational_mismatch.decision == PromotionDecision.KEEP_DETERMINISTIC
    assert relational_mismatch.required_recall_gate is False
    assert relational_mismatch.evidence_precision_gate is False
    assert relational_mismatch.unsupported_grounding_count == 6
    assert relational_mismatch.safety_gate is False


def test_repeated_invocations_are_clustered_inside_equal_weight_base_tasks() -> None:
    plan = _plan()
    repeated_task = next(item for item in plan.tasks if item.repeat_cell)
    single_task = next(item for item in plan.tasks if not item.repeat_cell)
    rows = []
    for task in (repeated_task, single_task):
        for replicate_index in range(task.repeat_count + 1):
            adaptive_tokens = (
                120
                if task is repeated_task and replicate_index == 0
                else 200
                if task is repeated_task
                else 20
            )
            full_tokens = 200 if task is repeated_task else 100
            for arm_id, tokens, irrelevant in (
                (DETERMINISTIC_ARM_ID, 0, 10),
                (FULL_CONTEXT_ARM_ID, full_tokens, 10),
                (ADAPTIVE_ARM_ID, adaptive_tokens, 3),
                (FORCED_EXHAUSTIVE_ARM_ID, full_tokens + 10, 10),
                (ORACLE_ARM_ID, 15, 0),
            ):
                rows.append(
                    _record(
                        plan=plan,
                        task=task,
                        replicate_index=replicate_index,
                        arm_id=arm_id,
                        model_id=None if arm_id == DETERMINISTIC_ARM_ID else MODEL_IDS[0],
                        input_tokens=tokens,
                        irrelevant_count=irrelevant,
                    )
                )
    assessment = evaluate_model_promotion(
        rows,
        model_id=MODEL_IDS[0],
        bootstrap_seed=991,
        bootstrap_resamples=200,
    )
    assert assessment.paired_execution_count == 2
    assert assessment.input_token_reduction.estimate == pytest.approx(43.3333333333)


def test_fail_closed_draft_and_unreconciled_agent_receipts_are_rejected() -> None:
    plan = _plan()
    task = plan.tasks[0]
    row = _record(
        plan=plan,
        task=task,
        replicate_index=0,
        arm_id=ADAPTIVE_ARM_ID,
        model_id=MODEL_IDS[0],
        input_tokens=60,
        irrelevant_count=3,
    )
    payload = row.model_dump(mode="json")
    payload["expected_fail_closed"] = True
    payload["record_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="fail-closed case"):
        type(row).model_validate(payload)

    payload = row.model_dump(mode="json")
    payload["provider_input_tokens"] = 61
    payload["record_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="input tokens differ"):
        type(row).model_validate(payload)


def test_exact_truth_rejects_wrong_grounding_even_when_counts_and_aliases_are_relevant() -> None:
    plan = _plan()
    task = next(item for item in plan.tasks if item.finding_count == 15)
    truth = next(item for item in plan.scoring_truths if item.task_id == task.task_id)
    row = _record(
        plan=plan,
        task=task,
        replicate_index=0,
        arm_id=ADAPTIVE_ARM_ID,
        model_id=MODEL_IDS[0],
        input_tokens=60,
        irrelevant_count=3,
    )
    serialized = row.model_dump(mode="json")
    assert "selected_grounding_pairs" in serialized
    assert "selected_finding_aliases" not in serialized
    assert "selected_evidence_aliases" not in serialized
    wrong_finding_pairs = (
        *row.selected_grounding_pairs[:-1],
        FindingEvidenceAliasPair(finding_alias="F6", evidence_alias="E5"),
    )
    forged = row.model_copy(update={"selected_grounding_pairs": wrong_finding_pairs})
    with pytest.raises(ValueError, match="exact adjudicated aliases"):
        validate_invocation_record_truth(forged, truth)

    # E2 is globally relevant, but it supports F2 rather than F1. Independent
    # alias-set scoring would accept this forged edge because counts and global
    # relevance are unchanged; exact relation scoring must reject it.
    wrong_relation = (
        FindingEvidenceAliasPair(finding_alias="F1", evidence_alias="E2"),
        *row.selected_grounding_pairs[1:],
    )
    forged = row.model_copy(update={"selected_grounding_pairs": wrong_relation})
    with pytest.raises(ValueError, match="exact adjudicated aliases"):
        validate_invocation_record_truth(forged, truth)


def test_wrong_evidence_for_correct_finding_is_retained_as_unsupported_grounding() -> None:
    plan = _plan()
    task = next(item for item in plan.tasks if item.finding_count == 15)
    truth = next(item for item in plan.scoring_truths if item.task_id == task.task_id)
    valid = _record(
        plan=plan,
        task=task,
        replicate_index=0,
        arm_id=ADAPTIVE_ARM_ID,
        model_id=MODEL_IDS[0],
        input_tokens=60,
        irrelevant_count=3,
    )
    wrong_relation = (
        FindingEvidenceAliasPair(finding_alias="F1", evidence_alias="E2"),
        *valid.selected_grounding_pairs[1:],
    )
    rejected = _record(
        plan=plan,
        task=task,
        replicate_index=0,
        arm_id=ADAPTIVE_ARM_ID,
        model_id=MODEL_IDS[0],
        input_tokens=60,
        irrelevant_count=3,
        selected_grounding_pairs_override=wrong_relation,
        reject_selection=True,
    )
    assert rejected.selected_finding_aliases == valid.selected_finding_aliases
    assert set(rejected.selected_evidence_aliases).issubset(
        truth.relevant_evidence_aliases
    )
    assert rejected.unsupported_grounding_count == 1
    assert rejected.selected_supported_finding_count == (
        valid.selected_supported_finding_count - 1
    )
    assert rejected.evidence_precision < 1
    assert rejected.draft_saved is False


def test_completed_campaign_summary_refuses_partial_or_unassociated_results() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="incomplete or unexpected"):
        summarize_completed_agent_complexity_campaign(
            plan=plan,
            records=_scored_rows(plan),
            completed_at=NOW + timedelta(hours=1),
            bootstrap_resamples=100,
        )


def test_completed_known_synthetic_campaign_reconciles_all_execution_and_trace_ids() -> None:
    plan = _plan()
    task_by_id = {task.task_id: task for task in plan.tasks}
    arm_metrics = {
        DETERMINISTIC_ARM_ID: (0, 10),
        FULL_CONTEXT_ARM_ID: (100, 10),
        ADAPTIVE_ARM_ID: (60, 3),
        FORCED_EXHAUSTIVE_ARM_ID: (90, 10),
        ORACLE_ARM_ID: (35, 0),
    }
    records = tuple(
        _record(
            plan=plan,
            task=task_by_id[task_id],
            replicate_index=replicate_index,
            arm_id=arm_id,
            model_id=model_id,
            input_tokens=arm_metrics[arm_id][0],
            irrelevant_count=arm_metrics[arm_id][1],
        )
        for task_id, replicate_index, arm_id, model_id in expected_execution_keys(plan)
    )
    summary = summarize_completed_agent_complexity_campaign(
        plan=plan,
        records=records,
        completed_at=NOW + timedelta(hours=1),
        bootstrap_seed=91,
        bootstrap_resamples=100,
    )
    assert summary.execution_count == 7_800
    assert summary.provider_invocation_count == 7_200
    assert summary.exact_weave_trace_count == 7_200
    assert summary.decision == PromotionDecision.PROMOTE_AGENT
    assert {item.model_id for item in summary.model_assessments} == set(MODEL_IDS)
