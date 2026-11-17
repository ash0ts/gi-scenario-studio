from __future__ import annotations

import copy
import importlib.util
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from gi_scenario_core.evidence_evaluation_v2 import load_split, provider_packet
from gi_scenario_core.evidence_guide_v2_campaign import (
    CANDIDATE_MODELS,
    MAX_INPUT_TOKENS_PER_CALL,
    AttemptOutcome,
    AttemptSpec,
    CampaignStore,
    FinalistConfig,
    ModelPrice,
    PerfectFakeExecutor,
    build_preregistration,
    estimate_hidden_receipt_budget,
    estimate_provider_budget,
    export_stage_tables,
    json_sha256,
    load_corpus,
    load_pricing_snapshot,
    reserve_stage,
    run_attempt_batch,
    stage_attempts,
)
from gi_scenario_core.evidence_guide_v2_protocol import deterministic_question_intent
from gi_scenario_core.evidence_guide_v2_provider import (
    _INTENT_SCHEMA,
    _PLAN_SCHEMA,
    INTENT_IDS,
    EvidenceGuideV2ProviderExecutor,
    _repository_tool_name,
    _repository_tool_result,
    _schema_messages,
    _tool_definitions,
    deterministic_plan_id,
    render_repository_plan,
)
from gi_scenario_core.inference import (
    InferenceCompletion,
    InferenceToolCall,
    InferenceUsage,
    build_inference_request_payload,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = ROOT / "benchmarks" / "evidence_guide" / "v2"
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "run_evidence_guide_v2_campaign", ROOT / "scripts" / "run_evidence_guide_v2_campaign.py"
)
assert RUNNER_SPEC and RUNNER_SPEC.loader
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


