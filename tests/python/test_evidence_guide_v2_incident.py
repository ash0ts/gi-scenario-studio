from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from gi_scenario_core.evidence_guide_v2_campaign import (
    AttemptOutcome,
    AttemptSpec,
    CampaignStore,
    ModelPrice,
    json_sha256,
)
from gi_scenario_core.evidence_guide_v2_incident import (
    build_trace_canary_incident,
    validate_trace_canary_incident,
)


def _outcome(attempt_id: str) -> AttemptOutcome:
    return AttemptOutcome(
        attempt_id=attempt_id,
        status="completed",
        provider_invocations=0,
        transport_attempts=0,
        input_tokens=0,
        output_tokens=0,
        cost_usd=Decimal(0),
        latency_ms=1,
        protocol_valid=True,
        safe=True,
        evidence_valid=True,
        repository_fidelity=True,
        injection_safe=True,
        causal=True,
        intent_correct=True,
        safety_intent_correct=None,
        false_refusal=False,
        trace_acknowledged=True,
    )


def _spec(attempt_id: str, architecture_id: str, case_id: str) -> AttemptSpec:
    return AttemptSpec(
        attempt_id=attempt_id,
        stage="trace_canary",
        split="development",
        case_id=case_id,
        project_family="cc0",
        model_id=(
            "ibm-granite/granite-4.1-8b"
            if architecture_id == "A1"
            else "repository"
        ),
        architecture_id=architecture_id,
        prompt_id="checklist",
        max_output_tokens=128,
        repeat=0,
        time_block="block-1",
    )


def _failed_checkpoint(root: Path) -> None:
    preregistration_body = {
        "schema_version": "1.0",
        "protocol_amendment": {"receipt_sha256": "c" * 64},
    }
    preregistration = {
        **preregistration_body,
        "preregistration_sha256": json_sha256(preregistration_body),
    }
    (root / "preregistration.json").write_text(
        json.dumps(preregistration, sort_keys=True), encoding="utf-8"
    )
    (root / "campaign-plan.json").write_text("{}\n", encoding="utf-8")

    store = CampaignStore(root / "campaign.sqlite")
    store.put_meta(
        "prior_usage",
        {
            "protocol_amendment_sha256": "c" * 64,
            "cost_usd": "0.013686",
            "provider_invocations": 290,
            "transport_attempts": 290,
            "input_tokens": 242189,
            "output_tokens": 37120,
        },
    )
    store.begin_session("trace_canary", "block-1")
    deterministic = [
        _spec("a" * 32, "D0", "case-d0-pass"),
        _spec("b" * 32, "D0", "case-d0-reject"),
        _spec("c" * 32, "D1", "case-d1"),
    ]
    for spec in deterministic:
        store.save_attempt(spec, _outcome(spec.attempt_id))
    a1 = _spec("d" * 32, "A1", "case-a1")
    store.plan_attempts(
        [a1],
        pricing={
            a1.model_id: ModelPrice(
                input_per_million=Decimal(1),
                output_per_million=Decimal(1),
                source="https://wandb.ai/test",
                observed_at="2026-08-07T00:00:00Z",
            )
        },
    )
    group = {"stage": "trace_canary", "architecture_id": "D0"}
    store.put_receipt(
        f"block:{json_sha256(group)}",
        {
            "group": group,
            "completed": 2,
            "reused": 0,
            "cloud": {"attempt_count": 2, "group_sha256": json_sha256(group)},
        },
    )
    store.close()


def test_trace_canary_incident_seals_only_zero_inference_preflight(tmp_path: Path) -> None:
    _failed_checkpoint(tmp_path)

    receipt = build_trace_canary_incident(tmp_path)

    assert validate_trace_canary_incident(receipt) == receipt
    assert receipt["canary_attempt_count"] == 3
    assert receipt["provider_usage"] == {
        "cost_usd": "0",
        "provider_invocations": 0,
        "transport_attempts": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert receipt["scientific_denominator_attempts"] == 0
    assert receipt["architecture_denominator_attempts"] == 0
    assert all(receipt["checks"].values())

    tampered = copy.deepcopy(receipt)
    tampered["provider_usage"]["provider_invocations"] = 1
    with pytest.raises(ValueError, match="incident receipt is invalid"):
        validate_trace_canary_incident(tampered)

