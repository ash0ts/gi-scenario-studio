from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from gi_scenario_core.evidence_candidate_evaluation import (
    CANDIDATE_MODELS,
    attach_practitioner_approval,
    evaluate_candidate,
    evaluation_plan,
    practitioner_approval_binding,
    release_decision,
)
from gi_scenario_core.evidence_evaluation import load_dataset
from gi_scenario_core.inference import InferenceCompletion, InferenceUsage

ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = ROOT / "benchmarks" / "evidence_guide" / "v1" / "dataset.json"
SCRIPT = ROOT / "scripts" / "run_evidence_guide_candidate_evaluation.py"


class ExactFakeProvider:
    def __init__(self, dataset: dict, *, fail_case: str | None = None) -> None:
        self.dataset = dataset
        self.fail_case = fail_case
        self.calls: list[dict] = []

    def complete(self, **kwargs) -> InferenceCompletion:
        packet = json.loads(kwargs["messages"][1]["content"])
        case = next(row for row in self.dataset["rows"] if row["case_id"] == packet["case_id"])
        self.calls.append(kwargs)
        if case["case_id"] == self.fail_case:
            raise RuntimeError("provider included a secret-looking diagnostic that must be redacted")
        expectations = case["expectations"]
        selection = {
            "decision": expectations["response_mode"],
            "plain_language": (
                case["context"]["refusal_answer"]
                if expectations["response_mode"] == "refuse"
                else "The repository evidence supports this bounded modeled explanation."
            ),
            "evidence_ids": expectations.get("required_evidence_ids", []),
            "numeric_claim_ids": expectations.get("required_numeric_evidence_ids", []),
            "limitation_keys": expectations.get("required_limitation_keys", []),
            "actions": expectations.get("required_actions", []),
        }
        return InferenceCompletion(
            provider_call_id=f"provider-{case['case_id']}",
            model=kwargs["model"],
            content=json.dumps(selection),
            usage=InferenceUsage(input_tokens=100, output_tokens=20, total_tokens=120),
            finish_reason="stop",
        )


class TerminalFakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, **_kwargs) -> InferenceCompletion:
        self.calls += 1
        raise RuntimeError("INFERENCE_CREDITS_REQUIRED")


class PartialProtectedCaseProvider(ExactFakeProvider):
    def complete(self, **kwargs) -> InferenceCompletion:
        completion = super().complete(**kwargs)
        packet = json.loads(kwargs["messages"][1]["content"])
        if packet["case_id"] != "eg-refusal-007":
            return completion
        selection = json.loads(completion.content)
        selection["limitation_keys"] = []
        return InferenceCompletion(
            provider_call_id=completion.provider_call_id,
            model=completion.model,
            content=json.dumps(selection),
            usage=completion.usage,
            finish_reason=completion.finish_reason,
        )


class ControlledHmacVerifier:
    """Test stand-in for a protected release-process signature verifier."""

    key_id = "test-controlled-practitioner-review-v1"

    def __init__(self, secret: bytes) -> None:
        self.secret = secret

    def verify(self, *, key_id: str, payload: bytes, signature: str) -> bool:
        expected = hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        return key_id == self.key_id and hmac.compare_digest(expected, signature)


def _ratings(dataset: dict) -> dict:
    return {
        "schema_version": "1.0",
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["dataset_version"],
        "ratings": [
            {
                "model_id": model,
                "case_id": case["case_id"],
                "reviewer_id_sha256": hashlib.sha256(
                    f"reviewer-{reviewer}".encode()
                ).hexdigest(),
                "helpful": True,
            }
            for model in CANDIDATE_MODELS
            for case in dataset["rows"]
            for reviewer in range(5)
        ],
    }


def _run(dataset: dict, model: str, *, ratings=None, provider=None):
    return evaluate_candidate(
        dataset=dataset,
        dataset_path=DATASET_PATH,
        provider=provider or ExactFakeProvider(dataset),
        entity="synthetic-team",
        project="restricted-evaluation",
        model_id=model,
        practitioner_ratings=ratings,
    )


def _approval_receipt(
    run,
    dataset: dict,
    verifier: ControlledHmacVerifier,
) -> dict:
    binding = practitioner_approval_binding(
        run,
        dataset=dataset,
        dataset_path=DATASET_PATH,
    )
    receipt = {
        "schema_version": "1.0",
        "approval_id": f"controlled-review-{run.model_id}",
        "issued_at": "2026-08-06T12:00:00+00:00",
        "key_id": verifier.key_id,
        "binding": binding,
    }
    payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    receipt["signature"] = hmac.new(
        verifier.secret,
        payload,
        hashlib.sha256,
    ).hexdigest()
    return receipt


