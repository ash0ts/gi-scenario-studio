from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip(
    "nacl",
    reason="release-evidence signatures require the optional release dependency group",
)

COMMIT = "a" * 40
COORDINATOR_KEY_SHA256 = "c" * 64
TASKS = [
    "decision",
    "controlling_assumption",
    "safeguard_limitation",
    "evidence_source",
    "wandb_lineage",
    "next_feasibility_action",
]


def _load(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _acceptance() -> dict[str, object]:
    cases = []
    for index in range(6):
        outcomes = {task: True for task in TASKS}
        if index == 5:
            outcomes["wandb_lineage"] = False
        cases.append(
            {
                "participant_id": f"participant-{index + 1:012x}",
                "eligible_role_attested": True,
                "uncoached": True,
                "facilitator_help": False,
                "task_outcomes": outcomes,
                "case_record_sha256": f"{index + 1:064x}",
                "completed_at": f"2026-08-03T{index + 1:02d}:00:00Z",
            }
        )
    return {
        "schema_version": "1.1",
        "protocol_id": "ecocorridor-practitioner-acceptance-v1",
        "source_commit_sha": COMMIT,
        "application_version": "0.1.0",
        "participant_count": 6,
        "successful_participant_count": 5,
        "tasks": TASKS,
        "cases": cases,
        "anonymous": True,
        "contains_personal_data": False,
        "coordinator_key_sha256": COORDINATOR_KEY_SHA256,
        "study_coordinator_attestation": (
            "An independent study coordinator attests that the six anonymous case records "
            "were collected under the published uncoached protocol."
        ),
        "completed_at": "2026-08-03T12:00:00Z",
    }


def _assistant_evaluation(assistant: ModuleType) -> dict[str, object]:
    from ecocorridor_core.assistant_evaluation import (
        AssistantEvaluationCaseRecord,
        load_dataset,
        make_case_record,
        recompute_aggregate,
    )
    from ecocorridor_core.assistants import (
        AssistantEvidenceAuthorization,
        AssistantRunAssociation,
        AuthorizedEvidenceClaimContext,
        EvidenceQuestionIntent,
        render_deterministic_evidence_answer,
    )
    from ecocorridor_core.metrics import CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION
    from nacl.signing import SigningKey

    dataset, dataset_sha256 = load_dataset()
    records: list[AssistantEvaluationCaseRecord] = []
    for case in dataset.cases:
        association = AssistantRunAssociation.create(
            project_id="ecocorridor-cc0-assistant-evaluation",
            wandb_external_project_id="wandb-project-1",
            wandb_run_id="ecocorridor-assistant-evaluation-release-1",
            wandb_run_kind="assistant_evaluation",
            run_subject_id="assistant-evaluation-release-1",
            analysis_id=f"evaluation-analysis-{case.case_id}",
        )
        if case.expected_disposition == "REJECTED":
            error = (
                "ASSISTANT_PROMPT_INJECTION_REJECTED"
                if case.category in {"PROMPT_INJECTION", "FABRICATED_EVIDENCE"}
                else "ASSISTANT_EVALUATION_REJECTED"
            )
            answer = None
            disposition = "REJECTED"
            request = None
            aborted_trace = {
                "trace_id": f"trace-{case.case_id}",
                "trace_url": f"https://wandb.example/traces/{case.case_id}",
                "call_id": f"evaluation-call-{case.case_id}",
                "error_code": error,
                "steps": ["authorize_project"],
                "association": association.model_dump(mode="json"),
                "completed": False,
                "persisted": True,
                "trace_readback_verified": True,
            }
        else:
            authorization = AssistantEvidenceAuthorization.create(
                project_id_sha256="1" * 64,
                analysis_id_sha256="2" * 64,
                model_revision_id_sha256="3" * 64,
                destination="SELF_MANAGED",
                external_project_id_sha256="4" * 64,
                scope_evidence_sha256="5" * 64,
                snapshot_bundle_sha256="6" * 64,
                current_authorizations_sha256="7" * 64,
                selected_evidence_sha256="8" * 64,
                authorized_asset_count=1,
                generalized_derivative_count=0,
            )
            context = AuthorizedEvidenceClaimContext(
                evidence_alias="E1",
                claim_kind="DIRECT_PAIR_METRIC",
                value=case.value,
                units=case.units,
                direct_result=True,
                limitations=[CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION],
            )
            rendering = render_deterministic_evidence_answer(
                intent=EvidenceQuestionIntent.EXPLAIN_SELECTED_EVIDENCE,
                ordered_contexts=[context],
            )
            answer = {
                "answer_id": f"answer-{case.case_id}",
                "call_id": f"evaluation-call-{case.case_id}",
                "project_id": "ecocorridor-cc0-assistant-evaluation",
                "text": rendering.text,
                "evidence_ids": [f"evaluation:{case.case_id}"],
                "numeric_claims": [
                    {
                        "evidence_id": f"evaluation:{case.case_id}",
                        "value": case.value,
                        "units": case.units,
                    }
                ],
                "limitations": rendering.limitations,
                "provider_call_id": f"provider-{case.case_id}",
                "provider_model": "approved-provider/model-version",
                "trace_id": f"trace-{case.case_id}",
                "trace_url": f"https://wandb.example/traces/{case.case_id}",
                "association": association.model_dump(mode="json"),
                "evidence_authorization": authorization.model_dump(mode="json"),
                "deterministic_validation": "PASSED",
                "validator_version": "ecocorridor-assistant-validator-v1",
                "trace_readback_verified": True,
                "read_only": True,
            }
            disposition = "ANSWERED"
            error = None
            request = {
                "operation": "evidence.explain",
                "user_prompt": "EXPLAIN_SELECTED_EVIDENCE",
                "context": {
                    "authorization_decision_id": authorization.decision_id,
                    "claims": [context.model_dump(mode="json")],
                },
                "validated_provider_selection": {
                    "intent": "EXPLAIN_SELECTED_EVIDENCE",
                    "items": [
                        {
                            "evidence_id": "E1",
                            "explanation_kind": "DIRECT_PAIR_METRIC",
                        }
                    ],
                },
            }
            completed_trace = {
                "trace_id": f"trace-{case.case_id}",
                "trace_url": f"https://wandb.example/traces/{case.case_id}",
                "call_id": f"evaluation-call-{case.case_id}",
                "steps": [
                    "authorize_project",
                    "minimize_sensitive_context",
                    "resolve_local_evidence",
                    "construct_context",
                    "call_provider",
                    "extract_numeric_claims",
                    "validate_evidence_references",
                    "validate_units_and_limitations",
                    "format_response",
                    "record_feedback",
                ],
                "association": association.model_dump(mode="json"),
                "completed": True,
                "persisted": True,
                "trace_readback_verified": True,
            }
            aborted_trace = None
        if case.expected_disposition == "REJECTED":
            completed_trace = None
        records.append(
            make_case_record(
                case,
                disposition=disposition,
                answer=answer,
                error_code=error,
                provider_request=request,
                weave_evaluation_call_id="provider-created-evaluation-call-1",
                completed_trace=completed_trace,
                aborted_trace=aborted_trace,
            )
        )
    records_payload: dict[str, object] = {
        "schema_version": "ecocorridor-assistant-case-records/v1",
        "evaluation_id": "assistant-evaluation-release-1",
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": dataset_sha256,
        "records": [item.model_dump(mode="json") for item in records],
    }
    encoded = (json.dumps(records_payload, indent=2, sort_keys=True) + "\n").encode()
    rated_records = [item for item in records if item.disposition == "ANSWERED"][:5]
    ratings = {item.case_id: "HELPFUL" for item in rated_records}
    aggregate = recompute_aggregate(dataset, records, ratings=ratings)
    ratings_payload = {
        "schema_version": "ecocorridor-assistant-ratings/v3",
        "source_commit_sha": COMMIT,
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": dataset_sha256,
        "provider_model": "approved-provider/model-version",
        "evaluation_id": "assistant-evaluation-release-1",
        "wandb_run_id": "ecocorridor-assistant-evaluation-release-1",
        "weave_evaluation_call_id": "provider-created-evaluation-call-1",
        "anonymous": True,
        "contains_personal_data": False,
        "ratings": [
            {
                "case_id": item.case_id,
                "case_input_sha256": item.case_input_sha256,
                "case_record_sha256": item.record_sha256,
                "answer_sha256": hashlib.sha256(
                    json.dumps(
                        item.answer,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ).encode("utf-8")
                ).hexdigest(),
                "trace_id": item.answer["trace_id"],
                "provider_call_id": item.answer["provider_call_id"],
                "provider_model": item.answer["provider_model"],
                "rating": ratings[item.case_id],
                "reviewer_role_attested": True,
                "uncoached": True,
                "completed_at": "2026-08-03T12:00:00Z",
            }
            for item in rated_records
        ],
    }
    ratings_bytes = (json.dumps(ratings_payload, indent=2, sort_keys=True) + "\n").encode()
    private_key = SigningKey.generate()
    public_key = private_key.verify_key.encode()
    ratings_signature = private_key.sign(ratings_bytes).signature
    public_key_b64 = base64.b64encode(public_key).decode()
    public_key_sha256 = hashlib.sha256(public_key).hexdigest()
    report: dict[str, object] = {
        "schema_version": "2.0",
        "source_commit_sha": COMMIT,
        "evaluation_id": "assistant-evaluation-release-1",
        "provider_model": "approved-provider/model-version",
        "provider_call_count": sum(item.disposition == "ANSWERED" for item in records),
        "wandb_external_project_id": "wandb-project-1",
        "wandb_run_id": "ecocorridor-assistant-evaluation-release-1",
        "weave_evaluation_url": "https://wandb.example/evaluations/release-1",
        "weave_evaluation_call_id": "provider-created-evaluation-call-1",
        "weave_dataset_ref": "weave:///team/project/object/ecocorridor-evidence:abcdefghijklmnopqrstuvwx",
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": dataset_sha256,
        "case_records_sha256": hashlib.sha256(encoded).hexdigest(),
        "case_records_merkle_root": assistant._merkle_root(records),
        "ratings_sha256": hashlib.sha256(ratings_bytes).hexdigest(),
        "ratings_signature_sha256": hashlib.sha256(ratings_signature).hexdigest(),
        "ratings_signing_key_sha256": public_key_sha256,
        "aggregate": aggregate,
        "completed_at": "2026-08-03T12:00:00Z",
    }
    return {
        "report": report,
        "records": records_payload,
        "encoded": encoded,
        "ratings": ratings_bytes,
        "ratings_signature": ratings_signature,
        "public_key_b64": public_key_b64,
        "public_key_sha256": public_key_sha256,
    }


def _validate_assistant(assistant: ModuleType, fixture: dict[str, object], **kwargs: object) -> int:
    return assistant.validate(
        fixture["report"],
        fixture["records"],
        case_records_bytes=fixture["encoded"],
        ratings_bytes=fixture["ratings"],
        ratings_signature=fixture["ratings_signature"],
        ratings_public_key_b64=fixture["public_key_b64"],
        expected_ratings_key_sha256=fixture["public_key_sha256"],
        **kwargs,
    )


def test_release_evidence_validators_bind_to_exact_source() -> None:
    acceptance = _load("validate_ecocorridor_acceptance.py")
    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)

    assert acceptance.validate(
        _acceptance(),
        expected_commit=COMMIT,
        expected_version="v0.1.0",
        expected_coordinator_key_sha256=COORDINATOR_KEY_SHA256,
    ) == (6, 5)
    assert _validate_assistant(assistant, evaluation, expected_commit=COMMIT) == 12

    with pytest.raises(ValueError, match="different source commit"):
        acceptance.validate(
            _acceptance(),
            expected_commit="b" * 40,
            expected_coordinator_key_sha256=COORDINATOR_KEY_SHA256,
        )
    with pytest.raises(ValueError, match="different source commit"):
        _validate_assistant(assistant, evaluation, expected_commit="b" * 40)


