from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import gi_scenario_core.evidence_guide_r4_grounding as grounding_module
import pytest
from gi_scenario_core.evidence_guide_r4_grounding import (
    GROUNDING_STAGE,
    MODELS,
    PROSE_OUTPUT_CAPS,
    PROSE_STAGE,
    R4C_MAX_INPUT_TOKENS,
    STAGES,
    GroundingExecutor,
    PerfectGroundingFakeProvider,
    analyze,
    build_preregistration,
    build_schedule,
    d1_positive_control,
    load_r4_corpus,
)
from gi_scenario_core.evidence_guide_v2_campaign import (
    BudgetCaps,
    CampaignStore,
    ModelPrice,
    NullAttemptObserver,
    file_sha256,
)
from gi_scenario_core.evidence_guide_v2_cloud import LocalCampaignPublisher

ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = ROOT / "benchmarks" / "evidence_guide" / "r4" / "corpus.json"
R4B_AMENDMENT_PATH = (
    ROOT / "benchmarks" / "evidence_guide" / "r4" / "protocol-amendment-r4b.json"
)
R4C_AMENDMENT_PATH = (
    ROOT / "benchmarks" / "evidence_guide" / "r4" / "protocol-amendment-r4c.json"
)
INCIDENT_PATH = (
    ROOT / "benchmarks" / "evidence_guide" / "r4" / "r4b-incident-snapshot.json"
)
INSTANCE = "a" * 64
RUNNER_PATH = ROOT / "scripts" / "run_evidence_guide_r4_grounding.py"
_RUNNER_SPEC = importlib.util.spec_from_file_location("evidence_guide_r4_runner", RUNNER_PATH)
assert _RUNNER_SPEC is not None and _RUNNER_SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(RUNNER)


def _pricing() -> dict[str, ModelPrice]:
    return {
        model: ModelPrice(
            input_per_million=Decimal("0.10"),
            output_per_million=Decimal("0.20"),
            source="test-only",
            observed_at="2026-08-07",
        )
        for model in MODELS
    }


def _executor(*, execution_mode: str = "fake") -> tuple[Any, GroundingExecutor]:
    corpus = load_r4_corpus(CORPUS_PATH)
    executor = GroundingExecutor(
        corpus=corpus,
        provider=PerfectGroundingFakeProvider(),
        entity="local",
        project="local",
        pricing=_pricing(),
        execution_mode=execution_mode,
        study_instance_sha256=INSTANCE,
    )
    return corpus, executor


def test_r4_schedule_is_the_exact_672_call_design() -> None:
    corpus = load_r4_corpus(CORPUS_PATH)
    schedule = build_schedule(corpus, instance_sha256=INSTANCE)

    assert len(schedule) == 672
    assert Counter(spec.stage for spec in schedule) == Counter(
        {GROUNDING_STAGE: 384, PROSE_STAGE: 288}
    )
    assert Counter(
        spec.max_output_tokens for spec in schedule if spec.stage == PROSE_STAGE
    ) == Counter({128: 144, 256: 144})
    assert len({spec.attempt_id for spec in schedule}) == 672
    assert d1_positive_control(corpus)["passed_count"] == 192