class GoldGuard(Mapping[str, Any]):
    """A row view that makes accidental answer-key access immediately visible."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        self.value = value

    def __getitem__(self, key: str) -> Any:
        if key == "expectations":
            raise AssertionError("deterministic runtime accessed evaluation gold")
        return self.value[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key in self.value if key != "expectations")

    def __len__(self) -> int:
        return len(self.value) - int("expectations" in self.value)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "expectations":
            raise AssertionError("deterministic runtime accessed evaluation gold")
        return self.value.get(key, default)


def _causal_case() -> tuple[dict[str, Any], dict[str, Any]]:
    dataset = load_split(CORPUS_ROOT / "development.json")
    row = next(item for item in dataset["rows"] if item["scenario_type"] == "causal_retrieval")
    return dataset, row


class _CapabilityProvider:
    def __init__(self, model_id: str, schema_variant: str, tool_name: str) -> None:
        self.model_id = model_id
        self.schema_variant = schema_variant
        self.tool_name = tool_name
        self.calls = 0

    def complete(self, **_: Any) -> InferenceCompletion:
        self.calls += 1
        if self.calls == 1:
            content = {
                "marker": '{"protocol":"strict_schema_supported"}',
                "intent": '{"intent_id":"next_checks"}',
                "finite_plan": '{"explanation_plan_id":"evidence_scope_v1"}',
            }[self.schema_variant]
            return InferenceCompletion(
                provider_call_id="schema-call",
                model=self.model_id,
                content=content,
                usage=InferenceUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                finish_reason="stop",
                request_bytes=200,
            )
        return InferenceCompletion(
            provider_call_id="tool-call",
            model=self.model_id,
            content="",
            tool_calls=[InferenceToolCall("tool-1", self.tool_name, {})],
            usage=InferenceUsage(input_tokens=12, output_tokens=4, total_tokens=16),
            finish_reason="tool_calls",
            request_bytes=220,
        )


class _CapabilityTrace:
    def __init__(self) -> None:
        self.tool_names: list[str] = []
        self.tool_nested_under_llm: list[bool] = []
        self.llm_active = False
        self.validated = False
        self.final_answer = ""
        self.llm_completions = 0
        self.tool_completions = 0
        self.expected_operations: tuple[int, int, bool] | None = None

    @contextmanager
    def llm(self, **_: Any) -> Iterator[Any]:
        self.llm_active = True
        try:
            trace = self

            class Handle:
                def record(self, _completion: Any) -> None:
                    trace.llm_completions += 1

            yield Handle()
        finally:
            self.llm_active = False

    @contextmanager
    def tool(self, *, name: str, **_: Any) -> Iterator[Any]:
        self.tool_names.append(name)
        self.tool_nested_under_llm.append(self.llm_active)
        trace = self

        class Handle:
            def record(self, _result: Any) -> None:
                trace.tool_completions += 1

        yield Handle()

    def validation(self, *, valid: bool, **_: Any) -> None:
        self.validated = valid

    def record_final_answer(self, answer: str) -> None:
        self.final_answer = answer

    def assert_operations(
        self, *, llm_calls: int, tool_calls: int, final_answer: bool
    ) -> None:
        assert self.llm_completions == llm_calls
        assert self.tool_completions == tool_calls
        assert bool(self.final_answer) is final_answer
        self.expected_operations = (llm_calls, tool_calls, final_answer)


class _CapabilityObserver:
    def __init__(self) -> None:
        self.trace = _CapabilityTrace()

    @contextmanager
    def attempt(self, _spec: AttemptSpec, _public_input: Mapping[str, Any]) -> Iterator[Any]:
        yield self.trace

    def log_outcome(self, _spec: AttemptSpec, _outcome: AttemptOutcome) -> None:
        return None


@pytest.mark.parametrize("presentation_index", [0, 1, 2])
def test_capability_executes_and_records_the_requested_repository_tool(
    presentation_index: int,
) -> None:
    corpus = load_corpus(CORPUS_ROOT)
    spec = next(
        item
        for item in stage_attempts("capability", corpus)
        if item.presentation_index == presentation_index
    )
    case = corpus.case_map("development")[spec.case_id]
    schema_variant = ("marker", "intent", "finite_plan")[presentation_index]
    tool_name = str(case["available_tools"][0])
    provider = _CapabilityProvider(spec.model_id, schema_variant, tool_name)
    price = type(
        "Price",
        (),
        {"worst_case": lambda _self, **_: Decimal("0.000001")},
    )()
    executor = EvidenceGuideV2ProviderExecutor(
        corpus=corpus,
        provider=provider,  # type: ignore[arg-type]
        entity="entity",
        project="gi-scenario-studio-agent-evals",
        pricing={spec.model_id: price},  # type: ignore[dict-item]
    )
    observer = _CapabilityObserver()

    outcome = executor.execute(spec=spec, case=case, observer=observer)

    assert outcome.status == "completed"
    assert outcome.protocol_valid is True
    assert outcome.trace_acknowledged is True
    assert outcome.provider_invocations == 2
    assert observer.trace.tool_names == [tool_name]
    assert observer.trace.tool_nested_under_llm == [True]
    assert observer.trace.validated is True
    assert observer.trace.final_answer == "Protocol compatibility passed."
    assert observer.trace.expected_operations == (2, 1, True)
    assert outcome.details["answer_released"] is False
    assert outcome.details["schema_variant"] == schema_variant
    assert outcome.details["tool_name"] == tool_name
    assert outcome.details["tool_count"] == 1
    assert outcome.details["cost_accounting"] == {
        "verified_provider_usage_cost_usd": "0.000002",
        "verified_cost_basis": (
            "pricing_snapshot_times_provider_reported_tokens_from_completed_responses"
        ),
        "provider_reported_currency_cost_available": False,
        "conservative_at_risk_cost_usd": "0.000001",
        "at_risk_cost_basis": "pricing_snapshot_times_retry_and_error_token_envelopes",
        "failed_transport_reservation_is_billed_cost": False,
    }
    assert outcome.details["request_accounting"]["request_bytes_used_as_token_count"] is False


def test_passing_d0_trace_contract_requires_no_provider_or_tool_call() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    spec = next(
        item
        for item in stage_attempts("architecture", corpus)
        if item.architecture_id == "D0"
        and item.case_id == "egv2-dev-multi-turn-fault-003"
        and item.repeat == 0
        and item.time_block == "block-1"
    )
    case = corpus.case_map("development")[spec.case_id]

    class Provider:
        def complete(self, **_: Any) -> InferenceCompletion:
            raise AssertionError("D0 must never invoke inference")

    executor = EvidenceGuideV2ProviderExecutor(
        corpus=corpus,
        provider=Provider(),  # type: ignore[arg-type]
        entity="entity",
        project="gi-scenario-studio-agent-evals",
        pricing={},
    )
    observer = _CapabilityObserver()

    outcome = executor.execute(spec=spec, case=case, observer=observer)

    assert outcome.status == "completed"
    assert outcome.trace_acknowledged is True
    assert outcome.provider_invocations == 0
    assert observer.trace.tool_names == []
    assert observer.trace.expected_operations == (0, 0, True)


@pytest.mark.parametrize(
    ("architecture_id", "case_id", "safe", "answer_released", "tool_count", "plan_id"),
    [
        (
            "D0",
            "egv2-dev-causal-retrieval-001",
            False,
            False,
            0,
            "recorded_rules_pass_v1",
        ),
        (
            "D1",
            "egv2-dev-unsupported-002",
            True,
            True,
            1,
            "authority_boundary_v1",
        ),
    ],
)
def test_real_deterministic_canary_paths_match_the_release_contract(
    architecture_id: str,
    case_id: str,
    safe: bool,
    answer_released: bool,
    tool_count: int,
    plan_id: str,
) -> None:
    corpus = load_corpus(CORPUS_ROOT)
    spec = next(
        item
        for item in RUNNER._trace_canary_specs(corpus, "a" * 64)
        if item.architecture_id == architecture_id and item.case_id == case_id
    )
    case = corpus.case_map("development")[case_id]

    class Provider:
        def complete(self, **_: Any) -> InferenceCompletion:
            raise AssertionError("deterministic canary paths must never invoke inference")

    executor = EvidenceGuideV2ProviderExecutor(
        corpus=corpus,
        provider=Provider(),  # type: ignore[arg-type]
        entity="entity",
        project="gi-scenario-studio-agent-evals",
        pricing={},
    )
    observer = _CapabilityObserver()

    outcome = executor.execute(spec=spec, case=case, observer=observer)

    assert outcome.status == "completed"
    assert outcome.trace_acknowledged is True
    assert outcome.provider_invocations == 0
    assert outcome.safe is safe
    assert outcome.details["answer_released"] is answer_released
    assert len(outcome.details["tool_events"]) == tool_count
    assert outcome.explanation_plan_id == plan_id
    assert observer.trace.expected_operations == (0, tool_count, answer_released)


def _fake_trace_canary() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    corpus = load_corpus(CORPUS_ROOT)
    specs = RUNNER._trace_canary_specs(corpus, "a" * 64)
    executor = PerfectFakeExecutor()
    observer = _CapabilityObserver()

    rows = [
        executor.execute(
            spec=spec,
            case=corpus.case_map(spec.split)[spec.case_id],
            observer=observer,
        ).as_record(spec)
        for spec in specs
    ]
    cloud_receipts = [
        {
            "group": group,
            "completed": len(selected),
            "reused": 0,
            "cloud": {
                "attempt_count": len(selected),
                "group_sha256": json_sha256(group),
            },
        }
        for group, selected in RUNNER.group_specs(specs, time_block="block-1")
    ]
    return rows, cloud_receipts


def test_r2_trace_canary_is_outside_denominator_and_exercises_four_trace_shapes() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    specs = RUNNER._trace_canary_specs(corpus, "a" * 64)
    rows, cloud_receipts = _fake_trace_canary()
    receipt = RUNNER._validate_trace_canary(rows, cloud_receipts)

    assert len(specs) == 4
    assert {spec.architecture_id for spec in specs} == {"D0", "D1", "A1"}
    assert all(spec.stage == "trace_canary" for spec in specs)
    assert all(spec.time_block == "block-1" for spec in specs)
    assert receipt["passed"] is True
    assert receipt["scientific_denominator_attempts"] == 0
    assert all(receipt["checks"].values())


@pytest.mark.parametrize(
    ("row_selector", "mutation", "failed_check"),
    [
        (
            ("D1", "egv2-dev-unsupported-002"),
            lambda row: row.update(repository_fidelity=False),
            "d1_tool_span_acknowledged",
        ),
        (
            ("A1", "egv2-dev-unsupported-002"),
            lambda row: row["details"].update(answer_released=False),
            "a1_model_and_tool_spans_acknowledged",
        ),
        (
            ("A1", "egv2-dev-unsupported-002"),
            lambda row: row.update(explanation_plan_id="wrong_plan_v1"),
            "a1_model_and_tool_spans_acknowledged",
        ),
    ],
)
def test_trace_canary_rejects_unfaithful_or_unreleased_repository_answers(
    row_selector: tuple[str, str], mutation: Any, failed_check: str
) -> None:
    rows, cloud_receipts = _fake_trace_canary()
    row = next(
        item
        for item in rows
        if (item["architecture_id"], item["case_id"]) == row_selector
    )
    mutation(row)

    receipt = RUNNER._validate_trace_canary(rows, cloud_receipts)

    assert receipt["passed"] is False
    assert receipt["checks"][failed_check] is False


def test_trace_canary_fails_cleanly_on_missing_row_or_cloud_receipt() -> None:
    rows, cloud_receipts = _fake_trace_canary()

    missing_row = RUNNER._validate_trace_canary(rows[:-1], cloud_receipts)
    missing_cloud = RUNNER._validate_trace_canary(rows, cloud_receipts[:-1])

    assert missing_row["passed"] is False
    assert missing_row["checks"]["four_unique_attempts"] is False
    assert missing_cloud["passed"] is False
    assert missing_cloud["checks"]["three_exact_cloud_block_receipts"] is False


def test_trace_canary_attempt_ids_are_bound_to_the_r2_campaign_instance() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    first = RUNNER._trace_canary_specs(corpus, "a" * 64)
    second = RUNNER._trace_canary_specs(corpus, "b" * 64)

    assert [item.attempt_id for item in first] != [item.attempt_id for item in second]
    assert [
        (item.architecture_id, item.case_id, item.provider_steps) for item in first
    ] == [
        (item.architecture_id, item.case_id, item.provider_steps) for item in second
    ]


def test_trace_canary_is_explicitly_preregistered_and_budgeted_once() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    pricing = load_pricing_snapshot(CORPUS_ROOT / "inference-pricing-2026-08-06.json")
    preregistration = build_preregistration(
        corpus,
        pricing=pricing,
        source_hashes={"test_source": "a" * 64},
    )
    canary = preregistration["stages"]["trace_canary"]
    plan = RUNNER._plan(corpus, pricing)

    assert canary["attempt_count"] == 4
    assert canary["scientific_denominator_attempts"] == 0
    assert canary["time_blocks"] == ["block-1"]
    assert canary["must_pass_before"] == "architecture"
    assert canary["budget_envelope"] == {
        "logical_provider_invocations": 1,
        "worst_case_transport_attempts": 2,
        "worst_case_input_tokens": 4096,
        "worst_case_output_tokens": 256,
        "worst_case_cost_usd": "0.000231",
    }
    assert plan["stages"]["trace_canary"] == {
        "attempts": 4,
        "scientific_denominator_attempts": 0,
        **canary["budget_envelope"],
    }


def test_scientific_stages_require_a_passed_trace_canary(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "campaign.sqlite")
    with pytest.raises(RuntimeError, match="TRACE_CANARY_MUST_PASS"):
        RUNNER._require_passed_trace_canary(store)

    store.put_receipt("trace_canary", {"passed": False})
    with pytest.raises(RuntimeError, match="TRACE_CANARY_MUST_PASS"):
        RUNNER._require_passed_trace_canary(store)
    store.close()


def test_real_batch_can_fail_fast_after_persisting_trace_infrastructure_errors(
    tmp_path: Path,
) -> None:
    corpus = load_corpus(CORPUS_ROOT)
    specs = stage_attempts("capability", corpus)[:2]
    store = CampaignStore(tmp_path / "campaign.sqlite")
    prices = {
        spec.model_id: ModelPrice(
            input_per_million=Decimal(1),
            output_per_million=Decimal(1),
            source="https://wandb.ai/test",
            observed_at="2026-08-07T00:00:00Z",
        )
        for spec in specs
    }
    reserve_stage("capability", specs, pricing=prices, store=store)

    class Executor:
        def execute(self, *, spec: AttemptSpec, **_: Any) -> AttemptOutcome:
            return AttemptOutcome(
                attempt_id=spec.attempt_id,
                status="failed_closed",
                provider_invocations=2,
                transport_attempts=2,
                input_tokens=10,
                output_tokens=2,
                cost_usd=Decimal(0),
                latency_ms=1,
                protocol_valid=True,
                safe=False,
                evidence_valid=False,
                repository_fidelity=False,
                injection_safe=False,
                causal=None,
                intent_correct=None,
                safety_intent_correct=None,
                false_refusal=None,
                trace_acknowledged=False,
                error_codes=("WEAVE_AGENT_TURN_INCOMPLETE",),
            )

    class Observer:
        @contextmanager
        def attempt(self, _spec: AttemptSpec, _public_input: Mapping[str, Any]) -> Iterator[Any]:
            yield None

        def log_outcome(self, _spec: AttemptSpec, _outcome: AttemptOutcome) -> None:
            return None

    with pytest.raises(RuntimeError, match="CAMPAIGN_INFRASTRUCTURE_FAILURE"):
        run_attempt_batch(
            store=store,
            specs=specs,
            corpus=corpus,
            executor=Executor(),  # type: ignore[arg-type]
            observer=Observer(),  # type: ignore[arg-type]
            abort_on_error_prefixes=("WEAVE_",),
        )

    assert len(store.attempts("capability")) == 2
    assert store.uncertain_attempts("capability") == []
    store.close()


def test_provider_attempt_crash_keeps_full_envelope_and_restart_never_retries(
    tmp_path: Path,
) -> None:
    corpus = load_corpus(CORPUS_ROOT)
    spec = next(
        item
        for item in stage_attempts(
            "architecture", corpus, retained_models=[CANDIDATE_MODELS[0]]
        )
        if item.architecture_id == "A1"
    )
    price = ModelPrice(
        input_per_million=Decimal("1.25"),
        output_per_million=Decimal("2.50"),
        source="https://wandb.ai/test",
        observed_at="2026-08-07T00:00:00Z",
    )
    path = tmp_path / "campaign.sqlite"
    store = CampaignStore(path)
    reserve_stage(
        "architecture", [spec], pricing={spec.model_id: price}, store=store
    )

    class CrashingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **_: Any) -> AttemptOutcome:
            self.calls += 1
            transport_view = CampaignStore(path)
            uncertain = transport_view.uncertain_attempts("architecture")
            transport_view.close()
            assert [row["attempt_id"] for row in uncertain] == [spec.attempt_id]
            raise RuntimeError("simulated process death before save_attempt")

    executor = CrashingExecutor()
    observer = _CapabilityObserver()
    with pytest.raises(RuntimeError, match="simulated process death"):
        run_attempt_batch(
            store=store,
            specs=[spec],
            corpus=corpus,
            executor=executor,  # type: ignore[arg-type]
            observer=observer,
        )

    expected_cost = price.worst_case(
        input_tokens=spec.provider_steps * 2 * MAX_INPUT_TOKENS_PER_CALL,
        output_tokens=spec.provider_steps * 2 * spec.max_output_tokens,
    )
    expected_usage = {
        "cost_usd": str(expected_cost),
        "provider_invocations": spec.provider_steps,
        "transport_attempts": spec.provider_steps * 2,
        "input_tokens": spec.provider_steps * 2 * MAX_INPUT_TOKENS_PER_CALL,
        "output_tokens": spec.provider_steps * 2 * spec.max_output_tokens,
    }
    assert store.attempts("architecture") == []
    assert store.usage() == expected_usage
    store.close()

    restarted = CampaignStore(path)

    class MustNotRun:
        def execute(self, **_: Any) -> AttemptOutcome:
            executor.calls += 1
            raise AssertionError("uncertain provider attempt was retried")

    with pytest.raises(RuntimeError, match="CAMPAIGN_PROVIDER_ATTEMPT_UNCERTAIN"):
        run_attempt_batch(
            store=restarted,
            specs=[spec],
            corpus=corpus,
            executor=MustNotRun(),  # type: ignore[arg-type]
            observer=observer,
        )
    assert executor.calls == 1
    assert restarted.usage() == expected_usage
    with pytest.raises(RuntimeError, match="CAMPAIGN_PROVIDER_ATTEMPT_UNCERTAIN"):
        reserve_stage(
            "architecture",
            [spec],
            pricing={spec.model_id: price},
            store=restarted,
        )
    restarted.close()


def test_saved_provider_outcome_atomically_replaces_uncertain_retry_envelope(
    tmp_path: Path,
) -> None:
    corpus = load_corpus(CORPUS_ROOT)
    spec = next(
        item
        for item in stage_attempts(
            "architecture", corpus, retained_models=[CANDIDATE_MODELS[0]]
        )
        if item.architecture_id == "A1"
    )
    price = ModelPrice(
        input_per_million=Decimal(5),
        output_per_million=Decimal(10),
        source="https://wandb.ai/test",
        observed_at="2026-08-07T00:00:00Z",
    )
    store = CampaignStore(tmp_path / "campaign.sqlite")
    path = store.path
    reserve_stage(
        "architecture", [spec], pricing={spec.model_id: price}, store=store
    )

    class ExactExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **_: Any) -> AttemptOutcome:
            self.calls += 1
            transport_view = CampaignStore(path)
            assert [
                row["attempt_id"]
                for row in transport_view.uncertain_attempts("architecture")
            ] == [spec.attempt_id]
            transport_view.close()
            return AttemptOutcome(
                attempt_id=spec.attempt_id,
                status="completed",
                provider_invocations=1,
                transport_attempts=1,
                input_tokens=17,
                output_tokens=5,
                input_token_envelope=21,
                output_token_envelope=7,
                cost_usd=Decimal("0.000123"),
                latency_ms=4,
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

    executor = ExactExecutor()
    completed, reused = run_attempt_batch(
        store=store,
        specs=[spec],
        corpus=corpus,
        executor=executor,  # type: ignore[arg-type]
        observer=_CapabilityObserver(),
    )

    assert (completed, reused) == (1, 0)
    assert executor.calls == 1
    assert store.uncertain_attempts("architecture") == []
    assert store.usage() == {
        "cost_usd": "0.000123",
        "provider_invocations": 1,
        "transport_attempts": 1,
        "input_tokens": 21,
        "output_tokens": 7,
    }
    assert run_attempt_batch(
        store=store,
        specs=[spec],
        corpus=corpus,
        executor=executor,  # type: ignore[arg-type]
        observer=_CapabilityObserver(),
    ) == (0, 1)
    assert executor.calls == 1
    store.close()


def test_prior_campaign_usage_counts_toward_actual_and_reserved_caps(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "campaign.sqlite")
    amendment_body = {
        "receipt_kind": "evidence_guide_v2_protocol_amendment",
        "cumulative_prior_usage": {
            "cost_usd": "0.013686",
            "provider_invocations": 290,
            "transport_attempts": 290,
            "input_tokens": 242_189,
            "output_tokens": 37_120,
        },
    }
    amendment = {
        **amendment_body,
        "receipt_sha256": json_sha256(amendment_body),
    }
    store.set_prior_usage(
        protocol_amendment=amendment,
    )
    store.reserve(
        {
            "stage": "architecture",
            "cost_usd": "1.25",
            "nominal_invocations": 10,
            "worst_case_invocations": 20,
            "input_tokens": 100,
            "output_tokens": 50,
        }
    )

    assert store.usage() == {
        "cost_usd": "0.013686",
        "provider_invocations": 290,
        "transport_attempts": 290,
        "input_tokens": 242_189,
        "output_tokens": 37_120,
    }
    assert store.reserved_budget() == {
        "cost_usd": "1.263686",
        "logical_provider_calls": 300,
        "provider_invocations": 310,
        "transport_attempts": 310,
        "input_tokens": 242_289,
        "output_tokens": 37_170,
    }
    with pytest.raises(RuntimeError, match="immutable campaign metadata drifted"):
        changed_body = {
            **amendment_body,
            "cumulative_prior_usage": {
                **amendment_body["cumulative_prior_usage"],
                "cost_usd": "0.013687",
            },
        }
        store.set_prior_usage(
            protocol_amendment={
                **changed_body,
                "receipt_sha256": json_sha256(changed_body),
            },
        )
    store.close()


def test_d1_is_non_oracle_and_changes_with_repository_tool_value() -> None:
    dataset, row = _causal_case()
    packet = provider_packet(dataset, row, 7)
    guarded = GoldGuard(row)
    tool_name = _repository_tool_name(packet)
    assert tool_name is not None
    positive = _repository_tool_result(guarded, tool_name=tool_name, attempt_index=7)
    positive_plan = deterministic_plan_id(packet, retrieved_evidence=positive)
    positive_output = render_repository_plan(
        packet,
        guarded,
        positive_plan,
        attempt_index=7,
        retrieved_evidence=positive,
    )

    changed = copy.deepcopy(row)
    changed["claim_graph"]["tool_claims"][0]["value"] = -0.08
    negative = _repository_tool_result(GoldGuard(changed), tool_name=tool_name, attempt_index=7)
    negative_plan = deterministic_plan_id(packet, retrieved_evidence=negative)
    negative_output = render_repository_plan(
        packet,
        GoldGuard(changed),
        negative_plan,
        attempt_index=7,
        retrieved_evidence=negative,
    )

    assert positive_plan == "recorded_rules_fail_v1"
    assert negative_plan == "recorded_rules_pass_v1"
    assert positive_output["answer"] != negative_output["answer"]
    assert positive_output["numeric_claims"][0]["value"] == 0.08
    assert negative_output["numeric_claims"][0]["value"] == -0.08


def test_architecture_ablation_is_balanced_two_cases_per_config() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    specs = stage_attempts("architecture", corpus, retained_models=CANDIDATE_MODELS[:3])
    ablations = [item for item in specs if item.ablation_condition]
    by_config: dict[tuple[str, str], set[str]] = {}
    for item in ablations:
        by_config.setdefault((item.model_id, item.architecture_id), set()).add(item.case_id)
    assert len(by_config) == 9
    assert all(len(case_ids) == 2 for case_ids in by_config.values())
    assert {item.ablation_condition for item in ablations} == {
        "catalog_only",
        "tool_only",
        "both",
        "withheld_tool",
        "perturbed_value",
        "perturbed_id",
        "stale_packet",
        "wrong_anchor",
        "contradictory_evidence",
        "counterfactual_positive",
        "counterfactual_nonpositive",
    }


def test_signed_counterfactuals_land_on_opposite_rule_sides() -> None:
    _dataset, row = _causal_case()
    tool_name = _repository_tool_name(
        {
            "question": row["question"],
            "available_tools": row["available_tools"],
        }
    )
    assert tool_name is not None
    ordinary = _repository_tool_result(row, tool_name=tool_name, attempt_index=3)
    positive = EvidenceGuideV2ProviderExecutor._conditioned_tool_result(
        row, 3, ordinary, "counterfactual_positive"
    )
    nonpositive = EvidenceGuideV2ProviderExecutor._conditioned_tool_result(
        row, 3, ordinary, "counterfactual_nonpositive"
    )
    threshold = float(row["expectations"]["causal_rule"]["threshold"])
    assert float(positive["value"]) > threshold
    assert float(nonpositive["value"]) <= threshold


def test_maximal_token_envelope_reserves_every_transport_attempt() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    prices = {
        model: type("Price", (), {"worst_case": lambda self, **_: 0})()
        for model in CANDIDATE_MODELS
    }
    specs = stage_attempts("capability", corpus)
    estimate = estimate_provider_budget(specs, pricing=prices)  # type: ignore[arg-type]
    assert estimate["worst_case_input_tokens"] == (
        estimate["worst_case_transport_attempts"] * MAX_INPUT_TOKENS_PER_CALL
    )
    assert estimate["worst_case_transport_attempts"] == (
        estimate["logical_provider_invocations"] * 2
    )


def test_hidden_budget_comes_from_public_receipt_without_rows() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    assert "hidden" not in corpus.splits
    assert corpus.split_counts["hidden"] == 300
    winner = type(
        "Winner",
        (),
        {
            "architecture_id": "A1",
            "model_id": CANDIDATE_MODELS[0],
            "max_output_tokens": 256,
        },
    )()
    price = type("Price", (), {"worst_case": lambda self, **_: 0})()
    estimate = estimate_hidden_receipt_budget(
        corpus,
        winner,  # type: ignore[arg-type]
        pricing={CANDIDATE_MODELS[0]: price},  # type: ignore[dict-item]
    )
    assert estimate["attempts"] == 1_500
    assert estimate["logical_provider_invocations"] == 1_500
    assert estimate["source"] == "public_hidden_lockbox_receipt"


def test_every_visible_production_eligible_payload_fits_retry_envelope() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    longest_model = max(CANDIDATE_MODELS, key=len)
    observed: list[int] = []
    for split in ("development", "validation"):
        dataset = corpus.datasets[split]
        for row in dataset["rows"]:
            packet = provider_packet(dataset, row, 0)
            capability_messages = [
                {
                    "role": "system",
                    "content": (
                        "Call exactly one supplied read-only evidence tool with an empty "
                        "argument object. Do not answer."
                    ),
                },
                {"role": "user", "content": "Inspect the evidence record."},
            ]
            observed.append(
                len(
                    build_inference_request_payload(
                        model=longest_model,
                        messages=capability_messages,
                        tools=_tool_definitions(sorted(row["available_tools"])),
                        max_tokens=128,
                    )
                )
            )
            observed.append(
                len(
                    build_inference_request_payload(
                        model=longest_model,
                        messages=_schema_messages(packet, prompt_id="checklist"),
                        response_schema=_PLAN_SCHEMA,
                        max_tokens=256,
                    )
                )
            )
            route_context = {
                "task": "select_read_only_evidence_tools",
                "intent": deterministic_question_intent(packet["question"]),
                "question": packet["question"],
                "anchor_kind": packet["anchor"].get("kind", "alternative"),
                "available_tool_aliases": sorted(row["available_tools"]),
                "rules": [
                    "Call one to four tools and do not answer the question.",
                    "Target evidence is available only through the tool result.",
                    "Never request another subject or invent an identifier.",
                ],
            }
            route_messages = [
                {
                    "role": "system",
                    "content": (
                        "Route a bounded, read-only engineering evidence investigation. "
                        "Use the supplied tools; do not answer the question."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(route_context, sort_keys=True, separators=(",", ":")),
                },
            ]
            observed.append(
                len(
                    build_inference_request_payload(
                        model=longest_model,
                        messages=route_messages,
                        tools=_tool_definitions(row["available_tools"]),
                        max_tokens=256,
                    )
                )
            )
            intent_messages = [
                {
                    "role": "system",
                    "content": (
                        "Classify one bounded, read-only engineering evidence question. "
                        "Return only the strict JSON intent object; do not explain or answer it."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "select_intent",
                            "question": packet["question"],
                            "allowed_intents": list(INTENT_IDS),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ]
            observed.append(
                len(
                    build_inference_request_payload(
                        model=longest_model,
                        messages=intent_messages,
                        response_schema=_INTENT_SCHEMA,
                        max_tokens=256,
                    )
                )
            )
    assert observed
    assert max(observed) <= MAX_INPUT_TOKENS_PER_CALL


def test_preregistered_full_a1_confirmatory_branch_fits_every_hard_cap() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    price = type("Price", (), {"worst_case": lambda self, **_: 0})()
    prices = {
        model: price
        for model in (
            *CANDIDATE_MODELS,
            "deepseek-ai/DeepSeek-V4-Pro",
            "google/gemma-4-31B-it",
        )
    }
    model = CANDIDATE_MODELS[0]
    architecture_finalists = (
        FinalistConfig(model, "A1", "checklist", 128),
        FinalistConfig(model, "A2", "checklist", 128),
    )
    confirmatory_finalists = (
        FinalistConfig(model, "A1", "checklist", 128),
        FinalistConfig(model, "A1", "counterexample", 128),
    )
    winner = confirmatory_finalists[0]
    visible_specs = [
        *stage_attempts("capability", corpus),
        *stage_attempts("architecture", corpus, retained_models=[model]),
        *stage_attempts("prompt_config", corpus, finalist_configs=architecture_finalists),
        *stage_attempts("validation", corpus, finalist_configs=confirmatory_finalists),
        *stage_attempts("judge_comparison", corpus),
        *stage_attempts("soak", corpus, winner=winner),
    ]
    visible = estimate_provider_budget(visible_specs, pricing=prices)  # type: ignore[arg-type]
    hidden = estimate_hidden_receipt_budget(
        corpus,
        winner,
        pricing=prices,  # type: ignore[arg-type]
    )
    transport = int(visible["worst_case_transport_attempts"]) + int(
        hidden["worst_case_transport_attempts"]
    )
    input_tokens = int(visible["worst_case_input_tokens"]) + int(hidden["worst_case_input_tokens"])
    output_tokens = int(visible["worst_case_output_tokens"]) + int(
        hidden["worst_case_output_tokens"]
    )
    assert transport == 11_986
    assert input_tokens == 24_547_328
    assert output_tokens == 1_755_392
    assert transport <= 12_000
    assert input_tokens <= 30_000_000
    assert output_tokens <= 2_000_000


def test_validation_has_exactly_five_attempts_and_three_blocks_per_scenario() -> None:
    corpus = load_corpus(CORPUS_ROOT)
    finalists = (
        FinalistConfig(CANDIDATE_MODELS[0], "A1", "checklist", 128),
        FinalistConfig(CANDIDATE_MODELS[0], "A1", "counterexample", 128),
    )
    specs = stage_attempts("validation", corpus, finalist_configs=finalists)
    by_config_case: dict[tuple[str, str], list[Any]] = {}
    for spec in specs:
        by_config_case.setdefault(
            (
                f"{spec.model_id}|{spec.architecture_id}|{spec.prompt_id}|{spec.max_output_tokens}",
                spec.case_id,
            ),
            [],
        ).append(spec)
    assert len(by_config_case) == 240
    assert all(len(values) == 5 for values in by_config_case.values())
    assert all(
        {item.time_block for item in values} == {"block-1", "block-2", "block-3"}
        for values in by_config_case.values()
    )


def test_public_stage_package_never_contains_hidden_attempt_material(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "campaign.sqlite")
    spec = AttemptSpec(
        attempt_id="a" * 32,
        stage="hidden",
        split="development",
        case_id="sealed-case-id",
        project_family="sealed-project-family",
        model_id=CANDIDATE_MODELS[0],
        architecture_id="A1",
        prompt_id="checklist",
        max_output_tokens=128,
        repeat=0,
        time_block="block-1",
        hidden=True,
    )
    store.save_attempt(
        spec,
        AttemptOutcome(
            attempt_id=spec.attempt_id,
            status="completed",
            provider_invocations=1,
            transport_attempts=1,
            input_tokens=1,
            output_tokens=1,
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
            details={
                "materialized_output": {"answer": "sealed-answer"},
                "tool_events": [{"result": "sealed-result"}],
            },
        ),
    )
    export_stage_tables(store, tmp_path / "public")
    package = "".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "public").iterdir()
        if path.is_file()
    )
    store.close()
    assert "sealed-case-id" not in package
    assert "sealed-project-family" not in package
    assert "sealed-answer" not in package
    assert "sealed-result" not in package