def test_practitioner_evidence_rejects_boolean_counts_and_extra_personal_fields() -> None:
    acceptance = _load("validate_ecocorridor_acceptance.py")
    payload = _acceptance()
    payload["participant_count"] = True
    with pytest.raises(ValueError, match="does not reconcile"):
        acceptance.validate(payload, expected_coordinator_key_sha256=COORDINATOR_KEY_SHA256)

    payload = _acceptance()
    payload["participant_names"] = ["must not be published"]
    with pytest.raises(ValueError, match="exactly the public release fields"):
        acceptance.validate(payload, expected_coordinator_key_sha256=COORDINATOR_KEY_SHA256)


def test_practitioner_evidence_recomputes_case_level_results_and_key_binding() -> None:
    acceptance = _load("validate_ecocorridor_acceptance.py")

    aggregate_only = _acceptance()
    aggregate_only.pop("cases")
    with pytest.raises(ValueError, match="exactly the public release fields"):
        acceptance.validate(aggregate_only, expected_coordinator_key_sha256=COORDINATOR_KEY_SHA256)

    mismatched = _acceptance()
    mismatched["successful_participant_count"] = 6
    with pytest.raises(ValueError, match="does not reconcile"):
        acceptance.validate(mismatched, expected_coordinator_key_sha256=COORDINATOR_KEY_SHA256)

    duplicate = _acceptance()
    duplicate["cases"][1]["participant_id"] = duplicate["cases"][0]["participant_id"]
    with pytest.raises(ValueError, match="must be unique"):
        acceptance.validate(duplicate, expected_coordinator_key_sha256=COORDINATOR_KEY_SHA256)

    with pytest.raises(ValueError, match="approved coordinator key"):
        acceptance.validate(_acceptance(), expected_coordinator_key_sha256="d" * 64)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("numeric_faithful_rate", 0.99),
        ("unit_consistent_rate", 0.99),
        ("required_limitations_present_rate", 0.99),
        ("deterministic_text_faithful_rate", 0.99),
        ("unauthorized_action_count", 1),
        ("fabricated_evidence_rejected_rate", 0.99),
        ("prompt_injection_rejected_rate", 0.99),
        ("sensitive_location_leak_count", 1),
        ("trace_readback_complete", False),
    ],
)
def test_assistant_evaluation_thresholds_fail_closed(field: str, value: object) -> None:
    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)
    evaluation["report"]["aggregate"][field] = value
    with pytest.raises(ValueError):
        _validate_assistant(assistant, evaluation)