def test_r4b_amendment_preserves_the_failed_r4_protocol_record() -> None:
    amendment = json.loads(R4B_AMENDMENT_PATH.read_text(encoding="utf-8"))
    corpus = load_r4_corpus(CORPUS_PATH)
    schedule = build_schedule(corpus, instance_sha256=INSTANCE)

    assert amendment["replacement_study_version"] == (
        "gi-evidence-guide-grounding-r4b-final"
    )
    assert amendment["r4_live_incident"]["attempt_count"] == 224
    assert amendment["r4_live_incident"]["failed_closed_count"] == 224
    assert amendment["r4_live_incident"]["scientific_denominator"] == 0
    assert amendment["six_call_schema_diagnostic"]["successful_calls"] == 4
    assert amendment["six_call_schema_diagnostic"]["expected_unique_items_failures"] == 2
    assert amendment["six_call_schema_diagnostic"]["conditions"]["r4_schema_shape"][
        "provider_message"
    ] == 'Grammar error: Unimplemented keys: ["uniqueItems"]'
    assert amendment["four_call_corrected_schema_preflight"]["http_200"] == 4
    assert amendment["r4b_protocol_change"]["state_reuse_permitted"] is False
    ledger = amendment["resource_ledger"]
    assert ledger["all_schema_diagnostics"]["input_token_envelope"] == 4320
    assert ledger["all_schema_diagnostics"]["output_token_envelope"] == 279
    assert ledger["all_schema_diagnostics"]["verified_observed_cost_usd"] == "0.0000455"
    assert ledger["all_schema_diagnostics"]["conservative_envelope_cost_usd"] == "0.0003783"
    assert ledger["failed_r4_block_at_risk"]["conservative_at_risk_cost_usd"] == "0.041520"
    assert ledger["r4b_prior_for_cap_enforcement"] == {
        "provider_invocations": 1679,
        "transport_attempts": 1678,
        "input_token_envelope": 1_253_981,
        "output_token_envelope": 219_807,
        "conservative_cumulative_cost_usd": "0.1087813",
        "formula": (
            "0.066883 preserved verified + 0.041520 failed-R4 at-risk + "
            "0.0003783 diagnostic conservative"
        ),
    }
    assert ledger["separate_verified_observed_total"]["verified_observed_cost_usd"] == (
        "0.0669285"
    )

    assert Counter((spec.stage, spec.time_block) for spec in schedule) == Counter(
        {
            **{(GROUNDING_STAGE, block): 128 for block in ("block-1", "block-2", "block-3")},
            **{(PROSE_STAGE, block): 96 for block in ("block-1", "block-2", "block-3")},
        }
    )
    grounding_strata = Counter(
        (
            spec.split,
            spec.model_id,
            spec.ablation_condition,
            spec.time_block,
        )
        for spec in schedule
        if spec.stage == GROUNDING_STAGE
    )
    prose_strata = Counter(
        (
            spec.split,
            spec.model_id,
            spec.ablation_condition,
            spec.time_block,
        )
        for spec in schedule
        if spec.stage == PROSE_STAGE
    )
    assert len(grounding_strata) == 48
    assert set(grounding_strata.values()) == {8}
    assert len(prose_strata) == 36
    assert set(prose_strata.values()) == {8}


def test_r4c_amendment_seals_the_r4b_cap_incident_and_clean_start() -> None:
    amendment = json.loads(R4C_AMENDMENT_PATH.read_text(encoding="utf-8"))
    incident = json.loads(INCIDENT_PATH.read_text(encoding="utf-8"))
    incident_body = {
        key: value for key, value in incident.items() if key != "snapshot_sha256"
    }

    assert amendment["replacement_study_version"] == grounding_module.STUDY_VERSION
    assert amendment["incident_snapshot"]["file_sha256"] == file_sha256(INCIDENT_PATH)
    assert incident["snapshot_sha256"] == grounding_module.json_sha256(incident_body)
    assert incident["incident_summary"] == {
        "accepted_traced_responses": 238,
        "block_1_attempts": 224,
        "configured_input_token_cap": 2048,
        "interrupted_block_2_attempts": 48,
        "post_response_input_cap_rejections": 34,
        "provider_invocations": 272,
        "r4b_cells_reused_by_r4c": 0,
        "transport_attempts": 272,
    }
    assert incident["execution_ledger"]["ambiguous_ledger_states"] == 0
    assert incident["receipt_state"]["stale_execution_receipt"][
        "unrepresented_saved_attempts"
    ] == 48
    assert incident["resource_ledger"]["r4c_prior_for_cap_enforcement"] == {
        "conservative_cumulative_cost_usd": "0.1524103",
        "input_token_envelope": 1_715_625,
        "output_token_envelope": 262_815,
        "provider_invocations": 1951,
        "transport_attempts": 1950,
        "verified_observed_cost_usd": "0.1065785",
    }
    assert amendment["r4c_protocol_change"]["state_reuse_permitted"] is False
    assert amendment["r4c_protocol_change"][
        "maximum_provider_input_tokens_per_transport"
    ] == R4C_MAX_INPUT_TOKENS


