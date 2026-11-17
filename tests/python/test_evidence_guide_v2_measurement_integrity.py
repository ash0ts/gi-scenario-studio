from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from gi_scenario_core.evidence_guide_v2_architecture_dev import (
    ResearchAttemptSpec,
    ResearchFakeExecutor,
    analyze_research_rows,
)
from gi_scenario_core.evidence_guide_v2_architecture_executor import (
    _AttemptMeter,
    build_semantic_intervention_receipt,
    research_remap_index,
    validate_semantic_intervention_receipt,
)
from gi_scenario_core.evidence_guide_v2_campaign import (
    ModelPrice,
    NullAttemptObserver,
    load_corpus,
)
from gi_scenario_core.evidence_guide_v2_provider import (
    _repository_tool_result,
    provider_packet,
)
from gi_scenario_core.inference import (
    InferenceCompletion,
    InferenceRequestError,
    InferenceUsage,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS = load_corpus(ROOT / "benchmarks/evidence_guide/v2")
PRICE = ModelPrice(Decimal(1), Decimal(2), "test", "2026-08-07")


class _CompletionClient:
    def complete(self, **_kwargs: object) -> InferenceCompletion:
        return InferenceCompletion(
            provider_call_id="provider-call",
            model="model",
            content="{}",
            tool_calls=[],
            usage=InferenceUsage(input_tokens=10, output_tokens=2, total_tokens=12),
            finish_reason="stop",
            transport_attempts=2,
            request_bytes=100,
            latency_ms=1,
        )


class _FailedTransportClient:
    def complete(self, **_kwargs: object) -> InferenceCompletion:
        raise InferenceRequestError(
            "INFERENCE_TIMEOUT",
            transport_attempts=2,
            request_bytes=100,
        )


def test_cost_accounting_separates_verified_usage_from_at_risk_reservation() -> None:
    meter = _AttemptMeter(_CompletionClient(), max_output_tokens=128)
    meter.complete()

    accounting = meter.cost_accounting(PRICE)
    assert accounting["verified_provider_usage_cost_usd"] == "0.000014"
    assert accounting["conservative_at_risk_cost_usd"] == "0.000532"
    assert accounting["failed_transport_reservation_is_billed_cost"] is False


def test_failed_transports_reserve_caps_but_are_not_reported_as_billed_cost() -> None:
    meter = _AttemptMeter(_FailedTransportClient(), max_output_tokens=128)
    with pytest.raises(InferenceRequestError):
        meter.complete()

    accounting = meter.cost_accounting(PRICE)
    assert accounting["verified_provider_usage_cost_usd"] == "0"
    assert accounting["conservative_at_risk_cost_usd"] == "0.004608"
    assert accounting["failed_transport_reservation_is_billed_cost"] is False


def _semantic_fixture() -> tuple[
    ResearchAttemptSpec,
    dict[str, object],
    str,
    list[dict[str, object]],
]:
    case = CORPUS.case_map("development")["egv2-dev-causal-retrieval-001"]
    spec = ResearchAttemptSpec(
        attempt_id="semantic-receipt",
        stage="causal_perturbation",
        split="development",
        case_id=case["case_id"],
        project_family=case["project_family"],
        model_id="ibm-granite/granite-4.1-8b",
        architecture_id="A1",
        prompt_id="checklist",
        max_output_tokens=128,
        repeat=0,
        time_block="block-1",
        presentation_index=0,
        ablation_condition="counterfactual_threshold_crossing",
    )
    attempt_index = research_remap_index(spec)
    packet = provider_packet(CORPUS.datasets["development"], case, attempt_index)
    tool_name = str(case["expectations"]["required_tool_plan"][0])
    original = _repository_tool_result(
        case,
        tool_name=tool_name,
        attempt_index=attempt_index,
        hidden_only=True,
    )
    return spec, packet, tool_name, original


def test_semantic_intervention_receipt_is_hash_sealed_and_tamper_evident() -> None:
    spec, packet, tool_name, original = _semantic_fixture()
    counterfactual = [{**original[0], "value": -0.08}]
    receipt = build_semantic_intervention_receipt(
        spec=spec,
        packet=packet,
        tool_name=tool_name,
        original_evidence=original,
        intervention_evidence=counterfactual,
        condition="counterfactual_threshold_crossing",
        operation_sequence=("evidence_tool", "synthesis_call", "validation"),
        response_contract_id="finite-plan-r3",
    )

    assert receipt["clean"] is True
    assert receipt["intended_delta"] == "value_only"
    assert receipt["opaque_evidence_id"] == original[0]["evidence_id"]
    assert receipt["units"] == original[0]["units"]
    assert receipt["evidence_binding"] == original[0]["evidence_binding"]
    assert validate_semantic_intervention_receipt(receipt)

    tampered = {**receipt, "units": "Mgal"}
    assert not validate_semantic_intervention_receipt(tampered)


def test_semantic_intervention_rejects_a_dirty_unit_change() -> None:
    spec, packet, tool_name, original = _semantic_fixture()
    dirty = [{**original[0], "value": -0.08, "units": "Mgal"}]
    with pytest.raises(RuntimeError, match="SEMANTIC_INTERVENTION_DIRTY"):
        build_semantic_intervention_receipt(
            spec=spec,
            packet=packet,
            tool_name=tool_name,
            original_evidence=original,
            intervention_evidence=dirty,
            condition="counterfactual_threshold_crossing",
            operation_sequence=("evidence_tool", "synthesis_call", "validation"),
            response_contract_id="finite-plan-r3",
        )


def _fake_causal_triad() -> list[dict[str, object]]:
    case = CORPUS.case_map("development")["egv2-dev-causal-retrieval-001"]
    executor = ResearchFakeExecutor()
    rows: list[dict[str, object]] = []
    for index, condition in enumerate(
        (
            "evidence_original",
            "counterfactual_threshold_crossing",
            "evidence_withheld",
        )
    ):
        spec = ResearchAttemptSpec(
            attempt_id=f"{index + 1:032x}",
            stage="causal_perturbation",
            split="development",
            case_id=case["case_id"],
            project_family=case["project_family"],
            model_id="ibm-granite/granite-4.1-8b",
            architecture_id="A1",
            prompt_id="checklist",
            max_output_tokens=128,
            repeat=0,
            time_block="block-1",
            presentation_index=0,
            ablation_condition=condition,
        )
        outcome = executor.execute(
            spec=spec,
            case=case,
            observer=NullAttemptObserver(),
        )
        rows.append(outcome.as_record(spec))
    return rows


def test_withheld_outcomes_are_exclusive_and_infrastructure_is_not_success() -> None:
    correct = analyze_research_rows(_fake_causal_triad(), bootstrap_samples=10)
    assert correct["causal_triads"]["withheld_outcomes"] == {
        "correct_refusal": 1,
        "validator_containment": 0,
        "infrastructure_or_provider_failure": 0,
        "unsafe_acceptance": 0,
    }
    assert correct["causal_triads"]["withheld_safe_count"] == 1

    failed_rows = _fake_causal_triad()
    withheld = failed_rows[2]
    withheld["status"] = "failed_closed"
    withheld["protocol_valid"] = False
    withheld["trace_acknowledged"] = False
    details = dict(withheld["details"])
    details["candidate_conformant"] = False
    details["hypothetical_release_eligible"] = False
    withheld["details"] = details
    failed = analyze_research_rows(failed_rows, bootstrap_samples=10)
    assert failed["causal_triads"]["withheld_outcomes"] == {
        "correct_refusal": 0,
        "validator_containment": 0,
        "infrastructure_or_provider_failure": 1,
        "unsafe_acceptance": 0,
    }
    assert failed["causal_triads"]["withheld_safe_count"] == 0
    assert (
        failed["causal_triads"][
            "infrastructure_failures_count_as_successful_withholding"
        ]
        is False
    )


@pytest.mark.parametrize(
    ("contained", "expected"),
    [
        (True, "validator_containment"),
        (False, "unsafe_acceptance"),
    ],
)
def test_withheld_invalid_response_distinguishes_containment_from_unsafe_acceptance(
    contained: bool,
    expected: str,
) -> None:
    rows = _fake_causal_triad()
    withheld = rows[2]
    details = dict(withheld["details"])
    details["raw_invalid"] = True
    details["candidate_conformant"] = False
    details["hypothetical_release_eligible"] = not contained
    withheld["details"] = details
    analysis = analyze_research_rows(rows, bootstrap_samples=10)
    outcomes = analysis["causal_triads"]["withheld_outcomes"]
    assert outcomes[expected] == 1
    assert sum(outcomes.values()) == 1


def test_analysis_rejects_a_tampered_complete_semantic_triad() -> None:
    rows = _fake_causal_triad()
    details = dict(rows[1]["details"])
    receipt = dict(details["semantic_intervention_receipt"])
    receipt["units"] = "Mgal"
    details["semantic_intervention_receipt"] = receipt
    rows[1]["details"] = details

    with pytest.raises(RuntimeError, match="SEMANTIC_INTERVENTION_TRIAD_DIRTY"):
        analyze_research_rows(rows, bootstrap_samples=10)