def test_assistant_evaluation_requires_meaningful_usefulness_sample() -> None:
    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)
    evaluation["report"]["aggregate"]["practitioner_rating_count"] = 4
    evaluation["report"]["aggregate"]["practitioner_positive_rating_count"] = 4
    with pytest.raises(ValueError):
        _validate_assistant(assistant, evaluation)


def test_assistant_evaluation_rejects_aggregate_only_or_changed_raw_case() -> None:
    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)

    with pytest.raises(ValueError, match="raw case-record"):
        assistant.validate(evaluation["report"], None)

    evaluation["records"]["records"][0]["answer"]["numeric_claims"][0]["value"] = 999.0
    with pytest.raises(ValueError):
        _validate_assistant(assistant, evaluation)


def test_assistant_evaluation_detects_returned_text_tampering() -> None:
    from ecocorridor_core.assistant_evaluation import derive_case_scores, load_dataset

    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)
    dataset, _ = load_dataset()
    record = next(
        item for item in evaluation["records"]["records"] if item["disposition"] == "ANSWERED"
    )
    case = next(item for item in dataset.cases if item.case_id == record["case_id"])
    tampered_answer = dict(record["answer"])
    tampered_answer["text"] += " Field surveys show this intervention reconnects habitat."

    scores = derive_case_scores(
        case,
        disposition="ANSWERED",
        answer=tampered_answer,
        error_code=None,
        provider_request=record["provider_request"],
        completed_trace=record["completed_trace"],
        aborted_trace=None,
    )

    assert scores["deterministic_text_faithful"] is False