def test_preregistration_seals_the_design_and_cap_pairs() -> None:
    corpus = load_r4_corpus(CORPUS_PATH)
    preregistration, schedule = build_preregistration(
        corpus,
        pricing=_pricing(),
        source_hashes={
            "corpus": "b" * 64,
            "r4b_protocol_amendment": "c" * 64,
            "r4c_protocol_amendment": "d" * 64,
            "r4b_incident_snapshot": "e" * 64,
        },
        prior_usage={
            "cost_usd": "0",
            "provider_invocations": 0,
            "transport_attempts": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        },
        execution_mode="fake",
        caps=BudgetCaps(),
    )

    assert len(schedule) == 672
    assert preregistration["design"]["provider_call_count"] == 672
    assert preregistration["design"]["grounding"]["provider_calls"] == 384
    assert preregistration["design"]["prose_diagnosis"]["provider_calls"] == 288
    assert preregistration["analysis_policy"]["primary_unit"] == "unique_scenario"
    assert preregistration["analysis_policy"]["pseudoreplicated_Wilson_intervals"] is False
    assert preregistration["d1_positive_control"]["all_passed"] is True
    assert preregistration["protocol_amendment"] == {
        "amendment_id": "gi-evidence-guide-r4c-input-cap-amendment-2026-08-07",
        "source_capsule_logical_name": "r4c_protocol_amendment",
        "source_sha256": "d" * 64,
        "predecessor_amendment_sha256": "c" * 64,
        "incident_snapshot_source_sha256": "e" * 64,
        "r4b_attempts_reused": 0,
        "r4b_responses_reused": 0,
        "clean_start_required": True,
    }
    assert preregistration["request_audit"]["maximum_utf8_bytes"] == 8017
    assert preregistration["request_audit"]["provider_input_token_cap"] == 8192
    assert preregistration["request_audit"][
        "minimum_byte_headroom_under_provider_input_cap"
    ] == 175
    assert preregistration["budget"]["incremental_worst_case"][
        "worst_case_input_tokens"
    ] == 672 * 2 * 8192


def test_r4b_provider_schemas_use_only_the_diagnostic_supported_subset() -> None:
    for stage in (GROUNDING_STAGE, PROSE_STAGE):
        schema = grounding_module._response_schema(
            stage, ("plan_a", "plan_b", "plan_c")
        )
        assert set(schema) == {
            "type",
            "additionalProperties",
            "properties",
            "required",
        }
        assert "uniqueItems" not in json.dumps(schema, sort_keys=True)
        for descriptor in schema["properties"].values():
            assert set(descriptor) <= {"type", "enum", "items"}
            if "items" in descriptor:
                assert descriptor["items"] == {"type": "string"}


@pytest.mark.parametrize(
    ("field", "expected_error"),
    (
        ("evidence_ids", "evidence_ids_duplicate"),
        ("limitation_ids", "limitation_ids_duplicate"),
        ("action_ids", "action_ids_duplicate"),
    ),
)
def test_r4b_rejects_duplicate_identifiers_locally(
    field: str, expected_error: str
) -> None:
    schema = grounding_module._response_schema(
        PROSE_STAGE, ("plan_a", "plan_b", "plan_c")
    )
    response = {
        "plan_id": "plan_a",
        "response_mode": "answer",
        "answer_text": "Synthetic evidence only.",
        "evidence_ids": ["ev_a"],
        "limitation_ids": ["modeled_not_measured"],
        "action_ids": ["open_evidence"],
    }
    response[field] = [response[field][0], response[field][0]]

    _parsed, errors = grounding_module._parse_response(
        json.dumps(response), schema=schema
    )

    assert expected_error in errors


