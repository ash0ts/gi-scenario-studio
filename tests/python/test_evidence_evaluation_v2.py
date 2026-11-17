from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
from gi_scenario_core.evidence_evaluation_v2 import (
    FINITE_PLAN_IDS,
    PLAN_REGISTRY,
    PLAN_REGISTRY_SHA256,
    PROTOCOL_SHA256,
    SCORER_VERSION,
    SPLIT_COUNTS,
    audit_provider_packet,
    build_causal_counterfactual,
    build_reference_output,
    clustered_bootstrap_interval,
    evaluate_attempts,
    expected_tool_result,
    extract_quantities,
    load_split,
    mutation_suite,
    one_sided_zero_failure_upper_bound,
    provider_packet,
    render_plan,
    scenario_ever_fails,
    score_attempt,
    score_causal_pair,
    sha256_file,
    unexplained_numeric_tokens,
    validate_split_isolation,
    wilson_interval,
)
from gi_scenario_core.evidence_guide import (
    PLAN_REGISTRY_SHA256 as RUNTIME_PLAN_REGISTRY_SHA256,
)
from gi_scenario_core.evidence_guide_v2_protocol import (
    DETERMINISTIC_TOOL_PLANS,
    NON_DOWNGRADABLE_SAFETY_INTENTS,
    deterministic_question_intent,
)
from gi_scenario_core.evidence_guide_v2_protocol import (
    EXPLANATION_PLANS as RUNTIME_EXPLANATION_PLANS,
)
from gi_scenario_core.evidence_guide_v2_protocol import (
    PROTOCOL_SHA256 as SHARED_PROTOCOL_SHA256,
)

ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = ROOT / "benchmarks" / "evidence_guide" / "v2"
_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "evidence_guide_v2_corpus_builder",
    ROOT / "scripts" / "build_evidence_guide_v2_corpus.py",
)
assert _BUILDER_SPEC is not None and _BUILDER_SPEC.loader is not None
_BUILDER = importlib.util.module_from_spec(_BUILDER_SPEC)
_BUILDER_SPEC.loader.exec_module(_BUILDER)
build_split = _BUILDER.build_split
build_public = _BUILDER.build_public
register_protected_hidden_receipt = _BUILDER.register_protected_hidden_receipt


def _tool_events(row: dict, attempt_index: int = 0) -> tuple[dict, ...]:
    if row["scenario_type"] != "causal_retrieval":
        return ()
    return (
        {
            "tool_name": row["expectations"]["required_tool_plan"][0],
            "result": expected_tool_result(row, attempt_index),
        },
    )


def test_v2_public_splits_match_preregistered_composition_and_hashes() -> None:
    manifest = json.loads((V2_ROOT / "manifest.json").read_text(encoding="utf-8"))
    datasets = [load_split("development"), load_split("validation")]
    validate_split_isolation(datasets)

    for dataset in datasets:
        split = dataset["split"]
        assert dataset["row_count"] == len(dataset["rows"]) == sum(SPLIT_COUNTS[split].values())
        assert Counter(row["scenario_type"] for row in dataset["rows"]) == SPLIT_COUNTS[split]
        assert manifest["splits"][split]["sha256"] == sha256_file(V2_ROOT / f"{split}.json")
        assert all(
            4
            <= sum(
                c["role"] == "distractor"
                for c in [*row["claim_graph"]["catalog_claims"], *row["claim_graph"]["tool_claims"]]
            )
            <= 12
            for row in dataset["rows"]
        )

    assert not (
        {row["project_family"] for row in datasets[0]["rows"]}
        & {row["project_family"] for row in datasets[1]["rows"]}
    )
    assert not (
        {row["question_template_family"] for row in datasets[0]["rows"]}
        & {row["question_template_family"] for row in datasets[1]["rows"]}
    )
    assert manifest["scorer_version"] == SCORER_VERSION
    assert manifest["shared_protocol_sha256"] == PROTOCOL_SHA256 == SHARED_PROTOCOL_SHA256
    assert manifest["plan_registry_sha256"] == PLAN_REGISTRY_SHA256 == RUNTIME_PLAN_REGISTRY_SHA256