def test_assistant_evaluation_rejects_unsigned_or_wrong_signing_key() -> None:
    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)

    evaluation["ratings_signature"] = b"x" * 64
    with pytest.raises(ValueError, match="signature"):
        _validate_assistant(assistant, evaluation)


def test_assistant_usefulness_rating_is_bound_to_exact_answer_and_trace() -> None:
    from nacl.signing import SigningKey

    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)
    payload = json.loads(evaluation["ratings"])
    payload["ratings"][0]["trace_id"] = "trace-from-an-unrelated-answer"
    ratings_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    private_key = SigningKey.generate()
    public_key = private_key.verify_key.encode()
    signature = private_key.sign(ratings_bytes).signature
    evaluation["ratings"] = ratings_bytes
    evaluation["ratings_signature"] = signature
    evaluation["public_key_b64"] = base64.b64encode(public_key).decode()
    evaluation["public_key_sha256"] = hashlib.sha256(public_key).hexdigest()
    evaluation["report"]["ratings_sha256"] = hashlib.sha256(ratings_bytes).hexdigest()
    evaluation["report"]["ratings_signature_sha256"] = hashlib.sha256(signature).hexdigest()
    evaluation["report"]["ratings_signing_key_sha256"] = evaluation["public_key_sha256"]

    with pytest.raises(ValueError, match="exact evaluated answer"):
        _validate_assistant(assistant, evaluation)


