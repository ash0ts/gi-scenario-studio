"""Build the self-authored CC0 Evidence Guide R4 grounding corpus.

The corpus is deliberately synthetic.  It tests whether an explanation system
uses the right repository record when applying a simple, pinned screening
rule; it is not evidence about any drainage system, model, or GI practice.

Every scenario is expanded into four matched provider packets:

* the original target value;
* the same target moved across its decision threshold;
* the target withheld while distractors remain; and
* the original target with one distractor perturbed.

Gold plans and intervention identities live only in ``evaluator_only``.  They
are never copied into a ``provider_payload``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, Final

BUILDER_VERSION: Final = "evidence-guide-r4-corpus-builder-1"
SCHEMA_VERSION: Final = "1.0"
DATASET_ID: Final = "gi-evidence-guide-grounding-r4-cc0"
DATASET_VERSION: Final = "r4.0"
RANDOM_SEED: Final = 417_204

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "benchmarks" / "evidence_guide" / "r4" / "corpus.json"

CONDITIONS: Final = (
    "original_target",
    "target_threshold_crossing",
    "target_withheld",
    "distractor_only_perturbation",
)
PLAN_SEMANTICS: Final = ("rule_met", "rule_not_met", "insufficient_evidence")

PROJECTS: Final = {
    "cedar_trace_cc0": {
        "display_name": "Cedar Trace synthetic screening fixture",
        "project_code": "CT",
    },
    "brookside_cc0": {
        "display_name": "Brookside synthetic screening fixture",
        "project_code": "BK",
    },
    "pine_ward_cc0": {
        "display_name": "Pine Ward synthetic screening fixture",
        "project_code": "PW",
    },
    "harbor_annex_cc0": {
        "display_name": "Harbor Annex synthetic screening fixture",
        "project_code": "HA",
    },
}

# Six scenarios per fictional project. Each project has three <= and three >=
# rules, plus three original passes and three original failures.
BLUEPRINTS: Final = (
    # Cedar Trace
    ("cedar_trace_cc0", "outfall_peak_flow_delta", "OUT-1", "Selected-storm outfall peak-flow change", "cfs", "<=", 0.02, 0.07, -0.03, "synthetic_model_metric"),
    ("cedar_trace_cc0", "outfall_volume_reduction", "OUT-1", "Selected-event outfall-volume reduction", "%", ">=", 6.0, 6.42, 5.58, "synthetic_derived_metric"),
    ("cedar_trace_cc0", "present_value_cost", "ALT-207", "Synthetic present-value screening cost", "USD", "<=", 200_000.0, 190_250.0, 209_750.0, "synthetic_cost_basis"),
    ("cedar_trace_cc0", "minimum_node_freeboard", "J-14", "Minimum selected-storm node freeboard", "ft", ">=", 1.0, 0.74, 1.26, "synthetic_model_metric"),
    ("cedar_trace_cc0", "flood_volume", "J-09", "Selected-event modeled flood volume", "Mgal", "<=", 0.015, 0.021, 0.009, "synthetic_model_metric"),
    ("cedar_trace_cc0", "impervious_area_capture", "BIO-2", "Modeled impervious-area capture", "%", ">=", 12.0, 14.5, 9.5, "synthetic_derived_metric"),
    # Brookside
    ("brookside_cc0", "flood_duration", "J-22", "Selected-event modeled flood duration", "min", "<=", 15.0, 11.0, 19.0, "synthetic_model_metric"),
    ("brookside_cc0", "surcharge_duration", "J-18", "Selected-event surcharge duration", "min", "<=", 30.0, 38.0, 22.0, "synthetic_model_metric"),
    ("brookside_cc0", "capacity_duration", "C-07", "Conduit capacity-exceedance duration", "min", "<=", 20.0, 16.0, 24.0, "synthetic_model_metric"),
    ("brookside_cc0", "runoff_volume_reduction", "OUT-2", "Selected-event runoff-volume reduction", "%", ">=", 8.0, 6.5, 9.5, "synthetic_derived_metric"),
    ("brookside_cc0", "final_surface_storage", "RG-1", "Final modeled surface storage", "acre-ft", ">=", 0.03, 0.038, 0.022, "synthetic_runoff_ledger"),
    ("brookside_cc0", "minimum_outlet_freeboard", "OUT-2", "Minimum selected-storm outlet freeboard", "ft", ">=", 0.75, 0.60, 0.90, "synthetic_model_metric"),
    # Pine Ward
    ("pine_ward_cc0", "outfall_peak_flow", "OUT-3", "Selected-storm outfall peak flow", "cfs", "<=", 8.5, 8.2, 8.8, "synthetic_model_metric"),
    ("pine_ward_cc0", "flood_rate", "J-31", "Maximum selected-event flood rate", "cfs", "<=", 0.25, 0.33, 0.17, "synthetic_model_metric"),
    ("pine_ward_cc0", "overflow_duration", "OF-1", "Selected-event overflow duration", "min", "<=", 10.0, 8.0, 12.0, "synthetic_model_metric"),
    ("pine_ward_cc0", "retained_event_volume", "PP-4", "Modeled retained event volume", "acre-ft", ">=", 0.05, 0.041, 0.059, "synthetic_runoff_ledger"),
    ("pine_ward_cc0", "peak_attenuation", "OUT-3", "Selected-storm peak attenuation", "%", ">=", 5.0, 6.3, 3.7, "synthetic_derived_metric"),
    ("pine_ward_cc0", "permeable_area_capture", "PP-4", "Modeled permeable-area capture", "%", ">=", 20.0, 17.0, 23.0, "synthetic_derived_metric"),
    # Harbor Annex
    ("harbor_annex_cc0", "peak_hgl", "J-42", "Selected-storm peak hydraulic grade", "ft", "<=", 102.5, 103.1, 101.9, "synthetic_model_metric"),
    ("harbor_annex_cc0", "outfall_event_volume", "OUT-4", "Selected-event outfall volume", "Mgal", "<=", 0.22, 0.205, 0.235, "synthetic_model_metric"),
    ("harbor_annex_cc0", "conduit_capacity_ratio", "C-19", "Maximum conduit capacity ratio", "ratio", "<=", 0.85, 0.79, 0.91, "synthetic_model_metric"),
    ("harbor_annex_cc0", "flood_free_junction_share", "NETWORK", "Flood-free junction share", "%", ">=", 95.0, 93.5, 96.5, "synthetic_derived_metric"),
    ("harbor_annex_cc0", "infiltration_loss", "S-12", "SWMM runoff-accounting infiltration loss", "acre-ft", ">=", 0.40, 0.44, 0.36, "synthetic_runoff_ledger"),
    ("harbor_annex_cc0", "storage_recovery", "RB-6", "Modeled storage recovery before next event", "%", ">=", 90.0, 86.0, 94.0, "synthetic_derived_metric"),
)

DISTRACTOR_POOL: Final = (
    ("outfall_peak_flow", "OUT-X", "Neighboring-outfall peak flow", "cfs", 4.70, "synthetic_model_metric"),
    ("outfall_volume_reduction", "OUT-Y", "Other-event outfall-volume reduction", "%", 3.80, "synthetic_derived_metric"),
    ("present_value_cost", "ALT-X", "Different alternative screening cost", "USD", 132_000.0, "synthetic_cost_basis"),
    ("minimum_node_freeboard", "J-X", "Other-node minimum freeboard", "ft", 1.42, "synthetic_model_metric"),
    ("flood_volume", "J-Y", "Other-node modeled flood volume", "Mgal", 0.047, "synthetic_model_metric"),
    ("flood_duration", "J-Z", "Other-event flood duration", "min", 26.0, "synthetic_model_metric"),
    ("surcharge_duration", "J-W", "Other-node surcharge duration", "min", 44.0, "synthetic_model_metric"),
    ("conduit_capacity_ratio", "C-X", "Other-conduit capacity ratio", "ratio", 0.72, "synthetic_model_metric"),
    ("final_surface_storage", "LID-X", "Other-practice final surface storage", "acre-ft", 0.018, "synthetic_runoff_ledger"),
    ("peak_hgl", "J-V", "Other-node peak hydraulic grade", "ft", 97.8, "synthetic_model_metric"),
)

LIMITATION_TEXT: Final = {
    "synthetic_fixture": "This is a fictional, self-authored evaluator fixture.",
    "modeled_not_measured": "The quantity is modeled rather than measured in the field.",
    "selected_events_only": "The quantity applies only to the stated synthetic event scope.",
    "screening_cost_not_bid": "The synthetic screening cost is not a bid or constructability finding.",
    "infiltration_loss_not_recharge": "SWMM infiltration loss is a runoff-accounting term, not measured groundwater recharge.",
}


def canonical_json(value: Any) -> bytes:
    """Return the corpus canonical representation used for every digest."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _opaque_id(namespace: str, scenario_id: str, ordinal: int) -> str:
    digest = hashlib.sha256(
        f"{DATASET_ID}:{namespace}:{scenario_id}:{ordinal}".encode()
    ).hexdigest()
    return f"{namespace}_{digest[:16]}"