class _ExitFailureObserver:
    @contextmanager
    def attempt(self, spec: Any, public_input: Mapping[str, Any]) -> Iterator[None]:
        del spec, public_input
        yield None
        raise RuntimeError("synthetic trace acknowledgement failure")

    def log_outcome(self, spec: Any, outcome: Any) -> None:
        del spec, outcome


def test_provisional_model_outcome_survives_trace_acknowledgement_failure() -> None:
    corpus, executor = _executor(execution_mode="live")
    spec = next(
        item
        for item in build_schedule(corpus, instance_sha256=INSTANCE)
        if item.stage == GROUNDING_STAGE
    )

    outcome = executor.execute(
        spec=spec,
        case=corpus.case_map(spec.split)[spec.case_id],
        observer=_ExitFailureObserver(),
    )

    assert outcome.status == "completed"
    assert outcome.protocol_valid is True
    assert outcome.details["provider_response_received"] is True
    assert outcome.details["decision_fidelity"] is True
    assert outcome.details["evidence_binding"] is True
    assert outcome.details["provisional_model_conformance"] is True
    assert outcome.trace_acknowledged is False
    assert outcome.details["end_to_end_conformance"] is False
    assert outcome.causal is None
    assert outcome.intent_correct is None
    assert outcome.injection_safe is False


def test_r4c_preserves_and_scores_a_post_receipt_cap_violation() -> None:
    corpus = load_r4_corpus(CORPUS_PATH)
    delegate = PerfectGroundingFakeProvider()

    class OverCapCompletionProvider:
        def complete(self, **kwargs: Any) -> Any:
            completion = delegate.complete(**kwargs)
            return replace(
                completion,
                usage=replace(
                    completion.usage,
                    input_tokens=R4C_MAX_INPUT_TOKENS + 1,
                    total_tokens=(
                        R4C_MAX_INPUT_TOKENS
                        + 1
                        + completion.usage.output_tokens
                    ),
                ),
            )

    executor = GroundingExecutor(
        corpus=corpus,
        provider=OverCapCompletionProvider(),
        entity="local",
        project="local",
        pricing=_pricing(),
        execution_mode="fake",
        study_instance_sha256=INSTANCE,
    )
    spec = next(
        item
        for item in build_schedule(corpus, instance_sha256=INSTANCE)
        if item.stage == GROUNDING_STAGE
    )
    outcome = executor.execute(
        spec=spec,
        case=corpus.case_map(spec.split)[spec.case_id],
        observer=NullAttemptObserver(),
    )

    assert outcome.status == "completed"
    assert outcome.details["provider_response_received"] is True
    assert outcome.details["parsed_response"] is not None
    assert outcome.details["raw_response_sha256"] is not None
    assert outcome.details["resource_policy_valid"] is False
    assert outcome.details["post_receipt_resource_violation"] == {
        "error_code": "post_receipt_input_tokens_cap_exceeded",
        "resource": "input_tokens",
        "observed": R4C_MAX_INPUT_TOKENS + 1,
        "limit": R4C_MAX_INPUT_TOKENS,
        "provider_response_preserved": True,
    }
    assert outcome.protocol_valid is True
    assert outcome.trace_acknowledged is True
    assert outcome.details["end_to_end_conformance"] is False
    assert "post_receipt_input_tokens_cap_exceeded" in outcome.error_codes


def test_fake_672_cell_run_analyzes_at_unique_scenario_level() -> None:
    corpus, executor = _executor()
    schedule = build_schedule(corpus, instance_sha256=INSTANCE)
    observer = NullAttemptObserver()
    rows = []
    for spec in schedule:
        outcome = executor.execute(
            spec=spec,
            case=corpus.case_map(spec.split)[spec.case_id],
            observer=observer,
        )
        rows.append(outcome.as_record(spec))

    analysis = analyze(rows, expected_schedule=schedule)

    assert analysis["attempt_count"] == 672
    assert analysis["stage_attempt_counts"] == {
        GROUNDING_STAGE: 384,
        PROSE_STAGE: 288,
    }
    assert len(analysis["grounding_primary_unique_case_results"]) == 16
    assert {
        row["unique_case_count"] for row in analysis["grounding_primary_unique_case_results"]
    } == {12}
    assert len(analysis["prose_cap_paired_mcnemar"]) == 12
    assert {row["paired_unique_case_count"] for row in analysis["prose_cap_paired_mcnemar"]} == {6}
    assert {row["exact_two_sided_mcnemar_p"] for row in analysis["prose_cap_paired_mcnemar"]} == {
        1.0
    }
    assert analysis["production_release_eligible"] is False


