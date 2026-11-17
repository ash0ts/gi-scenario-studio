from __future__ import annotations

import copy
from decimal import Decimal

import pytest
from gi_scenario_core.evidence_guide_v2_amendment import validate_protocol_amendment
from gi_scenario_core.evidence_guide_v2_campaign import json_sha256


def _receipt() -> dict[str, object]:
    capability_usage = {
        "cost_usd": "0.01",
        "provider_invocations": 10,
        "transport_attempts": 10,
        "provider_reported_input_tokens": 100,
        "provider_reported_output_tokens": 20,
        "input_tokens": 200,
        "output_tokens": 40,
    }
    architecture_usage = {
        "cost_usd": "0.001",
        "provider_invocations": 2,
        "transport_attempts": 2,
        "provider_reported_input_tokens": 20,
        "provider_reported_output_tokens": 4,
        "input_tokens": 40,
        "output_tokens": 8,
    }
    preflight_usage = {
        "cost_usd": "0.0001",
        "provider_invocations": 1,
        "transport_attempts": 1,
        "provider_reported_input_tokens": 10,
        "provider_reported_output_tokens": 2,
        "input_tokens": 20,
        "output_tokens": 4,
    }
    r1_usage = {
        key: (
            str(Decimal(str(capability_usage[key])) + Decimal(str(architecture_usage[key])))
            if key == "cost_usd"
            else int(capability_usage[key]) + int(architecture_usage[key])
        )
        for key in capability_usage
    }
    cumulative_usage = {
        key: (
            str(Decimal(str(r1_usage[key])) + Decimal(str(preflight_usage[key])))
            if key == "cost_usd"
            else int(r1_usage[key]) + int(preflight_usage[key])
        )
        for key in r1_usage
    }
    body: dict[str, object] = {
        "schema_version": "1.0",
        "receipt_kind": "evidence_guide_v2_protocol_amendment",
        "campaign_version": "evidence-guide-v2-architecture-first",
        "protocol_revision": "r2",
        "capability_inheritance": {
            "eligible": True,
            "attempt_count": 160,
            "usage": capability_usage,
        },
        "architecture_exclusion": {
            "excluded": True,
            "attempt_count": 18,
            "usage": architecture_usage,
        },
        "r1_usage": r1_usage,
        "preflight_usage": preflight_usage,
        "cumulative_prior_usage": cumulative_usage,
        "r2_requirements": {"capability_not_rerun": True},
    }
    return {**body, "receipt_sha256": json_sha256(body)}


def test_protocol_amendment_is_digest_bound_and_fail_closed() -> None:
    receipt = _receipt()
    assert validate_protocol_amendment(receipt) == receipt

    tampered = copy.deepcopy(receipt)
    tampered["architecture_exclusion"]["excluded"] = False  # type: ignore[index]
    with pytest.raises(ValueError, match="protocol amendment is invalid"):
        validate_protocol_amendment(tampered)
