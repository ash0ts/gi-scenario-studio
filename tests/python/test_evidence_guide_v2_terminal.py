from __future__ import annotations

import copy
from pathlib import Path

import pytest
from gi_scenario_core.evidence_guide_v2_campaign import (
    AttemptOutcome,
    AttemptSpec,
    CampaignStore,
    json_sha256,
)
from gi_scenario_core.evidence_guide_v2_terminal import (
    build_trace_canary_terminal_disposition,
    validate_terminal_disposition,
)


def _spec(attempt_id: str, architecture_id: str) -> AttemptSpec:
    return AttemptSpec(
        attempt_id=attempt_id,
        stage="trace_canary",
        split="development",
        case_id=f"case-{architecture_id}",
        project_family="cc0",
        model_id="model" if architecture_id == "A1" else "repository",
        architecture_id=architecture_id,
        prompt_id="checklist",
        max_output_tokens=128,
        repeat=0,
        time_block="block-1",
    )


def _outcome(spec: AttemptSpec) -> AttemptOutcome:
    is_a1 = spec.architecture_id == "A1"
    return AttemptOutcome(
        attempt_id=spec.attempt_id,
        status="completed",
        provider_invocations=1 if is_a1 else 0,
        transport_attempts=1 if is_a1 else 0,
        input_tokens=10 if is_a1 else 0,
        output_tokens=2 if is_a1 else 0,
        cost_usd="0.000001" if is_a1 else "0",
        latency_ms=1,
        protocol_valid=True,
        safe=not is_a1,
        evidence_valid=not is_a1,
        repository_fidelity=not is_a1,
        injection_safe=True,
        causal=True,
        intent_correct=not is_a1,
        safety_intent_correct=not is_a1,
        false_refusal=False,
        trace_acknowledged=True,
        explanation_plan_id=(
            "recorded_rules_fail_v1" if is_a1 else "authority_boundary_v1"
        ),
        error_codes=("explanation_plan_mismatch",) if is_a1 else (),
        details={"answer_released": not is_a1, "tool_events": []},
    )


def test_terminal_disposition_seals_a_rejected_model_canary(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "campaign.sqlite")
    store.put_meta(
        "prior_usage",
        {
            "protocol_amendment_sha256": "a" * 64,
            "cost_usd": "0",
            "provider_invocations": 0,
            "transport_attempts": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        },
    )
    store.begin_session("trace_canary", "block-1")
    rows = []
    for index, architecture_id in enumerate(("D0", "D0", "D1", "A1")):
        spec = _spec(f"{index + 1}" * 32, architecture_id)
        outcome = _outcome(spec)
        store.save_attempt(spec, outcome)
        rows.append(outcome.as_record(spec))
    store.finish_session("trace_canary", "block-1")
    canary_body = {
        "schema_version": "1.0",
        "stage": "trace_canary",
        "scientific_denominator_attempts": 0,
        "attempt_count": 4,
        "checks": {
            "four_unique_attempts": True,
            "all_trace_acknowledged": True,
            "all_protocol_completed": True,
            "d0_passing_answer_acknowledged_without_provider": True,
            "d0_rejection_acknowledged_without_release": True,
            "d1_tool_span_acknowledged": True,
            "a1_model_and_tool_spans_acknowledged": False,
            "three_exact_cloud_block_receipts": True,
        },
        "passed": False,
        "attempt_row_sha256": json_sha256(sorted(rows, key=lambda row: row["attempt_id"])),
        "claim_boundary": "test",
        "campaign_instance_sha256": "b" * 64,
        "protocol_amendment_sha256": "a" * 64,
        "cloud_block_receipts": [{}, {}, {}],
    }
    store.put_receipt(
        "trace_canary",
        {**canary_body, "receipt_sha256": json_sha256(canary_body)},
    )
    release_gate = {
        "decision": "DISABLE_LIVE_GUIDE",
        "selected_model": None,
        "production_scope": [],
        "release_gate_digest": "c" * 64,
    }
    receipt = build_trace_canary_terminal_disposition(
        store=store,
        preregistration={"preregistration_sha256": "b" * 64},
        release_gate=release_gate,
        finalizer_sha256="d" * 64,
    )

    assert validate_terminal_disposition(receipt) == receipt
    assert receipt["decision"] == "DISABLE_LIVE_GUIDE"
    assert receipt["scientific_denominator_attempts"] == 0
    assert receipt["a1_observation"]["selected_plan_id"] == "recorded_rules_fail_v1"
    assert receipt["d1_control"]["selected_plan_id"] == "authority_boundary_v1"

    tampered = copy.deepcopy(receipt)
    tampered["provider_answer_released"] = True
    with pytest.raises(ValueError, match="terminal disposition is invalid"):
        validate_terminal_disposition(tampered)
    store.close()