def test_analyzer_rejects_missing_schedule_cell() -> None:
    corpus = load_r4_corpus(CORPUS_PATH)
    schedule = build_schedule(corpus, instance_sha256=INSTANCE)

    with pytest.raises(RuntimeError, match="R4_ANALYSIS_INCOMPLETE"):
        analyze([], expected_schedule=schedule)


def test_prose_caps_are_exactly_the_preregistered_pair() -> None:
    assert PROSE_OUTPUT_CAPS == (128, 256)


def _runner_inputs(root: Path) -> tuple[Path, Path]:
    diagnostic = root / "r3-diagnostic.json"
    diagnostic.write_text(
        json.dumps(
            {
                "release_eligible": False,
                "publication_created": False,
                "article_publication_permitted": False,
                "production_decision": "DISABLE_LIVE_GUIDE",
                "analyzer_gate": {"outcome": "raised"},
                "receipt_sha256": "a" * 64,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return INCIDENT_PATH, diagnostic


def _runner_arguments(*values: str) -> Any:
    return RUNNER._parser().parse_args(list(values))


def test_runner_live_gate_requires_exact_block_destination_and_cost_cap() -> None:
    with pytest.raises(RuntimeError, match="explicit time block"):
        RUNNER._validate_arguments(_runner_arguments("--execute"))
    mode, blocks = RUNNER._validate_arguments(
        _runner_arguments(
            "--execute",
            "--time-block",
            "block-2",
            "--entity",
            "wandb",
            "--project",
            "gi-scenario-studio-agent-evals",
            "--confirm-cost-cap-usd",
            "300.00",
        )
    )
    assert mode == "live"
    assert blocks == ("block-2",)


def test_runner_fake_mode_is_all_three_blocks_and_rejects_live_flags() -> None:
    assert RUNNER.DEFAULT_OUTPUT.name == "evidence-guide-r4c-grounding-final-r1"
    assert RUNNER._validate_arguments(_runner_arguments("--fake-provider")) == (
        "fake",
        ("block-1", "block-2", "block-3"),
    )
    with pytest.raises(RuntimeError, match="always runs all three"):
        RUNNER._validate_arguments(_runner_arguments("--fake-provider", "--time-block", "block-1"))
    with pytest.raises(RuntimeError, match="only for live"):
        RUNNER._validate_arguments(
            _runner_arguments("--fake-provider", "--confirm-cost-cap-usd", "300.00")
        )


def test_runner_carries_r4b_and_all_predecessor_usage_into_the_conservative_prior(
    tmp_path: Path,
) -> None:
    incident, _diagnostic = _runner_inputs(tmp_path)
    usage = RUNNER._prior_usage(incident)
    assert usage["source_sha256"] == file_sha256(incident)
    assert usage["amendment_source_sha256"] == file_sha256(R4C_AMENDMENT_PATH)
    assert usage["predecessor_amendment_source_sha256"] == file_sha256(
        R4B_AMENDMENT_PATH
    )
    assert usage["cost_usd"] == "0.1524103"
    assert usage["verified_observed_cost_usd"] == "0.1065785"
    assert usage["provider_invocations"] == 1951
    assert usage["transport_attempts"] == 1950
    assert usage["input_tokens"] == 1_715_625
    assert usage["output_tokens"] == 262_815
    tampered = tmp_path / "tampered-incident.json"
    value = json.loads(incident.read_text(encoding="utf-8"))
    value["incident_summary"]["provider_invocations"] = 271
    tampered.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeError, match="R4C_INCIDENT_OR_AMENDMENT_INVALID"):
        RUNNER._prior_usage(tampered)


def test_runner_cloud_projection_never_materializes_evaluator_gold(
    tmp_path: Path,
) -> None:
    corpus = load_r4_corpus(CORPUS_PATH)
    projected, manifest = RUNNER._publisher_corpus(corpus, tmp_path)
    assert projected.split_counts == {
        "development": 12,
        "validation": 12,
        "judge_calibration": 1,
    }
    assert manifest["provider_input_source"] is False
    assert manifest["evaluator_content_included"] is False
    serialized = json.dumps(projected.datasets, sort_keys=True)
    for prohibited in (
        "evaluator_only",
        "expected_plan",
        "condition_id",
        "original_target",
        "target_threshold_crossing",
        "target_withheld",
        "distractor_only_perturbation",
        "retrieved_evidence",
        "decision_rule",
    ):
        assert prohibited not in serialized

    paths = RUNNER._source_paths()
    assert paths["pricing_snapshot"] == RUNNER.DEFAULT_PRICING.resolve()
    paths["cloud_dataset_projection"] = tmp_path / "cloud-dataset-projections" / "manifest.json"
    capsule = RUNNER._embedded_source_capsule(paths)
    assert capsule["identity_basis"] == "embedded_content_addressed_source_capsule"
    assert capsule["git_commit_is_scientific_identity"] is False
    assert capsule["absolute_paths_included"] is False
    assert capsule["file_count"] == len(paths)
    assert {
        "experiment_module",
        "runner",
        "r4_corpus",
        "r4_corpus_readme",
        "r4b_protocol_amendment",
        "r4c_protocol_amendment",
        "r4b_incident_snapshot",
        "r4b_incident_snapshot_builder",
        "dependency_lock",
    }.issubset(capsule["files"])
    assert {
        "evidence_guide_v2_protocol.py",
        "evidence_guide_v2_provider.py",
        "observability.py",
        "repository.py",
    }.issubset({record["filename"] for record in capsule["files"].values()})
    for record in capsule["files"].values():
        decoded = base64.b64decode(record["content"], validate=True)
        assert len(decoded) == record["utf8_bytes"]
        assert hashlib.sha256(decoded).hexdigest() == record["sha256"]
        assert not str(record["filename"]).startswith("/")


def test_runner_zero_network_fake_rehearsal_seals_all_672_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident, diagnostic = _runner_inputs(tmp_path)

    class CloudMustNotInitialize:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("fake R4 rehearsal initialized cloud inference")

    monkeypatch.setattr(RUNNER, "WandbInferenceAdapter", CloudMustNotInitialize)
    monkeypatch.setattr(
        RUNNER,
        "_prepare_publisher",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("fake R4 rehearsal initialized W&B/Weave")
        ),
    )
    output = tmp_path / "r4"
    assert (
        RUNNER.main(
            [
                "--fake-provider",
                "--output-dir",
                str(output),
                "--r4b-incident-snapshot",
                str(incident),
                "--r3-diagnostic",
                str(diagnostic),
            ]
        )
        == 0
    )

    fake = output / "fake"
    assert not (output / "live").exists()
    schedule = json.loads((fake / "attempt-schedule.json").read_text(encoding="utf-8"))
    assert len(schedule["rows"]) == 672
    assert Counter(row["stage"] for row in schedule["rows"]) == Counter(
        {GROUNDING_STAGE: 384, PROSE_STAGE: 288}
    )
    assert {row["time_block"] for row in schedule["rows"]} == {
        "block-1",
        "block-2",
        "block-3",
    }
    preregistration = json.loads((fake / "preregistration.json").read_text(encoding="utf-8"))
    budget = preregistration["budget"]
    prior = budget["prior_cumulative_usage"]
    incremental = budget["incremental_worst_case"]
    combined = budget["combined_worst_case"]
    assert prior["cost_usd"] == "0.1524103"
    assert prior["verified_observed_cost_usd"] == "0.1065785"
    assert prior["provider_invocations"] == 1951
    assert prior["transport_attempts"] == 1950
    assert prior["input_tokens"] == 1_715_625
    assert prior["output_tokens"] == 262_815
    assert preregistration["request_audit"]["maximum_utf8_bytes"] == 8017
    assert preregistration["request_audit"]["provider_input_token_cap"] == 8192
    assert Decimal(combined["cost_usd"]) == (
        Decimal(prior["cost_usd"]) + Decimal(incremental["worst_case_cost_usd"])
    )
    assert combined["provider_invocations"] == (
        prior["provider_invocations"] + incremental["logical_provider_invocations"]
    )
    assert combined["transport_attempts"] == (
        prior["transport_attempts"] + incremental["worst_case_transport_attempts"]
    )
    assert combined["input_tokens"] == (
        prior["input_tokens"] + incremental["worst_case_input_tokens"]
    )
    assert combined["output_tokens"] == (
        prior["output_tokens"] + incremental["worst_case_output_tokens"]
    )
    final = json.loads((fake / "execution-receipt.json").read_text(encoding="utf-8"))
    assert final["completed_all"] is True
    assert final["attempt_count"] == 672
    assert final["production_release_eligible"] is False
    assert final["actual_answers_released"] == 0

    store = CampaignStore(fake / "campaign.sqlite")
    try:
        context = store.get_meta("r4_execution_context")
        assert context["execution_mode"] == "fake"
        assert context["attempt_count"] == 672
        assert all(
            store.completed_time_blocks(stage) == set(RUNNER.TIME_BLOCKS) for stage in STAGES
        )
        attempts = store.attempts()
        assert len(attempts) == 672
        assert all(row["details"]["resource_policy_valid"] is True for row in attempts)
        assert not any(
            code.startswith("post_receipt_")
            for row in attempts
            for code in row["error_codes"]
        )
        for row in attempts:
            receipt = row["details"]["experimental_receipt"]
            visible = json.dumps(receipt["normalized_request"], sort_keys=True)
            assert row["ablation_condition"] not in visible
            assert all(
                item not in visible for item in ("evaluator_only", "condition_id", "expected_plan")
            )
    finally:
        store.close()


def test_runner_live_path_constructs_single_transport_adapter_after_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident, diagnostic = _runner_inputs(tmp_path)
    events: list[Any] = []

    def fake_prepare(**kwargs: Any) -> LocalCampaignPublisher:
        events.append(("publisher", kwargs["entity"], kwargs["project"]))
        return LocalCampaignPublisher(tmp_path / "local-live-observer")

    def fake_adapter(_credentials: Any, **kwargs: Any) -> PerfectGroundingFakeProvider:
        events.append(("adapter", kwargs))
        return PerfectGroundingFakeProvider()

    monkeypatch.setattr(RUNNER, "_prepare_publisher", fake_prepare)
    monkeypatch.setattr(RUNNER, "WandbInferenceAdapter", fake_adapter)
    output = tmp_path / "r4-live"
    assert (
        RUNNER.main(
            [
                "--execute",
                "--time-block",
                "block-1",
                "--entity",
                "wandb",
                "--project",
                "gi-scenario-studio-agent-evals",
                "--confirm-cost-cap-usd",
                "300.00",
                "--output-dir",
                str(output),
                "--r4b-incident-snapshot",
                str(incident),
                "--r3-diagnostic",
                str(diagnostic),
            ]
        )
        == 0
    )
    assert events == [
        ("publisher", "wandb", "gi-scenario-studio-agent-evals"),
        ("adapter", {"max_transport_attempts": 1}),
    ]
    final = json.loads((output / "live" / "execution-receipt.json").read_text(encoding="utf-8"))
    assert final["completed_all"] is False
    assert final["production_release_eligible"] is False
    assert not (output / "live" / "publication").exists()