def test_rejected_case_is_not_trace_verified_without_aborted_readback() -> None:
    from ecocorridor_core.assistant_evaluation import derive_case_scores, load_dataset

    dataset, _ = load_dataset()
    case = next(item for item in dataset.cases if item.category == "PROMPT_INJECTION")
    scores = derive_case_scores(
        case,
        disposition="REJECTED",
        answer=None,
        error_code="ASSISTANT_PROMPT_INJECTION_REJECTED",
        provider_request=None,
        completed_trace=None,
        aborted_trace=None,
    )

    assert scores["trace_readback_verified"] is False
    assert scores["prompt_injection_rejected"] is False
    assert scores["disposition_match"] is False


def test_pending_assistant_evaluation_finalizes_without_provider_rerun() -> None:
    from ecocorridor_core.assistant_evaluation import (
        AssistantEvaluationCaseRecord,
        load_dataset,
        recompute_aggregate,
    )

    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)
    dataset, _ = load_dataset()
    records = [
        AssistantEvaluationCaseRecord.model_validate(item)
        for item in evaluation["records"]["records"]
    ]
    final = evaluation["report"]
    pending = {
        "schema_version": "ecocorridor-assistant-evaluation-pending/v1",
        **{
            key: value
            for key, value in final.items()
            if key
            not in {
                "schema_version",
                "ratings_sha256",
                "ratings_signature_sha256",
                "ratings_signing_key_sha256",
                "aggregate",
            }
        },
        "deterministic_aggregate": recompute_aggregate(dataset, records),
        "practitioner_usefulness_status": "NOT_COLLECTED",
        "release_eligible": False,
    }

    finalized = assistant.finalize_pending(
        pending,
        evaluation["records"],
        expected_commit=COMMIT,
        dataset_path=assistant.DATASET_PATH,
        manifest_path=assistant.MANIFEST_PATH,
        case_records_bytes=evaluation["encoded"],
        ratings_bytes=evaluation["ratings"],
        ratings_signature=evaluation["ratings_signature"],
        ratings_public_key_b64=evaluation["public_key_b64"],
        expected_ratings_key_sha256=evaluation["public_key_sha256"],
    )

    assert finalized == final


def test_release_validator_rejects_duplicate_or_fallback_provider_call_ids() -> None:
    from ecocorridor_core.assistant_evaluation import AssistantEvaluationCaseRecord

    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)
    records = [
        AssistantEvaluationCaseRecord.model_validate(item)
        for item in evaluation["records"]["records"]
    ]
    answered = [item for item in records if item.answer is not None]
    duplicate_answer = {
        **answered[1].answer,
        "provider_call_id": answered[0].answer["provider_call_id"],
    }
    duplicated = [
        item.model_copy(update={"answer": duplicate_answer}) if item is answered[1] else item
        for item in records
    ]
    with pytest.raises(ValueError, match="unique"):
        assistant._validate_case_identities(evaluation["report"], duplicated)

    fallback_answer = {
        **answered[0].answer,
        "provider_call_id": "provider-call-unavailable",
    }
    fallback = [
        item.model_copy(update={"answer": fallback_answer}) if item is answered[0] else item
        for item in records
    ]
    with pytest.raises(ValueError, match="provider-issued"):
        assistant._validate_case_identities(evaluation["report"], fallback)


