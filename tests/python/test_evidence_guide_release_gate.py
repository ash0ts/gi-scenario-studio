from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gi_scenario_core import evidence_guide
from gi_scenario_core.contracts import (
    AssistantEvidenceAnchor,
    AssistantEvidencePacket,
    AssistantEvidenceReference,
    AssistantReleaseBasis,
    EvidenceClaim,
)
from gi_scenario_core.evidence_guide import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA_SHA256,
    RESPONSE_SCHEMA_VERSION,
    TOOL_CONTRACT_VERSION,
    VALIDATOR_VERSION,
    EvidenceGuideService,
)
from gi_scenario_core.evidence_guide_v2_campaign import CampaignStore, build_release_gate
from gi_scenario_core.inference import WandbInferenceAdapter
from gi_scenario_core.observability import CredentialSession, ObservabilityService
from gi_scenario_core.rpc_worker import RPCDispatcher
from test_evidence_guide_agent import _enabled_v2_gate

ROOT = Path(__file__).resolve().parents[2]
RELEASE_GATE = ROOT / "benchmarks" / "evidence_guide" / "v1" / "release-gate.json"
V2_RELEASE_GATE = ROOT / "benchmarks" / "evidence_guide" / "v2" / "release-gate.json"
V2_RELEASE_INDEX = ROOT / "benchmarks" / "evidence_guide" / "v2" / "release-gate-index.json"


def _service(*, release_gate: dict[str, Any] | None = None) -> EvidenceGuideService:
    observability = ObservabilityService()
    inference = WandbInferenceAdapter(CredentialSession())
    return EvidenceGuideService(
        observability,
        inference,
        release_gate=release_gate,
    )


def _enabled_gate() -> dict[str, Any]:
    gate = json.loads(RELEASE_GATE.read_text(encoding="utf-8"))
    gate.update(
        {
            "enabled": True,
            "selected_model": "ibm-granite/granite-4.1-8b",
            "decision": "ENABLE_LIVE_GUIDE",
            "reasons": [],
            "selected_prompt_version": PROMPT_VERSION,
            "selected_response_schema_version": RESPONSE_SCHEMA_VERSION,
            "selected_response_schema_sha256": RESPONSE_SCHEMA_SHA256,
            "selected_tool_contract_version": TOOL_CONTRACT_VERSION,
            "selected_validator_version": VALIDATOR_VERSION,
        }
    )
    return gate


