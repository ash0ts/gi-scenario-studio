from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from gi_scenario_core.evidence_guide_v2_architecture_dev import (
    RESEARCH_STAGES,
    TERMINAL_DISPOSITION,
    ResearchAttemptSpec,
    ResearchFakeExecutor,
    analyze_research_rows,
    apply_predecessor_debits,
    apply_r2b_debit,
    build_research_preregistration,
    build_study_schedule,
    exact_mcnemar,
    extract_research_branches,
    finalize_exploratory_study,
    schedule_counts,
    source_hashes_for_study,
    study_instance_sha256,
    validate_r1_architecture_incident_receipt,
    validate_r2b_terminal_receipt,
)
from gi_scenario_core.evidence_guide_v2_architecture_executor import (
    RESEARCH_PROVIDER_INPUT_TOKEN_CAP,
    RESEARCH_REQUEST_BYTE_CAP,
    _conditioned_causal_evidence,
    audit_research_request_envelopes,
    build_semantic_intervention_receipt,
    research_remap_index,
)
from gi_scenario_core.evidence_guide_v2_campaign import (
    TIME_BLOCKS,
    AttemptOutcome,
    CampaignStore,
    NullAttemptObserver,
    json_sha256,
    load_corpus,
    load_pricing_snapshot,
)
from gi_scenario_core.evidence_guide_v2_provider import (
    _repository_tool_result,
    provider_packet,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = ROOT / "benchmarks" / "evidence_guide" / "v2"
R2B_TERMINAL = ROOT / "dist" / "evidence-guide-v2-campaign-r2b" / "terminal-disposition.json"
R1_INCIDENT = ROOT / "dist" / "evidence-guide-v2-architecture-dev-r1-incident.json"
PRICING = CORPUS_ROOT / "inference-pricing-2026-08-06.json"


def _terminal() -> dict[str, object]:
    return json.loads(R2B_TERMINAL.read_text(encoding="utf-8"))


def _incident() -> dict[str, object]:
    return json.loads(R1_INCIDENT.read_text(encoding="utf-8"))


def _cli_module():
    script = ROOT / "scripts" / "run_evidence_guide_v2_architecture_dev.py"
    module_spec = importlib.util.spec_from_file_location(
        "architecture_development_cli_test", script
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_schedule_is_full_balanced_and_bound_to_its_instance() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    first_instance = study_instance_sha256(
        corpus,
        source_hashes={"code": "a" * 64},
        execution_mode="live",
    )
    second_instance = study_instance_sha256(
        corpus,
        source_hashes={"code": "b" * 64},
        execution_mode="live",
    )
    fake_instance = study_instance_sha256(
        corpus,
        source_hashes={"code": "a" * 64},
        execution_mode="fake",
    )
    first = build_study_schedule(corpus, instance_sha256=first_instance)
    replay = build_study_schedule(corpus, instance_sha256=first_instance)
    second = build_study_schedule(corpus, instance_sha256=second_instance)

    counts = schedule_counts(first)
    assert counts == {
        "attempts": 4_944,
        "by_stage": {
            "development": 1_440,
            "transfer": 1_440,
            "causal_perturbation": 1_344,
            "operational_stratum": 720,
        },
        "by_architecture": {
            "A0xF": 384,
            "A1": 2_064,
            "A3P": 384,
            "A3X": 960,
            "D0": 480,
            "D1": 672,
        },
        "by_model": {
            "Qwen/Qwen3-30B-A3B-Instruct-2507": 1_536,
            "ibm-granite/granite-4.1-8b": 1_536,
            "meta-llama/Llama-3.3-70B-Instruct": 240,
            "openai/gpt-oss-120b": 240,
            "openai/gpt-oss-20b": 240,
            "repository": 1_152,
        },
        "by_time_block": {"block-1": 1_648, "block-2": 1_648, "block-3": 1_648},
        "logical_provider_calls": 5_712,
        "worst_case_transport_attempts": 11_424,
    }
    assert [row.attempt_id for row in first["development"]] == [
        row.attempt_id for row in replay["development"]
    ]
    assert {row.attempt_id for rows in first.values() for row in rows}.isdisjoint(
        {row.attempt_id for rows in second.values() for row in rows}
    )
    fake = build_study_schedule(corpus, instance_sha256=fake_instance)
    assert first_instance != fake_instance
    assert {row.attempt_id for rows in first.values() for row in rows}.isdisjoint(
        {row.attempt_id for rows in fake.values() for row in rows}
    )


def test_preregistration_pins_every_behavior_determining_runtime_file() -> None:
    hashes = source_hashes_for_study(ROOT, PRICING)
    assert {
        "architecture_development_orchestrator",
        "architecture_development_cli",
        "architecture_development_executor",
        "architecture_r1_incident_validator",
        "paired_architecture_executor",
        "campaign_mechanics",
        "provider_executor",
        "deterministic_scorer",
        "inference_adapter",
        "weave_trace_contract",
        "cloud_projection",
        "shared_runtime_protocol",
        "production_evidence_guide",
        "pricing_snapshot",
    }.issubset(hashes)
    assert all(len(value) == 64 for value in hashes.values())


def test_paid_execution_guard_is_exact_and_has_no_implicit_broad_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _cli_module()
    parser = module._parser()
    validate = module._validate_execute_arguments
    with pytest.raises(SystemExit, match="one --stage and one --time-block"):
        validate(parser.parse_args(["--execute"]))

    base = [
        "--execute",
        "--stage",
        "development",
        "--time-block",
        "block-1",
        "--entity",
        "wandb",
        "--confirm-cost-cap-usd",
        "300.00",
    ]
    monkeypatch.setenv("WANDB_API_KEY", "test-only-placeholder-key")
    with pytest.raises(SystemExit, match="exact project"):
        validate(parser.parse_args([*base, "--project", "wrong-project"]))
    monkeypatch.delenv("WANDB_API_KEY")
    with pytest.raises(SystemExit, match="WANDB_ENTITY and WANDB_API_KEY"):
        validate(parser.parse_args(base))
    monkeypatch.setenv("WANDB_API_KEY", "test-only-placeholder-key")
    with pytest.raises(SystemExit, match="confirm-cost-cap-usd 300.00"):
        validate(parser.parse_args([*base[:-1], "299.99"]))
    validate(parser.parse_args(base))


def test_fake_rehearsal_receipt_cannot_complete_or_bind_live_execution(
    tmp_path: Path,
) -> None:
    """Regression for copied local receipts skipping a paid cloud block."""

    module = _cli_module()
    fake_instance = "a" * 64
    live_instance = "b" * 64
    fake_context = module._execution_context(
        mode="fake",
        study_instance_sha256=fake_instance,
        entity=None,
        project="gi-scenario-studio-agent-evals",
    )
    live_context = module._execution_context(
        mode="live",
        study_instance_sha256=live_instance,
        entity="wandb",
        project="gi-scenario-studio-agent-evals",
    )
    assert module._mode_output_root(tmp_path, "fake") != module._mode_output_root(tmp_path, "live")

    group = {
        "stage": "development",
        "model_id": "ibm-granite/granite-4.1-8b",
        "architecture_id": "A1",
        "repeat": 0,
        "time_block": "block-1",
        "hidden": False,
    }
    fake_body = {
        "schema_version": "1.0",
        "study_version": "evidence-guide-v2-architecture-development-r3",
        "execution_context": fake_context,
        "group": group,
        "cloud": {
            "mode": "local",
            "path": str(tmp_path / "local.json"),
            "attempt_count": 1,
            "group_sha256": json_sha256(group),
        },
        "completed": 1,
        "reused": 0,
    }
    fake_receipt = {
        **fake_body,
        "receipt_sha256": json_sha256(fake_body),
    }
    assert module._complete_block_receipt(
        fake_receipt,
        group=group,
        count=1,
        execution_context=fake_context,
    )

    # This is the original exploit shape: copy the local block receipt into a
    # live store. Even replacing its wrapper and recomputing its hash cannot
    # turn a local observer acknowledgement into W&B + Weave confirmation.
    forged_live_body = {**fake_body, "execution_context": live_context}
    forged_live_receipt = {
        **forged_live_body,
        "receipt_sha256": json_sha256(forged_live_body),
    }
    assert not module._complete_block_receipt(
        forged_live_receipt,
        group=group,
        count=1,
        execution_context=live_context,
    )

    store = CampaignStore(tmp_path / "copied-campaign.sqlite")
    try:
        module._bind_store_execution(
            store,
            execution_context=fake_context,
            scheduled_attempt_ids=set(),
        )
        store.put_receipt("r3:fake:local", fake_receipt)
        with pytest.raises(
            RuntimeError,
            match="R3_EXECUTION_MODE_OR_DESTINATION_MISMATCH",
        ):
            module._bind_store_execution(
                store,
                execution_context=live_context,
                scheduled_attempt_ids=set(),
            )
    finally:
        store.close()


def test_live_block_completion_requires_exact_wandb_weave_destination() -> None:
    module = _cli_module()
    context = module._execution_context(
        mode="live",
        study_instance_sha256="c" * 64,
        entity="wandb",
        project="gi-scenario-studio-agent-evals",
    )
    group = {
        "stage": "transfer",
        "model_id": "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "architecture_id": "A3X",
        "repeat": 1,
        "time_block": "block-2",
        "hidden": False,
    }
    cloud = {
        "mode": "wandb_weave",
        "run_id": "run-123",
        "run_url": ("https://wandb.ai/wandb/gi-scenario-studio-agent-evals/runs/run-123"),
        "attempt_count": 2,
        "group_sha256": json_sha256(group),
        "hidden": False,
        "weave_evaluation_uri": ("weave:///wandb/gi-scenario-studio-agent-evals/call/eval-123"),
        "weave_evaluation_run_url": (
            "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/weave/evaluations/eval-123"
        ),
    }
    body = {
        "schema_version": "1.0",
        "study_version": "evidence-guide-v2-architecture-development-r3",
        "execution_context": context,
        "group": group,
        "cloud": cloud,
        "completed": 2,
        "reused": 0,
    }
    receipt = {**body, "receipt_sha256": json_sha256(body)}
    assert module._complete_block_receipt(
        receipt,
        group=group,
        count=2,
        execution_context=context,
    )

    without_weave = json.loads(json.dumps(body))
    without_weave["cloud"]["weave_evaluation_uri"] = None
    without_weave_receipt = {
        **without_weave,
        "receipt_sha256": json_sha256(without_weave),
    }
    assert not module._complete_block_receipt(
        without_weave_receipt,
        group=group,
        count=2,
        execution_context=context,
    )

    wrong_entity = json.loads(json.dumps(body))
    wrong_entity["cloud"]["run_url"] = (
        "https://wandb.ai/other/gi-scenario-studio-agent-evals/runs/run-123"
    )
    wrong_entity_receipt = {
        **wrong_entity,
        "receipt_sha256": json_sha256(wrong_entity),
    }
    assert not module._complete_block_receipt(
        wrong_entity_receipt,
        group=group,
        count=2,
        execution_context=context,
    )

    hostile_host = json.loads(json.dumps(body))
    hostile_host["cloud"]["run_url"] = (
        "https://example.invalid/wandb/gi-scenario-studio-agent-evals/runs/run-123"
    )
    hostile_host_receipt = {
        **hostile_host,
        "receipt_sha256": json_sha256(hostile_host),
    }
    assert not module._complete_block_receipt(
        hostile_host_receipt,
        group=group,
        count=2,
        execution_context=context,
    )


def test_live_store_rejects_unbound_local_state(tmp_path: Path) -> None:
    module = _cli_module()
    store = CampaignStore(tmp_path / "unbound-local.sqlite")
    try:
        store.put_receipt(
            "preregistration_local_seal",
            {"kind": "local_preregistration", "entity": "local"},
        )
        with pytest.raises(RuntimeError, match="R3_UNBOUND_EXECUTION_STATE_REJECTED"):
            module._bind_store_execution(
                store,
                execution_context=module._execution_context(
                    mode="live",
                    study_instance_sha256="d" * 64,
                    entity="wandb",
                    project="gi-scenario-studio-agent-evals",
                ),
                scheduled_attempt_ids=set(),
            )
    finally:
        store.close()


def test_preregistration_consumes_r2b_debit_and_can_never_release() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    pricing = load_pricing_snapshot(PRICING)
    preregistration, _ = build_research_preregistration(
        corpus,
        pricing=pricing,
        source_hashes={"code": "a" * 64},
        terminal_receipt=_terminal(),
        incident_receipt=_incident(),
        execution_mode="live",
    )

    assert preregistration["release_eligible"] is False
    assert preregistration["study_version"].endswith("-r3")
    assert preregistration["execution_mode"] == "live"
    assert preregistration["execution_boundary"] == {
        "mode_is_part_of_study_instance": True,
        "attempt_ids_are_mode_specific": True,
        "database_and_output_root_must_be_mode_specific": True,
        "fake_receipts_may_satisfy_live_completion": False,
        "live_completion_requires_wandb_and_weave_readback": True,
    }
    assert preregistration["assistant_release_basis_may_be_created"] is False
    structural = preregistration["structural_plan_analysis"]
    assert structural["finite_plan_cardinality"] == {1: 240}
    assert structural["all_scenarios_have_one_allowed_plan"] is True
    assert structural["model_reasoning_attribution_permitted"] is False
    assert structural["splits"]["development"]["required_tool_plan_cardinality"] == {
        "1": 75,
        "2": 5,
        "3": 40,
    }
    assert structural["splits"]["validation"]["required_tool_plan_cardinality"] == {
        "1": 63,
        "2": 5,
        "3": 52,
    }
    assert preregistration["production_state_before_and_after"] == "DISABLE_LIVE_GUIDE"
    assert preregistration["schedule"]["attempt_count"] == 4_944
    assert preregistration["schedule"]["counts"]["logical_provider_calls"] == 5_712
    assert preregistration["budget"]["incremental"] == {
        "logical_provider_invocations": 5_712,
        "worst_case_transport_attempts": 11_424,
        "worst_case_input_tokens": 23_396_352,
        "worst_case_output_tokens": 1_462_272,
        "worst_case_cost_usd": "2.610528",
    }
    assert preregistration["budget"]["combined_with_predecessor_debits"] == {
        "cost_usd": "2.624279",
        "provider_invocations": 6_005,
        "transport_attempts": 11_716,
        "input_tokens": 23_640_718,
        "output_tokens": 1_499_528,
    }
    request_audit = preregistration["request_envelope_audit"]
    assert request_audit["request_count"] == 5_712
    assert request_audit["maximum_request_utf8_bytes"] == 2_667
    assert request_audit["request_byte_cap"] == RESEARCH_REQUEST_BYTE_CAP == 65_536
    assert request_audit["provider_input_token_cap"] == (RESEARCH_PROVIDER_INPUT_TOKEN_CAP) == 2_048
    assert request_audit["request_bytes_are_not_token_counts"] is True
    assert request_audit["all_requests_within_byte_cap"] is True
    body = {key: value for key, value in preregistration.items() if key != "preregistration_sha256"}
    assert preregistration["preregistration_sha256"] == json_sha256(body)


def test_r2b_receipt_validation_and_store_debit_are_fail_closed(tmp_path: Path) -> None:
    terminal = _terminal()
    validated = validate_r2b_terminal_receipt(terminal)
    assert validated["usage"]["transport_attempts"] == 291
    tampered = {**terminal, "decision": "ENABLE_LIVE_GUIDE"}
    with pytest.raises(ValueError, match="R2B_TERMINAL_RECEIPT_INVALID"):
        validate_r2b_terminal_receipt(tampered)

    store = CampaignStore(tmp_path / "campaign.sqlite")
    try:
        prior = apply_r2b_debit(store, terminal)
        assert prior["cost_usd"] == "0.013704"
        assert store.usage() == validated["usage"]
        with pytest.raises(RuntimeError, match="immutable campaign metadata drifted"):
            store.put_meta("prior_usage", {**prior, "transport_attempts": 292}, immutable=True)
    finally:
        store.close()


def test_r1_incident_is_required_and_its_usage_is_added_once(tmp_path: Path) -> None:
    incident = _incident()
    validated = validate_r1_architecture_incident_receipt(
        incident,
        r2b_terminal_receipt=_terminal(),
    )
    assert validated["incident_usage"] == {
        "cost_usd": "0.000047",
        "provider_invocations": 2,
        "transport_attempts": 1,
        "input_tokens": 438,
        "output_tokens": 8,
    }
    assert validated["cumulative_usage"] == {
        "cost_usd": "0.013751",
        "provider_invocations": 293,
        "transport_attempts": 292,
        "input_tokens": 244_366,
        "output_tokens": 37_256,
    }
    tampered = json.loads(json.dumps(incident))
    tampered["incident_usage"]["input_tokens"] = 439
    with pytest.raises(ValueError, match="R1_ARCHITECTURE_INCIDENT_RECEIPT_INVALID"):
        validate_r1_architecture_incident_receipt(
            tampered,
            r2b_terminal_receipt=_terminal(),
        )

    store = CampaignStore(tmp_path / "campaign.sqlite")
    try:
        prior = apply_predecessor_debits(store, _terminal(), incident)
        assert prior["input_tokens"] == 244_366
        assert store.usage() == validated["cumulative_usage"]
    finally:
        store.close()


def test_request_envelope_audit_fails_when_byte_cap_is_too_small() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    schedules = build_study_schedule(
        corpus,
        instance_sha256=study_instance_sha256(
            corpus,
            source_hashes={"code": "a" * 64},
            execution_mode="live",
        ),
    )
    one = {
        "development": (
            next(spec for spec in schedules["development"] if spec.architecture_id == "A1"),
        )
    }
    with pytest.raises(RuntimeError, match="R2_REQUEST_BYTE_CAP_EXCEEDED"):
        audit_research_request_envelopes(corpus, one, request_byte_cap=1_024)


def test_terminal_disposition_is_exploratory_and_not_a_release_basis(tmp_path: Path) -> None:
    corpus = load_corpus(CORPUS_ROOT)
    pricing = load_pricing_snapshot(PRICING)
    preregistration, full_schedule = build_research_preregistration(
        corpus,
        pricing=pricing,
        source_hashes={"code": "a" * 64},
        terminal_receipt=_terminal(),
        incident_receipt=_incident(),
        execution_mode="live",
    )
    tiny_schedule = {stage: (full_schedule[stage][0],) for stage in RESEARCH_STAGES}
    store = CampaignStore(tmp_path / "campaign.sqlite")
    try:
        apply_predecessor_debits(store, _terminal(), _incident())
        for stage, specs in tiny_schedule.items():
            spec = specs[0]
            details = {
                "answer_released": False,
                "candidate_conformant": True,
                "hypothetical_release_eligible": True,
            }
            if spec.architecture_id == "A3X":
                details.update(
                    {
                        "shared_router_tool_receipt_sha256": "a" * 64,
                        "paired_branch_outcomes": {
                            "A3P": {"status": "valid"},
                            "A0xF": {"status": "valid"},
                        },
                    }
                )
            store.save_attempt(
                spec,
                AttemptOutcome(
                    attempt_id=spec.attempt_id,
                    status="completed",
                    provider_invocations=spec.provider_steps,
                    transport_attempts=spec.provider_steps,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=Decimal(0),
                    latency_ms=1.0,
                    protocol_valid=True,
                    safe=True,
                    evidence_valid=True,
                    repository_fidelity=True,
                    injection_safe=True,
                    causal=True,
                    intent_correct=True,
                    safety_intent_correct=True,
                    false_refusal=False,
                    trace_acknowledged=True,
                    details=details,
                ),
            )
            for block in TIME_BLOCKS:
                store.begin_session(stage, block)
                store.finish_session(stage, block)

        summary, terminal = finalize_exploratory_study(
            store,
            preregistration=preregistration,
            schedules=tiny_schedule,
        )
        assert summary["release_eligible"] is False
        assert terminal["disposition"] == TERMINAL_DISPOSITION
        assert terminal["production_decision"] == "DISABLE_LIVE_GUIDE"
        assert terminal["assistant_release_basis_created"] is False
        assert store.get_receipt("terminal_disposition") == terminal
        assert store.get_receipt("release_basis") is None
    finally:
        store.close()


def _analysis_row(
    *,
    attempt: str,
    architecture: str,
    case: str,
    condition: str | None = None,
    raw_invalid: bool = False,
    candidate_eligible: bool = True,
    plan: str = "recorded_rules_fail_v1",
) -> dict[str, object]:
    details: dict[str, object] = {
        "answer_released": False,
        "candidate_conformant": candidate_eligible and not raw_invalid,
        "hypothetical_release_eligible": candidate_eligible and not raw_invalid,
        "raw_invalid": raw_invalid,
        "router_accuracy": True,
        "tool_accuracy": True,
        "plan_accuracy": not raw_invalid,
        "scenario_type": "causal_retrieval",
        "operation_sequence": [
            "repository_tool",
            "synthesis_call",
            "deterministic_validation",
        ],
        "protocol_shape_id": (
            "finite_plan_v1" if architecture in {"A1", "A3P"} else "diagnostic_prose_v1"
        ),
    }
    if architecture == "A3X":
        details = {
            "answer_released": False,
            "candidate_conformant": True,
            "hypothetical_release_eligible": True,
            "shared_router_tool_receipt_sha256": "a" * 64,
            "shared_router_usage": {
                "input_tokens": 20,
                "output_tokens": 2,
                "cost_usd": "0.002",
            },
            "paired_branch_outcomes": {
                "A3P": {
                    "status": "completed",
                    "protocol_valid": True,
                    "safe": True,
                    "evidence_valid": True,
                    "repository_fidelity": True,
                    "injection_safe": True,
                    "trace_acknowledged": True,
                    "answer_released": False,
                    "candidate_conformant": True,
                    "hypothetical_release_eligible": True,
                    "raw_invalid": False,
                    "canonical_correct": True,
                    "router_accuracy": True,
                    "tool_accuracy": True,
                    "plan_accuracy": True,
                    "causal": True,
                    "intent_correct": True,
                    "explanation_plan_id": plan,
                    "usage": {
                        "input_tokens": 30,
                        "output_tokens": 3,
                        "cost_usd": "0.003",
                    },
                },
                "A0xF": {
                    "status": "completed",
                    "protocol_valid": True,
                    "safe": not raw_invalid,
                    "evidence_valid": not raw_invalid,
                    "repository_fidelity": not raw_invalid,
                    "injection_safe": True,
                    "trace_acknowledged": True,
                    "answer_released": False,
                    "candidate_conformant": candidate_eligible and not raw_invalid,
                    "hypothetical_release_eligible": (candidate_eligible and not raw_invalid),
                    "raw_invalid": raw_invalid,
                    "canonical_correct": not raw_invalid,
                    "router_accuracy": True,
                    "tool_accuracy": True,
                    "plan_accuracy": None,
                    "causal": True,
                    "intent_correct": True,
                    "explanation_plan_id": "diagnostic_prose",
                    "usage": {
                        "input_tokens": 40,
                        "output_tokens": 4,
                        "cost_usd": "0.004",
                    },
                },
            },
        }
    row = {
        "attempt_id": attempt * 32,
        "stage": "causal_perturbation" if condition else "development",
        "split": "development",
        "case_id": case,
        "project_family": "cedar_trace_cc0",
        "model_id": "ibm-granite/granite-4.1-8b",
        "architecture_id": architecture,
        "prompt_id": "checklist",
        "max_output_tokens": 128,
        "repeat": 0,
        "time_block": "block-1",
        "hidden": False,
        "ablation_condition": condition,
        "status": "completed",
        "provider_invocations": 3 if architecture == "A3X" else 1,
        "transport_attempts": 3 if architecture == "A3X" else 1,
        "input_tokens": 100,
        "output_tokens": 10,
        "cost_usd": "0.01",
        "latency_ms": 25,
        "protocol_valid": True,
        "safe": not raw_invalid,
        "evidence_valid": not raw_invalid,
        "repository_fidelity": not raw_invalid,
        "injection_safe": True,
        "causal": True,
        "intent_correct": True,
        "safety_intent_correct": True,
        "false_refusal": False,
        "trace_acknowledged": True,
        "explanation_plan_id": plan,
        "details": details,
    }
    if condition and architecture in {"D1", "A1", "A3P", "A0xF"}:
        corpus = load_corpus(CORPUS_ROOT)
        case_record = corpus.case_map("development")[case]
        spec = ResearchAttemptSpec(
            attempt_id=str(row["attempt_id"]),
            stage=str(row["stage"]),
            split=str(row["split"]),
            case_id=str(row["case_id"]),
            project_family=str(row["project_family"]),
            model_id=str(row["model_id"]),
            architecture_id=str(row["architecture_id"]),
            prompt_id=str(row["prompt_id"]),
            max_output_tokens=int(row["max_output_tokens"]),
            repeat=int(row["repeat"]),
            time_block=str(row["time_block"]),
            presentation_index=int(row["repeat"]),
            ablation_condition=str(row["ablation_condition"]),
        )
        attempt_index = research_remap_index(spec)
        packet = provider_packet(
            corpus.datasets["development"],
            case_record,
            attempt_index,
        )
        tool_name = str(case_record["expectations"]["required_tool_plan"][0])
        original = _repository_tool_result(
            case_record,
            tool_name=tool_name,
            attempt_index=attempt_index,
            hidden_only=True,
        )
        _tool, intervention, _expected = _conditioned_causal_evidence(
            case_record,
            attempt_index=attempt_index,
            condition=condition,
        )
        details["semantic_intervention_receipt"] = (
            build_semantic_intervention_receipt(
                spec=spec,
                packet=packet,
                tool_name=tool_name,
                original_evidence=original,
                intervention_evidence=intervention,
                condition=condition,
                operation_sequence=details["operation_sequence"],
                response_contract_id=str(details["protocol_shape_id"]),
            )
        )
    return row


def test_branch_analysis_separates_generation_containment_and_release() -> None:
    primary_a1 = _analysis_row(
        attempt="p",
        architecture="A1",
        case="egv2-dev-causal-retrieval-001",
    )
    original_a1 = _analysis_row(
        attempt="a",
        architecture="A1",
        case="egv2-dev-causal-retrieval-001",
        condition="evidence_original",
    )
    original_pair = _analysis_row(
        attempt="b",
        architecture="A3X",
        case="egv2-dev-causal-retrieval-001",
        raw_invalid=True,
        candidate_eligible=False,
    )
    counterfactual = _analysis_row(
        attempt="c",
        architecture="A1",
        case="egv2-dev-causal-retrieval-001",
        condition="counterfactual_threshold_crossing",
        plan="recorded_rules_pass_v1",
    )
    withheld = _analysis_row(
        attempt="d",
        architecture="A1",
        case="egv2-dev-causal-retrieval-001",
        condition="evidence_withheld",
        plan="unsupported",
    )
    rows = [primary_a1, original_a1, original_pair, counterfactual, withheld]
    branches = extract_research_branches(rows)
    prose = next(
        row
        for row in branches
        if row["parent_attempt_id"] == original_pair["attempt_id"] and row["arm"] == "A0xF"
    )
    finite = next(
        row
        for row in branches
        if row["parent_attempt_id"] == original_pair["attempt_id"] and row["arm"] == "A3P"
    )
    assert prose["raw_invalid"] is True
    assert prose["validator_contained"] is True
    assert prose["actual_released_invalid"] is False
    assert prose["candidate_conformant"] is False
    assert prose["actual_answer_released"] is False
    assert finite["candidate_conformant"] is True
    assert finite["actual_answer_released"] is False
    # Branch cost includes half of the shared router cost.
    assert finite["cost_usd"] == "0.004"

    analysis = analyze_research_rows(rows, bootstrap_samples=100)
    assert analysis["raw_invalid_count"] == 1
    assert analysis["validator_contained_count"] == 1
    assert analysis["actual_response_released_count"] == 0
    assert analysis["actual_released_invalid_count"] == 0
    assert analysis["production_non_release_enforced"] is True
    assert analysis["pair_integrity"]["all_pairs_integral"] is True
    assert analysis["causal_triads"]["counterfactual_plan_changed_count"] == 1
    assert analysis["causal_triads"]["all_orchestration_equivalent"] is True
    comparison = next(
        row
        for row in analysis["paired_risk_differences"]
        if row["arm_a"] == "A3P" and row["arm_b"] == "A0xF"
    )
    assert comparison["pair_count"] == 1
    assert comparison["risk_difference_b_minus_a"] == -1
    assert comparison["mcnemar"]["exact_two_sided_p"] == 1.0
    assert exact_mcnemar(0, 2) == 0.5

    prohibited_release = json.loads(json.dumps(original_a1))
    prohibited_release["details"]["answer_released"] = True
    release_analysis = analyze_research_rows(
        [prohibited_release],
        bootstrap_samples=10,
    )
    assert release_analysis["actual_response_released_count"] == 1
    assert release_analysis["production_non_release_enforced"] is False
    assert release_analysis["candidate_conformant_count"] == 1


def test_fake_causal_executor_flips_plan_and_noncausal_rows_do_not_form_triads() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    case = corpus.case_map("development")["egv2-dev-causal-retrieval-001"]
    original = ResearchAttemptSpec(
        attempt_id="a" * 32,
        stage="causal_perturbation",
        split="development",
        case_id=str(case["case_id"]),
        project_family=str(case["project_family"]),
        model_id="ibm-granite/granite-4.1-8b",
        architecture_id="A1",
        prompt_id="checklist",
        max_output_tokens=128,
        repeat=0,
        time_block="block-1",
        ablation_condition="evidence_original",
    )
    counterfactual = replace(
        original,
        attempt_id="b" * 32,
        stage="causal_perturbation",
        ablation_condition="counterfactual_threshold_crossing",
    )
    executor = ResearchFakeExecutor()
    base = executor.execute(
        spec=original,
        case=case,
        observer=NullAttemptObserver(),
    )
    crossed = executor.execute(
        spec=counterfactual,
        case=case,
        observer=NullAttemptObserver(),
    )
    assert base.explanation_plan_id == "recorded_rules_fail_v1"
    assert crossed.explanation_plan_id == "recorded_rules_pass_v1"

    noncausal = _analysis_row(
        attempt="z",
        architecture="A1",
        case="egv2-dev-supported-001",
    )
    noncausal["details"]["scenario_type"] = "supported"
    analysis = analyze_research_rows([noncausal], bootstrap_samples=10)
    assert analysis["causal_triads"]["triad_count"] == 0