def _plan_map(scenario_id: str) -> dict[str, str]:
    values = [
        _opaque_id("plan", scenario_id, ordinal) for ordinal in range(len(PLAN_SEMANTICS))
    ]
    random.Random(f"{RANDOM_SEED}:{scenario_id}:plans").shuffle(values)
    return dict(zip(PLAN_SEMANTICS, values, strict=True))


def _limitations(metric: str, authority: str) -> list[str]:
    values = ["synthetic_fixture", "modeled_not_measured", "selected_events_only"]
    if authority == "synthetic_cost_basis":
        values.append("screening_cost_not_bid")
    if metric == "infiltration_loss":
        values.append("infiltration_loss_not_recharge")
    return values


def _claim(
    *,
    scenario_id: str,
    ordinal: int,
    family: str,
    metric: str,
    object_id: str,
    label: str,
    units: str,
    value: float,
    authority: str,
) -> dict[str, Any]:
    evidence_id = _opaque_id("ev", scenario_id, ordinal)
    source_record = {
        "asset_id": f"cc0:{family}:screening-ledger-v1",
        "record_id": _opaque_id("record", scenario_id, ordinal),
        "source_kind": "self_authored_synthetic_repository_record",
    }
    return {
        "evidence_id": evidence_id,
        "object_id": object_id,
        "metric": metric,
        "label": label,
        "value": value,
        "units": units,
        "authority": authority,
        "source": {
            **source_record,
            "record_sha256": sha256_json(
                {
                    **source_record,
                    "metric": metric,
                    "object_id": object_id,
                    "units": units,
                    "value": value,
                }
            ),
        },
        "limitation_ids": _limitations(metric, authority),
    }


