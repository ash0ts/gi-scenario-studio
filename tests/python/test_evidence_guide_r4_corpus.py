from __future__ import annotations

import importlib.util
import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = ROOT / "benchmarks" / "evidence_guide" / "r4" / "corpus.json"
BUILDER_PATH = ROOT / "scripts" / "build_evidence_guide_r4_corpus.py"
_SPEC = importlib.util.spec_from_file_location("evidence_guide_r4_corpus_builder", BUILDER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(BUILDER)


def _load() -> dict[str, Any]:
    value = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _conditions(scenario: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(item["condition_id"]): item for item in scenario["conditions"]}


def _claims(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item["evidence_id"]): item for item in payload["retrieved_evidence"]
    }


def _rule_result(value: float, operator: str, threshold: float) -> bool:
    return value <= threshold if operator == "<=" else value >= threshold


def test_checked_in_r4_corpus_is_a_deterministic_builder_product() -> None:
    checked_in = _load()
    rebuilt = BUILDER.build_corpus()

    assert checked_in == rebuilt
    BUILDER.validate_corpus(checked_in)
    assert checked_in["manifest"]["corpus_body_sha256"] == (
        "5f71147c8fe20e7be3606dcef3c74fb9e4dc105dc0ab1d562d381ec99eea825a"
    )


def test_r4_design_is_balanced_across_families_rules_and_original_outcomes() -> None:
    corpus = _load()
    scenarios = corpus["scenarios"]

    assert len(scenarios) == corpus["scenario_count"] == 24
    assert Counter(row["project_family"] for row in scenarios) == Counter(
        {
            "cedar_trace_cc0": 6,
            "brookside_cc0": 6,
            "pine_ward_cc0": 6,
            "harbor_annex_cc0": 6,
        }
    )
    assert Counter(
        _conditions(row)["original_target"]["provider_payload"]["decision_rule"]["operator"]
        for row in scenarios
    ) == Counter({"<=": 12, ">=": 12})
    assert Counter(row["evaluator_only"]["original_rule_result"] for row in scenarios) == Counter(
        {True: 12, False: 12}
    )

    for family in corpus["project_families"]:
        family_rows = [row for row in scenarios if row["project_family"] == family]
        rules = [
            _conditions(row)["original_target"]["provider_payload"]["decision_rule"]
            for row in family_rows
        ]
        assert Counter(rule["operator"] for rule in rules) == Counter({"<=": 3, ">=": 3})
        assert Counter(row["evaluator_only"]["original_rule_result"] for row in family_rows) == (
            Counter({True: 3, False: 3})
        )
        assert all(float(rule["threshold"]) != 0 for rule in rules)


def test_r4_covers_plausible_stormwater_screening_quantities() -> None:
    metrics = {row["metric_category"] for row in _load()["scenarios"]}

    assert {
        "outfall_peak_flow_delta",
        "outfall_volume_reduction",
        "present_value_cost",
        "minimum_node_freeboard",
        "flood_volume",
        "flood_duration",
        "surcharge_duration",
        "capacity_duration",
        "peak_hgl",
        "conduit_capacity_ratio",
        "infiltration_loss",
        "storage_recovery",
    } <= metrics


def test_every_scenario_has_four_matched_conditions_and_four_to_eight_distractors() -> None:
    for scenario in _load()["scenarios"]:
        cells = _conditions(scenario)
        assert tuple(cells) == BUILDER.CONDITIONS
        evaluator = scenario["evaluator_only"]
        target_id = evaluator["target_evidence_id"]
        perturbed_id = evaluator["perturbed_distractor_evidence_id"]

        original_payload = cells["original_target"]["provider_payload"]
        crossing_payload = cells["target_threshold_crossing"]["provider_payload"]
        withheld_payload = cells["target_withheld"]["provider_payload"]
        distractor_payload = cells["distractor_only_perturbation"]["provider_payload"]
        original = _claims(original_payload)
        crossing = _claims(crossing_payload)
        withheld = _claims(withheld_payload)
        distractor = _claims(distractor_payload)

        assert 4 <= len(original) - 1 <= 8
        assert set(crossing) == set(distractor) == set(original)
        assert set(withheld) == set(original) - {target_id}
        assert [key for key in original if original[key] != crossing[key]] == [target_id]
        assert [key for key in original if original[key] != distractor[key]] == [perturbed_id]
        assert original[target_id] == distractor[target_id]
        original_order = [item["evidence_id"] for item in original_payload["retrieved_evidence"]]
        assert [item["evidence_id"] for item in crossing_payload["retrieved_evidence"]] == (
            original_order
        )
        assert [item["evidence_id"] for item in distractor_payload["retrieved_evidence"]] == (
            original_order
        )
        assert [item["evidence_id"] for item in withheld_payload["retrieved_evidence"]] == [
            evidence_id for evidence_id in original_order if evidence_id != target_id
        ]

        invariant_keys = set(original_payload) - {"retrieved_evidence"}
        for key in invariant_keys:
            assert crossing_payload[key] == original_payload[key]
            assert withheld_payload[key] == original_payload[key]
            assert distractor_payload[key] == original_payload[key]

        rule = original_payload["decision_rule"]
        original_pass = _rule_result(
            float(original[target_id]["value"]),
            str(rule["operator"]),
            float(rule["threshold"]),
        )
        crossing_pass = _rule_result(
            float(crossing[target_id]["value"]),
            str(rule["operator"]),
            float(rule["threshold"]),
        )
        assert original_pass is not crossing_pass

        for claim in original.values():
            assert claim["units"]
            assert claim["source"]["source_kind"] == (
                "self_authored_synthetic_repository_record"
            )
            assert len(claim["source"]["record_sha256"]) == 64
            assert {"synthetic_fixture", "modeled_not_measured"} <= set(
                claim["limitation_ids"]
            )