def test_trace_evidence_requires_typed_association_and_exact_step_order() -> None:
    from ecocorridor_core.assistant_evaluation import (
        AssistantAbortedTraceEvidence,
        AssistantEvaluationCaseRecord,
    )
    from ecocorridor_core.assistants import AssistantRunAssociation

    assistant = _load("validate_ecocorridor_assistant_evaluation.py")
    evaluation = _assistant_evaluation(assistant)
    records = [
        AssistantEvaluationCaseRecord.model_validate(item)
        for item in evaluation["records"]["records"]
    ]
    rejected = next(item for item in records if item.aborted_trace is not None)
    malformed = rejected.aborted_trace.model_dump(mode="json")
    malformed["steps"] = ["authorize_project", "construct_context"]
    with pytest.raises(ValueError, match="ordered step prefix"):
        AssistantAbortedTraceEvidence.model_validate(malformed)

    answered = next(item for item in records if item.completed_trace is not None)
    wrong_association = AssistantRunAssociation.create(
        project_id="another-project",
        wandb_external_project_id="wandb-project-1",
        wandb_run_id="ecocorridor-assistant-evaluation-release-1",
        wandb_run_kind="assistant_evaluation",
        run_subject_id="assistant-evaluation-release-1",
        analysis_id=f"evaluation-analysis-{answered.case_id}",
    )
    wrong_trace = answered.completed_trace.model_copy(update={"association": wrong_association})
    changed = [
        item.model_copy(update={"completed_trace": wrong_trace}) if item is answered else item
        for item in records
    ]
    with pytest.raises(ValueError, match="trace association"):
        assistant._validate_case_identities(evaluation["report"], changed)


def _actions_records() -> tuple[dict[str, object], dict[str, object]]:
    run_id = 321
    artifact_id = 654
    repository = "ash0ts/gi-scenario-studio"
    commit = "b" * 40
    run = {
        "id": run_id,
        "workflow_id": 987,
        "run_attempt": 2,
        "path": ".github/workflows/ecocorridor-assistant-evaluation.yml@main",
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": commit,
        "html_url": f"https://github.com/{repository}/actions/runs/{run_id}/attempts/2",
        "repository": {"full_name": repository},
    }
    artifacts = {
        "total_count": 1,
        "artifacts": [
            {
                "id": artifact_id,
                "name": "ecocorridor-assistant-evaluation-candidate",
                "size_in_bytes": 4096,
                "expired": False,
                "digest": "sha256:" + "c" * 64,
                "workflow_run": {
                    "id": run_id,
                    "head_branch": "main",
                    "head_sha": commit,
                },
            }
        ],
    }
    return run, artifacts


def test_actions_provenance_binds_workflow_attempt_and_artifact_digest() -> None:
    resolver = _load("resolve_github_actions_provenance.py")
    run, artifacts = _actions_records()
    provenance = resolver.resolve(
        run,
        artifacts,
        repository="ash0ts/gi-scenario-studio",
        expected_run_id=321,
        expected_sha="b" * 40,
        expected_workflow_path=".github/workflows/ecocorridor-assistant-evaluation.yml",
        expected_artifact_name="ecocorridor-assistant-evaluation-candidate",
    )

    assert provenance["run_attempt"] == 2
    assert provenance["workflow_id"] == 987
    assert provenance["workflow_path"].endswith("ecocorridor-assistant-evaluation.yml")
    assert provenance["artifact"] == {
        "id": 654,
        "name": "ecocorridor-assistant-evaluation-candidate",
        "digest": "sha256:" + "c" * 64,
        "size_in_bytes": 4096,
    }

    wrong_path = {**run, "path": ".github/workflows/lookalike.yml@main"}
    with pytest.raises(ValueError, match="exact protected source"):
        resolver.resolve(
            wrong_path,
            artifacts,
            repository="ash0ts/gi-scenario-studio",
            expected_run_id=321,
            expected_sha="b" * 40,
            expected_workflow_path=".github/workflows/ecocorridor-assistant-evaluation.yml",
            expected_artifact_name="ecocorridor-assistant-evaluation-candidate",
        )
    missing_digest = json.loads(json.dumps(artifacts))
    missing_digest["artifacts"][0]["digest"] = None
    with pytest.raises(ValueError, match="exact protected workflow run"):
        resolver.resolve(
            run,
            missing_digest,
            repository="ash0ts/gi-scenario-studio",
            expected_run_id=321,
            expected_sha="b" * 40,
            expected_workflow_path=".github/workflows/ecocorridor-assistant-evaluation.yml",
            expected_artifact_name="ecocorridor-assistant-evaluation-candidate",
        )