def test_evaluator_and_runtime_use_one_finite_protocol() -> None:
    assert set(PLAN_REGISTRY) == FINITE_PLAN_IDS == set(RUNTIME_EXPLANATION_PLANS)
    for split in ("development", "validation"):
        dataset = load_split(split)
        for row in dataset["rows"]:
            expected = row["expectations"]
            assert deterministic_question_intent(row["question"]) == expected["intent"]
            assert (
                expected["intent"]
                in RUNTIME_EXPLANATION_PLANS[expected["explanation_plan_id"]].intents
            )
            assert (
                tuple(expected["required_tool_plan"])
                == DETERMINISTIC_TOOL_PLANS[expected["intent"]]
            )


def test_a2_cannot_downgrade_a_shared_safety_intent() -> None:
    dataset = load_split("development")
    row = next(
        row
        for row in dataset["rows"]
        if row["expectations"]["intent"] in NON_DOWNGRADABLE_SAFETY_INTENTS
    )
    output = build_reference_output(dataset, row)
    output["intent_id"] = "general_evidence"
    result = score_attempt(dataset, row, output, agent_variant_id="A2")
    assert result["passed"] is False
    assert result["safety_intent_correct"] is False
    assert "safety_intent_downgrade" in result["error_codes"]


def _normalized_question(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def test_questions_and_semantic_templates_are_isolated_across_splits() -> None:
    development = load_split("development")
    validation = load_split("validation")
    dev_questions = {_normalized_question(row["question"]) for row in development["rows"]}
    val_questions = {_normalized_question(row["question"]) for row in validation["rows"]}
    assert len(dev_questions) == development["row_count"]
    assert len(val_questions) == validation["row_count"]
    assert dev_questions.isdisjoint(val_questions)

    question_leak = deepcopy(validation)
    question_leak["rows"][0]["question"] = "  " + development["rows"][0][
        "question"
    ].upper().replace("?", " ? ")
    with pytest.raises(ValueError, match="question leakage"):
        validate_split_isolation([development, question_leak])

    template_leak = deepcopy(validation)
    leaked_suffix = development["rows"][0]["question_template_family"].split(":", 1)[1]
    template_leak["rows"][0]["question_template_family"] = f"validation:{leaked_suffix}"
    template_leak["question_template_families"] = sorted(
        {row["question_template_family"] for row in template_leak["rows"]}
    )
    with pytest.raises(ValueError, match="normalized question-template leakage"):
        validate_split_isolation([development, template_leak])


def test_public_builder_cannot_generate_a_release_hidden_set(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="protected workflow"):
        build_split("hidden", "even-a-secret-seed-is-insufficient")
    manifest = build_public(tmp_path)
    receipt = json.loads((tmp_path / "hidden-lockbox-receipt.json").read_text())
    assert manifest["hidden"]["release_eligible"] is False
    assert receipt["status"] == "AWAITING_PROTECTED_LOCKBOX"
    assert "content_sha256" not in receipt
    assert not (tmp_path / "hidden.json").exists()


def test_only_an_aggregate_protected_workflow_receipt_can_be_registered(tmp_path: Path) -> None:
    protected_receipt = {
        "schema_version": "2.0",
        "dataset_id": "gi-evidence-guide-architecture-evaluation",
        "dataset_version": "v2",
        "scorer_version": SCORER_VERSION,
        "shared_protocol_sha256": PROTOCOL_SHA256,
        "plan_registry_sha256": PLAN_REGISTRY_SHA256,
        "split": "hidden",
        "status": "SEALED_PROTECTED_LOCKBOX",
        "sealed": True,
        "release_eligible": True,
        "protected_generation": True,
        "public_builder_can_regenerate": False,
        "local_rows_checked_into_git": False,
        "row_count": 300,
        "composition": SPLIT_COUNTS["hidden"],
        "content_sha256": "1" * 64,
        "canonical_payload_sha256": "2" * 64,
        "protected_workflow_receipt_sha256": "3" * 64,
        "lockbox_artifact_ref": "protected-artifact:lockbox:v1",
    }
    source = tmp_path / "protected-receipt.json"
    source.write_text(json.dumps(protected_receipt), encoding="utf-8")
    output = tmp_path / "registered"
    registered = register_protected_hidden_receipt(source, output)
    assert registered["release_eligible"] is True
    assert json.loads((output / "hidden-lockbox-receipt.json").read_text()) == protected_receipt

    leaked = deepcopy(protected_receipt)
    leaked["questions"] = ["secret question"]
    source.write_text(json.dumps(leaked), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot contain lockbox rows"):
        register_protected_hidden_receipt(source, output)


def test_provider_visible_claims_do_not_reveal_relevance_or_distractor_status() -> None:
    for split in ("development", "validation"):
        dataset = load_split(split)
        for row in dataset["rows"]:
            packet = provider_packet(dataset, row, 0)
            for claim in packet["evidence_catalog"]:
                assert "role" not in claim
                assert claim["authority"] != "distractor_evidence"
                assert not claim["label"].casefold().startswith("context evidence")
                assert "target" not in claim["label"].casefold()


def test_hidden_lockbox_receipt_publishes_hash_and_aggregate_only() -> None:
    receipt = json.loads((V2_ROOT / "hidden-lockbox-receipt.json").read_text(encoding="utf-8"))
    manifest = json.loads((V2_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert receipt["sealed"] is False
    assert receipt["release_eligible"] is False
    assert receipt["status"] == "AWAITING_PROTECTED_LOCKBOX"
    assert receipt["expected_row_count"] == 300
    assert receipt["expected_composition"] == SPLIT_COUNTS["hidden"]
    assert receipt["local_rows_checked_into_git"] is False
    assert receipt["public_builder_can_regenerate"] is False
    assert "rows" not in receipt
    assert "questions" not in receipt
    assert "claim_graph" not in receipt
    assert "content_sha256" not in receipt
    assert "content_sha256" not in manifest["hidden"]
    assert manifest["hidden"]["receipt_sha256"] == sha256_file(
        V2_ROOT / "hidden-lockbox-receipt.json"
    )


def test_provider_packets_remap_ids_per_attempt_without_answer_key_leakage() -> None:
    for split in ("development", "validation"):
        dataset = load_split(split)
        for row in dataset["rows"]:
            packet_zero = provider_packet(dataset, row, 0)
            packet_one = provider_packet(dataset, row, 1)
            audit_provider_packet(dataset, row, packet_zero, attempt_index=0)
            audit_provider_packet(dataset, row, packet_one, attempt_index=1)
            ids_zero = {item["evidence_id"] for item in packet_zero["evidence_catalog"]}
            ids_one = {item["evidence_id"] for item in packet_one["evidence_catalog"]}
            assert ids_zero.isdisjoint(ids_one)
            assert all("claim_digest" not in item for item in packet_zero["evidence_catalog"])
            assert all("evidence_binding" in item for item in packet_zero["evidence_catalog"])
            serialized = json.dumps(packet_zero, sort_keys=True)
            assert "expectations" not in serialized
            assert "semantic_id" not in serialized
            assert "answer_summary" not in serialized
            assert "refusal_answer" not in serialized


def test_causal_target_is_absent_until_the_required_tool_executes() -> None:
    dataset = load_split("development")
    row = next(row for row in dataset["rows"] if row["scenario_type"] == "causal_retrieval")
    initial = provider_packet(dataset, row, 2)
    target_ids = {item["evidence_id"] for item in expected_tool_result(row, 2)}
    assert target_ids.isdisjoint({item["evidence_id"] for item in initial["evidence_catalog"]})

    retrieved = expected_tool_result(row, 2)
    after_tool = provider_packet(dataset, row, 2, retrieved)
    assert {item["evidence_id"] for item in after_tool["retrieved_evidence"]} == target_ids
    audit_provider_packet(dataset, row, after_tool, attempt_index=2)


def test_reference_and_repository_plan_renderer_pass_every_public_case() -> None:
    assert set(PLAN_REGISTRY) == FINITE_PLAN_IDS
    for split in ("development", "validation"):
        dataset = load_split(split)
        for row in dataset["rows"]:
            tool_events = _tool_events(row)
            reference = build_reference_output(dataset, row)
            result = score_attempt(dataset, row, reference, tool_events=tool_events)
            assert result["passed"], (row["case_id"], result["errors"])

            rendered = render_plan(
                dataset,
                row,
                row["expectations"]["explanation_plan_id"],
                tool_events=tool_events,
            )
            result = score_attempt(dataset, row, rendered, tool_events=tool_events)
            assert result["passed"], (row["case_id"], result["errors"])


def test_mutation_gate_detects_every_critical_mutation_and_accepts_controls() -> None:
    result = mutation_suite(load_split("development"))
    assert result["release_gate_passed"] is True
    assert result["safety_mutations_detected"] == result["safety_mutation_count"] == 26
    assert result["benign_controls_passed"] == result["benign_control_count"] == 2
    matrix_mutations = [
        item for item in result["results"] if item["mutation"].startswith("authority_unit_")
    ]
    assert len(matrix_mutations) == 8
    assert all(item["detected"] for item in matrix_mutations)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("negative three percent", [(-3.0, "%")]),
        ("eight percent", [(8.0, "%")]),
        ("point seven cfs", [(0.7, "cfs")]),
        ("roughly three megagrams", [(3.0, "megagram")]),
        ("The exact change is -0.03 cfs.", [(-0.03, "cfs")]),
    ],
)
def test_quantity_extractor_detects_digit_and_spelled_out_values(text: str, expected: list) -> None:
    observed = [(item["value"], item["units"]) for item in extract_quantities(text)]
    assert observed == expected


def test_quantity_extractor_does_not_treat_lowercase_mg_as_million_gallons() -> None:
    assert extract_quantities("The concentration was 3 mg.") == []
    assert unexplained_numeric_tokens("The concentration was 3 mg.") == ["3"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The exact result was 0.07 cfs.", []),
        ("The exact result was 0.07 cfs, plus 999.", ["999"]),
        ("ALT-017 is the selected record.", []),
        ("The shorthand estimate was $54.99k.", ["54.99k"]),
    ],
)
def test_unexplained_numeric_tokens_excludes_ids_but_rejects_bare_values(
    text: str, expected: list[str]
) -> None:
    assert unexplained_numeric_tokens(text) == expected


def test_causal_grounding_requires_a_plan_change_across_rule_boundary() -> None:
    dataset = load_split("development")
    row = next(row for row in dataset["rows"] if row["scenario_type"] == "causal_retrieval")
    positive = build_causal_counterfactual(row, 3, 0.12)
    nonpositive = build_causal_counterfactual(row, 3, -0.01)

    passed = score_causal_pair(
        row,
        3,
        [
            {"tool_event": positive, "selected_plan_id": "recorded_rules_fail_v1"},
            {"tool_event": nonpositive, "selected_plan_id": "recorded_rules_pass_v1"},
        ],
    )
    decorative_only = score_causal_pair(
        row,
        3,
        [
            {"tool_event": positive, "selected_plan_id": "recorded_rules_fail_v1"},
            {"tool_event": nonpositive, "selected_plan_id": "recorded_rules_fail_v1"},
        ],
    )
    always_pass = score_causal_pair(
        row,
        3,
        [
            {"tool_event": positive, "selected_plan_id": "recorded_rules_pass_v1"},
            {"tool_event": nonpositive, "selected_plan_id": "recorded_rules_pass_v1"},
        ],
    )
    assert passed["passed"] is True
    assert passed["plan_changed_with_tool_result"] is True
    assert decorative_only["passed"] is False
    assert decorative_only["plan_changed_with_tool_result"] is False
    assert always_pass["passed"] is False
    assert always_pass["plan_changed_with_tool_result"] is False


def test_canonical_causal_cases_cover_both_guardrail_directions() -> None:
    for split in ("development", "validation"):
        dataset = load_split(split)
        rows = [row for row in dataset["rows"] if row["scenario_type"] == "causal_retrieval"]
        values = [float(row["claim_graph"]["tool_claims"][0]["value"]) for row in rows]
        plans = {row["expectations"]["explanation_plan_id"] for row in rows}
        assert any(value > 0 for value in values)
        assert any(value <= 0 for value in values)
        assert plans == {"recorded_rules_fail_v1", "recorded_rules_pass_v1"}


def test_changed_tool_value_or_id_with_old_binding_fails_closed() -> None:
    dataset = load_split("development")
    row = next(row for row in dataset["rows"] if row["scenario_type"] == "causal_retrieval")
    output = build_reference_output(dataset, row)
    canonical = expected_tool_result(row, 0)

    changed_value = deepcopy(canonical)
    changed_value[0]["value"] = -1.0
    result = score_attempt(
        dataset,
        row,
        output,
        tool_events=(
            {"tool_name": row["expectations"]["required_tool_plan"][0], "result": changed_value},
        ),
    )
    assert "required_tool_not_executed" in result["error_codes"]

    changed_id = deepcopy(canonical)
    changed_id[0]["evidence_id"] = "ev_wrongsubject0000"
    result = score_attempt(
        dataset,
        row,
        output,
        tool_events=(
            {"tool_name": row["expectations"]["required_tool_plan"][0], "result": changed_id},
        ),
    )
    assert "required_tool_not_executed" in result["error_codes"]


def test_scenario_ever_fails_is_the_primary_repeated_attempt_outcome() -> None:
    records = [
        {"case_id": "a", "project_family": "p1", "passed": True},
        {"case_id": "a", "project_family": "p1", "passed": False},
        {"case_id": "b", "project_family": "p2", "passed": True},
        {"case_id": "b", "project_family": "p2", "passed": True},
    ]
    primary = scenario_ever_fails(records)
    assert primary == {
        "scenario_count": 2,
        "scenario_ever_failed_count": 1,
        "scenario_ever_failed_rate": 0.5,
        "failed_case_ids": ["a"],
    }
    low, high = wilson_interval(3, 4)
    assert 0 < low < 0.75 < high < 1
    assert one_sided_zero_failure_upper_bound(300) == pytest.approx(0.009936, rel=1e-3)
    first = clustered_bootstrap_interval(records, iterations=200, seed=17)
    second = clustered_bootstrap_interval(records, iterations=200, seed=17)
    assert first == second


def test_attempt_evaluation_reports_intervals_and_critical_failures() -> None:
    dataset = load_split("development")
    records = []
    for row in dataset["rows"][:4]:
        output = build_reference_output(dataset, row)
        scored = score_attempt(dataset, row, output, tool_events=_tool_events(row))
        records.extend([scored, dict(scored, attempt_index=1)])
    evaluation = evaluate_attempts(dataset, records)
    assert evaluation["attempt_count"] == 8
    assert evaluation["primary_outcome"]["scenario_count"] == 4
    assert evaluation["primary_outcome"]["scenario_ever_failed_count"] == 0
    assert evaluation["zero_failure_one_sided_95_upper_bound"] is not None


def test_judge_calibration_has_40_pairs_and_both_orders() -> None:
    calibration = json.loads((V2_ROOT / "judge-calibration.json").read_text(encoding="utf-8"))
    assert calibration["pair_count"] == 40
    assert calibration["presentation_count"] == 80
    assert len(calibration["pairs"]) == 40
    assert len(calibration["presentations"]) == 80
    grouped = Counter(item["pair_id"] for item in calibration["presentations"])
    assert set(grouped.values()) == {2}
    for pair in calibration["pairs"]:
        presentations = [
            item for item in calibration["presentations"] if item["pair_id"] == pair["pair_id"]
        ]
        assert {item["better"] for item in presentations} == {"A", "B"}


def test_public_corpus_files_do_not_contain_paths_credentials_or_customer_data() -> None:
    for name in (
        "development.json",
        "validation.json",
        "judge-calibration.json",
        "manifest.json",
        "hidden-lockbox-receipt.json",
    ):
        payload = (V2_ROOT / name).read_text(encoding="utf-8")
        lowered = payload.casefold()
        assert "/users/" not in lowered
        assert "wandb_api_key" not in lowered
        assert "authorization" not in lowered
        assert "customer" not in lowered
        assert hashlib.sha256(payload.encode()).hexdigest() == sha256_file(V2_ROOT / name)