def _distractors(*, scenario_id: str, family: str, scenario_index: int) -> list[dict[str, Any]]:
    count = 4 + scenario_index % 5
    start = (scenario_index * 3) % len(DISTRACTOR_POOL)
    selected = [DISTRACTOR_POOL[(start + offset) % len(DISTRACTOR_POOL)] for offset in range(count)]
    claims: list[dict[str, Any]] = []
    for offset, (metric, object_id, label, units, base, authority) in enumerate(selected, 1):
        scale = 1.0 + ((scenario_index + offset) % 7 - 3) * 0.04
        value = round(float(base) * scale, 6)
        claims.append(
            _claim(
                scenario_id=scenario_id,
                ordinal=offset,
                family=family,
                metric=metric,
                object_id=f"{object_id}-{scenario_index + 1:02d}",
                label=label,
                units=units,
                value=value,
                authority=authority,
            )
        )
    return claims


def _meets_rule(value: float, operator: str, threshold: float) -> bool:
    if operator == "<=":
        return value <= threshold
    if operator == ">=":
        return value >= threshold
    raise ValueError(f"unsupported operator: {operator}")


def _provider_payload(
    *,
    scenario_id: str,
    family: str,
    question: str,
    target_metric: str,
    target_object_id: str,
    operator: str,
    threshold: float,
    units: str,
    plans: Mapping[str, str],
    claims: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    shuffled = [deepcopy(dict(item)) for item in claims]
    random.Random(f"{RANDOM_SEED}:{scenario_id}:evidence-order").shuffle(shuffled)
    return {
        "schema_version": SCHEMA_VERSION,
        "task": "apply_one_screening_rule_to_retrieved_evidence",
        "question": question,
        "anchor": {
            "project_family_id": family,
            "analysis_id": _opaque_id("analysis", scenario_id, 0),
            "subject_id": _opaque_id("subject", scenario_id, 0),
        },
        "decision_target": {
            "object_id": target_object_id,
            "metric": target_metric,
        },
        "decision_rule": {
            "operator": operator,
            "threshold": threshold,
            "units": units,
            "if_rule_met_plan_id": plans["rule_met"],
            "if_rule_not_met_plan_id": plans["rule_not_met"],
            "if_evidence_missing_plan_id": plans["insufficient_evidence"],
        },
        "retrieved_evidence": shuffled,
        "response_contract": {
            "type": "object",
            "additional_properties": False,
            "required": ["plan_id", "evidence_ids"],
            "plan_id_enum": sorted(plans.values()),
            "evidence_ids_policy": "cite only evidence used; use an empty list when missing",
        },
        "boundary": {
            "scope": "synthetic screening evidence only",
            "prohibited_inferences": [
                "field performance",
                "calibration",
                "regulatory compliance",
                "design readiness",
                "ecological benefit",
            ],
        },
    }


def _condition(
    *,
    condition_id: str,
    provider_payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "condition_id": condition_id,
        "provider_payload": deepcopy(dict(provider_payload)),
        "provider_payload_sha256": sha256_json(provider_payload),
    }


def _scenario(blueprint: Sequence[Any], scenario_index: int) -> dict[str, Any]:
    (
        family,
        metric,
        object_id,
        label,
        units,
        operator,
        threshold,
        original_value,
        crossing_value,
        authority,
    ) = blueprint
    code = str(PROJECTS[family]["project_code"])
    scenario_id = f"r4-{code.lower()}-{scenario_index + 1:02d}"
    plans = _plan_map(scenario_id)
    target = _claim(
        scenario_id=scenario_id,
        ordinal=0,
        family=family,
        metric=metric,
        object_id=object_id,
        label=label,
        units=units,
        value=original_value,
        authority=authority,
    )
    crossing_target = deepcopy(target)
    crossing_target["value"] = crossing_value
    crossing_source = dict(crossing_target["source"])
    crossing_source["record_sha256"] = sha256_json(
        {
            "asset_id": crossing_source["asset_id"],
            "record_id": crossing_source["record_id"],
            "source_kind": crossing_source["source_kind"],
            "metric": metric,
            "object_id": object_id,
            "units": units,
            "value": crossing_value,
        }
    )
    crossing_target["source"] = crossing_source
    distractors = _distractors(
        scenario_id=scenario_id,
        family=family,
        scenario_index=scenario_index,
    )
    perturbed_distractors = deepcopy(distractors)
    perturbed = perturbed_distractors[scenario_index % len(perturbed_distractors)]
    perturbation_precision = 2 if perturbed["units"] == "USD" else 6
    perturbed["value"] = round(
        float(perturbed["value"]) * 1.13,
        perturbation_precision,
    )
    perturbed_source = dict(perturbed["source"])
    perturbed_source["record_sha256"] = sha256_json(
        {
            "asset_id": perturbed_source["asset_id"],
            "record_id": perturbed_source["record_id"],
            "source_kind": perturbed_source["source_kind"],
            "metric": perturbed["metric"],
            "object_id": perturbed["object_id"],
            "units": perturbed["units"],
            "value": perturbed["value"],
        }
    )
    perturbed["source"] = perturbed_source

    question = (
        f"Using only the retrieved synthetic repository records, which plan follows for "
        f"{label.lower()} at {object_id}?"
    )
    payload_arguments = {
        "scenario_id": scenario_id,
        "family": family,
        "question": question,
        "target_metric": metric,
        "target_object_id": object_id,
        "operator": operator,
        "threshold": threshold,
        "units": units,
        "plans": plans,
    }
    original_payload = _provider_payload(
        **payload_arguments,
        claims=[target, *distractors],
    )
    # Clone one shuffled packet so evidence order cannot become an accidental
    # second intervention. The withheld cell preserves the relative order of
    # every distractor after removing the target.
    crossing_payload = deepcopy(original_payload)
    withheld_payload = deepcopy(original_payload)
    distractor_payload = deepcopy(original_payload)
    crossing_payload["retrieved_evidence"] = [
        deepcopy(crossing_target) if item["evidence_id"] == target["evidence_id"] else item
        for item in crossing_payload["retrieved_evidence"]
    ]
    withheld_payload["retrieved_evidence"] = [
        item
        for item in withheld_payload["retrieved_evidence"]
        if item["evidence_id"] != target["evidence_id"]
    ]
    perturbed_by_id = {item["evidence_id"]: item for item in perturbed_distractors}
    distractor_payload["retrieved_evidence"] = [
        deepcopy(perturbed_by_id[item["evidence_id"]])
        if item["evidence_id"] == perturbed["evidence_id"]
        else item
        for item in distractor_payload["retrieved_evidence"]
    ]

    original_passes = _meets_rule(original_value, operator, threshold)
    crossing_passes = _meets_rule(crossing_value, operator, threshold)
    if original_passes == crossing_passes:
        raise RuntimeError(f"counterfactual does not cross threshold: {scenario_id}")
    expected_original = plans["rule_met" if original_passes else "rule_not_met"]
    expected_crossing = plans["rule_met" if crossing_passes else "rule_not_met"]
    return {
        "scenario_id": scenario_id,
        "project_family": family,
        "project_display_name": PROJECTS[family]["display_name"],
        "metric_category": metric,
        "synthetic_notice": (
            "Fictional screening evidence for agent evaluation; modeled quantities are not "
            "measurements or claims about a real drainage system."
        ),
        "conditions": [
            _condition(condition_id="original_target", provider_payload=original_payload),
            _condition(
                condition_id="target_threshold_crossing",
                provider_payload=crossing_payload,
            ),
            _condition(condition_id="target_withheld", provider_payload=withheld_payload),
            _condition(
                condition_id="distractor_only_perturbation",
                provider_payload=distractor_payload,
            ),
        ],
        "evaluator_only": {
            "target_evidence_id": target["evidence_id"],
            "perturbed_distractor_evidence_id": perturbed["evidence_id"],
            "original_rule_result": original_passes,
            "expected_plan_by_condition": {
                "original_target": expected_original,
                "target_threshold_crossing": expected_crossing,
                "target_withheld": plans["insufficient_evidence"],
                "distractor_only_perturbation": expected_original,
            },
            "expected_evidence_ids_by_condition": {
                "original_target": [target["evidence_id"]],
                "target_threshold_crossing": [target["evidence_id"]],
                "target_withheld": [],
                "distractor_only_perturbation": [target["evidence_id"]],
            },
            "semantic_plan_map": plans,
            "intervention_contract": {
                "original_target": "no evidence mutation",
                "target_threshold_crossing": "target value and its record hash only",
                "target_withheld": "target availability only",
                "distractor_only_perturbation": "one distractor value and its record hash only",
            },
        },
    }


def _body() -> dict[str, Any]:
    scenarios = [_scenario(item, index) for index, item in enumerate(BLUEPRINTS)]
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "title": "Evidence Guide R4 synthetic grounding corpus",
        "description": (
            "Self-authored matched-condition corpus for measuring evidence sensitivity, "
            "distractor specificity, and missing-evidence abstention."
        ),
        "license": {
            "spdx_id": "CC0-1.0",
            "declaration": (
                "To the extent possible under law, the authors waive all copyright and "
                "related rights in this fictional corpus under CC0 1.0."
            ),
            "source_policy": "Entirely self-authored fictional evaluator records.",
        },
        "scientific_boundary": {
            "measures": [
                "evidence sensitivity",
                "distractor specificity",
                "missing-evidence abstention",
                "response-contract reliability",
            ],
            "does_not_measure": [
                "hydraulic accuracy",
                "calibration",
                "field performance",
                "ecological benefit",
                "regulatory compliance",
                "practitioner usefulness",
            ],
        },
        "condition_ids": list(CONDITIONS),
        "limitation_registry": LIMITATION_TEXT,
        "project_families": PROJECTS,
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
    }