def test_actions_run_only_provenance_binds_path_attempt_event_and_canonical_url() -> None:
    resolver = _load("resolve_github_actions_provenance.py")
    run, _ = _actions_records()
    run["path"] = ".github/workflows/ci.yml"
    run["event"] = "push"

    provenance = resolver.resolve_run(
        run,
        repository="ash0ts/gi-scenario-studio",
        expected_run_id=321,
        expected_sha="b" * 40,
        expected_workflow_path=".github/workflows/ci.yml",
        expected_events=("push", "workflow_dispatch"),
    )

    assert provenance["schema_version"] == ("ecocorridor-github-actions-run-provenance/v1.0")
    assert provenance["run_attempt"] == 2
    assert provenance["workflow_id"] == 987
    assert provenance["workflow_path"] == ".github/workflows/ci.yml"
    assert provenance["event"] == "push"
    assert "artifact" not in provenance

    forged_url = {**run, "html_url": run["html_url"] + "/forged"}
    with pytest.raises(ValueError, match="exact protected source"):
        resolver.resolve_run(
            forged_url,
            repository="ash0ts/gi-scenario-studio",
            expected_run_id=321,
            expected_sha="b" * 40,
            expected_workflow_path=".github/workflows/ci.yml",
            expected_events=("push", "workflow_dispatch"),
        )

    with pytest.raises(ValueError, match="exact protected source"):
        resolver.resolve_run(
            run,
            repository="ash0ts/gi-scenario-studio",
            expected_run_id=321,
            expected_sha="b" * 40,
            expected_workflow_path=".github/workflows/ecocorridor-ci.yml",
            expected_events=("push", "workflow_dispatch"),
        )


def test_embedded_producer_identity_binds_artifact_to_exact_run_attempt() -> None:
    resolver = _load("resolve_github_actions_provenance.py")
    producer = _load("github_actions_producer_identity.py")
    run, artifacts = _actions_records()
    provenance = resolver.resolve(
        run,
        artifacts,
        repository="ash0ts/gi-scenario-studio",
        expected_run_id=321,
        expected_sha="b" * 40,
        expected_workflow_path=".github/workflows/ecocorridor-assistant-evaluation.yml",
        expected_artifact_name="ecocorridor-assistant-evaluation-candidate",
    )
    identity = producer.create_identity(
        repository="ash0ts/gi-scenario-studio",
        run_id=321,
        run_attempt=2,
        workflow_path=".github/workflows/ecocorridor-assistant-evaluation.yml",
        git_ref="refs/heads/main",
        workflow_ref=(
            "ash0ts/gi-scenario-studio/"
            ".github/workflows/ecocorridor-assistant-evaluation.yml@refs/heads/main"
        ),
        workflow_sha="b" * 40,
        head_sha="b" * 40,
    )

    assert producer.verify_identity(identity, provenance) == identity

    stale_attempt = {**identity, "run_attempt": 1}
    with pytest.raises(ValueError, match="run_attempt"):
        producer.verify_identity(stale_attempt, provenance)

    wrong_workflow_source = {**identity, "workflow_sha": "d" * 40}
    with pytest.raises(ValueError, match="workflow_sha"):
        producer.verify_identity(wrong_workflow_source, provenance)


def test_producer_identity_rejects_a_forged_workflow_ref() -> None:
    producer = _load("github_actions_producer_identity.py")
    with pytest.raises(ValueError, match="workflow ref"):
        producer.create_identity(
            repository="ash0ts/gi-scenario-studio",
            run_id=321,
            run_attempt=2,
            workflow_path=".github/workflows/ecocorridor-pages.yml",
            git_ref="refs/heads/main",
            workflow_ref=(
                "ash0ts/gi-scenario-studio/.github/workflows/lookalike.yml@refs/heads/main"
            ),
            workflow_sha="b" * 40,
            head_sha="b" * 40,
        )