def _walk_schema(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_schema(child)


def _packet() -> AssistantEvidencePacket:
    references = [
        AssistantEvidenceReference(
            evidence_id="evidence-support",
            kind="qa",
            label="Accepted execution QA",
            source_id="qa-record",
        ),
        AssistantEvidenceReference(
            evidence_id="evidence-limitation",
            kind="limitation",
            label="Model limitation",
            source_id="limitation-record",
        ),
    ]
    claims = [
        EvidenceClaim(
            claim_id="claim-support",
            project_id="project-release-gate-test",
            claim_type="qa",
            statement="The recorded execution QA was accepted.",
            evidence_ids=["evidence-support"],
            authority="qa_record",
        ),
        EvidenceClaim(
            claim_id="claim-limitation",
            project_id="project-release-gate-test",
            claim_type="limitation",
            statement="The modeled result remains conditional on the recorded assumptions.",
            evidence_ids=["evidence-limitation"],
            authority="documentation",
        ),
    ]
    return AssistantEvidencePacket(
        packet_id="packet-release-gate-test",
        project_id="project-release-gate-test",
        anchor=AssistantEvidenceAnchor(kind="project", identifier="project-release-gate-test"),
        project_kind="demo",
        evidence_references=references,
        claims=claims,
        packet_sha256="0" * 64,
    )


def test_checked_in_real_campaign_gate_loads_and_keeps_live_guide_disabled() -> None:
    service = _service()
    index = json.loads(V2_RELEASE_INDEX.read_text(encoding="utf-8"))

    assert evidence_guide.hashlib.sha256(V2_RELEASE_GATE.read_bytes()).hexdigest() == (
        index["release_gate_sha256"]
    )
    assert index["release_gate_file"] == V2_RELEASE_GATE.name
    assert service.release_gate_error is None
    assert service.release_gate["decision"] == "DISABLE_LIVE_GUIDE"
    assert service.release_gate["release_basis"]["production_scope"] == []
    assert service.release_gate["selected_model"] is None
    assert service.released_model is None


@pytest.mark.parametrize("failure", ["missing", "tampered", "malformed"])
def test_missing_tampered_or_malformed_gate_never_crashes_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    selected = tmp_path / "release-gate.json"
    if failure == "tampered":
        payload = json.loads(RELEASE_GATE.read_text(encoding="utf-8"))
        payload["reasons"] = ["tampered local reason"]
        selected.write_text(json.dumps(payload), encoding="utf-8")
    elif failure == "malformed":
        selected.write_text("{not-json", encoding="utf-8")
        monkeypatch.setattr(
            evidence_guide,
            "_RELEASE_GATE_SHA256",
            evidence_guide.hashlib.sha256(selected.read_bytes()).hexdigest(),
        )
    monkeypatch.setattr(evidence_guide, "_RELEASE_GATE_PATH", selected)
    monkeypatch.setattr(evidence_guide, "_V2_RELEASE_GATE_PATH", tmp_path / "v2-missing.json")
    monkeypatch.setattr(evidence_guide, "_V2_RELEASE_INDEX_PATH", tmp_path / "v2-index.json")

    dispatcher = RPCDispatcher(data_root=tmp_path / "worker", allowed_roots=[tmp_path])
    try:
        assert dispatcher.dispatch("system.ping", {})["api_version"] == "2.0"
        assert dispatcher.evidence_guide.released_model is None
        expected = (
            "EVIDENCE_GUIDE_RELEASE_GATE_UNAVAILABLE"
            if failure == "missing"
            else "EVIDENCE_GUIDE_RELEASE_GATE_INVALID"
        )
        assert dispatcher.evidence_guide.release_gate_error == expected
        project = dispatcher.dispatch("project.create", {"kind": "demo"})
        capabilities = dispatcher.dispatch(
            "assistant.get_capabilities",
            {
                "project_directory": project["projectDirectory"],
                "project_id": project["id"],
                "anchor": {"kind": "alternative", "identifier": "ALT-017"},
            },
        )
        assert capabilities["available"] is False
        assert capabilities["state"] == expected
        assert capabilities["local_evidence_available"] is True
    finally:
        dispatcher._executor.shutdown(wait=True)


def test_rpc_adapter_requires_explicit_valid_gate_injection() -> None:
    dispatcher = RPCDispatcher(
        observability_adapters={"evidence_guide_release_gate": _enabled_gate()}
    )
    try:
        assert dispatcher.evidence_guide.release_gate_error is None
        assert dispatcher.evidence_guide.released_model == "ibm-granite/granite-4.1-8b"
    finally:
        dispatcher._executor.shutdown(wait=True)

    invalid = RPCDispatcher(
        observability_adapters={"evidence_guide_release_gate": {"enabled": True}}
    )
    try:
        assert invalid.evidence_guide.released_model is None
        assert invalid.evidence_guide.release_gate_error == "EVIDENCE_GUIDE_RELEASE_GATE_INVALID"
    finally:
        invalid._executor.shutdown(wait=True)


def test_product_schema_uses_supported_subset_and_bounds_are_enforced_locally() -> None:
    unsupported = {"minItems", "maxItems", "uniqueItems", "minLength", "maxLength"}
    assert all(
        not unsupported.intersection(node) for node in _walk_schema(evidence_guide._ANSWER_SCHEMA)
    )

    packet = _packet()
    with pytest.raises(RuntimeError, match="EVIDENCE_CLAIM_SELECTION_INVALID"):
        evidence_guide._validate_interpretation(
            {
                "intent": "general_evidence",
                "plain_language": "This explanation is long enough and contains no generated values.",
                "claim_ids": ["claim-support"] * 9,
                "limitation_claim_ids": ["claim-limitation"],
                "next_check_ids": ["professional_model_review"],
            },
            packet=packet,
            intent="general_evidence",
        )


def _release_basis(**updates: Any) -> dict[str, Any]:
    issued = datetime.now(UTC)
    payload: dict[str, Any] = {
        "release_gate_digest": "1" * 64,
        "decision": "DISABLE_LIVE_GUIDE",
        "agent_variant_id": "D1",
        "model_id": None,
        "provider_call_count": 0,
        "prompt_sha256": "2" * 64,
        "response_schema_sha256": "3" * 64,
        "tool_contract_sha256": "4" * 64,
        "plan_registry_sha256": "5" * 64,
        "renderer_sha256": "6" * 64,
        "validator_sha256": "7" * 64,
        "dataset_sha256": "8" * 64,
        "judge_config_sha256": "9" * 64,
        "receipt_sha256": "a" * 64,
        "protocol_amendment_sha256": "b" * 64,
        "automated_gate_results": {"hidden": False},
        "production_scope": [],
        "issued_at": issued,
        "expires_at": issued + timedelta(days=90),
    }
    payload.update(updates)
    return payload


def test_release_basis_allows_disabled_d1_but_never_enables_it() -> None:
    disabled = AssistantReleaseBasis(**_release_basis())
    assert disabled.production_scope == []
    with pytest.raises(ValueError, match="D1"):
        AssistantReleaseBasis(
            **_release_basis(
                decision="ENABLE_LIVE_GUIDE",
                automated_gate_results={"hidden": True},
                production_scope=["demo", "standard_engineering"],
            )
        )


def test_release_basis_expires_within_ninety_days() -> None:
    issued = datetime.now(UTC)
    with pytest.raises(ValueError, match="90 days"):
        AssistantReleaseBasis(
            **_release_basis(
                issued_at=issued,
                expires_at=issued + timedelta(days=90, seconds=1),
            )
        )


def test_release_basis_requires_timezone_aware_timestamps() -> None:
    issued = datetime.now(UTC).replace(tzinfo=None)
    with pytest.raises(ValueError, match="UTC offset"):
        AssistantReleaseBasis(
            **_release_basis(
                issued_at=issued,
                expires_at=issued + timedelta(days=30),
            )
        )


def _redigest(gate: dict[str, Any]) -> dict[str, Any]:
    unsigned = copy.deepcopy(gate)
    unsigned.pop("release_gate_digest", None)
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return {**unsigned, "release_gate_digest": hashlib.sha256(encoded).hexdigest()}


def _weak_campaign_gate(case: str) -> dict[str, Any]:
    gate = _enabled_v2_gate()
    study = gate["study_evidence"]
    if case == "wrong_campaign":
        gate["campaign_version"] = "unregistered-campaign"
    elif case == "future_issuance":
        issued = datetime.now(UTC) + timedelta(days=1)
        gate["issued_at"] = issued.isoformat()
        gate["expires_at"] = (issued + timedelta(days=30)).isoformat()
    elif case == "overlong_expiry":
        gate["expires_at"] = (
            datetime.fromisoformat(gate["issued_at"]) + timedelta(days=90, seconds=1)
        ).isoformat()
    elif case == "expired":
        issued = datetime.now(UTC) - timedelta(days=91)
        gate["issued_at"] = issued.isoformat()
        gate["expires_at"] = (issued + timedelta(days=90)).isoformat()
    elif case == "scope_alias":
        gate["production_scope"] = ["cedar_trace", "consenting_standard_engineering"]
    elif case == "scope_reordered":
        gate["production_scope"] = ["standard_engineering", "demo"]
    elif case == "missing_study_evidence":
        gate.pop("study_evidence")
    elif case == "minimal_validation":
        gate["validation"] = {"passed": True}
    elif case == "minimal_hidden":
        gate["hidden"] = {"gate": {"passed": True}}
    elif case == "component_hash":
        gate["hash_pins"]["prompt_sha256"] = "f" * 64
        study["production_runtime"]["component_hashes"]["prompt_sha256"] = "f" * 64
    elif case == "extra_hash_pin":
        gate["hash_pins"]["unregistered_sha256"] = "f" * 64
    elif case == "validation_scenario_count":
        study["confirmatory_counts"]["validation"]["scenario_count"] = 119
        study["confirmatory_counts"]["validation"]["attempt_count"] = 595
    elif case == "validation_attempt_multiplicity":
        study["confirmatory_counts"]["validation"]["attempts_per_scenario"] = 4
        study["confirmatory_counts"]["validation"]["attempt_count"] = 480
    elif case == "time_blocks_per_scenario":
        study["confirmatory_counts"]["hidden"]["minimum_time_blocks_per_scenario"] = 2
    elif case == "one_judge":
        study["judge_comparison"]["judges"].pop("google/gemma-4-31B-it")
        study["judge_comparison"]["passed"] = False
    elif case == "judge_calibration":
        judge = study["judge_comparison"]["judges"]["deepseek-ai/DeepSeek-V4-Pro"]
        judge["calibration_correct"] = 37
        judge["calibration_passed"] = False
        judge["passed"] = False
        study["judge_comparison"]["passed"] = False
    elif case == "missing_soak_fault":
        study["operational_soak"]["observed_fault_ids"].pop()
        study["operational_soak"]["attempt_count"] = 6
        study["operational_soak"]["failed_closed_count"] = 6
        study["operational_soak"]["passed"] = False
        study["stage_evidence"]["soak"]["attempt_count"] = 6
        gate["operational_soak_passed"] = False
    elif case == "circuit_breaker_not_tested":
        study["operational_soak"]["circuit_breaker_verified"] = False
        study["operational_soak"]["passed"] = False
        gate["operational_soak_passed"] = False
    elif case == "trace_canary":
        study["trace_canary"]["passed"] = False
        study["trace_canary"]["receipt_sha256"] = None
        study["stage_evidence"]["trace_canary"]["completed_time_blocks"] = []
        gate["all_three_time_blocks_complete"] = False
    elif case == "stage_time_block":
        study["stage_evidence"]["hidden"]["completed_time_blocks"] = [
            "block-1",
            "block-2",
        ]
        gate["all_three_time_blocks_complete"] = False
    elif case == "stage_attempt_count":
        study["stage_evidence"]["validation"]["attempt_count"] = 600
    elif case == "budget_cap_drift":
        study["budget"]["caps"]["cost_usd"] = "301.00"
    elif case == "budget_exceeded":
        gate["usage"]["provider_invocations"] = 12_001
        study["budget"]["observed"]["provider_invocations"] = 12_001
        study["budget"]["within_caps"] = False
        gate["budget_within_caps"] = False
    elif case == "retry_invocation_cap":
        gate["usage"]["transport_attempts"] = 12_001
        study["budget"]["observed"]["transport_attempts"] = 12_001
        study["budget"]["within_caps"] = False
        gate["budget_within_caps"] = False
    elif case == "reservation_cap":
        study["budget"]["worst_case_reserved"]["transport_attempts"] = 12_001
        study["budget"]["worst_case_reserved"]["provider_invocations"] = 12_001
        study["budget"]["reservation_within_caps"] = False
        study["budget"]["within_caps"] = False
        gate["budget_within_caps"] = False
    elif case == "empty_usage":
        gate["usage"] = {}
    elif case == "runtime_parity":
        study["production_runtime"]["parity_verified"] = False
    elif case == "runtime_component_hash":
        study["production_runtime"]["component_hashes"]["validator_sha256"] = "f" * 64
    elif case == "selected_config":
        study["selected_configuration"]["config_id"] = "0" * 24
    elif case == "metric_gate_inconsistent":
        gate["validation"]["metrics"]["repository_fidelity"] = 0.9
    elif case == "top_summary_inconsistent":
        gate["operational_soak_passed"] = False
    else:  # pragma: no cover - keeps the table exhaustive for future edits
        raise AssertionError(case)
    return _redigest(gate)


@pytest.mark.parametrize(
    "case",
    [
        "wrong_campaign",
        "future_issuance",
        "overlong_expiry",
        "expired",
        "scope_alias",
        "scope_reordered",
        "missing_study_evidence",
        "minimal_validation",
        "minimal_hidden",
        "component_hash",
        "extra_hash_pin",
        "validation_scenario_count",
        "validation_attempt_multiplicity",
        "time_blocks_per_scenario",
        "one_judge",
        "judge_calibration",
        "missing_soak_fault",
        "circuit_breaker_not_tested",
        "trace_canary",
        "stage_time_block",
        "stage_attempt_count",
        "budget_cap_drift",
        "budget_exceeded",
        "retry_invocation_cap",
        "reservation_cap",
        "empty_usage",
        "runtime_parity",
        "runtime_component_hash",
        "selected_config",
        "metric_gate_inconsistent",
        "top_summary_inconsistent",
    ],
)
def test_campaign_v2_gate_rejects_minimal_forged_or_incomplete_evidence(
    case: str,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        evidence_guide._validate_release_gate(_weak_campaign_gate(case))


def test_campaign_v2_gate_accepts_only_complete_current_evidence() -> None:
    validated = evidence_guide._validate_release_gate(_enabled_v2_gate())
    assert validated["enabled"] is True
    assert validated["selected_agent_variant"] == "A1"
    assert all(validated["release_basis"]["automated_gate_results"].values())


def test_v2_nested_basis_cannot_bypass_complete_campaign_evidence() -> None:
    payload = {
        "schema_version": "2.0",
        "release_gate_version": evidence_guide.RELEASE_GATE_VERSION,
        "enabled": True,
        "decision": "ENABLE_LIVE_GUIDE",
        "selected_model": "ibm-granite/granite-4.1-8b",
        "selected_agent_variant": "A1",
        "reasons": [],
        "claim_boundary": (
            "A minimal nested release basis cannot establish the complete study evidence."
        ),
        "release_basis": _release_basis(
            decision="ENABLE_LIVE_GUIDE",
            agent_variant_id="A1",
            model_id="ibm-granite/granite-4.1-8b",
            provider_call_count=1,
            automated_gate_results={"minimal": True},
            production_scope=["demo", "standard_engineering"],
        ),
    }
    with pytest.raises(ValueError, match="RELEASE_GATE_INVALID"):
        evidence_guide._validate_release_gate(payload)


def test_campaign_v2_disabled_gate_is_ingested_without_enabling() -> None:
    gate = _enabled_v2_gate()
    gate.update(
        {
            "decision": "DISABLE_LIVE_GUIDE",
            "selected_model": None,
            "selected_architecture": None,
            "agent_variant_id": "D1",
            "production_scope": [],
        }
    )
    # Disabled historical evidence is still useful after its production window
    # and after implementation drift, but it can never authorize a model.
    issued = datetime.now(UTC) - timedelta(days=180)
    gate["issued_at"] = issued.isoformat()
    gate["expires_at"] = (issued + timedelta(days=90)).isoformat()
    gate["hash_pins"]["prompt_sha256"] = "f" * 64
    validated = evidence_guide._validate_release_gate(_redigest(gate))
    assert validated["enabled"] is False
    assert validated["selected_model"] is None
    assert validated["selected_agent_variant"] is None


def test_campaign_builder_disabled_receipt_matches_runtime_contract(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "campaign.sqlite")
    try:
        gate = build_release_gate(
            store=store,
            preregistration={
                "preregistration_sha256": "1" * 64,
                "corpus": {},
            },
            winner=None,
            hash_pins={
                "prompt_sha256": evidence_guide.PROMPT_SHA256,
                "response_schema_sha256": evidence_guide.RESPONSE_SCHEMA_SHA256,
                "tool_contract_sha256": evidence_guide.TOOL_CONTRACT_SHA256,
                "plan_registry_sha256": evidence_guide.PLAN_REGISTRY_SHA256,
                "protocol_sha256": evidence_guide.PROTOCOL_SHA256,
                "renderer_sha256": evidence_guide.RENDERER_SHA256,
                "validator_sha256": evidence_guide.VALIDATOR_SHA256,
                "dataset_sha256": "2" * 64,
                "judge_config_sha256": "3" * 64,
                "receipt_sha256": "4" * 64,
                "protocol_amendment_sha256": "5" * 64,
            },
        )
        validated = evidence_guide._validate_release_gate(gate)
    finally:
        store.close()
    assert validated["enabled"] is False
    assert (
        validated["campaign_receipt"]["study_evidence"]["production_runtime"]["parity_verified"]
        is False
    )
    assert validated["campaign_receipt"]["study_evidence"]["trace_canary"]["passed"] is False


def test_release_component_hashes_pin_implementation_not_only_version_labels() -> None:
    assert (
        evidence_guide.PROMPT_SHA256
        != hashlib.sha256(evidence_guide.PROMPT_VERSION.encode("utf-8")).hexdigest()
    )
    assert (
        evidence_guide.TOOL_CONTRACT_SHA256
        != hashlib.sha256(evidence_guide.TOOL_CONTRACT_VERSION.encode("utf-8")).hexdigest()
    )


def _write_v2_gate_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    gate_path = tmp_path / "release-gate.json"
    index_path = tmp_path / "release-gate-index.json"
    manifest_path = tmp_path / "manifest.json"
    gate_path.write_text(json.dumps(_enabled_v2_gate(), sort_keys=True), encoding="utf-8")
    manifest_path.write_text('{"dataset_version":"v2"}\n', encoding="utf-8")
    index = {
        "schema_version": "1.0",
        "release_gate_file": "release-gate.json",
        "release_gate_sha256": hashlib.sha256(gate_path.read_bytes()).hexdigest(),
        "corpus_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    index_path.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")
    monkeypatch.setattr(evidence_guide, "_V2_RELEASE_GATE_PATH", gate_path)
    monkeypatch.setattr(evidence_guide, "_V2_RELEASE_INDEX_PATH", index_path)
    monkeypatch.setattr(evidence_guide, "_V2_CORPUS_MANIFEST_PATH", manifest_path)
    return index


def test_v2_loader_validates_atomic_gate_index_and_corpus_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_v2_gate_index(tmp_path, monkeypatch)
    gate, error = evidence_guide._load_release_gate()
    assert error is None
    assert gate["enabled"] is True


@pytest.mark.parametrize(
    "corruption",
    ["missing_index", "gate_hash", "manifest_hash", "extra_key", "alternate_file"],
)
def test_v2_loader_fails_closed_on_partial_or_mismatched_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    index = _write_v2_gate_index(tmp_path, monkeypatch)
    index_path = tmp_path / "release-gate-index.json"
    if corruption == "missing_index":
        index_path.unlink()
    elif corruption == "gate_hash":
        index["release_gate_sha256"] = "f" * 64
    elif corruption == "manifest_hash":
        index["corpus_manifest_sha256"] = "f" * 64
    elif corruption == "extra_key":
        index["unregistered"] = True
    elif corruption == "alternate_file":
        index["release_gate_file"] = "../release-gate.json"
    if corruption != "missing_index":
        index_path.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")

    gate, error = evidence_guide._load_release_gate()
    assert error == "EVIDENCE_GUIDE_RELEASE_GATE_INVALID"
    assert gate["enabled"] is False