def build_corpus() -> dict[str, Any]:
    body = _body()
    scenario_hashes = {
        str(row["scenario_id"]): sha256_json(row) for row in body["scenarios"]
    }
    manifest = {
        "algorithm": "sha256",
        "canonicalization": "UTF-8 JSON; sorted keys; compact separators; no NaN",
        "builder_version": BUILDER_VERSION,
        "corpus_body_sha256": sha256_json(body),
        "scenario_hashes": scenario_hashes,
        "scenario_hashes_sha256": sha256_json(scenario_hashes),
        "provider_payload_hashes_sha256": sha256_json(
            {
                row["scenario_id"]: {
                    condition["condition_id"]: condition["provider_payload_sha256"]
                    for condition in row["conditions"]
                }
                for row in body["scenarios"]
            }
        ),
    }
    corpus = {**body, "manifest": manifest}
    validate_corpus(corpus)
    return corpus


def _claim_map(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item["evidence_id"]): item for item in payload["retrieved_evidence"]
    }


def validate_corpus(corpus: Mapping[str, Any]) -> None:
    """Fail closed on construction drift or a compromised matched design."""

    scenarios = corpus.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 24:
        raise ValueError("R4 corpus requires exactly 24 scenarios")
    identifiers = [str(row.get("scenario_id")) for row in scenarios]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("R4 scenario identifiers must be unique")
    families = Counter(str(row.get("project_family")) for row in scenarios)
    if families != Counter({family: 6 for family in PROJECTS}):
        raise ValueError("R4 corpus requires six scenarios per project family")

    operators: Counter[str] = Counter()
    original_results: Counter[bool] = Counter()
    for row in scenarios:
        conditions = row.get("conditions")
        if not isinstance(conditions, list) or [item.get("condition_id") for item in conditions] != list(CONDITIONS):
            raise ValueError(f"condition drift: {row.get('scenario_id')}")
        by_condition = {str(item["condition_id"]): item for item in conditions}
        for item in conditions:
            payload = item.get("provider_payload")
            if not isinstance(payload, Mapping):
                raise TypeError("provider payload is missing")
            if item.get("provider_payload_sha256") != sha256_json(payload):
                raise ValueError("provider payload hash drift")
            serialized = canonical_json(payload).decode("utf-8")
            if any(token in serialized for token in ("evaluator_only", "expected_plan", "condition_id", "gold_answer")):
                raise ValueError("evaluator-only information leaked to provider payload")
            claims = payload.get("retrieved_evidence")
            if not isinstance(claims, list):
                raise TypeError("retrieved evidence must be a list")
            for claim in claims:
                if not claim.get("units") or not claim.get("source") or not claim.get("limitation_ids"):
                    raise ValueError("every claim requires units, source, and limitations")
        original = by_condition["original_target"]["provider_payload"]
        crossing = by_condition["target_threshold_crossing"]["provider_payload"]
        withheld = by_condition["target_withheld"]["provider_payload"]
        distractor = by_condition["distractor_only_perturbation"]["provider_payload"]
        rule = original["decision_rule"]
        threshold = float(rule["threshold"])
        operator = str(rule["operator"])
        if threshold == 0 or operator not in {"<=", ">="}:
            raise ValueError("rules require nonzero thresholds and supported operators")
        operators[operator] += 1
        target_id = str(row["evaluator_only"]["target_evidence_id"])
        original_claims = _claim_map(original)
        crossing_claims = _claim_map(crossing)
        withheld_claims = _claim_map(withheld)
        distractor_claims = _claim_map(distractor)
        distractor_count = len(original_claims) - 1
        if not 4 <= distractor_count <= 8:
            raise ValueError("each scenario requires four to eight distractors")
        if target_id not in original_claims or target_id not in crossing_claims or target_id not in distractor_claims or target_id in withheld_claims:
            raise ValueError("target availability intervention is invalid")
        if set(original_claims) != set(crossing_claims) or set(original_claims) != set(distractor_claims):
            raise ValueError("non-withheld cells must retain evidence identity")
        if set(withheld_claims) != set(original_claims) - {target_id}:
            raise ValueError("withheld cell must remove only the target")
        original_order = [item["evidence_id"] for item in original["retrieved_evidence"]]
        crossing_order = [item["evidence_id"] for item in crossing["retrieved_evidence"]]
        withheld_order = [item["evidence_id"] for item in withheld["retrieved_evidence"]]
        distractor_order = [item["evidence_id"] for item in distractor["retrieved_evidence"]]
        if crossing_order != original_order or distractor_order != original_order:
            raise ValueError("non-withheld evidence order drift")
        if withheld_order != [item for item in original_order if item != target_id]:
            raise ValueError("withheld evidence order drift")
        for key in set(original) - {"retrieved_evidence"}:
            if crossing[key] != original[key] or withheld[key] != original[key] or distractor[key] != original[key]:
                raise ValueError("provider packet invariant drift")
        changed_crossing = [key for key in original_claims if original_claims[key] != crossing_claims[key]]
        changed_distractor = [key for key in original_claims if original_claims[key] != distractor_claims[key]]
        perturbed_id = str(row["evaluator_only"]["perturbed_distractor_evidence_id"])
        if changed_crossing != [target_id] or changed_distractor != [perturbed_id]:
            raise ValueError("semantic intervention changed the wrong claim")
        original_value = float(original_claims[target_id]["value"])
        crossing_value = float(crossing_claims[target_id]["value"])
        original_pass = _meets_rule(original_value, operator, threshold)
        crossing_pass = _meets_rule(crossing_value, operator, threshold)
        if original_pass == crossing_pass:
            raise ValueError("target intervention failed to cross threshold")
        original_results[original_pass] += 1
        expected = row["evaluator_only"]["expected_plan_by_condition"]
        plan_map = row["evaluator_only"]["semantic_plan_map"]
        if expected["original_target"] != plan_map["rule_met" if original_pass else "rule_not_met"]:
            raise ValueError("original evaluator plan is invalid")
        if expected["target_threshold_crossing"] != plan_map["rule_met" if crossing_pass else "rule_not_met"]:
            raise ValueError("counterfactual evaluator plan is invalid")
        if expected["target_withheld"] != plan_map["insufficient_evidence"]:
            raise ValueError("withheld evaluator plan is invalid")
        if expected["distractor_only_perturbation"] != expected["original_target"]:
            raise ValueError("distractor perturbation changed the expected plan")

    if operators != Counter({"<=": 12, ">=": 12}):
        raise ValueError("R4 operators must be balanced")
    if original_results != Counter({True: 12, False: 12}):
        raise ValueError("R4 original outcomes must be balanced")

    body = {key: value for key, value in corpus.items() if key != "manifest"}
    manifest = corpus.get("manifest")
    if not isinstance(manifest, Mapping):
        raise TypeError("hash manifest is missing")
    scenario_hashes = {str(row["scenario_id"]): sha256_json(row) for row in scenarios}
    provider_payload_hashes = {
        str(row["scenario_id"]): {
            str(item["condition_id"]): str(item["provider_payload_sha256"])
            for item in row["conditions"]
        }
        for row in scenarios
    }
    if manifest.get("corpus_body_sha256") != sha256_json(body):
        raise ValueError("corpus body hash drift")
    if manifest.get("scenario_hashes") != scenario_hashes:
        raise ValueError("scenario hash drift")
    if manifest.get("scenario_hashes_sha256") != sha256_json(scenario_hashes):
        raise ValueError("scenario hash-manifest drift")
    if manifest.get("provider_payload_hashes_sha256") != sha256_json(
        provider_payload_hashes
    ):
        raise ValueError("provider payload hash-manifest drift")


def write_corpus(path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    corpus = build_corpus()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return corpus


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Fail if the checked-in corpus drifts")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    built = build_corpus()
    if arguments.check:
        current = json.loads(arguments.output.read_text(encoding="utf-8"))
        if current != built:
            raise SystemExit("R4_CORPUS_DRIFT")
    else:
        write_corpus(arguments.output)
    print(
        json.dumps(
            {
                "corpus_body_sha256": built["manifest"]["corpus_body_sha256"],
                "output": str(arguments.output),
                "scenario_count": built["scenario_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