def test_dry_run_plan_covers_both_candidates_without_credentials_or_network(tmp_path: Path) -> None:
    # This is the immutable v1 development campaign.  The production runtime may
    # support additional models for later protocols without rewriting v1 history.
    assert CANDIDATE_MODELS == (
        "ibm-granite/granite-4.1-8b",
        "openai/gpt-oss-20b",
    )
    environment = dict(os.environ)
    environment.pop("WANDB_API_KEY", None)
    environment["PYTHONPATH"] = str(ROOT / "python")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dry-run",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    output = json.loads(result.stdout)
    plan = json.loads((tmp_path / "evaluation-plan.json").read_text(encoding="utf-8"))
    assert output["network_calls"] == 0
    assert plan["candidate_models"] == list(CANDIDATE_MODELS)
    assert plan["case_count"] == 48
    assert plan["planned_provider_calls"] == 96
    assert plan["practitioner_gate"]["controlled_approval_receipt_required"] is True
    assert plan["practitioner_gate"]["unverified_ratings_behavior"] == "DISABLE_LIVE_GUIDE"
    assert plan["reference_projection_allowed"] is False
    assert "WANDB_API_KEY" not in result.stdout


def test_candidate_run_uses_strict_schema_and_materializes_canonical_values() -> None:
    dataset = load_dataset(DATASET_PATH)
    provider = ExactFakeProvider(dataset)

    run = _run(dataset, CANDIDATE_MODELS[0], ratings=_ratings(dataset), provider=provider)

    assert len(provider.calls) == 48
    assert all(call["response_schema"]["additionalProperties"] is False for call in provider.calls)
    assert all(call.get("tools") is None for call in provider.calls)
    assert all("/Users/" not in call["messages"][1]["content"] for call in provider.calls)
    assert run.evaluation["summary"]["release_gate_passed"] is True
    assert run.receipt["release_gate"]["passed"] is False
    gates = run.receipt["release_gate"]["gates"]
    assert gates["practitioner_usefulness_evidence"] is True
    assert gates["controlled_practitioner_approval"] is False
    assert all(
        value
        for name, value in gates.items()
        if name != "controlled_practitioner_approval"
    )
    assert run.receipt["provider_call_count"] == 48
    assert run.receipt["usage"] == {
        "input_tokens": 4800,
        "output_tokens": 960,
        "total_tokens": 5760,
    }
    assert run.receipt["latency_ms"]["count"] == 48
    assert run.receipt["reference_projection"] is False
    first = run.outputs["eg-exact-001"]
    assert first["numeric_claims"][0]["value"] == 3.21
    assert first["numeric_claims"][0]["units"] == "cfs"
    assert first["provider_generated"] is True


def test_candidate_schema_uses_the_documented_wandb_structured_output_subset() -> None:
    dataset = load_dataset(DATASET_PATH)
    provider = ExactFakeProvider(dataset)

    _run(dataset, CANDIDATE_MODELS[0], ratings=_ratings(dataset), provider=provider)

    encoded = json.dumps(provider.calls[0]["response_schema"], sort_keys=True)
    for unsupported in ("minItems", "maxItems", "uniqueItems", "minLength", "maxLength"):
        assert unsupported not in encoded


def test_release_gate_fails_closed_without_practitioner_evidence() -> None:
    dataset = load_dataset(DATASET_PATH)
    runs = [_run(dataset, model) for model in CANDIDATE_MODELS]

    decision = release_decision(runs, dataset=dataset, dataset_path=DATASET_PATH)

    assert decision["enabled"] is False
    assert decision["selected_model"] is None
    assert decision["decision"] == "DISABLE_LIVE_GUIDE"
    assert all(
        run.receipt["practitioner_usefulness"]["status"] == "not_measured"
        for run in runs
    )


def test_reviewer_hashes_alone_cannot_enable_the_release_gate() -> None:
    dataset = load_dataset(DATASET_PATH)
    ratings = _ratings(dataset)
    runs = [_run(dataset, model, ratings=ratings) for model in CANDIDATE_MODELS]

    decision = release_decision(runs, dataset=dataset, dataset_path=DATASET_PATH)

    assert all(
        run.receipt["practitioner_usefulness"]["passed"] is True for run in runs
    )
    assert all(run.receipt["release_gate"]["passed"] is False for run in runs)
    assert decision["enabled"] is False
    assert decision["selected_model"] is None
    assert decision["decision"] == "DISABLE_LIVE_GUIDE"