def test_expected_plans_and_intervention_labels_are_evaluator_only() -> None:
    for scenario in _load()["scenarios"]:
        evaluator = scenario["evaluator_only"]
        expected = evaluator["expected_plan_by_condition"]
        plans = evaluator["semantic_plan_map"]
        assert set(expected) == set(BUILDER.CONDITIONS)
        assert expected["target_withheld"] == plans["insufficient_evidence"]
        assert expected["distractor_only_perturbation"] == expected["original_target"]
        assert expected["target_threshold_crossing"] != expected["original_target"]

        for condition in scenario["conditions"]:
            payload = condition["provider_payload"]
            serialized = BUILDER.canonical_json(payload).decode("utf-8")
            assert "evaluator_only" not in serialized
            assert "expected_plan" not in serialized
            assert "condition_id" not in serialized
            assert condition["condition_id"] not in serialized
            assert all("role" not in claim for claim in payload["retrieved_evidence"])


def test_provider_packets_have_units_sources_limitations_and_no_real_world_authority() -> None:
    prohibited_authorities = {
        "field_measurement",
        "calibrated_model",
        "regulatory_determination",
        "design_approval",
    }
    for scenario in _load()["scenarios"]:
        assert scenario["synthetic_notice"].startswith("Fictional screening evidence")
        for cell in scenario["conditions"]:
            payload = cell["provider_payload"]
            assert payload["boundary"]["scope"] == "synthetic screening evidence only"
            assert payload["decision_rule"]["units"]
            for claim in payload["retrieved_evidence"]:
                assert claim["authority"] not in prohibited_authorities
                assert claim["source"]["asset_id"].startswith("cc0:")
                assert claim["units"]
                assert claim["limitation_ids"]


def test_manifest_is_complete_and_tamper_evident() -> None:
    corpus = _load()
    manifest = corpus["manifest"]
    scenarios = corpus["scenarios"]
    body = {key: value for key, value in corpus.items() if key != "manifest"}
    scenario_hashes = {
        row["scenario_id"]: BUILDER.sha256_json(row) for row in scenarios
    }
    payload_hashes = {
        row["scenario_id"]: {
            cell["condition_id"]: cell["provider_payload_sha256"]
            for cell in row["conditions"]
        }
        for row in scenarios
    }

    assert manifest["algorithm"] == "sha256"
    assert manifest["corpus_body_sha256"] == BUILDER.sha256_json(body)
    assert manifest["scenario_hashes"] == scenario_hashes
    assert manifest["scenario_hashes_sha256"] == BUILDER.sha256_json(scenario_hashes)
    assert manifest["provider_payload_hashes_sha256"] == BUILDER.sha256_json(payload_hashes)

    tampered = deepcopy(corpus)
    tampered["scenarios"][0]["conditions"][0]["provider_payload"]["retrieved_evidence"][0][
        "value"
    ] = 999.0
    with pytest.raises(ValueError, match="provider payload hash drift"):
        BUILDER.validate_corpus(tampered)


def test_cc0_declaration_and_scientific_boundary_are_explicit() -> None:
    corpus = _load()

    assert corpus["license"]["spdx_id"] == "CC0-1.0"
    assert "waive all copyright" in corpus["license"]["declaration"]
    assert corpus["license"]["source_policy"] == (
        "Entirely self-authored fictional evaluator records."
    )
    assert "evidence sensitivity" in corpus["scientific_boundary"]["measures"]
    assert {
        "hydraulic accuracy",
        "field performance",
        "ecological benefit",
        "practitioner usefulness",
    } <= set(corpus["scientific_boundary"]["does_not_measure"])