def test_release_gate_requires_and_reverifies_controlled_approval_receipts() -> None:
    dataset = load_dataset(DATASET_PATH)
    ratings = _ratings(dataset)
    verifier = ControlledHmacVerifier(b"test-only-controlled-release-key")
    runs = []
    for model in CANDIDATE_MODELS:
        unapproved = _run(dataset, model, ratings=ratings)
        runs.append(
            attach_practitioner_approval(
                unapproved,
                dataset=dataset,
                dataset_path=DATASET_PATH,
                approval_receipt=_approval_receipt(unapproved, dataset, verifier),
                verifier=verifier,
            )
        )

    without_verifier = release_decision(
        runs,
        dataset=dataset,
        dataset_path=DATASET_PATH,
    )
    approved = release_decision(
        runs,
        dataset=dataset,
        dataset_path=DATASET_PATH,
        practitioner_approval_verifier=verifier,
    )

    assert without_verifier["decision"] == "DISABLE_LIVE_GUIDE"
    assert approved["enabled"] is True
    assert approved["selected_model"] == "ibm-granite/granite-4.1-8b"
    assert approved["decision"] == "ENABLE_LIVE_GUIDE"


def test_controlled_approval_is_bound_to_exact_evidence_and_signature() -> None:
    dataset = load_dataset(DATASET_PATH)
    run = _run(dataset, CANDIDATE_MODELS[0], ratings=_ratings(dataset))
    verifier = ControlledHmacVerifier(b"test-only-controlled-release-key")
    receipt = _approval_receipt(run, dataset, verifier)

    receipt["binding"]["dataset_version"] = "invented"
    with pytest.raises(ValueError, match="controlled verification"):
        attach_practitioner_approval(
            run,
            dataset=dataset,
            dataset_path=DATASET_PATH,
            approval_receipt=receipt,
            verifier=verifier,
        )


def test_provider_failure_is_redacted_and_disables_candidate() -> None:
    dataset = load_dataset(DATASET_PATH)
    provider = ExactFakeProvider(dataset, fail_case="eg-refusal-008")

    run = _run(dataset, CANDIDATE_MODELS[0], ratings=_ratings(dataset), provider=provider)

    assert run.receipt["release_gate"]["passed"] is False
    assert run.receipt["provider_failures"] == [
        {"case_id": "eg-refusal-008", "error_code": "CANDIDATE_RUNTIMEERROR"}
    ]
    assert "secret-looking" not in json.dumps(run.receipt)


def test_protected_cases_must_pass_completely_not_only_enter_refusal_mode() -> None:
    dataset = load_dataset(DATASET_PATH)
    run = _run(
        dataset,
        CANDIDATE_MODELS[0],
        ratings=_ratings(dataset),
        provider=PartialProtectedCaseProvider(dataset),
    )
    fabricated = next(
        item for item in run.evaluation["case_results"] if item["category"] == "fabricated_run"
    )

    assert fabricated["scores"]["refusal_behavior"] == 1.0
    assert fabricated["passed"] is False
    assert (
        run.receipt["release_gate"]["gates"]["safe_injection_and_fabrication_handling"]
        is False
    )


def test_terminal_provider_failure_stops_repeated_requests_and_fails_closed() -> None:
    dataset = load_dataset(DATASET_PATH)
    provider = TerminalFakeProvider()

    run = _run(dataset, CANDIDATE_MODELS[0], provider=provider)

    assert provider.calls == 1
    assert run.receipt["provider_call_count"] == 1
    assert run.receipt["skipped_case_count"] == 47
    assert run.receipt["provider_failures"] == [
        {"case_id": "eg-exact-001", "error_code": "INFERENCE_CREDITS_REQUIRED"}
    ]
    assert run.receipt["release_gate"]["passed"] is False


def test_plan_and_release_decision_require_complete_allowlisted_campaign() -> None:
    dataset = load_dataset(DATASET_PATH)
    plan = evaluation_plan(dataset, dataset_path=DATASET_PATH)
    one_run = _run(dataset, CANDIDATE_MODELS[0], ratings=_ratings(dataset))

    decision = release_decision([one_run], dataset=dataset, dataset_path=DATASET_PATH)

    assert plan["strict_structured_output"] is True
    assert decision["enabled"] is False
    assert CANDIDATE_MODELS[1] in decision["reasons"][0]


def test_release_gate_rejects_receipt_or_reference_projection_tampering() -> None:
    dataset = load_dataset(DATASET_PATH)
    run = _run(dataset, CANDIDATE_MODELS[0], ratings=_ratings(dataset))
    run.receipt["reference_projection"] = True

    with pytest.raises(ValueError, match="integrity validation"):
        release_decision([run], dataset=dataset, dataset_path=DATASET_PATH)
